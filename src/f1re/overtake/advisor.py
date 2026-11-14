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
from f1re.coaching.reference import NON_AI_SESSION_TYPES
from f1re.config import AppConfig
from f1re.laps.traces import LapTrace
from f1re.radio.queue import RadioKind, RadioMessage, RadioQueue
from f1re.radio.templates import render, render_corner
from f1re.tracks.model import Corner, Track, load_track

# F1 25 ERS 저장소 최대 에너지(J). `web/app.py`와 같은 공식 규정값이다.
ERS_STORE_MAX_J = 4_000_000.0

# Car Status의 ERS 배포 모드(공식 열거형): 3=Overtake.
ERS_DEPLOY_OVERTAKE = 3

# 선택지별 유효 시간(초). 설계 6.7의 "무전마다 유효 시간"이다.
#   - DRS: 다음 직선까지. 데모 랩(85~100초)에서 직선 하나를 넉넉히 덮는다.
#   - 느린 탈출: 이름을 부른 코너를 지나칠 때까지.
#   - 타이어 기다림: 두 랩을 기다리라는 선택이므로 한 랩 남짓.
DRS_TTL_S = 15.0
SLOW_EXIT_TTL_S = 30.0
TYRE_WAIT_TTL_S = 120.0

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
    """판단한 선택지 하나(점수 순 정렬 전)."""

    key: str
    score: float
    text: str
    ttl_s: float
    evidence: dict[str, Any]
    corner: int | None = None


@dataclass(frozen=True)
class _View:
    """스냅샷에서 추월 판단에 필요한 값만 꺼낸 것."""

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
    lap: int | None
    distance: float
    gap_s: float | None


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
            return []

        # 랩이 바뀌면 그 랩에 말할 수 있는 추월 무전 횟수를 새로 센다.
        if view.lap != self._lap:
            self._lap = view.lap
            self._messages_this_lap = 0

        if view.session_type in NON_AI_SESSION_TYPES or view.safety_car:
            self._ahead_index = view.ahead_index
            self._active = False
            return []

        if self._overtook(view):
            # 앞차를 추월했다. 이번 판단은 쉬고, 다음에 새 앞차로 다시 켠다.
            self._ahead_index = view.ahead_index
            self._active = False
            return []

        gap = view.gap_s if view.ahead_index is not None else None
        self._ahead_index = view.ahead_index
        self._observe_gap(gap, now)

        if gap is None or gap > self._config.gap_s:
            self._active = False
            return []

        self._active = True
        if self._messages_this_lap >= self._config.per_lap:
            return []

        options = self._options(view, traces_by_car)
        if not options:
            return []
        limit = min(
            self._config.max_options,
            self._config.per_lap - self._messages_this_lap,
        )
        messages: list[RadioMessage] = []
        for option in options[:limit]:
            message = RadioMessage.create(
                RadioKind.OVERTAKE.value,
                option.text,
                now_ns=now,
                ttl_ns=int(option.ttl_s * 1_000_000_000),
                evidence=option.evidence,
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

    def _overtook(self, view: _View) -> bool:
        """직전에 보던 앞차가 이번 상태에서 내 뒤로 갔는가."""
        previous = self._ahead_index
        current = view.ahead_index
        if previous is None or current is None or previous == current:
            return False
        return self._is_behind(previous, view)

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
            text=render(key, gap=_gap_text(gap)),
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
            text=render_corner(
                "overtake.slow_exit",
                corner.number,
                official=corner.official,
                gap=_gap_text(gap),
            ),
            ttl_s=SLOW_EXIT_TTL_S,
            evidence=evidence,
            corner=corner.number,
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
            text=render(
                "overtake.tyre_wait",
                gap=_gap_text(gap),
                laps=advantage,
                wait_laps=self._config.tyre_wait_laps,
            ),
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

    return _View(
        session_uid=_integer(source.get("session_uid")),
        session_type=_integer(session.get("session_type")),
        track_id=_integer(session.get("track_id")),
        safety_car=_integer(session.get("safety_car_status")) in (1, 2),
        player_index=player_index,
        entries=entries,
        player_entry=player_entry,
        player_status=player_status,
        ahead_index=ahead_index,
        ahead_status=ahead_status,
        lap=_integer(player_entry.get("current_lap_num")),
        distance=_number(player_entry.get("lap_distance")) or 0.0,
        gap_s=_gap_seconds(player_entry) if ahead_index is not None else None,
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
    "SLOW_EXIT_TTL_S",
    "TYRE_WAIT_TTL_S",
]
