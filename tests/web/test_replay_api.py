"""녹화 목록·다운로드·웹 리플레이 API 라우터(`replay_api.py`) 검증.

설계 4.5(온디맨드 복원·다운로드)·4.6(다시 보기)와 계획 작업 12를 확인한다.
공용 conftest를 만들지 않는 규칙에 따라 패킷 생성기·DB/파일 도우미·가짜
다운로더·가짜 백업 워커를 이 파일 안에 둔다.

실제 `rclone`·GCS·네트워크는 부르지 않는다. 복원은 `RecordingFetcher`에
주입한 가짜 다운로더가 파일만 만들어 주고, 리플레이는 실제 UDP 대신 녹화
파일(`.f1rec`)을 읽는다. 엔진은 앱에 붙이지 않으므로 엔진 OFF와 무관하게
동작하는지도 자연스럽게 확인된다(설계 4.5).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from f1re import db
from f1re.config import RecordingConfig, RecordingGcsConfig
from f1re.udp.formats import f2025
from f1re.udp.gcs_transfer import RcloneTransport, RecordingFetcher
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
from f1re.udp.web_replay import ReplayManager
from f1re.web.replay_api import create_replay_router

SESSION_UID = 0x0123_4567_89AB_CDEF
# 녹화 첫 패킷의 수신 시각. 실제 게임처럼 0이 아닌 절대 시각(ns)을 쓴다.
BASE_NS = 1_700_000_000_000_000_000

# 대시보드 정적 스크립트. 라우터만 붙인 앱에는 정적 파일이 없으므로 원본을 읽는다.
DASHBOARD_JS_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "f1re"
    / "web"
    / "static"
    / "dashboard.js"
)


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
    """테스트용 UDP 데이터그램 생성기."""

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
            weather=0, track_id=13, session_type=18, game_paused=0, total_laps=0
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
    ) -> bytes:
        body = _default_body(PacketId.CAR_TELEMETRY)
        entry = body["car_telemetry_data"][car]
        entry.update(
            speed=speed,
            gear=gear,
            throttle=throttle,
            brake=brake,
        )
        return self.raw(PacketId.CAR_TELEMETRY, body)

    def damage(self, *, car: int = 0, wear: float = 0.0) -> bytes:
        body = _default_body(PacketId.CAR_DAMAGE)
        body["car_damage_data"][car]["tyres_wear"] = [wear, wear, wear, wear]
        return self.raw(PacketId.CAR_DAMAGE, body)


# ---------------------------------------------------------------------------
# 도우미(DB·파일·가짜 전송)
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> tuple[Path, Callable[[], sqlite3.Connection]]:
    """파일 DB와 호출마다 새 연결을 여는 공장. `:memory:`는 연결마다 달라 안 된다."""
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
    gcs_object: str | None = None,
    backup_status: str = "uploaded",
    backup_error: str | None = None,
) -> int:
    """마감된 녹화 행을 만든다. `local_path` 파일이 있으면 로컬 보유 상태다.

    `backup_error`를 주면 백업 실패·건너뜀 사유까지 남긴다(예: rclone 미설치).
    """
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
        if gcs_object is not None:
            db.mark_recording_backup(
                conn,
                recording_id,
                backup_status="uploaded",
                gcs_object=gcs_object,
                backed_up_at_ns=started_at_ns + 2_000,
            )
        if backup_error is not None:
            db.mark_recording_backup(
                conn,
                recording_id,
                backup_status=backup_status,
                backup_error=backup_error,
            )
        return recording_id
    finally:
        conn.close()


def _write_file(path: Path, payload: bytes = b"zstd-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class _FakeDownloadTransport:
    """`RecordingFetcher`에 주입하는 가짜 다운로더. 실제 rclone을 부르지 않는다."""

    def __init__(self, payload: bytes = b"restored-zstd") -> None:
        self.calls: list[tuple[str, Path]] = []
        self._payload = payload

    def is_available(self) -> bool:
        return True

    async def download(self, remote_object: str, local_path: Path) -> None:
        self.calls.append((remote_object, Path(local_path)))
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._payload)


class _FakeWorker:
    """`BackupWorker.retry()`만 흉내 내는 가짜 워커."""

    def __init__(self, *, missing: set[int] | None = None) -> None:
        self.calls: list[int] = []
        self._missing = missing or set()

    async def retry(self, recording_id: int) -> dict[str, Any]:
        self.calls.append(int(recording_id))
        if recording_id in self._missing:
            return {
                "recording_id": int(recording_id),
                "backup_status": None,
                "gcs_object": None,
                "error": "녹화를 찾을 수 없습니다",
            }
        return {
            "recording_id": int(recording_id),
            "backup_status": "uploaded",
            "gcs_object": "race/retried.f1rec.zst",
            "error": None,
        }


def _build(
    tmp_path: Path,
    *,
    transport: Any = None,
    backup_worker: Any = None,
    manager: ReplayManager | None = None,
) -> tuple[FastAPI, Callable[[], sqlite3.Connection], RecordingFetcher, ReplayManager]:
    """라우터만 붙인 FastAPI 앱과 그 부품들을 만든다(엔진 없음)."""
    _db_path, conn_factory = _make_db(tmp_path)
    config = RecordingConfig(dir=str(tmp_path / "recordings"), keep_cache_count=3)
    fetcher = RecordingFetcher(
        conn_factory,
        config,
        transport=transport if transport is not None else _FakeDownloadTransport(),
    )
    replay_manager = manager if manager is not None else ReplayManager()
    app = FastAPI()
    app.include_router(
        create_replay_router(
            conn_factory=conn_factory,
            fetcher=fetcher,
            replay_manager=replay_manager,
            backup_worker=backup_worker,
        )
    )
    return app, conn_factory, fetcher, replay_manager


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _write_recording(path: Path, records: list[tuple[int, bytes]]) -> Path:
    with RecordingWriter(path) as writer:
        for recv_ns, data in records:
            writer.write(recv_ns, data)
    return path


def _race_recording(path: Path) -> Path:
    """0~400ms 길이의 경쟁 세션 녹화. 100ms마다 속도가 바뀐다."""
    builder = PacketBuilder()
    return _write_recording(
        path,
        [
            (BASE_NS, builder.session(session_type=15, total_laps=10)),
            (BASE_NS + 20_000_000, builder.participants(player_team_id=0)),
            (BASE_NS + 40_000_000, builder.lap(lap=1, distance=100.0)),
            (BASE_NS + 60_000_000, builder.telemetry(speed=100, gear=3)),
            # 타이어 마모 90% → 안전 경고가 메모리 무전 기록에 남는다.
            (BASE_NS + 80_000_000, builder.damage(wear=90.0)),
            (BASE_NS + 100_000_000, builder.telemetry(speed=120, gear=4)),
            (BASE_NS + 200_000_000, builder.telemetry(speed=140, gear=5)),
            (BASE_NS + 300_000_000, builder.telemetry(speed=160, gear=6)),
            (BASE_NS + 400_000_000, builder.telemetry(speed=180, gear=7)),
        ],
    )


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sessions", "reference_sets", "radio_messages")
    }


def _telemetry_speed(snapshot: dict[str, Any], car: int = 0) -> Any:
    body = (snapshot.get("packets") or {}).get("car_telemetry")
    if not isinstance(body, dict):
        return None
    return body["car_telemetry_data"][car].get("speed")


# ---------------------------------------------------------------------------
# 1. 녹화 목록: 부 번호·종료 사유·배지 이름·보관 위치
# ---------------------------------------------------------------------------


def test_recordings_list_includes_segment_and_end_reason(tmp_path: Path) -> None:
    app, _conn_factory, _fetcher, _manager = _build(tmp_path)
    db_path = tmp_path / "f1re.db"

    local_file = _write_file(tmp_path / "recordings" / "race" / "first.f1rec.zst")
    first = _add_recording(
        db_path,
        session_uid=111,
        segment=1,
        started_at_ns=1_000,
        end_reason="session_end",
        local_path=local_file,
    )
    second = _add_recording(
        db_path,
        session_uid=111,
        segment=2,
        started_at_ns=2_000,
        end_reason="inactivity_timeout",
        gcs_object="race/second.f1rec.zst",
    )
    third = _add_recording(
        db_path,
        session_uid=222,
        segment=1,
        started_at_ns=3_000,
        category="unknown",
        session_type=None,
        track_id=None,
        end_reason="engine_off",
        backup_status="failed",
    )

    with _client(app) as client:
        response = client.get("/api/recordings?limit=50")
        assert response.status_code == 200
        items = response.json()["recordings"]

        race_items = client.get("/api/recordings?category=race").json()["recordings"]
        limited = client.get("/api/recordings?limit=1").json()["recordings"]
        # 빈 범주는 "전체"로 본다(화면 필터 기본값).
        empty_category = client.get("/api/recordings?category=").json()["recordings"]

    # 최신순(started_at_ns DESC).
    assert [item["id"] for item in items] == [third, second, first]
    by_id = {item["id"]: item for item in items}

    # 1부·2부가 각각 다른 행으로 남는다(10분 만료 후 재개).
    assert by_id[first]["segment"] == 1
    assert by_id[second]["segment"] == 2
    assert by_id[first]["end_reason_label"] == "정상 종료"
    assert by_id[second]["end_reason_label"] == "10분 만료"
    assert by_id[third]["end_reason_label"] == "엔진 OFF"

    # 종류 배지 이름과 서킷 이름.
    assert by_id[first]["category_label"] == "레이스"
    assert by_id[third]["category_label"] == "미분류"
    assert by_id[first]["track_name"] == "스즈카 서킷"
    assert by_id[third]["track_name"] is None

    # 보관 위치·다운로드 가능 여부: 로컬 파일, GCS 백업, 둘 다 없음.
    assert by_id[first]["local_exists"] is True
    assert by_id[first]["downloadable"] is True
    assert by_id[second]["local_exists"] is False
    assert by_id[second]["backup_status"] == "uploaded"
    assert by_id[second]["downloadable"] is True
    assert by_id[third]["downloadable"] is False

    assert {item["id"] for item in race_items} == {first, second}
    assert [item["id"] for item in limited] == [third]
    assert len(empty_category) == 3


def test_recordings_list_includes_backup_error_reason(tmp_path: Path) -> None:
    """SCRUM-132: skipped·failed 녹화의 백업 사유를 목록 응답이 그대로 준다."""
    app, _conn_factory, _fetcher, _manager = _build(tmp_path)
    db_path = tmp_path / "f1re.db"
    skipped_file = _write_file(
        tmp_path / "recordings" / "race" / "skipped.f1rec.zst"
    )
    skipped = _add_recording(
        db_path,
        session_uid=601,
        started_at_ns=1_000,
        local_path=skipped_file,
        backup_status="skipped",
        backup_error="rclone을 사용할 수 없습니다",
    )
    failed = _add_recording(
        db_path,
        session_uid=602,
        started_at_ns=2_000,
        backup_status="failed",
        backup_error="rclone 업로드 실패: quota exceeded",
    )
    uploaded = _add_recording(
        db_path,
        session_uid=603,
        started_at_ns=3_000,
        gcs_object="race/uploaded.f1rec.zst",
    )

    with _client(app) as client:
        items = client.get("/api/recordings").json()["recordings"]

    by_id = {item["id"]: item for item in items}
    # 홈서버에서 본 rclone 미설치 사유가 API에서도 사라지지 않는다.
    assert by_id[skipped]["backup_status"] == "skipped"
    assert by_id[skipped]["backup_error"] == "rclone을 사용할 수 없습니다"
    # 건너뛴 녹화의 로컬 파일은 보존돼 다운로드·재생이 그대로 된다.
    assert by_id[skipped]["local_exists"] is True
    assert by_id[skipped]["downloadable"] is True
    # 업로드 실패 사유도 같은 필드로 온다.
    assert by_id[failed]["backup_status"] == "failed"
    assert by_id[failed]["backup_error"] == "rclone 업로드 실패: quota exceeded"
    # 업로드가 끝난 녹화는 사유가 없다(None).
    assert by_id[uploaded]["backup_status"] == "uploaded"
    assert by_id[uploaded]["backup_error"] is None
    # 기존 필드는 그대로 유지된다.
    assert by_id[skipped]["category_label"] == "레이스"
    assert by_id[skipped]["end_reason_label"] == "정상 종료"


def test_dashboard_shows_backup_error_reason_safely() -> None:
    """SCRUM-132: 대시보드가 skipped·failed 사유를 안전한 글자로 보여 준다."""
    script = DASHBOARD_JS_PATH.read_text(encoding="utf-8")

    match = re.search(
        r"function backupIssueText\(recording\) \{(.*?)\n\}", script, re.S
    )
    assert match, "백업 사유 문구를 만드는 함수가 없습니다"
    body = match.group(1)
    # skipped·failed만 사유를 보여 주고 uploaded 등은 무시한다.
    assert 'status !== "skipped" && status !== "failed"' in body
    assert "recording.backup_error" in body
    assert '"백업 안 함"' in body
    assert '"백업 실패"' in body
    # 사유는 HTML이 아니라 글자로만 다룬다(XSS 없음).
    assert "innerHTML" not in body

    # 목록 항목은 사유를 메타 줄과 title에 글자로 넣는다.
    assert "const backupIssue = backupIssueText(recording);" in script
    assert "if (backupIssue && recording.local_exists) parts.push(backupIssue);" in script
    assert "meta.textContent = parts.filter(Boolean).join(\" · \");" in script
    assert "if (backupIssue) meta.title = backupIssue;" in script
    # 대시보드는 innerHTML을 쓰지 않는다.
    assert "innerHTML" not in script


# ---------------------------------------------------------------------------
# 2. 다운로드: 로컬 우선, 없으면 GCS 복원, 동시 요청 병합
# ---------------------------------------------------------------------------


def test_download_serves_local_file_without_restoring(tmp_path: Path) -> None:
    transport = _FakeDownloadTransport()
    app, _conn_factory, _fetcher, _manager = _build(tmp_path, transport=transport)
    db_path = tmp_path / "f1re.db"
    local_file = _write_file(
        tmp_path / "recordings" / "race" / "local.f1rec.zst", b"local-zstd"
    )
    recording_id = _add_recording(
        db_path,
        session_uid=301,
        local_path=local_file,
        gcs_object="race/cloud.f1rec.zst",
    )

    with _client(app) as client:
        response = client.get(f"/api/recordings/{recording_id}/download")

    assert response.status_code == 200
    assert response.content == b"local-zstd"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "local.f1rec.zst" in disposition
    # 로컬 파일이 있으면 rclone 다운로드를 하지 않는다.
    assert transport.calls == []


def test_download_restores_from_gcs_when_local_deleted(tmp_path: Path) -> None:
    transport = _FakeDownloadTransport(b"restored-zstd")
    app, _conn_factory, fetcher, _manager = _build(tmp_path, transport=transport)
    db_path = tmp_path / "f1re.db"
    # 로컬 5개 정리로 파일은 지워지고 `local_exists = 0`만 남은 상태.
    recording_id = _add_recording(
        db_path,
        session_uid=302,
        local_path=tmp_path / "recordings" / "race" / "gone.f1rec.zst",
        gcs_object="race/gone.f1rec.zst",
    )

    with _client(app) as client:
        response = client.get(f"/api/recordings/{recording_id}/download")
        second = client.get(f"/api/recordings/{recording_id}/download")

    assert response.status_code == 200
    assert response.content == b"restored-zstd"
    assert "gone.f1rec.zst" in response.headers["content-disposition"]
    assert second.content == b"restored-zstd"

    # 첫 요청만 rclone 복원을 하고, 복원 캐시(`.cache/<id>.f1rec.zst`)를 재사용한다.
    assert len(transport.calls) == 1
    remote_object, partial_path = transport.calls[0]
    assert remote_object == "race/gone.f1rec.zst"
    assert partial_path.parent == fetcher.cache_path(recording_id).parent
    cache = fetcher.cache_path(recording_id)
    assert cache.is_file()
    assert cache.read_bytes() == b"restored-zstd"


def test_download_maps_missing_sources_and_restore_failures(tmp_path: Path) -> None:
    app, _conn_factory, _fetcher, _manager = _build(tmp_path)
    db_path = tmp_path / "f1re.db"
    empty = _add_recording(db_path, session_uid=303, backup_status="failed")

    with _client(app) as client:
        assert client.get("/api/recordings/99999/download").status_code == 404
        # 로컬 파일도 GCS 객체도 없으면 내려 줄 것이 없다.
        assert client.get(f"/api/recordings/{empty}/download").status_code == 404


def test_download_reports_rclone_unavailable_as_503(tmp_path: Path) -> None:
    """rclone·GCS 설정이 없으면 404가 아니라 503(복원 불가)으로 알린다."""
    transport = RcloneTransport(
        RecordingGcsConfig(enabled=True, bucket="bucket"),
        which=lambda name: None,
        runner=_failing_runner,
    )
    app, _conn_factory, _fetcher, _manager = _build(tmp_path, transport=transport)
    db_path = tmp_path / "f1re.db"
    recording_id = _add_recording(
        db_path, session_uid=304, gcs_object="race/x.f1rec.zst"
    )

    with _client(app) as client:
        response = client.get(f"/api/recordings/{recording_id}/download")

    assert response.status_code == 503
    assert "rclone" in response.json()["detail"]["message"]


def _failing_runner(args: Sequence[str]) -> None:
    raise AssertionError(f"rclone을 실행하면 안 됩니다: {args}")


# ---------------------------------------------------------------------------
# 3. 리플레이 시작·제어·시크·상태·WebSocket
# ---------------------------------------------------------------------------


def test_replay_start_control_seek_and_websocket(tmp_path: Path) -> None:
    app, _conn_factory, _fetcher, _manager = _build(tmp_path)
    db_path = tmp_path / "f1re.db"
    recording = _race_recording(tmp_path / "recordings" / "race" / "live.f1rec")
    # 파일 크기는 압축 파일과 다르지만 목록 표시에는 무관하다.
    recording_id = _add_recording(
        db_path,
        session_uid=SESSION_UID,
        started_at_ns=BASE_NS,
        duration_ms=400,
        local_path=recording,
    )

    conn = db.open_db(db_path)
    before = _row_counts(conn)

    with _client(app) as client:
        try:
            started = client.post(
                "/api/replay/start",
                json={"recording_id": recording_id, "speed": 1.0},
            )
            assert started.status_code == 200, started.text
            payload = started.json()
            # 실시간 `/api/state`와 같은 구조 + `replay` 메타(동결 API 계약).
            for key in (
                "snapshot",
                "car",
                "connection",
                "cars",
                "flag",
                "flags",
                "status",
            ):
                assert key in payload, key
            replay = payload["replay"]
            assert replay["active"] is True
            assert replay["recording_id"] == recording_id
            assert replay["session_uid"] == SESSION_UID
            assert replay["category"] == "race"
            assert replay["track_id"] == 13
            assert replay["duration_ms"] == 400
            assert replay["speed"] == 1.0
            assert replay["paused"] is False

            # 일시정지하면 위치가 더 이상 앞으로 가지 않는다.
            paused = client.post(
                "/api/replay/control", json={"action": "pause"}
            ).json()
            assert paused["replay"]["paused"] is True
            paused_position = paused["replay"]["position_ms"]
            assert (
                client.get("/api/replay/state").json()["replay"]["position_ms"]
                == paused_position
            )

            # 시크: 상태·파이프라인·경고 전이 기억을 새로 만들고 목표 직전까지
            # 재구성한다(계획 Review Focus 5). 300ms 직전 패킷(200ms, 속도 140).
            sought = client.post(
                "/api/replay/control",
                json={"action": "seek", "position_ms": 300},
            ).json()
            assert sought["replay"]["position_ms"] == 300
            assert sought["replay"]["paused"] is True
            assert _telemetry_speed(sought["snapshot"]) == 140

            # 배속 변경은 상태 메타에 반영된다.
            sped = client.post(
                "/api/replay/control", json={"action": "speed", "speed": 5.0}
            ).json()
            assert sped["replay"]["speed"] == 5.0

            state = client.get("/api/replay/state").json()
            assert state["replay"]["recording_id"] == recording_id
            assert state["replay"]["speed"] == 5.0

            # 지도·리포트·무전 기록은 기존 화면 부품이 읽는 모양 그대로다.
            track = client.get("/api/replay/track").json()
            assert track["track_id"] == 13
            assert track["corners"]
            report = client.get("/api/replay/report").json()
            assert report["available"] is True
            assert report["pipeline"]["track_id"] == 13
            radio = client.get("/api/replay/radio/log").json()
            assert radio["available"] is True
            kinds = {message["kind"] for message in radio["messages"]}
            # 타이어 마모 90% 안전 경고가 리플레이 무전 기록에 재현된다.
            assert "safety" in kinds
            assert all(message["played"] is True for message in radio["messages"])

            # `/ws/replay`는 붙자마자 현재 상태를 보낸다(10Hz 푸시의 첫 메시지).
            with client.websocket_connect("/ws/replay") as websocket:
                first = websocket.receive_json()
            assert first["replay"]["recording_id"] == recording_id
            assert first["replay"]["position_ms"] == 300
            assert first["replay"]["paused"] is True
            assert first["replay"]["speed"] == 5.0

            # 재개·정지.
            resumed = client.post(
                "/api/replay/control", json={"action": "resume"}
            ).json()
            assert resumed["replay"]["paused"] is False
            stopped = client.post(
                "/api/replay/control", json={"action": "stop"}
            ).json()
            assert stopped["replay"]["active"] is False
            assert client.get("/api/replay/state").json()["replay"]["active"] is False
        finally:
            client.post("/api/replay/control", json={"action": "stop"})

    # 리플레이 중 실서버 DB의 세 테이블은 전혀 쓰이지 않았다(설계 4.6).
    assert _row_counts(conn) == before
    # 녹화 행도 그대로 남아 있다(리플레이가 메타를 고치지 않는다).
    assert db.get_recording(conn, recording_id) is not None
    conn.close()


def test_replay_start_and_control_validate_inputs(tmp_path: Path) -> None:
    app, _conn_factory, _fetcher, _manager = _build(tmp_path)
    db_path = tmp_path / "f1re.db"
    recording = _race_recording(tmp_path / "recordings" / "race" / "valid.f1rec")
    recording_id = _add_recording(db_path, session_uid=401, local_path=recording)

    # 아직 녹화 중인 행은 재생할 수 없다.
    in_progress = _add_recording(
        db_path,
        session_uid=402,
        end_reason="recording",
        backup_status="recording",
    )

    with _client(app) as client:
        try:
            assert (
                client.post(
                    "/api/replay/start", json={"recording_id": 99999}
                ).status_code
                == 404
            )
            assert (
                client.post(
                    "/api/replay/start", json={"recording_id": in_progress}
                ).status_code
                == 409
            )
            assert (
                client.post(
                    "/api/replay/start",
                    json={"recording_id": recording_id, "speed": 0},
                ).status_code
                == 400
            )
            assert (
                client.post(
                    "/api/replay/control", json={"action": "jump"}
                ).status_code
                == 400
            )
            assert (
                client.post(
                    "/api/replay/control", json={"action": "seek"}
                ).status_code
                == 400
            )
            assert (
                client.post(
                    "/api/replay/control", json={"action": "speed"}
                ).status_code
                == 400
            )

            # 리플레이가 없으면 제어는 조용히 비활성 상태를 돌려준다.
            inactive = client.post(
                "/api/replay/control", json={"action": "pause"}
            ).json()
            assert inactive["replay"]["active"] is False
            assert client.get("/api/replay/report").json()["available"] is False
            assert client.get("/api/replay/radio/log").json()["messages"] == []
            assert client.get("/api/replay/track").json()["corners"] == []
        finally:
            client.post("/api/replay/control", json={"action": "stop"})


# ---------------------------------------------------------------------------
# 4. 수동 백업 재시도
# ---------------------------------------------------------------------------


def test_retry_backup_delegates_to_worker(tmp_path: Path) -> None:
    worker = _FakeWorker(missing={99999})
    app, _conn_factory, _fetcher, _manager = _build(
        tmp_path, backup_worker=worker
    )
    db_path = tmp_path / "f1re.db"
    recording_id = _add_recording(db_path, session_uid=501, backup_status="failed")

    with _client(app) as client:
        response = client.post(f"/api/recordings/{recording_id}/retry-backup")
        missing = client.post("/api/recordings/99999/retry-backup")

    assert response.status_code == 200
    assert response.json()["backup_status"] == "uploaded"
    assert worker.calls == [recording_id, 99999]
    assert missing.status_code == 404


def test_retry_backup_without_worker_returns_503(tmp_path: Path) -> None:
    app, _conn_factory, _fetcher, _manager = _build(tmp_path)
    db_path = tmp_path / "f1re.db"
    recording_id = _add_recording(db_path, session_uid=502)

    with _client(app) as client:
        response = client.post(f"/api/recordings/{recording_id}/retry-backup")

    assert response.status_code == 503
