#!/usr/bin/env python3
"""Live IMU plot through the VESC Tool TCP server.

Start the bridge first, for example:
    ./vesc_tool --offscreen --vescPort /dev/ttyACM0 --tcpServer 65102

Usage:
    python examples/imu_live_plot.py
    python examples/imu_live_plot.py --tcp 192.168.1.50:65102
    python examples/imu_live_plot.py --scan-udp
"""

from __future__ import annotations

import argparse
import math
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

import matplotlib

# Agg is the default in many headless setups; FuncAnimation + plt.show() needs a GUI backend.
if sys.platform == "darwin":
    _GUI_BACKENDS = ("MacOSX", "TkAgg", "QtAgg", "Qt5Agg")
else:
    _GUI_BACKENDS = ("TkAgg", "QtAgg", "Qt5Agg")

for _name in _GUI_BACKENDS:
    try:
        matplotlib.use(_name, force=True)
        break
    except (ImportError, ValueError):
        continue

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FuncAnimation
from matplotlib.axes import Axes
from matplotlib.artist import Artist
from matplotlib.lines import Line2D

from vesc_py import ImuValues, VescClient, udp_scan

DEFAULT_TCP_ENDPOINT = ("127.0.0.1", 65102)
DEFAULT_MASK = 0x01FF  # roll/pitch/yaw + accelerometer + gyroscope.
DEFAULT_POLL_HZ = 50.0
DEFAULT_REFRESH_HZ = 30.0
DEFAULT_SPECTRUM_REFRESH_HZ = 2.0


@dataclass(frozen=True)
class ImuSample:
    """One timestamped IMU response from the poller thread."""

    timestamp: float
    values: ImuValues


class ImuPoller:
    """Poll IMU data in the background so TCP timeouts do not block the UI."""

    def __init__(
        self,
        client: VescClient,
        *,
        mask: int,
        poll_hz: float,
        max_queue: int = 500,
    ) -> None:
        self._client = client
        self._mask = mask
        self._period = 1.0 / poll_hz
        self._samples: queue.Queue[ImuSample] = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="vesc-imu-poller", daemon=True)
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def drain(self) -> list[ImuSample]:
        samples: list[ImuSample] = []
        while True:
            try:
                samples.append(self._samples.get_nowait())
            except queue.Empty:
                return samples

    def _run(self) -> None:
        next_poll = time.monotonic()
        while not self._stop.is_set():
            try:
                imu = self._client.get_imu_data(self._mask)
            except Exception as exc:  # Keep polling; transient TCP stalls are common enough.
                self._last_error = str(exc)
            else:
                self._last_error = None
                self._put_latest(ImuSample(timestamp=time.monotonic(), values=imu))

            next_poll += self._period
            sleep_s = max(0.0, next_poll - time.monotonic())
            if self._stop.wait(sleep_s):
                return
            if sleep_s == 0.0:
                next_poll = time.monotonic()

    def _put_latest(self, sample: ImuSample) -> None:
        try:
            self._samples.put_nowait(sample)
        except queue.Full:
            try:
                self._samples.get_nowait()
            except queue.Empty:
                pass
            self._samples.put_nowait(sample)


