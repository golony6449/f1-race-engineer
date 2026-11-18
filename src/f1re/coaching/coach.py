"""코너 탈출 직후 코칭 무전 결정(작업 16, SCRUM-66).

설계 6.4의 "언제·무엇을 말하나"를 구현한다.

- 코너 탈출 지점(랩 거리 `Corner.exit_m`)을 처음 지나는 샘플에서 그 코너를 판단한다.
  집중 코너면 손실이 작아도 근거가 있으면 말하고, 아니면 손실이 `big_loss_ms`
  (기본 0.3초) 이상이거나 트랙을 벗어났을 때만 말한다. 단, 코너 손실이 0.1초
  미만이면 집중 코너여도 말하지 않는다(SCRUM-124, 아래 참고).
- 원인은 하나만 고른다(`pick_cause`). 트랙 이탈 > 브레이크 > 턴인 > 조향 수정 >
  정점 속도 > 스로틀 > 라인 순으로, 문턱값을 넘는 첫 원인을 쓴다. 근거가 약하면
  아무 말도 하지 않는다("도움 안 되는 말" 금지, 설계 2장).
- **민감도와 손실 하한**(SCRUM-124): 브레이크·턴인 지점 차이 문턱은 8m라 3~6m
  차이로는 말하지 않는다. 원인 문턱을 넘어도 코너 손실이
  `min_corner_loss_ms`(100ms) 미만이면 말하지 않는다. 문턱 기본값은
  `config/default.toml`의 `[coaching]`에서 읽는다.
- **접전 코너 제외**(SCRUM-124): 레이스 세션에서 코너 구간 중 앞/뒤차와 1초
  이내였거나 순위가 바뀐 코너는 코칭하지 않는다. 접전 중의 속도·조향·라인
  차이는 내 실수가 아니기 때문이다. 비레이스 세션에는 적용하지 않는다.
- 코칭 무전은 코너 탈출 직후 곧바로 큐에 넣는다(SCRUM-94, 사용자). 다음 직선으로
  미루지 않는다. 코칭 무전의 5초 유효 시간과 "새 코칭이 오면 옛 코칭 버리기"는
  `radio.queue.RadioQueue`가 맡는다. `defer_until_straight`는 리프트 앤 코스트
  (SCRUM-71)가 개선 알림을 미룰 때 그대로 쓴다.
- **연속 코너 순서 보정**(SCRUM-143): 코너 탈출을 지나 코칭을 만들 시점에
  이미 다음 코너 진입을 넘었다면(패킷 끊김·큰 거리 점프) 그 코칭은 만들지
  않는다. 또 코너 탈출을 하나라도 새로 지날 때마다 아직 재생 전인 옛 코칭을
  버려, "3번" 무전이 5번 구간에서 나가지 않게 한다. 코치가 받는 랩 거리는
  `Track` 코너 라벨과 같은 프레임이어야 하며, 실제 게임 세션은 파이프라인이
  `Track.in_lap_distance_frame()` 사본을 넘긴다.
- **개선 알림 적시성**(SCRUM-143): 랩 완료 개선 알림은 한 랩에 하나만 내고,
  큐 우선순위상 코너 코칭·추월보다 먼저 나간다. 만든 뒤 1번 코너 진입 전에
  말을 시작하지 못하면(유효 시간 만료 또는 1번 코너 진입 감지) 버려 엉뚱한
  코너에서 나가지 않게 한다. 랩 처리 큐가 밀려 다음 랩에서 이미 1번 코너를
  지난 뒤 완료 랩을 처리한 경우에도 개선 알림을 내지 않는다.
- 같은 랩에 같은 코너는 한 번만 말한다. 무효 랩·피트·SC/VSC·레이스 1랩은
  `car_state` 플래그로 받아 코칭하지 않는다(리뷰 포커스 4).
- **반복 실수 강조**(SCRUM-125): 코너별로 직전 코칭 원인과 랩을 기억해, 같은
  코너·같은 원인이 `REPEAT_STREAK_LAPS`(3)랩 연속이면 일반 코칭 틀 대신
  `coaching.repeat_*` 틀을 강한 어조(urgent, 마지막 3랩이면 hyped)로 쓴다.
  4랩 이상 이어져도 매 랩 다시 강조하며 연속 횟수를 근거(`evidence["streak"]`)에
  남긴다. 다른 원인이 나오거나 그 코너가 개선돼 코칭 조건을 넘지 못하면 횟수를
  0으로 되돌리고, 랩을 건너뛰면(무효 랩·피트·SC 등) 랩 번호 차이로 자동으로
  끊긴다.
- 집중 코너 목록 변화(개선)는 `on_lap_completed()`가 `FocusSet`에서 받아
  "3번 좋아졌어. 다음은 7번 보자." 무전으로 만든다.
- 감정 단계(SCRUM-91): 레이스 마지막 3랩이면 `hyped`, 개선 알림은 `warm`,
  그 밖의 코칭은 `calm`이다. 고른 단계는 근거(`evidence["mood"]`)에 남기고,
  같은 틀·단계가 연달아 나가면 직전 표현을 피한다(`VariantRotator`).

`on_sample(car_state, dist_m, now_ns)`의 `car_state`는 다음 키를 갖는 매핑이다
(모두 선택이며, 없으면 그 샘플은 건너뛴다).

- `speed`(km/h), `throttle`·`brake`·`steer`(0~1), `gear`
- `t_ms`(또는 `lap_time_ms`, `current_lap_time_in_ms`): 랩 시작 기준 ms.
  없으면 `now_ns` 차이로 만든다.
- `lap_num`(또는 `current_lap_num`): 없으면 랩 거리가 되감길 때 랩을 센다.
- `x`, `z`: 라인 이탈 판정용. 없으면 0으로 두어 라인 지표를 쓰지 않는다.
- `surface`: Car Telemetry의 바퀴별 노면 4개(RL·RR·FL·FR). 없으면 트랙 이탈을
  판단하지 않는다(옛 입력·데모 호환, SCRUM-93).
- `lap_valid`(기본 True), `invalid_reason`, `in_pit`, `safety_car`, `vsc`,
  `session_type`
- `car_index`/`is_player`: 내 차가 아니면 아무것도 하지 않는다.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from f1re.coaching.focus import (
    FocusChange,
    FocusConfig,
    FocusReason,
    FocusSet,
    corner_loss_ms,
    lap_time_points,
)
from f1re.coaching.metrics import (
    CoachingThresholds,
    CornerMetrics,
    corner_metrics,
    default_coaching_thresholds,
)
from f1re.coaching.reference import ReferenceSet, trace_outline
from f1re.config import AppConfig, CoachingSensitivityConfig
from f1re.laps.traces import (
    DRIVER_IN_LAP,
    DRIVER_IN_PIT,
    DRIVER_OFF_TRACK,
    DRIVER_OUT_LAP,
    LAP_DISTANCE_TOLERANCE_M,
    PIT_LANE_STATUSES,
    LapTrace,
    Sample,
    corner_has_traffic,
)
from f1re.race_distance import remaining_race_laps
from f1re.radio.queue import (
    COACHING_TTL_NS,
    DROP_REASON_STALE,
    RadioKind,
    RadioMessage,
    RadioQueue,
)
from f1re.udp.session_category import is_race_session
from f1re.radio.templates import (
    ORDINAL_KEYS,
    PRELOAD_KEYS,
    PRELOAD_WITH_SLOTS,
    Mood,
    VariantRotator,
    all_phrases,
    corner_label,
    is_last_laps,
    mood_for,
)
from f1re.tracks.model import Corner, Track
from f1re.tracks.outline import TrackOutline

# ---------------------------------------------------------------------------
# 원인 고르기 규칙(SCRUM-124로 민감도를 낮췄다)
#
# 기본값은 `config/default.toml`의 `[coaching]`과 같다. 실제 값은 `Coach`가
# 설정에서 받은 `CoachingSensitivityConfig`를 `pick_cause`에 넘겨 쓴다.
# 아래 상수는 그 기본값을 밖에서 참고할 수 있게 노출한 것이다.

# 브레이크·턴인 지점 차이 문턱(m). 3~6m 차이는 실제 레이스에서 대부분 0.1초
# 미만의 손실이었고(녹화 분석), 그런 사소한 오차로 말하지 않도록 8m를 쓴다.
MIN_BRAKE_DIFF_M = CoachingSensitivityConfig().brake_diff_m
MIN_TURN_IN_DIFF_M = CoachingSensitivityConfig().turn_in_diff_m
# 정점(최저) 속도 차이 문턱(km/h).
MIN_MIN_SPEED_DIFF_KMH = CoachingSensitivityConfig().min_speed_diff_kmh
# 스로틀을 다시 밟은 지점 차이 문턱(m).
MIN_THROTTLE_DIFF_M = CoachingSensitivityConfig().throttle_diff_m
# 조향 수정 횟수 차이 문턱(회).
MIN_STEER_CORRECTIONS = CoachingSensitivityConfig().steer_corrections
# 기준 궤적에서 더 벗어난 거리 문턱(m).
MIN_LINE_DEV_M = CoachingSensitivityConfig().line_dev_m
# 코너 손실이 이보다 작으면(ms) 원인 문턱을 넘어도 코칭하지 않는다.
MIN_CORNER_LOSS_MS = CoachingSensitivityConfig().min_corner_loss_ms

# 무전 템플릿 키.
CAUSE_BRAKE_LATE = "brake_late"
CAUSE_BRAKE_EARLY = "brake_early"
CAUSE_TURN_IN_EARLY = "turn_in_early"
CAUSE_TURN_IN_LATE = "turn_in_late"
CAUSE_STEER_CORRECTIONS = "steer_corrections"
CAUSE_MIN_SPEED_LOW = "min_speed_low"
CAUSE_LATE_THROTTLE = "late_throttle"
CAUSE_LINE_WIDE = "line_wide"
CAUSE_OFF_TRACK = "off_track"

_CAUSE_TEMPLATES: dict[str, str] = {
    CAUSE_BRAKE_LATE: "coaching.brake_late",
    CAUSE_BRAKE_EARLY: "coaching.brake_early",
    CAUSE_TURN_IN_EARLY: "coaching.turn_in_early",
    CAUSE_TURN_IN_LATE: "coaching.turn_in_late",
    CAUSE_STEER_CORRECTIONS: "coaching.steer_corrections",
    CAUSE_MIN_SPEED_LOW: "coaching.min_speed_low",
    CAUSE_LATE_THROTTLE: "coaching.late_throttle",
    CAUSE_LINE_WIDE: "coaching.line_wide",
    CAUSE_OFF_TRACK: "coaching.off_track",
}

# 같은 코너·같은 원인이 몇 랩 연속이면 반복 실수 강조 틀을 쓰는가(SCRUM-125).
REPEAT_STREAK_LAPS = 3

# 랩 완료 개선 알림은 한 랩에 하나만 낸다(SCRUM-143). 여러 코너가 한꺼번에
# 좋아져도 메인 스트레이트에서 다 읽을 수 없고, 뒤 문장이 1번 코너를 넘겨
# 엉뚱한 코너에서 나가게 된다.
MAX_IMPROVEMENTS_PER_LAP = 1

# 원인 → 반복 실수 강조 틀. 3랩째부터 이 틀을 강한 어조(urgent/hyped)로 쓴다.
_REPEAT_TEMPLATES: dict[str, str] = {
    CAUSE_BRAKE_LATE: "coaching.repeat_brake_late",
    CAUSE_BRAKE_EARLY: "coaching.repeat_brake_early",
    CAUSE_TURN_IN_EARLY: "coaching.repeat_turn_in_early",
    CAUSE_TURN_IN_LATE: "coaching.repeat_turn_in_late",
    CAUSE_STEER_CORRECTIONS: "coaching.repeat_steer_corrections",
    CAUSE_MIN_SPEED_LOW: "coaching.repeat_min_speed_low",
    CAUSE_LATE_THROTTLE: "coaching.repeat_late_throttle",
    CAUSE_LINE_WIDE: "coaching.repeat_line_wide",
    CAUSE_OFF_TRACK: "coaching.repeat_off_track",
}


@dataclass(frozen=True)
class Cause:
    """코너 손실의 가장 큰 원인 하나.

    `slot`·`value`는 템플릿에 넣을 수치다(예: `meters` 15, `count` 3, `kmh` 7).
    """

    key: str
    amount: float
    unit: str
    slot: str
    value: int
    detail: dict[str, float] = field(default_factory=dict)

    @property
    def template_key(self) -> str:
        return _CAUSE_TEMPLATES[self.key]


def pick_cause(
    metrics: CornerMetrics,
    reference: CornerMetrics,
    sensitivity: CoachingSensitivityConfig | None = None,
) -> Cause | None:
    """지표 두 개를 비교해 시간 손실에 가장 크게 작용한 원인 하나를 고른다.

    설계 6.4의 예("브레이크가 늦고 정점 라인이 밖으로 밀렸으면 브레이크 늦음")처럼
    여러 원인이 겹치면 앞 순서의 원인을 쓴다. 문턱을 넘는 원인이 없으면 `None`.
    `sensitivity`를 주면 설정 파일의 문턱을 쓴다(SCRUM-124).
    """
    limits = sensitivity or CoachingSensitivityConfig()
    # 트랙 이탈은 그 자체로 가장 큰 사고이므로 맨 앞에 둔다. 무전은 거리(미터)를
    # 말하지 않고 바퀴 4개가 모두 나갔다는 사실만 전한다(SCRUM-93).
    if metrics.off_track:
        return Cause(
            key=CAUSE_OFF_TRACK,
            amount=4.0,
            unit="count",
            slot="count",
            value=4,
            detail={
                "line_dev_m": round(metrics.max_line_dev_m, 1),
                "reference_line_dev_m": round(reference.max_line_dev_m, 1),
            },
        )

    if metrics.brake_start_m is not None and reference.brake_start_m is not None:
        diff = metrics.brake_start_m - reference.brake_start_m
        if diff >= limits.brake_diff_m:
            meters = max(1, int(round(diff)))
            return Cause(
                key=CAUSE_BRAKE_LATE,
                amount=float(meters),
                unit="m",
                slot="meters",
                value=meters,
                detail={
                    "brake_start_m": round(metrics.brake_start_m, 1),
                    "reference_brake_start_m": round(reference.brake_start_m, 1),
                },
            )
        if diff <= -limits.brake_diff_m:
            meters = max(1, int(round(-diff)))
            return Cause(
                key=CAUSE_BRAKE_EARLY,
                amount=float(meters),
                unit="m",
                slot="meters",
                value=meters,
                detail={
                    "brake_start_m": round(metrics.brake_start_m, 1),
                    "reference_brake_start_m": round(reference.brake_start_m, 1),
                },
            )

    if metrics.turn_in_m is not None and reference.turn_in_m is not None:
        diff = metrics.turn_in_m - reference.turn_in_m
        if diff <= -limits.turn_in_diff_m:
            meters = max(1, int(round(-diff)))
            return Cause(
                key=CAUSE_TURN_IN_EARLY,
                amount=float(meters),
                unit="m",
                slot="meters",
                value=meters,
                detail={
                    "turn_in_m": round(metrics.turn_in_m, 1),
                    "reference_turn_in_m": round(reference.turn_in_m, 1),
                },
            )
        if diff >= limits.turn_in_diff_m:
            meters = max(1, int(round(diff)))
            return Cause(
                key=CAUSE_TURN_IN_LATE,
                amount=float(meters),
                unit="m",
                slot="meters",
                value=meters,
                detail={
                    "turn_in_m": round(metrics.turn_in_m, 1),
                    "reference_turn_in_m": round(reference.turn_in_m, 1),
                },
            )

    extra_corrections = metrics.steer_corrections - reference.steer_corrections
    if extra_corrections >= limits.steer_corrections:
        return Cause(
            key=CAUSE_STEER_CORRECTIONS,
            amount=float(extra_corrections),
            unit="count",
            slot="count",
            value=int(extra_corrections),
            detail={
                "steer_corrections": float(metrics.steer_corrections),
                "reference_steer_corrections": float(reference.steer_corrections),
                "steer_peak": round(metrics.steer_peak, 3),
            },
        )

    speed_drop = reference.min_speed - metrics.min_speed
    if speed_drop >= limits.min_speed_diff_kmh:
        kmh = max(1, int(round(speed_drop)))
        return Cause(
            key=CAUSE_MIN_SPEED_LOW,
            amount=float(kmh),
            unit="km/h",
            slot="kmh",
            value=kmh,
            detail={
                "min_speed": round(metrics.min_speed, 1),
                "reference_min_speed": round(reference.min_speed, 1),
            },
        )

    if (
        metrics.throttle_on_m is not None
        and reference.throttle_on_m is not None
    ):
        diff = metrics.throttle_on_m - reference.throttle_on_m
        if diff >= limits.throttle_diff_m:
            meters = max(1, int(round(diff)))
            return Cause(
                key=CAUSE_LATE_THROTTLE,
                amount=float(meters),
                unit="m",
                slot="meters",
                value=meters,
                detail={
                    "throttle_on_m": round(metrics.throttle_on_m, 1),
                    "reference_throttle_on_m": round(reference.throttle_on_m, 1),
                    "exit_speed": round(metrics.exit_speed, 1),
                },
            )

    extra_line = metrics.max_line_dev_m - reference.max_line_dev_m
    if extra_line >= limits.line_dev_m:
        meters = max(1, int(round(extra_line)))
        return Cause(
            key=CAUSE_LINE_WIDE,
            amount=float(meters),
            unit="m",
            slot="meters",
            value=meters,
            detail={
                "line_dev_m": round(metrics.max_line_dev_m, 1),
                "reference_line_dev_m": round(reference.max_line_dev_m, 1),
            },
        )

    return None


# ---------------------------------------------------------------------------
# 미룸 규칙


@dataclass(frozen=True)
class DeferDecision:
    """지금 말해도 되는지와 그 근거.

    `seconds_to_brake`는 다음 코너 브레이크 지점까지 남은 시간(초)이고,
    `gap_m`은 그 지점까지 남은 랩 거리다. `inside_corner`는 지금 코너 구간
    안에 있는지다. 둘 중 하나라도 참이면 `defer=True`다.
    """

    defer: bool
    seconds_to_brake: float | None
    next_corner: int | None
    brake_m: float | None
    gap_m: float | None
    inside_corner: bool


def defer_until_straight(
    dist_m: float,
    speed_kmh: float,
    corners: Sequence[Corner],
    track_length_m: float,
    *,
    defer_s: float = 3.0,
    brake_lead_m: float | Mapping[int, float] = 0.0,
) -> DeferDecision:
    """다음 코너 브레이크 지점까지 `defer_s` 미만이면 다음 긴 직선으로 미룬다.

    브레이크 지점은 코너 진입(`entry_m`)보다 `brake_lead_m`만큼 앞이라고 본다.
    `brake_lead_m`에 코너 번호 → 앞당김 거리 매핑을 주면 코너마다 다르게 쓸 수
    있다(기준 랩의 실제 브레이크 시작 지점을 쓰는 리프트 앤 코스트, SCRUM-71).

    랩 끝을 넘어가는 다음 코너는 트랙 길이만큼 감아 계산한다. 속도가 0에
    가까우면 남은 시간을 알 수 없으므로 코너 안일 때만 미룬다.
    """
    if not corners or track_length_m <= 0:
        return DeferDecision(False, None, None, None, None, False)

    distance = float(dist_m)
    inside = any(
        corner.entry_m <= distance < corner.exit_m for corner in corners
    )
    ordered = sorted(corners, key=lambda corner: corner.entry_m)
    following = next(
        (corner for corner in ordered if corner.entry_m > distance), ordered[0]
    )

    if isinstance(brake_lead_m, Mapping):
        lead = float(brake_lead_m.get(following.number, 0.0))
    else:
        lead = float(brake_lead_m)
    brake_m = following.entry_m - lead

    if following.entry_m > distance:
        # 다음 코너가 이번 랩 안에 있다. 이미 브레이크 구간이면 남은 거리는 0.
        gap = max(0.0, brake_m - distance)
    else:
        # 랩 끝을 넘어 첫 코너로 돌아온다.
        gap = track_length_m - distance + brake_m
    gap = max(0.0, gap)

    speed_mps = float(speed_kmh) / 3.6
    if math.isfinite(speed_mps) and speed_mps > 0.5:
        seconds: float | None = gap / speed_mps
    else:
        seconds = None
    defer = inside or (seconds is not None and seconds < defer_s)
    return DeferDecision(
        defer=defer,
        seconds_to_brake=seconds,
        next_corner=following.number,
        brake_m=brake_m,
        gap_m=gap,
        inside_corner=inside,
    )


# ---------------------------------------------------------------------------
# 코치 설정


@dataclass(frozen=True)
class CoachConfig:
    """코칭 문턱값. 기본값은 `config/default.toml`의 `[global]`과 같다."""

    focus_corner_count: int = 5
    focus_recent_laps: int = 3
    # 집중 코너 산정을 시작하는 유효 랩 수. 레이스 세션은 `FocusSet`이 이 값과
    # `RACE_FOCUS_MIN_VALID_LAPS`(2) 중 큰 값을 쓴다(레이스 1랩은 따로 뺀다).
    focus_min_valid_laps: int = 1
    min_focus_loss_ms: int = 50
    big_loss_ms: int = 300
    # 아래 두 값은 리프트 앤 코스트(SCRUM-71)가 개선 알림을 다음 긴 직선으로
    # 미룰 때 쓴다. 코너 코칭은 더 이상 미루지 않는다(SCRUM-94).
    defer_s: float = 3.0
    # 다음 코너 브레이크 지점을 진입 지점보다 이만큼 앞으로 본다.
    brake_lead_m: float = 40.0
    # 개선 알림 무전 유효 시간(초). 랩 완료(결승선) 뒤 1번 코너(스즈카 게임
    # lap_distance 기준 약 640m)까지를 덮는 값이다. 이 안에 말을 시작하지
    # 못하면 큐가 버려, 1번 코너 이후 엉뚱한 코너에서 나가지 않는다(SCRUM-143).
    improvement_ttl_s: float = 6.0
    # 원인 판정 민감도·손실 하한(SCRUM-124). `config/default.toml`의 `[coaching]`.
    sensitivity: CoachingSensitivityConfig = field(
        default_factory=CoachingSensitivityConfig
    )

    def __post_init__(self) -> None:
        if self.focus_corner_count < 1:
            raise ValueError("집중 코너 수는 1 이상이어야 합니다")
        if self.focus_recent_laps < 1:
            raise ValueError("집중 코너 산정 랩 수는 1 이상이어야 합니다")
        if self.focus_min_valid_laps < 1:
            raise ValueError("집중 코너 시작 랩 수는 1 이상이어야 합니다")
        if self.big_loss_ms < 0 or self.min_focus_loss_ms < 0:
            raise ValueError("손실 문턱값은 0 이상이어야 합니다")
        if not math.isfinite(self.defer_s) or self.defer_s < 0:
            raise ValueError("defer_s는 0 이상의 유한한 값이어야 합니다")
        if (
            not math.isfinite(self.improvement_ttl_s)
            or self.improvement_ttl_s < 0
        ):
            raise ValueError(
                "improvement_ttl_s는 0 이상의 유한한 값이어야 합니다"
            )

    @classmethod
    def from_app_config(cls, config: AppConfig) -> CoachConfig:
        return cls(
            focus_corner_count=config.focus_corner_count,
            focus_recent_laps=config.focus_recent_laps,
            big_loss_ms=int(round(config.big_loss_s * 1000.0)),
            defer_s=config.defer_s,
            sensitivity=config.coaching,
        )


class Prefetchable(Protocol):
    """`VoiceCache`처럼 문장 목록을 미리 만드는 객체."""

    async def prefetch(self, texts: Iterable[str], voice: str | None = None) -> Any:
        ...


class Coach:
    """내 차 샘플을 받아 코너 탈출 직후 코칭 무전을 만든다.

    호출 순서(한 랩):
    1. Car Telemetry 샘플마다 `on_sample(car_state, dist_m, now_ns)`.
    2. 랩이 끝나면 `on_lap_completed(trace, now_ns)`.

    `queue`를 주면 만든 무전을 바로 `RadioQueue`에 넣는다(끈 종류는 큐가 걸러
    내며, 그때는 반환 목록에도 넣지 않는다). 세션이 바뀌면 새 `Coach`를 만든다.
    """

    def __init__(
        self,
        track: Track,
        *,
        references: ReferenceSet | None = None,
        queue: RadioQueue | None = None,
        config: CoachConfig | None = None,
        focus_config: FocusConfig | None = None,
        thresholds: CoachingThresholds | None = None,
        player_car_index: int = 0,
        session_type: int | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._track = track
        self._ordered_corners = tuple(sorted(track.corners, key=lambda c: c.exit_m))
        self._by_number = {corner.number: corner for corner in track.corners}
        # 결승선(랩 시작) 뒤 첫 코너와 코너별 다음 코너. 개선 알림 만료와
        # 연속 코너 지연 발화 판정에 쓴다(SCRUM-143).
        self._first_corner = (
            min(track.corners, key=lambda corner: corner.entry_m)
            if track.corners
            else None
        )
        self._next_by_number: dict[int, Corner] = {}
        if len(self._ordered_corners) > 1:
            for index, corner in enumerate(self._ordered_corners):
                following = self._ordered_corners[
                    (index + 1) % len(self._ordered_corners)
                ]
                self._next_by_number[corner.number] = following
        self._config = config or CoachConfig()
        self._references = references or ReferenceSet(
            track.corners,
            player_car_index=player_car_index,
            session_type=session_type,
        )
        self._focus = FocusSet(
            track.corners,
            self._references,
            config=focus_config
            or FocusConfig(
                count=self._config.focus_corner_count,
                recent_laps=self._config.focus_recent_laps,
                min_loss_ms=self._config.min_focus_loss_ms,
                min_valid_laps=self._config.focus_min_valid_laps,
            ),
            player_car_index=player_car_index,
            session_type=session_type,
        )
        self._thresholds = thresholds or default_coaching_thresholds()
        self._queue = queue
        self._clock = clock
        self._player_car_index = player_car_index
        self._session_type = session_type

        self._lap = 0
        self._lap_start_ns = 0
        self._last_dist: float | None = None
        self._samples: list[Sample] = []
        self._crossed: set[int] = set()

        # 감정 단계(SCRUM-91): 마지막 샘플에서 남은 랩을 기억해, 랩 완료 때
        # 개선 알림의 단계를 정한다. 표현 돌려 쓰기도 코치마다 하나씩 둔다.
        self._variants = VariantRotator()
        self._remaining_laps: float | None = None
        self._in_last_laps = False

        # 반복 실수 강조(SCRUM-125): 코너별 직전 코칭 원인과 연속 랩 수.
        self._cause_key: dict[int, str] = {}
        self._cause_streak: dict[int, int] = {}
        self._cause_lap: dict[int, int] = {}

        # 기준 랩 조회·지표·궤적은 랩마다 한 번만 계산해 캐시한다.
        self._ref_cache: dict[int, tuple[LapTrace, str] | None] = {}
        self._ref_metrics_cache: dict[
            tuple[int, int], tuple[LapTrace, CornerMetrics]
        ] = {}
        self._ref_path_cache: dict[int, tuple[LapTrace, TrackOutline | None]] = {}

    # --- 조회 -------------------------------------------------------------

    @property
    def focus(self) -> FocusSet:
        return self._focus

    @property
    def references(self) -> ReferenceSet:
        return self._references

    @property
    def focus_corners(self) -> tuple[int, ...]:
        return self._focus.corners

    def seed_focus(self, corners: Iterable[int]) -> None:
        """저장된 집중 코너로 시작한다(계획 작업 19)."""
        self._focus.seed(corners)

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
            dist_m if dist_m is not None else state.get("lap_distance"), 0.0
        )
        if distance is None:
            return []
        distance = float(distance)

        lap = self._resolve_lap(state, distance)
        if lap != self._lap or self._rewound(distance):
            self._start_lap(lap, distance, now)

        # 랩 완료 개선 알림은 1번 코너 진입 전까지만 유효하다. 새 랩에서 그
        # 지점을 이미 지났다면(패킷 끊김·처리 지연) 아직 대기 중인 개선 알림을
        # 버려 엉뚱한 코너에서 나가지 않게 한다(SCRUM-143).
        self._expire_improvements_at_first_corner(distance)

        # 감정 단계(SCRUM-91): 남은 랩과 레이스 여부를 매 샘플에서 갱신한다.
        # 무효 랩·SC 중에도 갱신해 두어야 다음 랩 완료 때 개선 알림 단계가 맞다.
        self._remaining_laps = _remaining_laps_from_state(
            state, self._remaining_laps
        )
        self._in_last_laps = self._is_race(state) and is_last_laps(
            self._remaining_laps
        )

        if not self._coaching_allowed(state, lap):
            self._samples.clear()
            self._last_dist = distance
            return []

        sample = self._make_sample(state, distance, now)
        if sample is None:
            self._last_dist = distance
            return []
        self._samples.append(sample)

        messages: list[RadioMessage] = []
        previous = self._last_dist
        mood = mood_for(last_laps=self._in_last_laps)
        race = self._is_race(state)
        if previous is not None and distance >= previous:
            crossed = self._crossed_exits(previous, distance)
            if crossed:
                # 다음 코너로 넘어갔다. 아직 재생 전인 옛 코칭은 위치가 지나
                # 더 이상 맞지 않으므로, 새 코칭이 나가지 않는 경우에도 버린다.
                self._discard_pending_coaching()
            for corner in crossed:
                messages.extend(
                    self._coach_corner(
                        corner,
                        sample,
                        now,
                        mood,
                        race=race,
                        distance=distance,
                    )
                )
        self._last_dist = distance
        return messages

    def on_lap_completed(
        self, trace: LapTrace, now_ns: int | None = None
    ) -> list[RadioMessage]:
        """완료 랩을 기준 랩·집중 코너에 반영하고 개선 알림을 돌려준다."""
        now = int(now_ns) if now_ns is not None else int(self._clock())
        self._references.update(trace)
        self._ref_cache.clear()
        self._ref_metrics_cache.clear()
        self._ref_path_cache.clear()
        if trace.car_index != self._player_car_index:
            return []

        messages: list[RadioMessage] = []
        changes = self._focus.update([trace])
        improvements = [
            change
            for change in changes
            if change.reason is FocusReason.IMPROVED
        ]
        # 랩 처리 큐가 밀려 다음 랩에서 이미 1번 코너를 지난 뒤라면 개선
        # 알림을 내지 않는다. 집중 코너 목록 갱신은 그대로 반영한다(SCRUM-143).
        if not improvements or not self._improvement_timely(trace.lap):
            return messages
        for change in improvements[:MAX_IMPROVEMENTS_PER_LAP]:
            message = self._improvement_message(change, trace.lap, now)
            if message is not None:
                self._emit(message, messages)
        return messages

    def add_reference_lap(self, trace: LapTrace) -> None:
        """다른 차의 완료 랩을 기준 랩 후보에만 넣는다(실서버 파이프라인, SCRUM-84).

        AI 상대의 가장 최근 랩을 코너별 기준 후보로 쓰기 위한 경로다(SCRUM-94).
        집중 코너 산정·개선 알림은 내 차 랩에만 걸리므로 건드리지 않는다.
        """
        self._references.update(trace)
        self._ref_cache.clear()
        self._ref_metrics_cache.clear()
        self._ref_path_cache.clear()

    # --- 세션 시작 prefetch -----------------------------------------------

    def prefetch_phrases(self) -> list[str]:
        """세션 시작 때 미리 만들 코칭·개선·추월 문장(그 서킷 코너 전부).

        감정 단계(calm·urgent·hyped·warm)와 표현을 모두 포함해, 어떤 단계로
        말해도 캐시에 있는 문장을 바로 쓸 수 있게 한다(SCRUM-91).

        개선 알림은 "N번 좋아졌어. 다음은 M번 보자."처럼 코너 번호 두 개가
        들어가 모든 조합을 미리 만들 수 없다. 그래서 코너 번호 순서에서 바로
        다음 코너를 `next_corner`로 둔 조합을 함께 만든다(SCRUM-141). 실제
        세션에서 다음 집중 코너는 이 순서와 자주 겹친다.
        """
        phrases: list[str] = []
        ordered = sorted(self._track.corners, key=lambda corner: corner.number)
        next_by_number = (
            {
                corner.number: ordered[(index + 1) % len(ordered)].number
                for index, corner in enumerate(ordered)
            }
            if len(ordered) > 1
            else {}
        )
        for corner in self._track.corners:
            label = corner_label(corner.number, official=corner.official)
            for key, slots in (
                ("coaching.brake_late", {"meters": 5}),
                ("coaching.min_speed_low", {"kmh": 5}),
                ("improvement.corner_improved_short", {}),
            ):
                if corner.official:
                    slots = {"corner": corner.number, **slots}
                    resolved = key
                else:
                    slots = {"corner_label": label, **slots}
                    resolved = ORDINAL_KEYS[key]
                phrases.extend(all_phrases(resolved, **slots))
            next_number = next_by_number.get(corner.number)
            if next_number is not None:
                next_corner = self._by_number.get(next_number)
                next_official = (
                    next_corner.official if next_corner is not None else True
                )
                if corner.official:
                    phrases.extend(
                        all_phrases(
                            "improvement.corner_improved",
                            corner=corner.number,
                            next_corner=next_number,
                        )
                    )
                else:
                    phrases.extend(
                        all_phrases(
                            ORDINAL_KEYS["improvement.corner_improved"],
                            corner_label=label,
                            next_label=corner_label(
                                next_number, official=next_official
                            ),
                        )
                    )
        for key in PRELOAD_KEYS:
            phrases.extend(all_phrases(key))
        for key, slots in PRELOAD_WITH_SLOTS:
            phrases.extend(all_phrases(key, **slots))
        return list(dict.fromkeys(phrases))

    async def prefetch(
        self, cache: Prefetchable, voice: str | None = None
    ) -> Any:
        """`VoiceCache.prefetch()`에 이 서킷 문장들을 넘긴다(설계 6.8)."""
        return await cache.prefetch(self.prefetch_phrases(), voice)

    # --- 랩·샘플 상태 ------------------------------------------------------

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
        self._samples = []
        self._crossed = set()

    def _coaching_allowed(self, state: Mapping[str, Any], lap: int) -> bool:
        """아웃랩·피트·무효·SC/VSC·레이스 1랩은 코칭하지 않는다(리뷰 포커스 4)."""
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
        # Lap Data 값을 그대로 넘겨받은 경우에도 같은 규칙을 적용한다.
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
        session_type = _number(state.get("session_type"))
        if session_type is None:
            session_type = self._session_type
        if is_race_session(session_type) and lap == 1:
            return False
        return True

    def _is_race(self, state: Mapping[str, Any]) -> bool:
        """레이스 세션인가(마지막 3랩 격앙 표현의 전제)."""
        session_type = _number(state.get("session_type"))
        if session_type is None:
            session_type = self._session_type
        return is_race_session(session_type)

    def _make_sample(
        self, state: Mapping[str, Any], distance: float, now_ns: int
    ) -> Sample | None:
        speed = _number(state.get("speed"))
        throttle = _number(state.get("throttle"))
        brake = _number(state.get("brake"))
        steer = _number(state.get("steer"))
        gear = _number(state.get("gear"))
        if speed is None or throttle is None or brake is None or steer is None:
            return None
        if gear is None:
            return None
        t_ms = _number(
            state.get(
                "t_ms",
                state.get("lap_time_ms", state.get("current_lap_time_in_ms")),
            )
        )
        if t_ms is None:
            t_ms = max(0.0, (now_ns - self._lap_start_ns) / 1_000_000.0)
        x = _number(state.get("x"), 0.0) or 0.0
        z = _number(state.get("z"), 0.0) or 0.0
        return Sample(
            dist_m=distance,
            t_ms=int(t_ms),
            speed=float(speed),
            throttle=float(throttle),
            brake=float(brake),
            steer=float(steer),
            gear=int(gear),
            x=float(x),
            z=float(z),
            surface=_surface(state.get("surface")),
            in_traffic=bool(state.get("in_traffic")),
            position_changed=bool(state.get("position_changed")),
        )

    def _crossed_exits(self, previous: float, distance: float) -> list[Corner]:
        return [
            corner
            for corner in self._ordered_corners
            if corner.number not in self._crossed
            and previous < corner.exit_m <= distance
        ]

    def _coaching_too_late(self, corner: Corner, distance: float) -> bool:
        """코너 탈출 뒤 이미 다음 코너 진입을 넘었는가.

        연속 코너(S자·시케인)에서 패킷이 끊겨 여러 코너 탈출을 한 샘플에서
        지나면, 앞선 코너 코칭은 지금 위치와 어긋난다. 마지막 코너의 다음
        코너는 다음 랩 첫 코너라 이번 랩에서는 늦지 않은 것으로 본다.
        """
        following = self._next_by_number.get(corner.number)
        if following is None:
            return False
        if following.entry_m <= corner.exit_m:
            # 다음 코너가 랩을 넘어 이어지거나(마지막 코너) 같은 지점이다.
            return False
        return distance >= following.entry_m

    def _discard_pending_coaching(self) -> None:
        """아직 재생 전인 옛 코칭을 버린다(위치가 지나 더 이상 맞지 않을 때)."""
        queue = self._queue
        if queue is not None:
            queue.discard_pending(
                RadioKind.COACHING.value, reason=DROP_REASON_STALE
            )

    def _expire_improvements_at_first_corner(self, distance: float) -> None:
        """1번 코너 진입을 지나면 대기 중인 개선 알림을 버린다(SCRUM-143)."""
        first = self._first_corner
        if first is None or distance < first.entry_m:
            return
        queue = self._queue
        if queue is not None:
            queue.discard_pending(
                RadioKind.IMPROVEMENT.value, reason=DROP_REASON_STALE
            )

    def _improvement_timely(self, lap: int) -> bool:
        """랩 완료 개선 알림을 지금 내도 되는가(1번 코너 진입 전).

        코치는 이미 다음 랩 샘플을 받았을 수 있다. 그 랩에서 1번 코너를
        지났거나 완료 랩보다 두 랩 이상 앞서 있으면, 개선 알림은 지금 위치와
        어긋나므로 내지 않는다.
        """
        if self._lap > lap + 1:
            return False
        if self._lap == lap + 1 and self._last_dist is not None:
            first = self._first_corner
            if first is not None and self._last_dist >= first.entry_m:
                return False
        return True

    # --- 코너 판단 ---------------------------------------------------------

    def _coach_corner(
        self,
        corner: Corner,
        sample: Sample,
        now_ns: int,
        mood: Mood = Mood.CALM,
        *,
        race: bool = False,
        distance: float,
    ) -> list[RadioMessage]:
        self._crossed.add(corner.number)
        # 탈출을 지나 만든 코칭 시점에 이미 다음 코너 구간으로 들어갔다면
        # 지금 위치와 다른 코너를 말하게 되므로 내보내지 않는다(SCRUM-143).
        if self._coaching_too_late(corner, distance):
            return []
        if len(self._samples) < 2:
            return []
        # 레이스 접전 코너는 코칭하지 않는다(SCRUM-124). 접전 중의 속도·조향·라인
        # 차이는 내 실수가 아니므로 비교 자체를 하지 않고 연속 실수 횟수도 끊는다.
        if race and corner_has_traffic(self._samples, corner):
            self._reset_streak(corner.number)
            return []
        reference = self._reference_for(corner.number)
        if reference is None:
            return []
        reference_trace, source = reference

        current = LapTrace(
            car_index=self._player_car_index,
            lap=self._lap,
            valid=True,
            invalid_reason=None,
            samples=list(self._samples),
        )
        ref_path = self._reference_path(reference_trace)
        metrics = corner_metrics(
            current, corner, ref_path, thresholds=self._thresholds
        )
        reference_metrics = self._reference_metrics(
            corner, reference_trace, ref_path
        )
        loss_ms = corner_loss_ms(
            lap_time_points(current),
            lap_time_points(reference_trace),
            corner,
        )

        # 손실 0.1초 미만은 원인 문턱을 넘어도 말하지 않는다(SCRUM-124). 집중
        # 코너라도 예외가 아니다. 연속 실수 횟수는 여기서도 끊는다.
        if loss_ms < self._config.sensitivity.min_corner_loss_ms:
            self._reset_streak(corner.number)
            return []

        focused = self._focus.contains(corner.number)
        if (
            not focused
            and loss_ms < self._config.big_loss_ms
            and not metrics.off_track
        ):
            # 코너가 개선돼 코칭 조건을 넘지 못했다. 연속 실수 횟수를 끊는다.
            self._reset_streak(corner.number)
            return []
        cause = pick_cause(metrics, reference_metrics, self._config.sensitivity)
        if cause is None:
            # 근거가 약하면 말하지 않는다(설계 2장 "도움 안 되는 말" 금지).
            # 코너가 좋아졌다는 뜻이므로 연속 실수 횟수도 끊는다.
            self._reset_streak(corner.number)
            return []

        evidence = _corner_evidence(
            cause, metrics, reference_metrics, loss_ms, source, focused, corner
        )
        streak = self._record_streak(corner.number, cause.key)
        evidence["streak"] = streak
        evidence["repeated"] = streak >= REPEAT_STREAK_LAPS
        if streak >= REPEAT_STREAK_LAPS:
            # 같은 실수를 3랩째 반복하면 강한 어조로 짚는다(SCRUM-125).
            # 마지막 3랩처럼 이미 격앙된 상황이면 hyped를 그대로 쓴다.
            repeat_mood = Mood.HYPED if mood is Mood.HYPED else Mood.URGENT
            evidence["mood"] = repeat_mood.value
            message = self._repeat_message(
                corner, cause, streak, now_ns, evidence, repeat_mood
            )
        else:
            evidence["mood"] = mood.value
            message = self._corner_message(
                corner, cause, sample, now_ns, evidence, mood
            )
        # 코너 탈출 직후 바로 큐에 넣는다(SCRUM-94). 다음 직선으로 미루지 않는다.
        messages: list[RadioMessage] = []
        self._emit(message, messages)
        return messages

    def _record_streak(self, corner: int, cause_key: str) -> int:
        """코너별 같은 원인 연속 랩 수를 갱신해 돌려준다.

        직전 코칭이 같은 원인이고 바로 앞 랩이면 이어 세고, 그 밖에는 1부터
        다시 센다. 다른 원인이 나오거나 코너를 한 랩 건너뛰면(개선·무효 랩·
        피트·접전 제외·손실 하한 미달 등) 자동으로 초기화된다.
        """
        previous_key = self._cause_key.get(corner)
        previous_lap = self._cause_lap.get(corner)
        if previous_key == cause_key and previous_lap == self._lap - 1:
            streak = self._cause_streak.get(corner, 1) + 1
        else:
            streak = 1
        self._cause_key[corner] = cause_key
        self._cause_streak[corner] = streak
        self._cause_lap[corner] = self._lap
        return streak

    def _reset_streak(self, corner: int) -> None:
        """코너가 개선되거나 건너뛰어졌을 때 연속 실수 횟수를 지운다."""
        self._cause_key.pop(corner, None)
        self._cause_streak.pop(corner, None)
        self._cause_lap.pop(corner, None)

    def _corner_message(
        self,
        corner: Corner,
        cause: Cause,
        sample: Sample,
        now_ns: int,
        evidence: dict[str, Any],
        mood: Mood = Mood.CALM,
    ) -> RadioMessage:
        text = self._variants.render_corner(
            cause.template_key,
            corner.number,
            official=corner.official,
            mood=mood,
            **{cause.slot: cause.value},
        )
        return RadioMessage.create(
            RadioKind.COACHING.value,
            text,
            now_ns=now_ns,
            ttl_ns=COACHING_TTL_NS,
            evidence=evidence,
            lap=self._lap,
            corner=corner.number,
        )

    def _repeat_message(
        self,
        corner: Corner,
        cause: Cause,
        streak: int,
        now_ns: int,
        evidence: dict[str, Any],
        mood: Mood,
    ) -> RadioMessage:
        """반복 실수 강조 무전. 공식/자동 탐지 코너 모두 `corner_label`로 말한다."""
        text = self._variants.render(
            _REPEAT_TEMPLATES[cause.key],
            mood=mood,
            corner_label=corner_label(corner.number, official=corner.official),
            lap_streak=streak,
            **{cause.slot: cause.value},
        )
        return RadioMessage.create(
            RadioKind.COACHING.value,
            text,
            now_ns=now_ns,
            ttl_ns=COACHING_TTL_NS,
            evidence=evidence,
            lap=self._lap,
            corner=corner.number,
        )

    # --- 기준 랩 캐시 ------------------------------------------------------

    def _reference_for(self, corner: int) -> tuple[LapTrace, str] | None:
        if corner not in self._ref_cache:
            self._ref_cache[corner] = self._references.best_for(corner)
        return self._ref_cache[corner]

    def _reference_metrics(
        self, corner: Corner, reference_trace: LapTrace, ref_path: TrackOutline | None
    ) -> CornerMetrics:
        key = (id(reference_trace), corner.number)
        cached = self._ref_metrics_cache.get(key)
        if cached is not None and cached[0] is reference_trace:
            return cached[1]
        metrics = corner_metrics(
            reference_trace, corner, ref_path, thresholds=self._thresholds
        )
        self._ref_metrics_cache[key] = (reference_trace, metrics)
        return metrics

    def _reference_path(self, reference_trace: LapTrace) -> TrackOutline | None:
        key = id(reference_trace)
        cached = self._ref_path_cache.get(key)
        if cached is not None and cached[0] is reference_trace:
            return cached[1]
        path = trace_outline(reference_trace)
        self._ref_path_cache[key] = (reference_trace, path)
        return path

    # --- 개선 알림 ---------------------------------------------------------

    def _improvement_message(
        self, change: FocusChange, lap: int, now_ns: int
    ) -> RadioMessage | None:
        corner = self._by_number.get(change.corner)
        official = corner.official if corner is not None else True
        next_corner = self._by_number.get(change.next_corner or -1)
        mood = mood_for(last_laps=self._in_last_laps, praise=True)

        if official:
            if change.next_corner is not None:
                key = "improvement.corner_improved"
                slots: dict[str, Any] = {
                    "corner": change.corner,
                    "next_corner": change.next_corner,
                }
            else:
                key = "improvement.corner_improved_short"
                slots = {"corner": change.corner}
        else:
            label = corner_label(change.corner, official=False)
            if change.next_corner is not None:
                key = "improvement.corner_improved_ordinal"
                slots = {
                    "corner_label": label,
                    "next_label": corner_label(
                        change.next_corner,
                        official=(
                            next_corner.official if next_corner is not None else True
                        ),
                    ),
                }
            else:
                key = "improvement.corner_improved_short_ordinal"
                slots = {"corner_label": label}
        text = self._variants.render(key, mood=mood, **slots)

        evidence: dict[str, Any] = {
            "corner": change.corner,
            "loss_ms": change.loss_ms,
            "next_corner": change.next_corner,
            "corner_official": official,
            "mood": mood.value,
        }
        return RadioMessage.create(
            RadioKind.IMPROVEMENT.value,
            text,
            now_ns=now_ns,
            ttl_ns=int(self._config.improvement_ttl_s * 10**9),
            evidence=evidence,
            lap=lap,
            corner=change.corner,
        )

    # --- 내보내기 ----------------------------------------------------------

    def _emit(
        self, message: RadioMessage, collected: list[RadioMessage]
    ) -> None:
        if self._queue is not None and not self._queue.push(message):
            return
        collected.append(message)


# ---------------------------------------------------------------------------


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _surface(value: Any) -> tuple[int, ...] | None:
    """바퀴별 노면 4개를 정수 튜플로 만든다. 없거나 형식이 다르면 None."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    surfaces: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        surfaces.append(int(item))
    return tuple(surfaces)


