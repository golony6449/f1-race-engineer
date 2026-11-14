"""코너 탈출 직후 코칭 무전 결정(작업 16, SCRUM-66).

설계 6.4의 "언제·무엇을 말하나"를 구현한다.

- 코너 탈출 지점(랩 거리 `Corner.exit_m`)을 처음 지나는 샘플에서 그 코너를 판단한다.
  집중 코너면 손실이 작아도 근거가 있으면 말하고, 아니면 손실이 `big_loss_ms`
  (기본 0.3초) 이상이거나 트랙을 벗어났을 때만 말한다.
- 원인은 하나만 고른다(`pick_cause`). 트랙 이탈 > 브레이크 > 턴인 > 조향 수정 >
  정점 속도 > 스로틀 > 라인 순으로, 문턱값을 넘는 첫 원인을 쓴다. 근거가 약하면
  아무 말도 하지 않는다("도움 안 되는 말" 금지, 설계 2장).
- 다음 코너 브레이크 지점까지 `defer_s`(기본 3초)보다 짧거나 아직 코너 안이면
  다음 긴 직선으로 미룬다(`defer_until_straight`). 미룬 무전은 그 랩이 끝나면
  버린다. 이 함수는 리프트 앤 코스트(SCRUM-71)도 다시 쓴다.
- 같은 랩에 같은 코너는 한 번만 말한다. 무효 랩·피트·SC/VSC·레이스 1랩은
  `car_state` 플래그로 받아 코칭하지 않는다(리뷰 포커스 4).
- 집중 코너 목록 변화(개선)는 `on_lap_completed()`가 `FocusSet`에서 받아
  "3번 좋아졌어. 다음은 7번 보자." 무전으로 만든다.

`on_sample(car_state, dist_m, now_ns)`의 `car_state`는 다음 키를 갖는 매핑이다
(모두 선택이며, 없으면 그 샘플은 건너뛴다).

- `speed`(km/h), `throttle`·`brake`·`steer`(0~1), `gear`
- `t_ms`(또는 `lap_time_ms`, `current_lap_time_in_ms`): 랩 시작 기준 ms.
  없으면 `now_ns` 차이로 만든다.
- `lap_num`(또는 `current_lap_num`): 없으면 랩 거리가 되감길 때 랩을 센다.
- `x`, `z`: 라인 이탈 판정용. 없으면 0으로 두어 라인 지표를 쓰지 않는다.
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
from f1re.config import AppConfig
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
from f1re.radio.templates import (
    PRELOAD_KEYS,
    corner_label,
    render,
    render_corner,
)
from f1re.tracks.model import Corner, Track
from f1re.tracks.outline import TrackOutline

# ---------------------------------------------------------------------------
# 원인 고르기 규칙(구현자가 정한 값, 데모 리플레이로 다듬는다)

# 브레이크·턴인 지점 차이 문턱(m). 이보다 작으면 그 원인으로 말하지 않는다.
MIN_BRAKE_DIFF_M = 3.0
MIN_TURN_IN_DIFF_M = 3.0
# 정점(최저) 속도 차이 문턱(km/h).
MIN_MIN_SPEED_DIFF_KMH = 2.0
# 스로틀을 다시 밟은 지점 차이 문턱(m).
MIN_THROTTLE_DIFF_M = 8.0
# 조향 수정 횟수 차이 문턱(회).
MIN_STEER_CORRECTIONS = 2
# 기준 궤적에서 더 벗어난 거리 문턱(m).
MIN_LINE_DEV_M = 1.5

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


def pick_cause(metrics: CornerMetrics, reference: CornerMetrics) -> Cause | None:
    """지표 두 개를 비교해 시간 손실에 가장 크게 작용한 원인 하나를 고른다.

    설계 6.4의 예("브레이크가 늦고 정점 라인이 밖으로 밀렸으면 브레이크 늦음")처럼
    여러 원인이 겹치면 앞 순서의 원인을 쓴다. 문턱을 넘는 원인이 없으면 `None`.
    """
    # 트랙 이탈은 그 자체로 가장 큰 사고이므로 맨 앞에 둔다.
    if metrics.off_track:
        meters = max(1, int(round(metrics.max_line_dev_m)))
        return Cause(
            key=CAUSE_OFF_TRACK,
            amount=float(meters),
            unit="m",
            slot="meters",
            value=meters,
            detail={
                "line_dev_m": round(metrics.max_line_dev_m, 1),
                "reference_line_dev_m": round(reference.max_line_dev_m, 1),
            },
        )

    if metrics.brake_start_m is not None and reference.brake_start_m is not None:
        diff = metrics.brake_start_m - reference.brake_start_m
        if diff >= MIN_BRAKE_DIFF_M:
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
        if diff <= -MIN_BRAKE_DIFF_M:
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
        if diff <= -MIN_TURN_IN_DIFF_M:
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
        if diff >= MIN_TURN_IN_DIFF_M:
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
    if extra_corrections >= MIN_STEER_CORRECTIONS:
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
    if speed_drop >= MIN_MIN_SPEED_DIFF_KMH:
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
        if diff >= MIN_THROTTLE_DIFF_M:
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
    if extra_line >= MIN_LINE_DEV_M:
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
    defer_s: float = 3.0
    # 다음 코너 브레이크 지점을 진입 지점보다 이만큼 앞으로 본다.
    brake_lead_m: float = 40.0
    # 개선 알림 무전 유효 시간(초).
    improvement_ttl_s: float = 10.0

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

    @classmethod
    def from_app_config(cls, config: AppConfig) -> CoachConfig:
        return cls(
            focus_corner_count=config.focus_corner_count,
            focus_recent_laps=config.focus_recent_laps,
            big_loss_ms=int(round(config.big_loss_s * 1000.0)),
            defer_s=config.defer_s,
        )


class Prefetchable(Protocol):
    """`VoiceCache`처럼 문장 목록을 미리 만드는 객체."""

    async def prefetch(self, texts: Iterable[str], voice: str | None = None) -> Any:
        ...


@dataclass
class _Pending:
    """미뤄 둔 무전. 다음 긴 직선에서 다시 판단한다."""

    lap: int
    message: RadioMessage


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
        self._pending: list[_Pending] = []

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

    @property
    def pending_count(self) -> int:
        return len(self._pending)

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

        if not self._coaching_allowed(state, lap):
            self._samples.clear()
            self._pending.clear()
            self._last_dist = distance
            return []

        sample = self._make_sample(state, distance, now)
        if sample is None:
            self._last_dist = distance
            return []
        self._samples.append(sample)

        messages: list[RadioMessage] = []
        if self._pending:
            messages.extend(self._release_pending(sample, now))

        previous = self._last_dist
        if previous is not None and distance >= previous:
            for corner in self._crossed_exits(previous, distance):
                messages.extend(self._coach_corner(corner, sample, now))
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
        # 랩이 끝났으므로 그 랩에서 미뤄 둔 무전은 버린다.
        self._pending = [
            pending for pending in self._pending if pending.lap != trace.lap
        ]
        if trace.car_index != self._player_car_index:
            return []

        messages: list[RadioMessage] = []
        for change in self._focus.update([trace]):
            if change.reason is not FocusReason.IMPROVED:
                continue
            message = self._improvement_message(change, trace.lap, now)
            if message is not None:
                self._emit(message, messages)
        return messages

    def add_reference_lap(self, trace: LapTrace) -> None:
        """다른 차의 완료 랩을 기준 랩 후보에만 넣는다(실서버 파이프라인, SCRUM-84).

        AI 상대의 최고 랩(설계 6.3)을 기준으로 쓰기 위한 경로다. 집중 코너
        산정·미뤄 둔 무전·개선 알림은 내 차 랩에만 걸리므로 건드리지 않는다.
        """
        self._references.update(trace)
        self._ref_cache.clear()
        self._ref_metrics_cache.clear()
        self._ref_path_cache.clear()

    # --- 세션 시작 prefetch -----------------------------------------------

    def prefetch_phrases(self) -> list[str]:
        """세션 시작 때 미리 만들 코칭·개선 문장(그 서킷 코너 전부 + 고정 경고)."""
        phrases: list[str] = []
        for corner in self._track.corners:
            phrases.append(
                render_corner(
                    "coaching.brake_late",
                    corner.number,
                    official=corner.official,
                    meters=5,
                )
            )
            phrases.append(
                render_corner(
                    "coaching.min_speed_low",
                    corner.number,
                    official=corner.official,
                    kmh=5,
                )
            )
            phrases.append(
                render_corner(
                    "improvement.corner_improved_short",
                    corner.number,
                    official=corner.official,
                )
            )
        phrases.extend(render(key) for key in PRELOAD_KEYS)
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
        # 앞 랩에서 미룬 무전은 그 랩 안에 못 했으므로 버린다.
        self._pending.clear()

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
        if session_type in RACE_SESSION_TYPES and lap == 1:
            return False
        return True

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
        )

    def _crossed_exits(self, previous: float, distance: float) -> list[Corner]:
        return [
            corner
            for corner in self._ordered_corners
            if corner.number not in self._crossed
            and previous < corner.exit_m <= distance
        ]

    # --- 코너 판단 ---------------------------------------------------------

    def _coach_corner(
        self, corner: Corner, sample: Sample, now_ns: int
    ) -> list[RadioMessage]:
        self._crossed.add(corner.number)
        if len(self._samples) < 2:
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

        focused = self._focus.contains(corner.number)
        if (
            not focused
            and loss_ms < self._config.big_loss_ms
            and not metrics.off_track
        ):
            return []
        cause = pick_cause(metrics, reference_metrics)
        if cause is None:
            # 근거가 약하면 말하지 않는다(설계 2장 "도움 안 되는 말" 금지).
            return []

        evidence = _corner_evidence(
            cause, metrics, reference_metrics, loss_ms, source, focused, corner
        )
        message = self._corner_message(
            corner, cause, sample, now_ns, evidence
        )
        decision = defer_until_straight(
            sample.dist_m,
            sample.speed,
            self._track.corners,
            self._track.length_m,
            defer_s=self._config.defer_s,
            brake_lead_m=self._config.brake_lead_m,
        )
        if decision.defer:
            self._pending.append(_Pending(lap=self._lap, message=message))
            return []
        messages: list[RadioMessage] = []
        self._emit(message, messages)
        return messages

    def _corner_message(
        self,
        corner: Corner,
        cause: Cause,
        sample: Sample,
        now_ns: int,
        evidence: dict[str, Any],
    ) -> RadioMessage:
        text = render_corner(
            cause.template_key,
            corner.number,
            official=corner.official,
            **{cause.slot: cause.value},
        )
        return RadioMessage.create(
            RadioKind.COACHING.value,
            text,
            now_ns=now_ns,
            ttl_ns=self._coaching_ttl_ns(corner, sample),
            evidence=evidence,
            lap=self._lap,
            corner=corner.number,
        )

    def _coaching_ttl_ns(self, corner: Corner, sample: Sample) -> int:
        """그 코너 다음 진입 전까지 유효하다(설계 6.7)."""
        length = self._track.length_m
        if length <= 0:
            return 60 * 10**9
        gap = (corner.entry_m - sample.dist_m) % length
        if gap <= 0.0:
            gap = length
        speed_mps = max(sample.speed / 3.6, 1.0)
        seconds = gap / speed_mps
        return int(min(max(seconds, 3.0), 120.0) * 10**9)

    def _release_pending(
        self, sample: Sample, now_ns: int
    ) -> list[RadioMessage]:
        """미뤄 둔 무전을 지금 말해도 되는지 다시 보고, 되면 내보낸다."""
        released: list[RadioMessage] = []
        remaining: list[_Pending] = []
        decision = defer_until_straight(
            sample.dist_m,
            sample.speed,
            self._track.corners,
            self._track.length_m,
            defer_s=self._config.defer_s,
            brake_lead_m=self._config.brake_lead_m,
        )
        for pending in self._pending:
            if pending.lap != self._lap:
                continue
            if pending.message.expired(now_ns):
                continue
            if decision.defer:
                remaining.append(pending)
                continue
            self._emit(pending.message, released)
        self._pending = remaining
        return released

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
        if change.next_corner is not None:
            next_label = corner_label(
                change.next_corner,
                official=next_corner.official if next_corner is not None else True,
            )
        else:
            next_label = None

        if official:
            if change.next_corner is not None:
                text = render(
                    "improvement.corner_improved",
                    corner=change.corner,
                    next_corner=change.next_corner,
                )
            else:
                text = render(
                    "improvement.corner_improved_short", corner=change.corner
                )
        else:
            label = corner_label(change.corner, official=False)
            if next_label is not None:
                text = render(
                    "improvement.corner_improved_ordinal",
                    corner_label=label,
                    next_label=next_label,
                )
            else:
                text = render(
                    "improvement.corner_improved_short_ordinal",
                    corner_label=label,
                )

        evidence: dict[str, Any] = {
            "corner": change.corner,
            "loss_ms": change.loss_ms,
            "next_corner": change.next_corner,
            "corner_official": official,
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
    "Cause",
    "Coach",
    "CoachConfig",
    "DeferDecision",
    "Prefetchable",
    "defer_until_straight",
    "metric_values",
    "pick_cause",
]
