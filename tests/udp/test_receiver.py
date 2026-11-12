"""UDP 수신기: 쓰레기·미지원 형식·예상 못 한 예외에도 루프가 산다."""

from __future__ import annotations

import asyncio
import socket
import struct
from pathlib import Path
from typing import Any

from f1re.state.events import EventKind
from f1re.state.store import StateStore
from f1re.udp.packer import pack_packet
from f1re.udp.recording import RecordingWriter, iter_recording
from f1re.udp.receiver import start_receiver
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId

HOST = "127.0.0.1"


def _event_datagram(
    code: str,
    *,
    uid: int = 0x0123_4567_89AB_CDEF,
    frame: int = 1,
) -> bytes:
    header = Header(
        packet_format=PACKET_FORMAT,
        game_year=25,
        game_major=1,
        game_minor=0,
        packet_version=1,
        packet_id=int(PacketId.EVENT),
        session_uid=uid,
        session_time=frame / 60.0,
        frame_id=frame,
        overall_frame_id=frame,
        player_car_index=0,
        secondary_player_car_index=255,
    )
    body: dict[str, Any] = {
        "event_string_code": code,
        "event_details": {},
    }
    return pack_packet(header, body)


def _unknown_format_datagram() -> bytes:
    """packetFormat만 2026으로 바꾼 EVENT 데이터그램."""
    payload = _event_datagram("SSTA", frame=2)
    return struct.pack("<H", 2026) + payload[2:]


async def _wait_until(predicate: Any, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("조건이 시간 안에 만족되지 않았습니다")
        await asyncio.sleep(0.01)


async def test_receiver_survives_garbage_and_unknown_format() -> None:
    store = StateStore()
    transport, protocol = await start_receiver(store, host=HOST, port=0)
    port = transport.get_extra_info("sockname")[1]
    assert protocol.store is store
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"\x00" * 7, (HOST, port))
        sock.sendto(b"garbage that is not a packet", (HOST, port))
        sock.sendto(_event_datagram("SSTA", frame=1), (HOST, port))
        sock.sendto(_unknown_format_datagram(), (HOST, port))
        sock.sendto(_event_datagram("RDFL", frame=3), (HOST, port))

        await _wait_until(lambda: store.diagnostics.total_packets >= 5)

        diagnostics = store.diagnostics
        assert diagnostics.unsupported_count == 3
        assert diagnostics.last_unsupported_format == 2026
        assert diagnostics.last_format == PACKET_FORMAT
        assert diagnostics.error_count == 0

        kinds = [event.kind for event in store.events.drain()]
        assert EventKind.SESSION_START in kinds
        assert EventKind.RED_FLAG in kinds

        # 쓰레기를 받은 뒤에도 새 패킷을 계속 처리한다.
        sock.sendto(_event_datagram("RDFL", frame=4), (HOST, port))
        await _wait_until(lambda: store.diagnostics.total_packets >= 6)
        kinds = [event.kind for event in store.events.drain()]
        assert kinds == [EventKind.RED_FLAG]
    finally:
        sock.close()
        transport.close()
        await asyncio.sleep(0)


class _BoomStore(StateStore):
    """첫 apply에서만 예상 못 한 예외를 내는 저장소."""

    def __init__(self) -> None:
        super().__init__()
        self.boom_pending = True

    def apply(  # type: ignore[override]
        self, pkt: Any, recv_ns: int, *, length: int | None = None
    ) -> list[Any]:
        if self.boom_pending:
            self.boom_pending = False
            raise RuntimeError("boom")
        return super().apply(pkt, recv_ns, length=length)


async def test_receiver_survives_unexpected_exception() -> None:
    store = _BoomStore()
    transport, _protocol = await start_receiver(store, host=HOST, port=0)
    port = transport.get_extra_info("sockname")[1]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(_event_datagram("RDFL", frame=1), (HOST, port))
        await _wait_until(lambda: store.diagnostics.error_count >= 1)
        assert "RuntimeError: boom" in (store.diagnostics.last_error or "")

        sock.sendto(_event_datagram("RDFL", frame=2), (HOST, port))
        await _wait_until(lambda: len(store.events) > 0)
        assert any(
            event.kind is EventKind.RED_FLAG for event in store.events.drain()
        )
        assert store.diagnostics.error_count == 1
        assert store.diagnostics.total_packets == 2
    finally:
        sock.close()
        transport.close()
        await asyncio.sleep(0)


async def test_receiver_records_raw_datagrams(tmp_path: Path) -> None:
    path = tmp_path / "raw.f1rec"
    writer = RecordingWriter(path)
    store = StateStore()
    transport, _protocol = await start_receiver(
        store, host=HOST, port=0, recorder=writer
    )
    port = transport.get_extra_info("sockname")[1]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    garbage = b"\x00" * 7
    try:
        sock.sendto(garbage, (HOST, port))
        sock.sendto(_event_datagram("RDFL", frame=1), (HOST, port))
        await _wait_until(lambda: store.diagnostics.total_packets >= 2)
    finally:
        sock.close()
        transport.close()
        await asyncio.sleep(0)
        writer.close()

    records = list(iter_recording(path))
    assert len(records) == 2
    assert records[0][1] == garbage
    assert all(recv_ns > 0 for recv_ns, _data in records)
    assert store.diagnostics.unsupported_count == 1
