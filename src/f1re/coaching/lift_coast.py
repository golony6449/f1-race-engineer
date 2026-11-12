"""리프트 앤 코스트 측정·구간 추천·필요할 때 무전(SCRUM-71).

리프트 앤 코스트는 브레이크를 밟기 전에 스로틀을 잠깐 떼어 연료를 아끼고
브레이크·타이어 온도를 낮추는 주행이다. 사용자 요청(설계 6.4·6.6의 연장)은
"내가 리프트 앤 코스트를 잘하고 있는지, 어디서 하면 좋을지"를 알려 주는 것이다.

이 모듈은 네 가지를 한다.

1. **측정**: 브레이크 구간마다 코스팅 거리(스로틀 < 5% 지점 → 브레이크가
   문턱값 이상이 된 지점, m)와 시간을 잰다. 랩마다 `Sample.fuel_kg`(Car Status의
   내 차 연료)로 연료 소모량도 기록한다. 데모 주행 샘플에는 연료 값이 없으므로
   그때는 연료 기록을 건너뛴다.
2. **추천**: 브레이크 구간의 "리프트 효율"을 진입 속도(브레이크 지점 속도),
   앞 직선 길이, 감속량으로 근사해 순위를 매기고 상위 2~3곳을 추천한다.
   감속이 거의 없는 코너(예: 130R)는 리프트로 얻을 게 없고 랩 타임만 잃으므로
   `MIN_DECEL_KMH` 미만이면 추천에서 뺀다.
3. **지시**: 레이스 세션에서만, `alerts.thresholds`(위험 기준 단일 출처)의
   `fuel_laps_margin`·`brake_temp`·앞 `tyre_temp` 중 하나가 WARN 이상일 때만
   추천 구간에서 리프트하라고 무전한다. 지시 유효 시간은 그 구간 리프트 지점
   전까지다(설계 6.7). 말할 타이밍은 작업 16의 `defer_until_straight`를 그대로
   재사용해 다음 긴 직선으로 미룬다.
4. **피드백**: 지시한 다음 랩, 그 구간을 빠져나온 직후 실제 코스팅 거리로
   "N번 리프트 짧음. M미터 더." 또는 "N번 리프트 좋음."을 한 번 말한다.
   같은 랩에 같은 구간을 두 번 말하지 않는다. 세 가지 지표가 모두 OK로
   돌아오면 "연료 회복. 리프트 그만."을 한 번 말하고 지시를 멈춘다.

무전은 작업 11의 문장 틀만 쓴다(`coaching.fuel_lift`, `coaching.fuel_lift_short`,
`improvement.fuel_lift_good`, `improvement.fuel_lift_stop`). 브레이크·타이어 온도만
위험할 때 연료 문장을 쓰면 거짓이 되므로 온도 사유용 틀 두 개를 같은 파일에
추가했다. 코너 데이터가 없는 서킷의 자동 탐지 코너는 "N번째 코너"로 말한다
(리뷰 포커스 5).

`on_sample(car_state, dist_m, now_ns)`의 `car_state`는 `coaching.coach.Coach`와
같은 키를 쓰고, 리프트 판단에 다음 키를 더 읽는다(모두 선택).

- `fuel_laps_margin`: 남은 연료 랩 - 남은 레이스 랩. 없으면
  `fuel_remaining_laps`와 `remaining_race_laps`의 차이로 계산한다.
- `brake_temp`(브레이크 4개 중 최대) 또는 `brake_temps`(4개 목록)
- `tyre_temp`(앞 타이어 최대) 또는 `tyre_temps`(패킷 순서 4개 목록,
  앞은 인덱스 2·3)
- `session_type`: 공식 세션 종류(10~12가 레이스)
- `lap_num`·`lap_valid`·`in_pit`·`safety_car`·`vsc` 등은 `Coach`와 같다.

`on_lap_completed(trace, now_ns)`는 연료 기록과 추천 갱신에 쓴다. 세션이 바뀌면
새 코치를 만든다.
"""

from __future__ import annotations

import bisect
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from f1re.alerts.thresholds import Severity, severity
from f1re.coaching.coach import CoachConfig, defer_until_straight
from f1re.coaching.metrics import (
    CoachingThresholds,
    corner_metrics,
    default_coaching_thresholds,
)
from f1re.laps.traces import (
    DRIVER_IN_LAP,
    DRIVER_IN_PIT,
    DRIVER_OFF_TRACK,
    DRIVER_OUT_LAP,
    LAP_DISTANCE_TOLERANCE_M,
    PIT_LANE_STATUSES,
    RACE_SESSION_TYPES,
    LapTrace,
    Sample,
)
from f1re.radio.queue import RadioKind, RadioMessage, RadioQueue
from f1re.radio.templates import corner_label, render, render_corner
from f1re.tracks.model import Corner, Track

# 스로틀을 뗀 것으로 보는 값(설계·Jira: 스로틀 < 5%).
DEFAULT_LIFT_THROTTLE = 0.05

# 리프트 앤 코스트로 얻을 게 있는 브레이크 구간의 최소 감속량(km/h).
# 130R처럼 거의 풀스로틀로 지나는 구간은 리프트하면 랩 타임만 잃는다.
DEFAULT_MIN_DECEL_KMH = 50.0

# 추천 구간 수(상위 2~3곳).
DEFAULT_RECOMMEND_COUNT = 3

# 코스팅 목표 거리 = 앞 직선 길이의 이 비율. 너무 짧거나 길지 않게 자른다.
DEFAULT_TARGET_FRACTION = 0.15
DEFAULT_MIN_TARGET_M = 30.0
DEFAULT_MAX_TARGET_M = 100.0

# 피드백: 목표보다 이만큼 짧으면 "짧음", 이 안쪽이면 "좋음".
DEFAULT_GOOD_TOLERANCE_M = 5.0

# 지시 무전 유효 시간의 하한·상한(초).
MIN_INSTRUCTION_TTL_S = 1.0
MAX_INSTRUCTION_TTL_S = 120.0

