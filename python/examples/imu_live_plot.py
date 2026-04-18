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
from matplotlib.artist import Artist

from vesc_py import ImuValues, VescClient, udp_scan

DEFAULT_TCP_ENDPOINT = ("127.0.0.1", 65102)
DEFAULT_MASK = 0x003F  # roll/pitch/yaw + accelerometer, the fields plotted below.
DEFAULT_POLL_HZ = 50.0
PLOT_REFRESH_HZ = 30.0
PLOT_INTERVAL_MS = 1000.0 / PLOT_REFRESH_HZ


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


def run_live_plot(poller: ImuPoller, history: int = 300) -> None:
    """Run a matplotlib live plot of IMU data."""
    require_interactive_backend()

    rad2deg = 180.0 / math.pi
    roll_hist: deque[float] = deque(maxlen=history)
    pitch_hist: deque[float] = deque(maxlen=history)
    yaw_hist: deque[float] = deque(maxlen=history)
    ax_hist: deque[float] = deque(maxlen=history)
    ay_hist: deque[float] = deque(maxlen=history)
    az_hist: deque[float] = deque(maxlen=history)

    fig, (ax_rpy, ax_acc) = plt.subplots(2, 1, figsize=(10, 7))
    fig.suptitle("VESC IMU Live Data")
    status = fig.text(0.01, 0.01, "Waiting for IMU data...", fontsize=9)

    x = np.arange(history)
    zeros = [0.0] * history

    (line_r,) = ax_rpy.plot(x, zeros, label="Roll")
    (line_p,) = ax_rpy.plot(x, zeros, label="Pitch")
    (line_y,) = ax_rpy.plot(x, zeros, label="Yaw")
    ax_rpy.set_ylabel("Degrees")
    ax_rpy.set_ylim(-180, 180)
    ax_rpy.legend(loc="upper left")
    ax_rpy.grid(True, alpha=0.3)

    (line_ax,) = ax_acc.plot(x, zeros, label="Acc X")
    (line_ay,) = ax_acc.plot(x, zeros, label="Acc Y")
    (line_az,) = ax_acc.plot(x, zeros, label="Acc Z")
    ax_acc.set_ylabel("g")
    ax_acc.set_ylim(-4, 4)
    ax_acc.legend(loc="upper left")
    ax_acc.grid(True, alpha=0.3)

    def _pad(values: deque[float]) -> list[float]:
        padded = list(values)
        return [0.0] * (history - len(padded)) + padded

    def update(_frame: int) -> tuple[Artist, ...]:
        latest_timestamp: float | None = None
        for sample in poller.drain():
            imu = sample.values
            roll_hist.append(imu.roll * rad2deg)
            pitch_hist.append(imu.pitch * rad2deg)
            yaw_hist.append(imu.yaw * rad2deg)
            ax_hist.append(imu.acc_x)
            ay_hist.append(imu.acc_y)
            az_hist.append(imu.acc_z)
            latest_timestamp = sample.timestamp

        line_r.set_ydata(_pad(roll_hist))
        line_p.set_ydata(_pad(pitch_hist))
        line_y.set_ydata(_pad(yaw_hist))
        line_ax.set_ydata(_pad(ax_hist))
        line_ay.set_ydata(_pad(ay_hist))
        line_az.set_ydata(_pad(az_hist))

        if latest_timestamp is not None:
            status.set_text(f"Last sample: {time.monotonic() - latest_timestamp:.2f}s ago")
        elif poller.last_error is not None:
            status.set_text(f"IMU read error: {poller.last_error}")

        return (line_r, line_p, line_y, line_ax, line_ay, line_az, status)

    _anim = FuncAnimation(
        fig, update, interval=PLOT_INTERVAL_MS, blit=False, cache_frame_data=False
    )
    plt.tight_layout()
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
        help="IMU field bitmask (default: 0x003f = RPY + accelerometer).",
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
        run_live_plot(poller, history=args.history)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        poller.stop(timeout=args.timeout + 0.1)
        client.close()


if __name__ == "__main__":
    main()
