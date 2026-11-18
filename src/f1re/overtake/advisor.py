"""추월 선택지 무전(작업 18, SCRUM-68).

설계 6.5(추월 선택지)·6.7(무전 전달 계층)을 구현한다.

- **켜지는 조건**: AI 상대 세션(타임 트라이얼 제외)에서 Lap Data의 앞차와의 시간
  간격(`delta_to_car_in_front`)이 `gap_s`(기본 1.0초) 이하일 때. 간격이 다시
  벌어지거나, 앞차를 추월해 그 차가 내 뒤로 가면 끈다.
- **판단 재료**: 간격·간격 변화, DRS 허용 여부와 활성 거리, 내 ERS 잔량·모드,
  양쪽 타이어 컴파운드·나이, 앞차와 나의 코너별 탈출 속도 비교(작업 14의 모든 차
  `LapTrace`).
- **말하는 것**: 점수 순 상위 1~2개 선택지. 기본 설정(랩당 1회)에서는 랩마다 한
  번만 말한다. 문장은 `radio/templates.py`의 추월 틀에서 만든다.
  - `overtake.drs` / `overtake.drs_ers`: "앞차랑 0.6초야. 다음 직선에서 DRS랑 ERS 다 써."
  - `overtake.slow_exit`: "앞차랑 0.6초야. 앞차가 9번 탈출이 느려. 9번에서 바짝 붙어서 직선에서 넘어."
    (코너 데이터가 없는 서킷의 자동 탐지 코너는 "9번째 코너"로 말한다)
  - `overtake.tyre_wait`: "앞차랑 0.6초야. 우리 타이어가 7랩 더 새거야. 2랩만 기다렸다가 공격해."
- 무전 종류는 `overtake`(우선순위: 코너 코칭 다음)이며, 코칭과 겹치면
  `RadioQueue`가 우선순위·유효 시간으로 정리한다(설계 6.7). 대시보드에서 추월
  무전을 끄면 `RadioQueue.push`가 거부하므로 결과에도 넣지 않는다.
- 안전차량(SC)·VSC 중에는 추월이 금지되므로 말하지 않는다.

추월 성공 긍정 코칭(SCRUM-145)
--------------------------------
직전 앞차(`ahead_index`)가 내 뒤로 간 것을 감지하면 곧바로 칭찬하지 않는다.
`SUCCESS_CONFIRM_S`(1.5초) 동안 다음이 모두 유지될 때만 추월 성공으로 본다.

- 그 차가 계속 내 뒤에 있다(재역전 없음).
- 나와 그 차가 피트 레인·인랩·아웃랩·차고 상태가 아니다. 그 차가 최근
  `PIT_RECENT_S`(30초) 안에 피트 레인에 있었으면 피트인으로 인한 순위 변동으로
  보고 말하지 않는다.
- 트랙 이탈(`driver_status` 5·`current_lap_invalid`)이 없고, 페널티·경고·
  미처리 페널티·앞날개 손상이 추월 시점보다 늘지 않았다(접촉 없음).
- 1랩(스타트 혼전)이 아니다(추월 리액션과 같은 규칙).

확인되면 추월 직전 `SUCCESS_EVIDENCE_WINDOW_S`(2초)의 플레이어 상태·텔레메트리와
최근 완료 랩을 비교해 가장 설득력 있는 기여 요인 하나를 고른다. 요인은
DRS/ERS 활용, 직전 코너 탈출 속도 우위, 늦은 브레이킹, 스로틀 우위, 깔끔한
추월(속도 우위) 순으로 점수를 매기며, 문턱을 넘는 근거가 하나도 없으면
말하지 않는다(억지 칭찬 금지). 무전은 추월 1건당 1회, `overtake` 종류이고
가이드 선택지의 랩당 횟수(`per_lap`)를 쓰지 않는다.

입력:

- `snapshot`: `StateStore.snapshot()` 그대로. `packets.session`의 세션 종류·트랙·
  SC 상태, `packets.lap_data.lap_data`의 차 22대 순위·간격·랩 거리,
  `packets.car_status.car_status_data`의 DRS·ERS·타이어 값을 읽는다.
- `traces_by_car`: 차 인덱스 → 그 차의 완료 랩(`LapTrace`) 목록. 코너별 탈출 속도
  비교에는 차마다 최신 유효 랩을 쓴다. 없으면 그 선택지만 빠진다.
- `now_ns`: 무전 시각·유효 시간의 기준.

`OvertakeAdvisor.evaluate()`는 순수 판단만 하며, `queue`를 넘기면 만든 무전을
바로 `RadioQueue`에 넣는다(`Coach`와 같은 방식).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from f1re.coaching.metrics import corner_metrics
from f1re.config import AppConfig
from f1re.laps.traces import (
    DRIVER_IN_LAP,
    DRIVER_IN_PIT,
    DRIVER_OFF_TRACK,
    DRIVER_OUT_LAP,
    PIT_LANE_STATUSES,
    LapTrace,
)
from f1re.race_distance import remaining_race_laps
from f1re.radio.queue import RadioKind, RadioMessage, RadioQueue
from f1re.radio.templates import (
    CLOSE_OVERTAKE_GAP_S,
    VariantRotator,
    corner_label,
    is_last_laps,
    mood_for,
)
from f1re.tracks.model import Corner, Track, load_track
from f1re.udp.session_category import is_time_trial_session

# F1 25 ERS 저장소 최대 에너지(J). `web/app.py`와 같은 공식 규정값이다.
ERS_STORE_MAX_J = 4_000_000.0

# Car Status의 ERS 배포 모드(공식 열거형): 3=Overtake.
ERS_DEPLOY_OVERTAKE = 3

# 선택지별 유효 시간(초). 설계 6.7의 "무전마다 유효 시간"이다.
#   - DRS: 다음 직선까지. 데모 랩(85~100초)에서 직선 하나를 넉넉히 덮는다.
#   - 느린 탈출: 이름을 부른 코너를 지나칠 때까지. 탐색 거리(600m)를 레이스
#     속도로 달리는 시간 안쪽으로 줄여, 큐에 밀려도 지난 코너를 말하지 않게 한다.
#   - 타이어 기다림: 두 랩을 기다리라는 선택이므로 한 랩 남짓.
DRS_TTL_S = 15.0
SLOW_EXIT_TTL_S = 15.0
TYRE_WAIT_TTL_S = 120.0

# 추월 성공 긍정 코칭(SCRUM-145). 직전 앞차가 뒤로 간 뒤 이 시간(초) 동안
# 재역전·피트 변동·트랙 이탈·접촉이 없어야 성공으로 인정한다.
SUCCESS_CONFIRM_S = 1.5
# 성공 확인 중 최소 관찰 횟수(10Hz 판단 주기에서 0.2초). 한 번의 늦은 호출만으로
# 성공을 확정하지 않게 한다.
SUCCESS_MIN_CHECKS = 2
# 추월 직전 기여 요인(DRS/ERS·텔레메트리)을 찾는 시간 창(초).
SUCCESS_EVIDENCE_WINDOW_S = 2.0
# 성공 무전 유효 시간(초). 추월 직후 짧게만 쓸모가 있다.
SUCCESS_TTL_S = 6.0
# 관찰 기록 보관 시간(초).
SUCCESS_HISTORY_S = 3.0
# 추월 대상 차가 이 시간(초) 안에 피트 레인에 있었으면 피트인 추월로 본다.
PIT_RECENT_S = 30.0
# 코너 탈출 속도 우위로 인정하는 최소 차이(km/h).
SUCCESS_EXIT_ADVANTAGE_KMH = 5.0
# 늦은 브레이킹으로 인정하는 최소 차이(m). 내 브레이크 시작이 이만큼 뒤여야 한다.
SUCCESS_LATE_BRAKE_M = 8.0
# 스로틀 우위로 인정하는 최소 차이(0~1).
SUCCESS_THROTTLE_ADVANTAGE = 0.15
# 깔끔한 추월로 인정하는 최소 속도 우위(km/h).
SUCCESS_SPEED_ADVANTAGE_KMH = 3.0
# 추월 지점에서 이 거리(m) 안의 직전 코너를 기여 요인 비교에 쓴다.
SUCCESS_CORNER_LOOKBACK_M = 400.0
# ERS 에너지가 이 비율(퍼센트 포인트) 이상 줄면 배포로 본다.
SUCCESS_ERS_DEPLOY_PCT = 4.0
# 텔레메트리 비교에 필요한 최소 샘플 수.
SUCCESS_MIN_TELEMETRY_SAMPLES = 2

# 추월 성공 무전의 기여 요인 → 틀 키. 키는 `overtake.success_<요인>`이다.
SUCCESS_TEMPLATE_PREFIX = "overtake.success_"

# Lap Data에서 페널티·경고 누적값(늘면 접촉·위반이 있었다고 본다).
LAP_PENALTY_FIELDS: tuple[str, ...] = (
    "penalties",
    "total_warnings",
    "corner_cutting_warnings",
    "num_unserved_drive_through_pens",
    "num_unserved_stop_go_pens",
)
# Car Status에서 손상 누적값(%).
DAMAGE_FIELDS: tuple[str, ...] = (
    "front_left_wing_damage",
    "front_right_wing_damage",
)

# 앞차가 DRS 활성 지점에서 이보다 멀면 아직 "다음 직선"이라고 말하지 않는다.
DEFAULT_DRS_ACTIVATION_WINDOW_M = 600.0
# 코너 탈출 속도 비교에서 앞차가 느리다고 보는 최소 차이(km/h).
DEFAULT_SLOW_EXIT_DEFICIT_KMH = 4.0
# 지금 위치에서 이 거리 안의 다음 코너만 "붙고 직선에서" 선택지로 쓴다.
DEFAULT_SLOW_EXIT_LOOKAHEAD_M = 600.0
# 내 타이어가 앞차보다 이만큼 새것일 때 기다리는 선택지를 말한다.
DEFAULT_TYRE_ADVANTAGE_LAPS = 4
# 기다리라고 권하는 랩 수.
DEFAULT_TYRE_WAIT_LAPS = 2

# 차 인덱스 → 그 차의 완료 랩들. 배열(인덱스 순)로 줘도 된다.
TraceSource = (
    Mapping[int, Sequence[LapTrace] | LapTrace]
    | Sequence[Sequence[LapTrace] | LapTrace]
)


@dataclass(frozen=True)
class OvertakeConfig:
    """추월 안내 문턱값. 기본값은 `config/default.toml`의 `[global]`과 같다."""

    gap_s: float = 1.0
    per_lap: int = 1
    max_options: int = 2
    drs_activation_window_m: float = DEFAULT_DRS_ACTIVATION_WINDOW_M
    ers_deploy_min_pct: float = 30.0
    slow_exit_deficit_kmh: float = DEFAULT_SLOW_EXIT_DEFICIT_KMH
    slow_exit_lookahead_m: float = DEFAULT_SLOW_EXIT_LOOKAHEAD_M
    tyre_advantage_laps: int = DEFAULT_TYRE_ADVANTAGE_LAPS
    tyre_wait_laps: int = DEFAULT_TYRE_WAIT_LAPS
    # 이보다 빠르게 간격이 줄면(초당 초) "붙는 중"으로 본다.
    gap_closing_s_per_s: float = 0.02

    def __post_init__(self) -> None:
        if not math.isfinite(self.gap_s) or self.gap_s <= 0:
            raise ValueError("gap_s는 0보다 큰 유한한 값이어야 합니다")
        if self.per_lap < 1:
            raise ValueError("per_lap은 1 이상이어야 합니다")
        if not 1 <= self.max_options <= 2:
            raise ValueError("max_options는 1~2여야 합니다")
        for name in (
            "drs_activation_window_m",
            "ers_deploy_min_pct",
            "slow_exit_deficit_kmh",
            "slow_exit_lookahead_m",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name}은 0 이상의 유한한 값이어야 합니다")
        if self.tyre_advantage_laps < 1:
            raise ValueError("tyre_advantage_laps는 1 이상이어야 합니다")
        if self.tyre_wait_laps < 1:
            raise ValueError("tyre_wait_laps는 1 이상이어야 합니다")
        if not math.isfinite(self.gap_closing_s_per_s):
            raise ValueError("gap_closing_s_per_s는 유한한 값이어야 합니다")

    @classmethod
    def from_app_config(cls, config: AppConfig) -> OvertakeConfig:
        """앱 설정의 `overtake_gap_s`·`overtake_per_lap`을 그대로 쓴다."""
        return cls(gap_s=config.overtake_gap_s, per_lap=config.overtake_per_lap)


@dataclass(frozen=True)
class _Option:
    """판단한 선택지 하나(점수 순 정렬 전).

    문장은 무전을 실제로 내보낼 때 감정 단계·표현 번호와 함께 만든다.
    후보로만 남고 버려진 선택지가 표현 돌려 쓰기를 소비하지 않게 하기 위해서다.
    """

    key: str
    score: float
    slots: dict[str, Any]
    ttl_s: float
    evidence: dict[str, Any]
    corner: int | None = None
    corner_render: bool = False
    official: bool = True


@dataclass(frozen=True)
class _View:
    """스냅샷에서 추월 판단에 필요한 값만 꺼낸 것."""

    session_uid: int | None
    session_type: int | None
    track_id: int | None
    safety_car: bool
    player_index: int | None
    entries: tuple[Mapping[str, Any], ...]
    telemetry: tuple[Mapping[str, Any], ...]
    player_entry: Mapping[str, Any]
    player_status: Mapping[str, Any]
    ahead_index: int | None
    ahead_status: Mapping[str, Any]
    lap: int | None
    distance: float
    gap_s: float | None
    last_laps: bool


@dataclass(frozen=True)
class _Observation:
    """추월 직전 판단 재료 한 순간(10Hz).

    `ahead_index`는 그때 내 바로 앞차다. `ahead_speed`·`ahead_throttle`은 그
    차의 Car Telemetry 값이며, 추월 성공 기여 요인을 비교하는 데 쓴다.
    """

    now_ns: int
    ahead_index: int | None
    gap_s: float | None
    drs_allowed: int | None
    ers_mode: int | None
    ers_energy_j: float | None
    player_speed: float | None
    player_throttle: float | None
    ahead_speed: float | None
    ahead_throttle: float | None


@dataclass
class _PendingSuccess:
    """추월을 감지했지만 아직 안정 시간을 확인 중인 건."""

    overtaken_index: int
    detected_ns: int
    lap: int | None
    track_id: int | None
    session_type: int | None
    distance: float
    close_battle: bool
    last_laps: bool
    observations: tuple[_Observation, ...]
    pass_observation: _Observation
    baselines: dict[str, float | None]
    checks: int = 0


@dataclass(frozen=True)
class _SuccessFactor:
    """추월 성공의 핵심 기여 요인 하나."""

    kind: str
    score: float
    slots: dict[str, Any]
    evidence: dict[str, Any]
    corner: int | None = None


class OvertakeAdvisor:
    """내 차와 앞차의 상태를 받아 추월 선택지 무전을 만든다.

    한 세션 동안 하나를 만들어 계속 쓴다. 내부에 "켜짐" 상태, 랩당 말한 횟수,
    간격 변화, 코너 탈출 속도 캐시를 둔다. 세션이 바뀌면 새로 만들어도 되고,
    같은 인스턴스가 `session_uid` 변화를 감지해 스스로 초기화한다.
    """

    def __init__(
        self,
        config: OvertakeConfig | None = None,
        *,
        queue: RadioQueue | None = None,
        track: Track | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._config = config or OvertakeConfig()
        self._queue = queue
        self._fixed_track = track
        self._clock = clock
        self._tracks: dict[int, Track | None] = {}
        self._active = False
        self._session_uid: int | None = None
        self._lap: int | None = None
        self._messages_this_lap = 0
        self._ahead_index: int | None = None
        self._previous_gap_s: float | None = None
        self._previous_gap_ns: int | None = None
        self._gap_change: float | None = None
        # 차 인덱스 → (랩 트레이스, 코너 번호 → 탈출 속도). 최신 랩만 들고 있어
        # 세션 내내 쌓이지 않는다.
        self._exit_cache: dict[int, tuple[LapTrace, dict[int, float]]] = {}
        # 감정 단계(SCRUM-91): 간격 0.5초 이하·마지막 3랩은 격앙, 그 밖은 calm.
        self._variants = VariantRotator()
        # 추월 성공 긍정 코칭(SCRUM-145): 직전 구간 관찰 기록과 확인 중인 건.
        self._observations: list[_Observation] = []
        self._pending_success: _PendingSuccess | None = None
        # 차 인덱스 → 마지막으로 피트 레인·인랩·아웃랩·차고 상태를 본 시각.
        # 피트인으로 순위가 바뀐 것을 추월로 칭찬하지 않게 한다.
        self._pit_seen_ns: dict[int, int] = {}
        self._pit_stops: dict[int, int] = {}

    # --- 조회 -------------------------------------------------------------

    @property
    def config(self) -> OvertakeConfig:
        return self._config

    @property
    def active(self) -> bool:
        """지금 추월 안내가 켜져 있는가(간격 1초 이하, AI 상대 세션)."""
        return self._active

    @property
    def messages_this_lap(self) -> int:
        return self._messages_this_lap

    def reset(self) -> None:
        """세션 시작 등에서 상태를 비운다."""
        self._active = False
        self._session_uid = None
        self._lap = None
        self._messages_this_lap = 0
        self._ahead_index = None
        self._previous_gap_s = None
        self._previous_gap_ns = None
        self._gap_change = None
        self._exit_cache.clear()
        self._variants.reset()
        self._observations.clear()
        self._pending_success = None
        self._pit_seen_ns.clear()
        self._pit_stops.clear()

    # --- 판단 -------------------------------------------------------------

    def evaluate(
        self,
        snapshot: Mapping[str, Any],
        traces_by_car: TraceSource | None = None,
        now_ns: int | None = None,
    ) -> list[RadioMessage]:
        """상태 한 순간을 반영하고, 지금 말할 추월 무전 목록을 돌려준다."""
        now = int(now_ns) if now_ns is not None else int(self._clock())
        view = _view(snapshot)
        if view.session_uid != self._session_uid:
            self.reset()
            self._session_uid = view.session_uid

        if view.player_index is None or view.lap is None:
            self._ahead_index = view.ahead_index
            self._active = False
            self._pending_success = None
            self._observations.clear()
            return []

        # 랩이 바뀌면 그 랩에 말할 수 있는 추월 무전 횟수를 새로 센다.
        if view.lap != self._lap:
            self._lap = view.lap
            self._messages_this_lap = 0

        if is_time_trial_session(view.session_type) or view.safety_car:
            self._ahead_index = view.ahead_index
            self._active = False
            self._pending_success = None
            return []

        # 추월 직전 구간을 기여 요인 비교에 쓸 수 있게 매 판단마다 남긴다.
        # 추월 감지 프레임도 남지만 그 프레임의 `ahead_index`는 이미 새 앞차라
        # 추월 대상 차의 관찰로는 쓰이지 않는다.
        self._record_observation(view, now)

        overtaken = self._overtaken_index(view)
        if overtaken is not None:
            # 앞차를 추월했다. 곧바로 칭찬하지 않고 안정 시간을 확인한다.
            self._begin_success(view, now, overtaken)
            self._ahead_index = view.ahead_index
            self._active = False
            return []

        success = self._observe_success(view, now, traces_by_car)

        gap = view.gap_s if view.ahead_index is not None else None
        self._ahead_index = view.ahead_index
        self._observe_gap(gap, now)

        if gap is None or gap > self._config.gap_s:
            self._active = False
            return [success] if success is not None else []

        self._active = True
        if success is not None:
            # 추월 칭찬은 한 건만 보낸다. 가이드 선택지는 다음 주기부터 다시 본다.
            return [success]
        if self._messages_this_lap >= self._config.per_lap:
            return []

        options = self._options(view, traces_by_car)
        if not options:
            return []
        limit = min(
            self._config.max_options,
            self._config.per_lap - self._messages_this_lap,
        )
        mood = mood_for(
            last_laps=view.last_laps,
            close_battle=gap <= CLOSE_OVERTAKE_GAP_S,
        )
        messages: list[RadioMessage] = []
        for option in options[:limit]:
            text = (
                self._variants.render_corner(
                    option.key,
                    int(option.corner or 0),
                    official=option.official,
                    mood=mood,
                    **option.slots,
                )
                if option.corner_render
                else self._variants.render(
                    option.key, mood=mood, **option.slots
                )
            )
            message = RadioMessage.create(
                RadioKind.OVERTAKE.value,
                text,
                now_ns=now,
                ttl_ns=int(option.ttl_s * 1_000_000_000),
                evidence={**option.evidence, "mood": mood.value},
                lap=view.lap,
                corner=option.corner,
            )
            # 대시보드에서 추월 무전을 끈 경우 큐가 거부한다(설계 6.7).
            if self._queue is not None and not self._queue.push(message):
                continue
            messages.append(message)
            self._messages_this_lap += 1
        return messages

    # --- 켜짐·꺼짐 ---------------------------------------------------------

    def _overtaken_index(self, view: _View) -> int | None:
        """직전에 보던 앞차가 이번 상태에서 내 뒤로 갔으면 그 차 인덱스.

        새 앞차가 없는 경우(선두를 넘은 경우)도 추월로 본다. 앞차 인덱스가
        그대로면 추월이 아니다.
        """
        previous = self._ahead_index
        current = view.ahead_index
        if previous is None or previous == current:
            return None
        if not self._is_behind(previous, view):
            return None
        return previous

    def _is_behind(self, car_index: int, view: _View) -> bool:
        if not 0 <= car_index < len(view.entries):
            return False
        entry = view.entries[car_index]
        player_total = _number(view.player_entry.get("total_distance"))
        total = _number(entry.get("total_distance"))
        if player_total is not None and total is not None:
            return total < player_total
        player_position = _integer(view.player_entry.get("car_position"))
        position = _integer(entry.get("car_position"))
        if player_position is None or position is None:
            return False
        return 0 < player_position < position

    def _observe_gap(self, gap: float | None, now_ns: int) -> None:
        """간격 변화(초당 초)를 갱신한다. 양수면 벌어지는 중, 음수면 붙는 중."""
        self._gap_change = None
        if gap is None:
            self._previous_gap_s = None
            self._previous_gap_ns = None
            return
        if (
            self._previous_gap_s is not None
            and self._previous_gap_ns is not None
            and now_ns > self._previous_gap_ns
        ):
            seconds = (now_ns - self._previous_gap_ns) / 1_000_000_000.0
            if seconds > 0:
                self._gap_change = (gap - self._previous_gap_s) / seconds
        self._previous_gap_s = gap
        self._previous_gap_ns = now_ns

    # --- 추월 성공 긍정 코칭(SCRUM-145) ----------------------------------

    def _record_observation(self, view: _View, now_ns: int) -> None:
        """추월 직전 비교에 쓸 관찰을 남기고 차별 최근 피트 상태를 갱신한다."""
        self._update_pit_memory(view, now_ns)
        if view.player_index is None or view.lap is None:
            return
        self._observations.append(self._observation(view, now_ns))
        cutoff = now_ns - int(SUCCESS_HISTORY_S * 1_000_000_000)
        if self._observations[0].now_ns < cutoff:
            self._observations = [
                item for item in self._observations if item.now_ns >= cutoff
            ]
        # 판단 주기가 아주 빨라도 기록이 무한히 자라지 않게 한다.
        if len(self._observations) > 120:
            del self._observations[:-120]

    def _observation(
        self, view: _View, now_ns: int, *, counterpart: int | None = None
    ) -> _Observation:
        """현재 상태로 관찰 하나를 만든다.

        `counterpart`를 주면 그 차(추월 대상)의 텔레메트리를 비교 대상으로
        쓴다. 생략하면 지금 바로 앞차를 쓴다.
        """
        index = view.ahead_index if counterpart is None else counterpart
        player_telemetry = _entry_at(view.telemetry, view.player_index)
        ahead_telemetry = _entry_at(view.telemetry, index)
        return _Observation(
            now_ns=now_ns,
            ahead_index=index,
            gap_s=view.gap_s if counterpart is None else None,
            drs_allowed=_integer(view.player_status.get("drs_allowed")),
            ers_mode=_integer(view.player_status.get("ers_deploy_mode")),
            ers_energy_j=_number(view.player_status.get("ers_store_energy")),
            player_speed=_number(player_telemetry.get("speed")),
            player_throttle=_number(player_telemetry.get("throttle")),
            ahead_speed=_number(ahead_telemetry.get("speed")),
            ahead_throttle=_number(ahead_telemetry.get("throttle")),
        )

    def _update_pit_memory(self, view: _View, now_ns: int) -> None:
        """차가 피트 레인·인랩·아웃랩에 있던 마지막 시각을 기억한다.

        피트 스톱 횟수가 늘어난 것도 같은 시각으로 친다. 추월 대상 차가
        최근에 피트했다면 순위 변동을 추월로 칭찬하지 않는다.
        """
        for index, entry in enumerate(view.entries):
            if not entry:
                continue
            if _pit_related(entry):
                self._pit_seen_ns[index] = now_ns
            stops = _integer(entry.get("num_pit_stops"))
            if stops is None:
                continue
            previous = self._pit_stops.get(index)
            if previous is not None and stops > previous:
                self._pit_seen_ns[index] = now_ns
            self._pit_stops[index] = stops

    def _begin_success(self, view: _View, now_ns: int, overtaken: int) -> None:
        """추월을 감지했다. 조건이 맞으면 안정 시간 확인을 시작한다."""
        # 연속 추월이면 앞 건은 안정 시간을 채우지 못했으므로 버린다.
        self._pending_success = None
        # 1랩은 스타트 혼전이라 순위 변동이 실제 추월이 아닐 수 있다.
        if view.lap is not None and view.lap < 2:
            return
        if _not_on_track(view.player_entry):
            return
        entry = _entry_at(view.entries, overtaken)
        if _not_on_track(entry):
            return
        if self._recent_pit(overtaken, now_ns):
            return

        window_ns = int(SUCCESS_EVIDENCE_WINDOW_S * 1_000_000_000)
        observations = tuple(
            item
            for item in self._observations
            if item.ahead_index == overtaken
            and item.now_ns >= now_ns - window_ns
        )
        gaps = [item.gap_s for item in observations if item.gap_s is not None]
        self._pending_success = _PendingSuccess(
            overtaken_index=overtaken,
            detected_ns=now_ns,
            lap=view.lap,
            track_id=view.track_id,
            session_type=view.session_type,
            distance=view.distance,
            close_battle=any(gap <= CLOSE_OVERTAKE_GAP_S for gap in gaps),
            last_laps=view.last_laps,
            observations=observations,
            pass_observation=self._observation(
                view, now_ns, counterpart=overtaken
            ),
            baselines=_baselines(view),
        )

    def _recent_pit(self, car_index: int, now_ns: int) -> bool:
        seen = self._pit_seen_ns.get(car_index)
        if seen is None:
            return False
        return now_ns - seen <= int(PIT_RECENT_S * 1_000_000_000)

    def _observe_success(
        self,
        view: _View,
        now_ns: int,
        traces_by_car: TraceSource | None,
    ) -> RadioMessage | None:
        """확인 중인 추월이 안정 시간을 채웠으면 칭찬 무전 하나를 만든다."""
        pending = self._pending_success
        if pending is None:
            return None
        pending.checks += 1
        if not self._success_stable(pending, view):
            # 재역전·피트 변동·트랙 이탈·접촉. 이 추월은 칭찬하지 않는다.
            self._pending_success = None
            return None
        if pending.checks < SUCCESS_MIN_CHECKS:
            return None
        if now_ns - pending.detected_ns < int(
            SUCCESS_CONFIRM_S * 1_000_000_000
        ):
            return None

        factor = self._choose_success_factor(pending, view, traces_by_car)
        self._pending_success = None
        if factor is None:
            # 문턱을 넘는 근거가 없다. 억지 칭찬은 하지 않는다.
            return None
        return self._success_message(pending, factor, now_ns)

    def _success_stable(self, pending: _PendingSuccess, view: _View) -> bool:
        """재역전·피트 변동·트랙 이탈·접촉 없이 추월이 유지되는가."""
        if view.player_index is None or view.lap is None:
            return False
        if _not_on_track(view.player_entry):
            return False
        if _integer(view.player_entry.get("driver_status")) == DRIVER_OFF_TRACK:
            return False
        if _integer(view.player_entry.get("current_lap_invalid")):
            return False
        if _penalty_grew(pending.baselines, view):
            return False
        if not self._is_behind(pending.overtaken_index, view):
            # 그 차가 다시 앞으로 나갔다(재역전).
            return False
        entry = _entry_at(view.entries, pending.overtaken_index)
        if _not_on_track(entry):
            return False
        if _integer(entry.get("driver_status")) == DRIVER_OFF_TRACK:
            return False
        return True

    def _choose_success_factor(
        self,
        pending: _PendingSuccess,
        view: _View,
        traces_by_car: TraceSource | None,
    ) -> _SuccessFactor | None:
        """추월 직전 자료에서 가장 설득력 있는 기여 요인 하나를 고른다."""
        samples = (*pending.observations, pending.pass_observation)
        candidates = _drs_ers_factors(samples)
        candidates.extend(self._corner_factors(pending, view, traces_by_car))
        candidates.extend(_telemetry_factors(samples))
        if not candidates:
            return None
        # 점수가 같으면 먼저 넣은 요인(DRS/ERS > 코너 > 텔레메트리)을 쓴다.
        return max(candidates, key=lambda item: item.score)

    def _corner_factors(
        self,
        pending: _PendingSuccess,
        view: _View,
        traces_by_car: TraceSource | None,
    ) -> list[_SuccessFactor]:
        """추월 지점 직전 코너의 탈출 속도·브레이킹을 비교한다."""
        track = self._track(view.track_id)
        if track is None or track.length_m <= 0 or not track.corners:
            return []
        corner = _pass_corner(track, pending.distance)
        if corner is None:
            return []
        player_trace = _latest_valid(_traces_for(traces_by_car, view.player_index))
        ahead_trace = _latest_valid(
            _traces_for(traces_by_car, pending.overtaken_index)
        )
        if player_trace is None or ahead_trace is None:
            return []
        player_metrics = corner_metrics(player_trace, corner)
        ahead_metrics = corner_metrics(ahead_trace, corner)
        label = corner_label(corner.number, official=corner.official)
        common = {
            "corner": corner.number,
            "corner_official": corner.official,
            "player_lap": player_trace.lap,
            "ahead_lap": ahead_trace.lap,
        }
        factors: list[_SuccessFactor] = []

        exit_advantage = player_metrics.exit_speed - ahead_metrics.exit_speed
        if exit_advantage >= SUCCESS_EXIT_ADVANTAGE_KMH:
            factors.append(
                _SuccessFactor(
                    "exit",
                    1.0 + min(0.4, exit_advantage / 20.0),
                    {"corner_label": label, "kmh": int(round(exit_advantage))},
                    {
                        **common,
                        "player_exit_speed_kmh": round(
                            player_metrics.exit_speed, 1
                        ),
                        "ahead_exit_speed_kmh": round(
                            ahead_metrics.exit_speed, 1
                        ),
                        "exit_advantage_kmh": round(exit_advantage, 1),
                    },
                    corner=corner.number,
                )
            )

        if (
            player_metrics.brake_start_m is not None
            and ahead_metrics.brake_start_m is not None
        ):
            late_brake = (
                player_metrics.brake_start_m - ahead_metrics.brake_start_m
            )
            if late_brake >= SUCCESS_LATE_BRAKE_M:
                factors.append(
                    _SuccessFactor(
                        "brake",
                        0.9 + min(0.4, late_brake / 25.0),
                        {
                            "corner_label": label,
                            "meters": int(round(late_brake)),
                        },
                        {
                            **common,
                            "player_brake_start_m": round(
                                player_metrics.brake_start_m, 1
                            ),
                            "ahead_brake_start_m": round(
                                ahead_metrics.brake_start_m, 1
                            ),
                            "late_brake_m": round(late_brake, 1),
                        },
                        corner=corner.number,
                    )
                )
        return factors

    def _success_message(
        self,
        pending: _PendingSuccess,
        factor: _SuccessFactor,
        now_ns: int,
    ) -> RadioMessage | None:
        mood = mood_for(
            last_laps=pending.last_laps,
            close_battle=pending.close_battle,
            praise=True,
        )
        key = f"{SUCCESS_TEMPLATE_PREFIX}{factor.kind}"
        text = self._variants.render(key, mood=mood, **factor.slots)
        evidence: dict[str, Any] = {
            "overtake_success": factor.kind,
            "mood": mood.value,
            "session_type": pending.session_type,
            "track_id": pending.track_id,
            "lap": pending.lap,
            "overtaken_car_index": pending.overtaken_index,
            "confirm_s": SUCCESS_CONFIRM_S,
            "checks": pending.checks,
            "close_battle": pending.close_battle,
            **factor.evidence,
        }
        message = RadioMessage.create(
            RadioKind.OVERTAKE.value,
            text,
            now_ns=now_ns,
            ttl_ns=int(SUCCESS_TTL_S * 1_000_000_000),
            evidence=evidence,
            lap=pending.lap,
            corner=factor.corner,
        )
        # 대시보드에서 추월 무전을 끈 경우 큐가 거부한다(설계 6.7).
        if self._queue is not None and not self._queue.push(message):
            return None
        return message

    # --- 선택지 -----------------------------------------------------------

    def _options(
        self,
        view: _View,
        traces_by_car: TraceSource | None,
    ) -> list[_Option]:
        gap = view.gap_s or 0.0
        closing = (
            self._gap_change is not None
            and self._gap_change <= -self._config.gap_closing_s_per_s
        )
        common: dict[str, Any] = {
            "gap_s": round(gap, 3),
            "gap_change_s_per_s": (
                None if self._gap_change is None else round(self._gap_change, 3)
            ),
            "ahead_car_index": view.ahead_index,
            "lap": view.lap,
            "session_type": view.session_type,
            "track_id": view.track_id,
        }

        candidates = [
            option
            for option in (
                self._drs_option(view, gap, closing, common),
                self._slow_exit_option(view, traces_by_car, gap, closing, common),
                self._tyre_option(view, gap, common),
            )
            if option is not None
        ]
        # 점수가 같으면 DRS > 느린 탈출 > 타이어 기다림 순서를 지킨다(안정 정렬).
        candidates.sort(key=lambda option: -option.score)
        return candidates

    def _drs_option(
        self, view: _View, gap: float, closing: bool, common: dict[str, Any]
    ) -> _Option | None:
        status = view.player_status
        if _integer(status.get("drs_allowed")) != 1:
            return None
        activation = _number(status.get("drs_activation_distance"))
        if (
            activation is not None
            and activation > self._config.drs_activation_window_m
        ):
            return None

        ers_pct = _ers_percent(status.get("ers_store_energy"))
        mode = _integer(status.get("ers_deploy_mode"))
        use_ers = (
            ers_pct is not None
            and ers_pct >= self._config.ers_deploy_min_pct
            and mode != ERS_DEPLOY_OVERTAKE
        )
        key = "overtake.drs_ers" if use_ers else "overtake.drs"
        closeness = max(0.0, 1.0 - gap / self._config.gap_s)
        score = (
            1.0
            + 0.4 * closeness
            + (0.1 if closing else 0.0)
            + (0.15 if use_ers else 0.0)
        )
        evidence = dict(common)
        evidence.update(
            {
                "option": "drs",
                "drs_allowed": 1,
                "drs_activation_distance_m": (
                    None if activation is None else int(activation)
                ),
                "ers_store_pct": None if ers_pct is None else round(ers_pct, 1),
                "ers_deploy_mode": mode,
                "use_ers": use_ers,
            }
        )
        return _Option(
            key=key,
            score=score,
            slots={"gap": _gap_text(gap)},
            ttl_s=DRS_TTL_S,
            evidence=evidence,
        )

    def _slow_exit_option(
        self,
        view: _View,
        traces_by_car: TraceSource | None,
        gap: float,
        closing: bool,
        common: dict[str, Any],
    ) -> _Option | None:
        track = self._track(view.track_id)
        if track is None or track.length_m <= 0 or not track.corners:
            return None
        player_trace = _latest_valid(_traces_for(traces_by_car, view.player_index))
        ahead_trace = _latest_valid(_traces_for(traces_by_car, view.ahead_index))
        if player_trace is None or ahead_trace is None:
            return None

        best: tuple[float, Corner, float, float, float] | None = None
        for corner in track.corners:
            distance_to_corner = (corner.entry_m - view.distance) % track.length_m
            if distance_to_corner > self._config.slow_exit_lookahead_m:
                continue
            player_exit = self._exit_speed(player_trace, corner)
            ahead_exit = self._exit_speed(ahead_trace, corner)
            deficit = player_exit - ahead_exit
            if deficit < self._config.slow_exit_deficit_kmh:
                continue
            if best is None or deficit > best[0]:
                best = (deficit, corner, player_exit, ahead_exit, distance_to_corner)
        if best is None:
            return None

        deficit, corner, player_exit, ahead_exit, distance_to_corner = best
        score = 0.9 + min(0.6, deficit / 15.0) + (0.1 if closing else 0.0)
        evidence = dict(common)
        evidence.update(
            {
                "option": "slow_exit",
                "corner": corner.number,
                "corner_official": corner.official,
                "corner_entry_m": round(corner.entry_m, 1),
                "distance_to_corner_m": round(distance_to_corner, 1),
                "player_exit_speed_kmh": round(player_exit, 1),
                "ahead_exit_speed_kmh": round(ahead_exit, 1),
                "exit_deficit_kmh": round(deficit, 1),
                "player_lap": player_trace.lap,
                "ahead_lap": ahead_trace.lap,
            }
        )
        return _Option(
            key="overtake.slow_exit",
            score=score,
            slots={"gap": _gap_text(gap)},
            ttl_s=SLOW_EXIT_TTL_S,
            evidence=evidence,
            corner=corner.number,
            corner_render=True,
            official=corner.official,
        )

    def _tyre_option(
        self, view: _View, gap: float, common: dict[str, Any]
    ) -> _Option | None:
        my_age = _integer(view.player_status.get("tyres_age_laps"))
        ahead_age = _integer(view.ahead_status.get("tyres_age_laps"))
        if my_age is None or ahead_age is None:
            return None
        advantage = ahead_age - my_age
        if advantage < self._config.tyre_advantage_laps:
            return None

        my_compound = _integer(view.player_status.get("actual_tyre_compound"))
        ahead_compound = _integer(view.ahead_status.get("actual_tyre_compound"))
        score = 0.4 + min(0.4, advantage / 20.0)
        if _harder_compound(my_compound, ahead_compound):
            # 더 단단한 타이어로 버티는 쪽이 뒤로 갈수록 유리해진다.
            score += 0.1
        evidence = dict(common)
        evidence.update(
            {
                "option": "tyre_wait",
                "player_tyre_age_laps": my_age,
                "ahead_tyre_age_laps": ahead_age,
                "tyre_advantage_laps": advantage,
                "player_compound": my_compound,
                "ahead_compound": ahead_compound,
                "wait_laps": self._config.tyre_wait_laps,
            }
        )
        return _Option(
            key="overtake.tyre_wait",
            score=score,
            slots={
                "gap": _gap_text(gap),
                "laps": advantage,
                "wait_laps": self._config.tyre_wait_laps,
            },
            ttl_s=TYRE_WAIT_TTL_S,
            evidence=evidence,
        )

    # --- 트랙·코너 ---------------------------------------------------------

    def _track(self, track_id: int | None) -> Track | None:
        """서킷 데이터. 없는 트랙이면 `None`(코너 선택지만 빠진다)."""
        if self._fixed_track is not None:
            return self._fixed_track
        if track_id is None:
            return None
        if track_id not in self._tracks:
            try:
                self._tracks[track_id] = load_track(int(track_id))
            except (OSError, ValueError):
                # 데이터 파일이 깨져도 추월 판단 전체를 멈추지 않는다.
                self._tracks[track_id] = None
        return self._tracks[track_id]

    def _exit_speed(self, trace: LapTrace, corner: Corner) -> float:
        """코너 탈출 속도(km/h). 같은 랩 트레이스는 한 번만 계산해 캐시한다."""
        cached = self._exit_cache.get(trace.car_index)
        if cached is None or cached[0] is not trace:
            cached = (trace, {})
            self._exit_cache[trace.car_index] = cached
        speeds = cached[1]
        if corner.number not in speeds:
            speeds[corner.number] = corner_metrics(trace, corner).exit_speed
        return speeds[corner.number]


# ---------------------------------------------------------------------------
# 스냅샷 읽기


def _view(snapshot: Mapping[str, Any] | None) -> _View:
    source: Mapping[str, Any] = snapshot if isinstance(snapshot, Mapping) else {}
    packets = _mapping(source.get("packets"))
    session = _mapping(packets.get("session"))
    lap_body = _mapping(packets.get("lap_data"))
    entries = _mapping_list(lap_body.get("lap_data"))
    statuses = _mapping_list(
        _mapping(packets.get("car_status")).get("car_status_data")
    )
    telemetry = _mapping_list(
        _mapping(packets.get("car_telemetry")).get("car_telemetry_data")
    )

    player_index = _integer(source.get("player_car_index"))
    if player_index is not None and not 0 <= player_index < len(entries):
        player_index = None
    player_entry: Mapping[str, Any] = (
        entries[player_index] if player_index is not None else {}
    )
    player_status: Mapping[str, Any] = (
        statuses[player_index]
        if player_index is not None and player_index < len(statuses)
        else {}
    )

    ahead_index = _find_ahead(entries, player_index, player_entry)
    ahead_status: Mapping[str, Any] = (
        statuses[ahead_index]
        if ahead_index is not None and ahead_index < len(statuses)
        else {}
    )

    session_type = _integer(session.get("session_type"))
    lap = _integer(player_entry.get("current_lap_num"))
    total_laps = _integer(session.get("total_laps"))
    # 레이스가 아니거나 total_laps가 0·없으면 마지막 랩 판정을 하지 않는다.
    remaining_laps = remaining_race_laps(session_type, total_laps, lap)

    return _View(
        session_uid=_integer(source.get("session_uid")),
        session_type=session_type,
        track_id=_integer(session.get("track_id")),
        safety_car=_integer(session.get("safety_car_status")) in (1, 2),
        player_index=player_index,
        entries=entries,
        telemetry=telemetry,
        player_entry=player_entry,
        player_status=player_status,
        ahead_index=ahead_index,
        ahead_status=ahead_status,
        lap=lap,
        distance=_number(player_entry.get("lap_distance")) or 0.0,
        gap_s=_gap_seconds(player_entry) if ahead_index is not None else None,
        last_laps=is_last_laps(remaining_laps),
    )


def _find_ahead(
    entries: tuple[Mapping[str, Any], ...],
    player_index: int | None,
    player_entry: Mapping[str, Any],
) -> int | None:
    """플레이어 바로 앞 차의 인덱스. 순위 값이 없으면 주행 거리로 찾는다."""
    if player_index is None:
        return None
    position = _integer(player_entry.get("car_position"))
    if position is not None and position > 1:
        for index, entry in enumerate(entries):
            if index == player_index:
                continue
            if _integer(entry.get("car_position")) == position - 1:
                return index
        # 순위 값이 비어 있는 차(리타이어 등)면 거리로 다시 찾는다.
    if position == 1:
        return None

    player_total = _number(player_entry.get("total_distance"))
    if player_total is None:
        return None
    best: int | None = None
    best_gap = math.inf
    for index, entry in enumerate(entries):
        if index == player_index:
            continue
        total = _number(entry.get("total_distance"))
        if total is None or total <= player_total:
            continue
        gap = total - player_total
        if gap < best_gap:
            best = index
            best_gap = gap
    return best


def _gap_seconds(entry: Mapping[str, Any]) -> float | None:
    """Lap Data의 앞차와의 간격(초). 분 파트 + ms 파트."""
    minutes = _integer(entry.get("delta_to_car_in_front_minutes_part"))
    milliseconds = _integer(entry.get("delta_to_car_in_front_ms_part"))
    if minutes is None and milliseconds is None:
        return None
    return max(0, minutes or 0) * 60.0 + max(0, milliseconds or 0) / 1000.0


# ---------------------------------------------------------------------------
# 추월 성공 판정 도우미


def _entry_at(
    entries: Sequence[Mapping[str, Any]], index: int | None
) -> Mapping[str, Any]:
    """차 인덱스로 항목 하나를 꺼낸다. 범위 밖이면 빈 매핑."""
    if index is None or not 0 <= index < len(entries):
        return {}
    return entries[index]


def _not_on_track(entry: Mapping[str, Any]) -> bool:
    """차가 주행 중이 아닌가(차고·인랩·아웃랩·피트·리타이어)."""
    result_status = _integer(entry.get("result_status"))
    if result_status is not None and result_status != 2:
        return True
    if _integer(entry.get("driver_status")) in {
        0,
        DRIVER_IN_LAP,
        DRIVER_OUT_LAP,
        DRIVER_IN_PIT,
    }:
        return True
    return _integer(entry.get("pit_status")) in PIT_LANE_STATUSES


def _pit_related(entry: Mapping[str, Any]) -> bool:
    """피트 레인·인랩·아웃랩에 있던 상태인가(피트인 추월 판정)."""
    if _integer(entry.get("pit_status")) in PIT_LANE_STATUSES:
        return True
    return _integer(entry.get("driver_status")) in {
        DRIVER_IN_LAP,
        DRIVER_OUT_LAP,
        DRIVER_IN_PIT,
    }


def _pass_corner(track: Track, distance: float) -> Corner | None:
    """추월 지점에서 가장 가까운 직전 코너. 코너 안에서 넘었으면 그 코너."""
    if track.length_m <= 0 or not track.corners:
        return None
    best: Corner | None = None
    best_gap = SUCCESS_CORNER_LOOKBACK_M
    for corner in track.corners:
        if corner.entry_m <= distance <= corner.exit_m:
            return corner
        gap = (distance - corner.exit_m) % track.length_m
        if gap <= best_gap:
            best = corner
            best_gap = gap
    return best


def _baselines(view: _View) -> dict[str, float | None]:
    """추월 시점의 페널티·경고·손상 누적값. 늘어나면 접촉으로 본다."""
    values: dict[str, float | None] = {}
    for name in LAP_PENALTY_FIELDS:
        value = _integer(view.player_entry.get(name))
        values[name] = None if value is None else float(value)
    for name in DAMAGE_FIELDS:
        values[name] = _number(view.player_status.get(name))
    return values


def _penalty_grew(
    baselines: dict[str, float | None], view: _View
) -> bool:
    """추월 시점보다 페널티·경고·손상 값이 늘었는가(접촉·위반)."""
    for name in LAP_PENALTY_FIELDS:
        value = _integer(view.player_entry.get(name))
        if _grew(baselines, name, None if value is None else float(value)):
            return True
    for name in DAMAGE_FIELDS:
        if _grew(baselines, name, _number(view.player_status.get(name))):
            return True
    return False


def _grew(
    baselines: dict[str, float | None], name: str, value: float | None
) -> bool:
    """누적값이 기준보다 늘었는가. 기준이 없던 값은 이번에 채운다."""
    if value is None:
        return False
    previous = baselines.get(name)
    baselines[name] = value
    return previous is not None and value > previous


def _drs_ers_factors(
    samples: Sequence[_Observation],
) -> list[_SuccessFactor]:
    """추월 직전 DRS 허용·ERS 배포 근거로 기여 요인을 만든다."""
    drs_samples = sum(1 for item in samples if item.drs_allowed == 1)
    ers_mode = any(item.ers_mode == ERS_DEPLOY_OVERTAKE for item in samples)
    energies = [
        item.ers_energy_j for item in samples if item.ers_energy_j is not None
    ]
    drop_j = 0.0
    if len(energies) >= 2:
        drop_j = max(0.0, max(energies) - min(energies))
    ers = ers_mode or drop_j >= ERS_STORE_MAX_J * SUCCESS_ERS_DEPLOY_PCT / 100.0
    evidence: dict[str, Any] = {
        "drs_samples": drs_samples,
        "ers_mode_overtake": ers_mode,
        "ers_energy_drop_j": int(round(drop_j)),
    }
    factors: list[_SuccessFactor] = []
    if drs_samples and ers:
        factors.append(
            _SuccessFactor(
                "drs_ers",
                1.5,
                {},
                {**evidence, "drs_used": True, "ers_used": True},
            )
        )
    if drs_samples:
        factors.append(
            _SuccessFactor(
                "drs", 1.3, {}, {**evidence, "drs_used": True}
            )
        )
    if ers:
        factors.append(
            _SuccessFactor(
                "ers", 1.2, {}, {**evidence, "ers_used": True}
            )
        )
    return factors


def _usable_speed_pair(item: _Observation) -> bool:
    """두 차의 속도가 실제 값인가.

    일부 세션(온라인 등)은 다른 차 Car Telemetry를 0으로 보낸다. 그 값을
    그대로 비교하면 "스로틀·속도 우위"를 항상 칭찬하게 되므로 걸러낸다.
    """
    return (
        item.player_speed is not None
        and item.ahead_speed is not None
        and item.player_speed > 0
        and item.ahead_speed > 0
    )


def _telemetry_factors(
    samples: Sequence[_Observation],
) -> list[_SuccessFactor]:
    """추월 직전 스로틀·속도 우위로 기여 요인을 만든다."""
    usable = [item for item in samples if _usable_speed_pair(item)]
    throttle_pairs = [
        (item.player_throttle, item.ahead_throttle)
        for item in usable
        if item.player_throttle is not None and item.ahead_throttle is not None
    ]
    speed_pairs = [
        (item.player_speed, item.ahead_speed)
        for item in usable
        if item.player_speed is not None and item.ahead_speed is not None
    ]
    factors: list[_SuccessFactor] = []

    if len(throttle_pairs) >= SUCCESS_MIN_TELEMETRY_SAMPLES:
        count = len(throttle_pairs)
        player_avg = sum(player for player, _ in throttle_pairs) / count
        ahead_avg = sum(ahead for _, ahead in throttle_pairs) / count
        difference = player_avg - ahead_avg
        if difference >= SUCCESS_THROTTLE_ADVANTAGE:
            factors.append(
                _SuccessFactor(
                    "throttle",
                    0.8 + min(0.35, difference),
                    {"percent": int(round(difference * 100))},
                    {
                        "samples": count,
                        "player_throttle_pct": round(player_avg * 100.0, 1),
                        "ahead_throttle_pct": round(ahead_avg * 100.0, 1),
                        "throttle_advantage_pct": round(difference * 100.0, 1),
                    },
                )
            )

    if len(speed_pairs) >= SUCCESS_MIN_TELEMETRY_SAMPLES:
        count = len(speed_pairs)
        player_avg = sum(player for player, _ in speed_pairs) / count
        ahead_avg = sum(ahead for _, ahead in speed_pairs) / count
        difference = player_avg - ahead_avg
        if difference >= SUCCESS_SPEED_ADVANTAGE_KMH:
            factors.append(
                _SuccessFactor(
                    "clean",
                    0.7 + min(0.25, difference / 20.0),
                    {},
                    {
                        "samples": count,
                        "player_speed_kmh": round(player_avg, 1),
                        "ahead_speed_kmh": round(ahead_avg, 1),
                        "speed_advantage_kmh": round(difference, 1),
                    },
                )
            )
    return factors


# ---------------------------------------------------------------------------
# 값 변환


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> tuple[Mapping[str, Any], ...]:
    """차 배열을 인덱스가 밀리지 않게 매핑/빈 매핑의 튜플로 바꾼다."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item if isinstance(item, Mapping) else {} for item in value)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _ers_percent(value: Any) -> float | None:
    joules = _number(value)
    if joules is None:
        return None
    return max(0.0, min(100.0, joules / ERS_STORE_MAX_J * 100.0))


