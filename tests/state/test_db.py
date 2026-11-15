"""SQLite 스키마 버전·순차 마이그레이션·세션 표·녹화 메타데이터."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from f1re import db


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_default_db_path_is_data_f1re_db() -> None:
    path = db.default_db_path()
    assert path.is_absolute()
    assert path.parent.name == "data"
    assert path.name == "f1re.db"


def test_schema_version_of_brand_new_database_is_zero(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "empty.db")
    try:
        assert db.schema_version(conn) == 0
    finally:
        conn.close()


def test_open_db_creates_schema_and_version(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        assert _table_names(conn) >= {"schema_version", "sessions"}
        assert db.schema_version(conn) == db.SCHEMA_VERSION

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        assert {
            "session_uid",
            "started_at_ns",
            "ended_at_ns",
            "track_id",
            "session_type",
            "recorded_file",
            "packets_received",
            "unsupported_count",
        } <= columns
    finally:
        conn.close()


def test_reopen_does_not_reapply_migrations(tmp_path: Path) -> None:
    path = tmp_path / "f1re.db"
    conn = db.open_db(path)
    versions = [
        row["version"] for row in conn.execute("SELECT version FROM schema_version")
    ]
    conn.close()

    conn = db.open_db(path)
    try:
        assert [
            row["version"] for row in conn.execute("SELECT version FROM schema_version")
        ] == versions
        assert db.schema_version(conn) == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_migrate_from_empty_database_applies_in_order(tmp_path: Path) -> None:
    path = tmp_path / "f1re.db"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at_ns INTEGER NOT NULL
        )
        """
    )
    raw.commit()
    assert db.schema_version(raw) == 0

    assert db.migrate(raw) == db.SCHEMA_VERSION
    applied = [
        row[0]
        for row in raw.execute("SELECT version FROM schema_version ORDER BY version")
    ]
    assert applied == [version for version, _ in db.MIGRATIONS]
    assert applied == list(range(1, db.SCHEMA_VERSION + 1))
    assert "sessions" in {
        row[0]
        for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    raw.close()


def test_session_rows_upsert_progress_and_finish(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        db.save_session(
            conn,
            42,
            started_at_ns=1_000,
            track_id=13,
            session_type=18,
            recorded_file="a.f1rec",
        )
        # 나중 호출이 아는 값만 채우고 기존 값을 지우지 않는다.
        db.save_session(conn, 42, started_at_ns=2_000)

        row = conn.execute(
            "SELECT * FROM sessions WHERE session_uid = ?", (42,)
        ).fetchone()
        assert row["started_at_ns"] == 1_000
        assert row["track_id"] == 13
        assert row["session_type"] == 18
        assert row["recorded_file"] == "a.f1rec"

        db.update_session_progress(
            conn, 42, packets_received=99, unsupported_count=3
        )
        db.finish_session(conn, 42, ended_at_ns=5_000)
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_uid = ?", (42,)
        ).fetchone()
        assert row["packets_received"] == 99
        assert row["unsupported_count"] == 3
        assert row["ended_at_ns"] == 5_000
    finally:
        conn.close()


# --- SCRUM-99: session_recordings 마이그레이션 006과 CRUD --------------------

_SESSION_RECORDING_COLUMNS = [
    "id",
    "session_uid",
    "segment",
    "end_reason",
    "category",
    "session_type",
    "track_id",
    "started_at_ns",
    "ended_at_ns",
    "packet_count",
    "duration_ms",
    "local_path",
    "local_exists",
    "file_size_bytes",
    "gcs_object",
    "backup_status",
    "backed_up_at_ns",
    "backup_error",
]


def _recording_row(
    conn: sqlite3.Connection,
    *,
    session_uid: int,
    started_at_ns: int,
    segment: int = 1,
    category: str = "race",
    backup_status: str = "uploaded",
    local_exists: bool = True,
) -> int:
    """마감된 녹화 행 하나를 만들고 `id`를 돌려준다(테스트 도우미)."""
    recording_id = db.create_recording(
        conn,
        session_uid=session_uid,
        segment=segment,
        started_at_ns=started_at_ns,
        category=category,
    )
    db.finalize_recording(
        conn,
        recording_id,
        end_reason="session_end",
        ended_at_ns=started_at_ns + 1_000,
        packet_count=500,
        duration_ms=1_000,
        local_path=(
            f"data/recordings/{category}/{session_uid}_s{segment}.f1rec.zst"
            if local_exists
            else None
        ),
        local_exists=local_exists,
        file_size_bytes=2_048 if local_exists else None,
        backup_status=backup_status,
    )
    if backup_status == "uploaded":
        db.mark_recording_backup(
            conn,
            recording_id,
            backup_status="uploaded",
            gcs_object=f"{category}/{session_uid}_s{segment}.f1rec.zst",
            backed_up_at_ns=started_at_ns + 2_000,
        )
    return recording_id


def test_migration_006_session_recordings_schema(tmp_path: Path) -> None:
    """006이 계획 4.4의 컬럼·기본값·유니크 제약·인덱스를 만든다."""
    assert db.SCHEMA_VERSION == 7
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        assert db.schema_version(conn) == 7
        assert "session_recordings" in _table_names(conn)

        info = [
            dict(row)
            for row in conn.execute("PRAGMA table_info(session_recordings)")
        ]
        assert [row["name"] for row in info] == _SESSION_RECORDING_COLUMNS
        columns = {row["name"]: row for row in info}

        assert columns["id"]["type"] == "INTEGER"
        assert columns["id"]["pk"] == 1
        ddl = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'session_recordings'
            """
        ).fetchone()["sql"]
        assert "AUTOINCREMENT" in ddl

        assert columns["session_uid"]["notnull"] == 1
        assert columns["segment"]["notnull"] == 1
        assert columns["segment"]["dflt_value"] == "1"
        assert columns["end_reason"]["notnull"] == 1
        assert columns["end_reason"]["dflt_value"] == "'recording'"
        assert columns["category"]["notnull"] == 1
        assert columns["started_at_ns"]["notnull"] == 1
        assert columns["packet_count"]["notnull"] == 1
        assert columns["packet_count"]["dflt_value"] == "0"
        assert columns["duration_ms"]["notnull"] == 1
        assert columns["duration_ms"]["dflt_value"] == "0"
        assert columns["local_exists"]["notnull"] == 1
        assert columns["local_exists"]["dflt_value"] == "0"
        assert columns["backup_status"]["notnull"] == 1
        assert columns["backup_status"]["dflt_value"] == "'recording'"
        for nullable in (
            "session_type",
            "track_id",
            "ended_at_ns",
            "local_path",
            "file_size_bytes",
            "gcs_object",
            "backed_up_at_ns",
            "backup_error",
        ):
            assert columns[nullable]["notnull"] == 0

        indexes = {
            row["name"]: row
            for row in conn.execute("PRAGMA index_list(session_recordings)")
        }
        assert {
            "idx_session_recordings_started",
            "idx_session_recordings_backup",
        } <= set(indexes)
        unique_columns = [
            [row["name"] for row in conn.execute(
                f'PRAGMA index_info("{name}")'
            )]
            for name, index in indexes.items()
            if index["unique"]
        ]
        assert ["session_uid", "segment"] in unique_columns

        # 유니크 제약은 실제 삽입에서도 충돌을 막는다.
        db.create_recording(conn, session_uid=7, segment=1, started_at_ns=10)
        with pytest.raises(sqlite3.IntegrityError):
            db.create_recording(conn, session_uid=7, segment=1, started_at_ns=20)
        assert len(db.list_recordings(conn)) == 1
    finally:
        conn.close()


def test_migration_006_upgrades_existing_v5_database(tmp_path: Path) -> None:
    """001~005가 적용된 기존 DB 파일이 데이터 그대로 006으로 올라간다."""
    path = tmp_path / "f1re.db"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at_ns INTEGER NOT NULL
        )
        """
    )
    for version, migration in db.MIGRATIONS[:5]:
        migration(raw)
        raw.execute(
            "INSERT INTO schema_version (version, applied_at_ns) VALUES (?, ?)",
            (version, version),
        )
    raw.execute(
        "INSERT INTO sessions (session_uid, started_at_ns) VALUES (?, ?)",
        (77, 1_000),
    )
    raw.execute(
        """
        INSERT INTO radio_messages (
            session_uid, recorded_at_ns, kind, priority, text, dropped_reason
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (77, 2_000, "coaching", 1, "기존 무전", "expired"),
    )
    raw.commit()
    assert db.schema_version(raw) == 5
    raw.close()

    conn = db.open_db(path)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION == 7
        assert [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_version ORDER BY version"
            )
        ] == [1, 2, 3, 4, 5, 6, 7]
        assert "session_recordings" in _table_names(conn)
        # 기존 행이 그대로 남는다.
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        radio = conn.execute("SELECT * FROM radio_messages").fetchone()
        assert radio["text"] == "기존 무전"
        assert radio["dropped_reason"] == "expired"
        # 다시 열어도 006·007이 중복 적용되지 않는다.
        db.migrate(conn)
        assert db.schema_version(conn) == 7
    finally:
        conn.close()


def test_migration_007_reclassifies_recordings_with_f1_25_session_types(
    tmp_path: Path,
) -> None:
    """F1 23 기준 범주를 F1 25 세션 종류(15=레이스, 18=TT)로 다시 계산한다."""
    path = tmp_path / "f1re.db"
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    raw.execute(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at_ns INTEGER NOT NULL
        )
        """
    )
    for version, migration in db.MIGRATIONS[:6]:
        migration(raw)
        raw.execute(
            "INSERT INTO schema_version (version, applied_at_ns) VALUES (?, ?)",
            (version, version),
        )
    rows = [
        # (uid, 기존 범주, session_type). 기존 범주는 F1 23 가정으로 분류된 값이다.
        (1, "unknown", 15),   # F1 25 레이스 → race
        (2, "unknown", 18),   # F1 25 타임 트라이얼 → time_trial
        (3, "race", 10),      # F1 25 스프린트 슛아웃 → qualifying
        (4, "practice", 5),   # 예선 → qualifying
        (5, "unknown", None), # 세션 종류 없음 → 그대로
        (6, "race", 19),      # 모르는 값 → unknown
        (7, "unknown", 16),   # 레이스 2 → race
    ]
    raw.executemany(
        """
        INSERT INTO session_recordings (
            session_uid, category, session_type, started_at_ns,
            local_path, gcs_object
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                uid,
                category,
                session_type,
                1_000 + uid,
                f"data/recordings/{category}/{uid}_s1.f1rec.zst",
                f"{category}/{uid}_s1.f1rec.zst",
            )
            for uid, category, session_type in rows
        ],
    )
    raw.commit()
    assert db.schema_version(raw) == 6
    raw.close()

    conn = db.open_db(path)
    try:
        assert db.schema_version(conn) == 7
        by_uid = {
            row["session_uid"]: row
            for row in conn.execute("SELECT * FROM session_recordings")
        }
        expected = {
            1: "race",
            2: "time_trial",
            3: "qualifying",
            4: "qualifying",
            5: "unknown",
            6: "unknown",
            7: "race",
        }
        assert {
            uid: row["category"] for uid, row in by_uid.items()
        } == expected
        # 파일 위치는 그대로 둔다(경로를 옮기지 않는다).
        assert by_uid[1]["local_path"] == (
            "data/recordings/unknown/1_s1.f1rec.zst"
        )
        assert by_uid[1]["gcs_object"] == "unknown/1_s1.f1rec.zst"
        # 다시 열어도 007이 중복 적용되지 않는다(멱등).
        db.migrate(conn)
        assert db.schema_version(conn) == 7
        assert by_uid[3]["category"] == "qualifying"
    finally:
        conn.close()


def test_recording_segments_and_prune_targets(tmp_path: Path) -> None:
    """세그먼트 1·2 공존과 로컬 5개 정리 대상 선정(업로드 확인분만)."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        first = db.create_recording(
            conn, session_uid=9, segment=1, started_at_ns=1_000
        )
        second = db.create_recording(
            conn, session_uid=9, segment=2, started_at_ns=2_000
        )
        segments = [
            (row["id"], row["segment"])
            for row in conn.execute(
                "SELECT id, segment FROM session_recordings ORDER BY segment"
            )
        ]
        assert segments == [(first, 1), (second, 2)]
        with pytest.raises(sqlite3.IntegrityError):
            db.create_recording(
                conn, session_uid=9, segment=2, started_at_ns=3_000
            )

        uploaded_ids = [
            _recording_row(
                conn, session_uid=100 + index, started_at_ns=10_000 + index
            )
            for index in range(7)
        ]
        failed_id = _recording_row(
            conn, session_uid=200, started_at_ns=20_000,
            backup_status="failed",
        )
        pending_id = _recording_row(
            conn, session_uid=201, started_at_ns=20_001,
            backup_status="pending",
        )
        skipped_id = _recording_row(
            conn, session_uid=202, started_at_ns=20_002,
            backup_status="skipped",
        )
        recording_id = db.create_recording(
            conn, session_uid=203, segment=1, started_at_ns=20_003
        )
        # GCS에만 있고 로컬에는 없는 행은 정리 대상이 아니다.
        remote_only_id = _recording_row(
            conn, session_uid=204, started_at_ns=20_004, local_exists=False
        )

        # 최신 5개를 빼면 가장 오래된 2개만 남는다(최신순 정렬).
        targets = db.find_local_prune_targets(conn, keep_count=5)
        assert [row["id"] for row in targets] == [
            uploaded_ids[1],
            uploaded_ids[0],
        ]
        assert all(row["local_exists"] == 1 for row in targets)
        assert all(row["backup_status"] == "uploaded" for row in targets)

        # 재생 중인 녹화는 순위 계산 전에 빠져 삭제되지 않는다.
        targets = db.find_local_prune_targets(
            conn, keep_count=5, exclude_ids=[uploaded_ids[0]]
        )
        assert [row["id"] for row in targets] == [uploaded_ids[1]]
        assert db.find_local_prune_targets(
            conn, keep_count=5, exclude_ids=uploaded_ids
        ) == []

        # keep_count=0이면 업로드 확인된 로컬 녹화만 전부 대상이 된다.
        all_targets = db.find_local_prune_targets(conn, keep_count=0)
        assert [row["id"] for row in all_targets] == list(
            reversed(uploaded_ids)
        )
        assert not {
            failed_id,
            pending_id,
            skipped_id,
            recording_id,
            remote_only_id,
        } & {row["id"] for row in all_targets}
    finally:
        conn.close()


