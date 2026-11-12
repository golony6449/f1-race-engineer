"""SQLite 스키마 버전·순차 마이그레이션·세션 표."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
            session_type=13,
            recorded_file="a.f1rec",
        )
        # 나중 호출이 아는 값만 채우고 기존 값을 지우지 않는다.
        db.save_session(conn, 42, started_at_ns=2_000)

        row = conn.execute(
            "SELECT * FROM sessions WHERE session_uid = ?", (42,)
        ).fetchone()
        assert row["started_at_ns"] == 1_000
        assert row["track_id"] == 13
        assert row["session_type"] == 13
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