# 여유 회복 무전 유효 시간(초).
DEFAULT_STOP_TTL_S = 15.0

# 보관할 최근 기록 수.
DEFAULT_MAX_COASTING = 60
DEFAULT_MAX_FUEL_LAPS = 20

# 세션 종류를 모를 때 메시지를 내보내지 않는다(레이스에서만 지시).
_RACE_TYPES = frozenset(RACE_SESSION_TYPES)


@dataclass(frozen=True)
class LiftCoastConfig:
    """리프트 앤 코스트 판단 설정. 값은 구현자가 데모 리플레이로 정했다."""

    lift_throttle: float = DEFAULT_LIFT_THROTTLE
    min_decel_kmh: float = DEFAULT_MIN_DECEL_KMH
    recommend_count: int = DEFAULT_RECOMMEND_COUNT
    target_fraction: float = DEFAULT_TARGET_FRACTION
    min_target_m: float = DEFAULT_MIN_TARGET_M
    max_target_m: float = DEFAULT_MAX_TARGET_M
    good_tolerance_m: float = DEFAULT_GOOD_TOLERANCE_M
    stop_ttl_s: float = DEFAULT_STOP_TTL_S
    max_coasting: int = DEFAULT_MAX_COASTING
    max_fuel_laps: int = DEFAULT_MAX_FUEL_LAPS

    def __post_init__(self) -> None:
        if not 0.0 <= self.lift_throttle <= 1.0:
            raise ValueError("lift_throttle은 0~1이어야 합니다")
        if not math.isfinite(self.min_decel_kmh) or self.min_decel_kmh < 0:
            raise ValueError("min_decel_kmh는 0 이상의 유한한 값이어야 합니다")
        if self.recommend_count < 1:
            raise ValueError("recommend_count는 1 이상이어야 합니다")
        if not 0.0 < self.target_fraction <= 1.0:
            raise ValueError("target_fraction은 0보다 크고 1 이하여야 합니다")
        if self.min_target_m <= 0 or self.max_target_m < self.min_target_m:
            raise ValueError("목표 거리 범위가 잘못됐습니다")
        if self.good_tolerance_m < 0:
            raise ValueError("good_tolerance_m은 0 이상이어야 합니다")
        if self.max_coasting < 1 or self.max_fuel_laps < 1:
            raise ValueError("보관 기록 수는 1 이상이어야 합니다")


# ---------------------------------------------------------------------------
# 값 객체


@dataclass(frozen=True)
class LiftZone:
    """리프트 앤 코스트 추천 구간 하나.

    `brake_m`은 기준 랩의 브레이크 시작 지점, `lift_m`은 목표 코스팅 거리만큼
    앞당긴 리프트 지점, `straight_m`은 앞 코너 탈출부터 이 코너 진입까지의
    직선 길이다. `efficiency`는 추천 순위 점수(진입 속도·앞 직선·감속량의 곱)다.
    """

    corner: int
    official: bool
    entry_m: float
    brake_m: float
    lift_m: float
    straight_m: float
    approach_speed_kmh: float
    decel_kmh: float
    efficiency: float
    target_m: float

    @property
    def label(self) -> str:
        return corner_label(self.corner, official=self.official)

    def to_dict(self) -> dict[str, Any]:
        return {
            "corner": self.corner,
            "official": self.official,
            "label": self.label,
            "entry_m": round(self.entry_m, 1),
            "brake_m": round(self.brake_m, 1),
            "lift_m": round(self.lift_m, 1),
            "straight_m": round(self.straight_m, 1),
            "approach_speed_kmh": round(self.approach_speed_kmh, 1),
            "decel_kmh": round(self.decel_kmh, 1),
            "efficiency": round(self.efficiency, 3),
            "target_m": round(self.target_m, 1),
        }


@dataclass(frozen=True)
class CoastingMeasurement:
    """브레이크 구간 하나에서 잰 코스팅 거리·시간."""

    corner: int
    lap: int
    lift_m: float
    brake_m: float
    distance_m: float
    time_ms: int

    def to_dict(
        self, *, target_m: float | None = None, status: str | None = None
    ) -> dict[str, Any]:
        return {
            "corner": self.corner,
            "lap": self.lap,
            "lift_m": round(self.lift_m, 1),
            "brake_m": round(self.brake_m, 1),
            "distance_m": round(self.distance_m, 1),
            "time_ms": int(self.time_ms),
            "target_m": None if target_m is None else round(target_m, 1),
            "status": status,
        }


@dataclass(frozen=True)
class LapFuel:
    """랩 하나의 연료 기록. `Sample.fuel_kg`가 없으면 `used_kg`가 None이다."""

    lap: int
    start_kg: float | None
    end_kg: float | None
    used_kg: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lap": self.lap,
            "start_kg": _rounded(self.start_kg),
            "end_kg": _rounded(self.end_kg),
            "used_kg": _rounded(self.used_kg),
        }


# ---------------------------------------------------------------------------
# 추천·측정 계산


def coast_target_m(straight_m: float, config: LiftCoastConfig | None = None) -> float:
    """앞 직선 길이로 목표 코스팅 거리를 정한다. 10m 단위로 반올림한다."""
    limits = config or LiftCoastConfig()
    raw = max(0.0, float(straight_m)) * limits.target_fraction
    clamped = min(max(raw, limits.min_target_m), limits.max_target_m)
    return float(round(clamped / 10.0) * 10.0)