def _remaining_laps_from_state(
    state: Mapping[str, Any], fallback: float | None
) -> float | None:
    """남은 랩 수. `remaining_laps`가 없으면 레이스 거리로 계산한다.

    레이스가 아니거나 `total_laps`가 0·없으면 `None`이다(SCRUM-95).
    """
    remaining = _number(state.get("remaining_laps"))
    if remaining is None:
        remaining = remaining_race_laps(
            state.get("session_type"),
            state.get("total_laps"),
            state.get("lap_num", state.get("current_lap_num")),
        )
    return remaining if remaining is not None else fallback


def _metric_values(metrics: CornerMetrics) -> dict[str, Any]:
    """무전 근거로 남길 지표(소수 한 자리)."""
    return {
        "brake_start_m": _rounded(metrics.brake_start_m),
        "brake_peak": round(metrics.brake_peak, 3),
        "turn_in_m": _rounded(metrics.turn_in_m),
        "steer_peak": round(metrics.steer_peak, 3),
        "min_speed": round(metrics.min_speed, 1),
        "min_speed_m": round(metrics.min_speed_m, 1),
        "steer_corrections": metrics.steer_corrections,
        "throttle_on_m": _rounded(metrics.throttle_on_m),
        "full_throttle_m": _rounded(metrics.full_throttle_m),
        "exit_speed": round(metrics.exit_speed, 1),
        "max_line_dev_m": round(metrics.max_line_dev_m, 1),
        "off_track": metrics.off_track,
        "corner_time_ms": metrics.corner_time_ms,
    }


