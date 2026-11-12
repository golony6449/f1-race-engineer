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
`coaching.store.ReferenceStore`가 이 모듈의 직렬화 함수(`reference_to_dict`·
`reference_from_dict`)를 써서 담당한다(작업 19).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from f1re.coaching.metrics import corner_duration_ms
from f1re.laps.traces import LapTrace, Sample
from f1re.tracks.model import Corner
from f1re.tracks.outline import TrackOutline

# `ReferenceSet.best_for`가 돌려주는 기준 출처.
SOURCE_SELF = "self"
SOURCE_FASTEST_CAR = "fastest_car"

# 기준 랩 저장(SQLite) 직렬화 형식 버전(작업 19).
REFERENCE_PAYLOAD_VERSION = 1


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


# ---------------------------------------------------------------------------
# 저장용 직렬화(작업 19, SCRUM-69)
#
# SCRUM-67이 이 모듈의 `ReferenceSet`을 동시에 고치므로 클래스에는 손대지 않고
# 모듈 수준 함수만 더한다. 함수는 같은 모듈의 클래스 내부 필드를 직접 읽는다.


def reference_to_dict(reference: ReferenceSet) -> dict[str, Any]:
    """`ReferenceSet`을 JSON 직렬화 가능한 dict로 만든다(작업 19).

    저장하는 랩은 기준 후보 두 개뿐이다: 내 최고 유효 랩과 세션 최속차의 최고
    랩. 세션의 모든 유효 랩을 쌓아 두면 저장 크기만 커지고, 다시 불러온 뒤의
    `best_for` 결과는 두 랩으로도 같다.

    코너 목록과 내 차 인덱스도 함께 넣는다. 데이터 파일이 없는 서킷(자동 탐지
    코너)은 서킷 ID만으로 코너를 되살릴 수 없기 때문이다.
    """
    laps: list[dict[str, Any]] = []
    for source in (SOURCE_SELF, SOURCE_FASTEST_CAR):
        trace = reference.best_lap(source)
        if trace is not None:
            laps.append(_trace_to_dict(trace))
    return {
        "version": REFERENCE_PAYLOAD_VERSION,
        "player_car_index": int(reference._player_car_index),
        "corners": [_corner_to_dict(corner) for corner in reference._corners.values()],
        "laps": laps,
    }


def reference_from_dict(data: Mapping[str, Any]) -> ReferenceSet:
    """`reference_to_dict`의 결과로 `ReferenceSet`을 되살린다(작업 19).

    형식이 다르거나 값이 잘못됐으면 `ValueError`를 올린다. DB에 손상된 행이
    있어도 서버가 멈추지 않도록 저장소(`coaching.store`)가 이 예외를 잡는다.
    """
    version = data.get("version")
    if version != REFERENCE_PAYLOAD_VERSION:
        raise ValueError(f"지원하지 않는 기준 랩 저장 형식입니다: {version!r}")
    try:
        raw_corners = data["corners"]
        raw_laps = data["laps"]
        if not isinstance(raw_corners, list) or not isinstance(raw_laps, list):
            raise ValueError("corners·laps는 목록이어야 합니다")
        corners = [_corner_from_dict(item) for item in raw_corners]
        reference = ReferenceSet(
            corners,
            player_car_index=_as_int(data.get("player_car_index", 0)),
        )
        for item in raw_laps:
            if not isinstance(item, dict):
                raise ValueError("lap 항목이 dict가 아닙니다")
            reference.update(_trace_from_dict(item))
        return reference
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("기준 랩 저장 내용이 잘못됐습니다") from exc


def _corner_to_dict(corner: Corner) -> dict[str, Any]:
    return {
        "number": int(corner.number),
        "official": bool(corner.official),
        "entry_m": float(corner.entry_m),
        "apex_m": float(corner.apex_m),
        "exit_m": float(corner.exit_m),
    }


def _corner_from_dict(data: Mapping[str, Any]) -> Corner:
    return Corner(
        number=_as_int(data["number"]),
        official=bool(data["official"]),
        entry_m=_as_float(data["entry_m"]),
        apex_m=_as_float(data["apex_m"]),
        exit_m=_as_float(data["exit_m"]),
    )


def _trace_to_dict(trace: LapTrace) -> dict[str, Any]:
    return {
        "car_index": int(trace.car_index),
        "lap": int(trace.lap),
        "valid": bool(trace.valid),
        "invalid_reason": trace.invalid_reason,
        "samples": [_sample_to_dict(sample) for sample in trace.samples],
    }


def _trace_from_dict(data: Mapping[str, Any]) -> LapTrace:
    raw_samples = data["samples"]
    if not isinstance(raw_samples, list):
        raise ValueError("samples는 목록이어야 합니다")
    return LapTrace(
        car_index=_as_int(data["car_index"]),
        lap=_as_int(data["lap"]),
        valid=bool(data["valid"]),
        invalid_reason=data.get("invalid_reason"),
        samples=[_sample_from_dict(sample) for sample in raw_samples],
    )


def _sample_to_dict(sample: Sample) -> dict[str, Any]:
    """샘플 하나를 dict로 만든다. 소수 자리는 비교에 영향 없는 만큼만 남긴다."""
    data: dict[str, Any] = {
        "dist_m": round(float(sample.dist_m), 2),
        "t_ms": int(sample.t_ms),
        "speed": round(float(sample.speed), 2),
        "throttle": round(float(sample.throttle), 4),
        "brake": round(float(sample.brake), 4),
        "steer": round(float(sample.steer), 4),
        "gear": int(sample.gear),
        "x": round(float(sample.x), 3),
        "z": round(float(sample.z), 3),
    }
    if sample.wheel_slip is not None:
        data["wheel_slip"] = [round(float(value), 4) for value in sample.wheel_slip]
    if sample.fuel_kg is not None:
        data["fuel_kg"] = round(float(sample.fuel_kg), 3)
    return data


def _sample_from_dict(data: Mapping[str, Any]) -> Sample:
    return Sample(
        dist_m=_as_float(data["dist_m"]),
        t_ms=_as_int(data["t_ms"]),
        speed=_as_float(data["speed"]),
        throttle=_as_float(data["throttle"]),
        brake=_as_float(data["brake"]),
        steer=_as_float(data["steer"]),
        gear=_as_int(data["gear"]),
        x=_as_float(data["x"]),
        z=_as_float(data["z"]),
        wheel_slip=_as_wheel_slip(data.get("wheel_slip")),
        fuel_kg=_as_optional_float(data.get("fuel_kg")),
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"정수가 아닙니다: {value!r}")
    return int(value)


def _as_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"숫자가 아닙니다: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"유한한 숫자가 아닙니다: {value!r}")
    return number


def _as_optional_float(value: Any) -> float | None:
    return None if value is None else _as_float(value)


def _as_wheel_slip(value: Any) -> tuple[float, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"휠 슬립이 목록이 아닙니다: {value!r}")
    return tuple(_as_float(item) for item in value)


__all__ = [
    "REFERENCE_PAYLOAD_VERSION",
    "SOURCE_FASTEST_CAR",
    "SOURCE_SELF",
    "ReferenceSet",
    "corner_time_loss_ms",
    "reference_from_dict",
    "reference_to_dict",
    "trace_outline",
]
