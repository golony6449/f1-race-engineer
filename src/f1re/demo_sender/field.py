"""그랑프리 레이스 필드: AI 19대 + 내 차.

타임 트라이얼 데모는 내 차 한 대만 보낸다. 그랑프리 모드는 같은 서킷을 도는
AI 차 19대를 더해 Lap Data의 순위·간격, Car Telemetry·Car Status·Motion의
차 배열을 채운다. AI 차는 기준 속도 곡선(``build_speed_profile``)을 실력
배율만큼 빠르거나 느리게 도는 모델이다. 물리 시뮬레이터가 아니라 순위와
간격이 서로 맞는 그럴듯한 값을 만드는 것이 목적이다.

플레이어(차 0)의 주행은 송신기가 만든 샘플을 그대로 쓰고, 이 모듈은 그
샘플과 AI 차들의 진행 거리를 합쳐 순위와 앞차·선두와의 간격을 계산한다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from f1re.demo_sender.driving import (
    GRAVITY,
    STEER_GAIN,
    DriverProfile,
    SpeedProfile,
    build_speed_profile,
    gear_and_rpm,
    pedals_for_acceleration,
    profile_state,
    profile_state_at_distance,
    profile_time_at_distance,
)
from f1re.tracks.model import Track
from f1re.tracks.outline import TrackOutline

PLAYER_CAR_INDEX = 0
GRID_SIZE = 20  # 게임 최대 22대 중 그랑프리는 20대(내 차 + AI 19대)
DEFAULT_CAR_COUNT = GRID_SIZE

# 출발 그리드 간격(m). 내 차는 맨 뒤에서 출발하고, 앞차일수록 더 많이 나가 있다.
GRID_SPACING_M = 25.0
# AI 실력 분포(기준 랩 타임 대비). +는 빠름. 편차를 크게 두어 추월이 생기게 한다.
PACE_SPREAD = 0.045
PACE_JITTER = 0.006

TYRE_COMPOUNDS: tuple[int, ...] = (16, 17, 18)  # C5, C4, C3
MAX_TYRE_AGE_LAPS = 255
MAX_DELTA_MS = 0xFFFF + 59 * 60_000  # Lap Data의 분·ms 파트가 표현할 수 있는 범위

# 시나리오 "앞차 탈출 느림"을 적용할 구간은 코너 apex부터 exit 뒤 이만큼까지다.
SLOW_EXIT_EXTRA_M = 30.0


@dataclass(frozen=True)
class CarProfile:
    """필드의 차 한 대. `pace`가 1보다 크면 기준 곡선보다 빠르다."""

    car_index: int
    name: str
    team_id: int
    ai_controlled: bool
    compound: int
    tyre_age_base: int
    pace: float
    start_offset_m: float
    lap_time_ms: int


@dataclass(frozen=True)
class CarState:
    """한 순간의 차 한 대 상태. 각도는 라디안, 시간은 ms다."""

    car_index: int
    position: int
    lap: int
    lap_distance_m: float
    total_distance_m: float
    speed_kmh: float
    delta_ahead_ms: int
    delta_leader_ms: int
    throttle: float
    brake: float
    steer: float
    gear: int
    rpm: int
    x: float
    z: float
    yaw: float
    lateral_g: float
    longitudinal_g: float
    last_lap_ms: int
    current_lap_ms: int
    sector: int
    sector1_ms: int
    sector2_ms: int
    tyre_age_laps: int
    compound: int


@dataclass(frozen=True)
class _SlowExit:
    """시나리오로 지정한 "앞차 탈출 느림" 구간."""

    car_index: int
    corner_number: int
    factor: float
    start_lap: int
    end_lap: int | None


class RaceField:
    """AI 차 19대와 내 차의 진행을 계산한다."""

    def __init__(
        self,
        track: Track,
        outline: TrackOutline,
        *,
        seed: int = 0,
        car_count: int = DEFAULT_CAR_COUNT,
    ) -> None:
        if not 2 <= car_count <= GRID_SIZE:
            raise ValueError(f"car_count는 2~{GRID_SIZE}여야 합니다")
        self._track = track
        self._outline = outline
        self._car_count = car_count
        self._rng = random.Random(seed ^ 0x5EED)
        self._profile: SpeedProfile = build_speed_profile(
            track, outline, DriverProfile(consistency=1.0), random.Random(0)
        )
        self._base_lap_s = self._profile.lap_time_s
        self._cars = self._build_cars(car_count)
        self._gap_override: tuple[float, int, int | None] | None = None
        self._slow_exit: _SlowExit | None = None

    # ------------------------------------------------------------------
    # 구성

    def _build_cars(self, car_count: int) -> list[CarProfile]:
        cars: list[CarProfile] = [
            CarProfile(
                car_index=PLAYER_CAR_INDEX,
                name="YOU",
                team_id=0,
                ai_controlled=False,
                compound=TYRE_COMPOUNDS[0],
                tyre_age_base=0,
                pace=1.0,
                start_offset_m=0.0,
                lap_time_ms=int(round(self._base_lap_s * 1000.0)),
            )
        ]
        ai_count = car_count - 1
        for offset in range(ai_count):
            car_index = offset + 1
            # offset=0이 가장 빠르고 마지막이 가장 느리다. 시드로 약간 흔든다.
            spread = -1.0 + 2.0 * offset / max(1, ai_count - 1)
            pace = 1.0 - PACE_SPREAD * spread + self._rng.uniform(-PACE_JITTER, PACE_JITTER)
            pace = max(0.85, min(1.10, pace))
            lap_time_ms = int(round(self._base_lap_s * 1000.0 / pace))
            cars.append(
                CarProfile(
                    car_index=car_index,
                    name=f"AI {car_index:02d}",
                    team_id=(car_index - 1) % 10,
                    ai_controlled=True,
                    compound=TYRE_COMPOUNDS[offset % len(TYRE_COMPOUNDS)],
                    tyre_age_base=(offset * 3) % 12,
                    pace=pace,
                    start_offset_m=(ai_count - offset) * GRID_SPACING_M,
                    lap_time_ms=lap_time_ms,
                )
            )
        return cars

    @property
    def cars(self) -> list[CarProfile]:
        return list(self._cars)

    @property
    def player(self) -> CarProfile:
        return self._cars[PLAYER_CAR_INDEX]

    # ------------------------------------------------------------------
    # 시나리오 주입

    def set_gap_ahead(
        self,
        seconds: float,
        *,
        start_lap: int = 1,
        end_lap: int | None = None,
    ) -> None:
        """앞차와의 간격을 `seconds`로 고정한다(추월 시험용)."""
        if seconds < 0:
            raise ValueError("간격은 0 이상이어야 합니다")
        self._gap_override = (seconds, start_lap, end_lap)

    def set_slow_exit(
        self,
        car_index: int,
        corner_number: int,
        factor: float = 0.7,
        *,
        start_lap: int = 1,
        end_lap: int | None = None,
    ) -> None:
        """`car_index`가 지정 코너 탈출에서 느려지게 한다(추월 시험용)."""
        if not 0.0 < factor <= 1.0:
            raise ValueError("속도 배율은 0보다 크고 1 이하여야 합니다")
        if not 0 <= car_index < len(self._cars):
            raise ValueError(f"차 인덱스가 범위를 벗어났습니다: {car_index}")
        self._slow_exit = _SlowExit(car_index, corner_number, factor, start_lap, end_lap)

    # ------------------------------------------------------------------
    # 상태 계산

    def states_at(
        self,
        race_time_s: float,
        *,
        player_lap: int,
        player_lap_distance_m: float,
        player_speed_kmh: float,
    ) -> list[CarState]:
        """레이스 시각의 차 20대 상태. 순위 순(1위부터)이 아니라 차 인덱스 순이다."""
        if race_time_s < 0:
            raise ValueError("레이스 시각은 0 이상이어야 합니다")
        length = self._track.length_m
        player_total = (player_lap - 1) * length + player_lap_distance_m
        states = [
            self._player_state(
                player_lap, player_lap_distance_m, player_total, player_speed_kmh
            )
        ]
        for car in self._cars[1:]:
            states.append(self._ai_state(car, race_time_s, player_lap))

        if self._gap_override is not None:
            seconds, start_lap, end_lap = self._gap_override
            if player_lap >= start_lap and (end_lap is None or player_lap <= end_lap):
                states = self._apply_gap_override(states, seconds, player_speed_kmh)

        ranked = sorted(
            states, key=lambda state: (-state.total_distance_m, state.car_index)
        )
        positions = {state.car_index: index + 1 for index, state in enumerate(ranked)}
        leader_total = ranked[0].total_distance_m
        result: list[CarState] = []
        previous: CarState | None = None
        for state in ranked:
            speed_mps = max(state.speed_kmh / 3.6, 1.0)
            if previous is None:
                delta_ahead = 0
            else:
                delta_ahead = min(
                    MAX_DELTA_MS,
                    int(round((previous.total_distance_m - state.total_distance_m) / speed_mps * 1000.0)),
                )
            delta_leader = min(
                MAX_DELTA_MS,
                int(round((leader_total - state.total_distance_m) / speed_mps * 1000.0)),
            )
            result.append(
                replace(
                    state,
                    position=positions[state.car_index],
                    delta_ahead_ms=delta_ahead,
                    delta_leader_ms=delta_leader,
                )
            )
            previous = state
        return result

    def _player_state(
        self,
        player_lap: int,
        lap_distance_m: float,
        total_distance_m: float,
        speed_kmh: float,
    ) -> CarState:
        x, z = self._outline.position_at(lap_distance_m)
        yaw = self._outline.heading_at(lap_distance_m)
        return CarState(
            car_index=PLAYER_CAR_INDEX,
            position=1,
            lap=player_lap,
            lap_distance_m=lap_distance_m,
            total_distance_m=total_distance_m,
            speed_kmh=speed_kmh,
            delta_ahead_ms=0,
            delta_leader_ms=0,
            throttle=0.0,
            brake=0.0,
            steer=0.0,
            gear=1,
            rpm=0,
            x=x,
            z=z,
            yaw=yaw,
            lateral_g=0.0,
            longitudinal_g=0.0,
            last_lap_ms=0,
            current_lap_ms=0,
            sector=0,
            sector1_ms=0,
            sector2_ms=0,
            tyre_age_laps=0,
            compound=self.player.compound,
        )

    def _ai_state(self, car: CarProfile, race_time_s: float, player_lap: int) -> CarState:
        length = self._track.length_m
        progress_s = race_time_s * car.pace
        completed = int(progress_s // self._base_lap_s)
        remainder_s = progress_s - completed * self._base_lap_s
        distance_in_lap = profile_state(self._profile, remainder_s)[0]
        total = car.start_offset_m + completed * length + distance_in_lap
        lap = 1 + int(total // length)
        lap_distance = total - (lap - 1) * length
        # 속도·페달·기어·조향은 차가 실제로 있는 랩 위치의 프로필 값이어야 한다.
        # 진행 거리는 그리드 출발 오프셋만큼 시간 기준 위치와 다르므로, 시간
        # 기준으로 조회한 거리 대신 차의 랩 위치(lap_distance)로 프로필 값을 얻는다.
        _, speed_mps, curvature, acceleration = profile_state_at_distance(
            self._profile, lap_distance
        )
        speed_kmh = speed_mps * car.pace * 3.6
        gear, rpm = gear_and_rpm(speed_kmh)
        throttle, brake = pedals_for_acceleration(acceleration * car.pace)

        slow = self._slow_exit
        if (
            slow is not None
            and slow.car_index == car.car_index
            and player_lap >= slow.start_lap
            and (slow.end_lap is None or player_lap <= slow.end_lap)
        ):
            for corner in self._track.corners:
                if corner.number != slow.corner_number:
                    continue
                if corner.apex_m <= lap_distance <= corner.exit_m + SLOW_EXIT_EXTRA_M:
                    speed_kmh *= slow.factor
                    throttle = min(throttle, 0.1)
                    brake = max(brake, 0.2)
                break

        x, z = self._outline.position_at(lap_distance)
        yaw = self._outline.heading_at(lap_distance)
        current_lap_ms = self._current_lap_ms(car, lap_distance)
        sector1, sector2 = self._sector_splits()
        sector = 0 if lap_distance < sector1 else 1 if lap_distance < sector2 else 2
        third_ms = car.lap_time_ms // 3
        return CarState(
            car_index=car.car_index,
            position=1,
            lap=lap,
            lap_distance_m=lap_distance,
            total_distance_m=total,
            speed_kmh=speed_kmh,
            delta_ahead_ms=0,
            delta_leader_ms=0,
            throttle=throttle,
            brake=brake,
            steer=max(-1.0, min(1.0, -curvature * STEER_GAIN)),
            gear=gear,
            rpm=rpm,
            x=x,
            z=z,
            yaw=yaw,
            lateral_g=speed_mps**2 * curvature / GRAVITY,
            longitudinal_g=acceleration * car.pace / GRAVITY,
            last_lap_ms=car.lap_time_ms,
            current_lap_ms=current_lap_ms,
            sector=sector,
            sector1_ms=third_ms if sector >= 1 else 0,
            sector2_ms=third_ms if sector >= 2 else 0,
            tyre_age_laps=min(MAX_TYRE_AGE_LAPS, car.tyre_age_base + lap - 1),
            compound=car.compound,
        )

    def _current_lap_ms(self, car: CarProfile, lap_distance_m: float) -> int:
        """그 차가 랩 위치까지 오는 데 걸린 시간(ms).

        거리에 비례하는 등속 가정 대신, 같은 위치의 프로필 시각을 그 차의
        pace로 나눈다. 느린 코너에서 실제보다 시간이 적게 흐르는 문제를 막는다.
        """
        return int(
            round(profile_time_at_distance(self._profile, lap_distance_m) / car.pace * 1000.0)
        )

    def _sector_splits(self) -> tuple[float, float]:
        return (self._track.length_m / 3.0, self._track.length_m * 2.0 / 3.0)

    def _apply_gap_override(
        self,
        states: list[CarState],
        seconds: float,
        player_speed_kmh: float,
    ) -> list[CarState]:
        """가장 가까운 앞차(없으면 가장 가까운 뒤차)를 정확히 `seconds` 앞에 둔다."""
        length = self._track.length_m
        player = next(state for state in states if state.car_index == PLAYER_CAR_INDEX)
        others = [state for state in states if state.car_index != PLAYER_CAR_INDEX]
        ahead = [state for state in others if state.total_distance_m > player.total_distance_m]
        if ahead:
            target = min(ahead, key=lambda state: state.total_distance_m)
        else:
            behind = [state for state in others if state.total_distance_m <= player.total_distance_m]
            if not behind:
                return states
            target = max(behind, key=lambda state: state.total_distance_m)
        speed_mps = max(player_speed_kmh / 3.6, 1.0)
        total = player.total_distance_m + seconds * speed_mps
        lap = 1 + int(total // length)
        lap_distance = total - (lap - 1) * length
        adjusted = replace(
            target,
            lap=lap,
            lap_distance_m=lap_distance,
            total_distance_m=total,
            current_lap_ms=self._current_lap_ms(self._cars[target.car_index], lap_distance),
        )
        return [adjusted if state.car_index == target.car_index else state for state in states]


def car_ahead(states: list[CarState]) -> CarState | None:
    """플레이어 바로 앞 차. 플레이어가 선두면 `None`."""
    player = next((state for state in states if state.car_index == PLAYER_CAR_INDEX), None)
    if player is None or player.position <= 1:
        return None
    for state in states:
        if state.position == player.position - 1:
            return state
    return None
