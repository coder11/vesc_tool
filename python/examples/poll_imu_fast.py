#!/usr/bin/env python3
"""Poll VESC IMU data over USB serial as fast as possible.

The regular VescClient API is easier to use, but it performs a firmware
handshake, uses generic packet dispatch, and returns Pydantic models. This
script keeps the hot path small: one pre-encoded COMM_GET_IMU_DATA request,
direct serial reads, CRC validation, and lightweight value decoding.

Examples:
    python examples/poll_imu_fast.py
    python examples/poll_imu_fast.py --port /dev/ttyACM0 --duration 10
    python examples/poll_imu_fast.py --fields rpy,acc,gyro --pipeline-depth 4
    python examples/poll_imu_fast.py --mask 0x01ff --csv > imu.csv
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TextIO, cast

import serial  # type: ignore[import-untyped]

from vesc_py import list_serial_ports
from vesc_py.comm_ids import CommPacketId
from vesc_py.crc import crc16
from vesc_py.packet import MAX_PACKET_LEN, encode_packet

DEFAULT_BAUDRATE = 115200
DEFAULT_MASK = 0x01FF  # roll/pitch/yaw + accelerometer + gyroscope.
DEFAULT_TIMEOUT = 0.1
DEFAULT_STATUS_INTERVAL = 1.0
DEFAULT_UI_RATE = 10.0
DEFAULT_PIPELINE_DEPTH = 1
NSEC_PER_SEC = 1_000_000_000

FIELD_NAMES: tuple[str, ...] = (
    "roll",
    "pitch",
    "yaw",
    "acc_x",
    "acc_y",
    "acc_z",
    "gyro_x",
    "gyro_y",
    "gyro_z",
    "mag_x",
    "mag_y",
    "mag_z",
    "q0",
    "q1",
    "q2",
    "q3",
)
FIELD_MASKS: dict[str, int] = {
    name: 1 << index for index, name in enumerate(FIELD_NAMES)
}
FIELD_GROUP_MASKS: dict[str, int] = {
    "default": DEFAULT_MASK,
    "rpy": 0x0007,
    "attitude": 0x0007,
    "acc": 0x0038,
    "accel": 0x0038,
    "accelerometer": 0x0038,
    "gyro": 0x01C0,
    "gyroscope": 0x01C0,
    "mag": 0x0E00,
    "magnetometer": 0x0E00,
    "quat": 0xF000,
    "quaternion": 0xF000,
    "all": 0xFFFF,
}


class SerialLike(Protocol):
    """Small pyserial surface used by the fast poller."""

    timeout: float | None

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...

    def reset_input_buffer(self) -> None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class ReaderStats:
    """Counters for packet-level decode problems."""

    discarded_bytes: int = 0
    bad_crc: int = 0
    bad_stop: int = 0
    invalid_length: int = 0
    unexpected_packets: int = 0


@dataclass(slots=True)
class PollStats:
    """Counters for the polling loop."""

    requests: int = 0
    samples: int = 0
    timeouts: int = 0
    parse_errors: int = 0


@dataclass(frozen=True, slots=True)
class ParsedImu:
    """A decoded COMM_GET_IMU_DATA payload."""

    mask: int
    values: tuple[float, ...]
    vesc_id: int | None


class TerminalImuDisplay:
    """Small ANSI terminal view that redraws IMU values in place."""

    def __init__(self, *, refresh_hz: float, stream: TextIO) -> None:
        self._stream = stream
        self._interval_ns = max(1, round(NSEC_PER_SEC / refresh_hz))
        self._next_render_ns = 0
        self._line_count = 0
        self._started = False

    def start(self) -> None:
        """Hide the cursor while the in-place display is active."""
        if self._started:
            return
        self._started = True
        self._next_render_ns = time.perf_counter_ns() + self._interval_ns
        self._stream.write("\x1b[?25l")
        self._stream.flush()

    def close(self) -> None:
        """Restore the cursor and leave the final display visible."""
        if not self._started:
            return
        self._stream.write("\x1b[?25h\n")
        self._stream.flush()
        self._started = False

    def due(self, now_ns: int) -> bool:
        """Return whether enough time has passed for another redraw."""
        return now_ns >= self._next_render_ns

    def render(
        self,
        *,
        now_ns: int,
        start_ns: int,
        sample_timestamp_ns: int,
        parsed: ParsedImu,
        poll_stats: PollStats,
        reader_stats: ReaderStats,
        current_rate: float,
        average_rate: float,
    ) -> None:
        """Redraw the latest sample and real poll-rate counters."""
        elapsed = (now_ns - start_ns) / NSEC_PER_SEC
        age_ms = (now_ns - sample_timestamp_ns) / 1_000_000.0
        vesc_id = "n/a" if parsed.vesc_id is None else str(parsed.vesc_id)
        names = field_names_for_mask(parsed.mask)

        lines = [
            "VESC IMU fast poller",
            (
                f"poll rate: {current_rate:9.1f} Hz   "
                f"average: {average_rate:9.1f} Hz   elapsed: {elapsed:8.2f}s"
            ),
            (
                f"samples: {poll_stats.samples}   requests: {poll_stats.requests}   "
                f"timeouts: {poll_stats.timeouts}   parse_errors: {poll_stats.parse_errors}"
            ),
            (
                f"bad_crc: {reader_stats.bad_crc}   bad_stop: {reader_stats.bad_stop}   "
                f"discarded: {reader_stats.discarded_bytes}   unexpected: "
                f"{reader_stats.unexpected_packets}"
            ),
            f"vesc_id: {vesc_id}   rx_mask: 0x{parsed.mask:04x}   sample_age: {age_ms:.2f} ms",
            "",
            "IMU values",
        ]
        lines.extend(
            f"{name:<8} {value:>16.8g}"
            for name, value in zip(names, parsed.values)
        )

        if self._line_count > 0:
            self._stream.write(f"\x1b[{self._line_count}A")
        for line in lines:
            self._stream.write(f"\r\x1b[2K{line}\n")
        self._stream.flush()

        self._line_count = len(lines)
        self._next_render_ns = now_ns + self._interval_ns


def parse_mask_arg(text: str) -> int:
    """Parse a numeric 16-bit IMU mask."""
    try:
        mask = int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("mask must be an integer, e.g. 0x01ff") from exc

    if not 1 <= mask <= 0xFFFF:
        raise argparse.ArgumentTypeError("mask must be in range 1..0xffff")
    return mask


def parse_fields_arg(text: str) -> int:
    """Parse comma-separated field or field-group names into an IMU mask."""
    mask = 0
    tokens = text.replace("+", ",").split(",")
    for raw_token in tokens:
        token = raw_token.strip().lower().replace("-", "_")
        if not token:
            continue
        if token in FIELD_GROUP_MASKS:
            mask |= FIELD_GROUP_MASKS[token]
            continue
        if token in FIELD_MASKS:
            mask |= FIELD_MASKS[token]
            continue
        valid = ", ".join(sorted((*FIELD_GROUP_MASKS, *FIELD_MASKS)))
        raise argparse.ArgumentTypeError(
            f"unknown IMU field '{raw_token}'. Valid names: {valid}"
        )

    if mask == 0:
        raise argparse.ArgumentTypeError("at least one IMU field is required")
    return mask


def field_names_for_mask(mask: int) -> tuple[str, ...]:
    """Return IMU field names in VESC wire order for *mask*."""
    return tuple(name for index, name in enumerate(FIELD_NAMES) if mask & (1 << index))


def decode_double32_auto(word: int) -> float:
    """Decode VESC's custom 32-bit auto-scaled float format."""
    exponent = (word >> 23) & 0xFF
    fraction = word & 0x7FFFFF
    negative = bool(word & (1 << 31))

    value = 0.0
    if exponent != 0 or fraction != 0:
        value = fraction / (8388608.0 * 2.0) + 0.5
        exponent -= 126

    if negative:
        value = -value
    return math.ldexp(value, exponent)


