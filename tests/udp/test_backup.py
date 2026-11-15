"""BackupWorker 비동기 압축·업로드·로컬 5개 보존·기동 복구(설계 4.2)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from f1re import db
from f1re.config import RecordingConfig, RecordingGcsConfig
from f1re.udp.backup import BackupWorker, RcloneUploadTransport
from f1re.udp.recording import (
    RECORD_HEADER,
    FinalizeJob,
    RecordingWriter,
    compress_recording,
    scan_recording,
)

_PACKET = b"\x01\x02\x03\x04"
_STEP_NS = 16_666_667


class FakeTransport:
    """실제 rclone·GCS·네트워크를 부르지 않는 업로더."""

    def __init__(self, *, available: bool = True, failures: int = 0) -> None:
        self.available = available
        self.failures = failures
        self.uploads: list[tuple[Path, str]] = []

    def is_available(self) -> bool:
        return self.available

    async def upload(self, local_path: Path, remote_object: str) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("가짜 업로드 실패")
        self.uploads.append((Path(local_path), remote_object))


def _config(tmp_path: Path, **overrides: object) -> RecordingConfig:
    values: dict[str, object] = {
        "dir": str(tmp_path / "recordings"),
        "keep_local_count": 5,
        "min_packets": 100,
        "gcs": RecordingGcsConfig(
            enabled=True, bucket="test-bucket", prefix="recordings"
        ),
    }
    values.update(overrides)
    return RecordingConfig(**values)  # type: ignore[arg-type]


def _write_active(
    root: Path,
    session_uid: int,
    segment: int,
    count: int,
    *,
    start_ns: int = 1_000_000_000,
) -> Path:
    path = root / ".active" / f"{session_uid}_s{segment}.f1rec"
    with RecordingWriter(path) as writer:
        for index in range(count):
            writer.write(start_ns + index * _STEP_NS, _PACKET)
    return path


def _pending_job(
    conn,
    root: Path,
    *,
    session_uid: int,
    segment: int = 1,
    count: int = 120,
    category: str = "race",
    track_id: int | None = 1,
    started_at_ns: int = 1_000_000_000,
    end_reason: str = "session_end",
) -> FinalizeJob:
    recording_id = db.create_recording(
        conn,
        session_uid=session_uid,
        segment=segment,
        started_at_ns=started_at_ns,
        category=category,
        session_type=10,
        track_id=track_id,
    )
    active_path = _write_active(
        root, session_uid, segment, count, start_ns=started_at_ns
    )
    return FinalizeJob(
        recording_id=recording_id,
        active_path=active_path,
        session_uid=session_uid,
        segment=segment,
        end_reason=end_reason,
        ended_at_ns=started_at_ns + count * _STEP_NS,
        category=category,
        session_type=10,
        track_id=track_id,
    )


async def test_backup_worker_compresses_uploads_and_keeps_latest_5(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    transport = FakeTransport()
    worker = BackupWorker(db_path, _config(tmp_path), transport=transport)
    try:
        await worker.start()
        for index in range(7):
            worker.enqueue(
                _pending_job(
                    conn,
                    root,
                    session_uid=1000 + index,
                    started_at_ns=1_000_000_000 + index * 3_600_000_000_000,
                )
            )
        await asyncio.wait_for(worker.join(), 10)

        rows = db.list_recordings(conn)
        assert len(rows) == 7
        assert all(row["backup_status"] == "uploaded" for row in rows)
        assert all(row["end_reason"] == "session_end" for row in rows)
        assert all(row["packet_count"] == 120 for row in rows)

        # 원격 객체는 `<범주>/<파일명>`이고 7개 모두 서로 다른 이름으로 올라갔다.
        assert len(transport.uploads) == 7
        assert all(
            remote.startswith("race/") and remote.endswith(".f1rec.zst")
            for _path, remote in transport.uploads
        )
        assert len({remote for _path, remote in transport.uploads}) == 7

        # 압축 파일은 잘린 레코드 없이 원본 시퀀스를 그대로 담는다.
        newest = rows[0]
        compressed = Path(newest["local_path"])
        count, duration_ms = scan_recording(compressed)
        assert count == 120
        assert duration_ms == (120 - 1) * _STEP_NS // 1_000_000

        # 로컬에는 최신 5개만 남고 나머지는 DB에서도 local_exists = 0이 된다.
        newest_ids = {row["id"] for row in rows[:5]}
        for row in rows:
            path = Path(row["local_path"])
            if row["id"] in newest_ids:
                assert row["local_exists"] == 1
                assert path.is_file()
            else:
                assert row["local_exists"] == 0
                assert not path.exists()
        assert list((root / ".active").glob("*.f1rec")) == []
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_keeps_local_file_on_upload_failure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    transport = FakeTransport(failures=1)
    worker = BackupWorker(db_path, _config(tmp_path), transport=transport)
    try:
        await worker.start()
        job = _pending_job(conn, root, session_uid=2001)
        worker.enqueue(job)
        await asyncio.wait_for(worker.join(), 10)

        row = db.get_recording(conn, job.recording_id)
        assert row is not None
        assert row["backup_status"] == "failed"
        assert row["local_exists"] == 1
        assert "가짜 업로드 실패" in row["backup_error"]
        assert transport.uploads == []
        compressed = Path(row["local_path"])
        assert compressed.is_file()
        assert not job.active_path.exists()

        # 수동 재시도가 성공하면 uploaded가 되고 로컬 파일도 유지된다.
        result = await worker.retry(job.recording_id)
        assert result["backup_status"] == "uploaded"
        assert compressed.is_file()
        updated = db.get_recording(conn, job.recording_id)
        assert updated["backup_status"] == "uploaded"
        assert updated["backup_error"] is None
        assert len(transport.uploads) == 1
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_protects_active_replay_recording(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    playing: dict[str, int | None] = {"id": None}
    worker = BackupWorker(
        db_path,
        _config(tmp_path, keep_local_count=2),
        transport=FakeTransport(),
        active_replay_id=lambda: playing["id"],
    )
    try:
        await worker.start()
        jobs = [
            _pending_job(
                conn,
                root,
                session_uid=3000 + index,
                started_at_ns=1_000_000_000 + index * 3_600_000_000_000,
            )
            for index in range(4)
        ]
        # 가장 오래된 녹화를 지금 웹 리플레이가 재생 중이다.
        playing["id"] = jobs[0].recording_id
        for job in jobs:
            worker.enqueue(job)
        await asyncio.wait_for(worker.join(), 10)

        rows = db.list_recordings(conn)
        assert all(row["backup_status"] == "uploaded" for row in rows)
        protected = db.get_recording(conn, jobs[0].recording_id)
        assert protected["local_exists"] == 1
        assert Path(protected["local_path"]).is_file()

        # 재생 중 1개 + 최신 2개만 남고, 두 번째로 오래된 것만 지워진다.
        assert sum(row["local_exists"] for row in rows) == 3
        second = db.get_recording(conn, jobs[1].recording_id)
        assert second["local_exists"] == 0
        assert not Path(second["local_path"]).exists()
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_recovers_active_f1rec_on_startup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    path = _write_active(root, 12345, 1, 150)
    # 프로세스가 쓰다 만 마지막 레코드(헤더 + 모자란 데이터)를 붙인다.
    with path.open("ab") as stream:
        stream.write(RECORD_HEADER.pack(2_000_000_000, 10) + b"abc")
    transport = FakeTransport()
    worker = BackupWorker(db_path, _config(tmp_path), transport=transport)
    try:
        await worker.start()
        await asyncio.wait_for(worker.join(), 10)

        rows = db.list_recordings(conn)
        assert len(rows) == 1
        row = rows[0]
        assert row["session_uid"] == 12345
        assert row["segment"] == 1
        assert row["end_reason"] == "recovered"
        assert row["packet_count"] == 150
        assert row["backup_status"] == "uploaded"
        assert row["local_exists"] == 1
        assert not path.exists()

        compressed = Path(row["local_path"])
        assert compressed.is_file()
        # 손상된 끝 레코드는 압축 결과에도 남지 않는다.
        assert scan_recording(compressed)[0] == 150
        assert len(transport.uploads) == 1
        assert transport.uploads[0][1].startswith("unknown/")
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_discards_under_min_packets(tmp_path: Path) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    transport = FakeTransport()
    worker = BackupWorker(db_path, _config(tmp_path), transport=transport)
    try:
        await worker.start()
        job = _pending_job(conn, root, session_uid=4001, count=30)
        worker.enqueue(job)
        await asyncio.wait_for(worker.join(), 10)

        assert db.get_recording(conn, job.recording_id) is None
        assert not job.active_path.exists()
        assert transport.uploads == []
        assert list(root.glob("*/*.f1rec.zst")) == []
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_processes_job_enqueued_before_start(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    transport = FakeTransport()
    worker = BackupWorker(db_path, _config(tmp_path), transport=transport)
    try:
        job = _pending_job(conn, root, session_uid=4501)
        # 엔진이 워커 기동 전에 마감 작업을 넘겨도 잃지 않는다.
        worker.enqueue(job)
        await worker.start()
        await asyncio.wait_for(worker.join(), 10)

        row = db.get_recording(conn, job.recording_id)
        assert row["backup_status"] == "uploaded"
        assert len(transport.uploads) == 1
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_discards_empty_active_file_on_startup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    path = root / ".active" / "9999_s1.f1rec"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    transport = FakeTransport()
    worker = BackupWorker(db_path, _config(tmp_path), transport=transport)
    try:
        await worker.start()
        await asyncio.wait_for(worker.join(), 10)

        assert not path.exists()
        assert db.list_recordings(conn) == []
        assert transport.uploads == []
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_retries_pending_recording_on_startup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    active = _write_active(root, 5001, 1, 150)
    target = root / "race" / "5001_s1.f1rec.zst"
    compress_recording(active, target)
    active.unlink()
    recording_id = db.create_recording(
        conn,
        session_uid=5001,
        segment=1,
        started_at_ns=1_000_000_000,
        category="race",
        track_id=3,
    )
    db.finalize_recording(
        conn,
        recording_id,
        end_reason="session_end",
        ended_at_ns=1_000_000_000 + 150 * _STEP_NS,
        packet_count=150,
        duration_ms=(150 - 1) * _STEP_NS // 1_000_000,
        local_path=str(target),
        local_exists=True,
        file_size_bytes=target.stat().st_size,
        backup_status="pending",
    )
    transport = FakeTransport()
    worker = BackupWorker(db_path, _config(tmp_path), transport=transport)
    try:
        await worker.start()
        await asyncio.wait_for(worker.join(), 10)

        row = db.get_recording(conn, recording_id)
        assert row["backup_status"] == "uploaded"
        assert row["gcs_object"] == "race/5001_s1.f1rec.zst"
        assert target.is_file()
        assert transport.uploads == [(target, "race/5001_s1.f1rec.zst")]
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_skips_upload_when_gcs_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    transport = FakeTransport()
    worker = BackupWorker(
        db_path,
        _config(tmp_path, gcs=RecordingGcsConfig(enabled=False)),
        transport=transport,
    )
    try:
        await worker.start()
        job = _pending_job(conn, root, session_uid=6001)
        worker.enqueue(job)
        await asyncio.wait_for(worker.join(), 10)

        row = db.get_recording(conn, job.recording_id)
        assert row["backup_status"] == "skipped"
        assert row["local_exists"] == 1
        assert Path(row["local_path"]).is_file()
        assert transport.uploads == []
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_skips_when_transport_unavailable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    worker = BackupWorker(
        db_path, _config(tmp_path), transport=FakeTransport(available=False)
    )
    try:
        await worker.start()
        job = _pending_job(conn, root, session_uid=7001)
        worker.enqueue(job)
        await asyncio.wait_for(worker.join(), 10)

        row = db.get_recording(conn, job.recording_id)
        # rclone이 없어도 녹화와 로컬 파일은 그대로 보존된다.
        assert row["backup_status"] == "skipped"
        assert row["local_exists"] == 1
        assert Path(row["local_path"]).is_file()
    finally:
        await worker.aclose()
        conn.close()


async def test_backup_worker_periodically_retries_failed_upload(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "f1re.db"
    conn = db.open_db(db_path)
    root = tmp_path / "recordings"
    transport = FakeTransport(failures=1)
    worker = BackupWorker(
        db_path,
        _config(tmp_path),
        transport=transport,
        retry_interval_s=0.05,
    )
    try:
        await worker.start()
        job = _pending_job(conn, root, session_uid=8001)
        worker.enqueue(job)
        deadline = time.monotonic() + 5
        row = None
        while time.monotonic() < deadline:
            row = db.get_recording(conn, job.recording_id)
            if row is not None and row["backup_status"] == "uploaded":
                break
            await asyncio.sleep(0.02)

        assert row is not None
        assert row["backup_status"] == "uploaded"
        assert len(transport.uploads) == 1
    finally:
        await worker.aclose()
        conn.close()


async def test_retry_unknown_recording_returns_error(tmp_path: Path) -> None:
    db_path = tmp_path / "f1re.db"
    worker = BackupWorker(
        db_path, _config(tmp_path), transport=FakeTransport()
    )
    try:
        await worker.start()
        result = await worker.retry(999)

        assert result["recording_id"] == 999
        assert result["backup_status"] is None
        assert result["error"] == "녹화를 찾을 수 없습니다"
    finally:
        await worker.aclose()


def test_rclone_transport_requires_binary_bucket_and_config(tmp_path: Path) -> None:
    config_path = tmp_path / "rclone.conf"
    gcs = RecordingGcsConfig(
        enabled=True,
        bucket="test-bucket",
        prefix="recordings",
        rclone_config=str(config_path),
    )
    missing_config = RcloneUploadTransport(
        gcs, which=lambda _name: "/usr/bin/rclone"
    )
    assert missing_config.is_available() is False

    config_path.write_text("[test]\ntype = local\n", encoding="utf-8")
    transport = RcloneUploadTransport(gcs, which=lambda _name: "/usr/bin/rclone")
    assert transport.is_available() is True
    assert (
        transport.remote_path("race/a.f1rec.zst")
        == "gcs:test-bucket/recordings/race/a.f1rec.zst"
    )

    no_binary = RcloneUploadTransport(gcs, which=lambda _name: None)
    assert no_binary.is_available() is False
    empty_bucket = RcloneUploadTransport(
        RecordingGcsConfig(enabled=True, bucket="", rclone_config=str(config_path)),
        which=lambda _name: "/usr/bin/rclone",
    )
    assert empty_bucket.is_available() is False
    disabled = RcloneUploadTransport(
        RecordingGcsConfig(
            enabled=False, bucket="test-bucket", rclone_config=str(config_path)
        ),
        which=lambda _name: "/usr/bin/rclone",
    )
    assert disabled.is_available() is False