def _gap_text(gap: float) -> str:
    return f"{gap:.1f}"


def _harder_compound(my: int | None, ahead: int | None) -> bool:
    """내 컴파운드가 더 단단한가(슬릭 코드 15~20은 클수록 단단하다)."""
    if my is None or ahead is None:
        return False
    if not (15 <= my <= 20 and 15 <= ahead <= 20):
        # 인터미디어트·웻은 나이 비교만 하고 컴파운드 우열은 따지지 않는다.
        return False
    return my > ahead


def _traces_for(
    traces_by_car: TraceSource | None,
    car_index: int | None,
) -> list[LapTrace]:
    if traces_by_car is None or car_index is None:
        return []
    value: Any
    if isinstance(traces_by_car, Mapping):
        value = traces_by_car.get(car_index)
    elif isinstance(traces_by_car, Sequence) and 0 <= car_index < len(traces_by_car):
        # 차 인덱스로 바로 찾는 배열 형태도 받는다.
        value = traces_by_car[car_index]
    else:
        return []
    if value is None:
        return []
    if isinstance(value, LapTrace):
        return [value]
    try:
        return [item for item in value if isinstance(item, LapTrace)]
    except TypeError:
        return []


def _latest_valid(traces: Sequence[LapTrace]) -> LapTrace | None:
    valid = [trace for trace in traces if trace.valid and len(trace.samples) >= 2]
    if not valid:
        return None
    # 랩 번호가 큰 쪽이 최신이다. 같은 랩이면 샘플이 많은 쪽.
    return max(valid, key=lambda trace: (trace.lap, len(trace.samples)))