def parse_imu_payload(payload: bytes) -> ParsedImu:
    """Decode a COMM_GET_IMU_DATA response payload without Pydantic overhead."""
    if len(payload) < 3:
        raise ValueError(f"IMU payload is too short: {len(payload)} bytes")
    if payload[0] != CommPacketId.COMM_GET_IMU_DATA:
        raise ValueError(f"unexpected command id {payload[0]}")

    mask = (payload[1] << 8) | payload[2]
    offset = 3
    values: list[float] = []
    for index in range(len(FIELD_NAMES)):
        if mask & (1 << index):
            if offset + 4 > len(payload):
                raise ValueError(
                    f"IMU payload ended inside field {FIELD_NAMES[index]}"
                )
            word = struct.unpack_from(">I", payload, offset)[0]
            values.append(decode_double32_auto(word))
            offset += 4

    vesc_id = None
    if offset < len(payload):
        vesc_id = payload[offset]

    return ParsedImu(mask=mask, values=tuple(values), vesc_id=vesc_id)


def build_imu_request(mask: int) -> bytes:
    """Build a fully framed COMM_GET_IMU_DATA request once for reuse."""
    payload = bytes(
        (
            int(CommPacketId.COMM_GET_IMU_DATA),
            (mask >> 8) & 0xFF,
            mask & 0xFF,
        )
    )
    return encode_packet(payload)


