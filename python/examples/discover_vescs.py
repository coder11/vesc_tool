#!/usr/bin/env python3
"""Discover connected VESCs (USB/serial and UDP broadcast).

Usage:
    python discover_vescs.py
    python discover_vescs.py --serial-only
    python discover_vescs.py --udp-only
    python discover_vescs.py --udp-timeout 5
"""

from __future__ import annotations

import argparse

from vesc_py.discovery import list_serial_ports, udp_scan


def _print_serial() -> None:
    ports = list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return
    print("Serial ports:")
    for p in ports:
        tag = ""
        if p.is_vesc:
            tag = " [VESC]"
        elif p.is_esp:
            tag = " [ESP]"
        print(f"  {p.system_path}  {p.name}{tag}")


def _print_udp(timeout: float) -> None:
    print(f"UDP (listening on port 65109 for {timeout}s) ...")
    devices = udp_scan(timeout=timeout)
    if not devices:
        print("No devices found via UDP.")
        return
    print("UDP devices:")
    for d in devices:
        print(f"  {d.hw_name}  {d.ip}:{d.port}")


def main() -> None:
    """Parse CLI flags and print serial and/or UDP-discovered VESCs."""
    parser = argparse.ArgumentParser(
        description="List VESC-related serial ports and UDP-discovered devices.",
    )
    parser.add_argument(
        "--serial-only",
        action="store_true",
        help="Only list serial ports.",
    )
    parser.add_argument(
        "--udp-only",
        action="store_true",
        help="Only listen for UDP announcements.",
    )
    parser.add_argument(
        "--udp-timeout",
        type=float,
        default=3.0,
        metavar="SEC",
        help="Seconds to listen for UDP broadcasts (default: 3).",
    )
    args = parser.parse_args()

    if args.serial_only and args.udp_only:
        parser.error("cannot use --serial-only and --udp-only together")

    if args.serial_only:
        _print_serial()
        return
    if args.udp_only:
        _print_udp(args.udp_timeout)
        return

    _print_serial()
    print()
    _print_udp(args.udp_timeout)


if __name__ == "__main__":
    main()
