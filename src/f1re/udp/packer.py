"""Packet을 UDP 바이트로 만든다. 파서와 같은 형식 정의를 쓴다.

데모 송신기(작업 4)와 테스트가 실제 게임 패킷과 같은 바이트를 만들 수 있게
한다. 지원하지 않는 형식·패킷 ID는 파서와 같은 `UnsupportedPacket`으로
알린다.
"""

from __future__ import annotations

from typing import Any

from f1re.udp.parser import UnsupportedPacket
from f1re.udp.spec import FORMATS, Header, PacketId


def pack_packet(header: Header, body: dict[str, Any]) -> bytes:
    """헤더와 body를 합쳐 UDP 데이터그램 하나를 만든다."""
    format_module = FORMATS.get(header.packet_format)
    if format_module is None:
        raise UnsupportedPacket(
            "format",
            packet_format=header.packet_format,
            packet_id=header.packet_id,
        )

    try:
        packet_id = PacketId(header.packet_id)
    except ValueError:
        raise UnsupportedPacket(
            "packet_id",
            packet_format=header.packet_format,
            packet_id=header.packet_id,
        ) from None

    definition = format_module.PACKETS.get(packet_id)
    if definition is None:
        raise UnsupportedPacket(
            "packet_id",
            packet_format=header.packet_format,
            packet_id=header.packet_id,
        )

    return header.pack() + definition.pack_body(body)
