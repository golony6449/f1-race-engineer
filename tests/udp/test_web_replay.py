"""웹 다시 보기 `ReplayManager`: 메모리 전용 파이프라인·경고·시크 검증.

공용 conftest를 만들지 않는 규칙에 따라 패킷 생성기·가상 시계·대기 도우미를
이 파일 안에 둔다. 실제 `rclone`·GCS·네트워크·DB 쓰기는 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from f1re.db import open_db, save_session, save_track_outline
from f1re.radio.log import RadioLog
from f1re.radio.queue import RadioKind, RadioMessage
from f1re.tracks.outline import TrackOutline
from f1re.udp.formats import f2025
from f1re.udp.packer import pack_packet
from f1re.udp.recording import RecordingWriter, compress_recording
from f1re.udp.schema import (
    MatrixField,
    RawField,
    ScalarField,
    StringField,
    StructField,
)
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId
from f1re.udp.web_replay import ReplayManager

SESSION_UID = 0x0123_4567_89AB_CDEF
# 녹화 첫 패킷의 수신 시각. 실제 게임처럼 0이 아닌 절대 시각(ns)을 쓴다.
BASE_NS = 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# 패킷 생성기(테스트 파일 안에 둔다)
# ---------------------------------------------------------------------------


def _default_value(field_def: Any) -> Any:
    if isinstance(field_def, ScalarField):
        zero: int | float = 0.0 if field_def.fmt in "fd" else 0
        return zero if field_def.count == 1 else [zero] * field_def.count
    if isinstance(field_def, StringField):
        return ""
    if isinstance(field_def, RawField):
        return bytes(field_def.length)
    if isinstance(field_def, MatrixField):
        return [[0] * field_def.cols for _ in range(field_def.rows)]
    if isinstance(field_def, StructField):
        if field_def.count is None:
            return _default_struct_body(field_def.struct)
        return [_default_struct_body(field_def.struct) for _ in range(field_def.count)]
    raise TypeError(f"알 수 없는 필드 정의: {field_def!r}")


def _default_struct_body(struct_def: Any) -> dict[str, Any]:
    return {field.key: _default_value(field) for field in struct_def.fields}


def _default_body(packet_id: PacketId) -> dict[str, Any]:
    return _default_struct_body(f2025.PACKETS[packet_id].body)


class PacketBuilder:
    """테스트용 UDP 데이터그램 생성기. `frame`을 순서대로 올린다."""

    def __init__(
        self, uid: int = SESSION_UID, player_car_index: int = 0
    ) -> None:
        self.uid = uid
        self.player_car_index = player_car_index
        self.frame = 0

    def raw(self, packet_id: PacketId, body: dict[str, Any]) -> bytes:
        self.frame += 1
        header = Header(
            packet_format=PACKET_FORMAT,
            game_year=25,
            game_major=1,
            game_minor=0,
            packet_version=1,
            packet_id=int(packet_id),
            session_uid=self.uid,
            session_time=self.frame / 60.0,
            frame_id=self.frame,
            overall_frame_id=self.frame,
            player_car_index=self.player_car_index,
            secondary_player_car_index=255,
        )
        return pack_packet(header, body)

    def session(self, **overrides: Any) -> bytes:
        body = _default_body(PacketId.SESSION)
        body.update(
            weather=0, track_id=13, session_type=13, game_paused=0, total_laps=0
        )
        body.update(overrides)
        return self.raw(PacketId.SESSION, body)

    def participants(self, *, player_team_id: int = 0) -> bytes:
        body = _default_body(PacketId.PARTICIPANTS)
        body["num_active_cars"] = 22
        for entry in body["participants"]:
            entry["team_id"] = 10
        body["participants"][self.player_car_index]["team_id"] = player_team_id
        return self.raw(PacketId.PARTICIPANTS, body)

    def lap(self, *, car: int = 0, lap: int = 1, distance: float = 0.0) -> bytes:
        body = _default_body(PacketId.LAP_DATA)
        entry = body["lap_data"][car]
        entry.update(current_lap_num=lap, lap_distance=distance)
        return self.raw(PacketId.LAP_DATA, body)

    def telemetry(
        self,
        *,
        car: int = 0,
        speed: int = 0,
        gear: int = 0,
        throttle: float = 0.0,
        brake: float = 0.0,
        steer: float = 0.0,
    ) -> bytes:
        body = _default_body(PacketId.CAR_TELEMETRY)
        entry = body["car_telemetry_data"][car]
        entry.update(
            speed=speed,
            gear=gear,
            throttle=throttle,
            brake=brake,
            steer=steer,
        )
        return self.raw(PacketId.CAR_TELEMETRY, body)

    def damage(self, *, car: int = 0, wear: float = 0.0) -> bytes:
        body = _default_body(PacketId.CAR_DAMAGE)
        body["car_damage_data"][car]["tyres_wear"] = [wear, wear, wear, wear]
        return self.raw(PacketId.CAR_DAMAGE, body)


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


class FakeClock:
    """실제로 기다리지 않는 시계. `sleep`이 시계를 그만큼 앞으로 돌린다."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


