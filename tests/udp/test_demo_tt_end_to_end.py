"""데모 송신기 → 실제 UDP → 수신기 → 상태·사건 전체 경로.

스즈카 2랩을 재생 속도 배율(40배)로 빠르게 돌려 실제 시간 몇 초 안에 끝낸다.
송신기를 별도 프로세스로 띄우는 이유는 같은 프로세스의 GIL 때문에 송신과
수신이 번갈아 실행되어 10초 넘게 걸리기 때문이다. 별도 프로세스에서는
수신기가 다른 코어에서 패킷을 처리하므로 5~6초에 끝난다.

녹화·리플레이도 같은 파이프라인(`process_datagram`)으로 확인한다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from f1re.demo_sender.sender import DemoSender
from f1re.state.events import EventKind
from f1re.state.store import StateStore
from f1re.tracks.model import load_outline, load_track
from f1re.udp.recording import RecordingWriter, iter_recording, replay
from f1re.udp.receiver import process_datagram, start_receiver

HOST = "127.0.0.1"
SUZUKA_ID = 13

# 별도 프로세스에서 스즈카 2랩을 40배로 보내는 작업 4 데모 송신기.
# 윈도우의 time.sleep 하한 때문에 틱마다 자면 배율이 안 나오므로 10틱씩 묶어 잔다.
_SENDER_CODE = """
import sys
import time

from f1re.demo_sender.sender import DemoSender
from f1re.tracks.model import load_outline, load_track


class _BatchedSleeper:
    def __init__(self, speed: float, batch: int = 10) -> None:
        self.interval = batch / 60.0 / speed
        self.batch = batch
        self.calls = 0

    def __call__(self, _seconds: float) -> None:
        self.calls += 1
        if self.calls % self.batch == 0:
            time.sleep(self.interval)


port = int(sys.argv[1])
track = load_track(13)
outline = load_outline(13)
sender = DemoSender(
    track,
    outline,
    laps=2,
    seed=0,
    host="127.0.0.1",
    port=port,
    rate_hz=20,
    speed=40.0,
)
sender.run(sleep=_BatchedSleeper(40.0))
"""


def _suzuka_sender(*, port: int, laps: int, speed: float, rate_hz: int) -> DemoSender:
    track = load_track(SUZUKA_ID)
    outline = load_outline(SUZUKA_ID)
    assert track is not None and outline is not None
    return DemoSender(
        track,
        outline,
        laps=laps,
        seed=0,
        host=HOST,
        port=port,
        rate_hz=rate_hz,
        speed=speed,
    )


async def test_demo_tt_end_to_end() -> None:
    """스즈카 2랩 데모를 40배로 돌려 랩 완료 사건 2회를 확인한다."""
    store = StateStore()
    transport, _protocol = await start_receiver(store, host=HOST, port=0)
    port = transport.get_extra_info("sockname")[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _SENDER_CODE,
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
        assert process.returncode == 0, stderr.decode(errors="replace")
        # 송신이 끝난 뒤 소켓에 남은 데이터그램까지 처리할 시간을 준다.
        await asyncio.sleep(0.5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        transport.close()
        await asyncio.sleep(0)

    events = store.events.drain()
    laps = sorted(
        event.lap
        for event in events
        if event.kind is EventKind.LAP_COMPLETE and event.car_index == 0
    )
    assert laps == [1, 2]

    diagnostics = store.diagnostics
    assert diagnostics.unsupported_count == 0
    assert diagnostics.error_count == 0
    assert diagnostics.total_packets > 100

    snapshot = store.snapshot()
    assert snapshot["packets"]["session"]["track_id"] == SUZUKA_ID
    assert snapshot["packets"]["session"]["track_length"] == 5807
    assert snapshot["packets"]["session"]["session_type"] == 13


async def test_recorded_demo_replays_through_same_pipeline(tmp_path: Path) -> None:
    """데모 송신분을 녹화한 뒤 리플레이해 같은 패킷 수·세션 상태를 얻는다."""
    store = StateStore()
    recording_path = tmp_path / "demo.f1rec"
    writer = RecordingWriter(recording_path)
    transport, _protocol = await start_receiver(
        store, host=HOST, port=0, recorder=writer
    )
    port = transport.get_extra_info("sockname")[1]
    sender = _suzuka_sender(port=port, laps=1, speed=100.0, rate_hz=60)
    try:
        await asyncio.wait_for(
            asyncio.to_thread(sender.run, duration_s=2.0), timeout=15.0
        )
        await asyncio.sleep(0.2)
    finally:
        transport.close()
        await asyncio.sleep(0)
        writer.close()

    live_packets = store.diagnostics.total_packets
    assert live_packets > 0
    assert store.diagnostics.unsupported_count == 0

    records = list(iter_recording(recording_path))
    assert len(records) == live_packets

    replay_store = StateStore()
    count = replay(
        recording_path,
        lambda recv_ns, data: process_datagram(replay_store, data, recv_ns),
        speed=10_000.0,
    )
    assert count == live_packets
    assert replay_store.diagnostics.total_packets == live_packets
    assert replay_store.diagnostics.unsupported_count == 0

    snapshot = replay_store.snapshot()
    assert snapshot["packets"]["session"]["track_id"] == SUZUKA_ID
    kinds = {event.kind for event in replay_store.events.drain()}
    assert EventKind.SESSION_START in kinds
