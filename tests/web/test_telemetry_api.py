"""다시 보기 텔레메트리 API·`/telemetry` 라우트 테스트(SCRUM-135, 계획 Task 3).

확인 항목:

- `POST /api/replay/telemetry/open`으로 인덱스를 연 뒤 `summary` → `series` →
  `map_frames`가 이어서 동작하고, 어느 호출도 전역 `ReplayManager` 재생 상태를
  바꾸지 않는다(계획 Review Focus 4).
- 없는 녹화는 404, 아직 녹화 중(`end_reason == "recording"`)인 녹화는 409,
  모르는 메트릭 키는 400으로 답한다(Review Focus 3·5).
- 미수신 구간의 `NaN`은 응답에서 빠지고(JSON `NaN` 직렬화 오류 방지),
  `max_points`·음수 `t_ms`·뒤집힌 구간·범위 밖 `compare_car`는 안전하게
  클램핑·정규화된다(Review Focus 3·5).
- 엔진 OFF에서도 `GET /telemetry`가 200 `text/html`이고, `/telemetry`가 연
  녹화는 `BackupWorker` 로컬 정리에서 보호된다(설계 5.1.E).

공용 conftest를 만들지 않는 규칙에 따라 패킷 생성기·DB/파일 도우미·가짜
다운로더를 이 파일 안에 둔다. 실제 rclone·GCS·네트워크는 부르지 않는다.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from f1re import db
from f1re.config import AppConfig, RecordingConfig
from f1re.engine import ENGINE_OFF, Engine
from f1re.state.store import StateStore
from f1re.udp.backup import BackupWorker
from f1re.udp.formats import f2025
from f1re.udp.gcs_transfer import RecordingFetcher
from f1re.udp.packer import pack_packet
from f1re.udp.recording import RecordingWriter
from f1re.udp.schema import (
    MatrixField,
    RawField,
    ScalarField,
    StringField,
    StructField,
)
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId
from f1re.udp.telemetry_index import TelemetryIndexStore
from f1re.udp.web_replay import ReplayManager
from f1re.web import app as web_app
from f1re.web.replay_api import create_replay_router

SESSION_UID = 0x0F1_2026_1115_0135
# 녹화 첫 패킷의 수신 시각. 실제 게임처럼 0이 아닌 절대 시각(ns)을 쓴다.
BASE_NS = 1_700_000_000_000_000_000
# 합성 녹화의 패킷 간격(ms). 50Hz면 다운샘플·원본 경로를 모두 확인하기에 충분하다.
STEP_MS = 20
# 비공개 텔레메트리 참가자 인덱스(비교 시리즈 억제 확인용).
PRIVATE_CAR = 5
# 내 차 `g_lat`을 일부러 NaN으로 비워 두는 구간(미수신 패킷 흉내).
NAN_FROM_MS = 400
NAN_TO_MS = 600


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
        return [
            _default_struct_body(field_def.struct)
            for _ in range(field_def.count)
        ]
    raise TypeError(f"알 수 없는 필드 정의: {field_def!r}")


def _default_struct_body(struct_def: Any) -> dict[str, Any]:
    return {field.key: _default_value(field) for field in struct_def.fields}


def _default_body(packet_id: PacketId) -> dict[str, Any]:
    return _default_struct_body(f2025.PACKETS[packet_id].body)


class RecordingBuilder:
    """합성 `.f1rec` 레코드를 순서대로 만든다."""

    def __init__(self, *, player_car_index: int = 0) -> None:
        self.player_car_index = player_car_index
        self.frame = 0
        self.records: list[tuple[int, bytes]] = []

    def add(self, packet_id: PacketId, body: dict[str, Any], t_ms: int) -> None:
        self.frame += 1
        header = Header(
            packet_format=PACKET_FORMAT,
            game_year=25,
            game_major=1,
            game_minor=0,
            packet_version=1,
            packet_id=int(packet_id),
            session_uid=SESSION_UID,
            session_time=t_ms / 1000.0,
            frame_id=self.frame,
            overall_frame_id=self.frame,
            player_car_index=self.player_car_index,
            secondary_player_car_index=255,
        )
        self.records.append(
            (BASE_NS + t_ms * 1_000_000, pack_packet(header, body))
        )

    def write(self, path: Path) -> Path:
        with RecordingWriter(path) as writer:
            for recv_ns, data in self.records:
                writer.write(recv_ns, data)
        return path


def _session_body(t_ms: int) -> dict[str, Any]:
    body = _default_body(PacketId.SESSION)
    body.update(
        weather=0,
        track_id=13,
        session_type=15,
        total_laps=5,
        game_paused=0,
        # 200~400ms 사이만 풀 세이프티 카(마커 구간 확인).
        safety_car_status=1 if 200 <= t_ms < 400 else 0,
        num_marshal_zones=0,
    )
    return body


def _participants_body() -> dict[str, Any]:
    body = _default_body(PacketId.PARTICIPANTS)
    body["num_active_cars"] = 22
    for car_index, entry in enumerate(body["participants"]):
        entry["name"] = f"드라이버 {car_index}"
        entry["team_id"] = car_index % 10
        entry["ai_controlled"] = 0
        entry["your_telemetry"] = 0 if car_index == PRIVATE_CAR else 1
    return body


def _lap_data_body(t_ms: int) -> dict[str, Any]:
    body = _default_body(PacketId.LAP_DATA)
    for car_index, entry in enumerate(body["lap_data"]):
        entry.update(
            current_lap_num=1,
            lap_distance=100.0 + car_index * 10.0 + t_ms * 0.05,
            current_lap_time_in_ms=t_ms,
            delta_to_car_in_front_minutes_part=0,
            delta_to_car_in_front_ms_part=500 + car_index,
            delta_to_race_leader_minutes_part=0,
            delta_to_race_leader_ms_part=2500 + car_index,
            car_position=3 if car_index == 0 else (car_index % 20) + 1,
            pit_status=0,
            result_status=1,
        )
    return body


def _telemetry_body(t_ms: int) -> dict[str, Any]:
    body = _default_body(PacketId.CAR_TELEMETRY)
    for car_index, entry in enumerate(body["car_telemetry_data"]):
        entry.update(
            speed=200 + t_ms // 100 if car_index == 0 else 150 + car_index,
            throttle=float(t_ms % 100),
            brake=0.0,
            steer=0.0,
            gear=4,
            engine_rpm=9000,
            drs=0,
            clutch=0,
            engine_temperature=95,
            # F1 25 배열 순서: [RL, RR, FL, FR]
            tyres_surface_temperature=[90, 91, 92, 93],
            tyres_inner_temperature=[80, 81, 82, 83],
            tyres_pressure=[22.0, 22.5, 23.0, 23.5],
            brakes_temperature=[400, 410, 420, 430],
        )
    return body


def _motion_body(t_ms: int, *, nan_motion: bool) -> dict[str, Any]:
    body = _default_body(PacketId.MOTION)
    for car_index, entry in enumerate(body["car_motion_data"]):
        lateral = 0.5 + car_index * 0.1 + t_ms / 10000.0
        if (
            nan_motion
            and car_index == 0
            and NAN_FROM_MS <= t_ms < NAN_TO_MS
        ):
            # 게임이 해당 구간 패킷을 못 보낸 상황과 같은 NaN.
            lateral = math.nan
        entry.update(
            g_force_lateral=lateral,
            g_force_longitudinal=-0.3,
            g_force_vertical=0.1,
        )
    return body


def _status_body() -> dict[str, Any]:
    body = _default_body(PacketId.CAR_STATUS)
    for entry in body["car_status_data"]:
        entry.update(
            fuel_in_tank=50.0,
            fuel_remaining_laps=10.0,
            ers_store_energy=3_000_000.0,
            ers_deployed_this_lap=1_000_000.0,
            ers_deploy_mode=2,
            front_brake_bias=57,
        )
    return body


def _damage_body() -> dict[str, Any]:
    body = _default_body(PacketId.CAR_DAMAGE)
    for entry in body["car_damage_data"]:
        entry.update(
            tyres_wear=[10.0, 11.0, 12.0, 13.0],
            front_left_wing_damage=5,
            front_right_wing_damage=6,
            rear_wing_damage=1,
            floor_damage=2,
            diffuser_damage=3,
            sidepod_damage=4,
            gear_box_damage=2,
            engine_damage=1,
        )
    return body


def _event_body(code: str, details: dict[str, Any]) -> dict[str, Any]:
    body = _default_body(PacketId.EVENT)
    body["event_string_code"] = code
    body["event_details"] = details
    return body


def build_recording(
    path: Path, *, duration_ms: int = 1_000, nan_motion: bool = False
) -> Path:
    """합성 F1 25 레이스 녹화를 만든다(50Hz, 22대)."""
    builder = RecordingBuilder()
    t_ms = 0
    while t_ms < duration_ms:
        if t_ms % 200 == 0:
            builder.add(PacketId.SESSION, _session_body(t_ms), t_ms)
        if t_ms % 500 == 0:
            builder.add(PacketId.PARTICIPANTS, _participants_body(), t_ms)
        builder.add(PacketId.LAP_DATA, _lap_data_body(t_ms), t_ms)
        builder.add(PacketId.CAR_TELEMETRY, _telemetry_body(t_ms), t_ms)
        builder.add(PacketId.MOTION, _motion_body(t_ms, nan_motion=nan_motion), t_ms)
        builder.add(PacketId.CAR_STATUS, _status_body(), t_ms)
        builder.add(PacketId.CAR_DAMAGE, _damage_body(), t_ms)
        if t_ms == 600:
            # 내 차 플래시백 사건 마커(요약의 events에 남는다).
            builder.add(
                PacketId.EVENT,
                _event_body(
                    "FLBK",
                    {"flashback_frame_identifier": 3, "flashback_session_time": 0.5},
                ),
                t_ms,
            )
        t_ms += STEP_MS
    return builder.write(path)


# ---------------------------------------------------------------------------
# DB·파일·앱 도우미
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> tuple[Path, Callable[[], sqlite3.Connection]]:
    """파일 DB와 호출마다 새 연결을 여는 공장(`:memory:`는 연결마다 달라 안 된다)."""
    db_path = tmp_path / "f1re.db"
    db.open_db(db_path).close()
    return db_path, lambda: db.open_db(db_path)


def _add_recording(
    db_path: Path,
    *,
    session_uid: int = SESSION_UID,
    segment: int = 1,
    category: str = "race",
    session_type: int | None = 15,
    track_id: int | None = 13,
    started_at_ns: int = 1_000,
    end_reason: str = "session_end",
    duration_ms: int = 1_000,
    packet_count: int = 100,
    local_path: Path | None = None,
    backup_status: str = "uploaded",
) -> int:
    """마감된 녹화 행 하나. `local_path` 파일이 있으면 로컬 보유 상태다."""
    conn = db.open_db(db_path)
    try:
        recording_id = db.create_recording(
            conn,
            session_uid=session_uid,
            segment=segment,
            started_at_ns=started_at_ns,
            category=category,
            session_type=session_type,
            track_id=track_id,
        )
        db.finalize_recording(
            conn,
            recording_id,
            end_reason=end_reason,
            ended_at_ns=started_at_ns + duration_ms * 1_000_000,
            packet_count=packet_count,
            duration_ms=duration_ms,
            local_path=None if local_path is None else str(local_path),
            local_exists=local_path is not None and local_path.is_file(),
            file_size_bytes=(
                local_path.stat().st_size
                if local_path is not None and local_path.is_file()
                else None
            ),
            backup_status=backup_status,
        )
        return recording_id
    finally:
        conn.close()


class _FakeDownloadTransport:
    """`RecordingFetcher`에 주입하는 가짜 다운로더(실제 rclone 미호출)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def is_available(self) -> bool:
        return True

    async def download(self, remote_object: str, local_path: Path) -> None:
        self.calls.append((remote_object, Path(local_path)))
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"restored-zstd")