def write_recording(
    path: Path, records: list[tuple[int, bytes]], *, compress: bool = False
) -> Path:
    """(recv_ns, 데이터그램) 목록을 `.f1rec`(또는 `.f1rec.zst`)로 쓴다."""
    with RecordingWriter(path) as writer:
        for recv_ns, data in records:
            writer.write(recv_ns, data)
    if not compress:
        return path
    target = path.with_name(path.name + ".zst")
    compress_recording(path, target)
    path.unlink()
    return target


def recording_meta(**overrides: Any) -> dict[str, Any]:
    """`session_recordings` 행 모양. 리플레이가 읽는 값만 채운다."""
    meta: dict[str, Any] = {
        "id": 7,
        "session_uid": SESSION_UID,
        "segment": 1,
        "category": "race",
        "session_type": 10,
        "track_id": 13,
        "duration_ms": 300,
    }
    meta.update(overrides)
    return meta


def _replay(manager: ReplayManager) -> dict[str, Any]:
    return manager.state_payload()["replay"]


def _snapshot_packets(manager: ReplayManager) -> dict[str, Any]:
    return manager.state_payload()["snapshot"]["packets"]


def _telemetry_speed(manager: ReplayManager, car: int = 0) -> int | None:
    packets = _snapshot_packets(manager)
    body = packets.get("car_telemetry")
    if not isinstance(body, dict):
        return None
    entry = body["car_telemetry_data"][car]
    return entry.get("speed")


def _messages(manager: ReplayManager) -> list[dict[str, Any]]:
    return manager.radio_log_payload(limit=None)["messages"]