def rank_lift_zones(
    track: Track,
    trace: LapTrace,
    *,
    config: LiftCoastConfig | None = None,
    thresholds: CoachingThresholds | None = None,
    limit: int | None = None,
) -> list[LiftZone]:
    """랩 하나로 브레이크 구간의 리프트 효율을 매겨 상위 구간을 돌려준다.

    효율 = 앞 직선 길이 × 브레이크 지점 속도 × 감속량. 브레이크를 밟지 않는
    구간과 감속량이 `min_decel_kmh` 미만인 구간은 뺀다. 같은 점수면 코너 번호가
    작은 쪽을 먼저 둔다.

    무전 문장 틀(`coaching.fuel_lift` 등)은 거리 슬롯이 하나뿐이므로 상위
    구간들은 **같은 목표 코스팅 거리**를 쓴다(가장 짧은 목표에 맞춘다). 그래야
    "16번, 1번, 10번 리프트. 40미터."라고 말한 뒤 피드백도 같은 40m로 비교한다.
    """
    limits = config or LiftCoastConfig()
    coaching_thresholds = thresholds or default_coaching_thresholds()
    corners = sorted(track.corners, key=lambda corner: corner.entry_m)
    if not corners or track.length_m <= 0 or len(trace.samples) < 2:
        return []

    speed_points = _speed_points(trace)
    if len(speed_points) < 2:
        return []

    count = limits.recommend_count if limit is None else max(0, int(limit))
    if count == 0:
        return []

    zones: list[LiftZone] = []
    previous_exit = corners[-1].exit_m
    for corner in corners:
        straight_m = corner.entry_m - previous_exit
        if straight_m < 0.0:
            straight_m += track.length_m
        previous_exit = corner.exit_m

        metrics = corner_metrics(
            trace, corner, thresholds=coaching_thresholds
        )
        if metrics.brake_start_m is None:
            continue
        approach = _speed_at(speed_points, metrics.brake_start_m)
        if approach is None:
            continue
        decel = approach - metrics.min_speed
        if decel < limits.min_decel_kmh:
            continue

        target = coast_target_m(straight_m, limits)
        zones.append(
            LiftZone(
                corner=corner.number,
                official=corner.official,
                entry_m=corner.entry_m,
                brake_m=metrics.brake_start_m,
                lift_m=max(0.0, metrics.brake_start_m - target),
                straight_m=straight_m,
                approach_speed_kmh=approach,
                decel_kmh=decel,
                efficiency=straight_m * approach * decel / 1_000_000.0,
                target_m=target,
            )
        )

    zones.sort(key=lambda zone: (-zone.efficiency, zone.corner))
    top = zones[:count]
    if not top:
        return []
    shared_target = min(zone.target_m for zone in top)
    return [
        _with_target(zone, shared_target)
        for zone in top
    ]


def _with_target(zone: LiftZone, target_m: float) -> LiftZone:
    """추천 구간의 목표 코스팅 거리를 하나로 맞춘다."""
    return replace(
        zone,
        target_m=target_m,
        lift_m=max(0.0, zone.brake_m - target_m),
    )


def measure_coasting(
    trace: LapTrace,
    track: Track,
    *,
    config: LiftCoastConfig | None = None,
    thresholds: CoachingThresholds | None = None,
) -> list[CoastingMeasurement]:
    """완료 랩에서 브레이크 구간마다 코스팅 거리·시간을 잰다.

    브레이크가 문턱값 아래에서 위로 올라온 샘플을 브레이크 지점으로 보고,
    그 앞으로 스로틀이 5% 미만으로 이어진 구간의 시작을 리프트 지점으로 본다.
    브레이크를 밟지 않은 코너는 결과에 없다.
    """
    limits = config or LiftCoastConfig()
    coaching_thresholds = thresholds or default_coaching_thresholds()
    ordered = sorted(track.corners, key=lambda corner: corner.entry_m)
    samples = _clean_samples(trace)
    results: list[CoastingMeasurement] = []
    for index in range(1, len(samples)):
        previous = samples[index - 1]
        current = samples[index]
        if not (previous.brake < coaching_thresholds.brake_on <= current.brake):
            continue
        corner = _corner_for_brake(
            ordered,
            track.length_m,
            current.dist_m,
            coaching_thresholds.brake_search_back_m,
        )
        if corner is None:
            continue
        results.append(
            _coast_before(samples, index, corner, trace.lap, limits)
        )
    return results


def _coast_before(
    samples: Sequence[Sample],
    brake_index: int,
    corner: Corner,
    lap: int,
    config: LiftCoastConfig,
) -> CoastingMeasurement:
    """브레이크 지점 앞의 연속된 스로틀 오프 구간을 잰다."""
    lift_index = brake_index
    while (
        lift_index > 0
        and samples[lift_index - 1].throttle < config.lift_throttle
    ):
        lift_index -= 1
    lift = samples[lift_index]
    brake = samples[brake_index]
    return CoastingMeasurement(
        corner=corner.number,
        lap=lap,
        lift_m=lift.dist_m,
        brake_m=brake.dist_m,
        distance_m=max(0.0, brake.dist_m - lift.dist_m),
        time_ms=max(0, int(brake.t_ms) - int(lift.t_ms)),
    )


def _corner_for_brake(
    ordered: Sequence[Corner],
    track_length_m: float,
    brake_m: float,
    search_back_m: float,
) -> Corner | None:
    """브레이크 지점이 속한 코너. 랩 끝을 넘으면 첫 코너로 감는다."""
    if not ordered:
        return None
    distance = float(brake_m)
    for corner in ordered:
        if corner.entry_m >= distance:
            # 브레이크 구간은 코너 진입 앞 `search_back_m` 안에 있어야 한다.
            if corner.entry_m - distance <= search_back_m:
                return corner
            return None
    # 마지막 코너 뒤에서 브레이크를 밟았다(랩 끝을 넘어 첫 코너로 돌아온다).
    first = ordered[0]
    gap = track_length_m - distance + first.entry_m
    return first if gap <= search_back_m else None


# ---------------------------------------------------------------------------
# 공유 상태(코치가 쓰고 웹 API가 읽는다)


