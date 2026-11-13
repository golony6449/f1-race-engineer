"""asyncio UDP 수신기. 받은 원본을 녹화하고 파싱해 상태 저장소에 넣는다.

이 모듈의 약속은 하나다. **어떤 데이터그램이 와도 수신 루프는 죽지 않는다.**
`parse_packet`의 `UnsupportedPacket`은 미지원 진단으로, 그 밖의 예상 못 한
예외는 오류 진단으로 올리고 다음 데이터그램을 계속 받는다.

`process_datagram`은 소켓 없이도 같은 파이프라인을 돌릴 수 있게 분리했다.
리플레이(`f1re.udp.recording.replay`)가 이 함수를 sink로 쓴다.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable
from typing import Any

from f1re.state.store import StateStore
from f1re.udp.parser import Packet, UnsupportedPacket, parse_packet
from f1re.udp.recording import RecordingWriter

# 수신 버퍼 기본 크기. 고속 재생(60Hz 다수 패킷)에서 커널 드롭을 줄인다.
DEFAULT_RECV_BUFFER_BYTES = 8 * 1024 * 1024


def process_datagram(
    store: StateStore,
    data: bytes,
    recv_ns: int,
    recorder: RecordingWriter | None = None,
    on_packet: Callable[[Packet, int], None] | None = None,
) -> Packet | None:
    """데이터그램 하나를 녹화 → 파싱 → 상태 반영 순서로 처리한다.

    `on_packet`을 주면 상태 반영 직후 파싱된 패킷으로 한 번 더 부른다. 실서버
    파이프라인(`f1re.pipeline`)이 랩 기록·코칭을 붙이는 자리다(SCRUM-84).
    실패는 모두 진단에 올리고 `None`을 돌려준다. 예외를 밖으로 내지 않는다.
    """
    if recorder is not None:
        try:
            recorder.write(recv_ns, data)
        except Exception as exc:  # 디스크 오류 등. 수신은 계속한다.
            store.record_error(exc, recv_ns)

    try:
        try:
            packet = parse_packet(data)
        except UnsupportedPacket as exc:
            store.record_unsupported(exc, recv_ns)
            return None
        store.apply(packet, recv_ns, length=len(data))
        if on_packet is not None:
            try:
                on_packet(packet, recv_ns)
            except Exception as exc:  # 파이프라인 버그도 수신 루프를 죽이지 않는다.
                store.record_error(exc, recv_ns)
        return packet
    except Exception as exc:  # 저장소 처리 중 버그도 수신 루프를 죽이지 않는다.
        store.record_error(exc, recv_ns)
        return None


class DatagramReceiver(asyncio.DatagramProtocol):
    """UDP 데이터그램을 `process_datagram`으로 넘기는 asyncio 프로토콜."""

    def __init__(
        self,
        store: StateStore,
        *,
        recorder: RecordingWriter | None = None,
        on_packet: Callable[[Packet, int], None] | None = None,
    ) -> None:
        self.store = store
        self.recorder = recorder
        self.on_packet = on_packet
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        # 수신 시각은 게임이 보낸 시각이 아니라 우리가 받은 시각이다.
        process_datagram(
            self.store,
            data,
            time.time_ns(),
            self.recorder,
            self.on_packet,
        )

    def error_received(self, exc: Exception) -> None:
        # ICMP 오류 등 데이터그램과 무관한 오류. 진단만 남기고 계속 받는다.
        self.store.record_error(exc, time.time_ns())

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None


async def start_receiver(
    store: StateStore,
    *,
    host: str = "0.0.0.0",
    port: int = 20777,
    recorder: RecordingWriter | None = None,
    on_packet: Callable[[Packet, int], None] | None = None,
    recv_buffer_bytes: int = DEFAULT_RECV_BUFFER_BYTES,
    loop: asyncio.AbstractEventLoop | None = None,
) -> tuple[asyncio.DatagramTransport, DatagramReceiver]:
    """UDP 소켓을 열고 수신을 시작한다. `port=0`이면 빈 포트를 고른다."""
    event_loop = loop or asyncio.get_running_loop()
    transport, protocol = await event_loop.create_datagram_endpoint(
        lambda: DatagramReceiver(store, recorder=recorder, on_packet=on_packet),
        local_addr=(host, port),
    )
    if recv_buffer_bytes > 0:
        sock: Any = transport.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, recv_buffer_bytes
                )
            except OSError:
                # 운영체제가 요청한 크기를 거부해도 수신 자체는 계속한다.
                pass
    return transport, protocol