async def wait_until(
    predicate: Callable[[], bool], timeout_s: float = 5.0, *, what: str = "조건"
) -> None:
    """조건이 참이 될 때까지 이벤트 루프를 양보하며 기다린다.

    `asyncio.sleep(0)`으로 양보해 가상 시계로 도는 재생 태스크와 한 패킷씩
    번갈아 실행된다(실제 시계를 기다리지 않는다).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"{what}이(가) 제한 시간 안에 만족되지 않았습니다")


async def wait_finished(manager: ReplayManager, timeout_s: float = 5.0) -> None:
    """재생 끝(위치 == 길이, 일시정지)까지 기다린다."""

    def finished() -> bool:
        replay = _replay(manager)
        return (
            bool(replay["paused"])
            and int(replay["position_ms"]) >= int(replay["duration_ms"])
        )

    await wait_until(finished, timeout_s, what="재생 끝")


def _seed_db(path: Path) -> sqlite3.Connection:
    """기존 세션·무전 행이 있는 `f1re.db`를 만들어 둔다."""
    conn = open_db(path)
    save_session(conn, SESSION_UID, started_at_ns=1)
    RadioLog(conn, session_uid=SESSION_UID).record(
        RadioMessage.create(
            kind=RadioKind.COACHING,
            text="기존 무전",
            now_ns=1,
            ttl_ns=1_000_000,
            evidence={"seed": True},
        ),
        played=True,
        latency_ms=3,
    )
    return conn


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sessions", "reference_sets", "radio_messages")
    }


# ---------------------------------------------------------------------------
# 1. 파이프라인·경고가 DB·TTS 없이 돌아간다
# ---------------------------------------------------------------------------


async def test_replay_runs_pipeline_and_alerts_without_db_or_tts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`.f1rec.zst` 재생에서 파이프라인·위험 경고가 돌고 DB·Qwen은 쓰지 않는다."""
    # 기존 세션·무전 행이 있는 DB를 만들고, 리플레이 중에는 새 DB 연결도
    # 네트워크(Qwen TTS)도 열리지 않는지 확인한다.
    conn = _seed_db(tmp_path / "f1re.db")
    before = _row_counts(conn)
    assert before["radio_messages"] == 1

    external_calls: list[str] = []

    def fail_connect(*args: Any, **kwargs: Any) -> Any:
        external_calls.append("sqlite3.connect")
        raise AssertionError("리플레이 중에는 f1re.db를 열면 안 됩니다")

    def fail_client(*args: Any, **kwargs: Any) -> Any:
        external_calls.append("httpx.AsyncClient")
        raise AssertionError("리플레이 중에는 Qwen TTS를 부르면 안 됩니다")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    monkeypatch.setattr(httpx, "AsyncClient", fail_client)

    builder = PacketBuilder()
    records = [
        # 경쟁 세션(10)이라 상대 참가자 팀 정보가 있어야 코치가 만들어진다.
        (BASE_NS, builder.session(session_type=10, total_laps=10)),
        (BASE_NS + 20_000_000, builder.participants(player_team_id=0)),
        (BASE_NS + 40_000_000, builder.lap(lap=1, distance=100.0)),
        (BASE_NS + 60_000_000, builder.telemetry(speed=120, gear=4, throttle=0.8)),
        # 타이어 마모 90%(DANGER 75%)와 레이스 total_laps>0 → 안전 경고 + 박스 지시.
        (BASE_NS + 80_000_000, builder.damage(wear=90.0)),
        (BASE_NS + 150_000_000, builder.telemetry(speed=150, gear=5, throttle=0.9)),
        (BASE_NS + 250_000_000, builder.telemetry(speed=160, gear=5, throttle=1.0)),
    ]
    path = write_recording(tmp_path / "race.f1rec", records, compress=True)
    assert path.name.endswith(".f1rec.zst")

    clock = FakeClock()
    manager = ReplayManager(clock=clock, sleep=clock.sleep)
    try:
        await manager.start(recording_meta(), path)
        await wait_finished(manager)

        # 파이프라인이 실제로 세션을 구성했다(서킷 데이터 파일 13 = 스즈카).
        report = manager.report_payload()
        assert report["available"] is True
        assert report["pipeline"]["track_id"] == 13
        assert report["pipeline"]["track_name"] == "스즈카 서킷"
        assert manager.track_payload()["corners"]

        # 상태 응답은 실시간과 같은 구조에 replay 메타가 더해진다.
        state = manager.state_payload()
        for key in ("snapshot", "car", "connection", "cars", "flag", "flags", "status"):
            assert key in state
        assert state["replay"]["active"] is True
        assert state["replay"]["position_ms"] == state["replay"]["duration_ms"]
        # 과거 녹화 시각(ns)을 실제 시각과 비교하지 않고 가상 시계로 본다.
        assert state["connection"]["stale"] is False
        assert state["status"]["voice"]["state"] == "offline"

        # 안전 경고(타이어 마모)와 박스 지시가 메모리 무전 기록에 남는다.
        messages = _messages(manager)
        kinds = {row["kind"] for row in messages}
        assert RadioKind.SAFETY.value in kinds
        assert RadioKind.BOX.value in kinds
        assert all(row["played"] is True for row in messages)
        assert any("타이어" in row["text"] for row in messages if row["kind"] == "safety")
        # 리포트도 같은 메모리 기록을 근거로 삼는다.
        assert report["message_count"] == len(messages)
        assert report["corner_losses"] == []
        assert report["summary"]["messages"] == len(messages)

        # Qwen·새 DB 연결은 한 번도 일어나지 않았다.
        assert external_calls == []
        # 기존 `f1re.db`의 세 테이블은 그대로다(쓰기 없음).
        assert _row_counts(conn) == before
    finally:
        await manager.aclose()
        conn.close()


# ---------------------------------------------------------------------------
# 2. 일시정지·재개·배속·정지
# ---------------------------------------------------------------------------


