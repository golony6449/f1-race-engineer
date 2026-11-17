"""`RcloneTransport`·`RecordingFetcher` 전송·온디맨드 복원·LRU 캐시 검증.

실제 `rclone`·GCS·네트워크는 부르지 않는다. `which`·`runner`(업로드·다운로드
실행기)와 DB 연결 공장을 가짜로 주입해 `copyto` 인자·호출 횟수·캐시 상태만
확인한다(설계 5 "테스트 전략", 계획 작업 7).
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from f1re import db
from f1re.config import RecordingConfig, RecordingGcsConfig
from f1re.udp import gcs_transfer
from f1re.udp.gcs_transfer import (
    BackupError,
    BackupSkippedError,
    RcloneTransport,
    RecordingFetcher,
    RecordingNotFoundError,
)


# --- 테스트 도우미(공용 conftest를 만들지 않는다) ---------------------------


def _make_db(tmp_path: Path) -> tuple[Path, Callable[[], sqlite3.Connection]]:
    """파일 DB를 만들고, 호출마다 새 연결을 여는 공장을 돌려준다.

    `:memory:`는 연결마다 DB가 따로 생기므로 실제 서버처럼 파일 DB를 쓴다.
    """
    db_path = tmp_path / "f1re.db"
    db.open_db(db_path).close()
    return db_path, lambda: db.open_db(db_path)


def _add_recording(
    db_path: Path,
    *,
    session_uid: int,
    segment: int = 1,
    gcs_object: str | None = None,
    local_path: str | None = None,
    local_exists: bool = False,
    started_at_ns: int = 1_000,
) -> int:
    """마감된 녹화 행을 하나 만든다. `gcs_object`가 있으면 업로드 완료 상태다."""
    conn = db.open_db(db_path)
    try:
        recording_id = db.create_recording(
            conn,
            session_uid=session_uid,
            segment=segment,
            started_at_ns=started_at_ns,
            category="race",
            session_type=15,
            track_id=1,
        )
        db.finalize_recording(
            conn,
            recording_id,
            end_reason="session_end",
            ended_at_ns=started_at_ns + 1_000_000,
            packet_count=100,
            duration_ms=1000,
            local_path=local_path,
            local_exists=local_exists,
            file_size_bytes=64,
            backup_status="uploaded" if gcs_object else "pending",
        )
        if gcs_object is not None:
            db.mark_recording_backup(
                conn,
                recording_id,
                backup_status="uploaded",
                gcs_object=gcs_object,
                backed_up_at_ns=started_at_ns + 2_000_000,
            )
        return recording_id
    finally:
        conn.close()


def _recording_config(tmp_path: Path, *, keep_cache_count: int = 3) -> RecordingConfig:
    return RecordingConfig(
        dir=str(tmp_path / "recordings"),
        keep_cache_count=keep_cache_count,
    )


class _FakeDownloadTransport:
    """`RecordingFetcher`에 주입하는 다운로더. 실제 rclone을 부르지 않는다."""

    def __init__(self, *, delay_s: float = 0.0, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, Path]] = []
        self._delay_s = delay_s
        self._error = error

    async def download(self, remote_object: str, local_path: Path) -> None:
        self.calls.append((remote_object, Path(local_path)))
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._error is not None:
            raise self._error
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-zstd")

    def is_available(self) -> bool:
        return True


def _failing_runner(args: Sequence[str]) -> None:
    raise AssertionError(f"rclone을 실행하면 안 됩니다: {args}")


# --- RecordingFetcher: 로컬 우선 ------------------------------------------


async def test_fetcher_returns_existing_local_path_without_download(
    tmp_path: Path,
) -> None:
    local = tmp_path / "race" / "20261115-120000_t1_123_s1.f1rec.zst"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"local-zstd")
    db_path, conn_factory = _make_db(tmp_path)
    recording_id = _add_recording(
        db_path,
        session_uid=123,
        gcs_object="race/20261115-120000_t1_123_s1.f1rec.zst",
        local_path=str(local),
        local_exists=True,
    )
    transport = _FakeDownloadTransport()
    fetcher = RecordingFetcher(
        conn_factory, _recording_config(tmp_path), transport=transport
    )

    assert await fetcher.ensure_local_file(recording_id) == local
    assert transport.calls == []


async def test_fetcher_prefers_existing_local_path_even_if_pruned(
    tmp_path: Path,
) -> None:
    """`local_exists = 0`이어도 파일이 실제로 있으면 내려받지 않고 그대로 쓴다."""
    local = tmp_path / "race" / "kept.f1rec.zst"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"local-zstd")
    db_path, conn_factory = _make_db(tmp_path)
    recording_id = _add_recording(
        db_path,
        session_uid=124,
        gcs_object="race/kept.f1rec.zst",
        local_path=str(local),
        local_exists=False,
    )
    transport = _FakeDownloadTransport()
    fetcher = RecordingFetcher(
        conn_factory, _recording_config(tmp_path), transport=transport
    )

    assert await fetcher.ensure_local_file(recording_id) == local
    assert transport.calls == []


# --- RecordingFetcher: GCS 복원·동시 요청 병합·LRU --------------------------


async def test_fetcher_deduplicates_concurrent_downloads_and_evicts_lru_cache(
    tmp_path: Path,
) -> None:
    db_path, conn_factory = _make_db(tmp_path)
    ids = [
        _add_recording(
            db_path,
            session_uid=200 + index,
            gcs_object=f"race/rec{index}.f1rec.zst",
            started_at_ns=1_000 + index,
        )
        for index in range(5)
    ]
    transport = _FakeDownloadTransport(delay_s=0.02)
    fetcher = RecordingFetcher(
        conn_factory,
        _recording_config(tmp_path, keep_cache_count=2),
        transport=transport,
    )

    # 같은 녹화를 동시에 5번 요청해도 rclone 다운로드는 한 번만 돈다.
    results = await asyncio.gather(
        *(fetcher.ensure_local_file(ids[0]) for _ in range(5))
    )
    assert len(set(results)) == 1
    assert len(transport.calls) == 1
    assert results[0] == fetcher.cache_path(ids[0])
    assert results[0].read_bytes() == b"fake-zstd"

    # 캐시에 있으면 다시 내려받지 않는다.
    assert await fetcher.ensure_local_file(ids[0]) == results[0]
    assert len(transport.calls) == 1

    # 최근 2개만 남기고 오래 쓴 것부터 지운다(LRU).
    for recording_id in ids[1:]:
        await fetcher.ensure_local_file(recording_id)
    cache_files = sorted(
        path.name for path in (tmp_path / "recordings" / ".cache").glob("*.f1rec.zst")
    )
    assert cache_files == sorted(
        [f"{ids[3]}.f1rec.zst", f"{ids[4]}.f1rec.zst"]
    )
    assert len(transport.calls) == 5


async def test_fetcher_keeps_current_download_when_cache_limit_is_zero(
    tmp_path: Path,
) -> None:
    """`keep_cache_count = 0`이어도 지금 돌려줄 파일은 지우지 않는다."""
    db_path, conn_factory = _make_db(tmp_path)
    first = _add_recording(
        db_path, session_uid=300, gcs_object="race/first.f1rec.zst", started_at_ns=10
    )
    second = _add_recording(
        db_path, session_uid=301, gcs_object="race/second.f1rec.zst", started_at_ns=20
    )
    fetcher = RecordingFetcher(
        conn_factory,
        _recording_config(tmp_path, keep_cache_count=0),
        transport=_FakeDownloadTransport(),
    )

    await fetcher.ensure_local_file(first)
    second_path = await fetcher.ensure_local_file(second)

    assert second_path.is_file()
    assert not fetcher.cache_path(first).exists()


# --- RecordingFetcher: 오류 처리 ------------------------------------------


async def test_fetcher_raises_for_missing_or_unbacked_recording(
    tmp_path: Path,
) -> None:
    db_path, conn_factory = _make_db(tmp_path)
    fetcher = RecordingFetcher(
        conn_factory, _recording_config(tmp_path), transport=_FakeDownloadTransport()
    )

    with pytest.raises(RecordingNotFoundError):
        await fetcher.ensure_local_file(9999)

    unbacked = _add_recording(db_path, session_uid=400, gcs_object=None)
    with pytest.raises(BackupError):
        await fetcher.ensure_local_file(unbacked)


async def test_fetcher_failure_leaves_no_partial_cache(tmp_path: Path) -> None:
    db_path, conn_factory = _make_db(tmp_path)
    recording_id = _add_recording(
        db_path, session_uid=500, gcs_object="race/broken.f1rec.zst"
    )
    fetcher = RecordingFetcher(
        conn_factory,
        _recording_config(tmp_path),
        transport=_FakeDownloadTransport(error=BackupError("rclone 실패(exit 1)")),
    )

    with pytest.raises(BackupError):
        await fetcher.ensure_local_file(recording_id)

    cache_dir = tmp_path / "recordings" / ".cache"
    assert list(cache_dir.glob("*")) == []


async def test_fetcher_propagates_skipped_when_rclone_is_unavailable(
    tmp_path: Path,
) -> None:
    db_path, conn_factory = _make_db(tmp_path)
    recording_id = _add_recording(
        db_path, session_uid=600, gcs_object="race/x.f1rec.zst"
    )
    transport = RcloneTransport(
        RecordingGcsConfig(enabled=True, bucket="bucket"),
        which=lambda name: None,
        runner=_failing_runner,
    )
    fetcher = RecordingFetcher(
        conn_factory, _recording_config(tmp_path), transport=transport
    )

    with pytest.raises(BackupSkippedError):
        await fetcher.ensure_local_file(recording_id)


# --- RcloneTransport: 설정 확인과 copyto ----------------------------------


async def test_rclone_transport_skips_when_binary_or_bucket_missing(
    tmp_path: Path,
) -> None:
    local = tmp_path / "a.f1rec.zst"
    local.write_bytes(b"zstd")

    disabled = [
        RecordingGcsConfig(enabled=False, bucket="bucket"),
        RecordingGcsConfig(enabled=True, bucket=""),
        RecordingGcsConfig(enabled=True, bucket="bucket", rclone_remote="  "),
    ]
    for config in disabled:
        transport = RcloneTransport(
            config, which=lambda name: "/usr/bin/rclone", runner=_failing_runner
        )
        assert transport.is_available() is False
        with pytest.raises(BackupSkippedError):
            await transport.upload(local, "race/a.f1rec.zst")
        with pytest.raises(BackupSkippedError):
            await transport.download("race/a.f1rec.zst", tmp_path / "out.f1rec.zst")

    # 바이너리가 없으면 설정이 멀쩡해도 건너뛴다.
    missing_binary = RcloneTransport(
        RecordingGcsConfig(enabled=True, bucket="bucket"),
        which=lambda name: None,
        runner=_failing_runner,
    )
    assert missing_binary.is_available() is False
    with pytest.raises(BackupSkippedError):
        await missing_binary.upload(local, "race/a.f1rec.zst")


async def test_rclone_transport_copies_to_gcs_object_with_copyto(
    tmp_path: Path,
) -> None:
    local = tmp_path / "a.f1rec.zst"
    local.write_bytes(b"zstd")
    calls: list[list[str]] = []
    config = RecordingGcsConfig(
        enabled=True,
        rclone_remote="gcs",
        bucket="f1-race-engineer",
        prefix="/telemetry-raw/",
        rclone_config="~/.config/rclone/rclone.conf",
    )
    transport = RcloneTransport(
        config, which=lambda name: "/usr/bin/rclone", runner=calls.append
    )

    await transport.upload(local, "race/20261115-120000_t1_123_s1.f1rec.zst")

    assert transport.is_available() is True
    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "/usr/bin/rclone"
    assert args[1] == "copyto"
    assert args[-2] == str(local)
    assert (
        args[-1]
        == "gcs:f1-race-engineer/telemetry-raw/race/"
        "20261115-120000_t1_123_s1.f1rec.zst"
    )
    config_index = args.index("--config")
    assert args[config_index + 1] == str(
        Path("~/.config/rclone/rclone.conf").expanduser()
    )

    calls.clear()
    target = tmp_path / "downloads" / "out.f1rec.zst"
    await transport.download("race/20261115-120000_t1_123_s1.f1rec.zst", target)
    assert calls[0][-2] == (
        "gcs:f1-race-engineer/telemetry-raw/race/"
        "20261115-120000_t1_123_s1.f1rec.zst"
    )
    assert calls[0][-1] == str(target)
    assert target.parent.is_dir()


def test_rclone_transport_rejects_escaping_object_path(tmp_path: Path) -> None:
    transport = RcloneTransport(
        RecordingGcsConfig(enabled=True, bucket="bucket"),
        which=lambda name: "/usr/bin/rclone",
        runner=_failing_runner,
    )

    for bad in ("", "/", "..", "race/../../etc/passwd"):
        with pytest.raises(BackupError):
            transport.remote_path(bad)


async def test_rclone_transport_failure_does_not_leak_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실패 메시지에는 rclone stderr 요약만 들어가고 환경변수 값은 새지 않는다."""
    monkeypatch.setenv("F1RE_FAKE_RCLONE_TOKEN", "fake-secret-value")
    local = tmp_path / "a.f1rec.zst"
    local.write_bytes(b"zstd")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="ERROR : failed to open config"
        )

    monkeypatch.setattr(gcs_transfer.subprocess, "run", fake_run)
    transport = RcloneTransport(
        RecordingGcsConfig(enabled=True, bucket="bucket"),
        which=lambda name: "/usr/bin/rclone",
    )

    with pytest.raises(BackupError) as excinfo:
        await transport.upload(local, "race/a.f1rec.zst")

    message = str(excinfo.value)
    assert "failed to open config" in message
    assert "fake-secret-value" not in message