class LiftCoastState:
    """리프트 앤 코스트 최근 상태. 스레드 안전하게 복사본을 돌려준다."""

    def __init__(
        self,
        *,
        max_coasting: int = DEFAULT_MAX_COASTING,
        max_fuel_laps: int = DEFAULT_MAX_FUEL_LAPS,
    ) -> None:
        self._lock = threading.Lock()
        self._recommended: list[LiftZone] = []
        self._recent: deque[tuple[CoastingMeasurement, float | None, str | None]] = (
            deque(maxlen=max_coasting)
        )
        self._fuel: deque[LapFuel] = deque(maxlen=max_fuel_laps)
        self._need: dict[str, Any] = {
            "active": False,
            "reasons": [],
            "fuel_laps_margin": None,
            "brake_temp": None,
            "front_tyre_temp": None,
        }
        self._instructed: dict[str, Any] | None = None

    # --- 쓰기 -------------------------------------------------------------

    def set_recommendations(self, zones: Sequence[LiftZone]) -> None:
        with self._lock:
            self._recommended = list(zones)

    def add_measurement(
        self,
        measurement: CoastingMeasurement,
        target_m: float | None = None,
        status: str | None = None,
    ) -> None:
        with self._lock:
            self._recent.append((measurement, target_m, status))

    def add_fuel(self, record: LapFuel) -> None:
        with self._lock:
            self._fuel.append(record)

    def set_need(
        self,
        active: bool,
        reasons: Sequence[str],
        values: Mapping[str, float | None],
    ) -> None:
        with self._lock:
            self._need = {
                "active": bool(active),
                "reasons": list(reasons),
                "fuel_laps_margin": values.get("fuel_laps_margin"),
                "brake_temp": values.get("brake_temp"),
                "front_tyre_temp": values.get("front_tyre_temp"),
            }

    def set_instruction(self, lap: int, zones: Sequence[LiftZone]) -> None:
        with self._lock:
            self._instructed = {
                "lap": int(lap),
                "corners": [zone.corner for zone in zones],
                "targets_m": {zone.corner: zone.target_m for zone in zones},
            }

    def clear_instruction(self) -> None:
        with self._lock:
            self._instructed = None

    # --- 읽기 -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            need = dict(self._need)
            need["reasons"] = list(self._need["reasons"])
            recommended = [zone.to_dict() for zone in self._recommended]
            recent = [
                measurement.to_dict(target_m=target, status=status)
                for measurement, target, status in self._recent
            ]
            fuel_records = list(self._fuel)
            fuel = [record.to_dict() for record in fuel_records]
            instructed = (
                None
                if self._instructed is None
                else {
                    "lap": self._instructed["lap"],
                    "corners": list(self._instructed["corners"]),
                    # JSON 키는 문자열이므로 API 응답과 같은 모양으로 맞춘다.
                    "targets_m": {
                        str(corner): target
                        for corner, target in self._instructed["targets_m"].items()
                    },
                }
            )

        used = [
            record.used_kg
            for record in reversed(fuel_records)
            if record.used_kg is not None
        ][:5]
        last_used = used[0] if used else None
        average = sum(used) / len(used) if used else None
        return {
            "need": need,
            "recommended": recommended,
            "recent_coasting": list(reversed(recent)),
            "fuel": {
                "last_used_kg": _rounded(last_used),
                "average_used_kg": _rounded(average),
                "recent": list(reversed(fuel)),
            },
            "instructed": instructed,
        }


# ---------------------------------------------------------------------------
# 코치


@dataclass
class _Pending:
    """미뤄 둔 무전. 다음 긴 직선에서 다시 판단한다."""

    lap: int
    message: RadioMessage
    zones: tuple[LiftZone, ...] = ()