def read_exact(serial_port: SerialLike, size: int, deadline_ns: int) -> bytes | None:
    """Read exactly *size* bytes before *deadline_ns*."""
    chunks = bytearray()
    while len(chunks) < size:
        remaining_ns = deadline_ns - time.perf_counter_ns()
        if remaining_ns <= 0:
            return None

        serial_port.timeout = remaining_ns / NSEC_PER_SEC
        chunk = serial_port.read(size - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)

    return bytes(chunks)


def read_packet(
    serial_port: SerialLike,
    deadline_ns: int,
    stats: ReaderStats,
) -> bytes | None:
    """Read one valid VESC packet payload before *deadline_ns*."""
    while True:
        start = read_exact(serial_port, 1, deadline_ns)
        if start is None:
            return None
        start_byte = start[0]
        if start_byte in (2, 3, 4):
            break
        stats.discarded_bytes += 1

    if start_byte == 2:
        length_raw = read_exact(serial_port, 1, deadline_ns)
        if length_raw is None:
            return None
        payload_len = length_raw[0]
        if payload_len < 1:
            stats.invalid_length += 1
            return None
    elif start_byte == 3:
        length_raw = read_exact(serial_port, 2, deadline_ns)
        if length_raw is None:
            return None
        payload_len = (length_raw[0] << 8) | length_raw[1]
        if payload_len < 255:
            stats.invalid_length += 1
            return None
    else:
        length_raw = read_exact(serial_port, 3, deadline_ns)
        if length_raw is None:
            return None
        payload_len = (length_raw[0] << 16) | (length_raw[1] << 8) | length_raw[2]
        if payload_len < 65535:
            stats.invalid_length += 1
            return None

    if payload_len > MAX_PACKET_LEN:
        stats.invalid_length += 1
        return None

    body = read_exact(serial_port, payload_len + 3, deadline_ns)
    if body is None:
        return None
    if body[-1] != 3:
        stats.bad_stop += 1
        return None

    payload = body[:payload_len]
    received_crc = (body[payload_len] << 8) | body[payload_len + 1]
    if crc16(payload) != received_crc:
        stats.bad_crc += 1
        return None

    return payload


def read_expected_imu_packet(
    serial_port: SerialLike,
    packet_timeout: float,
    reader_stats: ReaderStats,
) -> bytes | None:
    """Read packets until an IMU response arrives or the timeout expires."""
    deadline_ns = time.perf_counter_ns() + round(packet_timeout * NSEC_PER_SEC)
    while True:
        payload = read_packet(serial_port, deadline_ns, reader_stats)
        if payload is None:
            return None
        if payload and payload[0] == CommPacketId.COMM_GET_IMU_DATA:
            return payload
        reader_stats.unexpected_packets += 1


def open_serial(
    port: str,
    baudrate: int,
    timeout: float,
    exclusive: bool,
) -> SerialLike:
    """Open a low-overhead serial connection to the USB CDC device."""
    kwargs = {
        "port": port,
        "baudrate": baudrate,
        "bytesize": serial.EIGHTBITS,
        "parity": serial.PARITY_NONE,
        "stopbits": serial.STOPBITS_ONE,
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
        "timeout": timeout,
        "write_timeout": timeout,
    }
    try:
        raw_serial = serial.Serial(**kwargs, exclusive=exclusive)
    except TypeError:
        raw_serial = serial.Serial(**kwargs)

    serial_port = cast(SerialLike, raw_serial)
    serial_port.reset_input_buffer()
    return serial_port


