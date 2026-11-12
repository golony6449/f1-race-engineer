"""`.f1rec` 녹화 왕복과 리플레이 순서·속도 배율."""

from __future__ import annotations

from pathlib import Path

import pytest

from f1re.udp.recording import (
    RECORD_HEADER,
    RecordingWriter,
    iter_recording,
    new_recording_path,
    replay,
)


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
