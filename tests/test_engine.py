"""SCRUM-82: 레이스 엔지니어 엔진 ON/OFF 수명 주기.

- 최초에는 상태 파일이 없으므로 OFF로 시작한다.
- ON이면 UDP 포트를 잡고, OFF면 소켓을 닫아 같은 포트로 다시 바인드할 수 있다.
- ON→OFF→ON을 반복해도 태스크·소켓이 쌓이지 않는다.
- 마지막 상태는 `data/engine-state.json` 모양의 파일에 남는다.

SCRUM-106: 엔진 ON 동안 세션별 원본을 자동 녹화하고, 종료·세션 전환·무수신
타임아웃 때 활성 파일만 닫아 백업 큐에 넘긴다(압축·업로드는 워커 몫).
"""

from __future__ import annotations

import asyncio
import dataclasses
import socket
import time
from pathlib import Path
from typing import Any

import pytest

import f1re.engine as engine_module
from f1re.config import AppConfig, RecordingConfig
from f1re.db import get_recording, open_db
from f1re.engine import (
    ENGINE_OFF,
    ENGINE_ON,
    Engine,
    read_engine_state,
    write_engine_state,
)
from f1re.radio.queue import RadioQueue
from f1re.udp.packer import pack_packet
from f1re.udp.recording import RecordingWriter, iter_recording
from f1re.udp.session_uid import demo_session_uid
from f1re.udp.spec import FORMATS, PACKET_FORMAT, Header, PacketId


def _free_udp_port() -> int:
    """지금 비어 있는 UDP 포트 하나(짧은 경합은 감수한다)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _can_bind(port: int) -> bool:
    """같은 UDP 포트로 새 소켓을 바인드할 수 있는지."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


async def _wait_until_bindable(port: int, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _can_bind(port):
            return True
        await asyncio.sleep(0.02)
    return False


def _pending_tasks() -> list[asyncio.Task[object]]:
    """지금 테스트 태스크 말고 살아 있는 태스크(엔진 누수 확인)."""
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]


