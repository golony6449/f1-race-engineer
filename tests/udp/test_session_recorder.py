"""세션별 자동 녹화기 `SessionRecordingManager` 테스트(SCRUM-105).

세션 UID 분리, `session_type` 지연 도착 반영, 데모 세션 무시, 무수신 타임아웃
마감과 2부 재개, `SESSION_END` 마감, 마감 큐 전달을 가짜 큐·가짜 시계로
검증한다. 실제 `rclone`·GCS·네트워크는 부르지 않는다(압축·업로드는 백업
워커의 몫이라 여기서는 마감 작업만 확인한다).
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from f1re.config import RecordingConfig
from f1re.db import get_recording, list_recordings, open_db
from f1re.state.events import Event, EventKind
from f1re.udp.parser import Packet
from f1re.udp.recording import FinalizeJob, iter_recording
from f1re.udp.session_recorder import ACTIVE_DIR_NAME, SessionRecordingManager
from f1re.udp.session_uid import demo_session_uid
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId


# ---------------------------------------------------------------------------
# 도우미


def _config(root: Path, **overrides: object) -> RecordingConfig:
    """테스트용 설정. 녹화 폴더만 임시 경로로 바꾼다."""
    return dataclasses.replace(RecordingConfig(), dir=str(root), **overrides)


def _header(session_uid: int, packet_id: PacketId) -> Header:
    return Header(
        packet_format=PACKET_FORMAT,
        game_year=25,
        game_major=1,
        game_minor=0,
        packet_version=1,
        packet_id=int(packet_id),
        session_uid=session_uid,
        session_time=0.0,
        frame_id=0,
        overall_frame_id=0,
        player_car_index=0,
        secondary_player_car_index=255,
    )


def _packet(
    session_uid: int,
    packet_id: PacketId = PacketId.MOTION,
    body: dict[str, object] | None = None,
) -> Packet:
    """파싱된 패킷 하나. 녹화기는 헤더 UID와 SESSION body만 본다."""
    return Packet(
        header=_header(session_uid, packet_id),
        body={} if body is None else body,
    )


def _session_packet(
    session_uid: int, session_type: int = 15, track_id: int = 7
) -> Packet:
    return _packet(
        session_uid,
        PacketId.SESSION,
        {"session_type": session_type, "track_id": track_id},
    )


def _active_files(root: Path) -> list[str]:
    folder = root / ACTIVE_DIR_NAME
    if not folder.is_dir():
        return []
    return sorted(path.name for path in folder.iterdir())


def _active_path(root: Path, name: str) -> Path:
    return root / ACTIVE_DIR_NAME / name


class _FakeQueue:
    """`BackupWorker.enqueue` 자리. 마감 작업을 순서대로 모은다."""

    def __init__(self) -> None:
        self.jobs: list[FinalizeJob] = []

    def enqueue(self, job: FinalizeJob) -> None:
        self.jobs.append(job)


class _Clock:
    """나노초 가짜 시계. 무수신 타임아웃 판정에만 쓴다."""

    def __init__(self, now: int = 0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = open_db(":memory:")
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 세션 분리와 범주 추적


def test_splits_files_by_session_uid_and_tracks_category(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "recordings"
    queue = _FakeQueue()
    manager = SessionRecordingManager(conn, _config(root), queue)

    manager.on_packet_bytes(1_000, b"motion-111", _packet(111))
    [first] = list_recordings(conn)
    assert first["session_uid"] == 111
    assert first["segment"] == 1
    # 첫 패킷 시점에는 SESSION 패킷이 아직 없다.
    assert first["category"] == "unknown"
    assert first["end_reason"] == "recording"
    assert first["backup_status"] == "recording"
    assert manager.active_path == _active_path(root, "111_s1.f1rec")

    # SESSION 패킷(2Hz)이 늦게 도착하면 범주·세션 종류·서킷을 채운다.
    manager.on_packet_bytes(2_000, b"session-111", _session_packet(111, 15, 7))
    first = get_recording(conn, first["id"])
    assert first is not None
    assert first["category"] == "race"
    assert first["session_type"] == 15
    assert first["track_id"] == 7

    # 다른 UID가 오면 이전 파일을 session_switch로 마감하고 새로 연다.
    manager.on_packet_bytes(3_000, b"motion-222", _packet(222))

    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert isinstance(job, FinalizeJob)
    assert job.recording_id == first["id"]
    assert job.session_uid == 111
    assert job.segment == 1
    assert job.end_reason == "session_switch"
    assert job.ended_at_ns == 2_000
    assert job.category == "race"
    assert job.session_type == 15
    assert job.track_id == 7
    assert job.active_path == _active_path(root, "111_s1.f1rec")

    # 마감된 파일은 원본 바이트를 그대로 보존한다.
    assert list(iter_recording(job.active_path)) == [
        (1_000, b"motion-111"),
        (2_000, b"session-111"),
    ]
    assert manager.active_path == _active_path(root, "222_s1.f1rec")
    # 파일 내용은 마감(close) 뒤에 확인한다. 쓰는 중인 파일은 버퍼에 남아
    # 있어 별도 핸들로 읽으면 아직 비어 보일 수 있다.
    manager.close_active("engine_off")
    assert list(iter_recording(_active_path(root, "222_s1.f1rec"))) == [
        (3_000, b"motion-222")
    ]

    rows = list_recordings(conn)
    assert [row["session_uid"] for row in rows] == [222, 111]
    assert rows[0]["segment"] == 1


@pytest.mark.parametrize(
    ("session_type", "category"),
    [
        (1, "practice"),
        (4, "practice"),
        (5, "qualifying"),
        (9, "qualifying"),
        # F1 25: 10~14는 스프린트 슛아웃이며 녹화 범주는 예선이다.
        (10, "qualifying"),
        (12, "qualifying"),
        (14, "qualifying"),
        # F1 25: 15~17이 레이스다(F1 23의 10~12가 아니다).
        (15, "race"),
        (17, "race"),
        # F1 25: 18이 타임 트라이얼이다(F1 23의 13이 아니다).
        (18, "time_trial"),
        (0, "unknown"),
        (19, "unknown"),
        (99, "unknown"),
    ],
)
def test_session_packet_updates_category(
    tmp_path: Path,
    conn: sqlite3.Connection,
    session_type: int,
    category: str,
) -> None:
    manager = SessionRecordingManager(
        conn, _config(tmp_path / "recordings"), _FakeQueue()
    )
    manager.on_packet_bytes(1_000, b"motion", _packet(42))
    manager.on_packet_bytes(
        2_000, b"session", _session_packet(42, session_type, 11)
    )

    [row] = list_recordings(conn)
    assert row["category"] == category
    assert row["session_type"] == session_type
    assert row["track_id"] == 11


def test_does_not_overwrite_unrecovered_active_file(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """복구 전 `.active/<uid>_s1.f1rec`이 남아 있으면 2부로 피해 간다."""
    root = tmp_path / "recordings"
    active = root / ACTIVE_DIR_NAME
    active.mkdir(parents=True)
    stale = active / "42_s1.f1rec"
    stale.write_bytes(b"stale-recording")

    manager = SessionRecordingManager(conn, _config(root), _FakeQueue())
    manager.on_packet_bytes(1_000, b"new", _packet(42))

    assert stale.read_bytes() == b"stale-recording"
    assert manager.active_path == _active_path(root, "42_s2.f1rec")
    [row] = list_recordings(conn)
    assert row["segment"] == 2


# ---------------------------------------------------------------------------
# 데모 세션 필터


def test_ignores_demo_session_uid(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "recordings"
    queue = _FakeQueue()
    manager = SessionRecordingManager(conn, _config(root), queue)
    uid = demo_session_uid(7)

    manager.on_packet_bytes(1_000, b"demo-motion", _packet(uid))
    manager.on_packet_bytes(2_000, b"demo-session", _session_packet(uid))

    # 파일·DB 행·마감 작업 어느 것도 만들지 않는다.
    assert _active_files(root) == []
    assert list_recordings(conn) == []
    assert queue.jobs == []
    assert manager.active_path is None

    # 데모 패킷이 섞여도 실제 세션 녹화는 정상으로 시작한다.
    manager.on_packet_bytes(3_000, b"real", _packet(99))
    assert manager.active_path == _active_path(root, "99_s1.f1rec")
    [row] = list_recordings(conn)
    assert row["session_uid"] == 99


def test_records_demo_session_when_exclude_demo_is_off(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "recordings"
    queue = _FakeQueue()
    manager = SessionRecordingManager(
        conn, _config(root, exclude_demo=False), queue
    )
    uid = demo_session_uid(7)

    manager.on_packet_bytes(1_000, b"demo", _packet(uid))

    assert manager.active_path == _active_path(root, f"{uid}_s1.f1rec")
    [row] = list_recordings(conn)
    assert row["session_uid"] == uid


# ---------------------------------------------------------------------------
# 무수신 타임아웃과 2부 재개


def test_inactivity_timeout_closes_and_resumes_as_segment_2(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "recordings"
    queue = _FakeQueue()
    clock = _Clock(1_000_000_000)
    manager = SessionRecordingManager(
        conn,
        _config(root, inactivity_timeout_s=600.0),
        queue,
        clock_ns=clock,
    )

    manager.on_packet_bytes(clock.now, b"first", _packet(555))
    clock.now += 599_000_000_000
    assert manager.check_inactivity() is False
    assert manager.active_session_uid == 555

    # 정확히 600초가 되면 마감한다(기본값 10분).
    clock.now += 1_000_000_000
    assert manager.check_inactivity() is True
    assert manager.active_path is None

    [job] = queue.jobs
    assert job.end_reason == "inactivity_timeout"
    assert job.session_uid == 555
    # 마감 시각은 마지막 패킷 시각이다(무수신 10분은 데이터가 없다).
    assert job.ended_at_ns == 1_000_000_000
    assert list(iter_recording(job.active_path)) == [
        (1_000_000_000, b"first")
    ]

    # 30분 뒤 같은 세션이 재개되면 같은 파일을 덮지 않고 2부로 이어 받는다.
    clock.now += 30 * 60 * 1_000_000_000
    manager.on_packet_bytes(clock.now, b"resumed", _packet(555))

    assert manager.active_path == _active_path(root, "555_s2.f1rec")
    manager.close_active("engine_off")
    assert list(iter_recording(_active_path(root, "555_s2.f1rec"))) == [
        (clock.now, b"resumed")
    ]
    rows = list_recordings(conn)
    assert [row["segment"] for row in rows] == [2, 1]
    assert rows[0]["id"] != job.recording_id
    assert rows[0]["started_at_ns"] == clock.now


def test_check_inactivity_without_active_recording_returns_false(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    manager = SessionRecordingManager(
        conn, _config(tmp_path / "recordings"), _FakeQueue()
    )

    assert manager.check_inactivity(10**18) is False


# ---------------------------------------------------------------------------
# SESSION_END 사건과 수동 마감


def test_session_end_event_closes_with_session_end_reason(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    queue = _FakeQueue()
    manager = SessionRecordingManager(
        conn, _config(tmp_path / "recordings"), queue
    )
    manager.on_packet_bytes(1_000, b"data", _packet(900))

    manager.on_events(
        [
            Event(
                kind=EventKind.SESSION_END,
                data={"session_uid": 900, "recv_ns": 2_000},
            )
        ]
    )

    [job] = queue.jobs
    assert job.end_reason == "session_end"
    assert job.recording_id is not None
    assert manager.active_recording_id is None

    # 같은 사건이 다시 와도 마감 작업이 중복으로 쌓이지 않는다.
    manager.on_events(
        [Event(kind=EventKind.SESSION_END, data={"session_uid": 900})]
    )
    assert len(queue.jobs) == 1


def test_session_end_event_for_other_session_uid_is_ignored(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """전환 직후 늦게 도착한 이전 세션의 종료 사건이 새 세션을 끊지 않는다."""
    queue = _FakeQueue()
    manager = SessionRecordingManager(
        conn, _config(tmp_path / "recordings"), queue
    )
    manager.on_packet_bytes(1_000, b"data", _packet(901))

    manager.on_events(
        [Event(kind=EventKind.SESSION_END, data={"session_uid": 900})]
    )

    assert queue.jobs == []
    assert manager.active_session_uid == 901


def test_close_active_engine_off_is_idempotent_and_validates_reason(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    queue = _FakeQueue()
    manager = SessionRecordingManager(
        conn, _config(tmp_path / "recordings"), queue
    )

    # 녹화 중이 아니면 아무 일도 일어나지 않는다.
    manager.close_active("engine_off")
    assert queue.jobs == []
    with pytest.raises(ValueError, match="종료 사유"):
        manager.close_active("not-a-reason")

    manager.on_packet_bytes(1_000, b"data", _packet(77))
    manager.close_active("engine_off")
    # 엔진 종료는 두 번 불릴 수 있다(HTTP stop과 서버 종료 경로).
    manager.close_active("engine_off")

    [job] = queue.jobs
    assert job.end_reason == "engine_off"
    assert manager.active_path is None


# ---------------------------------------------------------------------------
# 원본 보존과 비활성 설정


def test_unparsed_datagrams_append_only_to_active_recording(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "recordings"
    manager = SessionRecordingManager(conn, _config(root), _FakeQueue())

    # 파싱 실패 바이트만으로는 세션을 열지 않는다(쓰레기 트래픽 방어).
    manager.on_packet_bytes(1_000, b"noise", None)
    assert _active_files(root) == []
    assert list_recordings(conn) == []

    manager.on_packet_bytes(2_000, b"parsed", _packet(12))
    manager.on_packet_bytes(3_000, b"raw-tail", None)

    [row] = list_recordings(conn)
    assert row["session_uid"] == 12
    assert manager.active_session_uid == 12
    # 녹화 중에는 파싱 실패 데이터그램도 원본 그대로 남긴다.
    manager.close_active("engine_off")
    assert list(iter_recording(_active_path(root, "12_s1.f1rec"))) == [
        (2_000, b"parsed"),
        (3_000, b"raw-tail"),
    ]


def test_disabled_recording_does_nothing(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "recordings"
    queue = _FakeQueue()
    manager = SessionRecordingManager(conn, _config(root, enabled=False), queue)

    manager.on_packet_bytes(1_000, b"data", _packet(42))
    manager.on_events([Event(kind=EventKind.SESSION_END, data={})])
    manager.close_active("engine_off")

    assert _active_files(root) == []
    assert list_recordings(conn) == []
    assert queue.jobs == []
    assert manager.check_inactivity(10**18) is False


# ---------------------------------------------------------------------------
# uint64 세션 UID(SCRUM-140)


@pytest.mark.parametrize(
    "uid",
    [0xF000_0000_0000_0001, 0xFFFF_FFFF_FFFF_FFFF],
    ids=["high", "max"],
)
def test_high_uint64_session_uid_records_and_resumes_segment(
    tmp_path: Path, conn: sqlite3.Connection, uid: int
) -> None:
    """2**63 이상 UID도 파일명·마감 작업은 uint64, DB는 int64로 일관되게 쓴다."""
    root = tmp_path / "recordings"
    queue = _FakeQueue()
    clock = _Clock(1_000_000_000)
    manager = SessionRecordingManager(
        conn,
        _config(root, inactivity_timeout_s=600.0),
        queue,
        clock_ns=clock,
    )

    manager.on_packet_bytes(clock.now, b"first", _packet(uid))
    # 파일 이름은 원래의 양수 uint64 그대로다.
    assert manager.active_path == _active_path(root, f"{uid}_s1.f1rec")
    [row] = list_recordings(conn)
    assert row["session_uid"] == uid
    raw = conn.execute("SELECT session_uid FROM session_recordings").fetchone()[0]
    assert raw == uid - 2**64

    # 무수신 타임아웃으로 마감한 뒤 같은 UID가 재개되면 2부로 이어 받는다.
    clock.now += 600 * 1_000_000_000
    assert manager.check_inactivity() is True
    [job] = queue.jobs
    assert job.session_uid == uid
    assert job.active_path == _active_path(root, f"{uid}_s1.f1rec")

    clock.now += 1_000_000_000
    manager.on_packet_bytes(clock.now, b"resumed", _packet(uid))
    assert manager.active_path == _active_path(root, f"{uid}_s2.f1rec")
    assert [
        (row["session_uid"], row["segment"]) for row in list_recordings(conn)
    ] == [(uid, 2), (uid, 1)]

    manager.close_active("engine_off")
    assert [job.session_uid for job in queue.jobs] == [uid, uid]
    assert list(iter_recording(_active_path(root, f"{uid}_s2.f1rec"))) == [
        (clock.now, b"resumed")
    ]
