"""High-level VESC client with serial and TCP transports."""

from __future__ import annotations

import socket
import time
from abc import ABC, abstractmethod
from typing import Any

import serial  # type: ignore[import-untyped]

from vesc_py.appconf import (
    AppConfSchema,
    deserialize_appconf,
    load_appconf_xml,
    serialize_appconf,
)
from vesc_py.buffer import VescBuffer
from vesc_py.comm_ids import CommPacketId
from vesc_py.config_paths import find_appconf_xml
from vesc_py.fw_version import build_get_fw_version, parse_fw_version
from vesc_py.imu import build_get_imu_data, parse_imu_data
from vesc_py.models import FwVersion, ImuValues
from vesc_py.packet import PacketDecoder, encode_packet


class Transport(ABC):
    """Abstract byte transport for VESC communication."""

    @abstractmethod
    def send(self, data: bytes) -> None: ...

    @abstractmethod
    def recv(self, timeout: float) -> bytes: ...

    @abstractmethod
    def close(self) -> None: ...


class SerialTransport(Transport):
    """pyserial transport at 115200 8N1, no flow control."""

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        self._ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            xonxoff=False,
            rtscts=False,
            timeout=0.1,
        )

    def send(self, data: bytes) -> None:
        self._ser.write(data)

    def recv(self, timeout: float) -> bytes:
        self._ser.timeout = timeout
        data: bytes = self._ser.read(4096)
        return data

    def close(self) -> None:
        self._ser.close()


class TcpTransport(Transport):
    """Plain TCP socket transport."""

    def __init__(self, host: str, port: int) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.connect((host, port))

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def recv(self, timeout: float) -> bytes:
        self._sock.settimeout(timeout)
        try:
            return self._sock.recv(4096)
        except OSError:
            return b""

    def close(self) -> None:
        self._sock.close()


class VescClient:
    """High-level client for communicating with a VESC controller.

    Manages packet framing, firmware version handshake, and commands.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        timeout: float = 1.0,
        fw_retries: int = 25,
    ) -> None:
        self._transport = transport
        self._decoder = PacketDecoder()
        self._timeout = timeout
        self._fw: FwVersion | None = None
        self._schema: AppConfSchema | None = None
        self._fw_retries = fw_retries

    @classmethod
    def connect_serial(
        cls,
        port: str,
        baudrate: int = 115200,
        **kwargs: Any,
    ) -> VescClient:
        """Open a serial connection and perform the FW handshake."""
        transport = SerialTransport(port, baudrate)
        client = cls(transport, **kwargs)
        client._handshake()
        return client

    @classmethod
    def connect_tcp(
        cls,
        host: str,
        port: int,
        **kwargs: Any,
    ) -> VescClient:
        """Open a TCP connection and perform the FW handshake."""
        transport = TcpTransport(host, port)
        client = cls(transport, **kwargs)
        client._handshake()
        return client

    def close(self) -> None:
        self._transport.close()

    @property
    def fw_version(self) -> FwVersion | None:
        return self._fw

    @property
    def appconf_schema(self) -> AppConfSchema | None:
        return self._schema

    def _send_command(self, payload: bytes) -> None:
        """Encode and send a command payload."""
        self._transport.send(encode_packet(payload))

    def _recv_response(self, timeout: float | None = None) -> bytes:
        """Receive and decode exactly one response payload."""
        t = timeout if timeout is not None else self._timeout
        deadline = time.monotonic() + t

        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            raw = self._transport.recv(remaining)
            if raw:
                for payload in self._decoder.process(raw):
                    return payload

        raise TimeoutError("No response from VESC")

    def _handshake(self) -> None:
        """Request firmware version and load matching appconf XML."""
        for attempt in range(self._fw_retries):
            self._send_command(build_get_fw_version())
            try:
                payload = self._recv_response(timeout=0.2)
            except TimeoutError:
                continue

            if len(payload) >= 1 and payload[0] == CommPacketId.COMM_FW_VERSION:
                self._fw = parse_fw_version(payload)
                break
        else:
            raise ConnectionError(
                f"No firmware version response after {self._fw_retries} retries"
            )

        if self._fw.major >= 0 and self._fw.minor >= 0:
            try:
                xml_path = find_appconf_xml(self._fw.major, self._fw.minor)
                self._schema = load_appconf_xml(xml_path)
            except FileNotFoundError:
                pass

    def get_fw_version(self) -> FwVersion:
        """Request firmware version from the VESC."""
        self._send_command(build_get_fw_version())
        payload = self._recv_response()
        fw = parse_fw_version(payload)
        self._fw = fw
        return fw

    def get_imu_data(self, mask: int = 0xFFFF) -> ImuValues:
        """Request IMU data with the given field mask."""
        self._send_command(build_get_imu_data(mask))
        payload = self._recv_response()
        return parse_imu_data(payload)

    def get_appconf(self) -> dict[str, object]:
        """Read the current app configuration from the VESC."""
        if self._schema is None:
            raise RuntimeError("No appconf schema loaded (unknown firmware version?)")

        buf = VescBuffer()
        buf.append_uint8(CommPacketId.COMM_GET_APPCONF)
        self._send_command(buf.to_bytes())
        payload = self._recv_response()

        if len(payload) < 1 or payload[0] not in (
            CommPacketId.COMM_GET_APPCONF,
            CommPacketId.COMM_GET_APPCONF_DEFAULT,
        ):
            raise ValueError(f"Unexpected response command: {payload[0] if payload else 'empty'}")

        return deserialize_appconf(self._schema, payload[1:])

    def set_appconf(
        self,
        values: dict[str, object],
        *,
        store: bool = True,
    ) -> None:
        """Write app configuration to the VESC.

        If *store* is False, uses COMM_SET_APPCONF_NO_STORE (temporary).
        """
        if self._schema is None:
            raise RuntimeError("No appconf schema loaded (unknown firmware version?)")

        cmd = (
            CommPacketId.COMM_SET_APPCONF
            if store
            else CommPacketId.COMM_SET_APPCONF_NO_STORE
        )
        blob = serialize_appconf(self._schema, values)
        buf = VescBuffer()
        buf.append_uint8(cmd)
        buf._buf.extend(blob)
        self._send_command(buf.to_bytes())

    def send_alive(self) -> None:
        """Send a COMM_ALIVE keepalive."""
        buf = VescBuffer()
        buf.append_uint8(CommPacketId.COMM_ALIVE)
        self._send_command(buf.to_bytes())
