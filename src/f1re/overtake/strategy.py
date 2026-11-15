"""서킷 추월 지점 기반 공격·방어 전략 규칙 엔진(SCRUM-130).

`tracks/model.py`의 `OvertakingSpot`(공격 코너·준비 코너·방어 라인·난이도·무전
거리 창)과 실시간 레이스 상황을 합쳐 **공격(추월)과 방어 중 무엇을 언제 말할지**
정한다. 문장 생성은 이 모듈이 하지 않는다. 여기서는 구조화된 사실(`facts`)만
만들고, `radio/strategy.py`의 `StrategyNarrator`가 Qwen LLM 또는 템플릿으로
문장을 만든다.

판단 규칙
---------
- **레이스 전용**: `is_race_session()`이 참인 세션에서만 동작한다. 타임 트라이얼·
  연습·예선에서는 말하지 않는다.
- **제외 조건**: SC·VSC(`safety_car_status` 1·2), 피트 레인 안, 리타이어한 내 차.
- **공격**: 내 Lap Data의 앞차와의 간격이 `attack_gap_s`(기본 1.0초) 이하이고,
  지금이 어느 추월 지점의 무전 창(`entry_window_m`, 없으면 마지막 준비 코너
  탈출~공격 코너 진입) 안일 때. 간격·DRS·ERS·타이어 나이 차이·앞차의 느린
  탈출·남은 랩·지점까지 거리·난이도를 점수로 합쳐 지점을 고른다.
- **방어**: 뒤차와의 간격이 `defense_gap_s`(기본 1.0초) 이하일 때 같은 무전 창
  안에서 다음 추월 지점의 방어 계획을 고른다. 뒤차 간격은 그 차의 Lap Data
  `delta_to_car_in_front`(바로 앞이 나)를 쓴다.
- **동시 접근 우선순위**: 앞·뒤가 모두 문턱 안이면 간격이 더 좁은 쪽을 먼저
  고르고, 간격이 같으면 방어를 먼저 고른다.
- **빈도**: 공격과 방어를 **따로** 센다. 설정의 `overtake_per_lap`(기본 1)을
  각각에 적용해 한 랩에 공격 1회·방어 1회까지 말할 수 있다. 앞쪽이 소진됐으면
  같은 순간에 뒤쪽으로 넘어간다.
- **하위 호환**: `overtaking_spots`가 없는 트랙(모나코 등)에서는 이 엔진이
  아무 계획도 만들지 않는다. 파이프라인은 그런 트랙에서 기존
  `overtake/advisor.py`의 추월 무전(DRS·느린 탈출·타이어 기다림)을 그대로 쓴다.

무전 시점은 `OvertakingSpot.entry_window_m`을 우선한다. 이 값은 서킷 데이터를
만들 때 "이 지점의 공격·방어 무전을 내기 좋은 랩 거리 창"으로 정리한 값이다.
값이 없는 트랙은 마지막 준비 코너 탈출부터 첫 공격 코너 진입까지를 창으로
삼는다(결승선을 넘는 구간도 지원).
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
    DRIVER_OUT_LAP,
    PIT_LANE_STATUSES,
    LapTrace,
)
from f1re.overtake.advisor import ERS_STORE_MAX_J
from f1re.race_distance import remaining_race_laps
from f1re.radio.templates import corner_label, is_last_laps
from f1re.tracks.model import Corner, OvertakingSpot, Track
from f1re.udp.session_category import is_race_session

# 공격·방어 무전의 유효 시간(초). 지점을 지나기 전에 말을 시작해야 하므로
# 한 랩(스즈카 데모 기준 85~100초)보다 짧게 둔다.
ATTACK_TTL_S = 20.0
DEFENSE_TTL_S = 20.0

# `entry_window_m`이 없는 트랙에서 공격 코너 진입 앞으로 잡는 기본 창 길이(m).
DEFAULT_ENTRY_WINDOW_M = 250.0
# DRS 활성 지점이 이보다 멀면 "지금 DRS로 붙는다"고 보지 않는다.
DEFAULT_DRS_READY_M = 600.0

# 난이도에 따른 공격 점수 가감. 낮은 난이도일수록 공격을 권한다.
DIFFICULTY_ATTACK_FACTORS: dict[str, float] = {
    "low": 0.10,
    "medium": 0.0,
    "high": -0.15,
    "very_high": -0.35,
}

# `avoid_reason`이 있는 지점(스즈카 130R)은 기본적으로 공격하지 않는다.
# 아래 조건(아주 가까운 간격 + 큰 타이어 우위)을 모두 만족할 때만 예외로 둔다.
AVOID_PENALTY = -0.60
DOMINANT_GAP_S = 0.3
DOMINANT_TYRE_ADVANTAGE_LAPS = 8

# 차 인덱스 → 그 차의 완료 랩들. 배열(인덱스 순)로 줘도 된다.
TraceSource = (
    Mapping[int, Sequence[LapTrace] | LapTrace]
    | Sequence[Sequence[LapTrace] | LapTrace]
)


@dataclass(frozen=True)
class StrategyConfig:
    """공격·방어 규칙 문턱값.

    `from_app_config()`는 기존 `overtake_gap_s`·`overtake_per_lap`을 그대로
    쓴다. 공격과 방어는 같은 문턱값을 각각 따로 센다.
    """

    attack_gap_s: float = 1.0
    defense_gap_s: float = 1.0
    attack_per_lap: int = 1
    defense_per_lap: int = 1
    ers_min_pct: float = 30.0
    tyre_advantage_laps: int = 4
    slow_exit_deficit_kmh: float = 4.0
    drs_ready_m: float = DEFAULT_DRS_READY_M
    default_window_m: float = DEFAULT_ENTRY_WINDOW_M

    def __post_init__(self) -> None:
        for name in ("attack_gap_s", "defense_gap_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name}은 0보다 큰 유한한 값이어야 합니다")
        for name in ("attack_per_lap", "defense_per_lap"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name}은 1 이상이어야 합니다")
        for name in ("ers_min_pct", "slow_exit_deficit_kmh", "drs_ready_m"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name}은 0 이상의 유한한 값이어야 합니다")
        if self.tyre_advantage_laps < 1:
            raise ValueError("tyre_advantage_laps는 1 이상이어야 합니다")
        if not math.isfinite(self.default_window_m) or self.default_window_m < 0:
            raise ValueError("default_window_m은 0 이상의 유한한 값이어야 합니다")

    @classmethod
    def from_app_config(cls, config: AppConfig) -> StrategyConfig:
        """앱 설정의 `overtake_gap_s`·`overtake_per_lap`을 공격·방어에 각각 쓴다."""
        return cls(
            attack_gap_s=config.overtake_gap_s,
            defense_gap_s=config.overtake_gap_s,
            attack_per_lap=config.overtake_per_lap,
            defense_per_lap=config.overtake_per_lap,
        )


@dataclass(frozen=True)
class StrategyPlan:
    """규칙 엔진이 고른 공격 또는 방어 계획 하나.

    `facts`는 그대로 LLM 프롬프트와 무전 기록(`evidence`)에 들어가는 구조화된
    판단 근거다. `template_key`는 LLM을 쓸 수 없을 때 쓸 폴백 틀이다.
    """

    side: str
    spot: OvertakingSpot
    score: float
    gap_s: float
    lap: int | None
    remaining_laps: float | None
    facts: dict[str, Any]
    template_key: str
    ttl_s: float
    corner: int | None = None

    def __post_init__(self) -> None:
        if self.side not in ("attack", "defense"):
            raise ValueError(f"알 수 없는 전략 종류: {self.side!r}")

    @property
    def llm_kind(self) -> str:
        """LLM 프롬프트에 넣는 무전 종류 이름."""
        return f"overtake.{self.side}"


@dataclass(frozen=True)
class _SlowExit:
    """앞차가 나보다 느리게 탈출하는 코너 하나."""

    corner: int
    deficit_kmh: float
    player_kmh: float
    ahead_kmh: float


@dataclass(frozen=True)
class _View:
    """스냅샷에서 전략 판단에 필요한 값만 꺼낸 것."""

    session_uid: int | None
    session_type: int | None
    track_id: int | None
    safety_car: bool
    player_index: int | None
    entries: tuple[Mapping[str, Any], ...]
    player_entry: Mapping[str, Any]
    player_status: Mapping[str, Any]
    ahead_index: int | None
    ahead_status: Mapping[str, Any]
    behind_index: int | None
    behind_status: Mapping[str, Any]
    lap: int | None
    total_laps: int | None
    distance: float | None
    gap_ahead_s: float | None
    gap_behind_s: float | None
    in_pit: bool
    retired: bool
    remaining_laps: float | None
    drs_allowed: int | None
    drs_activation_distance: float | None
    ers_pct: float | None


class OvertakeStrategy:
    """추월 지점이 있는 서킷에서 공격·방어 계획을 고른다.

    한 세션 동안 하나를 만들어 계속 쓴다. 세션이 바뀌면 `session_uid` 변화를
    감지해 스스로 초기화하고, 랩이 바뀌면 공격·방어 횟수를 각각 새로 센다.
    """

    def __init__(
        self,
        track: Track,
        config: StrategyConfig | None = None,
        *,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._track = track
        self._config = config or StrategyConfig()
        self._clock = clock
        self._corners: dict[int, Corner] = {
            corner.number: corner for corner in track.corners
        }
        # (지점, 무전 창). 창은 (시작_m, 끝_m)이고 끝이 작으면 결승선을 넘는다.
        self._spots: tuple[tuple[OvertakingSpot, tuple[float, float]], ...] = tuple(
            (spot, self._window_for(spot)) for spot in track.overtaking_spots
        )
        self._session_uid: int | None = None
        self._lap: int | None = None
        self._attack_messages = 0
        self._defense_messages = 0
        self._last_plan: StrategyPlan | None = None
        # 차 인덱스 → (랩 트레이스, 코너 번호 → 탈출 속도). 최신 랩만 들고 있다.
        self._exit_cache: dict[int, tuple[LapTrace, dict[int, float]]] = {}

    # --- 조회 -------------------------------------------------------------

    @property
    def config(self) -> StrategyConfig:
        return self._config

    @property
    def active(self) -> bool:
        """이번 판단에서 공격·방어 계획이 나왔는가(진단용)."""
        return self._last_plan is not None

    @property
    def last_plan(self) -> StrategyPlan | None:
        return self._last_plan

    @property
    def attack_messages_this_lap(self) -> int:
        return self._attack_messages

    @property
    def defense_messages_this_lap(self) -> int:
        return self._defense_messages

    def reset(self) -> None:
        """세션 시작 등에서 상태를 비운다."""
        self._session_uid = None
        self._lap = None
        self._attack_messages = 0
        self._defense_messages = 0
        self._last_plan = None
        self._exit_cache.clear()

    # --- 판단 -------------------------------------------------------------

    def evaluate(
        self,
        snapshot: Mapping[str, Any],
        traces_by_car: TraceSource | None = None,
        now_ns: int | None = None,
    ) -> list[StrategyPlan]:
        """상태 한 순간을 반영하고, 지금 말할 공격·방어 계획을 돌려준다."""
        del now_ns  # 시각은 문장 생성기·큐가 쓴다. 규칙 판단은 상태만 본다.
        if not self._spots:
            return []
        view = _view(snapshot)
        if view.session_uid != self._session_uid:
            self.reset()
            self._session_uid = view.session_uid
        if view.lap is not None and view.lap != self._lap:
            self._lap = view.lap
            self._attack_messages = 0
            self._defense_messages = 0

        if not self._eligible(view):
            self._last_plan = None
            return []

        # 앞·뒤 모두 문턱 안이면 좁은 쪽을 먼저, 같으면 방어를 먼저 본다.
        sides: list[tuple[str, float]] = []
        if (
            view.gap_behind_s is not None
            and view.gap_behind_s <= self._config.defense_gap_s
            and self._defense_messages < self._config.defense_per_lap
        ):
            sides.append(("defense", view.gap_behind_s))
        if (
            view.gap_ahead_s is not None
            and view.gap_ahead_s <= self._config.attack_gap_s
            and self._attack_messages < self._config.attack_per_lap
        ):
            sides.append(("attack", view.gap_ahead_s))
        sides.sort(key=lambda item: (item[1], 0 if item[0] == "defense" else 1))

        for side, gap in sides:
            plan = (
                self._attack_plan(view, gap, traces_by_car)
                if side == "attack"
                else self._defense_plan(view, gap)
            )
            if plan is None:
                continue
            if side == "attack":
                self._attack_messages += 1
            else:
                self._defense_messages += 1
            self._last_plan = plan
            return [plan]
        self._last_plan = None
        return []

    def _eligible(self, view: _View) -> bool:
        if not is_race_session(view.session_type):
            return False
        if view.safety_car or view.in_pit or view.retired:
            return False
        if view.player_index is None or view.lap is None:
            return False
        if view.distance is None or not self._track.corners:
            return False
        return self._track.length_m > 0

    # --- 공격 -------------------------------------------------------------

    def _attack_plan(
        self,
        view: _View,
        gap: float,
        traces_by_car: TraceSource | None,
    ) -> StrategyPlan | None:
        assert view.distance is not None
        candidates: list[tuple[float, OvertakingSpot, tuple[float, float]]] = []
        for spot, window in self._spots:
            if not _in_window(view.distance, window):
                continue
            # 고위험이라 기본적으로 권하지 않는 지점은 압도적 우위일 때만 남긴다.
            if spot.avoid_reason is not None and not self._dominant(view, gap):
                continue
            score = self._attack_score(view, spot, gap, traces_by_car)
            candidates.append((score, spot, window))
        if not candidates:
            return None
        candidates.sort(key=lambda item: -item[0])
        score, spot, _window = candidates[0]
        distance = _distance_to_attack(view.distance, spot, self._corners, self._track)
        remaining = view.remaining_laps
        drs_ready = self._drs_ready(view)
        slow = self._ahead_slow_exit(view, traces_by_car, spot)
        my_age = _integer(view.player_status.get("tyres_age_laps"))
        ahead_age = _integer(view.ahead_status.get("tyres_age_laps"))
        advantage = (
            ahead_age - my_age
            if my_age is not None and ahead_age is not None
            else None
        )
        ahead_ers = _ers_percent(view.ahead_status.get("ers_store_energy"))

        facts: dict[str, Any] = {
            "side": "attack",
            "spot": spot.name,
            "difficulty": spot.difficulty,
            "attack_corners": list(spot.attack_corners),
            "prep_corners": list(spot.prep_corners),
            "attack_corners_text": _corner_text(
                spot.attack_corners, self._corners
            ),
            "prep_corners_text": _corner_text(spot.prep_corners, self._corners),
            "attack_method": spot.attack_method,
            "defense_line": spot.defense_line,
            "spot_drs": spot.drs,
            "method": "drs_late_brake" if spot.drs and drs_ready else "late_brake",
            "gap_s": round(gap, 3),
            "distance_to_attack_m": round(distance, 1),
            "lap": view.lap,
            "total_laps": view.total_laps,
            "remaining_laps": remaining,
            "session_type": view.session_type,
            "track_id": view.track_id,
            "drs_allowed": view.drs_allowed,
            "drs_activation_distance_m": (
                None
                if view.drs_activation_distance is None
                else round(view.drs_activation_distance, 1)
            ),
            "ers_store_pct": (
                None if view.ers_pct is None else round(view.ers_pct, 1)
            ),
            # 상대 ERS는 Car Status에 값이 보일 때만 참고한다.
            "ahead_ers_store_pct": (
                None if ahead_ers is None else round(ahead_ers, 1)
            ),
            "player_tyre_age_laps": my_age,
            "ahead_tyre_age_laps": ahead_age,
            "tyre_advantage_laps": advantage,
        }
        if slow is not None:
            facts.update(
                {
                    "ahead_slow_corner": slow.corner,
                    "player_exit_speed_kmh": round(slow.player_kmh, 1),
                    "ahead_exit_speed_kmh": round(slow.ahead_kmh, 1),
                    "exit_deficit_kmh": round(slow.deficit_kmh, 1),
                }
            )
        return StrategyPlan(
            side="attack",
            spot=spot,
            score=score,
            gap_s=gap,
            lap=view.lap,
            remaining_laps=remaining,
            facts=facts,
            template_key=(
                "strategy.attack_drs"
                if spot.drs and drs_ready
                else "strategy.attack"
            ),
            ttl_s=ATTACK_TTL_S,
            corner=spot.attack_corners[0],
        )

    def _attack_score(
        self,
        view: _View,
        spot: OvertakingSpot,
        gap: float,
        traces_by_car: TraceSource | None,
    ) -> float:
        closeness = _closeness(gap, self._config.attack_gap_s)
        score = 0.5 + 0.5 * closeness
        score += DIFFICULTY_ATTACK_FACTORS.get(spot.difficulty, 0.0)

        # DRS가 이 지점과 직접 이어지고 지금 쓸 수 있으면 강하게 올린다.
        if spot.drs and self._drs_ready(view):
            score += 0.25

        # 내 ERS가 충분하고, 상대 ERS가 보이는데 바닥이면 조금 더 밀어붙인다.
        if view.ers_pct is not None and view.ers_pct >= self._config.ers_min_pct:
            score += 0.10
        ahead_ers = _ers_percent(view.ahead_status.get("ers_store_energy"))
        if ahead_ers is not None and ahead_ers < 20.0:
            score += 0.08

        my_age = _integer(view.player_status.get("tyres_age_laps"))
        ahead_age = _integer(view.ahead_status.get("tyres_age_laps"))
        if my_age is not None and ahead_age is not None:
            advantage = ahead_age - my_age
            if advantage >= self._config.tyre_advantage_laps:
                score += min(0.25, advantage / 20.0)

        # 마지막 랩들에서는 기회를 놓치지 않도록 조금 더 공격적으로 본다.
        if is_last_laps(view.remaining_laps):
            score += 0.10

        slow = self._ahead_slow_exit(view, traces_by_car, spot)
        if slow is not None:
            score += 0.15 + min(0.20, slow.deficit_kmh / 20.0)

        assert view.distance is not None
        distance = _distance_to_attack(view.distance, spot, self._corners, self._track)
        score += 0.10 * max(0.0, 1.0 - distance / 1000.0)

        if spot.avoid_reason is not None:
            score += AVOID_PENALTY
        return score

    def _dominant(self, view: _View, gap: float) -> bool:
        """고위험 지점도 공격할 만큼 압도적인 우위인가."""
        if gap > DOMINANT_GAP_S:
            return False
        my_age = _integer(view.player_status.get("tyres_age_laps"))
        ahead_age = _integer(view.ahead_status.get("tyres_age_laps"))
        if my_age is None or ahead_age is None:
            return False
        return ahead_age - my_age >= DOMINANT_TYRE_ADVANTAGE_LAPS

    def _drs_ready(self, view: _View) -> bool:
        if view.drs_allowed != 1:
            return False
        distance = view.drs_activation_distance
        return distance is None or distance <= self._config.drs_ready_m

    # --- 방어 -------------------------------------------------------------

    def _defense_plan(self, view: _View, gap: float) -> StrategyPlan | None:
        assert view.distance is not None
        candidates: list[tuple[float, OvertakingSpot, tuple[float, float]]] = []
        for spot, window in self._spots:
            if not _in_window(view.distance, window):
                continue
            closeness = _closeness(gap, self._config.defense_gap_s)
            score = 0.5 + 0.5 * closeness
            # 방어는 난이도가 낮은 지점(정상적인 추월 지점)에서 하는 편이 쉽다.
            score += DIFFICULTY_ATTACK_FACTORS.get(spot.difficulty, 0.0)
            candidates.append((score, spot, window))
        if not candidates:
            return None
        candidates.sort(key=lambda item: -item[0])
        score, spot, _window = candidates[0]
        distance = _distance_to_attack(view.distance, spot, self._corners, self._track)

        facts: dict[str, Any] = {
            "side": "defense",
            "spot": spot.name,
            "difficulty": spot.difficulty,
            "attack_corners": list(spot.attack_corners),
            "prep_corners": list(spot.prep_corners),
            "attack_corners_text": _corner_text(
                spot.attack_corners, self._corners
            ),
            "prep_corners_text": _corner_text(spot.prep_corners, self._corners),
            "attack_method": spot.attack_method,
            "defense_line": spot.defense_line,
            "spot_drs": spot.drs,
            "gap_s": round(gap, 3),
            "distance_to_attack_m": round(distance, 1),
            "lap": view.lap,
            "total_laps": view.total_laps,
            "remaining_laps": view.remaining_laps,
            "session_type": view.session_type,
            "track_id": view.track_id,
        }
        return StrategyPlan(
            side="defense",
            spot=spot,
            score=score,
            gap_s=gap,
            lap=view.lap,
            remaining_laps=view.remaining_laps,
            facts=facts,
            template_key="strategy.defense",
            ttl_s=DEFENSE_TTL_S,
            corner=spot.attack_corners[0],
        )

    # --- 창·코너 ----------------------------------------------------------

    def _window_for(self, spot: OvertakingSpot) -> tuple[float, float]:
        """지점의 무전 창. 데이터의 `entry_window_m`을 우선한다."""
        if spot.entry_window_m is not None:
            return spot.entry_window_m
        attack_corners = [
            self._corners[number]
            for number in spot.attack_corners
            if number in self._corners
        ]
        prep_corners = [
            self._corners[number]
            for number in spot.prep_corners
            if number in self._corners
        ]
        if not attack_corners:
            return (0.0, self._track.length_m)
        attack_entry = min(corner.entry_m for corner in attack_corners)
        if prep_corners:
            # 공격 코너 진입에 가장 가까운(랩 순서상 마지막) 준비 코너의 탈출.
            prep = min(
                prep_corners,
                key=lambda corner: _forward_distance(
                    corner.exit_m, attack_entry, self._track.length_m
                ),
            )
            start = prep.exit_m
        else:
            start = (
                attack_entry - self._config.default_window_m
            ) % self._track.length_m
        return (start, attack_entry)

    # --- 근거 계산 --------------------------------------------------------

    def _ahead_slow_exit(
        self,
        view: _View,
        traces_by_car: TraceSource | None,
        spot: OvertakingSpot,
    ) -> _SlowExit | None:
        """앞차가 공격·준비 코너에서 나보다 느리게 탈출하는가."""
        if view.ahead_index is None:
            return None
        player_trace = _latest_valid(_traces_for(traces_by_car, view.player_index))
        ahead_trace = _latest_valid(_traces_for(traces_by_car, view.ahead_index))
        if player_trace is None or ahead_trace is None:
            return None

        best: _SlowExit | None = None
        for number in (*spot.attack_corners, *spot.prep_corners):
            corner = self._corners.get(number)
            if corner is None:
                continue
            player_exit = self._exit_speed(player_trace, corner)
            ahead_exit = self._exit_speed(ahead_trace, corner)
            deficit = player_exit - ahead_exit
            if deficit < self._config.slow_exit_deficit_kmh:
                continue
            if best is None or deficit > best.deficit_kmh:
                best = _SlowExit(
                    corner=corner.number,
                    deficit_kmh=deficit,
                    player_kmh=player_exit,
                    ahead_kmh=ahead_exit,
                )
        return best

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
    behind_index = _find_behind(entries, player_index, player_entry)
    behind_entry_map: Mapping[str, Any] = (
        entries[behind_index] if behind_index is not None else {}
    )
    behind_status: Mapping[str, Any] = (
        statuses[behind_index]
        if behind_index is not None and behind_index < len(statuses)
        else {}
    )

    session_type = _integer(session.get("session_type"))
    lap = _integer(player_entry.get("current_lap_num"))
    total_laps = _integer(session.get("total_laps"))
    driver_status = _integer(player_entry.get("driver_status"))
    pit_status = _integer(player_entry.get("pit_status"))
    result_status = _integer(player_entry.get("result_status"))

    return _View(
        session_uid=_integer(source.get("session_uid")),
        session_type=session_type,
        track_id=_integer(session.get("track_id")),
        safety_car=_integer(session.get("safety_car_status")) in (1, 2),
        player_index=player_index,
        entries=entries,
        player_entry=player_entry,
        player_status=player_status,
        ahead_index=ahead_index,
        ahead_status=ahead_status,
        behind_index=behind_index,
        behind_status=behind_status,
        lap=lap,
        total_laps=total_laps,
        distance=_number(player_entry.get("lap_distance")),
        gap_ahead_s=_gap_seconds(player_entry) if ahead_index is not None else None,
        gap_behind_s=(
            _gap_seconds(behind_entry_map) if behind_index is not None else None
        ),
        in_pit=(
            driver_status in {DRIVER_IN_LAP, DRIVER_OUT_LAP, DRIVER_IN_PIT}
            or pit_status in PIT_LANE_STATUSES
        ),
        retired=result_status is not None and result_status != 2,
        remaining_laps=remaining_race_laps(session_type, total_laps, lap),
        drs_allowed=_integer(player_status.get("drs_allowed")),
        drs_activation_distance=_number(
            player_status.get("drs_activation_distance")
        ),
        ers_pct=_ers_percent(player_status.get("ers_store_energy")),
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
            if index == player_index or not _on_track(entry):
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
        if index == player_index or not _on_track(entry):
            continue
        total = _number(entry.get("total_distance"))
        if total is None or total <= player_total:
            continue
        gap = total - player_total
        if gap < best_gap:
            best = index
            best_gap = gap
    return best


def _find_behind(
    entries: tuple[Mapping[str, Any], ...],
    player_index: int | None,
    player_entry: Mapping[str, Any],
) -> int | None:
    """플레이어 바로 뒤 차의 인덱스. 순위 값이 없으면 주행 거리로 찾는다."""
    if player_index is None:
        return None
    position = _integer(player_entry.get("car_position"))
    if position is not None:
        for index, entry in enumerate(entries):
            if index == player_index or not _on_track(entry):
                continue
            if _integer(entry.get("car_position")) == position + 1:
                return index

    player_total = _number(player_entry.get("total_distance"))
    if player_total is None:
        return None
    best: int | None = None
    best_gap = math.inf
    for index, entry in enumerate(entries):
        if index == player_index or not _on_track(entry):
            continue
        total = _number(entry.get("total_distance"))
        if total is None or total >= player_total:
            continue
        gap = player_total - total
        if gap < best_gap:
            best = index
            best_gap = gap
    return best


def _on_track(entry: Mapping[str, Any]) -> bool:
    """그 차가 실제 주행 중인가(리타이어·피트·차고 제외).

    값이 없는 옛 입력·테스트는 주행 중으로 본다.
    """
    result_status = _integer(entry.get("result_status"))
    if result_status is not None and result_status != 2:
        return False
    driver_status = _integer(entry.get("driver_status"))
    if driver_status in {0, DRIVER_IN_LAP, DRIVER_OUT_LAP, DRIVER_IN_PIT}:
        return False
    return _integer(entry.get("pit_status")) not in PIT_LANE_STATUSES


# ---------------------------------------------------------------------------
# 거리·값 도우미


def _in_window(distance: float, window: tuple[float, float]) -> bool:
    """랩 거리가 무전 창 안인가. 끝이 시작보다 작으면 결승선을 넘는 창이다."""
    start, end = window
    if start <= end:
        return start <= distance <= end
    return distance >= start or distance <= end


def _forward_distance(start_m: float, end_m: float, length_m: float) -> float:
    """`start_m`에서 랩 방향으로 `end_m`까지의 거리. 같은 점이면 한 랩."""
    if length_m <= 0:
        return 0.0
    distance = (end_m - start_m) % length_m
    return distance if distance > 0 else length_m


def _distance_to_attack(
    distance: float,
    spot: OvertakingSpot,
    corners: Mapping[int, Corner],
    track: Track,
) -> float:
    """지금 위치에서 공격 코너 진입까지 남은 랩 거리(m)."""
    entries = [
        corners[number].entry_m
        for number in spot.attack_corners
        if number in corners
    ]
    if not entries:
        return 0.0
    if track.length_m <= 0:
        return 0.0
    attack_entry = min(entries)
    return (attack_entry - distance) % track.length_m


def _closeness(gap: float, threshold: float) -> float:
    if threshold <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - gap / threshold))


def _corner_text(numbers: Sequence[int], corners: Mapping[int, Corner]) -> str:
    """코너 번호 묶음을 무전 슬롯용 문자열로 만든다("16번, 17번").

    데이터 파일이 없는 서킷의 자동 탐지 코너면 "16번째 코너"로 말한다.
    구분자는 LLM 출력 검증(`radio/llm.py`)이 허용하는 쉼표를 쓴다.
    """
    labels: list[str] = []
    for number in numbers:
        corner = corners.get(number)
        official = True if corner is None else corner.official
        labels.append(corner_label(number, official=official))
    return ", ".join(labels)


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


def _gap_seconds(entry: Mapping[str, Any]) -> float | None:
    """Lap Data의 앞차와의 간격(초). 분 파트 + ms 파트."""
    minutes = _integer(entry.get("delta_to_car_in_front_minutes_part"))
    milliseconds = _integer(entry.get("delta_to_car_in_front_ms_part"))
    if minutes is None and milliseconds is None:
        return None
    return max(0, minutes or 0) * 60.0 + max(0, milliseconds or 0) / 1000.0


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
    return max(valid, key=lambda trace: (trace.lap, len(trace.samples)))


__all__ = [
    "ATTACK_TTL_S",
    "DEFAULT_ENTRY_WINDOW_M",
    "DEFENSE_TTL_S",
    "DIFFICULTY_ATTACK_FACTORS",
    "OvertakeStrategy",
    "StrategyConfig",
    "StrategyPlan",
]
