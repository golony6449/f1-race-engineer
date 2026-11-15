"""원본 UDP 녹화 파일 읽기·쓰기와 리플레이.

파일 형식(`.f1rec`)은 헤더 없이 다음 레코드를 반복한다. 모두 리틀 엔디언이다.

    [u64 recv_ns][u16 len][len바이트 데이터그램]

수신 시각을 그대로 저장하므로 리플레이는 녹화 당시 간격을 재생 속도 배율로
나눠 흘려보낸다. 수신 루프가 어떤 바이트를 받았든 그대로 저장하므로, 게임
없이 같은 파이프라인(파싱 → 상태 → 사건)을 다시 돌려 볼 수 있다.

세션이 마감되면 같은 레코드열을 표준 라이브러리 `compression.zstd`로
스트리밍 압축해 `.f1rec.zst`로 남긴다. 읽기(`iter_recording`, `replay`,
`scan_recording`)는 확장자나 zstd 매직 바이트로 압축 여부를 자동 판별하므로
호출자는 어느 쪽 파일인지 신경 쓰지 않아도 된다.
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import time
from collections.abc import Callable, Iterator
from compression import zstd
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# 녹화 레코드 헤더: 수신 시각(ns), 데이터그램 길이.
RECORD_HEADER = struct.Struct("<QH")
RECORD_SUFFIX = ".f1rec"
ZST_SUFFIX = ".f1rec.zst"
# zstd 프레임 매직 바이트(RFC 8878). 확장자가 없어도 압축 파일임을 알아낸다.
ZST_MAGIC = b"\x28\xb5\x2f\xfd"


@dataclass(frozen=True)
class FinalizeJob:
    """마감된 활성 녹화 파일을 백업 워커(`BackupWorker`)에 넘기는 작업 단위.

    `recording_id`는 `session_recordings` 행, `active_path`는 아직 압축되지
    않은 `.active/<uid>_s<segment>.f1rec` 경로다. 압축·업로드·DB 갱신은
    이 작업을 받은 워커가 비동기로 처리한다.
    """

    recording_id: int
    active_path: Path
    session_uid: int
    segment: int
    end_reason: str
    ended_at_ns: int
    category: str
    session_type: int | None
    track_id: int | None


class RecordingWriter:
    """받은 데이터그램을 수신 시각과 함께 `.f1rec` 파일에 쓴다."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("wb")
        self._count = 0
        self._closed = False

    @property
    def record_count(self) -> int:
        return self._count

    def write(self, recv_ns: int, data: bytes) -> None:
        if self._closed:
            raise ValueError("닫힌 녹화 파일에는 쓸 수 없습니다")
        if not 0 <= recv_ns < 2**64:
            raise ValueError("recv_ns는 u64 범위여야 합니다")
        if len(data) > 0xFFFF:
            raise ValueError("데이터그램이 너무 큽니다(65535바이트 초과)")
        self._file.write(RECORD_HEADER.pack(recv_ns, len(data)))
        self._file.write(data)
        self._count += 1

    def flush(self) -> None:
        if not self._closed:
            self._file.flush()

    def close(self) -> None:
        if not self._closed:
            self._file.close()
            self._closed = True

    def __enter__(self) -> RecordingWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    """`size`바이트를 최대한 읽는다. 파일 끝이면 그보다 짧은 바이트열을 낸다."""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _iter_records(stream: BinaryIO) -> Iterator[tuple[int, bytes]]:
    """이진 스트림에서 완전한 레코드만 (수신 시각, 데이터그램)으로 낸다."""
    while True:
        header = _read_exact(stream, RECORD_HEADER.size)
        if len(header) < RECORD_HEADER.size:
            return
        recv_ns, length = RECORD_HEADER.unpack(header)
        data = _read_exact(stream, length)
        if len(data) < length:
            return
        yield recv_ns, data


