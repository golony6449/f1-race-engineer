"""SQLite 스키마와 순차 마이그레이션.

표준 `sqlite3`만 쓴다. 데이터베이스 파일 기본 위치는 `data/f1re.db`이고,
`data/*.db`는 `.gitignore`에 있다.

스키마 버전은 `schema_version` 표에 적용한 마이그레이션 번호로 남긴다.
`MIGRATIONS`는 번호가 1부터 연속인 목록이며, `open_db`가 아직 적용되지 않은
것을 번호 순서대로 하나씩 적용한다. 뒤 작업(기준 랩 저장, 무전 기록 등)은
목록 끝에 새 마이그레이션을 덧붙인다.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

# 지금 코드가 기대하는 스키마 버전.
SCHEMA_VERSION = 1

DEFAULT_DB_RELPATH = Path("data") / "f1re.db"


def repo_root() -> Path:
    """레포 루트. 상대 경로 설정의 기준이다."""
    return Path(__file__).resolve().parents[2]


def default_db_path() -> Path:
    """기본 DB 파일 경로(`<레포>/data/f1re.db`)."""
    return repo_root() / DEFAULT_DB_RELPATH


def _migration_001_sessions(conn: sqlite3.Connection) -> None:
    """세션 한 번을 한 행으로 남긴다."""
    conn.execute(
        """
        CREATE TABLE sessions (
            session_uid INTEGER PRIMARY KEY,
            started_at_ns INTEGER NOT NULL,
            ended_at_ns INTEGER,
            track_id INTEGER,
            session_type INTEGER,
            recorded_file TEXT,
            packets_received INTEGER NOT NULL DEFAULT 0,
            unsupported_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )


# (버전, 적용 함수). 버전은 1부터 연속이어야 한다.
MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migration_001_sessions),
)


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at_ns INTEGER NOT NULL
        )
        """
    )


def schema_version(conn: sqlite3.Connection) -> int:
    """지금 DB에 적용된 스키마 버전. 빈 DB는 0이다."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        # 아직 schema_version 표가 없는 새 DB.
        return 0
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """적용 안 된 마이그레이션을 번호 순서대로 적용하고 버전을 돌려준다."""
    _ensure_version_table(conn)
    current = schema_version(conn)
    for version, migration in MIGRATIONS:
        if version <= current:
            continue
        with conn:
            migration(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at_ns) VALUES (?, ?)",
                (version, time.time_ns()),
            )
        current = version
    return current


def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    """DB를 열고(없으면 만들고) 마이그레이션을 적용한다.

    `path`가 없으면 `data/f1re.db`를 쓴다. `":memory:"`도 받는다.
    """
    db_path = default_db_path() if path is None else Path(path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def save_session(
    conn: sqlite3.Connection,
    session_uid: int,
    *,
    started_at_ns: int,
    track_id: int | None = None,
    session_type: int | None = None,
    recorded_file: str | None = None,
) -> None:
    """세션 행을 만들거나 없는 정보만 채운다(이미 있으면 덮어쓰지 않는다)."""
    with conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_uid, started_at_ns, track_id, session_type, recorded_file
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_uid) DO UPDATE SET
                track_id = COALESCE(excluded.track_id, sessions.track_id),
                session_type = COALESCE(excluded.session_type, sessions.session_type),
                recorded_file = COALESCE(excluded.recorded_file, sessions.recorded_file)
            """,
            (session_uid, started_at_ns, track_id, session_type, recorded_file),
        )


def update_session_progress(
    conn: sqlite3.Connection,
    session_uid: int,
    *,
    packets_received: int,
    unsupported_count: int,
) -> None:
    """세션 행의 수신 카운터를 갱신한다."""
    with conn:
        conn.execute(
            """
            UPDATE sessions
            SET packets_received = ?, unsupported_count = ?
            WHERE session_uid = ?
            """,
            (packets_received, unsupported_count, session_uid),
        )


def finish_session(
    conn: sqlite3.Connection, session_uid: int, *, ended_at_ns: int
) -> None:
    """세션 종료 시각을 기록한다."""
    with conn:
        conn.execute(
            "UPDATE sessions SET ended_at_ns = ? WHERE session_uid = ?",
            (ended_at_ns, session_uid),
        )