def autodetect_port() -> str:
    """Return the first discovered serial port, with VESC-like ports first."""
    ports = list_serial_ports()
    if not ports:
        raise SystemExit(
            "No serial ports found. Connect the VESC over USB or pass --port explicitly."
        )
    return ports[0].system_path


def print_serial_ports() -> None:
    """Print serial ports found by the VESC discovery helper."""
    ports = list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return

    for port in ports:
        tags = []
        if port.is_vesc:
            tags.append("vesc")
        if port.is_esp:
            tags.append("esp")
        tag_text = f" ({', '.join(tags)})" if tags else ""
        print(f"{port.system_path}{tag_text}  {port.name}")


def format_values(mask: int, values: tuple[float, ...], max_fields: int = 9) -> str:
    """Format selected IMU values for low-rate status output."""
    names = field_names_for_mask(mask)
    parts: list[str] = []
    for name, value in zip(names[:max_fields], values[:max_fields]):
        parts.append(f"{name}={value:.6g}")
    if len(values) > max_fields:
        parts.append("...")
    return " ".join(parts)


def write_csv_header(mask: int) -> None:
    """Write the CSV header for the requested field mask."""
    fields = ",".join(field_names_for_mask(mask))
    print(f"time_s,dt_s,vesc_id,rx_mask,{fields}")


def write_sample(
    sample_index: int,
    timestamp_ns: int,
    start_ns: int,
    previous_ns: int | None,
    parsed: ParsedImu,
    *,
    csv: bool,
) -> None:
    """Write one sample in CSV or human-readable form."""
    elapsed = (timestamp_ns - start_ns) / NSEC_PER_SEC
    dt = "" if previous_ns is None else f"{(timestamp_ns - previous_ns) / NSEC_PER_SEC:.9f}"
    vesc_id = "" if parsed.vesc_id is None else str(parsed.vesc_id)

    if csv:
        values = ",".join(f"{value:.9g}" for value in parsed.values)
        sys.stdout.write(
            f"{elapsed:.9f},{dt},{vesc_id},0x{parsed.mask:04x},{values}\n"
        )
        return

    value_text = format_values(parsed.mask, parsed.values, max_fields=len(parsed.values))
    sys.stdout.write(
        f"sample={sample_index} t={elapsed:.6f}s dt={dt or 'n/a'} "
        f"id={vesc_id or 'n/a'} mask=0x{parsed.mask:04x} {value_text}\n"
    )


def should_stop(
    start_ns: int,
    samples: int,
    *,
    duration: float,
    max_samples: int,
) -> bool:
    """Return whether the requested run limit has been reached."""
    if max_samples > 0 and samples >= max_samples:
        return True
    if duration > 0.0:
        elapsed = (time.perf_counter_ns() - start_ns) / NSEC_PER_SEC
        return elapsed >= duration
    return False