__all__ = [
    "DEFAULT_DRS_ACTIVATION_WINDOW_M",
    "DEFAULT_SLOW_EXIT_DEFICIT_KMH",
    "DEFAULT_SLOW_EXIT_LOOKAHEAD_M",
    "DEFAULT_TYRE_ADVANTAGE_LAPS",
    "DEFAULT_TYRE_WAIT_LAPS",
    "DRS_TTL_S",
    "ERS_DEPLOY_OVERTAKE",
    "ERS_STORE_MAX_J",
    "OvertakeAdvisor",
    "OvertakeConfig",
    "PIT_RECENT_S",
    "SLOW_EXIT_TTL_S",
    "SUCCESS_CONFIRM_S",
    "SUCCESS_CORNER_LOOKBACK_M",
    "SUCCESS_EVIDENCE_WINDOW_S",
    "SUCCESS_EXIT_ADVANTAGE_KMH",
    "SUCCESS_HISTORY_S",
    "SUCCESS_LATE_BRAKE_M",
    "SUCCESS_MIN_CHECKS",
    "SUCCESS_MIN_TELEMETRY_SAMPLES",
    "SUCCESS_SPEED_ADVANTAGE_KMH",
    "SUCCESS_THROTTLE_ADVANTAGE",
    "SUCCESS_TTL_S",
    "TYRE_WAIT_TTL_S",
]
