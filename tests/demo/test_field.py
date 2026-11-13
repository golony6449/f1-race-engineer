"""그랑프리 레이스 필드 테스트: 차 20대·순위·간격·시나리오 주입."""

from __future__ import annotations

import random

import pytest

from f1re.demo_sender.driving import DriverProfile, build_speed_profile
from f1re.demo_sender.field import (
    PLAYER_CAR_INDEX,
    CarState,
    RaceField,
    car_ahead,
)
from f1re.tracks.model import load_outline, load_track

SUZUKA_ID = 13


def _field(seed: int = 0) -> RaceField:
    track = load_track(SUZUKA_ID)
    outline = load_outline(SUZUKA_ID)
    assert track is not None and outline is not None
    return RaceField(track, outline, seed=seed)


def _states(
    field: RaceField,
    *,
    race_time_s: float = 40.0,
    player_lap: int = 1,
    player_distance_m: float = 900.0,
    player_speed_kmh: float = 240.0,
) -> list[CarState]:
    return field.states_at(
        race_time_s,
        player_lap=player_lap,
        player_lap_distance_m=player_distance_m,
        player_speed_kmh=player_speed_kmh,
    )


def _by_index(states: list[CarState], car_index: int) -> CarState:
    return next(state for state in states if state.car_index == car_index)


def test_field_has_20_cars_and_positions_consistent() -> None:
    """차 20대의 순위가 진행 거리와 맞고, 앞차 간격이 거리·속도와 일치한다."""
    field = _field()
    states = _states(field)
    track = load_track(SUZUKA_ID)
    assert track is not None

    assert len(states) == 20
    assert sorted(state.car_index for state in states) == list(range(20))
    assert sorted(state.position for state in states) == list(range(1, 21))
    assert field.cars[PLAYER_CAR_INDEX].ai_controlled is False
    assert sum(1 for car in field.cars if car.ai_controlled) == 19

    player = _by_index(states, PLAYER_CAR_INDEX)
    assert player.total_distance_m == (1 - 1) * track.length_m + 900.0

    ranked = sorted(states, key=lambda state: state.position)
    assert ranked[0].delta_ahead_ms == 0
    assert ranked[0].delta_leader_ms == 0
    for index in range(1, len(ranked)):
        ahead = ranked[index - 1]
        me = ranked[index]
        assert ahead.total_distance_m > me.total_distance_m
        expected_ahead = (
            (ahead.total_distance_m - me.total_distance_m)
            / max(me.speed_kmh / 3.6, 1.0)
            * 1000.0
        )
        assert abs(me.delta_ahead_ms - expected_ahead) <= 2.0
        expected_leader = (
            (ranked[0].total_distance_m - me.total_distance_m)
            / max(me.speed_kmh / 3.6, 1.0)
            * 1000.0
        )
        assert abs(me.delta_leader_ms - expected_leader) <= 2.0

    # 내 차 바로 앞 차는 순위로 찾을 수 있다.
    ahead = car_ahead(states)
    if player.position > 1:
        assert ahead is not None and ahead.position == player.position - 1
    else:
        assert ahead is None


def test_field_skill_spread_and_seed_reproducible() -> None:
    """AI 차마다 랩 타임이 다르고, 같은 시드는 같은 필드를 만든다."""
    field = _field(seed=7)
    ai_laps = [car.lap_time_ms for car in field.cars if car.ai_controlled]
    player_lap = field.cars[PLAYER_CAR_INDEX].lap_time_ms
    assert len(set(ai_laps)) > 10
    assert min(ai_laps) < player_lap < max(ai_laps)

    same = _field(seed=7)
    other = _field(seed=8)
    assert [car.lap_time_ms for car in field.cars] == [
        car.lap_time_ms for car in same.cars
    ]
    assert [car.lap_time_ms for car in field.cars] != [
        car.lap_time_ms for car in other.cars
    ]


def test_gap_override_under_1s() -> None:
    """시나리오로 앞차와의 간격을 0.6초로 고정할 수 있다."""
    field = _field()
    field.set_gap_ahead(0.6, start_lap=2)
    states = _states(field, player_lap=2, player_distance_m=500.0, player_speed_kmh=230.0)
    player = _by_index(states, PLAYER_CAR_INDEX)
    assert 0 < player.delta_ahead_ms < 1000
    assert abs(player.delta_ahead_ms - 600) <= 5
    ahead = car_ahead(states)
    assert ahead is not None
    assert ahead.total_distance_m > player.total_distance_m

    # 순위·간격 일관성은 간격을 고정한 뒤에도 유지된다.
    ranked = sorted(states, key=lambda state: state.position)
    for index in range(1, len(ranked)):
        me = ranked[index]
        expected = (
            (ranked[index - 1].total_distance_m - me.total_distance_m)
            / max(me.speed_kmh / 3.6, 1.0)
            * 1000.0
        )
        assert abs(me.delta_ahead_ms - expected) <= 2.0


