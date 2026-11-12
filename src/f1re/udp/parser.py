"""UDP 바이트를 Packet으로 푼다.

수신 루프가 쓰레기 바이트·다른 형식·모르는 패킷 때문에 죽지 않도록, 실패는
모두 `UnsupportedPacket` 하나로 알린다. `reason`은 다음 셋 중 하나다.

- ``length``: 헤더 29바이트보다 짧거나 패킷 전체 길이가 규격과 다름
- ``format``: ``packetFormat``이 지원 형식 표(`FORMATS`)에 없음(2026 Season Pack 등)
- ``packet_id``: ``packetId``가 모르는 값이거나 그 형식에 정의가 없음

정상 패킷이면 `Packet`을 돌려준다. 그 밖의 다른 예외는 이 함수 밖으로
새지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from f1re.udp.spec import FORMATS, HEADER_SIZE, Header, PacketId

# UnsupportedPacket.reason으로 쓸 수 있는 값.
UNSUPPORTED_REASONS = frozenset({"length", "format", "packet_id"})


@dataclass(frozen=True)
class Packet:
    """파싱 결과. body 필드 이름은 공식 문서 이름의 snake_case다."""

    header: Header
    body: dict[str, Any]


class UnsupportedPacket(Exception):
    """지원하지 않는 패킷. 수신 루프는 이 예외만 잡아 진단으로 올린다."""

    def __init__(
        self,
        reason: str,
        *,
        length: int | None = None,
        packet_format: int | None = None,
        packet_id: int | None = None,
    ) -> None:
        if reason not in UNSUPPORTED_REASONS:
            raise ValueError(f"알 수 없는 reason: {reason}")
        self.reason = reason
        self.length = length
        self.packet_format = packet_format
        self.packet_id = packet_id
        details = []
        if length is not None:
            details.append(f"len={length}")
        if packet_format is not None:
            details.append(f"format={packet_format}")
        if packet_id is not None:
            details.append(f"id={packet_id}")
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"unsupported packet: {reason}{suffix}")


def parse_packet(data: bytes) -> Packet:
    """UDP 데이터그램 하나를 파싱한다. 실패는 `UnsupportedPacket`뿐이다."""
    length = len(data)
    if length < HEADER_SIZE:
        raise UnsupportedPacket("length", length=length)

    header = Header.unpack_from(data)

    format_module = FORMATS.get(header.packet_format)
    if format_module is None:
        raise UnsupportedPacket(
            "format",
            length=length,
            packet_format=header.packet_format,
            packet_id=header.packet_id,
        )

    try:
        packet_id = PacketId(header.packet_id)
    except ValueError:
        raise UnsupportedPacket(
            "packet_id",
            length=length,
            packet_format=header.packet_format,
            packet_id=header.packet_id,
        ) from None

    expected_size = format_module.PACKET_SIZES.get(packet_id)
    if expected_size is None or length != expected_size:
        raise UnsupportedPacket(
            "length",
            length=length,
            packet_format=header.packet_format,
            packet_id=header.packet_id,
        )

    definition = format_module.PACKETS.get(packet_id)
    if definition is None:
        raise UnsupportedPacket(
            "packet_id",
            length=length,
            packet_format=header.packet_format,
            packet_id=header.packet_id,
        )

    return Packet(header=header, body=definition.parse_body(data[HEADER_SIZE:]))
