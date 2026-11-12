"""집중 코너 선택과 개선 감지(작업 16, SCRUM-66).

설계 6.4의 집중 코너 규칙을 구현한다.

- 최근 몇 랩(기본 3랩)에서 기준 랩 대비 시간을 가장 많이 잃는 코너 4~5개(기본 5개)를
  집중 코너로 정한다. 무효 랩(아웃랩·피트·무효·SC/VSC·레이스 1랩)과 다른 차의 랩은
  산정에서 뺀다(리뷰 포커스 4).
- 집중 코너의 최신 랩 손실이 문턱값(`min_loss_ms`) 아래로 내려가면 목록에서 빼고
  다음으로 손실이 큰 코너를 넣는다. 그때 `FocusChange(reason="improved")`를 한 번
  돌려주며, 무전 문장("3번 좋아짐. 다음 7번.")은 `coaching.coach.Coach`가 만든다.
- 저장된 집중 코너가 있는 서킷은 세션 시작 때 `seed()`로 그 목록부터 시작한다
  (계획 작업 19가 쓸 자리). 레이스 세션에서는 1랩(출발 혼전)을 산정에서 빼고,
  유효 랩이 `RACE_FOCUS_MIN_VALID_LAPS`(2랩) 쌓인 뒤부터 집중 코너를 정한다
  (설계 6.4 "기록이 2~3랩 쌓이면 시작"). 세션 종류를 모르면(`None`) 바로 시작한다.

손실 계산은 `f1re.coaching.reference.corner_time_loss_ms`와 같은 정의(랩 거리 기준
선형 보간 시간 차)를 쓰되, 랩마다 샘플 시각 표를 한 번만 만들어 코너 18개 × 최근
3랩을 빠르게 훑는다. 같은 입력이면 두 계산 결과는 ±1ms 안에서 같다(테스트로 확인).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from f1re.coaching.reference import ReferenceSet
from f1re.config import AppConfig
from f1re.laps.traces import RACE_SESSION_TYPES, LapTrace
from f1re.tracks.model import Corner

# 집중 코너로 볼 최소 평균 손실(ms). 이보다 작으면 코칭할 가치가 없다고 본다.
# 같은 값을 "개선됐다"의 문턱으로도 쓴다.
DEFAULT_MIN_LOSS_MS = 50

# 레이스 세션에서 집중 코너 산정을 시작하는 유효 랩 수. 1랩(출발 혼전)은 빼고
# 기록이 2랩 쌓인 뒤부터 시작한다(설계 6.4 "2~3랩"). 타임 트라이얼·연습·예선은
# 1랩부터 시작한다.
RACE_FOCUS_MIN_VALID_LAPS = 2


@dataclass(frozen=True)
class FocusConfig:
    """집중 코너 선택 설정. 기본값은 `config/default.toml`의 `[global]`과 같다."""

    count: int = 5
    recent_laps: int = 3
    min_loss_ms: int = DEFAULT_MIN_LOSS_MS
    min_valid_laps: int = 1

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("집중 코너 수는 1 이상이어야 합니다")
        if self.recent_laps < 1:
            raise ValueError("집중 코너 산정 랩 수는 1 이상이어야 합니다")
        if self.min_loss_ms < 0:
            raise ValueError("집중 코너 손실 문턱값은 0 이상이어야 합니다")
        if self.min_valid_laps < 1:
            raise ValueError("집중 코너 시작 랩 수는 1 이상이어야 합니다")

    @classmethod
    def from_app_config(cls, config: AppConfig) -> FocusConfig:
        """앱 설정의 `focus_corner_count`·`focus_recent_laps`를 쓴다."""
        return cls(
            count=config.focus_corner_count,
            recent_laps=config.focus_recent_laps,
        )


class FocusReason(StrEnum):
    """집중 코너 목록이 바뀐 이유."""

    ADDED = "added"
    IMPROVED = "improved"
    DROPPED = "dropped"


@dataclass(frozen=True)
class FocusChange:
    """집중 코너 목록 변화 하나.

    `IMPROVED`는 문턱 아래로 줄어 빠진 코너이며 `next_corner`에 그 자리를 채운
    코너가 있다(없으면 None). `ADDED`·`DROPPED`는 알림을 내보내지 않는다.
    """

    corner: int
    reason: FocusReason
    loss_ms: int
    next_corner: int | None = None


class FocusSet:
    """최근 유효 랩들로 집중 코너 목록을 갱신한다.

    `references`는 세션 기준 랩 집합이며, 이 객체는 기준 랩을 바꾸지 않는다.
    호출자(코치)가 완료 랩을 `ReferenceSet.update()`에 먼저 넣은 뒤
    `update()`를 부른다. `session_type`을 주면 레이스 1랩을 빼고 레이스의
    시작 랩 수(`RACE_FOCUS_MIN_VALID_LAPS`)를 적용한다.
    """

    def __init__(
        self,
        corners: Sequence[Corner],
        references: ReferenceSet,
        *,
        config: FocusConfig | None = None,
        player_car_index: int = 0,
        session_type: int | None = None,
        initial: Iterable[int] = (),
    ) -> None:
        self._corners = tuple(corners)
        self._by_number = {corner.number: corner for corner in self._corners}
        self._references = references
        self._config = config or FocusConfig()
        self._player_car_index = player_car_index
        self._session_type = session_type
        self._recent: list[LapTrace] = []
        self._focus: list[int] = []
        self._last_loss: dict[int, int] = {}
        self._time_cache: dict[int, tuple[tuple[float, float], ...]] = {}
        if initial:
            self.seed(initial)

    # --- 조회 -------------------------------------------------------------

    @property
    def corners(self) -> tuple[int, ...]:
        """집중 코너 번호를 손실이 큰 순서로 돌려준다."""
        return tuple(self._focus)

    @property
    def config(self) -> FocusConfig:
        return self._config

    @property
    def min_valid_laps(self) -> int:
        """집중 코너 산정을 시작하는 유효 랩 수(레이스 세션 규칙 반영)."""
        if self._session_type in RACE_SESSION_TYPES:
            return max(self._config.min_valid_laps, RACE_FOCUS_MIN_VALID_LAPS)
        return self._config.min_valid_laps

    @property
    def recent_laps(self) -> tuple[LapTrace, ...]:
        return tuple(self._recent)

    def contains(self, corner: int) -> bool:
        return corner in self._focus

    def losses(self) -> dict[int, int]:
        """최근 랩들의 코너별 평균 손실(ms). 진단·화면용."""
        references = {
            corner.number: self._references.best_for(corner.number)
            for corner in self._corners
        }
        return self._mean_losses(references)

    # --- 설정 -------------------------------------------------------------

    def seed(self, corners: Iterable[int]) -> None:
        """저장된 집중 코너로 시작한다(작업 19). 모르는 코너 번호는 버린다."""
        seeded: list[int] = []
        for number in corners:
            number = int(number)
            if number in self._by_number and number not in seeded:
                seeded.append(number)
        self._focus = seeded[: self._config.count]

    # --- 갱신 -------------------------------------------------------------

    def update(self, completed_laps: list[LapTrace]) -> list[FocusChange]:
        """완료 랩을 반영하고 집중 코너 목록 변화를 돌려준다.

        무효 랩과 내 차가 아닌 랩은 산정에서 뺀다. 최근 랩 수가 시작 랩 수보다
        적으면 아직 아무것도 바꾸지 않는다. 레이스 세션은 1랩을 빼고 2랩부터
        시작하며, 설정값이 더 크면 설정값을 따른다.
        """
        self._collect(completed_laps)
        if len(self._recent) < self.min_valid_laps:
            return []

        # 기준 랩 조회는 ReferenceSet이 후보마다 코너 시간을 다시 계산하므로
        # 갱신 한 번에 코너별로 한 번만 묻는다.
        references = {
            corner.number: self._references.best_for(corner.number)
            for corner in self._corners
        }
        mean_losses = self._mean_losses(references)
        latest_losses = self._latest_losses(references)
        minimum = self._config.min_loss_ms

        # 최신 랩에서 이미 문턱 아래로 줄어든 코너는 목록에서 뺀다.
        recovered = {
            number for number, loss in latest_losses.items() if loss < minimum
        }
        ranked = sorted(
            (
                number
                for number, loss in mean_losses.items()
                if loss >= minimum and number not in recovered
            ),
            key=lambda number: (-mean_losses[number], number),
        )
        selected = ranked[: self._config.count]

        previous = list(self._focus)
        removed = [number for number in previous if number not in selected]
        added = [number for number in selected if number not in previous]

        # 개선 알림은 직전 갱신에서 실제로 손실이 있던 집중 코너만 낸다.
        # 세션 첫 갱신에서 저장된 목록이 통째로 빠지는 경우는 알리지 않는다.
        improved = [
            number
            for number in removed
            if number in recovered and self._last_loss.get(number, 0) >= minimum
        ]

        changes: list[FocusChange] = []
        remaining_added = list(added)
        announced: set[int] = set()
        for number in improved:
            next_corner = remaining_added.pop(0) if remaining_added else None
            changes.append(
                FocusChange(
                    corner=number,
                    reason=FocusReason.IMPROVED,
                    loss_ms=latest_losses.get(number, 0),
                    next_corner=next_corner,
                )
            )
            announced.add(number)
        for number in removed:
            if number in announced:
                continue
            changes.append(
                FocusChange(
                    corner=number,
                    reason=FocusReason.DROPPED,
                    loss_ms=latest_losses.get(number, 0),
                )
            )
        for number in remaining_added:
            changes.append(
                FocusChange(
                    corner=number,
                    reason=FocusReason.ADDED,
                    loss_ms=mean_losses.get(number, 0),
                )
            )

        self._focus = selected
        self._last_loss = dict(latest_losses)
        return changes

    # --- 내부 -------------------------------------------------------------

    def _collect(self, completed_laps: Iterable[LapTrace]) -> None:
        for trace in completed_laps:
            if trace.car_index != self._player_car_index:
                continue
            if not trace.valid or len(trace.samples) < 2:
                continue
            # 레이스 1랩(출발 혼전)은 기록 표시와 상관없이 산정에서 뺀다.
            if self._session_type in RACE_SESSION_TYPES and trace.lap == 1:
                continue
            self._recent.append(trace)
        # 오래된 랩은 산정에서 빼되, 시각 표 캐시는 그대로 둔다(세션 규모라 작다).
        while len(self._recent) > self._config.recent_laps:
            dropped = self._recent.pop(0)
            self._time_cache.pop(id(dropped), None)

    def _mean_losses(
        self, references: dict[int, tuple[LapTrace, str] | None]
    ) -> dict[int, int]:
        if not self._recent:
            return {}
        result: dict[int, int] = {}
        for corner in self._corners:
            reference = references.get(corner.number)
            if reference is None:
                continue
            reference_points = self._points(reference[0])
            values = [
                corner_loss_ms(self._points(trace), reference_points, corner)
                for trace in self._recent
            ]
            result[corner.number] = int(round(sum(values) / len(values)))
        return result

    def _latest_losses(
        self, references: dict[int, tuple[LapTrace, str] | None]
    ) -> dict[int, int]:
        if not self._recent:
            return {}
        trace = self._recent[-1]
        points = self._points(trace)
        result: dict[int, int] = {}
        for corner in self._corners:
            reference = references.get(corner.number)
            if reference is None:
                continue
            result[corner.number] = corner_loss_ms(
                points, self._points(reference[0]), corner
            )
        return result

    def _points(self, trace: LapTrace) -> tuple[tuple[float, float], ...]:
        key = id(trace)
        points = self._time_cache.get(key)
        if points is None:
            points = lap_time_points(trace)
            self._time_cache[key] = points
        return points


# ---------------------------------------------------------------------------
# 랩 거리 → 랩 시각 보간 (코너 손실 빠른 계산)


def lap_time_points(trace: LapTrace) -> tuple[tuple[float, float], ...]:
    """랩 샘플을 (랩 거리, 랩 시각 ms) 순서 목록으로 만든다.

    유한한 값만 남기고 랩 거리 순으로 정렬한다. `metrics.time_at_distance`와
    같은 보간을 쓰기 위한 사전 계산이다.
    """
    points: list[tuple[float, float]] = []
    for sample in trace.samples:
        if not (
            math.isfinite(sample.dist_m) and math.isfinite(float(sample.t_ms))
        ):
            continue
        points.append((sample.dist_m, float(sample.t_ms)))
    points.sort(key=lambda point: point[0])
    return tuple(points)


def time_at_distance(points: Sequence[tuple[float, float]], dist_m: float) -> float:
    """랩 거리 `dist_m`의 랩 시각(ms). 샘플 사이 선형 보간, 범위 밖은 자른다."""
    if not points:
        return 0.0
    if dist_m <= points[0][0]:
        return points[0][1]
    if dist_m >= points[-1][0]:
        return points[-1][1]
    index = bisect.bisect_left(points, (dist_m, -math.inf))
    previous = points[index - 1]
    current = points[index]
    span = current[0] - previous[0]
    if span <= 0.0:
        return current[1]
    fraction = (dist_m - previous[0]) / span
    return previous[1] + fraction * (current[1] - previous[1])


def corner_loss_ms(
    points: Sequence[tuple[float, float]],
    reference_points: Sequence[tuple[float, float]],
    corner: Corner,
) -> int:
    """코너 구간 시간 손실(ms). 느리면 양수. 기준 랩과 같은 정의를 쓴다."""
    duration = time_at_distance(points, corner.exit_m) - time_at_distance(
        points, corner.entry_m
    )
    reference_duration = time_at_distance(
        reference_points, corner.exit_m
    ) - time_at_distance(reference_points, corner.entry_m)
    return int(round(duration - reference_duration))


__all__ = [
    "DEFAULT_MIN_LOSS_MS",
    "RACE_FOCUS_MIN_VALID_LAPS",
    "FocusChange",
    "FocusConfig",
    "FocusReason",
    "FocusSet",
    "corner_loss_ms",
    "lap_time_points",
    "time_at_distance",
]
