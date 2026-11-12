"""중심선 윤곽: 랩 거리로 위치와 진행 방향을 선형 보간한다.

점은 `(dist_m, x, z)`이고 랩 거리 순이다. 마지막 점에서 첫 점으로 다시 이어지는
닫힌 고리로 다루며, 거리는 랩 길이로 감긴다.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TrackOutline:
    """서킷 중심선. 좌표 단위는 m이고 게임 좌표계와는 별개다."""

    points: list[tuple[float, float, float]]
    _dists: tuple[float, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("중심선 점이 2개 이상이어야 합니다")
        dists: list[float] = []
        previous = -math.inf
        for point in self.points:
            if len(point) != 3:
                raise ValueError("중심선 점은 (dist_m, x, z)여야 합니다")
            dist, x, z = point
            if not all(math.isfinite(value) for value in (dist, x, z)):
                raise ValueError("중심선에 유한하지 않은 값이 있습니다")
            if dist <= previous:
                raise ValueError("중심선 dist_m는 엄격히 증가해야 합니다")
            dists.append(float(dist))
            previous = dist
        object.__setattr__(self, "_dists", tuple(dists))

    @property
    def lap_length_m(self) -> float:
        """마지막 점에서 첫 점으로 닫는 변까지 포함한 랩 길이."""
        first = self.points[0]
        last = self.points[-1]
        return (last[0] - first[0]) + math.dist(last[1:], first[1:])

    def position_at(self, dist_m: float) -> tuple[float, float]:
        """랩 거리 `dist_m`(m) 지점의 (x, z). 범위를 넘으면 랩을 감는다."""
        a, b, t = self._segment_at(dist_m)
        return (a[1] + t * (b[1] - a[1]), a[2] + t * (b[2] - a[2]))

    def heading_at(self, dist_m: float) -> float:
        """랩 거리 `dist_m`(m) 지점 진행 방향(라디안, atan2(dz, dx))."""
        a, b, _t = self._segment_at(dist_m)
        return math.atan2(b[2] - a[2], b[1] - a[1])

    def _segment_at(
        self, dist_m: float
    ) -> tuple[tuple[float, float, float], tuple[float, float, float], float]:
        first = self.points[0]
        last = self.points[-1]
        total = self.lap_length_m
        if not math.isfinite(dist_m):
            raise ValueError("거리는 유한해야 합니다")
        dist = (dist_m - first[0]) % total + first[0]
        index = bisect.bisect_right(self._dists, dist) - 1
        if index < 0:
            index = 0
        if index >= len(self.points) - 1:
            a, b = last, first
            start = last[0]
            length = total - (last[0] - first[0])
        else:
            a, b = self.points[index], self.points[index + 1]
            start = a[0]
            length = b[0] - a[0]
        t = 0.0 if length == 0 else (dist - start) / length
        return a, b, min(1.0, max(0.0, t))