def metric_values(metrics: CornerMetrics) -> dict[str, Any]:
    """코너 지표를 지도·리포트 API가 쓰는 dict로 만든다(SCRUM-84).

    무전 근거(`evidence["metrics"]`)와 같은 모양이라 화면이 두 곳을 같은 코드로
    그린다. 화면(`map.js`)은 `player`·`reference`의 키만 읽는다.
    """
    return _metric_values(metrics)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _corner_evidence(
    cause: Cause,
    metrics: CornerMetrics,
    reference: CornerMetrics,
    loss_ms: int,
    source: str,
    focused: bool,
    corner: Corner,
) -> dict[str, Any]:
    return {
        "cause": cause.key,
        "amount": cause.value,
        "unit": cause.unit,
        "loss_ms": loss_ms,
        "reference": source,
        "corner_official": corner.official,
        "focus": focused,
        cause.key: cause.detail,
        "metrics": _metric_values(metrics),
        "reference_metrics": _metric_values(reference),
    }


__all__ = [
    "CAUSE_BRAKE_EARLY",
    "CAUSE_BRAKE_LATE",
    "CAUSE_LATE_THROTTLE",
    "CAUSE_LINE_WIDE",
    "CAUSE_MIN_SPEED_LOW",
    "CAUSE_OFF_TRACK",
    "CAUSE_STEER_CORRECTIONS",
    "CAUSE_TURN_IN_EARLY",
    "CAUSE_TURN_IN_LATE",
    "MIN_CORNER_LOSS_MS",
    "REPEAT_STREAK_LAPS",
    "Cause",
    "Coach",
    "CoachConfig",
    "DeferDecision",
    "Prefetchable",
    "defer_until_straight",
    "metric_values",
    "pick_cause",
]
