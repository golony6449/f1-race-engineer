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
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from f1re.tracks.outline import TrackOutline

# 지금 코드가 기대하는 스키마 버전.
SCHEMA_VERSION = 6

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


def _migration_006_session_recordings(conn: sqlite3.Connection) -> None:
    """세션별 raw 텔레메트리 녹화 메타데이터를 남긴다(SCRUM-99).

    `sessions`와는 `session_uid`로만 느슨하게 연결한다. 세션 행이 아직 없거나
    세션 밖에서 만든 녹화도 기록이 막히면 안 되기 때문이다. 같은 세션 UID를
    10분 무수신 뒤 재개하면 `segment`를 올려 새 행을 만들므로,
    `UNIQUE (session_uid, segment)` 제약이 부 번호 충돌을 DB에서 막는다.
    `local_exists`는 SQLite에 불리언이 없어 `0`/`1` 정수로 둔다.
    """
    conn.execute(
        """
        CREATE TABLE session_recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_uid INTEGER NOT NULL,
            segment INTEGER NOT NULL DEFAULT 1,
            end_reason TEXT NOT NULL DEFAULT 'recording',
            category TEXT NOT NULL,
            session_type INTEGER,
            track_id INTEGER,
            started_at_ns INTEGER NOT NULL,
            ended_at_ns INTEGER,
            packet_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            local_path TEXT,
            local_exists INTEGER NOT NULL DEFAULT 0,
            file_size_bytes INTEGER,
            gcs_object TEXT,
            backup_status TEXT NOT NULL DEFAULT 'recording',
            backed_up_at_ns INTEGER,
            backup_error TEXT,
            UNIQUE (session_uid, segment)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_session_recordings_started
        ON session_recordings (started_at_ns)
        """
    )
    # 목록·5개 정리·백업 재시도 질의가 모두 두 열을 함께 거른다.
    conn.execute(
        """
        CREATE INDEX idx_session_recordings_backup
        ON session_recordings (backup_status, local_exists)
        """
    )


# (버전, 적용 함수). 버전은 1부터 연속이어야 한다.
MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migration_001_sessions),
    (2, _migration_002_radio_messages),
    (3, _migration_003_track_outlines),
    (4, _migration_004_reference_sets),
    (5, _migration_005_radio_dropped_reason),
    (6, _migration_006_session_recordings),
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


# --- 세션별 raw 텔레메트리 녹화 메타데이터(SCRUM-99) -------------------------
#
# 녹화 행은 수신 루프(T10)가 열고, 백업 워커(T8)가 마감·압축·업로드·정리를
# 하며 갱신한다. 여기 함수는 파일을 만지지 않는다. 실제 파일 삭제 전에
# `backup_status == "uploaded"`인지 호출자가 확인하도록 조회 함수가 그 조건을
# 강제한다(업로드 확인 전 로컬 파일 삭제 금지).


def create_recording(
    conn: sqlite3.Connection,
    *,
    session_uid: int,
    segment: int,
    started_at_ns: int,
    category: str = "unknown",
    session_type: int | None = None,
    track_id: int | None = None,
) -> int:
    """녹화 행을 열고 `id`를 돌려준다.

    `end_reason`·`backup_status`는 DB 기본값(`recording`)으로 두어 녹화 중임을
    나타낸다. 같은 `(session_uid, segment)`가 이미 있으면
    `sqlite3.IntegrityError`가 난다.
    """
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO session_recordings (
                session_uid, segment, started_at_ns, category, session_type,
                track_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_uid,
                segment,
                started_at_ns,
                category,
                session_type,
                track_id,
            ),
        )
    return int(cursor.lastrowid)


def next_recording_segment(conn: sqlite3.Connection, session_uid: int) -> int:
    """같은 세션 UID의 다음 부 번호. 기록이 없으면 `1`(1부)이다.

    10분 무수신으로 1부를 마감한 뒤 같은 세션이 재개되면 `2`를 돌려주므로
    `_s2` 파일과 새 행을 충돌 없이 열 수 있다.
    """
    row = conn.execute(
        "SELECT MAX(segment) FROM session_recordings WHERE session_uid = ?",
        (session_uid,),
    ).fetchone()
    if row is None or row[0] is None:
        return 1
    return int(row[0]) + 1


def update_recording_session_meta(
    conn: sqlite3.Connection,
    recording_id: int,
    *,
    category: str | None = None,
    session_type: int | None = None,
    track_id: int | None = None,
) -> None:
    """세션 정보가 늦게 도착했을 때 녹화 행을 채운다.

    `SESSION` 패킷은 2Hz라 첫 패킷 시점에는 아직 없을 수 있다. 넘기지 않은
    (`None`) 값은 기존 값을 지우지 않는다.
    """
    with conn:
        conn.execute(
            """
            UPDATE session_recordings
            SET category = COALESCE(?, category),
                session_type = COALESCE(?, session_type),
                track_id = COALESCE(?, track_id)
            WHERE id = ?
            """,
            (category, session_type, track_id, recording_id),
        )


