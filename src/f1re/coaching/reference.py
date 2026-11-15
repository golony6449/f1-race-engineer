"""기준 랩 선택과 코너 시간 손실(작업 15, SCRUM-65 · SCRUM-94).

기준 랩은 **차마다 가장 최근 유효 랩**이다(SCRUM-94, 사용자 요청). AI 상대가
있는 세션에서는 내 차를 포함한 모든 차의 가장 최근 유효 랩이 후보이고,
`ReferenceSet.best_for`가 그 코너 구간 시간이 가장 짧은 랩을 그 코너의 기준으로
쓴다. 출처는 내 차 랩이면 `"self"`, 다른 차 랩이면 `"recent_car"`다.

- 이번 세션에 유효 랩이 하나라도 들어오면 저장된 기준(DB)은 코칭 비교에 쓰지
  않는다. 세션 후보가 하나도 없을 때만 `add_stored`로 넣어 둔 저장 기준을 쓰며,
  그때 다른 차 랩의 출처는 `"fastest_car"`다.
- 타임 트라이얼(`NON_AI_SESSION_TYPES`)은 최근 랩 규칙을 적용하지 않고 지금처럼
  내 최고 유효 랩을 기준으로 쓴다.
- 무효 랩(`LapTrace.valid is False`)은 어떤 경우에도 기준이 되지 않는다
  (리뷰 포커스 4: 아웃랩·피트·무효·SC/VSC·레이스 1랩). 세션 종류를 알면
  레이스 세션의 1랩(출발 혼전)도 `valid` 표시와 상관없이 뺀다.
- 최근 랩만 후보로 두므로 `prune()`은 차마다 가장 최근 유효 랩 하나만 남긴다
  (메모리 절약). 저장·요약에 쓰는 `best_lap()`은 코칭 비교와 달리 차마다 가장
  빠른 랩(최고 기록)을 따로 기억해 돌려준다.
- 다른 차 랩과 비교할 때 쓸 수 있는 값은 양쪽에 다 있는 위치·속도·페달·조향
  뿐이다. 휠 슬립처럼 내 차에만 있는 값은 `CornerMetrics.exit_wheel_slip`이
  다른 차에서 `None`이므로 자연히 내 차 랩과만 비교된다.

조건(모드·날씨·차)별 SQLite 저장과 조건이 다를 때 시간 차이를 말하지 않는 규칙은
`coaching.store.ReferenceStore`가 이 모듈의 직렬화 함수(`reference_to_dict`·
`reference_from_dict`)를 써서 담당한다(작업 19).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from f1re.coaching.metrics import corner_duration_ms
from f1re.laps.traces import RACE_SESSION_TYPES, LapTrace, Sample
from f1re.tracks.model import Corner
from f1re.tracks.outline import TrackOutline

# `ReferenceSet.best_for`가 돌려주는 기준 출처.
SOURCE_SELF = "self"
# 이번 세션 다른 차의 가장 최근 유효 랩(SCRUM-94).
SOURCE_RECENT_CAR = "recent_car"
# 이번 세션 랩이 없을 때만 쓰는 저장 기준의 다른 차 최고 랩.
SOURCE_FASTEST_CAR = "fastest_car"

# 기준 랩 저장(SQLite) 직렬화 형식 버전(작업 19).
REFERENCE_PAYLOAD_VERSION = 1

# 공식 세션 종류 중 AI 상대가 없는 세션. 13=Time Trial.
NON_AI_SESSION_TYPES = frozenset({13})


class ReferenceSet:
    """한 세션에서 들어온 유효 랩들로 기준 랩 후보를 고른다.

    `corners`는 이 서킷의 코너 목록이다(공식 데이터 또는 자동 탐지 결과).
    `player_car_index`는 내 차 인덱스다. `session_type`은 공식 세션 종류로,
    주면 레이스 1랩 제외와 타임 트라이얼(최근 랩 규칙 대신 내 최고 랩)을
    판단한다. 세션이 바뀌면(sessionUID 변경) 새 `ReferenceSet`을 만든다.

    저장된 기준(지난 세션의 최고 기록)은 `add_stored()`로 넣는다. 이번 세션
    유효 랩(`update`)이 하나라도 있으면 저장 기준은 `best_for`에서 쓰지 않는다.
    """

    def __init__(
        self,
        corners: Sequence[Corner],
        *,
        player_car_index: int = 0,
        session_type: int | None = None,
    ) -> None:
        self._corners = {corner.number: corner for corner in corners}
        self._player_car_index = player_car_index
        self._session_type = session_type
        # 이번 세션 유효 랩(코칭 비교 후보). prune()이 차마다 최근 랩만 남긴다.
        self._laps: list[LapTrace] = []
        # 저장·요약용 최고 기록. 차마다 전체 랩 시간이 가장 짧은 랩 하나만 둔다.
        self._self_best: LapTrace | None = None
        self._other_best: LapTrace | None = None
        # 지난 세션에서 저장해 둔 기준 랩(이번 세션 랩이 없을 때만 쓴다).
        self._stored: list[LapTrace] = []

    @property
    def session_type(self) -> int | None:
        return self._session_type

    @property
    def has_session_laps(self) -> bool:
        """이번 세션에서 기준 후보(유효 랩)가 하나라도 들어왔는가."""
        return bool(self._laps)

    def update(self, trace: LapTrace) -> None:
        """완료된 랩 하나를 이번 세션 기준 후보로 넣는다.

        무효 랩과 샘플이 모자란 랩은 버린다. 레이스 세션의 1랩(출발 혼전)은
        `valid` 표시와 상관없이 기준 후보에서 뺀다(설계 6.4, 리뷰 포커스 4).
        차마다 가장 최근 랩이 `best_for` 후보가 되고, 가장 빠른 랩은 저장용
        최고 기록으로 따로 기억한다.
        """
        if not self._accepts(trace):
            return
        self._laps.append(trace)
        if trace.car_index == self._player_car_index:
            if self._self_best is None or _lap_time_ms(trace) < _lap_time_ms(
                self._self_best
            ):
                self._self_best = trace
        elif self._other_best is None or _lap_time_ms(trace) < _lap_time_ms(
            self._other_best
        ):
            self._other_best = trace

    def add_stored(self, trace: LapTrace) -> None:
        """지난 세션에서 저장해 둔 기준 랩을 넣는다(코칭 비교용 대체 후보).

        이번 세션 유효 랩이 하나라도 들어오면 `best_for`는 저장 기준을 쓰지
        않는다(SCRUM-94). 저장 기준끼리는 코너마다 더 빠른 랩을 고른다.
        """
        if not self._accepts(trace):
            return
        self._stored.append(trace)

    def best_for(self, corner: int) -> tuple[LapTrace, str] | None:
        """코너 하나의 기준 랩과 출처를 돌려준다. 기준이 없으면 `None`.

        타임 트라이얼은 내 최고 유효 랩을 쓴다(출처 `"self"`). 그 밖의 세션은
        차마다 가장 최근 유효 랩 중 그 코너 구간 시간이 가장 짧은 랩을 쓰고,
        출처는 내 차면 `"self"`, 다른 차면 `"recent_car"`다. 이번 세션에 쓸 랩이
        하나도 없을 때만 저장된 기준을 쓰며, 그때 다른 차 랩의 출처는
        `"fastest_car"`다. 같은 시간이면 내 차 랩, 그다음 낮은 차 인덱스를 쓴다.
        """
        corner_def = self._corners.get(corner)
        if corner_def is None:
            return None

        if self._session_type in NON_AI_SESSION_TYPES:
            best_self = self.best_lap(SOURCE_SELF)
            if best_self is None:
                return None
            return best_self, SOURCE_SELF

        # 이번 세션 랩이 하나라도 있으면 저장 기준은 쓰지 않는다(SCRUM-94).
        candidates = (
            self._recent_candidates()
            if self.has_session_laps
            else self._stored_candidates()
        )
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (
                corner_duration_ms(item[0], corner_def),
                0 if item[0].car_index == self._player_car_index else 1,
                item[0].car_index,
            ),
        )

    def best_lap(self, source: str) -> LapTrace | None:
        """출처별 최고 기록 랩. 없으면 `None`.

        코칭 비교(`best_for`)와 달리 **전체 랩 시간이 가장 짧은 랩**이다.
        이번 세션 랩과 저장된 기준 랩을 함께 보고, 세션 끝에 저장하거나
        화면에 기준 요약을 보여 줄 때 쓴다(SCRUM-94). 타임 트라이얼에는 다른
        차 기준을 두지 않는다.
        """
        if source == SOURCE_SELF:
            laps = [
                trace
                for trace in self._stored
                if trace.car_index == self._player_car_index
            ]
            if self._self_best is not None:
                laps.append(self._self_best)
            return min(laps, key=_lap_time_ms) if laps else None
        if source == SOURCE_FASTEST_CAR:
            if self._session_type in NON_AI_SESSION_TYPES:
                return None
            laps = [
                trace
                for trace in self._stored
                if trace.car_index != self._player_car_index
            ]
            if self._other_best is not None:
                laps.append(self._other_best)
            return min(laps, key=_lap_time_ms) if laps else None
        raise ValueError(f"알 수 없는 기준 출처입니다: {source}")

    def prune(self) -> None:
        """차마다 가장 최근 유효 랩 하나만 남긴다(실서버 메모리 절약, SCRUM-94).

        `best_for`는 차마다 가장 최근 랩만 보므로 결과가 그대로다. 최고 기록
        (`_self_best`·`_other_best`)은 한 랩씩만 들고 있어 영향받지 않는다.
        """
        recent: dict[int, LapTrace] = {}
        for trace in self._laps:
            recent[trace.car_index] = trace
        self._laps = list(recent.values())

    # ------------------------------------------------------------------
    # 후보 선택

    def _accepts(self, trace: LapTrace) -> bool:
        if not trace.valid or len(trace.samples) < 2:
            return False
        # 레이스 1랩(출발 혼전)은 `valid` 표시와 상관없이 뺀다.
        if self._session_type in RACE_SESSION_TYPES and trace.lap == 1:
            return False
        return True

    def _recent_candidates(self) -> list[tuple[LapTrace, str]]:
        """차마다 가장 최근 유효 랩. 이번 세션 랩이 없으면 빈 목록."""
        recent: dict[int, LapTrace] = {}
        for trace in self._laps:
            recent[trace.car_index] = trace
        return [
            (
                trace,
                SOURCE_SELF
                if trace.car_index == self._player_car_index
                else SOURCE_RECENT_CAR,
            )
            for trace in recent.values()
        ]

    def _stored_candidates(self) -> list[tuple[LapTrace, str]]:
        return [
            (
                trace,
                SOURCE_SELF
                if trace.car_index == self._player_car_index
                else SOURCE_FASTEST_CAR,
            )
            for trace in self._stored
        ]


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
# SCRUM-67과의 동시 작업 충돌을 피하려고 클래스 대신 모듈 수준 함수로 둔다.
# 함수는 같은 모듈의 클래스 내부 필드를 직접 읽는다.


def reference_to_dict(reference: ReferenceSet) -> dict[str, Any]:
    """`ReferenceSet`을 JSON 직렬화 가능한 dict로 만든다(작업 19).

    저장하는 랩은 **최고 기록 두 개**다: 내 최고 유효 랩과 가장 빠른 다른 차의
    최고 랩. 코칭 비교는 이번 세션의 최근 랩을 쓰지만(SCRUM-94), 다음 방문
    세션의 첫 랩부터 비교할 기준은 최고 기록이 낫다. 세션의 모든 유효 랩을
    쌓아 두면 저장 크기만 커진다.

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
        "session_type": reference.session_type,
        "corners": [_corner_to_dict(corner) for corner in reference._corners.values()],
        "laps": laps,
    }


