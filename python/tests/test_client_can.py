from __future__ import annotations

from vesc_py.buffer import VescBuffer
from vesc_py.client import Transport, VescClient
from vesc_py.comm_ids import CommPacketId
from vesc_py.packet import PacketDecoder, encode_packet


class FakeTransport(Transport):
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, timeout: float) -> bytes:
        del timeout
        if not self.responses:
            return b""
        return self.responses.pop(0)

    def close(self) -> None:
        pass


def _decode_sent_payload(frame: bytes) -> bytes:
    payloads = list(PacketDecoder().process(frame))
    assert len(payloads) == 1
    return payloads[0]


def test_scan_can_parses_returned_ids() -> None:
    response = encode_packet(bytes([CommPacketId.COMM_PING_CAN, 2, 7, 42]))
    transport = FakeTransport([response])
    client = VescClient(transport, timeout=0.1)

    assert client.scan_can() == [2, 7, 42]
    assert _decode_sent_payload(transport.sent[0]) == bytes([CommPacketId.COMM_PING_CAN])


def test_get_fw_version_can_wraps_request() -> None:
    response = VescBuffer()
    response.append_uint8(CommPacketId.COMM_FW_VERSION)
    response.append_int8(6)
    response.append_int8(6)
    response.append_string("VESC Express")
    transport = FakeTransport([encode_packet(response.to_bytes())])
    client = VescClient(transport, timeout=0.1)

    fw = client.get_fw_version(can_id=12)

    assert fw.hw == "VESC Express"
    assert _decode_sent_payload(transport.sent[0]) == bytes(
        [
            CommPacketId.COMM_FORWARD_CAN,
            12,
            CommPacketId.COMM_FW_VERSION,
        ]
    )