def test_ai_corner_speed_matches_profile_at_same_lap_distance() -> None:
    """AI 차 헤어핀 최저 속도가 같은 위치의 플레이어 프로필 최저 속도 × pace다.

    출발 그리드 오프셋을 속도 조회에 반영하지 않으면 AI 차가 헤어핀 위치에
    있으면서 직선 속도를 보고한다(가장 빠른 차 기준 코너 지표가 비현실적이 된다).
    """
    field = _field()
    track = load_track(SUZUKA_ID)
    outline = load_outline(SUZUKA_ID)
    assert track is not None and outline is not None
    # 필드의 AI 주행 곡선과 같은 기준 프로필이다.
    profile = build_speed_profile(
        track, outline, DriverProfile(consistency=1.0), random.Random(0)
    )
    corner = next(item for item in track.corners if item.number == 11)
    car = field.cars[1]
    assert car.ai_controlled
    profile_min_kmh = (
        min(
            speed
            for distance, speed in zip(profile.dists, profile.speeds)
            if corner.entry_m <= distance <= corner.exit_m
        )
        * 3.6
    )

    # AI 차가 헤어핀에 들어서는 대략 시각: 프로필에서 그 거리(출발 오프셋을
    # 뺀)에 도달하는 시간을 차의 pace로 나눈다. 두 번째 랩은 랩 위치가
    # 한 바퀴 감긴 뒤에도 같은 값을 내는지 확인한다.
    target_m = (corner.entry_m - car.start_offset_m) % track.length_m
    index = min(
        range(len(profile.dists)),
        key=lambda item: abs(profile.dists[item] - target_m),
    )
    enter_time_s = profile.times[index] / car.pace
    for lap_offset_s in (0.0, profile.lap_time_s / car.pace):
        slowest: CarState | None = None
        for step in range(600):
            state = _by_index(
                _states(
                    field,
                    race_time_s=enter_time_s + lap_offset_s - 1.0 + step * 0.01,
                    player_distance_m=0.0,
                ),
                car.car_index,
            )
            if (
                corner.entry_m <= state.lap_distance_m <= corner.exit_m
                and (slowest is None or state.speed_kmh < slowest.speed_kmh)
            ):
                slowest = state
        assert slowest is not None
        # 100Hz로 훑어 apex 근처 값을 잡는다. 곡선 보간 오차만 허용한다.
        assert slowest.speed_kmh == pytest.approx(
            profile_min_kmh * car.pace, abs=2.0
        )
        # 속도가 위치와 맞으면 헤어핀답게 낮은 기어·큰 조향이 나온다.
        assert slowest.gear <= 3
        assert abs(slowest.steer) > 0.2


def test_slow_exit_lowers_car_speed_at_corner() -> None:
    """앞차 탈출 느림 시나리오가 지정 코너에서 그 차의 속도를 낮춘다."""
    field = _field()
    track = load_track(SUZUKA_ID)
    assert track is not None
    corner = next(item for item in track.corners if item.number == 3)
    car_index = 5

    def speed_in_corner() -> tuple[float, float] | None:
        for step in range(0, 60 * 4):
            race_time_s = step * 0.25
            state = _by_index(
                _states(
                    field,
                    race_time_s=race_time_s,
                    player_lap=1,
                    player_distance_m=0.0,
                    player_speed_kmh=240.0,
                ),
                car_index,
            )
            if corner.apex_m <= state.lap_distance_m <= corner.exit_m:
                return race_time_s, state.speed_kmh
        return None

    baseline = speed_in_corner()
    assert baseline is not None
    race_time_s, baseline_speed = baseline

    field.set_slow_exit(car_index, corner.number, 0.65, start_lap=1)
    slowed = _by_index(
        _states(
            field,
            race_time_s=race_time_s,
            player_lap=1,
            player_distance_m=0.0,
            player_speed_kmh=240.0,
        ),
        car_index,
    )
    assert 0 < slowed.speed_kmh < baseline_speed
    assert slowed.brake >= 0.2