def test_create_recording_defaults_and_next_segment(tmp_path: Path) -> None:
    """녹화 행 기본값(`recording`/`unknown`)과 재개 부 번호 계산."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        assert db.next_recording_segment(conn, 11) == 1
        recording_id = db.create_recording(
            conn, session_uid=11, segment=1, started_at_ns=500
        )
        row = db.get_recording(conn, recording_id)
        assert row is not None
        assert row["session_uid"] == 11
        assert row["segment"] == 1
        assert row["end_reason"] == "recording"
        assert row["category"] == "unknown"
        assert row["session_type"] is None
        assert row["track_id"] is None
        assert row["started_at_ns"] == 500
        assert row["ended_at_ns"] is None
        assert row["packet_count"] == 0
        assert row["duration_ms"] == 0
        assert row["local_path"] is None
        assert row["local_exists"] == 0
        assert row["file_size_bytes"] is None
        assert row["gcs_object"] is None
        assert row["backup_status"] == "recording"
        assert row["backed_up_at_ns"] is None
        assert row["backup_error"] is None

        assert db.next_recording_segment(conn, 11) == 2
        db.create_recording(conn, session_uid=11, segment=2, started_at_ns=600)
        assert db.next_recording_segment(conn, 11) == 3
        assert db.next_recording_segment(conn, 12) == 1
        assert db.get_recording(conn, 9_999) is None
    finally:
        conn.close()


def test_update_recording_session_meta_fills_late_session_info(
    tmp_path: Path,
) -> None:
    """SESSION 패킷이 늦게 와도 넘긴 값만 채우고 나머지는 유지한다."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        recording_id = db.create_recording(
            conn, session_uid=21, segment=1, started_at_ns=1_000
        )
        db.update_recording_session_meta(
            conn, recording_id, category="practice", track_id=7
        )
        row = db.get_recording(conn, recording_id)
        assert row["category"] == "practice"
        assert row["track_id"] == 7
        assert row["session_type"] is None

        db.update_recording_session_meta(conn, recording_id, session_type=3)
        row = db.get_recording(conn, recording_id)
        assert row["category"] == "practice"
        assert row["track_id"] == 7
        assert row["session_type"] == 3

        # 없는 행을 갱신해도 예외 없이 아무 일도 일어나지 않는다.
        db.update_recording_session_meta(conn, 9_999, category="race")
    finally:
        conn.close()


