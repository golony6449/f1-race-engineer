"""SCRUM-108: `web/app.py` 최종 배선(설계 4.2·4.5·4.6, 계획 작업 13).

확인 항목:

- 엔진 OFF에서도 `/api/recordings*`·`/api/replay*`·`/ws/replay`가 503이 아니라
  정상 응답하고, 엔진이 필요한 다른 API는 그대로 503이다.
- 앱 `lifespan`이 `BackupWorker`를 기동·종료하고, `ReplayManager.active_recording_id`
  콜백과 `Engine`에 연결한다(로컬 5개 정리에서 재생 중 파일 보호).
- rclone 미설치·GCS 비활성(기본값)에서도 앱이 뜨고 `.active/` 미마감 파일을
  복구·압축하되 업로드는 건너뛴다(`skipped`). 실제 `rclone`·GCS·네트워크는
  부르지 않는다.

공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다. 리플레이는
실제 UDP 대신 녹화 파일(`.f1rec`)을 읽고, 녹화 파일 내용은 패킷 파싱 성공 여부와
무관하게 배선 검증에 충분한 임의 바이트다.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from f1re import db
from f1re.config import AppConfig, RecordingConfig
from f1re.engine import ENGINE_OFF, Engine
from f1re.udp.backup import BackupWorker
from f1re.udp.recording import RecordingWriter
from f1re.web.app import create_app

SESSION_UID = 0x0123_4567_89AB_CDEF
BASE_NS = 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _app_config(tmp_path: Path, **recording_overrides: Any) -> AppConfig:
    """녹화 폴더를 임시 경로로 둔 앱 설정. GCS는 기본값(꺼짐) 그대로다."""
    values: dict[str, Any] = {"dir": str(tmp_path / "recordings")}
    values.update(recording_overrides)
    return AppConfig(recording=RecordingConfig(**values))  # type: ignore[arg-type]


def _engine(tmp_path: Path, config: AppConfig) -> Engine:
    """엔진 OFF로 시작하는 소유 엔진(웹 lifespan이 워커를 붙이는 실서버 모양)."""
    return Engine(
        config,
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "f1re.db",
        state_path=tmp_path / "engine-state.json",
        persist_state=False,
    )


def _write_recording(path: Path, count: int = 20) -> Path:
    """`.f1rec`/`.active` 파일 하나. 프레임 형식만 맞추면 된다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with RecordingWriter(path) as writer:
        for index in range(count):
            writer.write(BASE_NS + index * 1_000_000, b"\x00\x01\x02\x03")
    return path


def _add_recording(
    db_path: Path,
    *,
    local_path: Path | None = None,
    session_uid: int = SESSION_UID,
    segment: int = 1,
    end_reason: str = "session_end",
) -> int:
    """마감된 녹화 행 하나(로컬 파일 보유 상태)."""
    conn = db.open_db(db_path)
    try:
        recording_id = db.create_recording(
            conn,
            session_uid=session_uid,
            segment=segment,
            started_at_ns=BASE_NS,
            category="race",
            session_type=15,
            track_id=13,
        )
        db.finalize_recording(
            conn,
            recording_id,
            end_reason=end_reason,
            ended_at_ns=BASE_NS + 1_000_000_000,
            packet_count=20,
            duration_ms=1_000,
            local_path=str(local_path) if local_path is not None else None,
            local_exists=local_path is not None and local_path.is_file(),
            file_size_bytes=(
                local_path.stat().st_size
                if local_path is not None and local_path.is_file()
                else None
            ),
            backup_status="uploaded",
        )
        return recording_id
    finally:
        conn.close()