class _FakeUploadTransport:
    """`BackupWorker` 업로더. 이 테스트에서는 정리만 확인하므로 부르면 실패."""

    def is_available(self) -> bool:
        return False

    async def upload(self, local_path: Path, remote_object: str) -> None:
        raise AssertionError("업로드를 호출하면 안 됩니다")


def _build_router_app(
    tmp_path: Path,
) -> tuple[FastAPI, Path, TelemetryIndexStore, ReplayManager]:
    """텔레메트리 라우터만 붙인 앱(전역 엔진 없음)."""
    db_path, conn_factory = _make_db(tmp_path)
    config = RecordingConfig(dir=str(tmp_path / "recordings"))
    fetcher = RecordingFetcher(
        conn_factory, config, transport=_FakeDownloadTransport()
    )
    manager = ReplayManager()
    store = TelemetryIndexStore(conn_factory=conn_factory)
    app = FastAPI()
    app.include_router(
        create_replay_router(
            conn_factory=conn_factory,
            fetcher=fetcher,
            replay_manager=manager,
            telemetry_store=store,
        )
    )
    return app, db_path, store, manager


def _wait_ready(
    client: TestClient, store: TelemetryIndexStore, recording_id: int
) -> None:
    """백그라운드 인덱스 빌드가 끝날 때까지 앱 이벤트 루프에서 기다린다."""
    client.portal.call(
        lambda: asyncio.wait_for(store.wait_ready(recording_id), 30)
    )


