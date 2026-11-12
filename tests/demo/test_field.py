"""그랑프리 레이스 필드 테스트: 차 20대·순위·간격·시나리오 주입."""

from __future__ import annotations

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
