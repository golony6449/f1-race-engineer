"""녹화 파일의 GCS 백업·온디맨드 복원 전송 계층(설계 4.2, 4.5).

백업 워커(T8)는 `RcloneTransport`로 `.f1rec.zst`를 GCS에 올리고, 웹 API(T12)는
`RecordingFetcher`로 로컬에 없는 녹화를 GCS에서 `.cache/`로 내려받아 내려준다.

- 업로드도 `rclone sync`가 아니라 `copyto`만 쓴다. 로컬 5개 정리로 녹화 파일을
  지워도 GCS 객체가 함께 지워지지 않게 하기 위해서다.
- rclone 바이너리가 없거나 GCS 설정(`enabled`·`remote`·`bucket`)이 비어 있으면
  `BackupSkippedError`를 올린다. 이때 호출자(백업 워커)는 `skipped`로 남기고
  로컬 파일은 그대로 보존한다(업로드 확인 전 로컬 파일 삭제 금지).
- 실패 사유에는 환경변수·설정 파일 내용을 넣지 않는다. rclone stderr의 마지막
  줄만 잘라 남겨 비밀값이 로그·응답으로 새지 않게 한다.

테스트는 실제 rclone·GCS·네트워크를 부르지 않는다. `which`·`runner`를 주입해
`copyto` 인자와 호출 횟수만 확인한다.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from f1re.config import RecordingConfig, RecordingGcsConfig, resolve_asset_path
from f1re.db import get_recording

# GCS 온디맨드 복원 캐시 폴더와 파일 확장자(설계 4.1).
CACHE_DIRNAME = ".cache"
CACHE_SUFFIX = ".f1rec.zst"

# rclone stderr를 오류 메시지에 남길 때 자르는 길이.
_ERROR_DETAIL_LIMIT = 300


class BackupError(RuntimeError):
    """rclone 업로드·다운로드가 실패했을 때.

    호출자는 이 예외를 받아도 로컬 파일을 지우면 안 된다. 업로드 성공이
    확인되지 않은 녹화는 보존 대상이다.
    """


class BackupSkippedError(RuntimeError):
    """rclone이 없거나 GCS 설정이 비어 있어 전송을 시도할 수 없을 때.

    실패(`failed`)와 구분해 `skipped`로 남기기 위한 예외다. 로컬 녹화와
    보존 정책에는 아무 영향이 없다.
    """


class RecordingNotFoundError(LookupError):
    """`session_recordings`에 없는 녹화 id를 복원 요청했을 때."""


def _expand_config_path(path: str) -> Path:
    """`~/.config/rclone/rclone.conf` 같은 설정 경로를 절대 경로로 편다."""
    return Path(path).expanduser()


class RcloneTransport:
    """시스템 `rclone` CLI로 GCS 객체를 올리고 내려받는다.

    `which`·`runner`는 테스트에서 실제 rclone 실행 없이 호출 인자를 확인하기
    위한 주입 지점이다. `runner`는 동기 함수이며 `asyncio.to_thread`로 워커
    스레드에서 실행해 이벤트 루프를 막지 않는다.
    """

    def __init__(
        self,
        config: RecordingGcsConfig | None = None,
        *,
        which: Callable[[str], str | None] | None = None,
        runner: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        self.config = config if config is not None else RecordingGcsConfig()
        self._which = which if which is not None else shutil.which
        self._runner = runner if runner is not None else self._run_subprocess

    # --- 설정 해석 ---------------------------------------------------------

    @property
    def remote_root(self) -> str:
        """`<remote>:<bucket>/<prefix>` 형식의 GCS 루트.

        prefix가 비어 있으면 `<remote>:<bucket>`만 된다. 앞뒤 `/`는 지워
        객체 경로를 조립할 때 `//`가 생기지 않게 한다.
        """
        root = f"{self.config.rclone_remote.strip()}:{self.config.bucket.strip()}"
        prefix = self.config.prefix.strip().strip("/")
        return f"{root}/{prefix}" if prefix else root

    def remote_path(self, remote_object: str) -> str:
        """DB의 `gcs_object`(예: `race/파일명.f1rec.zst`)를 rclone 원격 경로로.

        `..` 세그먼트나 빈 경로는 설정한 prefix 밖을 건드릴 수 있으므로
        거부한다.
        """
        relative = str(remote_object).replace("\\", "/").strip()
        parts = [part for part in relative.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise BackupError("GCS 객체 경로가 비었거나 잘못됐습니다")
        return f"{self.remote_root}/{'/'.join(parts)}"

    def is_available(self) -> bool:
        """업로드·복원을 시도할 수 있는 상태인지.

        GCS 사용 설정, 원격 이름·버킷, 시스템 rclone 바이너리를 차례로 본다.
        rclone 설정 파일 내용은 읽지 않는다(경로가 틀리면 rclone 실행이
        실패하고 호출자가 `failed`로 남긴다).
        """
        config = self.config
        if not config.enabled:
            return False
        if not config.rclone_remote.strip() or not config.bucket.strip():
            return False
        return self._which("rclone") is not None

    # --- 전송 -------------------------------------------------------------

    async def upload(self, local_path: Path, remote_object: str) -> None:
        """로컬 파일을 `<remote>:<bucket>/<prefix>/<remote_object>`로 올린다."""
        self._require_available()
        source = Path(local_path)
        if not source.is_file():
            raise BackupError(f"업로드할 로컬 파일이 없습니다: {source.name}")
        destination = self.remote_path(remote_object)
        await asyncio.to_thread(
            self._runner, self._copyto_args(str(source), destination)
        )

    async def download(self, remote_object: str, local_path: Path) -> None:
        """GCS 객체를 `local_path`로 내려받는다. 부모 폴더는 만들어 둔다."""
        self._require_available()
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self.remote_path(remote_object)
        await asyncio.to_thread(self._runner, self._copyto_args(source, str(target)))

    # --- 내부 -------------------------------------------------------------

    def _require_available(self) -> None:
        if not self.is_available():
            raise BackupSkippedError(
                "rclone 또는 GCS 설정이 없어 전송을 건너뜁니다"
            )

    def _copyto_args(self, source: str, destination: str) -> list[str]:
        binary = self._which("rclone")
        if binary is None:
            raise BackupSkippedError("rclone을 찾지 못했습니다")
        args = [binary, "copyto"]
        config_file = self.config.rclone_config.strip()
        if config_file:
            args.extend(["--config", str(_expand_config_path(config_file))])
        args.extend([source, destination])
        return args

    @staticmethod
    def _run_subprocess(args: Sequence[str]) -> None:
        """rclone을 실행하고 실패하면 `BackupError`로 바꾼다.

        stderr 마지막 줄만 잘라 남긴다. 환경변수나 설정 파일 내용은 넣지
        않으므로 시크릿이 오류 메시지로 새지 않는다.
        """
        try:
            completed = subprocess.run(
                list(args), capture_output=True, text=True, check=False
            )
        except OSError as exc:
            # 실행 파일 경로·권한 문제. 예외 문자열에 환경변수는 들어가지 않는다.
            raise BackupError(
                f"rclone을 실행하지 못했습니다: {exc.__class__.__name__}"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            last_line = detail.splitlines()[-1][:_ERROR_DETAIL_LIMIT] if detail else ""
            suffix = f": {last_line}" if last_line else ""
            raise BackupError(f"rclone 실패(exit {completed.returncode}){suffix}")


def _existing_file(raw_path: Any) -> Path | None:
    """DB의 `local_path`가 실제 파일이면 절대 경로로 돌려준다.

    레포 기준 상대 경로(설계 4.4)는 레포 루트에서 편다. 파일이 없으면 `None`.
    """
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = resolve_asset_path(path)
    return path if path.is_file() else None


class RecordingFetcher:
    """로컬에 없는 녹화를 GCS에서 `.cache/`로 복원한다(설계 4.5).

    - 로컬 보유 파일(`local_path`)이 있으면 내려받지 않고 바로 돌려준다.
    - 없으면 `gcs_object`를 `.cache/<recording_id>.f1rec.zst`로 한 번만
      내려받는다. 같은 `recording_id`의 동시 요청은 `asyncio.Lock`으로 합쳐
      중복 rclone 호출을 막는다.
    - 캐시는 `keep_cache_count`개까지만 남기고 오래 쓴 것부터 지운다(LRU).
      지금 돌려줄 파일은 `keep_cache_count`가 0이어도 지우지 않는다.

    `conn_factory`는 호출마다 새 SQLite 연결을 여는 공장이어야 한다. 이
    클래스가 읽고 닫는다.
    """

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        config: RecordingConfig,
        *,
        transport: RcloneTransport | None = None,
    ) -> None:
        self.config = config
        self.recordings_dir = resolve_asset_path(config.dir)
        self.cache_dir = self.recordings_dir / CACHE_DIRNAME
        self.transport = (
            transport if transport is not None else RcloneTransport(config.gcs)
        )
        self._conn_factory = conn_factory
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        # 이 프로세스에서 캐시를 쓴 순서. 파일 mtime이 같아도 LRU 순서가
        # 결정되도록 메모리 순번을 우선한다.
        self._cache_order: dict[int, int] = {}
        self._cache_clock = 0

    # --- 공개 -------------------------------------------------------------

    def cache_path(self, recording_id: int) -> Path:
        """`recording_id`의 복원 캐시 파일 경로(설계 4.1)."""
        return self.cache_dir / f"{int(recording_id)}{CACHE_SUFFIX}"

    async def ensure_local_file(self, recording_id: int) -> Path:
        """녹화 파일의 로컬 경로를 보장해 돌려준다.

        로컬 보유 파일이 있으면 그 경로, 없으면 GCS에서 복원한 캐시 경로다.
        녹화 행이 없으면 `RecordingNotFoundError`, 로컬·GCS 어디에도 파일이
        없으면 `BackupError`, rclone이 없으면 `BackupSkippedError`를 올린다.
        """
        key = int(recording_id)
        lock = await self._lock_for(key)
        async with lock:
            # 잠금을 기다리는 동안 다른 요청이 이미 받아 두었을 수 있으므로
            # 잠금 안에서 다시 확인한다.
            return await self._ensure_local_file_locked(key)

    # --- 내부 -------------------------------------------------------------

    async def _ensure_local_file_locked(self, recording_id: int) -> Path:
        row = self._load_row(recording_id)
        if row is None:
            raise RecordingNotFoundError(
                f"녹화 {recording_id}를 찾을 수 없습니다"
            )

        local = _existing_file(row.get("local_path"))
        if local is not None:
            return local

        cached = self.cache_path(recording_id)
        if cached.is_file() and cached.stat().st_size > 0:
            self._touch_cache(recording_id)
            self._prune_cache(protect=recording_id)
            return cached

        remote_object = row.get("gcs_object")
        if not remote_object:
            raise BackupError(
                f"녹화 {recording_id}는 로컬에도 GCS에도 없습니다"
            )

        # 반쯤 내려받은 파일을 캐시로 노출하지 않도록 임시 파일에 받아
        # 원자적으로 바꿔 끼운다.
        partial = self.cache_dir / (
            f".{recording_id}.{os.getpid()}.{uuid.uuid4().hex}.part"
        )
        try:
            await self.transport.download(str(remote_object), partial)
            if not partial.is_file() or partial.stat().st_size <= 0:
                raise BackupError(
                    f"녹화 {recording_id} 복원 파일이 비어 있습니다"
                )
            os.replace(partial, cached)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

        self._touch_cache(recording_id)
        self._prune_cache(protect=recording_id)
        return cached

    def _load_row(self, recording_id: int) -> dict[str, Any] | None:
        """녹화 행을 읽는다. 연결은 factory가 새로 열고 여기서 닫는다."""
        conn = self._conn_factory()
        try:
            return get_recording(conn, recording_id)
        finally:
            conn.close()

    async def _lock_for(self, recording_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(recording_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[recording_id] = lock
            return lock

    def _touch_cache(self, recording_id: int) -> None:
        self._cache_clock += 1
        # dict는 삽입 순서를 지키므로 지웠다 다시 넣으면 최신이 된다.
        self._cache_order.pop(recording_id, None)
        self._cache_order[recording_id] = self._cache_clock

    def _prune_cache(self, *, protect: int) -> None:
        """`keep_cache_count`개를 넘는 복원 캐시를 오래된 것부터 지운다.

        `protect`(지금 돌려줄 파일)는 상한 계산에서 항상 남긴다.
        """
        try:
            entries = list(self.cache_dir.glob(f"*{CACHE_SUFFIX}"))
        except OSError:
            return
        files: dict[int, Path] = {}
        for entry in entries:
            stem = entry.name[: -len(CACHE_SUFFIX)]
            if stem.isdigit():
                files[int(stem)] = entry
        limit = max(1, int(self.config.keep_cache_count))
        if len(files) <= limit:
            return

        def age_key(recording_id: int) -> tuple[int, int]:
            order = self._cache_order.get(recording_id)
            if order is not None:
                return (1, order)
            try:
                return (0, files[recording_id].stat().st_mtime_ns)
            except OSError:
                return (-1, 0)

        ordered = sorted(files, key=age_key)
        removable = [rid for rid in ordered if rid != protect]
        for recording_id in removable[: len(files) - limit]:
            try:
                files[recording_id].unlink(missing_ok=True)
            except OSError:
                # 지우지 못한 파일은 다음 정리 때 다시 시도한다.
                continue
            self._cache_order.pop(recording_id, None)
