"""마감된 raw 녹화의 비동기 압축·GCS 백업·로컬 보존 정리(설계 4.2).

`BackupWorker`는 웹 서버 `lifespan`이 소유하는 독립 워커다(엔진 내부가 아님).
60Hz UDP 수신 경로(`SessionRecordingManager`)는 마감된 `.active/*.f1rec`을
`enqueue(FinalizeJob)`로 넘기기만 하고 즉시 돌아간다. 워커는 전용 SQLite
연결로 다음을 순차 수행한다.

1. `.active/<uid>_s<segment>.f1rec` → `<category>/<파일명>.f1rec.zst` 스트리밍 압축
2. DB에 `backup_status = 'pending'`, `local_exists = 1`, `file_size_bytes` 기록
3. `rclone copyto`로 GCS 업로드(주입 가능한 `UploadTransport`)
4. 성공 시 `uploaded`, 실패 시 `failed` — 업로드 성공이 확인되기 전에는 로컬
   파일을 절대 지우지 않는다.
5. 업로드된 녹화 중 최신 `keep_local_count`개만 남기고 나머지 로컬 파일을
   지운다(웹 리플레이가 재생 중인 녹화는 제외).

웹 서버가 기동되면 `.active/`에 남은 미마감 파일을 손상된 끝부분을 뺀 완전한
레코드까지만 압축해 복구(`end_reason = 'recovered'`)하고, `pending`·`failed`
행을 재시도 큐에 넣는다. `rclone`이 없거나 GCS가 꺼져 있으면 `skipped`로
남기고 로컬 파일은 그대로 보존한다. 주기 재시도(기본 60초)도 같은 워커가 한다.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from f1re import db
from f1re.config import RecordingConfig, RecordingGcsConfig, resolve_asset_path
from f1re.udp.recording import (
    RECORD_SUFFIX,
    ZST_SUFFIX,
    FinalizeJob,
    compress_recording,
    iter_recording,
    scan_recording,
)

logger = logging.getLogger(__name__)

# 활성 녹화가 쓰이는 하위 폴더(설계 4.1 디렉터리 구조).
ACTIVE_DIRNAME = ".active"
# pending/failed 재시도 주기. 서버가 켜져 있는 동안 자동으로 다시 올린다.
DEFAULT_RETRY_INTERVAL_S = 60.0
# `backup_error` 열에 남길 사유의 최대 길이. rclone stderr가 길어도 DB를 부풀리지 않는다.
BACKUP_ERROR_MAX_CHARS = 500
# 세션 범주(설계 4.1). 폴더 이름으로 쓰이므로 목록 밖의 값은 `unknown`으로 낮춘다.
CATEGORIES = frozenset({"practice", "qualifying", "race", "time_trial", "unknown"})


class BackupTransportError(RuntimeError):
    """원격 업로드(rclone) 실패. 워커가 `failed`로 기록하고 로컬을 보존한다."""


class UploadTransport(Protocol):
    """녹화 파일을 원격 저장소로 올리는 최소 인터페이스.

    실제 구현은 시스템 `rclone`을 부르는 `RcloneUploadTransport`이고, 테스트는
    실제 네트워크·rclone 호출 없이 가짜를 주입한다. `upload`는 실패 시 예외를
    올리며, 워커가 그 예외를 `backup_error`로 요약해 남긴다.
    """

    def is_available(self) -> bool:
        """업로드 경로(바이너리·설정·버킷)가 준비됐는지."""

    async def upload(self, local_path: Path, remote_object: str) -> None:
        """`local_path`를 `<remote>:<bucket>/<prefix>/<remote_object>`로 올린다."""


class RcloneUploadTransport:
    """시스템 `rclone copyto`로 GCS에 올리는 기본 업로더.

    `rclone sync`가 아니라 `copyto`를 써서 로컬 파일 삭제가 원격 삭제로
    이어지지 않게 한다. 비밀값은 쓰지 않으며 설정 파일 경로만 rclone에 넘긴다.
    """

    def __init__(
        self,
        gcs: RecordingGcsConfig,
        *,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._gcs = gcs
        self._which = which

    def is_available(self) -> bool:
        gcs = self._gcs
        if not gcs.enabled or not gcs.bucket.strip() or not gcs.rclone_remote.strip():
            return False
        if self._which("rclone") is None:
            return False
        return Path(gcs.rclone_config).expanduser().is_file()

    def remote_path(self, remote_object: str) -> str:
        """`<remote>:<bucket>/<prefix>/<object>` 전체 원격 경로."""
        base = f"{self._gcs.rclone_remote.strip()}:{self._gcs.bucket.strip()}"
        parts = [self._gcs.prefix.strip("/"), remote_object.strip("/")]
        suffix = "/".join(part for part in parts if part)
        return f"{base}/{suffix}" if suffix else base

    async def upload(self, local_path: Path, remote_object: str) -> None:
        command = [
            "rclone",
            "copyto",
            str(local_path),
            self.remote_path(remote_object),
            "--config",
            str(Path(self._gcs.rclone_config).expanduser()),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BackupTransportError(f"rclone 실행 실패: {exc}") from exc
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            lines = stderr.decode("utf-8", "replace").strip().splitlines()
            detail = lines[-1] if lines else f"종료 코드 {process.returncode}"
            raise BackupTransportError(f"rclone 업로드 실패: {detail}")


@dataclass(frozen=True)
class _RetryJob:
    """DB에 이미 확정된 녹화를 다시 업로드하는 내부 작업."""

    recording_id: int


@dataclass(frozen=True)
class _FileStats:
    """활성 `.f1rec` 한 번 훑어 얻은 패킷 수와 첫·마지막 수신 시각."""

    packet_count: int
    first_ns: int | None
    last_ns: int


class BackupWorker:
    """마감된 녹화를 압축·업로드하고 로컬 최신 N개만 남기는 독립 워커.

    `Engine`과 분리돼 있어 45분 레이스의 압축·업로드가 `Engine.stop()`이나
    서비스 종료(`TimeoutStopSec`)를 막지 않는다. `enqueue`는 어느 스레드에서
    불려도 되며(비블로킹), 실제 처리는 워커 태스크가 전용 SQLite 연결로 순차
    수행한다. `aclose`는 진행 중 작업을 취소하고 즉시 돌아간다. 아직 확정되지
    않은 작업은 DB(`pending`/`failed`)나 `.active/`에 남아 다음 기동에서
    복구·재시도된다.
    """

    def __init__(
        self,
        db_path: str | Path | None,
        config: RecordingConfig,
        *,
        transport: UploadTransport | None = None,
        active_replay_id: Callable[[], int | None] | None = None,
        retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    ) -> None:
        if not math.isfinite(retry_interval_s) or retry_interval_s <= 0:
            raise ValueError("retry_interval_s는 0보다 큰 유한한 수여야 합니다")
        self._config = config
        self._transport: UploadTransport = (
            transport if transport is not None else RcloneUploadTransport(config.gcs)
        )
        self._active_replay_id = active_replay_id
        self._retry_interval_s = retry_interval_s
        # 전용 SQLite 연결(설계 4.2). 워커 태스크/수동 재시도만 이 연결을 쓴다.
        self._conn = db.open_db(db_path)
        self._process_lock = asyncio.Lock()
        self._queue: asyncio.Queue[FinalizeJob | _RetryJob] = asyncio.Queue()
        self._state_lock = threading.Lock()
        self._pending_count = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._prestart: deque[FinalizeJob | _RetryJob] = deque()
        self._scheduled_retries: set[int] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        # 취소된 코루틴과 별개로 스레드풀 작업은 끝까지 돈다. 종료 시 DB 연결을
        # 그런 작업이 쓰는 중에 닫지 않으려고 진행 중 개수를 센다.
        self._inflight_lock = threading.Lock()
        self._inflight_sync = 0

    # --- 수명 주기 ---------------------------------------------------------

    async def start(self) -> None:
        """미마감 활성 파일 복구와 pending/failed 재시도를 등록하고 워커를 띄운다."""
        if self._task is not None:
            return
        self._closing = False
        self._loop = asyncio.get_running_loop()
        # `start()` 전에 들어온 작업을 먼저 넣는다. 아래 await 중에 복구 작업이
        # 먼저 큐에 들어가 순서가 뒤집히지 않게 여기서 바로 비운다.
        with self._state_lock:
            queued = list(self._prestart)
            self._prestart.clear()
        for job in queued:
            self._queue.put_nowait(job)
        async with self._process_lock:
            recovered = await self._run_blocking(self._recover_active_files)
            for job in recovered:
                self._enqueue(job)
            pending = await self._run_blocking(
                db.list_pending_backup_recordings, self._conn
            )
            for row in pending:
                self._enqueue_retry(int(row["id"]))
        self._task = asyncio.create_task(self._run(), name="f1re-backup-worker")

    async def aclose(self) -> None:
        """워커 태스크를 멈추고 DB 연결을 닫는다(진행 중 압축은 다음 기동이 복구)."""
        self._closing = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        with self._inflight_lock:
            busy = self._inflight_sync > 0
        if busy:
            # 스레드풀 작업이 아직 연결을 쓰고 있다. 프로세스가 곧 끝나므로
            # 연결을 닫지 않고 둔다(닫으면 그 스레드가 sqlite3 오류를 낸다).
            logger.warning("백업 작업이 진행 중이라 SQLite 연결을 닫지 않고 종료합니다")
            return
        await asyncio.to_thread(self._conn.close)

    async def join(self, timeout: float | None = None) -> None:
        """현재까지 들어온 작업이 모두 끝날 때까지 기다린다(테스트·종료 확인용)."""
        waiter = self._idle.wait()
        if timeout is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout)

    # --- 작업 등록 ---------------------------------------------------------

    def enqueue(self, job: FinalizeJob) -> None:
        """마감 작업을 큐에 넣는다. 어느 스레드에서 불러도 비블로킹이다."""
        self._enqueue(job)

    async def retry(self, recording_id: int) -> dict[str, Any]:
        """녹화 하나의 업로드를 즉시 다시 시도하고 결과 요약을 돌려준다.

        수동 재시도 API가 쓴다. 로컬 파일이 없거나 아직 녹화 중이면 업로드하지
        않고 사유를 돌려준다. 성공하면 로컬 5개 정리 규칙도 곧바로 적용한다.
        """
        target = int(recording_id)
        async with self._process_lock:
            row = await self._db_call(db.get_recording, target)
            if row is None:
                return {
                    "recording_id": target,
                    "backup_status": None,
                    "gcs_object": None,
                    "error": "녹화를 찾을 수 없습니다",
                }
            if row["end_reason"] == "recording" or row["backup_status"] == "recording":
                return {
                    "recording_id": target,
                    "backup_status": row["backup_status"],
                    "gcs_object": row["gcs_object"],
                    "error": "아직 녹화가 끝나지 않았습니다",
                }
            await self._upload_and_prune(target)
            updated = await self._db_call(db.get_recording, target) or row
            return {
                "recording_id": target,
                "backup_status": updated["backup_status"],
                "gcs_object": updated["gcs_object"],
                "error": updated["backup_error"],
            }

    def _enqueue(self, job: FinalizeJob | _RetryJob) -> None:
        with self._state_lock:
            self._pending_count += 1
            self._idle.clear()
            loop = self._loop
            if loop is None or not loop.is_running():
                self._prestart.append(job)
                return
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, job)
            except RuntimeError:
                # 루프가 막 닫힌 경우. 다음 기동 복구가 파일을 다시 찾는다.
                self._prestart.append(job)

    def _enqueue_retry(self, recording_id: int) -> None:
        with self._state_lock:
            if recording_id in self._scheduled_retries:
                return
            self._scheduled_retries.add(recording_id)
        self._enqueue(_RetryJob(recording_id))

    # --- 워커 루프 ---------------------------------------------------------

    async def _run(self) -> None:
        while not self._closing:
            try:
                job = await asyncio.wait_for(
                    self._queue.get(), timeout=self._retry_interval_s
                )
            except TimeoutError:
                try:
                    await self._retry_sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:  # 재시도 목록 조회 실패로 워커를 죽이지 않는다.
                    logger.exception("백업 재시도 목록을 읽지 못했습니다")
                continue
            try:
                async with self._process_lock:
                    await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # 한 작업의 버그가 다음 작업을 막지 않게 한다.
                logger.exception("백업 작업 처리 실패: %s", _job_label(job))
            finally:
                self._queue.task_done()
                self._finish_job(job)
                self._mark_done()

    async def _retry_sweep(self) -> None:
        # 수동 재시도(`retry`)와 같은 전용 연결을 쓰므로 같은 잠금으로 직렬화한다.
        async with self._process_lock:
            rows = await self._db_call(db.list_pending_backup_recordings)
        for row in rows:
            self._enqueue_retry(int(row["id"]))

    def _finish_job(self, job: FinalizeJob | _RetryJob) -> None:
        if isinstance(job, _RetryJob):
            with self._state_lock:
                self._scheduled_retries.discard(job.recording_id)

    def _mark_done(self) -> None:
        with self._state_lock:
            if self._pending_count > 0:
                self._pending_count -= 1
            # 다른 스레드의 enqueue(clear)와 엇갈리지 않게 잠금 안에서 깨운다.
            if self._pending_count == 0:
                self._idle.set()

    # --- 처리 -------------------------------------------------------------

    async def _process(self, job: FinalizeJob | _RetryJob) -> None:
        if isinstance(job, _RetryJob):
            await self._upload_and_prune(job.recording_id)
            return
        row = await self._db_call(db.get_recording, job.recording_id)
        if row is None:
            # 행 없이 파일만 남은 경우. 메타데이터를 만들 수 없으니 손대지 않는다.
            logger.warning("마감 작업의 녹화 행이 없습니다: %s", job.recording_id)
            return
        local_path = row["local_path"]
        if local_path and Path(local_path).is_file():
            # 이미 압축된 파일이 있다(중복 마감·기동 복구). 업로드 단계부터 잇는다.
            await self._run_blocking(_unlink_quietly, job.active_path)
            await self._upload_and_prune(job.recording_id)
            return
        if row["end_reason"] != "recording" or row["backup_status"] != "recording":
            # 이미 마감된 행의 중복 작업(기동 복구와 큐 작업이 겹친 경우 등).
            # 상태를 `failed`로 낮추지 않고 남은 활성 파일만 치운다.
            await self._run_blocking(_unlink_quietly, job.active_path)
            return
        await self._finalize_active(job, row)

    async def _finalize_active(
        self, job: FinalizeJob, row: dict[str, Any]
    ) -> None:
        finalized = await self._run_blocking(self._compress_and_finalize, job, row)
        if finalized:
            await self._upload_and_prune(job.recording_id)

    def _compress_and_finalize(
        self, job: FinalizeJob, row: dict[str, Any]
    ) -> bool:
        """스캔 → min_packets 검사 → 압축 → 원본 삭제 → DB pending 기록.

        전부 한 번의 스레드풀 호출로 처리해, 코루틴이 취소돼도 DB와 파일 상태가
        어긋난 중간 지점에서 멈추지 않게 한다. 반환값은 업로드 단계로 갈지다.
        """
        active_path = Path(job.active_path)
        if not active_path.is_file():
            self._finalize_missing_file(job)
            return False
        packet_count, duration_ms = scan_recording(active_path)
        if packet_count < self._config.min_packets:
            self._discard_under_min_packets(job, row, active_path)
            return False
        category = _safe_category(job.category or row["category"])
        target = self._compressed_path(
            category=category,
            started_at_ns=int(row["started_at_ns"]),
            track_id=job.track_id if job.track_id is not None else row["track_id"],
            session_uid=job.session_uid,
            segment=job.segment,
        )
        file_size = compress_recording(active_path, target)
        db.update_recording_session_meta(
            self._conn,
            job.recording_id,
            category=category,
            session_type=job.session_type,
            track_id=job.track_id,
        )
        # DB에 압축 파일을 먼저 남기고 원본을 지운다. 그 사이에 프로세스가
        # 죽어도 다음 기동 복구가 압축 파일을 찾아 업로드만 다시 한다.
        db.finalize_recording(
            self._conn,
            job.recording_id,
            end_reason=job.end_reason,
            ended_at_ns=job.ended_at_ns,
            packet_count=packet_count,
            duration_ms=duration_ms,
            local_path=str(target),
            local_exists=True,
            file_size_bytes=file_size,
            backup_status="pending",
        )
        _unlink_quietly(active_path)
        return True

    def _finalize_missing_file(self, job: FinalizeJob) -> None:
        """활성 파일이 사라진 경우 행만 마감해 화면·재시도가 일관되게 남긴다."""
        db.finalize_recording(
            self._conn,
            job.recording_id,
            end_reason=job.end_reason,
            ended_at_ns=job.ended_at_ns,
            packet_count=0,
            duration_ms=0,
            local_path=None,
            local_exists=False,
            file_size_bytes=None,
            backup_status="failed",
        )
        db.mark_recording_backup(
            self._conn,
            job.recording_id,
            backup_status="failed",
            backup_error="활성 녹화 파일이 없습니다",
        )

    def _discard_under_min_packets(
        self, job: FinalizeJob, row: dict[str, Any], active_path: Path
    ) -> None:
        """0바이트·`min_packets` 미만 파일은 압축·업로드하지 않고 폐기한다."""
        _unlink_quietly(active_path)
        already_finalized = (
            row["end_reason"] != "recording"
            or bool(row["local_path"])
            or row["backup_status"] != "recording"
        )
        if not already_finalized:
            db.delete_recording_row(self._conn, job.recording_id)

    async def _upload_and_prune(self, recording_id: int) -> None:
        row = await self._db_call(db.get_recording, recording_id)
        if row is None:
            return
        local_path = Path(row["local_path"]) if row["local_path"] else None
        if local_path is None or not local_path.is_file():
            if row["backup_status"] != "uploaded":
                await self._db_call(
                    db.mark_recording_backup,
                    recording_id,
                    backup_status="failed",
                    backup_error="로컬 녹화 파일이 없습니다",
                )
            return
        if row["backup_status"] == "uploaded":
            await self._prune_local()
            return
        remote_object = _gcs_object(row, local_path)
        if not self._config.gcs.enabled:
            await self._mark_skipped(recording_id, "GCS 백업이 꺼져 있습니다")
            return
        try:
            available = await asyncio.to_thread(self._transport.is_available)
        except Exception as exc:  # 업로더 상태 확인 자체가 실패한 경우
            await self._mark_failed(recording_id, _short_error(exc))
            return
        if not available:
            await self._mark_skipped(recording_id, "rclone을 사용할 수 없습니다")
            return
        try:
            await self._transport.upload(local_path, remote_object)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = _short_error(exc)
            logger.warning("녹화 백업 업로드 실패(%s): %s", recording_id, message)
            await self._mark_failed(recording_id, message)
            return
        await self._db_call(
            _mark_uploaded,
            recording_id,
            remote_object,
            time.time_ns(),
        )
        await self._prune_local()

    async def _mark_failed(self, recording_id: int, message: str) -> None:
        await self._db_call(
            db.mark_recording_backup,
            recording_id,
            backup_status="failed",
            backup_error=message,
        )

    async def _mark_skipped(self, recording_id: int, reason: str) -> None:
        logger.warning("녹화 백업 건너뜀(%s): %s", recording_id, reason)
        await self._db_call(
            db.mark_recording_backup,
            recording_id,
            backup_status="skipped",
            backup_error=reason,
        )

    async def _prune_local(self) -> None:
        """업로드 확인된 녹화 중 최신 `keep_local_count`개만 로컬에 남긴다.

        `exclude_ids`로 지금 웹 리플레이가 재생 중인 녹화를 순위 계산 전에
        빼고, 삭제 직전에 한 번 더 확인해 재생 시작과의 경합도 피한다.
        업로드되지 않은 행은 `find_local_prune_targets`가 후보로 주지 않는다.
        """
        replay_id = self._current_replay_id()
        exclude_ids = [] if replay_id is None else [replay_id]
        rows = await self._db_call(
            db.find_local_prune_targets,
            keep_count=self._config.keep_local_count,
            exclude_ids=exclude_ids,
        )
        if not rows:
            return
        root = self._recordings_root()
        for row in rows:
            recording_id = int(row["id"])
            replay_id = self._current_replay_id()
            if replay_id is not None and replay_id == recording_id:
                continue
            path_text = row["local_path"]
            if not path_text:
                await self._db_call(db.mark_recording_local_deleted, recording_id)
                continue
            path = Path(path_text)
            if not _is_within(root, path):
                # DB가 이상해도 녹화 폴더 밖 파일은 지우지 않는다.
                logger.warning("녹화 폴더 밖의 파일은 지우지 않습니다: %s", path)
                continue
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError as exc:
                logger.warning("로컬 녹화 삭제 실패(%s): %s", path, exc)
                continue
            await self._db_call(db.mark_recording_local_deleted, recording_id)

    # --- 기동 복구 ---------------------------------------------------------

    def _recover_active_files(self) -> list[FinalizeJob | _RetryJob]:
        """`.active/*.f1rec` 잔여 파일을 복구 작업으로 바꾼다.

        손상된 끝부분은 `iter_recording`이 무시하므로 완전한 레코드까지만
        압축된다. 0바이트·`min_packets` 미만 파일은 폐기하고, 이미 DB에
        확정된 행이면 업로드 재시도로 돌린다.
        """
        active_dir = self._recordings_root() / ACTIVE_DIRNAME
        if not active_dir.is_dir():
            return []
        jobs: list[FinalizeJob | _RetryJob] = []
        for path in sorted(active_dir.glob(f"*{RECORD_SUFFIX}")):
            parsed = _parse_active_name(path.name)
            if parsed is None:
                logger.warning("활성 녹화 파일 이름을 해석하지 못했습니다: %s", path)
                continue
            session_uid, segment = parsed
            stats = _inspect_recording(path)
            row = self._find_recording(session_uid, segment)
            if stats.packet_count < self._config.min_packets:
                _unlink_quietly(path)
                if (
                    row is not None
                    and row["end_reason"] == "recording"
                    and not row["local_path"]
                ):
                    db.delete_recording_row(self._conn, int(row["id"]))
                continue
            started_at_ns = (
                stats.first_ns
                if stats.first_ns is not None
                else path.stat().st_mtime_ns
            )
            ended_at_ns = stats.last_ns or started_at_ns
            if row is None:
                recording_id = db.create_recording(
                    self._conn,
                    session_uid=session_uid,
                    segment=segment,
                    started_at_ns=started_at_ns,
                )
                row = db.get_recording(self._conn, recording_id)
            if row is None:  # pragma: no cover - create 직후에는 항상 있다.
                continue
            if row["local_path"] and Path(row["local_path"]).is_file():
                # 압축은 끝났는데 원본만 남은 경우. 원본을 지우고 업로드만 다시 한다.
                _unlink_quietly(path)
                jobs.append(_RetryJob(int(row["id"])))
                continue
            end_reason = (
                "recovered"
                if row["end_reason"] == "recording"
                else str(row["end_reason"])
            )
            jobs.append(
                FinalizeJob(
                    recording_id=int(row["id"]),
                    active_path=path,
                    session_uid=session_uid,
                    segment=segment,
                    end_reason=end_reason,
                    ended_at_ns=ended_at_ns,
                    category=str(row["category"]),
                    session_type=row["session_type"],
                    track_id=row["track_id"],
                )
            )
        return jobs

    def _find_recording(
        self, session_uid: int, segment: int
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM session_recordings
            WHERE session_uid = ? AND segment = ?
            """,
            (session_uid, segment),
        ).fetchone()
        return None if row is None else dict(row)

    # --- 공용 도우미 -------------------------------------------------------

    async def _db_call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await self._run_blocking(func, self._conn, *args, **kwargs)

    async def _run_blocking(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """스레드풀에서 실행하고 진행 중 개수를 센다(종료 시 연결 보호)."""
        with self._inflight_lock:
            self._inflight_sync += 1
        return await asyncio.to_thread(self._call_counted, func, args, kwargs)

    def _call_counted(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        try:
            return func(*args, **kwargs)
        finally:
            with self._inflight_lock:
                self._inflight_sync -= 1

    def _recordings_root(self) -> Path:
        folder = Path(self._config.dir)
        return folder if folder.is_absolute() else resolve_asset_path(folder)

    def _compressed_path(
        self,
        *,
        category: str,
        started_at_ns: int,
        track_id: int | None,
        session_uid: int,
        segment: int,
    ) -> Path:
        stamp = time.strftime(
            "%Y%m%d-%H%M%S", time.localtime(started_at_ns / 1_000_000_000)
        )
        track = "t0" if track_id is None else f"t{int(track_id)}"
        name = f"{stamp}_{track}_{int(session_uid)}_s{int(segment)}{ZST_SUFFIX}"
        return self._recordings_root() / category / name

    def _current_replay_id(self) -> int | None:
        if self._active_replay_id is None:
            return None
        try:
            value = self._active_replay_id()
        except Exception:  # 리플레이 상태 조회 버그가 정리를 막지 않게 한다.
            logger.exception("리플레이 사용 중 녹화를 확인하지 못했습니다")
            return None
        return None if value is None else int(value)


def _mark_uploaded(
    conn: sqlite3.Connection,
    recording_id: int,
    gcs_object: str,
    backed_up_at_ns: int,
) -> None:
    """업로드 성공 상태를 남기고 이전 실패 사유를 지운다."""
    db.mark_recording_backup(
        conn,
        recording_id,
        backup_status="uploaded",
        gcs_object=gcs_object,
        backed_up_at_ns=backed_up_at_ns,
    )
    with conn:
        conn.execute(
            "UPDATE session_recordings SET backup_error = NULL WHERE id = ?",
            (recording_id,),
        )


def _gcs_object(row: dict[str, Any], local_path: Path) -> str:
    """`session_recordings.gcs_object`에 남길 `<category>/<파일명>`."""
    category = _safe_category(row["category"])
    return f"{category}/{local_path.name}"


def _safe_category(value: str | None) -> str:
    text = (value or "").strip()
    return text if text in CATEGORIES else "unknown"


def _parse_active_name(name: str) -> tuple[int, int] | None:
    """`.active/<session_uid>_s<segment>.f1rec` 이름을 `(uid, segment)`로."""
    if not name.endswith(RECORD_SUFFIX):
        return None
    stem = name[: -len(RECORD_SUFFIX)]
    uid_text, separator, segment_text = stem.rpartition("_s")
    if not separator or not uid_text.isdigit() or not segment_text.isdigit():
        return None
    session_uid = int(uid_text)
    segment = int(segment_text)
    if session_uid < 0 or segment < 1:
        return None
    return session_uid, segment


def _inspect_recording(path: Path) -> _FileStats:
    """손상된 끝을 뺀 완전한 레코드만 세어 첫·마지막 수신 시각을 낸다."""
    count = 0
    first_ns: int | None = None
    last_ns = 0
    for recv_ns, _data in iter_recording(path):
        if first_ns is None:
            first_ns = recv_ns
        last_ns = recv_ns
        count += 1
    return _FileStats(packet_count=count, first_ns=first_ns, last_ns=last_ns)


def _unlink_quietly(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:  # 파일이 잠겨 있어도 워커를 멈추지 않는다.
        logger.warning("파일을 지우지 못했습니다(%s): %s", path, exc)


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:BACKUP_ERROR_MAX_CHARS]


def _job_label(job: FinalizeJob | _RetryJob) -> str:
    if isinstance(job, _RetryJob):
        return f"retry:{job.recording_id}"
    return f"finalize:{job.recording_id}"