class LiftCoastCoach:
    """내 차 샘플을 받아 리프트 앤 코스트 지시·피드백 무전을 만든다.

    호출 순서(한 랩)는 `coaching.coach.Coach`와 같다.

    1. Car Telemetry 샘플마다 `on_sample(car_state, dist_m, now_ns)`.
    2. 랩이 끝나면 `on_lap_completed(trace, now_ns)`.

    `queue`를 주면 만든 무전을 바로 `RadioQueue`에 넣는다(끈 종류는 큐가 걸러
    낸다). `state`를 주면 추천·측정·연료 기록을 그 상태에 남겨 웹 API가 읽게
    한다(생략하면 내부 상태를 만들어 `coach.state`로 읽을 수 있다).
    """

    def __init__(
        self,
        track: Track,
        *,
        queue: RadioQueue | None = None,
        state: LiftCoastState | None = None,
        config: LiftCoastConfig | None = None,
        coach_config: CoachConfig | None = None,
        thresholds: CoachingThresholds | None = None,
        player_car_index: int = 0,
        session_type: int | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._track = track
        self._ordered = sorted(track.corners, key=lambda corner: corner.entry_m)
        self._config = config or LiftCoastConfig()
        self._coach_config = coach_config or CoachConfig()
        self._thresholds = thresholds or default_coaching_thresholds()
        self._queue = queue
        self._state = state or LiftCoastState(
            max_coasting=self._config.max_coasting,
            max_fuel_laps=self._config.max_fuel_laps,
        )
        self._clock = clock
        self._player_car_index = player_car_index
        self._session_type = session_type

        self._lap = 0
        self._lap_start_ns = 0
        self._last_dist: float | None = None
        self._last_sample: Sample | None = None
        self._samples: list[Sample] = []
        self._coast_by_corner: dict[int, CoastingMeasurement] = {}
        self._fed: set[int] = set()
        self._pending: list[_Pending] = []

        self._recommended: list[LiftZone] = []
        self._targets: dict[int, float] = {}
        self._fuel_laps: deque[LapFuel] = deque(
            maxlen=self._config.max_fuel_laps
        )

        # 지표 캐시. 값이 없으면 마지막으로 본 값을 유지한다(데이터 끊김으로
        # 여유가 돌아왔다고 잘못 판단하지 않게).
        self._fuel_margin: float | None = None
        self._brake_temp: float | None = None
        self._front_tyre_temp: float | None = None
        self._need_active = False
        self._need_reasons: tuple[str, ...] = ()

        # 지시 에피소드(위험 시작 → 회복).
        self._episode_instructed = False
        self._episode_start_lap: int | None = None
        self._instructed_lap: int | None = None
        self._planned_lap: int | None = None
        self._instructed_on: dict[int, int] = {}
        self._instructed_zones: dict[int, LiftZone] = {}
        self._instructed_targets: dict[int, float] = {}
        self._reinstruct = False

    # --- 조회 -------------------------------------------------------------

    @property
    def track(self) -> Track:
        return self._track

    @property
    def state(self) -> LiftCoastState:
        return self._state

    @property
    def recommendations(self) -> tuple[LiftZone, ...]:
        return tuple(self._recommended)

    @property
    def fuel_laps(self) -> tuple[LapFuel, ...]:
        return tuple(self._fuel_laps)

    @property
    def need_active(self) -> bool:
        return self._need_active

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # --- 샘플 받기 ---------------------------------------------------------

    def on_sample(
        self,
        car_state: Mapping[str, Any],
        dist_m: float | None = None,
        now_ns: int | None = None,
    ) -> list[RadioMessage]:
        """샘플 하나를 반영하고, 이 샘플에서 나가야 할 무전을 돌려준다."""
        state: Mapping[str, Any] = car_state or {}
        if not self._is_player(state):
            return []
        now = int(now_ns) if now_ns is not None else int(self._clock())
        distance = _number(
            dist_m if dist_m is not None else state.get("lap_distance")
        )
        if distance is None:
            return []
        distance = float(distance)

        lap = self._resolve_lap(state, distance)
        if lap != self._lap or self._rewound(distance):
            self._start_lap(lap, distance, now)

        messages: list[RadioMessage] = []
        for message in self._update_need(state, now):
            self._emit(message, messages)

        if not self._sampling_allowed(state, lap):
            self._samples.clear()
            self._coast_by_corner.clear()
            self._pending.clear()
            self._last_dist = distance
            self._last_sample = None
            return messages

        sample = self._make_sample(state, distance, now)
        if sample is None:
            self._last_dist = distance
            self._last_sample = None
            return messages

        previous_sample = self._last_sample
        previous_dist = self._last_dist
        self._samples.append(sample)
        self._last_sample = sample

        if (
            previous_sample is not None
            and previous_sample.brake < self._thresholds.brake_on
            <= sample.brake
            and sample.speed > 0.0
        ):
            self._record_brake_onset(sample)

        if self._pending:
            messages.extend(self._release_pending(sample, now))

        if previous_dist is not None and distance >= previous_dist:
            messages.extend(
                self._handle_exits(previous_dist, distance, sample, state, now)
            )

        self._maybe_instruct(state, sample, now, messages)
        self._last_dist = distance
        return messages

    def on_lap_completed(
        self, trace: LapTrace, now_ns: int | None = None
    ) -> list[RadioMessage]:
        """완료 랩을 연료 기록·추천에 반영한다.

        피드백 무전은 코너 탈출 직후(`on_sample`)에 이미 나갔다. 여기서는 다음
        랩에 다시 지시할지(`_reinstruct`)만 정한다.
        """
        if trace.car_index != self._player_car_index:
            return []
        self._record_fuel(trace)
        self._update_recommendations(trace)
        self._update_reinstruct(trace)
        return []

    # --- 세션·랩 상태 ------------------------------------------------------

    def _is_player(self, state: Mapping[str, Any]) -> bool:
        is_player = state.get("is_player")
        if is_player is not None:
            return bool(is_player)
        car_index = _number(state.get("car_index"))
        if car_index is None:
            return True
        return int(car_index) == self._player_car_index

    def _resolve_lap(self, state: Mapping[str, Any], distance: float) -> int:
        explicit = state.get("lap_num", state.get("current_lap_num"))
        lap = _number(explicit)
        if lap is not None:
            return int(lap)
        if self._lap == 0:
            return 1
        if self._rewound(distance):
            return self._lap + 1
        return self._lap

    def _rewound(self, distance: float) -> bool:
        return (
            self._last_dist is not None
            and distance + LAP_DISTANCE_TOLERANCE_M < self._last_dist
        )

    def _start_lap(self, lap: int, distance: float, now_ns: int) -> None:
        self._lap = lap
        self._lap_start_ns = now_ns
        self._last_dist = distance
        self._last_sample = None
        self._samples = []
        self._coast_by_corner = {}
        self._fed = set()
        # 앞 랩에서 미룬 무전은 그 랩 안에 못 했으므로 버린다.
        self._pending.clear()
        self._planned_lap = None

    def _session_type_of(self, state: Mapping[str, Any]) -> int | None:
        value = _number(state.get("session_type"))
        if value is not None:
            self._session_type = int(value)
        return self._session_type

    def _is_race(self, state: Mapping[str, Any]) -> bool:
        session_type = self._session_type_of(state)
        return session_type in _RACE_TYPES

    def _sampling_allowed(
        self, state: Mapping[str, Any], lap: int
    ) -> bool:
        """아웃랩·피트·무효·SC/VSC에서는 재지 않는다(리뷰 포커스 4)."""
        if lap < 1:
            return False
        if "lap_valid" in state and not state["lap_valid"]:
            return False
        if state.get("invalid_reason"):
            return False
        if state.get("in_pit") or state.get("pit"):
            return False
        if state.get("safety_car") or state.get("vsc"):
            return False
        driver_status = _number(state.get("driver_status"))
        if driver_status in {
            DRIVER_IN_LAP,
            DRIVER_OUT_LAP,
            DRIVER_OFF_TRACK,
            DRIVER_IN_PIT,
        }:
            return False
        if _number(state.get("pit_status")) in PIT_LANE_STATUSES:
            return False
        return True

    def _messages_allowed(self, state: Mapping[str, Any], lap: int) -> bool:
        """무전은 레이스 세션에서만, 레이스 1랩(출발 혼전)은 뺀다."""
        if not self._is_race(state):
            return False
        if lap == 1:
            return False
        return self._sampling_allowed(state, lap)

    def _make_sample(
        self, state: Mapping[str, Any], distance: float, now_ns: int
    ) -> Sample | None:
        speed = _number(state.get("speed"))
        throttle = _number(state.get("throttle"))
        brake = _number(state.get("brake"))
        if speed is None or throttle is None or brake is None:
            return None
        t_ms = _number(
            state.get(
                "t_ms",
                state.get("lap_time_ms", state.get("current_lap_time_in_ms")),
            )
        )
        if t_ms is None:
            t_ms = max(0.0, (now_ns - self._lap_start_ns) / 1_000_000.0)
        return Sample(
            dist_m=distance,
            t_ms=int(t_ms),
            speed=float(speed),
            throttle=float(throttle),
            brake=float(brake),
            steer=_number(state.get("steer"), 0.0) or 0.0,
            gear=int(_number(state.get("gear"), 0.0) or 0),
            x=_number(state.get("x"), 0.0) or 0.0,
            z=_number(state.get("z"), 0.0) or 0.0,
            fuel_kg=_number(state.get("fuel_kg")),
        )

    # --- 측정 -------------------------------------------------------------

    def _record_brake_onset(self, sample: Sample) -> None:
        corner = self._corner_for_brake(sample.dist_m)
        if corner is None:
            return
        measurement = _coast_before(
            self._samples, len(self._samples) - 1, corner, self._lap, self._config
        )
        self._coast_by_corner[corner.number] = measurement

    def _corner_for_brake(self, brake_m: float) -> Corner | None:
        return _corner_for_brake(
            self._ordered,
            self._track.length_m,
            brake_m,
            self._thresholds.brake_search_back_m,
        )

    def _handle_exits(
        self,
        previous: float,
        distance: float,
        sample: Sample,
        car_state: Mapping[str, Any],
        now_ns: int,
    ) -> list[RadioMessage]:
        messages: list[RadioMessage] = []
        allowed = self._messages_allowed(car_state, self._lap)
        for corner in self._ordered:
            if corner.number in self._fed:
                continue
            if not (previous < corner.exit_m <= distance):
                continue
            self._fed.add(corner.number)
            measurement = self._coast_by_corner.get(corner.number)
            instructed_lap = self._instructed_on.get(corner.number)
            target = self._instructed_targets.get(corner.number)
            status: str | None = None
            if measurement is not None:
                status = (
                    None
                    if target is None
                    else (
                        "good"
                        if measurement.distance_m
                        + self._config.good_tolerance_m
                        >= target
                        else "short"
                    )
                )
                self._state.add_measurement(
                    measurement,
                    target if target is not None else self._targets.get(corner.number),
                    status,
                )
            if (
                allowed
                and measurement is not None
                and instructed_lap is not None
                and instructed_lap + 1 == self._lap
                and target is not None
            ):
                message = self._feedback_message(
                    corner, measurement, target, sample, now_ns
                )
                if message is not None:
                    self._pending_or_emit(message, sample, now_ns, messages)
        return messages

    def _feedback_message(
        self,
        corner: Corner,
        measurement: CoastingMeasurement,
        target_m: float,
        sample: Sample,
        now_ns: int,
    ) -> RadioMessage | None:
        short_m = target_m - measurement.distance_m
        if short_m > self._config.good_tolerance_m:
            meters = max(1, int(round(short_m)))
            text = render_corner(
                "coaching.fuel_lift_short",
                corner.number,
                official=corner.official,
                meters=meters,
            )
            kind = RadioKind.COACHING.value
            status = "short"
        else:
            text = render_corner(
                "improvement.fuel_lift_good",
                corner.number,
                official=corner.official,
            )
            kind = RadioKind.IMPROVEMENT.value
            status = "good"

        evidence = {
            "corner": corner.number,
            "lap": self._lap,
            "measured_m": round(measurement.distance_m, 1),
            "target_m": round(target_m, 1),
            "short_m": round(max(0.0, short_m), 1),
            "coast_time_ms": int(measurement.time_ms),
            "status": status,
            "corner_official": corner.official,
        }
        return RadioMessage.create(
            kind,
            text,
            now_ns=now_ns,
            ttl_ns=self._ttl_to_lift(corner.number, sample),
            evidence=evidence,
            lap=self._lap,
            corner=corner.number,
        )

    # --- 지시 -------------------------------------------------------------

    def _update_need(
        self, state: Mapping[str, Any], now_ns: int
    ) -> list[RadioMessage]:
        """위험 지표를 갱신하고, 여유가 돌아오면 "리프트 그만"을 한 번 낸다."""
        fuel = _number(state.get("fuel_laps_margin"))
        if fuel is None:
            remaining = _number(state.get("fuel_remaining_laps"))
            race_laps = _number(state.get("remaining_race_laps"))
            if remaining is not None and race_laps is not None:
                fuel = remaining - race_laps
        if fuel is not None:
            self._fuel_margin = fuel

        brake = _number(state.get("brake_temp"))
        if brake is None:
            brake = _max_number(state.get("brake_temps"))
        if brake is not None:
            self._brake_temp = brake

        tyre = _max_front_tyre_temp(state)
        if tyre is not None:
            self._front_tyre_temp = tyre

        severities = {
            "fuel_laps_margin": severity("fuel_laps_margin", self._fuel_margin),
            "brake_temp": severity("brake_temp", self._brake_temp),
            "tyre_temp": severity("tyre_temp", self._front_tyre_temp),
        }
        reasons = tuple(
            name
            for name in ("fuel_laps_margin", "brake_temp", "tyre_temp")
            if severities[name] in {Severity.WARN, Severity.DANGER}
        )
        active = bool(reasons)
        was_active = self._need_active
        self._need_active = active
        self._need_reasons = reasons
        self._state.set_need(
            active,
            reasons,
            {
                "fuel_laps_margin": _rounded(self._fuel_margin),
                "brake_temp": _rounded(self._brake_temp),
                "front_tyre_temp": _rounded(self._front_tyre_temp),
            },
        )

        messages: list[RadioMessage] = []
        if (
            was_active
            and not active
            and self._episode_instructed
            and self._is_race(state)
        ):
            messages.append(self._stop_message(now_ns))
            self._episode_instructed = False
            self._episode_start_lap = None
            self._instructed_on.clear()
            self._instructed_zones.clear()
            self._instructed_targets.clear()
            self._reinstruct = False
            self._state.clear_instruction()
        return messages

    def _stop_message(self, now_ns: int) -> RadioMessage:
        laps = (
            None
            if self._episode_start_lap is None
            else max(1, self._lap - self._episode_start_lap + 1)
        )
        evidence = {
            "lap": self._lap,
            "episode_start_lap": self._episode_start_lap,
            "episode_laps": laps,
            "fuel_laps_margin": _rounded(self._fuel_margin),
            "brake_temp": _rounded(self._brake_temp),
            "front_tyre_temp": _rounded(self._front_tyre_temp),
        }
        return RadioMessage.create(
            RadioKind.IMPROVEMENT.value,
            render("improvement.fuel_lift_stop"),
            now_ns=now_ns,
            ttl_ns=int(self._config.stop_ttl_s * 10**9),
            evidence=evidence,
            lap=self._lap,
        )

    def _maybe_instruct(
        self,
        car_state: Mapping[str, Any],
        sample: Sample,
        now_ns: int,
        collected: list[RadioMessage],
    ) -> None:
        if not self._need_active or not self._recommended:
            return
        if not self._messages_allowed(car_state, self._lap):
            return
        if self._instructed_lap == self._lap or self._planned_lap == self._lap:
            return
        if self._episode_instructed and not self._reinstruct:
            return

        zones = tuple(self._recommended)
        message = self._instruction_message(zones, sample, now_ns)
        if message is None:
            return
        # 미뤄 둔 동안 같은 랩에 또 만들지 않는다.
        self._planned_lap = self._lap
        self._pending_or_emit(message, sample, now_ns, collected, zones=zones)

    def _instruction_message(
        self, zones: Sequence[LiftZone], sample: Sample, now_ns: int
    ) -> RadioMessage | None:
        if not zones:
            return None
        corners = ", ".join(zone.label for zone in zones)
        meters = max(1, int(round(min(zone.target_m for zone in zones))))
        reason = self._need_reasons[0] if self._need_reasons else ""
        if reason == "fuel_laps_margin":
            margin = self._fuel_margin or 0.0
            laps = max(1, int(round(abs(margin))))
            text = render(
                "coaching.fuel_lift", laps=laps, corners=corners, meters=meters
            )
        elif reason == "brake_temp":
            text = render(
                "coaching.brake_temp_lift",
                corners=corners,
                meters=meters,
                celsius=int(round(self._brake_temp or 0.0)),
            )
        else:
            text = render(
                "coaching.tyre_temp_lift",
                corners=corners,
                meters=meters,
                celsius=int(round(self._front_tyre_temp or 0.0)),
            )

        evidence = {
            "reason": reason,
            "reasons": list(self._need_reasons),
            "fuel_laps_margin": _rounded(self._fuel_margin),
            "brake_temp": _rounded(self._brake_temp),
            "front_tyre_temp": _rounded(self._front_tyre_temp),
            "corners": [zone.corner for zone in zones],
            "targets_m": {zone.corner: zone.target_m for zone in zones},
            "meters": meters,
            "lap": self._lap,
        }
        return RadioMessage.create(
            RadioKind.COACHING.value,
            text,
            now_ns=now_ns,
            ttl_ns=self._instruction_ttl(zones, sample),
            evidence=evidence,
            lap=self._lap,
            corner=zones[0].corner if len(zones) == 1 else None,
        )

    def _instruction_ttl(self, zones: Sequence[LiftZone], sample: Sample) -> int:
        """가장 가까운 추천 구간의 리프트 지점 전까지(설계 6.7)."""
        length = self._track.length_m
        if length <= 0:
            return int(MIN_INSTRUCTION_TTL_S * 10**9)
        gaps = [_wrap_gap(zone.lift_m, sample.dist_m, length) for zone in zones]
        gap = min(gaps) if gaps else length
        speed_mps = max(sample.speed / 3.6, 1.0)
        seconds = gap / speed_mps
        seconds = min(max(seconds, MIN_INSTRUCTION_TTL_S), MAX_INSTRUCTION_TTL_S)
        return int(seconds * 10**9)

    def _ttl_to_lift(self, corner: int, sample: Sample) -> int:
        zone = self._instructed_zones.get(corner)
        target = zone.lift_m if zone is not None else None
        length = self._track.length_m
        if target is None or length <= 0:
            return int(MIN_INSTRUCTION_TTL_S * 10**9)
        gap = _wrap_gap(target, sample.dist_m, length)
        speed_mps = max(sample.speed / 3.6, 1.0)
        seconds = min(
            max(gap / speed_mps, MIN_INSTRUCTION_TTL_S), MAX_INSTRUCTION_TTL_S
        )
        return int(seconds * 10**9)

    def _mark_instructed(self, zones: Sequence[LiftZone]) -> None:
        self._episode_instructed = True
        if self._episode_start_lap is None:
            self._episode_start_lap = self._lap
        self._instructed_lap = self._lap
        self._instructed_on = {zone.corner: self._lap for zone in zones}
        self._instructed_zones = {zone.corner: zone for zone in zones}
        self._instructed_targets = {zone.corner: zone.target_m for zone in zones}
        self._reinstruct = False
        self._state.set_instruction(self._lap, zones)

    # --- 미룸 -------------------------------------------------------------

    def _pending_or_emit(
        self,
        message: RadioMessage,
        sample: Sample,
        now_ns: int,
        collected: list[RadioMessage],
        *,
        zones: Sequence[LiftZone] = (),
    ) -> None:
        decision = defer_until_straight(
            sample.dist_m,
            sample.speed,
            self._track.corners,
            self._track.length_m,
            defer_s=self._coach_config.defer_s,
            brake_lead_m=self._coach_config.brake_lead_m,
        )
        if decision.defer:
            self._pending.append(
                _Pending(lap=self._lap, message=message, zones=tuple(zones))
            )
            return
        if self._emit(message, collected) and zones:
            self._mark_instructed(zones)

    def _release_pending(
        self, sample: Sample, now_ns: int
    ) -> list[RadioMessage]:
        released: list[RadioMessage] = []
        remaining: list[_Pending] = []
        decision = defer_until_straight(
            sample.dist_m,
            sample.speed,
            self._track.corners,
            self._track.length_m,
            defer_s=self._coach_config.defer_s,
            brake_lead_m=self._coach_config.brake_lead_m,
        )
        for pending in self._pending:
            if pending.lap != self._lap:
                continue
            if pending.message.expired(now_ns):
                continue
            if decision.defer:
                remaining.append(pending)
                continue
            if self._emit(pending.message, released) and pending.zones:
                self._mark_instructed(pending.zones)
        self._pending = remaining
        return released

    # --- 랩 마감 -----------------------------------------------------------

    def _record_fuel(self, trace: LapTrace) -> None:
        values = [
            sample.fuel_kg
            for sample in trace.samples
            if sample.fuel_kg is not None
        ]
        if len(values) < 2:
            return
        record = LapFuel(
            lap=trace.lap,
            start_kg=values[0],
            end_kg=values[-1],
            used_kg=values[0] - values[-1],
        )
        self._fuel_laps.append(record)
        self._state.add_fuel(record)

    def _update_recommendations(self, trace: LapTrace) -> None:
        if not trace.valid or len(trace.samples) < 2:
            return
        # 레이스 1랩(출발 혼전)은 코칭·기준 랩에서 빼는 규칙을 추천에도 적용한다
        # (리뷰 포커스 4). 다음 유효 랩부터 추천한다.
        if self._session_type in _RACE_TYPES and trace.lap == 1:
            return
        zones = rank_lift_zones(
            self._track,
            trace,
            config=self._config,
            thresholds=self._thresholds,
        )
        if not zones:
            return
        self._recommended = zones
        self._targets = {zone.corner: zone.target_m for zone in zones}
        self._state.set_recommendations(zones)

    def _update_reinstruct(self, trace: LapTrace) -> None:
        """방금 끝난 랩이 지시 다음 랩이면, 못 지켰을 때 다음 랩에 다시 지시한다."""
        instructed = {
            corner: lap
            for corner, lap in self._instructed_on.items()
            if lap + 1 == trace.lap
        }
        short = False
        for corner in instructed:
            measurement = self._coast_by_corner.get(corner)
            target = self._instructed_targets.get(corner)
            if measurement is None or target is None:
                short = True
                continue
            if measurement.distance_m + self._config.good_tolerance_m < target:
                short = True
        self._reinstruct = bool(instructed) and short and self._need_active
        # 오래된 지시 기록은 버린다.
        self._instructed_on = {
            corner: lap
            for corner, lap in self._instructed_on.items()
            if trace.lap - lap <= 2
        }

    # --- 내보내기 ---------------------------------------------------------

    def _emit(
        self, message: RadioMessage, collected: list[RadioMessage]
    ) -> bool:
        if self._queue is not None and not self._queue.push(message):
            return False
        collected.append(message)
        return True


# ---------------------------------------------------------------------------
# 도우미


def _wrap_gap(target_m: float, dist_m: float, length_m: float) -> float:
    gap = (target_m - dist_m) % length_m
    if gap <= 1e-9:
        gap = length_m
    return gap


def _speed_points(trace: LapTrace) -> list[tuple[float, float]]:
    points = sorted(
        (
            (sample.dist_m, sample.speed)
            for sample in trace.samples
            if math.isfinite(sample.dist_m) and math.isfinite(sample.speed)
        ),
        key=lambda item: item[0],
    )
    return points


def _speed_at(points: Sequence[tuple[float, float]], dist_m: float) -> float | None:
    if not points:
        return None
    distances = [point[0] for point in points]
    index = bisect.bisect_left(distances, dist_m)
    if index <= 0:
        return points[0][1]
    if index >= len(points):
        return points[-1][1]
    previous = points[index - 1]
    current = points[index]
    span = current[0] - previous[0]
    if span <= 0.0:
        return current[1]
    fraction = (dist_m - previous[0]) / span
    return previous[1] + fraction * (current[1] - previous[1])


def _clean_samples(trace: LapTrace) -> list[Sample]:
    samples = [
        sample
        for sample in trace.samples
        if all(
            math.isfinite(value)
            for value in (sample.dist_m, sample.speed, sample.throttle, sample.brake)
        )
    ]
    samples.sort(key=lambda sample: sample.dist_m)
    return samples


def _max_number(values: Any) -> float | None:
    if not isinstance(values, (list, tuple)):
        return None
    numbers = [
        float(value)
        for value in values
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    ]
    return max(numbers) if numbers else None


def _max_front_tyre_temp(state: Mapping[str, Any]) -> float | None:
    direct = _number(state.get("tyre_temp"))
    if direct is not None:
        return direct
    values = state.get("tyre_temps")
    if not isinstance(values, (list, tuple)) or len(values) < 4:
        return None
    # 공식 패킷 순서: 뒤왼쪽, 뒤오른쪽, 앞왼쪽, 앞오른쪽. 앞은 2·3번이다.
    fronts = [
        float(value)
        for value in (values[2], values[3])
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ]
    return max(fronts) if fronts else None


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


__all__ = [
    "CoastingMeasurement",
    "LapFuel",
    "LiftCoastCoach",
    "LiftCoastConfig",
    "LiftCoastState",
    "LiftZone",
    "coast_target_m",
    "measure_coasting",
    "rank_lift_zones",
]
