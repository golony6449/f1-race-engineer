"""`.f1rec` 녹화 왕복, `.f1rec.zst` 압축·투명 읽기, 손상 끝 스캔, 리플레이 배율."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from f1re.udp.recording import (
    RECORD_HEADER,
    ZST_MAGIC,
    ZST_SUFFIX,
    FinalizeJob,
    RecordingWriter,
    compress_recording,
    iter_recording,
    new_recording_path,
    replay,
    scan_recording,
)


def _write_recording(path: Path, records: list[tuple[int, bytes]]) -> None:
    with RecordingWriter(path) as writer:
        for recv_ns, data in records:
            writer.write(recv_ns, data)


def _sample_records() -> list[tuple[int, bytes]]:
    return [
        (1_000_000_000, b"\x01\x02"),
        (1_010_000_000, b"hello"),
        (1_030_000_000, bytes(100)),
    ]


def _noisy_records(count: int) -> list[tuple[int, bytes]]:
    """압축률이 낮은 결정적 데이터. 잘린 스트림에서 앞부분 복구를 확인한다."""
    records: list[tuple[int, bytes]] = []
    for index in range(count):
        payload = b"".join(
            hashlib.sha256(f"{index}-{part}".encode()).digest() for part in range(8)
        )
        records.append((index * 1_000_000, payload))
    return records


def test_recording_roundtrip_keeps_order_and_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "session.f1rec"
    records = [
        (1_000_000_000, b"\x01\x02"),
        (1_010_000_000, b"hello"),
        (1_030_000_000, bytes(100)),
    ]
    with RecordingWriter(path) as writer:
        for recv_ns, data in records:
            writer.write(recv_ns, data)
        assert writer.record_count == 3

    assert list(iter_recording(path)) == records


def test_recording_file_layout_is_little_endian(tmp_path: Path) -> None:
    path = tmp_path / "session.f1rec"
    with RecordingWriter(path) as writer:
        writer.write(0x0102_0304_0506_0708, b"abc")

    raw = path.read_bytes()
    recv_ns, length = RECORD_HEADER.unpack_from(raw)
    assert recv_ns == 0x0102_0304_0506_0708
    assert length == 3
    assert raw[RECORD_HEADER.size :] == b"abc"
    assert len(raw) == RECORD_HEADER.size + 3


def test_truncated_tail_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "session.f1rec"
    with RecordingWriter(path) as writer:
        writer.write(1_000, b"first")
        writer.write(2_000, b"second")
    raw = path.read_bytes()
    path.write_bytes(raw[:-3])  # 마지막 레코드 데이터가 잘린 상태

    assert list(iter_recording(path)) == [(1_000, b"first")]


def test_writer_validates_and_rejects_after_close(tmp_path: Path) -> None:
    path = tmp_path / "session.f1rec"
    writer = RecordingWriter(path)
    with pytest.raises(ValueError):
        writer.write(-1, b"x")
    with pytest.raises(ValueError):
        writer.write(1, b"x" * 65_536)
    writer.close()
    with pytest.raises(ValueError):
        writer.write(1, b"x")


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_replay_uses_recorded_gaps_and_speed(tmp_path: Path) -> None:
    path = tmp_path / "session.f1rec"
    with RecordingWriter(path) as writer:
        writer.write(0, b"a")
        writer.write(100_000_000, b"b")  # 0.1초 뒤
        writer.write(300_000_000, b"c")  # 첫 기록에서 0.3초 뒤

    clock = _FakeClock()
    received: list[bytes] = []
    count = replay(
        path,
        lambda recv_ns, data: received.append(data),
        speed=2.0,
        clock=clock,
        sleep=clock.sleep,
    )

    assert count == 3
    assert received == [b"a", b"b", b"c"]
    # 0.1초/2, (0.3-0.1)초/2 만큼 기다린다.
    assert clock.sleeps == pytest.approx([0.05, 0.1])


def test_replay_validates_speed(tmp_path: Path) -> None:
    path = tmp_path / "session.f1rec"
    with RecordingWriter(path):
        pass
    with pytest.raises(ValueError):
        replay(path, lambda recv_ns, data: None, speed=0)


def test_new_recording_path_creates_folder_and_suffix(tmp_path: Path) -> None:
    folder = tmp_path / "recordings"
    path = new_recording_path(folder, now_ns=1_700_000_000_123_456_789)

    assert folder.is_dir()
    assert path.parent == folder
    assert path.suffix == ".f1rec"
    assert path.name.startswith("f1rec-")


def test_compress_and_iter_zst_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "session.f1rec"
    records = _sample_records()
    _write_recording(src, records)

    dst = tmp_path / "race" / f"session{ZST_SUFFIX}"
    size = compress_recording(src, dst)

    assert src.read_bytes()  # 압축은 원본을 지우지 않는다
    assert dst.name.endswith(ZST_SUFFIX)
    assert dst.read_bytes()[: len(ZST_MAGIC)] == ZST_MAGIC
    assert size == dst.stat().st_size
    assert list(iter_recording(src)) == records
    assert list(iter_recording(dst)) == records


def test_iter_recording_detects_zstd_by_magic_without_suffix(tmp_path: Path) -> None:
    src = tmp_path / "session.f1rec"
    records = _sample_records()
    _write_recording(src, records)
    odd_name = tmp_path / "session.bin"
    compress_recording(src, odd_name)

    assert list(iter_recording(odd_name)) == records


def test_replay_reads_zst_recording(tmp_path: Path) -> None:
    src = tmp_path / "session.f1rec"
    _write_recording(
        src,
        [(0, b"a"), (100_000_000, b"b"), (300_000_000, b"c")],
    )
    dst = tmp_path / f"session{ZST_SUFFIX}"
    compress_recording(src, dst)

    clock = _FakeClock()
    received: list[bytes] = []
    count = replay(
        dst,
        lambda recv_ns, data: received.append(data),
        speed=2.0,
        clock=clock,
        sleep=clock.sleep,
    )

    assert count == 3
    assert received == [b"a", b"b", b"c"]
    assert clock.sleeps == pytest.approx([0.05, 0.1])


def test_scan_recording_ignores_truncated_tail(tmp_path: Path) -> None:
    path = tmp_path / "session.f1rec"
    _write_recording(
        path,
        [
            (1_000_000_000, b"first"),
            (1_500_000_000, b"second"),
            (9_000_000_000, b"third"),
        ],
    )
    raw = path.read_bytes()
    path.write_bytes(raw[:-5])  # 마지막 레코드가 잘린 상태

    assert scan_recording(path) == (2, 500)


def test_scan_recording_reads_zst_and_empty_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty.f1rec"
    empty.write_bytes(b"")
    assert scan_recording(empty) == (0, 0)

    empty_zst = tmp_path / f"empty{ZST_SUFFIX}"
    compress_recording(empty, empty_zst)
    assert scan_recording(empty_zst) == (0, 0)

    src = tmp_path / "session.f1rec"
    _write_recording(src, [(1_000_000_000, b"a"), (1_250_000_000, b"b")])
    zst = tmp_path / f"session{ZST_SUFFIX}"
    compress_recording(src, zst)
    assert scan_recording(zst) == (2, 250)


def test_compress_recording_drops_truncated_tail(tmp_path: Path) -> None:
    src = tmp_path / "crash.f1rec"
    _write_recording(src, [(1_000, b"first"), (2_000, b"second")])
    src.write_bytes(src.read_bytes()[:-3])

    dst = tmp_path / f"crash{ZST_SUFFIX}"
    compress_recording(src, dst)

    assert list(iter_recording(dst)) == [(1_000, b"first")]
    assert scan_recording(dst) == (1, 0)


def test_compress_recording_replaces_target_and_cleans_temp(tmp_path: Path) -> None:
    src = tmp_path / "session.f1rec"
    records = _sample_records()
    _write_recording(src, records)
    dst = tmp_path / f"session{ZST_SUFFIX}"
    dst.write_bytes(b"stale")

    compress_recording(src, dst)

    assert list(iter_recording(dst)) == records
    leftovers = [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == []


def test_iter_recording_ignores_truncated_zstd_stream(tmp_path: Path) -> None:
    src = tmp_path / "session.f1rec"
    records = _noisy_records(60)
    _write_recording(src, records)
    dst = tmp_path / f"session{ZST_SUFFIX}"
    compress_recording(src, dst)
    dst.write_bytes(dst.read_bytes()[:-5])  # 압축 스트림 끝이 잘린 상태

    recovered = list(iter_recording(dst))

    assert recovered  # 예외 없이 완전한 레코드까지만 낸다
    assert recovered == records[: len(recovered)]


def test_finalize_job_is_frozen(tmp_path: Path) -> None:
    job = FinalizeJob(
        recording_id=7,
        active_path=tmp_path / "12345_s2.f1rec",
        session_uid=12345,
        segment=2,
        end_reason="inactivity_timeout",
        ended_at_ns=1_800_000_000_000_000_000,
        category="race",
        session_type=15,
        track_id=7,
    )

    assert job.active_path == tmp_path / "12345_s2.f1rec"
    assert job.end_reason == "inactivity_timeout"
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.segment = 3  # type: ignore[misc]
