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
import ctypes
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from vesc_py import ImuValues, VescClient, udp_scan

DEFAULT_TCP_ENDPOINT = ("127.0.0.1", 65102)
DEFAULT_MASK = 0x01FF  # roll/pitch/yaw + accelerometer + gyroscope.
DEFAULT_POLL_HZ = 50.0
DEFAULT_REFRESH_HZ = 30.0
DEFAULT_SPECTRUM_REFRESH_HZ = 2.0
QT_XCB_RUNTIME_LIBS = ("libxcb-cursor.so.0", "libxcb-icccm.so.4")


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


def import_pyqtgraph() -> tuple[Any, Any]:
    """Import PyQtGraph lazily so non-plot commands do not require Qt."""
    if (
        sys.platform.startswith("linux")
        and "QT_QPA_PLATFORM" not in os.environ
        and "DISPLAY" in os.environ
    ):
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    try:
        import pyqtgraph as pg  # type: ignore[import-untyped]
        from pyqtgraph.Qt import QtCore  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "PyQtGraph live plotting requires pyqtgraph and a Qt binding. "
            "Install the project dependencies, or install them directly with "
            "`python -m pip install pyqtgraph PySide6`."
        ) from exc

    return pg, QtCore


def require_qt_platform_runtime() -> None:
    """Fail before QApplication aborts when XCB runtime libraries are missing."""
    if not sys.platform.startswith("linux"):
        return
    if os.environ.get("QT_QPA_PLATFORM") != "xcb":
        return

    missing: list[str] = []
    for lib_name in QT_XCB_RUNTIME_LIBS:
        try:
            ctypes.CDLL(lib_name)
        except OSError:
            missing.append(lib_name)

    if missing:
        raise RuntimeError(
            "Qt's xcb platform plugin is missing runtime libraries: "
            f"{', '.join(missing)}. Run this from the python Nix dev shell "
            "(`nix develop .#python`), or install the matching system packages "
            "(for example libxcb-cursor0 and libxcb-icccm4 on Debian/Ubuntu)."
        )


