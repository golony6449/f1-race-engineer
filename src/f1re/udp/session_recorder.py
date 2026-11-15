"""세션별 자동 원본 녹화기(SCRUM-105, 설계 4.1·4.2).

UDP 수신 루프에서 데이터그램마다 :meth:`SessionRecordingManager.on_packet_bytes`를
불러 `.active/<session_uid>_s<segment>.f1rec`에 원본을 그대로 순차 기록한다.
세션이 끝나면(정상 종료·세션 전환·엔진 종료·무수신 타임아웃) 파일을 닫아
:class:`~f1re.udp.recording.FinalizeJob`으로 백업 큐에 넘긴다.

이 모듈이 하지 않는 일:

- 압축·GCS 업로드·로컬 5개 정리·`min_packets` 필터는 `BackupWorker`(작업 8)가
  맡는다. 45분 레이스 압축이 60Hz 수신 루프나 `Engine.stop()`을 막으면 안 되기
  때문이다. 이 모듈은 마감된 파일을 큐에 넣고 즉시 돌아온다.
- 최종 `<category>/<시각>_t<서킷>_<uid>_s<segment>.f1rec.zst` 경로 확정도
  백업 워커가 `FinalizeJob`의 값을 써서 한다.

`PacketId.SESSION`(2Hz)은 첫 패킷보다 늦게 도착할 수 있다. 그래서 처음에는
`unknown` 범주로 파일을 열고, SESSION 패킷이 올 때마다 세션 종류·서킷을
`session_recordings` 행에 갱신한다. 데모 세션(`is_demo_session_uid`)은
`exclude_demo`가 켜져 있으면 패킷 자체를 무시해 실제 세션 녹화를 방해하지
않는다.

수신 스레드와 `Engine`의 1초 루프가 같은 객체를 부르므로 공개 메서드는
재진입 잠금으로 감싼다. DB 쓰기는 이 클래스가 만든 행(시작·메타 갱신)뿐이며,
마감 통계·백업 상태는 백업 워커가 채운다.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from f1re.config import RecordingConfig, resolve_asset_path
from f1re.db import (
    create_recording,
    next_recording_segment,
    update_recording_session_meta,
)
from f1re.state.events import Event, EventKind
from f1re.udp.parser import Packet
from f1re.udp.recording import RECORD_SUFFIX, FinalizeJob, RecordingWriter
from f1re.udp.session_category import END_REASONS, session_category
from f1re.udp.session_uid import is_demo_session_uid
from f1re.udp.spec import PacketId

logger = logging.getLogger(__name__)

# 녹화 중인 파일을 두는 하위 디렉터리(설계 4.1). 마감되면 백업 워커가
# `<category>/`로 옮기고 이 폴더에서는 지운다.
ACTIVE_DIR_NAME = ".active"


class FinalizeQueue(Protocol):
    """마감된 녹화를 받는 백업 큐(`BackupWorker.enqueue`)의 최소 인터페이스."""

    def enqueue(self, job: FinalizeJob) -> None: ...


@dataclass
class _ActiveRecording:
    """지금 쓰고 있는 `.active/*.f1rec` 하나의 상태."""

    recording_id: int
    session_uid: int
    segment: int
    path: Path
    writer: RecordingWriter
    started_at_ns: int
    last_packet_ns: int
    category: str
    session_type: int | None
    track_id: int | None


class SessionRecordingManager:
    """세션 UID별로 원본 UDP를 나눠 녹화하고 마감 큐에 넘긴다.

    ``conn``은 `session_recordings` 행을 만들고 갱신하는 데만 쓴다(엔진의
    연결을 그대로 받는다). ``backup_worker``가 `None`이면 마감된 파일은
    `.active/`에 남겨 두고 아무 데도 넘기지 않는다. 다음 기동 때 백업 워커의
    복구 경로가 주워 갈 수 있게 하기 위해서다.

    ``clock_ns``는 무수신 타임아웃 판정에만 쓴다. 테스트가 시간을 대신하기
    위한 것이고, 녹화 레코드의 수신 시각은 호출자가 넘기는 ``recv_ns``다.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: RecordingConfig,
        backup_worker: FinalizeQueue | None,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._conn = conn
        self._config = config
        self._backup_worker = backup_worker
        self._clock_ns = clock_ns
        self._root = resolve_asset_path(config.dir)
        self._active_dir = self._root / ACTIVE_DIR_NAME
        # `inactivity_timeout_s`는 초 단위 실수다. 나노초 정수로 바꿔 비교한다.
        self._inactivity_timeout_ns = int(config.inactivity_timeout_s * 1_000_000_000)
        self._lock = RLock()
        self._active: _ActiveRecording | None = None

    # ------------------------------------------------------------------
    # 조회(테스트·엔진 진단용)

    @property
    def active_recording_id(self) -> int | None:
        """지금 녹화 중인 `session_recordings.id`. 없으면 `None`."""
        with self._lock:
            return None if self._active is None else self._active.recording_id

    @property
    def active_session_uid(self) -> int | None:
        """지금 녹화 중인 세션 UID. 없으면 `None`."""
        with self._lock:
            return None if self._active is None else self._active.session_uid

    @property
    def active_path(self) -> Path | None:
        """지금 쓰고 있는 `.active/*.f1rec` 경로. 없으면 `None`."""
        with self._lock:
            return None if self._active is None else self._active.path

    # ------------------------------------------------------------------
    # 수신 경로

    def on_packet_bytes(
        self, recv_ns: int, data: bytes, packet: Packet | None
    ) -> None:
        """데이터그램 하나를 녹화한다. 60Hz 수신 루프가 부르는 경로다.

        ``packet``은 같은 데이터그램을 파싱한 결과다. 파싱에 실패했으면
        `None`을 넘긴다. `None`이어도 녹화 중인 세션이 있으면 원본은 그대로
        기록한다(수신 루프가 어떤 바이트를 받았든 남기는 것이 목적이다).
        다만 UID를 알 수 없으므로 새 세션을 열지는 않는다.

        `sessionUID`가 바뀌면 이전 파일을 `session_switch`로 마감하고 새
        세션의 1부 파일을 연다. 데모 세션은 `exclude_demo`가 켜져 있으면
        무시한다(실제 세션 녹화를 끊지 않도록 활동 시각도 갱신하지 않는다).
        """
        if not self._config.enabled:
            return
        with self._lock:
            packet_uid = self._packet_session_uid(packet)
            if (
                packet_uid is not None
                and self._config.exclude_demo
                and is_demo_session_uid(packet_uid)
            ):
                return

            active = self._active
            if (
                packet_uid is not None
                and active is not None
                and packet_uid != active.session_uid
            ):
                self._close_active_locked("session_switch")
            if packet_uid is not None and self._active is None:
                self._open_locked(packet_uid, recv_ns, packet)
            active = self._active
            if active is None:
                # 세션이 없을 때 파싱 실패 바이트만으로는 열지 않는다.
                return

            # 파일 쓰기가 먼저다. DB 메타 갱신이 실패해도 원본은 남긴다.
            try:
                active.writer.write(recv_ns, data)
            finally:
                # 쓰기 실패라도 데이터그램은 도착했으므로 무수신 판정은 갱신한다.
                active.last_packet_ns = recv_ns

            if packet is not None and packet.header.packet_id == PacketId.SESSION:
                self._update_session_meta_locked(active, packet)

    def on_events(self, events: Iterable[Event]) -> None:
        """상태 저장소가 만든 사건 중 세션 종료를 녹화 마감으로 반영한다.

        `SESSION_END`(게임의 SEND 이벤트)만 처리한다. 사건에 실린
        `session_uid`가 지금 녹화 중인 세션과 다르면(전환 직후 늦게 도착한
        사건) 무시해 새 세션을 잘못 끊지 않는다.
        """
        if not self._config.enabled:
            return
        with self._lock:
            for event in events:
                if event.kind is not EventKind.SESSION_END:
                    continue
                active = self._active
                if active is None:
                    continue
                event_uid = event.data.get("session_uid")
                if isinstance(event_uid, int) and int(event_uid) != active.session_uid:
                    continue
                self._close_active_locked("session_end")

    # ------------------------------------------------------------------
    # 마감

    def check_inactivity(self, now_ns: int | None = None) -> bool:
        """무수신 타임아웃을 확인하고 필요하면 마감한다. 마감했으면 `True`.

        `inactivity_timeout_s`(기본 600초) 동안 데이터그램이 없으면
        `inactivity_timeout`으로 닫는다. 같은 세션이 다시 시작되면
        :func:`f1re.db.next_recording_segment`가 2부(`_s2`)를 돌려주므로
        기존 파일을 덮지 않는다.
        """
        if not self._config.enabled:
            return False
        with self._lock:
            active = self._active
            if active is None:
                return False
            now = self._clock_ns() if now_ns is None else now_ns
            if now - active.last_packet_ns < self._inactivity_timeout_ns:
                return False
            self._close_active_locked("inactivity_timeout")
            return True

    def close_active(self, end_reason: str) -> None:
        """지금 녹화 중인 파일을 닫고 마감 큐에 넘긴다. 없으면 아무것도 하지 않는다.

        ``end_reason``은 `END_REASONS` 중 하나여야 한다. `Engine.stop()`은
        `engine_off`, 수신기 종료·오류 경로는 같은 값을 쓴다. 이 메서드는
        압축·업로드를 기다리지 않고 즉시 돌아온다.
        """
        if end_reason not in END_REASONS:
            raise ValueError(f"알 수 없는 종료 사유: {end_reason!r}")
        if not self._config.enabled:
            return
        with self._lock:
            self._close_active_locked(end_reason)

    # ------------------------------------------------------------------
    # 내부

    def _open_locked(
        self, session_uid: int, recv_ns: int, packet: Packet | None
    ) -> None:
        """새 세션 UID의 녹화를 연다. 같은 UID 재개면 다음 부 번호를 쓴다."""
        segment = next_recording_segment(self._conn, session_uid)
        path = self._active_path(session_uid, segment)
        # 백업 워커가 아직 주워 가지 못한 같은 부 파일이 있으면 덮어쓰지
        # 않는다. 부 번호를 올려 새 파일로 안전하게 이어 받는다.
        while path.exists():
            segment += 1
            path = self._active_path(session_uid, segment)

        session_type: int | None = None
        track_id: int | None = None
        category = session_category(None)
        if packet is not None and packet.header.packet_id == PacketId.SESSION:
            session_type = _as_int(packet.body.get("session_type"))
            track_id = _as_int(packet.body.get("track_id"))
            if session_type is not None:
                category = session_category(session_type)

        writer = RecordingWriter(path)
        try:
            recording_id = create_recording(
                self._conn,
                session_uid=session_uid,
                segment=segment,
                started_at_ns=recv_ns,
                category=category,
                session_type=session_type,
                track_id=track_id,
            )
        except BaseException:
            # DB 행을 못 만들면 방금 만든 빈 파일도 남기지 않는다. 기존
            # 파일은 위 while 문이 건너뛰었으므로 여기서 지워도 안전하다.
            writer.close()
            try:
                path.unlink()
            except OSError:
                pass
            raise

        self._active = _ActiveRecording(
            recording_id=recording_id,
            session_uid=session_uid,
            segment=segment,
            path=path,
            writer=writer,
            started_at_ns=recv_ns,
            last_packet_ns=recv_ns,
            category=category,
            session_type=session_type,
            track_id=track_id,
        )

    def _update_session_meta_locked(
        self, active: _ActiveRecording, packet: Packet
    ) -> None:
        """`PacketId.SESSION`의 값으로 범주·세션 종류·서킷을 갱신한다.

        SESSION은 2Hz라 값이 바뀔 때만 DB에 쓴다. 값이 아직 없는 필드는
        기존 값을 유지한다(`None`으로 지우지 않는다).
        """
        changed = False
        session_type = _as_int(packet.body.get("session_type"))
        if session_type is not None and session_type != active.session_type:
            active.session_type = session_type
            active.category = session_category(session_type)
            changed = True
        track_id = _as_int(packet.body.get("track_id"))
        if track_id is not None and track_id != active.track_id:
            active.track_id = track_id
            changed = True
        if not changed:
            return
        try:
            update_recording_session_meta(
                self._conn,
                active.recording_id,
                category=active.category,
                session_type=active.session_type,
                track_id=active.track_id,
            )
        except sqlite3.Error:
            # 메타데이터 한 번 실패로 60Hz 수신·녹화를 멈추지 않는다. 마감
            # 작업에는 메모리에 갱신한 값이 그대로 실린다.
            logger.exception(
                "녹화 메타데이터를 DB에 반영하지 못했습니다(recording_id=%s)",
                active.recording_id,
            )

    def _close_active_locked(self, end_reason: str) -> None:
        """활성 파일을 닫고 `FinalizeJob`을 백업 큐에 넣는다."""
        active = self._active
        if active is None:
            return
        self._active = None
        try:
            active.writer.close()
        except Exception:
            # 디스크 오류로 닫기가 실패해도 이미 쓴 레코드는 파일에 남는다.
            # 백업 워커가 완전한 레코드까지만 압축하므로 작업은 계속 넘긴다.
            logger.exception(
                "녹화 파일을 닫지 못했습니다(path=%s)", active.path
            )

        job = FinalizeJob(
            recording_id=active.recording_id,
            active_path=active.path,
            session_uid=active.session_uid,
            segment=active.segment,
            end_reason=end_reason,
            # 마지막 패킷 시각을 데이터의 끝으로 본다. 무수신 타임아웃처럼
            # 마감이 늦어도 길이·파일명이 실제 기록과 어긋나지 않는다.
            ended_at_ns=active.last_packet_ns,
            category=active.category,
            session_type=active.session_type,
            track_id=active.track_id,
        )
        if self._backup_worker is None:
            # 워커가 없으면 파일을 `.active/`에 남긴다. 다음 기동 때 서버의
            # 복구 경로가 주워 가고, 그전까지 원본은 그대로 보존된다.
            return
        try:
            self._backup_worker.enqueue(job)
        except Exception:
            # 큐 오류로 수신 루프·엔진 종료가 막히면 안 된다. 파일은
            # `.active/`에 남아 있으므로 시작 시 복구가 다시 줍는다.
            logger.exception(
                "마감된 녹화를 백업 큐에 넣지 못했습니다(path=%s)", active.path
            )

    def _active_path(self, session_uid: int, segment: int) -> Path:
        return self._active_dir / f"{session_uid}_s{segment}{RECORD_SUFFIX}"

    @staticmethod
    def _packet_session_uid(packet: Packet | None) -> int | None:
        if packet is None:
            return None
        return int(packet.header.session_uid)


def _as_int(value: object) -> int | None:
    """패킷 body 값 하나를 정수로. bool은 0/1로 본다."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


__all__ = [
    "ACTIVE_DIR_NAME",
    "FinalizeQueue",
    "SessionRecordingManager",
]
