"""기준 랩 선택과 코너 시간 손실 테스트(작업 15, SCRUM-65).

기준 랩 = 내 최고 유효 랩, AI 상대 세션이면 세션 최속차의 최고 랩도 후보로 두고
코너마다 더 빠른 쪽을 쓴다(설계 6.3). 무효 랩은 절대 기준이 되지 않는다
(리뷰 포커스 4).
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from f1re.coaching.metrics import corner_metrics
from f1re.coaching.reference import (
    SOURCE_FASTEST_CAR,
    SOURCE_SELF,
    ReferenceSet,
    corner_time_loss_ms,
    trace_outline,
)
from f1re.demo_sender.driving import DriverProfile, Sample as DrivingSample, generate_lap
from f1re.demo_sender.scenarios import (
    SCENARIO_DIR,
    corner_effects,
    events_for_lap,
    load_scenarios,
    parse_scenarios,
)
from f1re.laps.traces import LapTrace, Sample
from f1re.tracks.model import Corner, Track, load_outline, load_track
from f1re.tracks.outline import TrackOutline

SUZUKA_ID = 13

_TRACK = load_track(SUZUKA_ID)
_OUTLINE = load_outline(SUZUKA_ID)
assert _TRACK is not None and _OUTLINE is not None
TRACK: Track = _TRACK
OUTLINE: TrackOutline = _OUTLINE
CORNER_NUMBERS = [corner.number for corner in TRACK.corners]


# ---------------------------------------------------------------------------
# 도우미


def _corner(number: int) -> Corner:
    return next(corner for corner in TRACK.corners if corner.number == number)


def _to_sample(sample: DrivingSample) -> Sample:
    return Sample(
        dist_m=sample.dist_m,
        t_ms=sample.t_ms,
        speed=sample.speed_kmh,
        throttle=sample.throttle,
        brake=sample.brake,
        steer=sample.steer,
        gear=sample.gear,
        x=sample.x,
        z=sample.z,
    )


def _trace(
    lap: int,
    *,
    driver: DriverProfile | None = None,
    effects: dict | None = None,
    car_index: int = 0,
    seed: int = 0,
    valid: bool = True,
    invalid_reason: str | None = None,
) -> LapTrace:
    samples = generate_lap(
        TRACK,
        OUTLINE,
        driver or DriverProfile(consistency=1.0),
        lap,
        random.Random(seed),
        corner_effects=effects or {},
    )
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=valid,
        invalid_reason=invalid_reason,
        samples=[_to_sample(sample) for sample in samples],
    )


def _coaching_trace(lap: int, **kwargs) -> LapTrace:
    events = events_for_lap(load_scenarios(SCENARIO_DIR / "coaching.yaml"), lap)
    return _trace(lap, effects=corner_effects(events, CORNER_NUMBERS), **kwargs)


def _early_brake_effects(number: int, meters: float = 10.0) -> dict:
    events = parse_scenarios(
        [
            {
                "lap": 1,
                "corner": number,
                "kind": "early_brake",
                "params": {"meters": meters},
            }
        ]
    )
    return corner_effects(events, CORNER_NUMBERS)


def _samples_with_slip(trace: LapTrace) -> LapTrace:
    return LapTrace(
        car_index=trace.car_index,
        lap=trace.lap,
        valid=True,
        invalid_reason=None,
        samples=[
            dataclasses.replace(sample, wheel_slip=(0.1, 0.2, 0.3, 0.4))
            for sample in trace.samples
        ],
    )


def _refs() -> ReferenceSet:
    return ReferenceSet(TRACK.corners)


# ---------------------------------------------------------------------------
# 내 최고 랩


def test_best_for_self_uses_best_valid_lap() -> None:
    refs = _refs()
    fast = _trace(1, driver=DriverProfile(consistency=1.0))
    slow = _trace(2, driver=DriverProfile(pace=0.98, consistency=1.0))
    assert fast.samples[-1].t_ms < slow.samples[-1].t_ms
    refs.update(slow)
    refs.update(fast)

    best = refs.best_for(3)
    assert best is not None
    trace, source = best
    assert source == SOURCE_SELF
    assert trace.lap == 1
    assert trace.car_index == 0


def test_invalid_laps_never_become_reference() -> None:
    """아웃랩·무효 랩이 아무리 빨라도 기준이 되지 않는다(리뷰 포커스 4)."""
    refs = _refs()
    valid = _trace(2, driver=DriverProfile(pace=0.99, consistency=1.0))
    invalid = _trace(
        1,
        driver=DriverProfile(pace=1.1, consistency=1.0),
        valid=False,
        invalid_reason="outlap",
    )
    assert invalid.samples[-1].t_ms < valid.samples[-1].t_ms
    refs.update(invalid)
    refs.update(valid)

    best = refs.best_for(3)
    assert best is not None
    trace, source = best
    assert source == SOURCE_SELF
    assert trace.lap == 2


def test_only_invalid_laps_leave_no_reference() -> None:
    refs = _refs()
    refs.update(
        _trace(1, valid=False, invalid_reason="pit", car_index=0)
    )
    assert refs.best_for(3) is None
    assert refs.best_lap(SOURCE_SELF) is None


def test_update_ignores_too_short_trace() -> None:
    refs = _refs()
    short = _trace(1)
    short.samples = short.samples[:1]
    refs.update(short)
    assert refs.best_for(3) is None


# ---------------------------------------------------------------------------
# 세션 최속차 기준


def test_fastest_car_reference_used_per_corner() -> None:
    """AI 세션: 코너마다 내 최고 랩과 최속차 최고 랩 중 더 빠른 쪽을 쓴다."""
    refs = _refs()
    me = _trace(1, driver=DriverProfile(consistency=1.0))
    # 전체 랩은 더 빠르지만 5번 코너만 이른 브레이크로 느린 AI 차.
    fastest_car = _trace(
        1,
        driver=DriverProfile(pace=1.02, consistency=1.0),
        effects=_early_brake_effects(5),
        car_index=1,
    )
    assert fastest_car.samples[-1].t_ms < me.samples[-1].t_ms
    refs.update(me)
    refs.update(fastest_car)

    assert refs.best_lap(SOURCE_FASTEST_CAR) is fastest_car

    # 5번: 내가 더 빠르므로 내 랩, 9번: 최속차가 더 빠르므로 최속차 랩.
    corner5 = refs.best_for(5)
    corner9 = refs.best_for(9)
    assert corner5 is not None and corner5[1] == SOURCE_SELF
    assert corner5[0] is me
    assert corner9 is not None and corner9[1] == SOURCE_FASTEST_CAR
    assert corner9[0] is fastest_car


def test_fastest_car_is_the_fastest_other_car() -> None:
    """코너별로 모든 상대 차를 보지 않고, 세션 최속차 한 대의 랩만 후보로 둔다."""
    refs = _refs()
    me = _trace(1, driver=DriverProfile(consistency=1.0))
    # 최속차(1번): 5번 코너에서 느리지만 전체 랩은 가장 빠르다.
    fastest_car = _trace(
        1,
        driver=DriverProfile(pace=1.03, consistency=1.0),
        effects=_early_brake_effects(5),
        car_index=1,
    )
    # 두 번째로 빠른 차(2번): 5번 코너는 최속차보다 빠르다.
    second_car = _trace(
        1,
        driver=DriverProfile(pace=1.01, consistency=1.0),
        car_index=2,
    )
    assert fastest_car.samples[-1].t_ms < second_car.samples[-1].t_ms

    refs.update(me)
    refs.update(fastest_car)
    refs.update(second_car)

    assert refs.best_lap(SOURCE_FASTEST_CAR) is fastest_car
    # 최속차는 5번에서 나보다 느리므로 내 랩이 기준이다. 2번 차의 코너 기록은
    # "세션 최속차"가 아니므로 후보에 끼지 않는다.
    corner5 = refs.best_for(5)
    assert corner5 is not None
    assert corner5[1] == SOURCE_SELF
    assert corner5[0] is me


def test_time_trial_has_no_fastest_car() -> None:
    refs = _refs()
    refs.update(_trace(1))
    refs.update(_trace(2, driver=DriverProfile(pace=0.99, consistency=1.0)))

    assert refs.best_lap(SOURCE_FASTEST_CAR) is None
    best = refs.best_for(3)
    assert best is not None and best[1] == SOURCE_SELF


def test_fastest_car_comparison_has_no_player_only_values() -> None:
    """최속차 랩에는 휠 슬립이 없다. 휠 슬립은 내 최고 랩과만 비교한다."""
    refs = _refs()
    me = _samples_with_slip(_trace(1))
    fastest_car = _trace(1, driver=DriverProfile(pace=1.02), car_index=1)
    refs.update(me)
    refs.update(fastest_car)

    corner = _corner(3)
    assert corner_metrics(me, corner).exit_wheel_slip is not None
    reference = refs.best_for(3)
    assert reference is not None and reference[1] == SOURCE_FASTEST_CAR
    assert corner_metrics(reference[0], corner).exit_wheel_slip is None


def test_best_for_tie_prefers_self() -> None:
    refs = _refs()
    me = _trace(1)
    same_lap_other_car = _trace(1, car_index=1)
    refs.update(me)
    refs.update(same_lap_other_car)

    best = refs.best_for(3)
    assert best is not None
    assert best[0] is me
    assert best[1] == SOURCE_SELF


def test_best_for_unknown_corner_or_empty_set() -> None:
    refs = _refs()
    assert refs.best_for(3) is None
    refs.update(_trace(1))
    assert refs.best_for(99) is None


# ---------------------------------------------------------------------------
# 코너 시간 손실


def test_corner_time_loss_positive_when_slower() -> None:
    """이른 브레이크로 느려진 랩은 기준 랩보다 코너 시간 손실이 양수다."""
    reference = _trace(4)
    slower = _coaching_trace(1)
    corner = _corner(5)
    loss = corner_time_loss_ms(slower, reference, corner)
    assert loss > 0, loss


def test_corner_time_loss_negative_when_faster() -> None:
    """늦은 브레이크로 코너를 빨리 지나간 랩은 손실이 음수다."""
    reference = _trace(4)
    faster = _coaching_trace(1)
    corner = _corner(3)
    loss = corner_time_loss_ms(faster, reference, corner)
    assert loss < 0, loss


def test_corner_time_loss_zero_for_same_lap() -> None:
    trace = _trace(4)
    assert corner_time_loss_ms(trace, trace, _corner(3)) == 0


def test_trace_outline_and_line_reference() -> None:
    """기준 랩 궤적으로 만든 윤곽으로 라인 이탈을 잰다."""
    reference = _trace(4)
    path = trace_outline(reference)
    assert path is not None
    assert len(path.points) >= 2
    deviations = corner_metrics(reference, _corner(5), path).max_line_dev_m
    assert deviations == pytest.approx(0.0, abs=0.05)