def run_live_plot(
    poller: ImuPoller,
    *,
    history: int = 300,
    refresh_hz: float = DEFAULT_REFRESH_HZ,
    show_freq: bool = False,
    spectrum_refresh_hz: float = DEFAULT_SPECTRUM_REFRESH_HZ,
) -> None:
    """Run a PyQtGraph live plot of IMU data."""
    pg, QtCore = import_pyqtgraph()
    pg.setConfigOptions(antialias=True)

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

    x = np.arange(-history + 1, 1)
    zeros = np.zeros(history)
    require_qt_platform_runtime()
    app = pg.mkQApp("VESC IMU Live Data")
    window = pg.GraphicsLayoutWidget(title="VESC IMU Live Data")
    window.setWindowTitle("VESC IMU Live Data")
    window.resize(1400 if show_freq else 1000, 900)

    column_count = 2 if show_freq else 1
    title = pg.LabelItem("VESC IMU Live Data", size="14pt", bold=True)
    window.addItem(title, row=0, col=0, colspan=column_count)
    status = pg.LabelItem(
        "Waiting for IMU data... | Sample: measuring | Plot: measuring",
        justify="left",
    )
    window.addItem(status, row=4, col=0, colspan=column_count)

    def _make_plot(
        row: int,
        col: int,
        title_text: str,
        y_label: str,
        *,
        x_label: str | None = None,
        y_range: tuple[float, float] | None = None,
        x_range: tuple[float, float] | None = None,
    ) -> Any:
        plot = window.addPlot(row=row, col=col, title=title_text)
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.addLegend(offset=(10, 10))
        plot.setLabel("left", y_label)
        if x_label is not None:
            plot.setLabel("bottom", x_label)
        if y_range is not None:
            plot.setYRange(*y_range, padding=0.0)
        if x_range is not None:
            plot.setXRange(*x_range, padding=0.0)
        return plot

    def _add_lines(
        plot: Any,
        names: tuple[str, str, str],
        *,
        initial_x: np.ndarray = x,
        initial_y: np.ndarray = zeros,
    ) -> tuple[Any, Any, Any]:
        colors = ((230, 88, 85), (80, 190, 120), (85, 150, 245))
        return tuple(
            plot.plot(initial_x, initial_y, pen=pg.mkPen(color, width=1.5), name=name)
            for name, color in zip(names, colors)
        )

    ax_acc = _make_plot(
        1,
        0,
        "Accel Data",
        "g",
        y_range=(-8, 8),
        x_range=(-history + 1, 0),
    )
    line_ax, line_ay, line_az = _add_lines(ax_acc, ("Acc X", "Acc Y", "Acc Z"))

    ax_gyro = _make_plot(
        2,
        0,
        "Gyro Data",
        "Gyro",
        y_range=(-2000, 2000),
        x_range=(-history + 1, 0),
    )
    line_gx, line_gy, line_gz = _add_lines(ax_gyro, ("Gyro X", "Gyro Y", "Gyro Z"))

    ax_rpy = _make_plot(
        3,
        0,
        "RPY Data",
        "Degrees",
        x_label="Samples",
        y_range=(-200, 200),
        x_range=(-history + 1, 0),
    )
    line_r, line_p, line_y = _add_lines(ax_rpy, ("Roll", "Pitch", "Yaw"))

    acc_freq_lines: tuple[Any, Any, Any] | None = None
    gyro_freq_lines: tuple[Any, Any, Any] | None = None
    rpy_freq_lines: tuple[Any, Any, Any] | None = None
    ax_acc_freq = ax_gyro_freq = ax_rpy_freq = None

    if show_freq:
        empty = np.array([])
        ax_acc_freq = _make_plot(1, 1, "Accel Data Frequency Analysis", "Magnitude")
        acc_freq_lines = _add_lines(
            ax_acc_freq,
            ("Acc X", "Acc Y", "Acc Z"),
            initial_x=empty,
            initial_y=empty,
        )

        ax_gyro_freq = _make_plot(2, 1, "Gyro Data Frequency Analysis", "Magnitude")
        gyro_freq_lines = _add_lines(
            ax_gyro_freq,
            ("Gyro X", "Gyro Y", "Gyro Z"),
            initial_x=empty,
            initial_y=empty,
        )

        ax_rpy_freq = _make_plot(
            3,
            1,
            "RPY Data Frequency Analysis",
            "Magnitude",
            x_label="Frequency (Hz)",
        )
        rpy_freq_lines = _add_lines(
            ax_rpy_freq,
            ("Roll", "Pitch", "Yaw"),
            initial_x=empty,
            initial_y=empty,
        )

    def _pad(values: deque[float]) -> np.ndarray:
        padded = np.asarray(values, dtype=float)
        if padded.size >= history:
            return padded[-history:]
        return np.concatenate((np.zeros(history - padded.size), padded))

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
        axis: Any,
        lines: tuple[Any, Any, Any],
        values: tuple[deque[float], deque[float], deque[float]],
        bins: tuple[int, np.ndarray, np.ndarray],
    ) -> None:
        sample_count, frequencies, window = bins
        max_magnitude = 0.0
        for line, hist in zip(lines, values):
            magnitudes = _frequency_magnitudes(hist, sample_count, window)
            if magnitudes.size > 0:
                max_magnitude = max(max_magnitude, float(np.max(magnitudes)))
            line.setData(frequencies, magnitudes)

        axis.setXRange(0.0, max(float(frequencies[-1]), 1.0), padding=0.0)
        axis.setYRange(0.0, max(max_magnitude * 1.1, 1e-6), padding=0.0)

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

    def update() -> None:
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
            line_ax.setData(x, _pad(ax_hist))
            line_ay.setData(x, _pad(ay_hist))
            line_az.setData(x, _pad(az_hist))
            line_gx.setData(x, _pad(gx_hist))
            line_gy.setData(x, _pad(gy_hist))
            line_gz.setData(x, _pad(gz_hist))
            line_r.setData(x, _pad(roll_hist))
            line_p.setData(x, _pad(pitch_hist))
            line_y.setData(x, _pad(yaw_hist))

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
        status.setText(f"{sample_text} | {plot_text}")

    timer = QtCore.QTimer()
    timer.setInterval(max(1, round(1000.0 / refresh_hz)))
    timer.timeout.connect(update)

    def stop_timer(*_args: object) -> None:
        timer.stop()

    window.destroyed.connect(stop_timer)
    update()
    window.show()
    timer.start()
    exec_app = getattr(app, "exec", None)
    if exec_app is None:
        exec_app = app.exec_
    exec_app()


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
