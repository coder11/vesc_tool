#!/usr/bin/env python3
"""Live IMU data plot from a VESC controller.

Usage:
    python imu_live_plot.py --serial /dev/ttyACM0
    python imu_live_plot.py --tcp 192.168.1.100:65102
    python imu_live_plot.py --scan-serial
    python imu_live_plot.py --scan-udp
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import deque

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

from vesc_py import VescClient, list_serial_ports, udp_scan

# Recv timeout matches one plot frame (library default 1s would cap updates ~1 Hz on miss).
_PLOT_REFRESH_HZ = 60.0
_PLOT_PERIOD_S = 1.0 / _PLOT_REFRESH_HZ
_PLOT_INTERVAL_MS = 1000.0 / _PLOT_REFRESH_HZ


def scan_and_print_serial() -> None:
    """Print discovered serial ports with VESC/ESP markers."""
    ports = list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return
    for p in ports:
        tag = ""
        if p.is_vesc:
            tag = " [VESC]"
        elif p.is_esp:
            tag = " [ESP]"
        print(f"  {p.system_path}  {p.name}{tag}")


def scan_and_print_udp(timeout: float = 3.0) -> None:
    """Listen for UDP broadcast announcements and print them."""
    print(f"Listening for UDP announcements on port 65109 for {timeout}s ...")
    devices = udp_scan(timeout=timeout)
    if not devices:
        print("No devices found via UDP.")
        return
    for d in devices:
        print(f"  {d.hw_name}  {d.ip}:{d.port}")


def run_live_plot(client: VescClient, mask: int, history: int = 200) -> None:
    """Run a matplotlib live plot of IMU data."""
    if matplotlib.get_backend().lower() == "agg":
        raise RuntimeError(
            "No interactive matplotlib backend is available (still using Agg). "
            "Install a GUI toolkit (e.g. python3-tk / tkinter, or PyQt5/PyQt6), "
            "ensure DISPLAY is set for X11/Wayland, and unset MPLBACKEND if it forces Agg."
        )

    RAD2DEG = 180.0 / math.pi

    roll_hist: deque[float] = deque(maxlen=history)
    pitch_hist: deque[float] = deque(maxlen=history)
    yaw_hist: deque[float] = deque(maxlen=history)
    ax_hist: deque[float] = deque(maxlen=history)
    ay_hist: deque[float] = deque(maxlen=history)
    az_hist: deque[float] = deque(maxlen=history)

    fig, (ax_rpy, ax_acc) = plt.subplots(2, 1, figsize=(10, 7))
    fig.suptitle("VESC IMU Live Data")

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

    def update(_frame: int) -> tuple[Artist, ...]:
        try:
            imu = client.get_imu_data(mask)
        except Exception as exc:
            print(f"IMU read error: {exc}", file=sys.stderr)
            return (line_r, line_p, line_y, line_ax, line_ay, line_az)

        roll_hist.append(imu.roll * RAD2DEG)
        pitch_hist.append(imu.pitch * RAD2DEG)
        yaw_hist.append(imu.yaw * RAD2DEG)
        ax_hist.append(imu.acc_x)
        ay_hist.append(imu.acc_y)
        az_hist.append(imu.acc_z)

        def _pad(d: deque[float]) -> list[float]:
            lst = list(d)
            return [0.0] * (history - len(lst)) + lst

        line_r.set_ydata(_pad(roll_hist))
        line_p.set_ydata(_pad(pitch_hist))
        line_y.set_ydata(_pad(yaw_hist))
        line_ax.set_ydata(_pad(ax_hist))
        line_ay.set_ydata(_pad(ay_hist))
        line_az.set_ydata(_pad(az_hist))

        return (line_r, line_p, line_y, line_ax, line_ay, line_az)

    _anim = FuncAnimation(
        fig, update, interval=_PLOT_INTERVAL_MS, blit=True, cache_frame_data=False
    )
    plt.tight_layout()
    plt.show()


def main() -> None:
    parser = argparse.ArgumentParser(description="VESC IMU live plot")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serial", metavar="PORT", help="Serial port (e.g. /dev/ttyACM0)")
    group.add_argument("--tcp", metavar="HOST:PORT", help="TCP host:port (e.g. 192.168.1.100:65102)")
    group.add_argument("--scan-serial", action="store_true", help="Scan and list serial ports")
    group.add_argument("--scan-udp", action="store_true", help="Scan for UDP announcements")

    parser.add_argument("--mask", type=lambda s: int(s, 0), default=0x1FF,
                        help="IMU field bitmask (default 0x1FF = RPY + accel + gyro)")

    args = parser.parse_args()

    if args.scan_serial:
        scan_and_print_serial()
        return

    if args.scan_udp:
        scan_and_print_udp()
        return

    if args.serial:
        print(f"Connecting via serial: {args.serial}")
        client = VescClient.connect_serial(args.serial, timeout=_PLOT_PERIOD_S)
    else:
        host, _, port_str = args.tcp.rpartition(":")
        print(f"Connecting via TCP: {host}:{port_str}")
        client = VescClient.connect_tcp(host, int(port_str), timeout=_PLOT_PERIOD_S)

    fw = client.fw_version
    if fw:
        print(f"Firmware: {fw.major}.{fw.minor:02d}  HW: {fw.hw}")

    try:
        run_live_plot(client, args.mask)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