async def test_replay_pause_resume_speed_and_stop(tmp_path: Path) -> None:
    """일시정지 중에는 위치가 멈추고, 재개·배속·정지가 상태에 반영된다."""
    builder = PacketBuilder()
    records = [
        (BASE_NS + index * 50_000_000, builder.telemetry(speed=100 + index))
        for index in range(21)
    ]
    path = write_recording(tmp_path / "long.f1rec", records)
    clock = FakeClock()
    manager = ReplayManager(clock=clock, sleep=clock.sleep)

    try:
        await manager.start(recording_meta(duration_ms=1000), path, speed=1.0)
        replay = _replay(manager)
        assert replay["active"] is True
        assert replay["recording_id"] == 7
        assert manager.active_recording_id == 7
        assert replay["speed"] == 1.0

        await wait_until(
            lambda: _replay(manager)["position_ms"] >= 200,
            what="200ms 지점 재생",
        )
        await manager.pause()
        paused = _replay(manager)
        assert paused["paused"] is True
        assert paused["active"] is True
        position = int(paused["position_ms"])

        # 이벤트 루프를 여러 번 양보해도 일시정지 중에는 앞으로 가지 않는다.
        for _ in range(50):
            await asyncio.sleep(0)
        assert int(_replay(manager)["position_ms"]) == position

        await manager.resume()
        assert _replay(manager)["paused"] is False
        await wait_until(
            lambda: int(_replay(manager)["position_ms"]) > position,
            what="재개 뒤 재생",
        )

        await manager.set_speed(10.0)
        assert _replay(manager)["speed"] == 10.0
        await wait_finished(manager)
        finished = _replay(manager)
        assert finished["position_ms"] == 1000
        assert finished["active"] is True
    finally:
        await manager.stop()

    stopped = _replay(manager)
    assert stopped["active"] is False
    assert stopped["recording_id"] is None
    assert stopped["position_ms"] == 0
    assert manager.active_recording_id is None


# ---------------------------------------------------------------------------
# 3. 시크: 전이 기억 초기화 후 목표 지점까지 재구성
# ---------------------------------------------------------------------------


async def test_replay_seek_resets_state_and_rebuilds_to_position(
    tmp_path: Path,
) -> None:
    """시크는 상태·파이프라인·경고 기억을 새로 만들고 목표 직전까지 재구성한다."""
    builder = PacketBuilder()
    records = [
        (BASE_NS, builder.session(session_type=13)),
        (BASE_NS + 10_000_000, builder.damage(wear=90.0)),
        (BASE_NS + 100_000_000, builder.telemetry(speed=15, gear=2)),
        (BASE_NS + 200_000_000, builder.telemetry(speed=20, gear=3)),
        (BASE_NS + 400_000_000, builder.telemetry(speed=30, gear=4)),
        (BASE_NS + 500_000_000, builder.telemetry(speed=111, gear=7)),
        (BASE_NS + 800_000_000, builder.telemetry(speed=40, gear=5)),
    ]
    path = write_recording(tmp_path / "seek.f1rec", records)
    clock = FakeClock()
    manager = ReplayManager(clock=clock, sleep=clock.sleep)

    try:
        await manager.start(recording_meta(duration_ms=1000), path)
        await manager.pause()

        # 0으로 시크하면 첫 패킷도 아직 재생 전이다("직전까지").
        await manager.seek(0)
        assert _replay(manager)["position_ms"] == 0
        assert _snapshot_packets(manager) == {}
        assert _messages(manager) == []

        # 400ms로 시크: 400ms 패킷은 아직 적용하지 않고 200ms 상태까지 만든다.
        await manager.seek(400)
        replay = _replay(manager)
        assert replay["position_ms"] == 400
        assert replay["active"] is True
        assert _telemetry_speed(manager) == 20
        # 시크 재구성 중 나온 무전이 그 시점까지 다시 쌓인다(타이어 경고 1회).
        rows = _messages(manager)
        assert [row["kind"] for row in rows] == [RadioKind.SAFETY.value]
        assert rows[0]["recorded_at_ns"] >= BASE_NS

        # 파이프라인도 해당 시점 세션으로 다시 만들어졌다.
        report = manager.report_payload()
        assert report["pipeline"]["track_id"] == 13
        assert report["available"] is True

        # 500ms로 시크: 400ms 패킷은 적용되고 500ms 패킷은 아직 아니다.
        await manager.seek(500)
        assert _telemetry_speed(manager) == 30
        assert _replay(manager)["position_ms"] == 500

        # 재개해 끝까지 재생해도 이미 지나온 경고를 다시 말하지 않는다.
        await manager.resume()
        await wait_finished(manager)
        assert _telemetry_speed(manager) == 40
        assert _replay(manager)["position_ms"] == 1000
        assert [row["kind"] for row in _messages(manager)] == [
            RadioKind.SAFETY.value
        ]
    finally:
        await manager.aclose()