# ---------------------------------------------------------------------------
# 1. open → summary → series → map_frames 전체 흐름과 ReplayManager 비간섭
# ---------------------------------------------------------------------------


def test_telemetry_open_summary_series_and_map_frames_end_to_end(
    tmp_path: Path,
) -> None:
    app, db_path, store, manager = _build_router_app(tmp_path)
    recording = build_recording(tmp_path / "recordings" / "race" / "tele.f1rec")
    recording_id = _add_recording(db_path, local_path=recording)

    with TestClient(app) as client:
        opened = client.post(
            "/api/replay/telemetry/open", json={"recording_id": recording_id}
        )
        assert opened.status_code == 200, opened.text
        assert opened.json() == {
            "status": "building",
            "recording_id": recording_id,
        }
        _wait_ready(client, store, recording_id)
        assert store.get_ready(recording_id) is not None
        # 열어 둔 녹화는 백업 워커 보호 목록(protected_recording_ids)에 들어간다.
        assert recording_id in store.protected_recording_ids()

        # 이미 준비된 녹화를 다시 열면 재빌드 없이 곧바로 ready다.
        again = client.post(
            "/api/replay/telemetry/open", json={"recording_id": recording_id}
        )
        assert again.json()["status"] == "ready"

        summary = client.get(
            "/api/replay/telemetry/summary",
            params={"recording_id": recording_id},
        )
        assert summary.status_code == 200, summary.text
        payload = summary.json()
        assert payload["status"] == "ready"
        assert payload["recording_id"] == recording_id
        assert len(payload["catalog"]) == 5
        assert payload["presets"]
        assert len(payload["participants"]) == 22
        assert payload["participants"][0]["is_player"] is True
        assert payload["track"]["outline"]
        assert payload["duration_ms"] >= 900
        assert set(payload["markers"]) == {"radio", "events", "intervals", "laps"}
        # 내 차 플래시백 마커가 요약에 남는다.
        assert any(
            event["kind"] == "flashback"
            for event in payload["markers"]["events"]
        )

        series = client.get(
            "/api/replay/telemetry/series",
            params={
                "recording_id": recording_id,
                "metrics": "speed,brake,g_lat",
                "from_ms": 0,
                "to_ms": 1_000,
                "compare_car": 3,
            },
        )
        assert series.status_code == 200, series.text
        body = series.json()
        assert body["recording_id"] == recording_id
        assert body["compare_car"] == 3
        assert body["compare_your_telemetry"] == 1
        assert body["series"]["speed"]["series"]
        # 공개 차량(3번)의 비교 시리즈가 같은 시간축으로 내려온다.
        assert body["series"]["speed"]["compare_series"]
        assert body["series"]["speed"]["compare_restricted"] is False
        # Starlette JSONResponse는 allow_nan=False라 NaN이 섞이면 500이 된다.
        assert "NaN" not in series.text

        single = client.get(
            "/api/replay/telemetry/map_frames",
            params={"recording_id": recording_id, "t_ms": 250},
        )
        assert single.status_code == 200, single.text
        single_body = single.json()
        frame = single_body["frame"]
        assert frame["t_ms"] <= 250
        assert len(frame["cars"]) == 22
        assert single_body["frames"] == [frame]

        ranged = client.get(
            "/api/replay/telemetry/map_frames",
            params={
                "recording_id": recording_id,
                "from_ms": 0,
                "to_ms": 500,
            },
        )
        assert ranged.status_code == 200, ranged.text
        frames = ranged.json()["frames"]
        assert frames
        assert len(frames) < len(store.get_ready(recording_id).map_frames)
        assert [item["t_ms"] for item in frames] == sorted(
            item["t_ms"] for item in frames
        )

        # 텔레메트리 분석은 전역 ReplayManager를 전혀 건드리지 않는다.
        assert manager.active_recording_id is None
        state = client.get("/api/replay/state")
        assert state.status_code == 200
        assert state.json()["replay"]["active"] is False


