#!/usr/bin/env python3
"""Discover serial VESCs and their CAN-connected devices.

Usage:
    python discover_vescs.py
    python discover_vescs.py --can-timeout 8
    python discover_vescs.py --udp-only
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import serial  # type: ignore[import-untyped]

from vesc_py import VescClient, list_serial_ports, udp_scan
from vesc_py.models import FwVersion, VescSerialPort


@dataclass(frozen=True)
class CanNode:
    """Discovered CAN node and optional firmware metadata."""

    can_id: int
    fw: FwVersion | None
    error: str | None = None


@dataclass(frozen=True)
class VescNode:
    """Discovered root VESC on a serial port."""

    port: VescSerialPort
    fw: FwVersion
    can_nodes: list[CanNode]
    can_error: str | None = None


def _fw_label(fw: FwVersion) -> str:
    """Return a compact human-readable firmware/device label."""
    parts = [fw.hw or "Unknown HW", f"FW {fw.major}.{fw.minor}"]
    if fw.fw_name:
        parts.append(fw.fw_name)
    if fw.uuid:
        parts.append(f"UUID {fw.uuid.hex()}")
    return " | ".join(parts)


def _scan_can_nodes(client: VescClient, *, timeout: float) -> list[CanNode]:
    """Scan CAN IDs and query firmware metadata from each responding node."""
    nodes: list[CanNode] = []
    for can_id in client.scan_can(timeout=timeout):
        try:
            nodes.append(
                CanNode(
                    can_id=can_id,
                    fw=client.get_fw_version(can_id=can_id, timeout=timeout),
                )
            )
        except (ConnectionError, OSError, TimeoutError, ValueError) as exc:
            nodes.append(CanNode(can_id=can_id, fw=None, error=str(exc)))
    return nodes


def _probe_serial_port(
    port: VescSerialPort,
    *,
    timeout: float,
    can_timeout: float,
    fw_retries: int,
    verbose: bool,
) -> VescNode | None:
    """Open *port*, perform a VESC handshake, and scan its CAN bus."""
    client: VescClient | None = None
    if verbose:
        print(f"Probing {port.system_path} ({port.name}) ...", file=sys.stderr)
    try:
        client = VescClient.connect_serial(
            port.system_path,
            timeout=timeout,
            fw_retries=fw_retries,
        )
        fw = client.fw_version
        if fw is None:
            fw = client.get_fw_version()
        try:
            can_nodes = _scan_can_nodes(client, timeout=can_timeout)
            can_error = None
        except (ConnectionError, OSError, TimeoutError, ValueError) as exc:
            can_nodes = []
            can_error = str(exc)
            if verbose:
                print(f"  CAN scan failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return VescNode(port=port, fw=fw, can_nodes=can_nodes, can_error=can_error)
    except (ConnectionError, OSError, serial.SerialException, TimeoutError, ValueError) as exc:
        if verbose:
            print(f"  skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    finally:
        if client is not None:
            client.close()


def _scan_serial_tree(
    timeout: float,
    can_timeout: float,
    fw_retries: int,
    *,
    verbose: bool,
) -> list[VescNode]:
    """Probe available serial ports and return all ports that answer as VESCs."""
    result: list[VescNode] = []
    ports = list_serial_ports()
    if verbose and not ports:
        print("No serial ports reported by pyserial.", file=sys.stderr)

    # VESC-like ports are listed first by list_serial_ports(); still probe the
    # rest because USB metadata is not always populated on every platform.
    for port in ports:
        node = _probe_serial_port(
            port,
            timeout=timeout,
            can_timeout=can_timeout,
            fw_retries=fw_retries,
            verbose=verbose,
        )
        if node is not None:
            result.append(node)

    return result


def _print_serial_tree(nodes: list[VescNode]) -> None:
    """Print discovered serial VESCs and their CAN children."""
    print("VESC:")
    if not nodes:
        print("  none")
        return

    for node in nodes:
        print(f"  {node.port.system_path}  {node.port.name}")
        print(f"    {_fw_label(node.fw)}")
        print("    CAN:")
        if node.can_error is not None:
            print(f"      scan failed: {node.can_error}")
        elif not node.can_nodes:
            print("      none")
        else:
            for can_node in node.can_nodes:
                if can_node.fw is not None:
                    print(f"      {can_node.can_id}: {_fw_label(can_node.fw)}")
                elif can_node.error:
                    print(f"      {can_node.can_id}: no firmware response ({can_node.error})")
                else:
                    print(f"      {can_node.can_id}: no firmware response")


def _print_udp(timeout: float) -> None:
    """Print UDP-discovered VESC Tool bridge announcements."""
    print(f"UDP (listening on port 65109 for {timeout}s) ...")
    devices = udp_scan(timeout=timeout)
    if not devices:
        print("No devices found via UDP.")
        return
    print("UDP devices:")
    for device in devices:
        print(f"  {device.hw_name}  {device.ip}:{device.port}")


def main() -> None:
    """Parse CLI flags and print serial VESC/CAN discovery results."""
    parser = argparse.ArgumentParser(
        description="Discover serial VESCs and print their CAN device tree.",
    )
    parser.add_argument(
        "--serial-only",
        action="store_true",
        help="Only scan serial ports. This is the default.",
    )
    parser.add_argument(
        "--udp-only",
        action="store_true",
        help="Only listen for VESC Tool UDP announcements.",
    )
    parser.add_argument(
        "--udp-timeout",
        type=float,
        default=3.0,
        metavar="SEC",
        help="Seconds to listen for UDP broadcasts (default: 3).",
    )
    parser.add_argument(
        "--serial-timeout",
        type=float,
        default=1.0,
        metavar="SEC",
        help="Response timeout for each serial request (default: 1.0).",
    )
    parser.add_argument(
        "--can-timeout",
        type=float,
        default=5.0,
        metavar="SEC",
        help="Timeout for CAN scan and CAN firmware queries (default: 5.0).",
    )
    parser.add_argument(
        "--fw-retries",
        type=int,
        default=5,
        metavar="N",
        help="Firmware-version handshake retries per serial port (default: 5).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each probed serial port and the reason it was skipped.",
    )
    args = parser.parse_args()

    if args.serial_only and args.udp_only:
        parser.error("cannot use --serial-only and --udp-only together")

    if args.udp_only:
        _print_udp(args.udp_timeout)
        return

    _print_serial_tree(
        _scan_serial_tree(
            args.serial_timeout,
            args.can_timeout,
            args.fw_retries,
            verbose=args.verbose,
        )
    )


if __name__ == "__main__":
    main()
