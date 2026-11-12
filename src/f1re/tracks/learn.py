"""서킷 윤곽 학습과 서킷별 저장(설계 6.9-2, 리뷰 포커스 5).

데이터 파일(`tracks/data/*.json`)이 있는 서킷은 그 중심선을 쓴다. 처음 오는
서킷은 내 첫 유효 랩의 위치 샘플로 윤곽을 만들고(`learn_outline`), 서킷 ID로
SQLite에 저장해(`remember_outline`) 다음 랩·다음 방문에서 다시 쓴다
(`resolve_outline`).

- 무효 랩은 윤곽을 배우지 않는다(리뷰 포커스 4). `learn_outline`은 예외를 내고,
  수신 루프에서 부르는 `remember_outline`은 `None`을 돌려주고 넘어간다.
- 점은 랩 거리 순이며 `LEARN_STEP_M` 간격으로 다시 샘플링한다. 60Hz 원본을
  그대로 저장하면 한 랩에 수천 점이라 DB·브라우저 전송이 무거워진다.
"""

from __future__ import annotations

import math
import sqlite3
import time
from typing import TYPE_CHECKING

from f1re.db import load_track_outline, save_track_outline
from f1re.tracks.model import TrackDataError, load_outline
from f1re.tracks.outline import TrackOutline

if TYPE_CHECKING:
    from f1re.laps.traces import LapTrace

# 학습 윤곽의 점 간격(m). 곡선이 뚜렷한 코너에서도 지도가 매끄럽게 보이는 값.
LEARN_STEP_M = 5.0

# 윤곽으로 쓸 수 있는 최소 점 수. 두 점은 선분 하나라 지도가 되지 않는다.
MIN_OUTLINE_POINTS = 3

# 같은 거리의 샘플을 버릴 때 쓰는 부동소수 여유.
_DIST_EPS = 1e-6


def learn_outline(trace: LapTrace) -> TrackOutline:
    """유효 랩의 위치 샘플로 랩 거리 순 트랙 윤곽을 만든다.

    샘플의 `(dist_m, x, z)`를 랩 거리 순으로 정렬하고 `LEARN_STEP_M` 간격으로
    선형 보간해 다시 샘플링한다. 마지막 점에서 첫 점으로 닫는 변은
    `TrackOutline`이 처리한다.

    무효 랩이거나 위치가 유한한 샘플이 모자라면 `ValueError`.
    """
    if not trace.valid:
        raise ValueError("무효 랩으로는 서킷 윤곽을 배울 수 없습니다")

    positions: list[tuple[float, float, float]] = []
    for sample in trace.samples:
        if not all(
            math.isfinite(value) for value in (sample.dist_m, sample.x, sample.z)
        ):
            continue
        positions.append((float(sample.dist_m), float(sample.x), float(sample.z)))
    positions.sort(key=lambda point: point[0])

    # 같은 거리의 샘플은 먼저 온 것만 남긴다(TrackOutline은 거리가 엄격히 증가해야 한다).
    unique: list[tuple[float, float, float]] = []
    for point in positions:
        if unique and point[0] <= unique[-1][0] + _DIST_EPS:
            continue
        unique.append(point)
    if len(unique) < 2:
        raise ValueError("윤곽을 배울 위치 샘플이 모자랍니다")

    points = _resample(unique)
    if len(points) < MIN_OUTLINE_POINTS:
        raise ValueError(
            f"윤곽 점이 {len(points)}개뿐입니다(최소 {MIN_OUTLINE_POINTS}개)"
        )
    return TrackOutline(points)


def remember_outline(
    conn: sqlite3.Connection,
    track_id: int,
    trace: LapTrace,
    *,
    learned_at_ns: int | None = None,
) -> TrackOutline | None:
    """첫 유효 랩이면 윤곽을 배워 서킷 ID로 저장하고, 이미 있으면 저장본을 쓴다.

    수신 루프에서 랩이 완료될 때마다 불러도 된다. 무효 랩·샘플 부족·DB 오류는
    `None`으로 넘긴다(수신·코칭 파이프라인을 멈추지 않는다).
    """
    try:
        stored = load_track_outline(conn, track_id)
    except sqlite3.Error:
        return None
    if stored is not None:
        return stored

    try:
        outline = learn_outline(trace)
    except ValueError:
        return None
    try:
        save_track_outline(
            conn,
            track_id,
            outline,
            learned_at_ns=time.time_ns() if learned_at_ns is None else learned_at_ns,
        )
    except sqlite3.Error:
        return None
    return outline


def resolve_outline(
    track_id: int | None, conn: sqlite3.Connection | None = None
) -> tuple[TrackOutline | None, str]:
    """서킷 ID로 그릴 윤곽과 출처를 고른다.

    데이터 파일이 있으면 그것("data")을, 없으면 저장된 학습 윤곽("learned")을
    쓴다. 둘 다 없으면 `(None, "none")`이고 화면은 첫 유효 랩을 기다린다.
    """
    if track_id is None:
        return None, "none"
    try:
        outline = load_outline(track_id)
    except TrackDataError:
        outline = None
    if outline is not None:
        return outline, "data"
    if conn is not None:
        try:
            stored = load_track_outline(conn, track_id)
        except sqlite3.Error:
            stored = None
        if stored is not None:
            return stored, "learned"
    return None, "none"


def _resample(
    points: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """랩 거리 `LEARN_STEP_M` 간격으로 선형 보간한 점 목록."""
    first = points[0]
    last = points[-1]
    span = last[0] - first[0]
    count = int(span // LEARN_STEP_M)
    grid: list[tuple[float, float, float]] = []
    index = 0
    for step in range(count + 1):
        distance = first[0] + step * LEARN_STEP_M
        while index + 1 < len(points) and points[index + 1][0] <= distance:
            index += 1
        start = points[index]
        end = points[min(index + 1, len(points) - 1)]
        length = end[0] - start[0]
        fraction = 0.0 if length <= 0.0 else (distance - start[0]) / length
        grid.append(
            (
                distance,
                start[1] + fraction * (end[1] - start[1]),
                start[2] + fraction * (end[2] - start[2]),
            )
        )
    if last[0] - grid[-1][0] > _DIST_EPS:
        grid.append(last)
    return grid
