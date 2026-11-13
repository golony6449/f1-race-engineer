"""SQLite 스키마와 순차 마이그레이션.

표준 `sqlite3`만 쓴다. 데이터베이스 파일 기본 위치는 `data/f1re.db`이고,
`data/` 폴더 전체(DB와 `-wal`·`-shm`)가 `.gitignore`에 있다.

스키마 버전은 `schema_version` 표에 적용한 마이그레이션 번호로 남긴다.
`MIGRATIONS`는 번호가 1부터 연속인 목록이며, `open_db`가 아직 적용되지 않은
것을 번호 순서대로 하나씩 적용한다. 뒤 작업(기준 랩 저장, 무전 기록 등)은
목록 끝에 새 마이그레이션을 덧붙인다.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from f1re.tracks.outline import TrackOutline

# 지금 코드가 기대하는 스키마 버전.
SCHEMA_VERSION = 5

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


def _migration_002_radio_messages(conn: sqlite3.Connection) -> None:
    """무전 한 줄을 한 행으로 남긴다(작업 11).

    `session_uid`는 세션 표를 참조하지 않는다. 세션 행이 아직 없거나 세션 밖에서
    만든 무전도 기록이 막히면 안 되기 때문이다.
    """
    conn.execute(
        """
        CREATE TABLE radio_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_uid INTEGER,
            recorded_at_ns INTEGER NOT NULL,
            lap INTEGER,
            corner INTEGER,
            kind TEXT NOT NULL,
            priority INTEGER NOT NULL,
            text TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '{}',
            played INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_radio_messages_session_time
        ON radio_messages (session_uid, recorded_at_ns)
        """
    )


def _migration_003_track_outlines(conn: sqlite3.Connection) -> None:
    """서킷 윤곽(랩 거리 순 점 배열)을 서킷 ID로 저장한다(작업 10).

    데이터 파일이 없는 서킷의 윤곽은 첫 유효 랩에서 배워 여기에 남긴다.
    점은 `[[dist_m, x, z], ...]` JSON이다.
    """
    conn.execute(
        """
        CREATE TABLE track_outlines (
            track_id INTEGER PRIMARY KEY,
            points TEXT NOT NULL,
            length_m REAL NOT NULL,
            learned_at_ns INTEGER NOT NULL
        )
        """
    )


def _migration_004_reference_sets(conn: sqlite3.Connection) -> None:
    """조건(서킷·모드·날씨·차)별 기준 랩과 코너 기록을 저장한다(작업 19).

    `reference`에는 기준 랩(내 최고 랩·세션 최속차 최고 랩)을, `focus`에는
    집중 코너와 코너별 손실을 JSON으로 넣는다. 키가 다르면 다른 행이므로
    맑음·비, 타임 트라이얼·레이스 주말, 팀별 기준이 서로 섞이지 않는다.
    """
    conn.execute(
        """
        CREATE TABLE reference_sets (
            track_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            weather TEXT NOT NULL,
            car_key TEXT NOT NULL,
            reference TEXT NOT NULL,
            focus TEXT NOT NULL,
            updated_at_ns INTEGER NOT NULL,
            PRIMARY KEY (track_id, mode, weather, car_key)
        )
        """
    )


def _migration_005_radio_dropped_reason(conn: sqlite3.Connection) -> None:
    """재생되지 않은 무전의 이유(만료 등)를 남긴다(SCRUM-81).

    큐가 유효 시간이 지나 버린 무전도 `played=0`과 이유로 기록해, 세션 뒤
    화면에서 왜 말하지 못했는지 볼 수 있게 한다.
    """
    conn.execute("ALTER TABLE radio_messages ADD COLUMN dropped_reason TEXT")


# (버전, 적용 함수). 버전은 1부터 연속이어야 한다.
MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migration_001_sessions),
    (2, _migration_002_radio_messages),
    (3, _migration_003_track_outlines),
    (4, _migration_004_reference_sets),
    (5, _migration_005_radio_dropped_reason),
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
    웹 API(작업 10)가 수신 스레드와 다른 스레드에서 윤곽을 읽으므로
    `check_same_thread=False`로 연다. 쓰기는 수신 루프 한 곳에서만 한다.
    """
    db_path = default_db_path() if path is None else Path(path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
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


def save_track_outline(
    conn: sqlite3.Connection,
    track_id: int,
    outline: TrackOutline,
    *,
    learned_at_ns: int | None = None,
) -> None:
    """서킷 윤곽을 서킷 ID로 저장한다(이미 있으면 덮어쓴다).

    점은 소수 3자리로 줄여 저장한다. 지도 픽셀 단위에서 1mm 차이는 보이지
    않으면서 DB와 브라우저 전송 크기가 크게 줄어든다.
    """
    points = [
        [round(dist, 3), round(x, 3), round(z, 3)]
        for dist, x, z in outline.points
    ]
    when = time.time_ns() if learned_at_ns is None else learned_at_ns
    with conn:
        conn.execute(
            """
            INSERT INTO track_outlines (track_id, points, length_m, learned_at_ns)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                points = excluded.points,
                length_m = excluded.length_m,
                learned_at_ns = excluded.learned_at_ns
            """,
            (track_id, json.dumps(points, separators=(",", ":")),
             outline.lap_length_m, when),
        )


def load_track_outline(
    conn: sqlite3.Connection, track_id: int
) -> TrackOutline | None:
    """저장된 서킷 윤곽. 없거나 내용이 잘못됐으면 `None`."""
    row = conn.execute(
        "SELECT points FROM track_outlines WHERE track_id = ?", (track_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row["points"])
        points = [(float(dist), float(x), float(z)) for dist, x, z in raw]
        return TrackOutline(points)
    except (TypeError, ValueError):
        # DB가 손상돼도 서버가 멈추지 않게 윤곽 없음으로 본다.
        return None