def test_finalize_recording_and_backup_transitions(tmp_path: Path) -> None:
    """마감 값 저장, 실패해도 로컬 보존, 업로드 성공, 로컬 삭제 표시."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        recording_id = db.create_recording(
            conn,
            session_uid=31,
            segment=1,
            started_at_ns=1_000,
            category="qualifying",
            session_type=6,
            track_id=11,
        )
        local_path = "data/recordings/qualifying/31_s1.f1rec.zst"
        db.finalize_recording(
            conn,
            recording_id,
            end_reason="inactivity_timeout",
            ended_at_ns=2_000,
            packet_count=321,
            duration_ms=990,
            local_path=local_path,
            local_exists=True,
            file_size_bytes=4_096,
            backup_status="pending",
        )
        row = db.get_recording(conn, recording_id)
        assert row["end_reason"] == "inactivity_timeout"
        assert row["ended_at_ns"] == 2_000
        assert row["packet_count"] == 321
        assert row["duration_ms"] == 990
        assert row["local_path"] == local_path
        assert row["local_exists"] == 1
        assert row["file_size_bytes"] == 4_096
        assert row["backup_status"] == "pending"

        # rclone 실패: 상태·사유만 남기고 로컬 보존 표시는 그대로다.
        db.mark_recording_backup(
            conn,
            recording_id,
            backup_status="failed",
            backup_error="rclone: not found",
        )
        row = db.get_recording(conn, recording_id)
        assert row["backup_status"] == "failed"
        assert row["backup_error"] == "rclone: not found"
        assert row["local_exists"] == 1
        assert row["gcs_object"] is None

        # 재시도 성공: GCS 위치·완료 시각이 채워지고 마지막 실패 사유는 남는다.
        db.mark_recording_backup(
            conn,
            recording_id,
            backup_status="uploaded",
            gcs_object="qualifying/31_s1.f1rec.zst",
            backed_up_at_ns=3_000,
        )
        row = db.get_recording(conn, recording_id)
        assert row["backup_status"] == "uploaded"
        assert row["gcs_object"] == "qualifying/31_s1.f1rec.zst"
        assert row["backed_up_at_ns"] == 3_000
        assert row["backup_error"] == "rclone: not found"

        # 로컬 5개 정리 후에는 보유 여부만 내리고 경로·GCS 위치는 남긴다.
        db.mark_recording_local_deleted(conn, recording_id)
        row = db.get_recording(conn, recording_id)
        assert row["local_exists"] == 0
        assert row["local_path"] == local_path
        assert row["gcs_object"] == "qualifying/31_s1.f1rec.zst"
        assert row["backup_status"] == "uploaded"
    finally:
        conn.close()


def test_list_recordings_orders_filters_and_limits(tmp_path: Path) -> None:
    """최신순 정렬, 같은 시각은 id 역순, 범주 필터·limit."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        oldest = db.create_recording(
            conn, session_uid=41, segment=1, started_at_ns=1_000,
            category="practice",
        )
        first_at_3000 = db.create_recording(
            conn, session_uid=42, segment=1, started_at_ns=3_000,
            category="race",
        )
        middle = db.create_recording(
            conn, session_uid=43, segment=1, started_at_ns=2_000,
            category="practice",
        )
        second_at_3000 = db.create_recording(
            conn, session_uid=44, segment=1, started_at_ns=3_000,
            category="race",
        )

        assert [row["id"] for row in db.list_recordings(conn)] == [
            second_at_3000,
            first_at_3000,
            middle,
            oldest,
        ]
        assert [
            row["id"] for row in db.list_recordings(conn, limit=2)
        ] == [second_at_3000, first_at_3000]
        assert [
            row["id"]
            for row in db.list_recordings(conn, category="practice")
        ] == [middle, oldest]
        assert [
            row["id"]
            for row in db.list_recordings(conn, category="race", limit=1)
        ] == [second_at_3000]
        assert db.list_recordings(conn, limit=0) == []
    finally:
        conn.close()