def parse_tcp_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse HOST:PORT for argparse."""
    host, sep, port_str = endpoint.rpartition(":")
    if not sep or not host or not port_str:
        raise argparse.ArgumentTypeError("TCP endpoint must be HOST:PORT")

    try:
        port = int(port_str)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("TCP port must be an integer") from exc

    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("TCP port must be in range 1..65535")

    return host, port


def scan_and_print_udp(timeout: float = 3.0) -> None:
    """Listen for VESC Tool TCP server broadcast announcements and print them."""
    print(f"Listening for VESC Tool TCP server broadcasts on UDP port 65109 for {timeout}s ...")
    devices = udp_scan(timeout=timeout)
    if not devices:
        print("No VESC Tool TCP servers found.")
        return

    for device in devices:
        print(f"  {device.hw_name}  {device.ip}:{device.port}")


def tcp_server_help(endpoint: str, port: int) -> str:
    """Return a concise hint for starting the VESC Tool TCP bridge."""
    return "\n".join(
        [
            f"Could not connect to VESC Tool TCP server at {endpoint}.",
            "",
            "Start VESC Tool with tcpServer enabled, then retry. For example:",
            f"  vesc_tool --offscreen --vescPort /dev/ttyACM0 --tcpServer {port}",
            "",
            "If VESC Tool is already running, check the host and port passed to --tcp.",
        ]
    )


def require_interactive_backend() -> None:
    """Fail early when Matplotlib selected a non-interactive backend."""
    if matplotlib.get_backend().lower() == "agg":
        raise RuntimeError(
            "No interactive matplotlib backend is available (still using Agg). "
            "Install a GUI toolkit (e.g. python3-tk / tkinter, or PyQt5/PyQt6), "
            "ensure DISPLAY is set for X11/Wayland, and unset MPLBACKEND if it forces Agg."
        )


def run_live_plot(
    poller: ImuPoller,
    *,
    history: int = 300,
    refresh_hz: float = DEFAULT_REFRESH_HZ,
    show_freq: bool = False,
    spectrum_refresh_hz: float = DEFAULT_SPECTRUM_REFRESH_HZ,
) -> None:
    """Run a matplotlib live plot of IMU data."""
    require_interactive_backend()

    rad2deg = 180.0 / math.pi
    timestamp_hist: deque[float] = deque(maxlen=history)
    roll_hist: deque[float] = deque(maxlen=history)
    pitch_hist: deque[float] = deque(maxlen=history)
    yaw_hist: deque[float] = deque(maxlen=history)
    ax_hist: deque[float] = deque(maxlen=history)
    ay_hist: deque[float] = deque(maxlen=history)
    az_hist: deque[float] = deque(maxlen=history)
    gx_hist: deque[float] = deque(maxlen=history)
    gy_hist: deque[float] = deque(maxlen=history)
    gz_hist: deque[float] = deque(maxlen=history)
    refresh_timestamp_hist: deque[float] = deque(maxlen=120)
    last_sample_timestamp: float | None = None
    spectrum_enabled = show_freq and spectrum_refresh_hz > 0.0
    spectrum_period = 1.0 / spectrum_refresh_hz if spectrum_enabled else math.inf
    next_spectrum_update = 0.0

    if show_freq:
        fig, axes = plt.subplots(3, 2, figsize=(14, 9), sharex="col")
        (ax_acc, ax_acc_freq), (ax_gyro, ax_gyro_freq), (ax_rpy, ax_rpy_freq) = axes
    else:
        fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
        ax_acc, ax_gyro, ax_rpy = np.asarray(axes).ravel()
        ax_acc_freq = ax_gyro_freq = ax_rpy_freq = None

    fig.suptitle("VESC IMU Live Data")
    status = fig.text(
        0.01,
        0.01,
        "Waiting for IMU data... | Sample: measuring | Plot: measuring",
        fontsize=9,
    )

    x = np.arange(-history + 1, 1)
    zeros = [0.0] * history

    (line_ax,) = ax_acc.plot(x, zeros, label="Acc X")
    (line_ay,) = ax_acc.plot(x, zeros, label="Acc Y")
    (line_az,) = ax_acc.plot(x, zeros, label="Acc Z")
    ax_acc.set_title("Accel Data")
    ax_acc.set_ylabel("g")
    ax_acc.set_ylim(-8, 8)
    ax_acc.legend(loc="upper left")
    ax_acc.grid(True, alpha=0.3)

    (line_gx,) = ax_gyro.plot(x, zeros, label="Gyro X")
    (line_gy,) = ax_gyro.plot(x, zeros, label="Gyro Y")
    (line_gz,) = ax_gyro.plot(x, zeros, label="Gyro Z")
    ax_gyro.set_title("Gyro Data")
    ax_gyro.set_ylabel("Gyro")
    ax_gyro.set_ylim(-2000, 2000)
    ax_gyro.legend(loc="upper left")
    ax_gyro.grid(True, alpha=0.3)

    (line_r,) = ax_rpy.plot(x, zeros, label="Roll")
    (line_p,) = ax_rpy.plot(x, zeros, label="Pitch")
    (line_y,) = ax_rpy.plot(x, zeros, label="Yaw")
    ax_rpy.set_title("RPY Data")
    ax_rpy.set_xlabel("Samples")
    ax_rpy.set_ylabel("Degrees")
    ax_rpy.set_ylim(-200, 200)
    ax_rpy.legend(loc="upper left")
    ax_rpy.grid(True, alpha=0.3)

    acc_freq_lines: tuple[Line2D, Line2D, Line2D] | None = None
    gyro_freq_lines: tuple[Line2D, Line2D, Line2D] | None = None
    rpy_freq_lines: tuple[Line2D, Line2D, Line2D] | None = None
    frequency_artists: tuple[Artist, ...] = ()
    if show_freq:
        assert ax_acc_freq is not None
        assert ax_gyro_freq is not None
        assert ax_rpy_freq is not None

        (line_ax_freq,) = ax_acc_freq.plot([], [], label="Acc X")
        (line_ay_freq,) = ax_acc_freq.plot([], [], label="Acc Y")
        (line_az_freq,) = ax_acc_freq.plot([], [], label="Acc Z")
        ax_acc_freq.set_title("Accel Data Frequency Analysis")
        ax_acc_freq.set_ylabel("Magnitude")
        ax_acc_freq.legend(loc="upper right")
        ax_acc_freq.grid(True, alpha=0.3)
        acc_freq_lines = (line_ax_freq, line_ay_freq, line_az_freq)

        (line_gx_freq,) = ax_gyro_freq.plot([], [], label="Gyro X")
        (line_gy_freq,) = ax_gyro_freq.plot([], [], label="Gyro Y")
        (line_gz_freq,) = ax_gyro_freq.plot([], [], label="Gyro Z")
        ax_gyro_freq.set_title("Gyro Data Frequency Analysis")
        ax_gyro_freq.set_ylabel("Magnitude")
        ax_gyro_freq.legend(loc="upper right")
        ax_gyro_freq.grid(True, alpha=0.3)
        gyro_freq_lines = (line_gx_freq, line_gy_freq, line_gz_freq)

        (line_r_freq,) = ax_rpy_freq.plot([], [], label="Roll")
        (line_p_freq,) = ax_rpy_freq.plot([], [], label="Pitch")
        (line_y_freq,) = ax_rpy_freq.plot([], [], label="Yaw")
        ax_rpy_freq.set_title("RPY Data Frequency Analysis")
        ax_rpy_freq.set_xlabel("Frequency (Hz)")
        ax_rpy_freq.set_ylabel("Magnitude")
        ax_rpy_freq.legend(loc="upper right")
        ax_rpy_freq.grid(True, alpha=0.3)
        rpy_freq_lines = (line_r_freq, line_p_freq, line_y_freq)

        frequency_artists = acc_freq_lines + gyro_freq_lines + rpy_freq_lines

    def _pad(values: deque[float]) -> list[float]:
        padded = list(values)
        return [0.0] * (history - len(padded)) + padded

    def _frequency_bins() -> tuple[int, np.ndarray, np.ndarray] | None:
        sample_count = len(timestamp_hist)
        if sample_count < 2:
            return None

        timestamps = np.asarray(timestamp_hist, dtype=float)
        sample_periods = np.diff(timestamps)
        sample_periods = sample_periods[sample_periods > 0.0]
        if sample_periods.size == 0:
            return None

        sample_period = float(np.median(sample_periods))
        if not np.isfinite(sample_period) or sample_period <= 0.0:
            return None

        if sample_count > 2:
            window = np.hanning(sample_count)
        else:
            window = np.ones(sample_count)

        frequencies = np.fft.rfftfreq(sample_count, d=sample_period)
        return sample_count, frequencies, window

    def _frequency_magnitudes(
        values: deque[float],
        sample_count: int,
        window: np.ndarray,
    ) -> np.ndarray:
        samples = np.asarray(values, dtype=float)[-sample_count:]
        centered = samples - np.mean(samples)
        centered = centered * window

        magnitudes = np.abs(np.fft.rfft(centered)) / sample_count
        if magnitudes.size > 2:
            magnitudes[1:-1] *= 2.0
        return magnitudes

    def _update_frequency_axis(
        axis: Axes,
        lines: tuple[Line2D, Line2D, Line2D],
        values: tuple[deque[float], deque[float], deque[float]],
        bins: tuple[int, np.ndarray, np.ndarray],
    ) -> None:
        sample_count, frequencies, window = bins
        for line, hist in zip(lines, values):
            magnitudes = _frequency_magnitudes(hist, sample_count, window)
            line.set_data(frequencies, magnitudes)

        axis.set_xlim(0.0, float(frequencies[-1]))
        axis.relim()
        axis.autoscale_view(scalex=False, scaley=True)

    def _actual_refresh_hz() -> float | None:
        if len(refresh_timestamp_hist) < 2:
            return None

        elapsed = refresh_timestamp_hist[-1] - refresh_timestamp_hist[0]
        if elapsed <= 0.0:
            return None

        return (len(refresh_timestamp_hist) - 1) / elapsed

    def _actual_sample_hz() -> float | None:
        if len(timestamp_hist) < 2:
            return None

        elapsed = timestamp_hist[-1] - timestamp_hist[0]
        if elapsed <= 0.0:
            return None

        return (len(timestamp_hist) - 1) / elapsed

    def update(_frame: int) -> tuple[Artist, ...]:
        nonlocal last_sample_timestamp, next_spectrum_update

        now = time.monotonic()
        refresh_timestamp_hist.append(now)
        actual_refresh_hz = _actual_refresh_hz()
        samples = poller.drain()
        for sample in samples:
            imu = sample.values
            timestamp_hist.append(sample.timestamp)
            roll_hist.append(imu.roll * rad2deg)
            pitch_hist.append(imu.pitch * rad2deg)
            yaw_hist.append(imu.yaw * rad2deg)
            ax_hist.append(imu.acc_x)
            ay_hist.append(imu.acc_y)
            az_hist.append(imu.acc_z)
            gx_hist.append(imu.gyro_x)
            gy_hist.append(imu.gyro_y)
            gz_hist.append(imu.gyro_z)
            last_sample_timestamp = sample.timestamp

        if samples:
            line_ax.set_ydata(_pad(ax_hist))
            line_ay.set_ydata(_pad(ay_hist))
            line_az.set_ydata(_pad(az_hist))
            line_gx.set_ydata(_pad(gx_hist))
            line_gy.set_ydata(_pad(gy_hist))
            line_gz.set_ydata(_pad(gz_hist))
            line_r.set_ydata(_pad(roll_hist))
            line_p.set_ydata(_pad(pitch_hist))
            line_y.set_ydata(_pad(yaw_hist))

            if spectrum_enabled and now >= next_spectrum_update:
                bins = _frequency_bins()
                if bins is not None:
                    assert ax_acc_freq is not None
                    assert ax_gyro_freq is not None
                    assert ax_rpy_freq is not None
                    assert acc_freq_lines is not None
                    assert gyro_freq_lines is not None
                    assert rpy_freq_lines is not None

                    _update_frequency_axis(
                        ax_acc_freq,
                        acc_freq_lines,
                        (ax_hist, ay_hist, az_hist),
                        bins,
                    )
                    _update_frequency_axis(
                        ax_gyro_freq,
                        gyro_freq_lines,
                        (gx_hist, gy_hist, gz_hist),
                        bins,
                    )
                    _update_frequency_axis(
                        ax_rpy_freq,
                        rpy_freq_lines,
                        (roll_hist, pitch_hist, yaw_hist),
                        bins,
                    )
                    next_spectrum_update = now + spectrum_period

        if actual_refresh_hz is None:
            plot_text = "Plot: measuring"
        else:
            plot_text = f"Plot: {actual_refresh_hz:.1f} Hz actual ({refresh_hz:.1f} Hz target)"

        if poller.last_error is not None and not samples:
            sample_text = f"IMU read error: {poller.last_error}"
        elif last_sample_timestamp is not None:
            actual_sample_hz = _actual_sample_hz()
            if actual_sample_hz is None:
                sample_text = f"Sample: measuring | Age: {now - last_sample_timestamp:.2f}s"
            else:
                sample_text = (
                    f"Sample: {actual_sample_hz:.1f} Hz actual | "
                    f"Age: {now - last_sample_timestamp:.2f}s"
                )
        else:
            sample_text = "Waiting for IMU data..."
        status.set_text(f"{sample_text} | {plot_text}")

        return (
            line_ax,
            line_ay,
            line_az,
            line_gx,
            line_gy,
            line_gz,
            line_r,
            line_p,
            line_y,
            *frequency_artists,
            status,
        )

    interval_ms = 1000.0 / refresh_hz
    _anim = FuncAnimation(
        fig, update, interval=interval_ms, blit=False, cache_frame_data=False
    )
    fig.tight_layout(rect=(0.0, 0.03, 1.0, 0.96))
    plt.show()


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Plot live VESC IMU data through a VESC Tool --tcpServer bridge.",
        epilog=(
            "Start VESC Tool first, for example: "
            "vesc_tool --offscreen --vescPort /dev/ttyACM0 --tcpServer 65102"
        ),
    )
    parser.add_argument(
        "--tcp",
        type=parse_tcp_endpoint,
        default=DEFAULT_TCP_ENDPOINT,
        metavar="HOST:PORT",
        help="VESC Tool TCP server endpoint (default: 127.0.0.1:65102).",
    )
    parser.add_argument(
        "--scan-udp",
        action="store_true",
        help="Scan for VESC Tool TCP server UDP broadcasts and exit.",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=3.0,
        metavar="SEC",
        help="UDP scan duration in seconds (default: 3.0).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.5,
        metavar="SEC",
        help="VESC response timeout in seconds (default: 0.5).",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=DEFAULT_POLL_HZ,
        metavar="HZ",
        help="IMU poll rate in Hz (default: 50).",
    )
    parser.add_argument(
        "--refresh-rate",
        type=float,
        default=DEFAULT_REFRESH_HZ,
        metavar="HZ",
        help="Plot redraw rate in Hz (default: 30).",
    )
    parser.add_argument(
        "--show-freq",
        action="store_true",
        help="Show frequency-analysis plots. Hidden by default for faster redraws.",
    )
    parser.add_argument(
        "--spectrum-refresh-rate",
        type=float,
        default=DEFAULT_SPECTRUM_REFRESH_HZ,
        metavar="HZ",
        help="Frequency-analysis redraw rate in Hz when --show-freq is set (default: 2).",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=300,
        metavar="SAMPLES",
        help="Number of samples visible in the plot (default: 300).",
    )
    parser.add_argument(
        "--mask",
        type=lambda value: int(value, 0),
        default=DEFAULT_MASK,
        metavar="MASK",
        help="IMU field bitmask (default: 0x01ff = RPY + accelerometer + gyroscope).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.scan_udp:
        scan_and_print_udp(args.scan_timeout)
        return

    if args.rate <= 0.0:
        raise SystemExit("--rate must be greater than 0")
    if args.refresh_rate <= 0.0:
        raise SystemExit("--refresh-rate must be greater than 0")
    if args.spectrum_refresh_rate < 0.0:
        raise SystemExit("--spectrum-refresh-rate must be greater than or equal to 0")
    if args.history <= 0:
        raise SystemExit("--history must be greater than 0")
    if args.timeout <= 0.0:
        raise SystemExit("--timeout must be greater than 0")

    host, port = args.tcp
    endpoint = f"{host}:{port}"
    print(f"Connecting to VESC Tool TCP server at {endpoint} ...")
    try:
        client = VescClient.connect_tcp(host, port, timeout=args.timeout)
    except ConnectionRefusedError:
        raise SystemExit(tcp_server_help(endpoint, port)) from None
    except ConnectionError as exc:
        raise SystemExit(
            f"{exc}\n\n"
            "The TCP socket opened, but no VESC firmware response was received. "
            "Make sure VESC Tool is connected to a controller before starting the plot."
        ) from None
    poller = ImuPoller(client, mask=args.mask, poll_hz=args.rate)

    try:
        fw = client.fw_version
        if fw is not None:
            print(f"Firmware: {fw.major}.{fw.minor:02d}  HW: {fw.hw}")
        poller.start()
        run_live_plot(
            poller,
            history=args.history,
            refresh_hz=args.refresh_rate,
            show_freq=args.show_freq,
            spectrum_refresh_hz=args.spectrum_refresh_rate,
        )
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        poller.stop(timeout=args.timeout + 0.1)
        client.close()


if __name__ == "__main__":
    main()