def finalize_recording(
    conn: sqlite3.Connection,
    recording_id: int,
    *,
    end_reason: str,
    ended_at_ns: int,
    packet_count: int,
    duration_ms: int,
    local_path: str | None,
    local_exists: bool,
    file_size_bytes: int | None,
    backup_status: str,
) -> None:
    """활성 녹화를 마감하고 마감 사유·통계·로컬 파일·백업 상태를 남긴다.

    마감 값은 호출 시점의 것이 최종이므로 `None`도 그대로 덮어쓴다.
    `local_exists`는 SQLite에 맞춰 `0`/`1`로 저장한다.
    """
    with conn:
        conn.execute(
            """
            UPDATE session_recordings
            SET end_reason = ?,
                ended_at_ns = ?,
                packet_count = ?,
                duration_ms = ?,
                local_path = ?,
                local_exists = ?,
                file_size_bytes = ?,
                backup_status = ?
            WHERE id = ?
            """,
            (
                end_reason,
                ended_at_ns,
                packet_count,
                duration_ms,
                local_path,
                1 if local_exists else 0,
                file_size_bytes,
                backup_status,
                recording_id,
            ),
        )


def mark_recording_backup(
    conn: sqlite3.Connection,
    recording_id: int,
    *,
    backup_status: str,
    gcs_object: str | None = None,
    backed_up_at_ns: int | None = None,
    backup_error: str | None = None,
) -> None:
    """백업 상태와 GCS 위치·완료 시각·실패 사유를 갱신한다.

    넘기지 않은(`None`) 값은 기존 값을 지우지 않는다. 업로드 실패를
    `failed`로 남겨도 이 함수는 파일을 건드리지 않으므로 로컬 녹화는
    그대로 보존된다.
    """
    with conn:
        conn.execute(
            """
            UPDATE session_recordings
            SET backup_status = ?,
                gcs_object = COALESCE(?, gcs_object),
                backed_up_at_ns = COALESCE(?, backed_up_at_ns),
                backup_error = COALESCE(?, backup_error)
            WHERE id = ?
            """,
            (
                backup_status,
                gcs_object,
                backed_up_at_ns,
                backup_error,
                recording_id,
            ),
        )


def list_recordings(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """녹화 목록을 최신순(`started_at_ns DESC, id DESC`)으로 돌려준다.

    `limit`이 0 이하면 빈 목록이다. 시간이 같으면 나중에 만든 행이 앞선다.
    """
    query = "SELECT * FROM session_recordings"
    params: list[Any] = []
    if category is not None:
        query += " WHERE category = ?"
        params.append(category)
    query += " ORDER BY started_at_ns DESC, id DESC LIMIT ?"
    params.append(max(0, int(limit)))
    return [dict(row) for row in conn.execute(query, params)]


def get_recording(
    conn: sqlite3.Connection, recording_id: int
) -> dict[str, Any] | None:
    """녹화 한 행. 없으면 `None`."""
    row = conn.execute(
        "SELECT * FROM session_recordings WHERE id = ?", (recording_id,)
    ).fetchone()
    return None if row is None else dict(row)


def find_local_prune_targets(
    conn: sqlite3.Connection,
    *,
    keep_count: int,
    exclude_ids: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """로컬에서 지워도 되는 녹화 행(최신 `keep_count`개를 뺀 나머지).

    대상은 `local_exists = 1 AND backup_status = 'uploaded'`인 행뿐이다.
    업로드 성공이 확인되지 않은(`pending`·`failed`·`skipped`·`recording`)
    로컬 파일은 절대 후보가 되지 않는다. `exclude_ids`(예: 지금 웹 리플레이가
    재생 중인 `recording_id`)는 순위 계산 전에 빼므로, 제외된 녹화는
    `keep_count` 자리를 차지하지도 삭제 대상이 되지도 않는다.
    """
    excluded = {int(item) for item in exclude_ids}
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM session_recordings
            WHERE local_exists = 1 AND backup_status = 'uploaded'
            ORDER BY started_at_ns DESC, id DESC
            """
        )
    ]
    candidates = [row for row in rows if row["id"] not in excluded]
    return candidates[max(0, int(keep_count)):]


def mark_recording_local_deleted(
    conn: sqlite3.Connection, recording_id: int
) -> None:
    """로컬 파일을 지운 뒤 `local_exists = 0`으로 표시한다.

    `local_path`와 `gcs_object`는 이력·재복원 판단에 필요하므로 남긴다.
    """
    with conn:
        conn.execute(
            "UPDATE session_recordings SET local_exists = 0 WHERE id = ?",
            (recording_id,),
        )


def list_pending_backup_recordings(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """업로드가 남은 녹화(로컬 보유, `pending`/`failed`)를 오래된 순으로.

    재시도 워커가 이 목록을 순서대로 처리한다. 로컬 파일이 이미 없는 행은
    올릴 수 없으므로 제외한다.
    """
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM session_recordings
            WHERE local_exists = 1 AND backup_status IN ('pending', 'failed')
            ORDER BY started_at_ns ASC, id ASC
            """
        )
    ]


def delete_recording_row(conn: sqlite3.Connection, recording_id: int) -> None:
    """녹화 행을 지운다.

    초단기·0바이트 녹화를 폐기할 때 쓴다. 로컬 파일 삭제 여부는 호출자가
    정한다(이 함수는 DB 행만 지운다).
    """
    with conn:
        conn.execute(
            "DELETE FROM session_recordings WHERE id = ?", (recording_id,)
        )