def test_pending_backups_retry_list_and_delete_row(tmp_path: Path) -> None:
    """백업 재시도 목록(pending/failed, 로컬 보유)과 행 삭제."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        pending_id = _recording_row(
            conn, session_uid=51, started_at_ns=3_000, backup_status="pending"
        )
        failed_id = _recording_row(
            conn, session_uid=52, started_at_ns=1_000, backup_status="failed"
        )
        _recording_row(
            conn, session_uid=53, started_at_ns=2_000, backup_status="uploaded"
        )
        _recording_row(
            conn, session_uid=54, started_at_ns=4_000, backup_status="skipped"
        )
        # 로컬 파일이 없으면 업로드할 수 없으므로 재시도 목록에서 빠진다.
        _recording_row(
            conn,
            session_uid=55,
            started_at_ns=5_000,
            backup_status="failed",
            local_exists=False,
        )

        rows = db.list_pending_backup_recordings(conn)
        assert [row["id"] for row in rows] == [failed_id, pending_id]
        assert {row["backup_status"] for row in rows} == {"pending", "failed"}
        assert all(row["local_exists"] == 1 for row in rows)

        db.delete_recording_row(conn, failed_id)
        assert db.get_recording(conn, failed_id) is None
        assert [
            row["id"] for row in db.list_pending_backup_recordings(conn)
        ] == [pending_id]
    finally:
        conn.close()