def poll_imu(
    serial_port: SerialLike,
    *,
    request: bytes,
    mask: int,
    packet_timeout: float,
    status_interval: float,
    duration: float,
    max_samples: int,
    pipeline_depth: int,
    print_every: int,
    csv: bool,
    no_decode: bool,
    tui: TerminalImuDisplay | None,
) -> None:
    """Run the high-rate IMU polling loop."""
    reader_stats = ReaderStats()
    poll_stats = PollStats()
    start_ns = time.perf_counter_ns()
    previous_sample_ns: int | None = None
    next_status_ns = start_ns + round(status_interval * NSEC_PER_SEC)
    status_window_ns = start_ns
    status_window_samples = 0
    outstanding = 0
    latest: ParsedImu | None = None

    if csv and not no_decode:
        write_csv_header(mask)
    if tui is not None:
        tui.start()

    try:
        while not should_stop(
            start_ns,
            poll_stats.samples,
            duration=duration,
            max_samples=max_samples,
        ):
            while outstanding < pipeline_depth:
                serial_port.write(request)
                poll_stats.requests += 1
                outstanding += 1

            payload = read_expected_imu_packet(
                serial_port,
                packet_timeout,
                reader_stats,
            )
            now_ns = time.perf_counter_ns()
            if payload is None:
                poll_stats.timeouts += 1
                outstanding = 0
                serial_port.reset_input_buffer()
                continue

            outstanding = max(0, outstanding - 1)
            poll_stats.samples += 1
            status_window_samples += 1

            if not no_decode:
                try:
                    latest = parse_imu_payload(payload)
                except ValueError as exc:
                    poll_stats.parse_errors += 1
                    print(f"parse error: {exc}", file=sys.stderr)
                    continue

                if print_every > 0 and poll_stats.samples % print_every == 0:
                    write_sample(
                        poll_stats.samples,
                        now_ns,
                        start_ns,
                        previous_sample_ns,
                        latest,
                        csv=csv,
                    )

            previous_sample_ns = now_ns

            if tui is not None and latest is not None and tui.due(now_ns):
                elapsed = (now_ns - start_ns) / NSEC_PER_SEC
                window_elapsed = (now_ns - status_window_ns) / NSEC_PER_SEC
                current_rate = (
                    status_window_samples / window_elapsed
                    if window_elapsed > 0.0
                    else 0.0
                )
                average_rate = poll_stats.samples / elapsed if elapsed > 0.0 else 0.0
                tui.render(
                    now_ns=now_ns,
                    start_ns=start_ns,
                    sample_timestamp_ns=previous_sample_ns,
                    parsed=latest,
                    poll_stats=poll_stats,
                    reader_stats=reader_stats,
                    current_rate=current_rate,
                    average_rate=average_rate,
                )

                status_window_ns = now_ns
                status_window_samples = 0
            elif tui is None and status_interval > 0.0 and now_ns >= next_status_ns:
                elapsed = (now_ns - start_ns) / NSEC_PER_SEC
                window_elapsed = (now_ns - status_window_ns) / NSEC_PER_SEC
                window_rate = (
                    status_window_samples / window_elapsed
                    if window_elapsed > 0.0
                    else 0.0
                )
                total_rate = poll_stats.samples / elapsed if elapsed > 0.0 else 0.0
                status = (
                    f"{elapsed:8.2f}s  rate={window_rate:8.1f} Hz  "
                    f"avg={total_rate:8.1f} Hz  samples={poll_stats.samples}  "
                    f"requests={poll_stats.requests}  timeouts={poll_stats.timeouts}  "
                    f"bad_crc={reader_stats.bad_crc}"
                )
                if latest is not None:
                    status += "  " + format_values(latest.mask, latest.values)
                print(status, file=sys.stderr)

                status_window_ns = now_ns
                status_window_samples = 0
                next_status_ns = now_ns + round(status_interval * NSEC_PER_SEC)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    finally:
        if tui is not None:
            tui.close()
        elapsed = (time.perf_counter_ns() - start_ns) / NSEC_PER_SEC
        rate = poll_stats.samples / elapsed if elapsed > 0.0 else 0.0
        print(
            f"Done. samples={poll_stats.samples} requests={poll_stats.requests} "
            f"elapsed={elapsed:.3f}s avg_rate={rate:.1f}Hz "
            f"timeouts={poll_stats.timeouts} parse_errors={poll_stats.parse_errors} "
            f"discarded={reader_stats.discarded_bytes} bad_crc={reader_stats.bad_crc} "
            f"bad_stop={reader_stats.bad_stop} invalid_length={reader_stats.invalid_length} "
            f"unexpected={reader_stats.unexpected_packets}",
            file=sys.stderr,
        )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Poll VESC IMU data over USB serial with minimal host overhead.",
    )
    parser.add_argument(
        "--port",
        help="Serial port path. If omitted, the first discovered VESC-like port is used.",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List discovered serial ports and exit.",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=DEFAULT_BAUDRATE,
        help=(
            "Serial baudrate (default: 115200). Native USB CDC VESC devices "
            "usually ignore this value."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help="Per-response timeout in seconds (default: 0.1).",
    )
    parser.add_argument(
        "--mask",
        type=parse_mask_arg,
        default=None,
        metavar="MASK",
        help="IMU field bitmask, e.g. 0x01ff. Mutually exclusive with --fields.",
    )
    parser.add_argument(
        "--fields",
        type=parse_fields_arg,
        default=None,
        metavar="LIST",
        help=(
            "Comma-separated fields/groups. Groups: default, rpy, acc, gyro, "
            "mag, quat, all. Default: default."
        ),
    )
    parser.add_argument(
        "--pipeline-depth",
        type=int,
        default=DEFAULT_PIPELINE_DEPTH,
        metavar="N",
        help=(
            "Number of outstanding IMU requests to keep in flight. Higher values "
            "can improve throughput over USB; 1 gives lowest request/response latency "
            "(default: 1)."
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        metavar="SEC",
        help="Stop after this many seconds. Default 0 runs until Ctrl-C.",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=0,
        metavar="N",
        help="Stop after this many decoded samples. Default 0 runs until another limit.",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=DEFAULT_STATUS_INTERVAL,
        metavar="SEC",
        help=(
            "Plain status print interval on stderr when the TUI is disabled. "
            "Use 0 to disable (default: 1.0)."
        ),
    )
    parser.add_argument(
        "--ui-rate",
        type=float,
        default=DEFAULT_UI_RATE,
        metavar="HZ",
        help=(
            "Terminal UI refresh rate in Hz. Poll-rate measurement still uses every "
            "sample (default: 10)."
        ),
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Disable the in-place terminal UI and use plain periodic status lines.",
    )
    parser.add_argument(
        "--print-every",
        type=int,
        default=0,
        metavar="N",
        help="Print every Nth decoded sample. Default 0 avoids sample-print overhead.",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Write printed samples as CSV to stdout. Implies --print-every 1.",
    )
    parser.add_argument(
        "--no-decode",
        action="store_true",
        help="Validate IMU packets but skip value decoding for a raw transport benchmark.",
    )
    parser.add_argument(
        "--no-exclusive",
        action="store_true",
        help="Do not request exclusive serial access on platforms that support it.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_ports:
        print_serial_ports()
        return

    if args.mask is not None and args.fields is not None:
        parser.error("--mask and --fields are mutually exclusive")
    if args.baudrate <= 0:
        parser.error("--baudrate must be greater than 0")
    if args.timeout <= 0.0:
        parser.error("--timeout must be greater than 0")
    if args.duration < 0.0:
        parser.error("--duration must be greater than or equal to 0")
    if args.max_samples < 0:
        parser.error("--max-samples must be greater than or equal to 0")
    if args.status_interval < 0.0:
        parser.error("--status-interval must be greater than or equal to 0")
    if args.ui_rate <= 0.0:
        parser.error("--ui-rate must be greater than 0")
    if args.print_every < 0:
        parser.error("--print-every must be greater than or equal to 0")
    if args.pipeline_depth <= 0:
        parser.error("--pipeline-depth must be greater than 0")

    mask = DEFAULT_MASK
    if args.fields is not None:
        mask = args.fields
    if args.mask is not None:
        mask = args.mask

    print_every = args.print_every
    if args.csv and print_every == 0:
        print_every = 1

    use_tui = (
        not args.no_tui
        and not args.csv
        and not args.no_decode
        and print_every == 0
        and sys.stderr.isatty()
    )
    tui = (
        TerminalImuDisplay(refresh_hz=args.ui_rate, stream=sys.stderr)
        if use_tui
        else None
    )

    port = args.port or autodetect_port()
    request = build_imu_request(mask)
    fields = ", ".join(field_names_for_mask(mask))

    print(
        f"Opening {port} at {args.baudrate} baud; mask=0x{mask:04x} ({fields}); "
        f"pipeline_depth={args.pipeline_depth}; "
        f"display={'tui' if tui is not None else 'plain'}",
        file=sys.stderr,
    )
    serial_port = open_serial(
        port,
        args.baudrate,
        args.timeout,
        exclusive=not args.no_exclusive,
    )
    try:
        poll_imu(
            serial_port,
            request=request,
            mask=mask,
            packet_timeout=args.timeout,
            status_interval=args.status_interval,
            duration=args.duration,
            max_samples=args.max_samples,
            pipeline_depth=args.pipeline_depth,
            print_every=print_every,
            csv=args.csv,
            no_decode=args.no_decode,
            tui=tui,
        )
    finally:
        serial_port.close()


if __name__ == "__main__":
    main()