def _recording_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = db.open_db(db_path)
    try:
        return db.list_recordings(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 엔진 OFF에서도 녹화·리플레이 API가 동작한다(설계 4.5)
# ---------------------------------------------------------------------------


def test_recordings_and_replay_work_when_engine_is_off(tmp_path: Path) -> None:
    config = _app_config(tmp_path)
    engine = _engine(tmp_path, config)
    assert engine.state == ENGINE_OFF
    recording_path = _write_recording(
        tmp_path / "recordings" / "race" / "saved.f1rec"
    )
    recording_id = _add_recording(
        tmp_path / "f1re.db", local_path=recording_path
    )

    with TestClient(create_app(engine=engine)) as client:
        # 엔진이 필요한 API는 그대로 503이다.
        assert client.get("/api/state").status_code == 503
        assert client.get("/api/radio/log").status_code == 503
        # 녹화·리플레이와 무관한 비슷한 이름의 경로는 통과시키지 않는다.
        assert client.get("/api/recordings-archive").status_code == 503

        # 목록: 최신순 + 화면 배지·보관 위치·다운로드 가능 여부.
        listing = client.get("/api/recordings")
        assert listing.status_code == 200, listing.text
        items = listing.json()["recordings"]
        assert [item["id"] for item in items] == [recording_id]
        item = items[0]
        assert item["segment"] == 1
        assert item["end_reason_label"] == "정상 종료"
        assert item["category_label"] == "레이스"
        assert item["local_exists"] is True
        assert item["downloadable"] is True

        # 다운로드: 로컬 파일을 그대로 내려 준다(복원 rclone 호출 없음).
        download = client.get(f"/api/recordings/{recording_id}/download")
        assert download.status_code == 200, download.text
        assert download.content == recording_path.read_bytes()
        assert download.headers["content-disposition"].startswith("attachment")

        # 리플레이 상태·부속 조회는 리플레이가 없어도 200이다.
        state = client.get("/api/replay/state")
        assert state.status_code == 200
        assert state.json()["replay"]["active"] is False
        for path in (
            "/api/replay/track",
            "/api/replay/report",
            "/api/replay/radio/log",
        ):
            assert client.get(path).status_code == 200, path

        # 시작·제어도 엔진 OFF에서 동작한다.
        started = client.post(
            "/api/replay/start",
            json={"recording_id": recording_id, "speed": 2.0},
        )
        assert started.status_code == 200, started.text
        meta = started.json()["replay"]
        assert meta["active"] is True
        assert meta["recording_id"] == recording_id
        assert meta["session_uid"] == SESSION_UID
        assert meta["speed"] == 2.0

        with client.websocket_connect("/ws/replay") as websocket:
            pushed = websocket.receive_json()
        assert pushed["replay"]["active"] is True
        assert pushed["replay"]["recording_id"] == recording_id

        stopped = client.post("/api/replay/control", json={"action": "stop"})
        assert stopped.status_code == 200
        assert stopped.json()["replay"]["active"] is False
        assert client.get("/api/replay/state").json()["replay"]["active"] is False


# ---------------------------------------------------------------------------
# 2. lifespan이 워커를 기동·종료하고 재생 중 파일 보호 콜백을 연결한다
# ---------------------------------------------------------------------------


def test_lifespan_wires_backup_worker_and_protects_active_replay(
    tmp_path: Path,
) -> None:
    config = _app_config(tmp_path)
    engine = _engine(tmp_path, config)
    recording_path = _write_recording(
        tmp_path / "recordings" / "race" / "playing.f1rec"
    )
    recording_id = _add_recording(
        tmp_path / "f1re.db", local_path=recording_path
    )
    app = create_app(engine=engine)

    with TestClient(app) as client:
        worker = app.state.backup_worker
        assert isinstance(worker, BackupWorker)
        # 엔진의 마감 큐가 이 워커로 흘러간다(설계 4.2 — 웹 서버 레벨 워커).
        assert engine.backup_worker is worker
        assert app.state.replay_manager is not None
        # 재생 중이 아니면 보호할 녹화가 없다.
        assert worker._active_replay_id() is None

        started = client.post(
            "/api/replay/start", json={"recording_id": recording_id}
        )
        assert started.status_code == 200, started.text
        # 로컬 5개 정리는 지금 재생 중인 파일을 지우지 않는다(Review Focus 4).
        assert worker._active_replay_id() == recording_id

        client.post("/api/replay/control", json={"action": "stop"})
        assert worker._active_replay_id() is None

    # lifespan 종료: 엔진에서 워커를 떼고 워커 태스크를 멈춘다.
    assert engine.backup_worker is None
    assert worker._task is None


# ---------------------------------------------------------------------------
# 3. rclone 미설치·GCS 비활성(기본값)에서도 기동·복구가 된다
# ---------------------------------------------------------------------------


def test_startup_recovers_active_recording_without_gcs_or_rclone(
    tmp_path: Path,
) -> None:
    config = _app_config(tmp_path, min_packets=10)
    active = _write_recording(
        tmp_path / "recordings" / ".active" / f"{SESSION_UID}_s1.f1rec",
        count=30,
    )
    engine = _engine(tmp_path, config)
    db_path = tmp_path / "f1re.db"
    app = create_app(engine=engine)

    with TestClient(app) as client:
        worker = app.state.backup_worker
        assert isinstance(worker, BackupWorker)
        # 기동 복구가 끝나고 워커가 압축·백업 처리를 마칠 때까지 기다린다.
        client.portal.call(lambda: asyncio.wait_for(worker.join(), 10))

        # 손상된 끝부분 없이 완전한 레코드만 압축되고 원본은 정리됐다.
        assert not active.exists()
        rows = _recording_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["session_uid"] == SESSION_UID
        assert row["segment"] == 1
        assert row["end_reason"] == "recovered"
        assert row["packet_count"] == 30
        # GCS가 꺼져 있으면(기본값) 업로드를 시도하지 않고 로컬만 보존한다.
        assert row["backup_status"] == "skipped"
        assert row["backup_error"] == "GCS 백업이 꺼져 있습니다"
        assert row["local_exists"] == 1
        compressed = Path(row["local_path"])
        assert compressed.is_file()
        assert compressed.name.endswith(".f1rec.zst")
        assert compressed.stat().st_size > 0

        # 화면 목록에도 복구된 녹화가 그대로 보인다.
        listed = client.get("/api/recordings").json()["recordings"]
        assert [item["id"] for item in listed] == [row["id"]]
        assert listed[0]["local_exists"] is True
        assert listed[0]["downloadable"] is True


# ---------------------------------------------------------------------------
# 4. DB 파일이 없는 테스트·단독 앱에서도 녹화 API는 503이 아니다
# ---------------------------------------------------------------------------


def test_recordings_api_answers_empty_without_a_file_database(
    tmp_path: Path,
) -> None:
    """`create_app(store)`류 고정 앱은 빈 목록으로 답한다(워커 없음)."""
    from f1re.state.store import StateStore

    app = create_app(StateStore())
    assert app.state.backup_worker is None
    with TestClient(app) as client:
        response = client.get("/api/recordings")
        assert response.status_code == 200
        assert response.json()["recordings"] == []
        # 재시도 API는 워커가 없으므로 503으로 사유를 알린다(목록과 별개).
        retry = client.post("/api/recordings/1/retry-backup")
        assert retry.status_code == 503


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sessions", "reference_sets", "radio_messages")
    }


def test_replay_wiring_does_not_touch_live_tables(tmp_path: Path) -> None:
    """리플레이 배선이 실서버 DB 테이블을 쓰지 않는다(설계 4.6)."""
    config = _app_config(tmp_path)
    engine = _engine(tmp_path, config)
    recording_path = _write_recording(
        tmp_path / "recordings" / "race" / "quiet.f1rec"
    )
    recording_id = _add_recording(
        tmp_path / "f1re.db", local_path=recording_path
    )
    conn = db.open_db(tmp_path / "f1re.db")
    before = _row_counts(conn)

    with TestClient(create_app(engine=engine)) as client:
        try:
            assert (
                client.post(
                    "/api/replay/start", json={"recording_id": recording_id}
                ).status_code
                == 200
            )
            client.post("/api/replay/control", json={"action": "seek", "position_ms": 10})
            client.post("/api/replay/control", json={"action": "stop"})
        finally:
            client.post("/api/replay/control", json={"action": "stop"})

    assert _row_counts(conn) == before
    conn.close()