class _FakeRadio:
    """음성(TTS)·오디오 없이 무전 큐만 제공하는 무전 서비스 대역."""

    def __init__(self) -> None:
        self.queue = RadioQueue()

    def ensure_started(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {
            "voice": {"state": "offline", "label": "음성 오프라인"},
            "radio_page": {
                "state": "disconnected",
                "label": "무전 페이지 미연결",
                "connected": False,
                "clients": 0,
            },
        }


def _radio_factory(_store: Any, _conn: Any) -> _FakeRadio:
    return _FakeRadio()


class _FakeBackupWorker:
    """`BackupWorker.enqueue` 자리. 마감 작업을 받은 순서대로 모은다."""

    def __init__(self) -> None:
        self.jobs: list[Any] = []

    def enqueue(self, job: Any) -> None:
        self.jobs.append(job)


def _recording_config(root: Path, **overrides: Any) -> AppConfig:
    """자동 녹화 폴더만 임시 경로로 바꾼 앱 설정."""
    recording = dataclasses.replace(
        RecordingConfig(enabled=True), dir=str(root), **overrides
    )
    return AppConfig(recording=recording)


def _engine(
    tmp_path: Path,
    port: int,
    *,
    config: AppConfig | None = None,
    backup_worker: Any | None = None,
    recorder: Any | None = None,
    recorded_file: Path | None = None,
) -> Engine:
    return Engine(
        config=config,
        host="127.0.0.1",
        port=port,
        db_path=tmp_path / "f1re.db",
        state_path=tmp_path / "engine-state.json",
        radio_factory=_radio_factory,
        backup_worker=backup_worker,
        recorder=recorder,
        recorded_file=recorded_file,
    )


def _header(uid: int, packet_id: PacketId, frame: int = 1) -> Header:
    return Header(
        packet_format=PACKET_FORMAT,
        game_year=25,
        game_major=1,
        game_minor=0,
        packet_version=1,
        packet_id=int(packet_id),
        session_uid=uid,
        session_time=frame / 60.0,
        frame_id=frame,
        overall_frame_id=frame,
        player_car_index=0,
        secondary_player_car_index=255,
    )


def _event_datagram(code: str, *, uid: int, frame: int = 1) -> bytes:
    """수신기·녹화기가 실제로 받는 것과 같은 UDP 데이터그램 하나."""
    body: dict[str, Any] = {
        "event_string_code": code,
        "event_details": {},
    }
    return pack_packet(_header(uid, PacketId.EVENT, frame), body)


def _session_datagram(
    uid: int, *, session_type: int = 15, track_id: int = 13
) -> bytes:
    """SESSION 패킷 데이터그램. 기본값(0)으로 채운 본문에 값만 덮어쓴다."""
    definition = FORMATS[PACKET_FORMAT].PACKETS[PacketId.SESSION]
    body = definition.body.unpack(bytes(definition.body.size))
    body["session_type"] = session_type
    body["track_id"] = track_id
    return pack_packet(_header(uid, PacketId.SESSION), body)


def _send(port: int, *datagrams: bytes) -> None:
    """루프백 UDP로 데이터그램을 보낸다."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for data in datagrams:
            sock.sendto(data, ("127.0.0.1", port))


async def _wait_until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("조건이 시간 안에 만족되지 않았습니다")
        await asyncio.sleep(0.01)


def _recorded_datagrams(path: Path) -> list[bytes]:
    return [data for _recv_ns, data in iter_recording(path)]


# ---------------------------------------------------------------------------
# 상태 파일


def test_first_run_is_off_when_state_file_is_missing(tmp_path: Path) -> None:
    assert read_engine_state(tmp_path / "engine-state.json") == ENGINE_OFF


def test_state_file_round_trip_and_corrupt_file_falls_back_off(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine-state.json"

    write_engine_state(ENGINE_ON, path)
    assert read_engine_state(path) == ENGINE_ON

    write_engine_state(ENGINE_OFF, path)
    assert read_engine_state(path) == ENGINE_OFF

    # 파일이 깨져도 서버는 OFF로 시작한다.
    path.write_text("{깨진 json", encoding="utf-8")
    assert read_engine_state(path) == ENGINE_OFF

    with pytest.raises(ValueError):
        write_engine_state("maybe", path)


# ---------------------------------------------------------------------------
# 소켓·상태 파일 수명 주기


async def test_on_opens_udp_and_off_closes_it(tmp_path: Path) -> None:
    port = _free_udp_port()
    state_path = tmp_path / "engine-state.json"
    engine = _engine(tmp_path, port)

    assert engine.state == ENGINE_OFF
    assert engine.udp_port is None

    await engine.start()
    try:
        assert engine.state == ENGINE_ON
        assert engine.udp_port == port
        assert engine.store is not None
        assert engine.radio is not None
        assert engine.conn is not None
        # ON인 동안에는 같은 포트에 새 소켓을 바인드할 수 없다.
        assert not _can_bind(port)
        assert read_engine_state(state_path) == ENGINE_ON
    finally:
        await engine.stop()

    assert engine.state == ENGINE_OFF
    # 상태 저장소·무전 큐·지도·DB 참조를 모두 끊는다.
    assert engine.store is None
    assert engine.radio is None
    assert engine.conn is None
    assert engine.udp_port is None
    assert read_engine_state(state_path) == ENGINE_OFF
    # UDP 포트가 실제로 닫혀 같은 포트로 새 소켓을 바인드할 수 있다.
    assert await _wait_until_bindable(port)


async def test_shutdown_keeps_last_saved_state(tmp_path: Path) -> None:
    """서버 종료는 사용자가 끈 것이 아니다. 재시작하면 켜 둔 ON이 이어진다."""
    port = _free_udp_port()
    state_path = tmp_path / "engine-state.json"
    engine = _engine(tmp_path, port)

    await engine.start()
    assert read_engine_state(state_path) == ENGINE_ON

    await engine.aclose()  # systemctl restart 등 서버 종료 경로
    assert engine.state == ENGINE_OFF
    assert read_engine_state(state_path) == ENGINE_ON
    assert await _wait_until_bindable(port)


async def test_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    port = _free_udp_port()
    engine = _engine(tmp_path, port)

    await engine.start()
    await engine.start()  # 같은 상태로 다시 요청해도 성공하고 소켓은 하나다.
    assert engine.state == ENGINE_ON
    assert engine.udp_port == port

    await engine.stop()
    await engine.stop()
    assert engine.state == ENGINE_OFF
    assert await _wait_until_bindable(port)

    assert await engine.set_state(ENGINE_ON) == ENGINE_ON
    assert await engine.set_state(ENGINE_ON) == ENGINE_ON
    await engine.stop()
    assert engine.state == ENGINE_OFF

    with pytest.raises(ValueError):
        await engine.set_state("maybe")


async def test_on_off_cycles_do_not_leak_tasks_or_sockets(tmp_path: Path) -> None:
    port = _free_udp_port()
    engine = _engine(tmp_path, port)
    baseline = len(_pending_tasks())

    for _ in range(3):
        await engine.start()
        assert not _can_bind(port)
        await engine.stop()
        await asyncio.sleep(0.05)
        assert engine.state == ENGINE_OFF
        assert _pending_tasks() == [], "OFF인데 엔진 태스크가 남아 있습니다"
        assert await _wait_until_bindable(port)

    assert len(_pending_tasks()) == baseline


# ---------------------------------------------------------------------------
# 자동 원본 녹화(SCRUM-106)


async def test_engine_auto_records_and_submits_to_backup_worker_on_stop(
    tmp_path: Path,
) -> None:
    """수신한 원본이 `.active/`에 쌓이고, 엔진 종료 시 engine_off로 마감된다."""
    uid = 0x1122_3344_5566_7788
    port = _free_udp_port()
    worker = _FakeBackupWorker()
    config = _recording_config(tmp_path / "recordings")
    engine = _engine(tmp_path, port, config=config, backup_worker=worker)

    await engine.start()
    try:
        manager = engine.recording_manager
        assert manager is not None, "자동 녹화기가 만들어지지 않았습니다"
        assert engine.udp_port == port

        datagram = _event_datagram("SSTA", uid=uid)
        _send(port, datagram)
        # 원본 파일이 열리고 데이터그램이 기록될 때까지 기다린다.
        await _wait_until(lambda: manager.active_path is not None)

        active_path = manager.active_path
        assert active_path.name == f"{uid}_s1.f1rec"
        assert active_path.parent.name == ".active"
        assert manager.active_session_uid == uid
        # 압축·업로드는 하지 않는다. 마감 작업은 아직 큐로 가지 않았다.
        assert worker.jobs == []
    finally:
        await engine.stop()

    assert engine.recording_manager is None
    assert len(worker.jobs) == 1
    job = worker.jobs[0]
    assert job.end_reason == "engine_off"
    assert job.session_uid == uid
    assert job.segment == 1
    # 활성 파일은 워커가 가져가기 전까지 `.active/`에 원본 그대로 남는다.
    assert _recorded_datagrams(job.active_path) == [datagram]

    conn = open_db(tmp_path / "f1re.db")
    try:
        row = get_recording(conn, job.recording_id)
    finally:
        conn.close()
    assert row is not None
    assert row["session_uid"] == uid
    assert row["segment"] == 1
    # 마감 통계·압축·업로드는 백업 워커가 채운다.
    assert row["end_reason"] == "recording"


async def test_engine_session_switch_closes_previous_recording(
    tmp_path: Path,
) -> None:
    """sessionUID가 바뀌면 이전 파일을 session_switch로 마감하고 새로 연다."""
    first_uid = 0x1000_0000_0000_0001
    second_uid = 0x2000_0000_0000_0002
    port = _free_udp_port()
    worker = _FakeBackupWorker()
    config = _recording_config(tmp_path / "recordings")
    engine = _engine(tmp_path, port, config=config, backup_worker=worker)

    await engine.start()
    try:
        _send(port, _event_datagram("SSTA", uid=first_uid))
        manager = engine.recording_manager
        await _wait_until(lambda: manager.active_session_uid == first_uid)

        _send(port, _event_datagram("SSTA", uid=second_uid))
        await _wait_until(lambda: manager.active_session_uid == second_uid)
    finally:
        await engine.stop()

    assert [job.end_reason for job in worker.jobs] == [
        "session_switch",
        "engine_off",
    ]
    assert [job.session_uid for job in worker.jobs] == [first_uid, second_uid]
    assert [job.segment for job in worker.jobs] == [1, 1]


async def test_engine_inactivity_timeout_closes_and_resumes_as_segment_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """무수신 타임아웃 마감 뒤 같은 세션이 돌아오면 2부(`_s2`)로 잇는다."""
    uid = 0x3000_0000_0000_0003
    port = _free_udp_port()
    worker = _FakeBackupWorker()
    config = _recording_config(
        tmp_path / "recordings", inactivity_timeout_s=0.05
    )
    engine = _engine(tmp_path, port, config=config, backup_worker=worker)
    # 1초 주기를 기다리지 않고 타임아웃을 확인한다.
    monkeypatch.setattr(engine_module, "PERSIST_INTERVAL_S", 0.02)

    await engine.start()
    try:
        _send(port, _event_datagram("SSTA", uid=uid))
        manager = engine.recording_manager
        await _wait_until(lambda: manager.active_session_uid == uid)
        await _wait_until(
            lambda: bool(worker.jobs)
            and worker.jobs[0].end_reason == "inactivity_timeout"
        )
        first_job = worker.jobs[0]
        assert first_job.session_uid == uid
        assert first_job.segment == 1
        assert manager.active_path is None

        # 재개 뒤에는 주기 점검을 멈춰 테스트가 2부를 닫아 버리지 않게 한다.
        monkeypatch.setattr(engine_module, "PERSIST_INTERVAL_S", 60.0)
        _send(port, _event_datagram("RDFL", uid=uid, frame=2))
        await _wait_until(
            lambda: manager.active_path is not None
            and manager.active_path.name == f"{uid}_s2.f1rec"
        )
    finally:
        await engine.stop()

    assert [job.end_reason for job in worker.jobs] == [
        "inactivity_timeout",
        "engine_off",
    ]
    assert [job.segment for job in worker.jobs] == [1, 2]


async def test_engine_manual_record_and_auto_recording_run_together(
    tmp_path: Path,
) -> None:
    """`--record` 수동 녹화와 자동 세션 녹화가 같은 데이터그램을 함께 받는다."""
    uid = 0x4000_0000_0000_0004
    port = _free_udp_port()
    worker = _FakeBackupWorker()
    config = _recording_config(tmp_path / "recordings")
    manual_path = tmp_path / "manual" / "session.f1rec"
    writer = RecordingWriter(manual_path)
    engine = _engine(
        tmp_path,
        port,
        config=config,
        backup_worker=worker,
        recorder=writer,
        recorded_file=manual_path,
    )

    await engine.start()
    try:
        datagram = _event_datagram("SSTA", uid=uid)
        _send(port, datagram)
        manager = engine.recording_manager
        await _wait_until(lambda: manager.active_path is not None)
        await _wait_until(lambda: writer.record_count == 1)
    finally:
        await engine.stop()
        writer.close()

    assert _recorded_datagrams(manual_path) == [datagram]
    assert len(worker.jobs) == 1
    assert worker.jobs[0].end_reason == "engine_off"
    assert _recorded_datagrams(worker.jobs[0].active_path) == [datagram]


async def test_engine_buffers_finalize_jobs_until_backup_worker_attaches(
    tmp_path: Path,
) -> None:
    """웹 lifespan의 워커가 엔진보다 늦게 붙어도 마감 작업을 잃지 않는다."""
    uid = 0x5000_0000_0000_0005
    port = _free_udp_port()
    config = _recording_config(tmp_path / "recordings")
    engine = _engine(tmp_path, port, config=config)

    await engine.start()
    try:
        _send(port, _event_datagram("SSTA", uid=uid))
        manager = engine.recording_manager
        await _wait_until(lambda: manager.active_path is not None)
    finally:
        await engine.stop()

    assert engine.backup_worker is None
    worker = _FakeBackupWorker()
    engine.attach_backup_worker(worker)
    assert engine.backup_worker is worker
    assert [job.end_reason for job in worker.jobs] == ["engine_off"]
    assert worker.jobs[0].session_uid == uid


async def test_engine_session_packet_fills_recording_category(
    tmp_path: Path,
) -> None:
    """SESSION 패킷이 늦게 와도 녹화 행·마감 작업의 범주가 갱신된다."""
    uid = 0x6000_0000_0000_0006
    port = _free_udp_port()
    worker = _FakeBackupWorker()
    config = _recording_config(tmp_path / "recordings")
    engine = _engine(tmp_path, port, config=config, backup_worker=worker)

    await engine.start()
    try:
        # 첫 데이터그램(EVENT)에는 세션 종류가 없어 unknown으로 열린다.
        _send(port, _event_datagram("SSTA", uid=uid))
        manager = engine.recording_manager
        await _wait_until(lambda: manager.active_path is not None)

        _send(port, _session_datagram(uid, session_type=15, track_id=13))
        reader = open_db(tmp_path / "f1re.db")
        try:
            await _wait_until(
                lambda: (
                    reader.execute(
                        "SELECT category FROM session_recordings "
                        "WHERE session_uid = ? AND segment = 1",
                        (uid,),
                    ).fetchone()
                    or [None]
                )[0]
                == "race"
            )
        finally:
            reader.close()
    finally:
        await engine.stop()

    assert len(worker.jobs) == 1
    job = worker.jobs[0]
    assert job.category == "race"
    assert job.session_type == 15
    assert job.track_id == 13


async def test_engine_ignores_demo_session_packets(tmp_path: Path) -> None:
    """`f1re-demo` 세션은 자동 녹화·백업 대상이 아니다(설계 4.1)."""
    uid = demo_session_uid(3)
    port = _free_udp_port()
    worker = _FakeBackupWorker()
    config = _recording_config(tmp_path / "recordings")
    engine = _engine(tmp_path, port, config=config, backup_worker=worker)

    await engine.start()
    try:
        _send(port, _event_datagram("SSTA", uid=uid))
        # 수신기는 패킷을 처리했지만 자동 녹화기는 세션을 열지 않는다.
        await _wait_until(
            lambda: engine.store is not None
            and engine.store.diagnostics.total_packets >= 1
        )
        manager = engine.recording_manager
        assert manager is not None
        assert manager.active_path is None
    finally:
        await engine.stop()

    assert worker.jobs == []


async def test_engine_session_end_event_closes_with_session_end_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """게임의 SEND 이벤트는 1초 루프를 거쳐 session_end로 마감된다."""
    uid = 0x7000_0000_0000_0007
    port = _free_udp_port()
    worker = _FakeBackupWorker()
    config = _recording_config(tmp_path / "recordings")
    engine = _engine(tmp_path, port, config=config, backup_worker=worker)
    # 1초 주기를 기다리지 않고 사건을 DB·녹화기에 반영한다.
    monkeypatch.setattr(engine_module, "PERSIST_INTERVAL_S", 0.02)

    await engine.start()
    try:
        _send(port, _event_datagram("SSTA", uid=uid))
        manager = engine.recording_manager
        await _wait_until(lambda: manager.active_path is not None)

        _send(port, _event_datagram("SEND", uid=uid, frame=2))
        await _wait_until(lambda: bool(worker.jobs))
        assert worker.jobs[0].end_reason == "session_end"
        assert worker.jobs[0].session_uid == uid
    finally:
        await engine.stop()

    # 이미 마감된 뒤라 엔진 종료가 새 마감 작업을 만들지 않는다.
    assert [job.end_reason for job in worker.jobs] == ["session_end"]