# ---------------------------------------------------------------------------
# 4. 지도 윤곽은 DB에서 읽기만 한다
# ---------------------------------------------------------------------------


def _read_conn_factory(db_path: Path) -> Callable[[], sqlite3.Connection]:
    """리플레이가 쓸 읽기 전용 연결 공장. 마이그레이션 같은 쓰기는 하지 않는다."""

    def factory() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    return factory


async def test_replay_reads_learned_outline_without_writing_db(
    tmp_path: Path,
) -> None:
    """학습 윤곽이 DB에 있으면 리플레이 지도가 그걸 읽고, 테이블은 그대로다."""
    db_path = tmp_path / "f1re.db"
    conn = _seed_db(db_path)
    # 데이터 파일이 없는 서킷(99)이라야 저장된 학습 윤곽을 읽는다.
    save_track_outline(
        conn,
        99,
        TrackOutline(
            [(0.0, 0.0, 0.0), (100.0, 100.0, 0.0), (200.0, 100.0, 100.0)]
        ),
    )
    before = _row_counts(conn)

    builder = PacketBuilder()
    path = write_recording(
        tmp_path / "learned.f1rec",
        [
            (BASE_NS, builder.session(track_id=99, session_type=13)),
            (BASE_NS + 50_000_000, builder.telemetry(speed=80, gear=3)),
        ],
    )
    clock = FakeClock()
    manager = ReplayManager(
        clock=clock,
        sleep=clock.sleep,
        conn_factory=_read_conn_factory(db_path),
    )
    try:
        await manager.start(recording_meta(track_id=99, duration_ms=100), path)
        await wait_finished(manager)

        track = manager.track_payload()
        assert track["outline_source"] == "learned"
        assert track["outline"]
        assert track["track_id"] == 99
        # 어떤 테이블에도 쓰지 않았다.
        assert _row_counts(conn) == before
    finally:
        await manager.aclose()
        conn.close()


# ---------------------------------------------------------------------------
# 5. 작은 계약: 잘못된 배속·없는 파일·비활성 상태
# ---------------------------------------------------------------------------
async def test_replay_rejects_invalid_speed_and_missing_file(tmp_path: Path) -> None:
    clock = FakeClock()
    manager = ReplayManager(clock=clock, sleep=clock.sleep)
    try:
        with pytest.raises(FileNotFoundError):
            await manager.start(recording_meta(), tmp_path / "none.f1rec")
        path = write_recording(tmp_path / "one.f1rec", [(BASE_NS, b"x")])
        with pytest.raises(ValueError):
            await manager.start(recording_meta(), path, speed=0)
        with pytest.raises(ValueError):
            await manager.set_speed(float("nan"))
    finally:
        await manager.aclose()


async def test_replay_state_is_inactive_before_start(tmp_path: Path) -> None:
    clock = FakeClock()
    manager = ReplayManager(clock=clock, sleep=clock.sleep)
    try:
        payload = manager.state_payload()
        for key in ("snapshot", "car", "connection", "cars", "flag", "flags", "status"):
            assert key in payload
        assert payload["replay"]["active"] is False
        assert manager.active_recording_id is None
        assert manager.report_payload()["available"] is False
        assert manager.radio_log_payload()["messages"] == []
        assert manager.track_payload()["corners"] == []
        # 제어는 리플레이가 없으면 조용히 상태만 돌려준다.
        assert (await manager.pause())["replay"]["active"] is False
        assert (await manager.resume())["replay"]["active"] is False
        assert (await manager.seek(1000))["replay"]["active"] is False
        await manager.stop()
    finally:
        await manager.aclose()
