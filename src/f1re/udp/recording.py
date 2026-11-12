"""원본 UDP 녹화 파일 읽기·쓰기와 리플레이.

파일 형식(`.f1rec`)은 헤더 없이 다음 레코드를 반복한다. 모두 리틀 엔디언이다.

    [u64 recv_ns][u16 len][len바이트 데이터그램]

수신 시각을 그대로 저장하므로 리플레이는 녹화 당시 간격을 재생 속도 배율로
나눠 흘려보낸다. 수신 루프가 어떤 바이트를 받았든 그대로 저장하므로, 게임
없이 같은 파이프라인(파싱 → 상태 → 사건)을 다시 돌려 볼 수 있다.
"""

from __future__ import annotations

import math
import struct
import time
from collections.abc import Callable, Iterator
from pathlib import Path

# 녹화 레코드 헤더: 수신 시각(ns), 데이터그램 길이.
RECORD_HEADER = struct.Struct("<QH")
RECORD_SUFFIX = ".f1rec"


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


def iter_recording(path: str | Path) -> Iterator[tuple[int, bytes]]:
    """녹화 파일의 (수신 시각, 데이터그램)을 파일 순서대로 낸다.

    마지막 레코드가 잘려 있으면(프로세스가 쓰는 중 종료 등) 거기서 멈춘다.
    """
    with Path(path).open("rb") as file:
        while True:
            header = file.read(RECORD_HEADER.size)
            if len(header) < RECORD_HEADER.size:
                return
            recv_ns, length = RECORD_HEADER.unpack(header)
            data = file.read(length)
            if len(data) < length:
                return
            yield recv_ns, data


def replay(
    path: str | Path,
    sink: Callable[[int, bytes], None],
    speed: float = 1.0,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """녹화를 `sink(recv_ns, data)`로 순서대로 흘려보낸다.

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