@contextmanager
def _open_recording(path: str | Path) -> Iterator[BinaryIO]:
    """녹화 파일을 연다. `.f1rec.zst`이거나 zstd 매직으로 시작하면 풀어서 준다."""
    file_path = Path(path)
    raw = file_path.open("rb")
    try:
        magic = raw.read(len(ZST_MAGIC))
        raw.seek(0)
        if file_path.suffix == ".zst" or magic == ZST_MAGIC:
            stream = zstd.open(raw, "rb")
            try:
                yield stream
            finally:
                # 잘린 스트림은 닫을 때도 EOFError가 날 수 있다. 읽을 때 이미
                # 완전한 레코드까지만 냈으므로 닫기 오류는 삼킨다.
                try:
                    stream.close()
                except (zstd.ZstdError, EOFError):
                    pass
        else:
            yield raw
    finally:
        raw.close()


def iter_recording(path: str | Path) -> Iterator[tuple[int, bytes]]:
    """녹화 파일의 (수신 시각, 데이터그램)을 파일 순서대로 낸다.

    `.f1rec`와 `.f1rec.zst`를 확장자 또는 zstd 매직 바이트로 자동 판별한다.
    마지막 레코드가 잘려 있으면(프로세스가 쓰는 중 종료 등) 거기서 멈춘다.
    압축 스트림이 잘린 경우에도 이미 풀어낸 완전 레코드까지만 내고 조용히
    끝난다(비정상 종료 복구 경로가 예외로 멈추지 않게).
    """
    try:
        with _open_recording(path) as stream:
            yield from _iter_records(stream)
    except (zstd.ZstdError, EOFError):
        return


def compress_recording(src: str | Path, dst: str | Path) -> int:
    """녹화 파일을 `.f1rec.zst`로 스트리밍 압축하고 압축 파일 크기를 낸다.

    원본에서 완전한 레코드만 다시 쓰므로, 프로세스가 쓰다 만 잘린 끝 레코드는
    압축 결과에 남지 않는다. 임시 파일에 쓴 뒤 `os.replace`로 바꿔 끼우므로
    압축 도중 실패해도 기존 `dst`가 반쯤 덮이지 않는다. 원본 `src`는 지우지
    않는다(압축 후 원본 삭제는 백업 워커의 몫).
    """
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with _open_recording(source) as stream, zstd.open(tmp_path, "wb") as compressed:
            for recv_ns, data in _iter_records(stream):
                compressed.write(RECORD_HEADER.pack(recv_ns, len(data)))
                compressed.write(data)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target.stat().st_size


def scan_recording(path: str | Path) -> tuple[int, int]:
    """녹화 파일의 (데이터그램 수, 첫~마지막 패킷 길이 ms)을 센다.

    잘린 끝 레코드는 세지 않는다. 레코드가 하나도 없으면 `(0, 0)`이다.
    """
    count = 0
    first_ns: int | None = None
    last_ns = 0
    for recv_ns, _data in iter_recording(path):
        if first_ns is None:
            first_ns = recv_ns
        last_ns = recv_ns
        count += 1
    if first_ns is None:
        return 0, 0
    return count, max(0, last_ns - first_ns) // 1_000_000


def replay(
    path: str | Path,
    sink: Callable[[int, bytes], None],
    speed: float = 1.0,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """녹화를 `sink(recv_ns, data)`로 순서대로 흘려보낸다.

    `.f1rec`와 `.f1rec.zst` 모두에서 동작한다.
    `speed`가 1이면 녹화 당시 간격대로, 10이면 10배 빠르게 보낸다. 반환값은
    흘려보낸 데이터그램 수다. `clock`·`sleep`은 테스트에서 시간을 대신하기
    위한 것이다.
    """
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("speed는 0보다 큰 유한한 값이어야 합니다")

    started = clock()
    first_ns: int | None = None
    count = 0
    for recv_ns, data in iter_recording(path):
        if first_ns is None:
            first_ns = recv_ns
        else:
            target = started + (recv_ns - first_ns) / 1_000_000_000.0 / speed
            wait = target - clock()
            if wait > 0:
                sleep(wait)
        sink(recv_ns, data)
        count += 1
    return count


def new_recording_path(directory: str | Path, *, now_ns: int | None = None) -> Path:
    """`--record DIR`에 쓸 새 녹화 파일 경로를 만든다. 폴더도 만든다."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    ns = time.time_ns() if now_ns is None else now_ns
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ns / 1_000_000_000.0))
    return folder / f"f1rec-{stamp}-{ns % 1_000_000_000:09d}{RECORD_SUFFIX}"
