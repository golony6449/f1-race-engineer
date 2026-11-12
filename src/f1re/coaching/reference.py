"""기준 랩 선택과 코너 시간 손실(작업 15, SCRUM-65).

설계 6.3대로 기준 랩은 **같은 조건의 내 최고 유효 랩**이다. AI 상대 세션이면
**세션에서 가장 빠른 차의 최고 랩**도 후보로 두고, 코너마다 둘 중 그 코너 구간
시간이 더 짧은 쪽을 그 코너의 기준으로 쓴다(`ReferenceSet.best_for`).

- 무효 랩(`LapTrace.valid is False`)은 어떤 경우에도 기준이 되지 않는다
  (리뷰 포커스 4: 아웃랩·피트·무효·SC/VSC·레이스 1랩).
- 가장 빠른 차는 "다른 차 중 전체 랩 시간이 가장 짧은 차" 한 대다. 그 차의 최고
  랩만 후보가 된다(다른 차들의 코너별 최고를 모아 쓰지 않는다).
- 가장 빠른 차와 비교할 때 쓸 수 있는 값은 양쪽에 다 있는 위치·속도·페달·조향
  뿐이다. 휠 슬립처럼 내 차에만 있는 값은 `CornerMetrics.exit_wheel_slip`이
  다른 차에서 `None`이므로 자연히 내 최고 랩과만 비교된다.

조건(모드·날씨·차)별 SQLite 저장과 조건이 다를 때 시간 차이를 말하지 않는 규칙은
계획 작업 19에서 이 모듈 위에 얹는다. 이 작업은 세션 안의 기준 선택만 한다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from f1re.coaching.metrics import corner_duration_ms
from f1re.laps.traces import LapTrace
from f1re.tracks.model import Corner
from f1re.tracks.outline import TrackOutline

# `ReferenceSet.best_for`가 돌려주는 기준 출처.
SOURCE_SELF = "self"
SOURCE_FASTEST_CAR = "fastest_car"


class ReferenceSet:
    """한 세션에서 들어온 유효 랩들로 기준 랩 후보를 고른다.

    `corners`는 이 서킷의 코너 목록이다(공식 데이터 또는 자동 탐지 결과).
    `player_car_index`는 내 차 인덱스이며, 그 차의 랩이 "내 최고 랩" 후보가 된다.
    세션이 바뀌면(sessionUID 변경) 새 `ReferenceSet`을 만들어 쓴다.
    """

    def __init__(
        self,
        corners: Sequence[Corner],
        *,
        player_car_index: int = 0,
    ) -> None:
        self._corners = {corner.number: corner for corner in corners}
        self._player_car_index = player_car_index
        self._laps: list[LapTrace] = []

    def update(self, trace: LapTrace) -> None:
        """완료된 랩 하나를 넣는다. 무효 랩과 샘플이 모자란 랩은 버린다."""
        if not trace.valid or len(trace.samples) < 2:
            return
        self._laps.append(trace)

    def best_for(self, corner: int) -> tuple[LapTrace, str] | None:
        """코너 하나의 기준 랩과 출처를 돌려준다. 기준이 없으면 `None`.

        출처는 `"self"`(내 최고 유효 랩) 또는 `"fastest_car"`(세션 최속차의 최고
        랩)다. 두 후보의 그 코너 구간 시간이 같으면 내 랩을 쓴다.
        """
        corner_def = self._corners.get(corner)
        if corner_def is None:
            return None

        candidates: list[tuple[LapTrace, str]] = []
        self_best = self._best_lap(self._player_car_index)
        if self_best is not None:
            candidates.append((self_best, SOURCE_SELF))
        fastest_trace = self._fastest_car_best_lap()
        if fastest_trace is not None:
            candidates.append((fastest_trace, SOURCE_FASTEST_CAR))
        if not candidates:
            return None

        return min(
            candidates,
            key=lambda item: corner_duration_ms(item[0], corner_def),
        )

    def best_lap(self, source: str) -> LapTrace | None:
        """출처별 최고 랩. 없으면 `None`."""
        if source == SOURCE_SELF:
            return self._best_lap(self._player_car_index)
        if source == SOURCE_FASTEST_CAR:
            return self._fastest_car_best_lap()
        raise ValueError(f"알 수 없는 기준 출처입니다: {source}")

    # ------------------------------------------------------------------
    # 후보 선택

    def _best_lap(self, car_index: int) -> LapTrace | None:
        laps = [trace for trace in self._laps if trace.car_index == car_index]
        if not laps:
            return None
        return min(laps, key=_lap_time_ms)

    def _fastest_car_best_lap(self) -> LapTrace | None:
        """다른 차 중 전체 랩이 가장 빠른 차 한 대의 최고 랩."""
        best_by_car: dict[int, LapTrace] = {}
        for trace in self._laps:
            if trace.car_index == self._player_car_index:
                continue
            previous = best_by_car.get(trace.car_index)
            if previous is None or _lap_time_ms(trace) < _lap_time_ms(previous):
                best_by_car[trace.car_index] = trace
        if not best_by_car:
            return None
        fastest_car = min(best_by_car, key=lambda car: _lap_time_ms(best_by_car[car]))
        return best_by_car[fastest_car]


def _lap_time_ms(trace: LapTrace) -> float:
    """랩 전체 시간(ms). 랩 시작 기준 마지막 샘플 시각을 쓴다."""
    return float(max(sample.t_ms for sample in trace.samples))


def corner_time_loss_ms(trace: LapTrace, ref: LapTrace, corner: Corner) -> int:
    """코너 구간(진입~탈출) 시간 손실(ms). 느리면 양수, 빠르면 음수.

    두 랩 모두 랩 거리 기준으로 시각을 보간해 같은 구간을 비교한다.
    """
    trace_time = corner_duration_ms(trace, corner)
    ref_time = corner_duration_ms(ref, corner)
    return int(round(trace_time - ref_time))


def trace_outline(trace: LapTrace) -> TrackOutline | None:
    """랩 샘플의 위치를 기준 궤적(`TrackOutline`)으로 만든다.

    기준 랩의 라인과 비교할 때 `corner_metrics(..., ref_path=trace_outline(ref))`
    처럼 쓴다. 위치가 없거나 점이 2개보다 적으면 `None`.
    """
    points: list[tuple[float, float, float]] = []
    previous_dist = -float("inf")
    for sample in sorted(trace.samples, key=lambda item: item.dist_m):
        if not all(
            math.isfinite(value) for value in (sample.dist_m, sample.x, sample.z)
        ):
            continue
        if sample.dist_m <= previous_dist:
            continue
        points.append((sample.dist_m, sample.x, sample.z))
        previous_dist = sample.dist_m
    if len(points) < 2:
        return None
    return TrackOutline(points)


__all__ = [
    "SOURCE_FASTEST_CAR",
    "SOURCE_SELF",
    "ReferenceSet",
    "corner_time_loss_ms",
    "trace_outline",
]