# ---------------------------------------------------------------------------
# 2. 오류 처리: 없는 녹화 404, 녹화 중 409, 미준비 404
# ---------------------------------------------------------------------------


def test_telemetry_open_errors_404_and_409(tmp_path: Path) -> None:
    app, db_path, _store, _manager = _build_router_app(tmp_path)
    recording = build_recording(
        tmp_path / "recordings" / "race" / "short.f1rec", duration_ms=400
    )
    recording_id = _add_recording(
        db_path, local_path=recording, duration_ms=400
    )
    in_progress = _add_recording(
        db_path,
        session_uid=SESSION_UID + 1,
        local_path=recording,
        end_reason="recording",
        backup_status="recording",
    )

    with TestClient(app) as client:
        missing = client.post(
            "/api/replay/telemetry/open", json={"recording_id": 99999}
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["recording_id"] == 99999

        recording_now = client.post(
            "/api/replay/telemetry/open", json={"recording_id": in_progress}
        )
        assert recording_now.status_code == 409

        # open 전(또는 없는) 녹화의 조회 API는 인덱스가 없으므로 404다.
        for path, params in (
            ("/api/replay/telemetry/summary", {"recording_id": recording_id}),
            (
                "/api/replay/telemetry/series",
                {"recording_id": recording_id, "metrics": "speed"},
            ),
            (
                "/api/replay/telemetry/map_frames",
                {"recording_id": recording_id, "t_ms": 0},
            ),
        ):
            response = client.get(path, params=params)
            assert response.status_code == 404, (path, response.text)


# ---------------------------------------------------------------------------
# 3. 입력 검증: 모르는 메트릭 400
# ---------------------------------------------------------------------------


def test_telemetry_series_rejects_unknown_metric_400(tmp_path: Path) -> None:
    app, db_path, store, _manager = _build_router_app(tmp_path)
    recording = build_recording(
        tmp_path / "recordings" / "race" / "metrics.f1rec", duration_ms=400
    )
    recording_id = _add_recording(
        db_path, local_path=recording, duration_ms=400
    )

    with TestClient(app) as client:
        assert (
            client.post(
                "/api/replay/telemetry/open",
                json={"recording_id": recording_id},
            ).status_code
            == 200
        )
        _wait_ready(client, store, recording_id)

        for metrics in ("not_a_metric", "speed,not_a_metric", ""):
            response = client.get(
                "/api/replay/telemetry/series",
                params={"recording_id": recording_id, "metrics": metrics},
            )
            assert response.status_code == 400, metrics
            assert "message" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 4. 클램핑·정규화·NaN 필터링과 비교 차량 비공개 처리
# ---------------------------------------------------------------------------


def test_telemetry_series_and_map_frames_clamp_inputs_and_filter_nan(
    tmp_path: Path,
) -> None:
    app, db_path, store, _manager = _build_router_app(tmp_path)
    recording = build_recording(
        tmp_path / "recordings" / "race" / "nan.f1rec", nan_motion=True
    )
    recording_id = _add_recording(db_path, local_path=recording)

    with TestClient(app) as client:
        client.post(
            "/api/replay/telemetry/open", json={"recording_id": recording_id}
        )
        _wait_ready(client, store, recording_id)

        # max_points 상한 2000으로 클램핑되고, NaN 구간은 응답에서 빠진다.
        big = client.get(
            "/api/replay/telemetry/series",
            params={
                "recording_id": recording_id,
                "metrics": "g_lat",
                "max_points": 99_999,
            },
        )
        assert big.status_code == 200, big.text
        assert big.json()["max_points"] == 2_000
        points = big.json()["series"]["g_lat"]["series"]
        assert points
        assert all(math.isfinite(value) for _t_ms, value in points)
        assert "NaN" not in big.text
        times = [t_ms for t_ms, _value in points]
        # NaN 구간 앞(0~380ms)과 뒤(600ms~)의 유효 점은 남는다.
        assert any(t_ms < NAN_FROM_MS for t_ms in times)
        assert any(t_ms >= NAN_TO_MS for t_ms in times)
        assert not any(NAN_FROM_MS <= t_ms < NAN_TO_MS for t_ms in times)

        # 하한 10으로 올린다(0을 보내도 빈 그래프가 되지 않는다).
        tiny = client.get(
            "/api/replay/telemetry/series",
            params={
                "recording_id": recording_id,
                "metrics": "speed",
                "max_points": 0,
            },
        )
        assert tiny.status_code == 200
        assert tiny.json()["max_points"] == 10

        # 뒤집힌 구간은 두 값을 바꿔 받아들인다.
        flipped = client.get(
            "/api/replay/telemetry/series",
            params={
                "recording_id": recording_id,
                "metrics": "speed",
                "from_ms": 800,
                "to_ms": 200,
            },
        )
        assert flipped.status_code == 200
        assert (flipped.json()["from_ms"], flipped.json()["to_ms"]) == (200, 800)

        # 내 차·범위 밖 비교 차량은 비교 없음으로 안전하게 정규화한다.
        for compare in (0, 99, -1):
            response = client.get(
                "/api/replay/telemetry/series",
                params={
                    "recording_id": recording_id,
                    "metrics": "speed",
                    "compare_car": compare,
                },
            )
            assert response.status_code == 200, compare
            assert response.json()["compare_car"] is None
            assert response.json()["series"]["speed"]["compare_series"] is None

        # 음수 시각·뒤집힌 지도 구간은 클램핑·교환된다.
        negative = client.get(
            "/api/replay/telemetry/map_frames",
            params={"recording_id": recording_id, "t_ms": -500},
        )
        assert negative.status_code == 200, negative.text
        assert negative.json()["t_ms"] == 0
        assert negative.json()["frame"]["t_ms"] == 0

        swapped = client.get(
            "/api/replay/telemetry/map_frames",
            params={
                "recording_id": recording_id,
                "from_ms": 900,
                "to_ms": 100,
            },
        )
        assert swapped.status_code == 200, swapped.text
        assert (swapped.json()["from_ms"], swapped.json()["to_ms"]) == (100, 900)
        assert swapped.json()["frames"]

        # 인자를 하나도 주지 않으면 전체 세션 프레임을 준다.
        everything = client.get(
            "/api/replay/telemetry/map_frames",
            params={"recording_id": recording_id},
        )
        assert everything.status_code == 200
        assert len(everything.json()["frames"]) == len(
            store.get_ready(recording_id).map_frames
        )


def test_telemetry_compare_private_car_is_restricted(tmp_path: Path) -> None:
    app, db_path, store, _manager = _build_router_app(tmp_path)
    recording = build_recording(
        tmp_path / "recordings" / "race" / "private.f1rec", duration_ms=400
    )
    recording_id = _add_recording(
        db_path, local_path=recording, duration_ms=400
    )

    with TestClient(app) as client:
        client.post(
            "/api/replay/telemetry/open", json={"recording_id": recording_id}
        )
        _wait_ready(client, store, recording_id)

        response = client.get(
            "/api/replay/telemetry/series",
            params={
                "recording_id": recording_id,
                "metrics": "speed,gap_front_s",
                "compare_car": PRIVATE_CAR,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["compare_car"] == PRIVATE_CAR
        assert body["compare_your_telemetry"] == 0
        # CarTelemetry 원천은 비공개라 비교 시리즈를 억제한다.
        assert body["series"]["speed"]["compare_series"] is None
        assert body["series"]["speed"]["compare_restricted"] is True
        # LapData 원천(간격)은 정상 비교한다.
        assert body["series"]["gap_front_s"]["compare_series"]
        assert body["series"]["gap_front_s"]["compare_restricted"] is False


# ---------------------------------------------------------------------------
# 5. 엔진 OFF의 /telemetry 라우트와 BackupWorker 정리 보호 연동
# ---------------------------------------------------------------------------


def test_telemetry_page_and_backup_protection_when_engine_off(
    tmp_path: Path, monkeypatch
) -> None:
    # Task 5 전이라 `static/telemetry.html`이 아직 없다. 라우트가 실제로
    # `STATIC_DIR/telemetry.html`을 서빙하는지 보려고 임시 정적 폴더를 쓴다.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "telemetry.html").write_text(
        "<!doctype html><title>텔레메트리 분석</title>", encoding="utf-8"
    )
    (static_dir / "dashboard.html").write_text(
        "<!doctype html><title>대시보드</title>", encoding="utf-8"
    )
    monkeypatch.setattr(web_app, "STATIC_DIR", static_dir)

    config = AppConfig(recording=RecordingConfig(dir=str(tmp_path / "recordings")))
    engine = Engine(
        config,
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "f1re.db",
        state_path=tmp_path / "engine-state.json",
        persist_state=False,
    )
    assert engine.state == ENGINE_OFF
    recording = build_recording(
        tmp_path / "recordings" / "race" / "page.f1rec", duration_ms=400
    )
    recording_id = _add_recording(
        tmp_path / "f1re.db", local_path=recording, duration_ms=400
    )

    app = web_app.create_app(engine=engine)
    store = app.state.telemetry_store
    assert isinstance(store, TelemetryIndexStore)
    worker = app.state.backup_worker
    assert isinstance(worker, BackupWorker)
    # app.py가 텔레메트리 인덱스 보호 콜백을 백업 워커에 연결했다.
    assert worker._extra_protected_ids is not None

    with TestClient(app) as client:
        # 엔진이 필요한 API는 503이지만 화면·분석 API는 그대로 뜬다.
        assert client.get("/api/state").status_code == 503
        page = client.get("/telemetry")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "텔레메트리 분석" in page.text
        # 배포 뒤 옛 화면이 캐시에 남지 않게 재검증을 요구한다.
        assert page.headers["cache-control"] == "no-cache"

        opened = client.post(
            "/api/replay/telemetry/open", json={"recording_id": recording_id}
        )
        assert opened.status_code == 200, opened.text
        _wait_ready(client, store, recording_id)
        # 분석 화면이 열어 둔 녹화는 로컬 정리 보호 목록에 들어간다.
        assert worker._extra_protected_ids() == {recording_id}
        # 분석 API를 다 쓴 뒤에도 전역 다시 보기 재생은 꺼져 있다.
        assert client.get("/api/replay/state").json()["replay"]["active"] is False


# ---------------------------------------------------------------------------
# 6. `/telemetry` 3단 레이아웃 HTML·정적 자산(SCRUM-136, 계획 Task 5)
# ---------------------------------------------------------------------------


def test_telemetry_html_has_three_pane_layout_and_assets() -> None:
    """`/telemetry` HTML에 필수 DOM·스크립트·스타일 참조가 모두 있다.

    계획 Task 5의 화면 골격(상단 컨트롤 바, 좌측 차트·메트릭 트리, 우측
    지도)이 실제로 서빙되는지 확인한다. 브라우저 JS는 실행하지 않고 응답
    본문과 정적 자산 200만 본다.
    """
    client = TestClient(web_app.create_app(StateStore()))
    response = client.get("/telemetry")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text

    # 상단 컨트롤 바: 녹화/비교 선택과 독립 재생 컨트롤.
    for element_id in (
        "recording-select",
        "compare-select",
        "play-toggle",
        "playback-seek",
        "playback-time",
        "zoom-reset",
        "auto-follow",
        "toggle-radio-markers",
        "toggle-event-markers",
    ):
        assert f'id="{element_id}"' in html, element_id

    # 좌측 차트·메트릭 트리, 우측 지도.
    for element_id in (
        "telemetry-chart",
        "metric-tree",
        "telemetry-map",
        "preset-bar",
        "chart-tooltip",
        "private-badge",
        "chart-overlay",
        "index-progress",
    ):
        assert f'id="{element_id}"' in html, element_id

    # 지도 배지 줄은 기존 순위(SCRUM-139)·깃발 배지 자리를 그대로 둔다.
    assert 'class="map-position"' in html
    assert 'class="map-flag"' in html
    assert 'class="map-preview"' in html

    # 외부 CDN 없이 telemetry.js·map.js·telemetry.css를 직접 참조한다.
    assert "/static/telemetry.js" in html
    assert "/static/map.js" in html
    assert "/static/telemetry.css" in html
    assert "http://" not in html and "https://" not in html

    for asset in (
        "/static/telemetry.js",
        "/static/telemetry.css",
        "/static/map.js",
    ):
        asset_response = client.get(asset)
        assert asset_response.status_code == 200, asset

    # 화면 스크립트는 서버 카탈로그·프리셋·map 프레임 API만 쓴다.
    script = client.get("/static/telemetry.js").text
    for token in (
        "/api/replay/telemetry/open",
        "/api/replay/telemetry/summary",
        "/api/replay/telemetry/map_frames",
        "skipLiftCoast",
        "setCompareCar",
        "(비공개)",
        "localStorage",
    ):
        assert token in script, token


# ---------------------------------------------------------------------------
# 7. 실제 게임 녹화 픽스처 end-to-end 스모크(SCRUM-137, 계획 Task 6 Step 3)
# ---------------------------------------------------------------------------

#: 2026-11-15 스즈카 실전 레이스(recording id 6)의 짧은 연속 구간 픽스처.
REAL_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "real"
    / "recording-6-suzuka-race-snippet.f1rec"
)


def test_telemetry_real_recording_fixture_end_to_end(tmp_path: Path) -> None:
    """실제 F1 25 녹화 조각으로 open→summary→series→map_frames를 검증한다.

    합성 패킷이 아닌 게임 원본 패킷에서도 인덱스·다운샘플·10Hz 지도 프레임이
    오류 없이 만들어지는지 확인하는 스모크다(설계 5.1, 계획 Task 6 Step 3).
    """
    assert REAL_FIXTURE.is_file(), REAL_FIXTURE
    app, db_path, store, manager = _build_router_app(tmp_path)
    recording_id = _add_recording(
        db_path,
        local_path=REAL_FIXTURE,
        duration_ms=2_500,
        packet_count=100,
    )

    with TestClient(app) as client:
        opened = client.post(
            "/api/replay/telemetry/open", json={"recording_id": recording_id}
        )
        assert opened.status_code == 200, opened.text
        _wait_ready(client, store, recording_id)

        summary = client.get(
            "/api/replay/telemetry/summary",
            params={"recording_id": recording_id},
        )
        assert summary.status_code == 200, summary.text
        payload = summary.json()
        assert payload["status"] == "ready"
        assert len(payload["catalog"]) == 5
        assert len(payload["participants"]) == 22
        assert payload["track"]["outline"]
        assert payload["duration_ms"] > 0
        assert set(payload["markers"]) == {
            "radio",
            "events",
            "intervals",
            "laps",
        }

        duration = payload["duration_ms"]
        series = client.get(
            "/api/replay/telemetry/series",
            params={
                "recording_id": recording_id,
                "metrics": "speed,throttle,brake,gear,g_lat,gap_front_s",
                "from_ms": 0,
                "to_ms": duration,
                "max_points": 2_000,
            },
        )
        assert series.status_code == 200, series.text
        body = series.json()
        # 내 차 CarTelemetry·Motion·LapData 시계열이 실제 패킷에서 나온다.
        assert body["series"]["speed"]["series"]
        assert body["series"]["g_lat"]["series"]
        assert body["series"]["gear"]["step"] is True
        times = [point[0] for point in body["series"]["speed"]["series"]]
        assert times == sorted(times)
        assert "NaN" not in series.text

        single = client.get(
            "/api/replay/telemetry/map_frames",
            params={"recording_id": recording_id, "t_ms": duration // 2},
        )
        assert single.status_code == 200, single.text
        assert single.json()["frame"]["cars"]

        ranged = client.get(
            "/api/replay/telemetry/map_frames",
            params={
                "recording_id": recording_id,
                "from_ms": 0,
                "to_ms": duration,
            },
        )
        assert ranged.status_code == 200, ranged.text
        assert ranged.json()["frames"]

        # 분석 API를 모두 쓴 뒤에도 전역 ReplayManager 재생은 꺼져 있다.
        assert manager.active_recording_id is None
        assert client.get("/api/replay/state").json()["replay"]["active"] is False


def test_telemetry_script_has_task6_chart_interactions() -> None:
    """Task 6의 다중 축·점선·마커·줌/팬·호버/Δ 연동이 정적 자산에 있다.

    브라우저 JS를 실행하지 않는 테스트라, 서버가 서빙하는 스크립트에 계획
    Task 6의 핵심 동작(시리즈 재조회, 비교 점선, 비공개 안내, 미리보기,
    자동 따라가기, 포인터 상호작용)이 실제로 들어 있는지 문자열로 확인한다.
    """
    client = TestClient(web_app.create_app(StateStore()))
    response = client.get("/static/telemetry.js")
    assert response.status_code == 200
    script = response.text
    for token in (
        "/api/replay/telemetry/series",
        "/api/replay/telemetry/map_frames",
        "[5, 4]",
        "compare_restricted",
        "비공개 텔레메트리",
        "미리보기",
        "setPreviewLabel",
        "followCursor",
        "resetZoom",
        "pointerdown",
        "pointermove",
        "wheel",
        "dblclick",
        "shiftKey",
        "CLICK_DRAG_PX",
        "SERIES_DEBOUNCE_MS",
        "PREVIEW_DEBOUNCE_MS",
        "Δ",
    ):
        assert token in script, token

    css = client.get("/static/telemetry.css").text
    assert "cursor: crosshair" in css
    assert ".chart-wrap.panning" in css


async def test_backup_worker_prune_keeps_open_telemetry_recording(
    tmp_path: Path,
) -> None:
    """보호 ID로 넘긴 텔레메트리 녹화 파일은 로컬 정리에서 지워지지 않는다."""
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    config = RecordingConfig(
        dir=str(root), keep_local_count=1, min_packets=1
    )
    paths: list[Path] = []
    ids: list[int] = []
    for index in range(3):
        path = root / "race" / f"rec-{index}.f1rec.zst"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"zstd")
        recording_id = db.create_recording(
            conn,
            session_uid=9_000 + index,
            segment=1,
            started_at_ns=1_000 + index * 1_000,
            category="race",
            session_type=15,
            track_id=13,
        )
        db.finalize_recording(
            conn,
            recording_id,
            end_reason="session_end",
            ended_at_ns=2_000 + index * 1_000,
            packet_count=10,
            duration_ms=100,
            local_path=str(path),
            local_exists=True,
            file_size_bytes=4,
            backup_status="uploaded",
        )
        paths.append(path)
        ids.append(recording_id)

    worker = BackupWorker(
        db_path,
        config,
        transport=_FakeUploadTransport(),
        extra_protected_ids=lambda: {ids[0]},
    )
    try:
        await worker._prune_local()

        # keep_local_count=1 + 보호 1개 → 최신(ids[2])과 열어 둔 ids[0]만 남고
        # 가운데 ids[1]만 지워진다.
        assert paths[0].is_file()
        assert paths[2].is_file()
        assert not paths[1].exists()
        assert db.get_recording(conn, ids[0])["local_exists"] == 1
        assert db.get_recording(conn, ids[1])["local_exists"] == 0
        assert db.get_recording(conn, ids[2])["local_exists"] == 1
    finally:
        await worker.aclose()
        conn.close()