def reference_from_dict(data: Mapping[str, Any]) -> ReferenceSet:
    """`reference_to_dict`의 결과로 `ReferenceSet`을 되살린다(작업 19).

    되살린 랩은 저장 기준(`add_stored`)으로 넣는다. 이번 세션 랩이 생기면
    코칭 비교에서 빠지고, 생기기 전까지만 쓰인다(SCRUM-94).

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
        raw_session_type = data.get("session_type")
        reference = ReferenceSet(
            corners,
            player_car_index=_as_int(data.get("player_car_index", 0)),
            session_type=(
                None if raw_session_type is None else _as_int(raw_session_type)
            ),
        )
        for item in raw_laps:
            if not isinstance(item, dict):
                raise ValueError("lap 항목이 dict가 아닙니다")
            reference.add_stored(_trace_from_dict(item))
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
    if sample.surface is not None:
        data["surface"] = [int(value) for value in sample.surface]
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
        surface=_as_surface(data.get("surface")),
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


def _as_surface(value: Any) -> tuple[int, ...] | None:
    """바퀴별 노면(4개). 키가 없거나 형식이 틀린 옛 데이터는 None으로 읽는다."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    surfaces: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        surfaces.append(int(item))
    return tuple(surfaces)


__all__ = [
    "NON_AI_SESSION_TYPES",
    "REFERENCE_PAYLOAD_VERSION",
    "SOURCE_FASTEST_CAR",
    "SOURCE_RECENT_CAR",
    "SOURCE_SELF",
    "ReferenceSet",
    "corner_time_loss_ms",
    "reference_from_dict",
    "reference_to_dict",
    "trace_outline",
]
