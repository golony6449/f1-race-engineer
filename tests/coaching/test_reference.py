"""기준 랩 선택과 코너 시간 손실 테스트(작업 15, SCRUM-65 · SCRUM-94).

기준 랩 = 차마다 가장 최근 유효 랩(내 차 포함)이고, 코너마다 그 코너 구간이
가장 짧은 랩을 쓴다(SCRUM-94). 이번 세션 랩이 없을 때만 저장된 기준을 쓴다.
타임 트라이얼은 내 최고 유효 랩을 그대로 쓴다. 무효 랩은 절대 기준이 되지
않는다(리뷰 포커스 4).
"""

from __future__ import annotations

import dataclasses
import json
import random

import pytest

from f1re.coaching.metrics import corner_metrics
from f1re.coaching.reference import (
    SOURCE_FASTEST_CAR,
    SOURCE_RECENT_CAR,
    SOURCE_SELF,
    ReferenceSet,
    corner_time_loss_ms,
    reference_from_dict,
    reference_to_dict,
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
# 내 최근 랩


def test_best_for_self_uses_most_recent_lap_even_if_slower() -> None:
    """같은 차의 최고 랩보다 최근 랩이 느려도 최근 랩이 기준이 된다(SCRUM-94)."""
    refs = _refs()
    fast = _trace(1, driver=DriverProfile(consistency=1.0))
    slow = _trace(2, driver=DriverProfile(pace=0.98, consistency=1.0))
    assert fast.samples[-1].t_ms < slow.samples[-1].t_ms
    refs.update(fast)
    refs.update(slow)

    best = refs.best_for(3)
    assert best is not None
    trace, source = best
    assert source == SOURCE_SELF
    assert trace is slow
    assert trace.lap == 2
    assert trace.car_index == 0


def test_best_lap_keeps_best_record_for_saving() -> None:
    """기록·저장용 `best_lap`은 최근 랩이 아니라 최고 랩을 그대로 돌려준다."""
    refs = _refs()
    fast = _trace(1, driver=DriverProfile(consistency=1.0))
    slow = _trace(2, driver=DriverProfile(pace=0.98, consistency=1.0))
    refs.update(fast)
    refs.update(slow)

    assert refs.best_lap(SOURCE_SELF) is fast
    assert refs.best_for(3) is not None and refs.best_for(3)[0] is slow


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
# 다른 차의 최근 랩 기준(SCRUM-94)


def test_recent_lap_reference_used_per_corner() -> None:
    """AI 세션: 코너마다 내 최근 랩과 다른 차 최근 랩 중 더 빠른 쪽을 쓴다."""
    refs = _refs()
    me = _trace(1, driver=DriverProfile(consistency=1.0))
    # 전체 랩은 더 빠르지만 5번 코너만 이른 브레이크로 느린 AI 차.
    other = _trace(
        1,
        driver=DriverProfile(pace=1.02, consistency=1.0),
        effects=_early_brake_effects(5),
        car_index=1,
    )
    assert other.samples[-1].t_ms < me.samples[-1].t_ms
    refs.update(me)
    refs.update(other)

    # 5번: 내가 더 빠르므로 내 랩, 9번: 다른 차가 더 빠르므로 그 차 랩.
    corner5 = refs.best_for(5)
    corner9 = refs.best_for(9)
    assert corner5 is not None and corner5[1] == SOURCE_SELF
    assert corner5[0] is me
    assert corner9 is not None and corner9[1] == SOURCE_RECENT_CAR
    assert corner9[0] is other


def test_every_other_cars_recent_lap_is_a_candidate() -> None:
    """최속차 한 대만 보지 않고, 모든 다른 차의 최근 랩을 코너별로 비교한다."""
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

    # 다른 차 중 가장 빠른 랩은 여전히 최속차 랩이다(기록·저장용).
    assert refs.best_lap(SOURCE_FASTEST_CAR) is fastest_car
    # 5번 코너는 2번 차의 최근 랩이 가장 빠르므로 그 랩이 기준이다.
    corner5 = refs.best_for(5)
    assert corner5 is not None
    assert corner5[1] == SOURCE_RECENT_CAR
    assert corner5[0] is second_car


def test_most_recent_lap_per_car_replaces_previous_lap() -> None:
    """다른 차도 자기 최고 랩이 아니라 가장 최근 랩만 후보로 남는다."""
    refs = _refs()
    fast = _trace(1, driver=DriverProfile(pace=1.02), car_index=1)
    slow = _trace(2, driver=DriverProfile(pace=0.98), car_index=1)
    refs.update(fast)
    refs.update(slow)

    assert refs.best_lap(SOURCE_FASTEST_CAR) is fast
    best = refs.best_for(3)
    assert best is not None
    assert best[0] is slow
    assert best[1] == SOURCE_RECENT_CAR


def test_time_trial_uses_best_self_lap_and_ignores_other_cars() -> None:
    """타임 트라이얼은 최근 랩 규칙 대신 내 최고 랩을 그대로 쓴다(SCRUM-94)."""
    refs = ReferenceSet(TRACK.corners, session_type=18)
    fast = _trace(1)
    slow = _trace(2, driver=DriverProfile(pace=0.98, consistency=1.0))
    other = _trace(1, driver=DriverProfile(pace=1.05, consistency=1.0), car_index=1)
    refs.update(fast)
    refs.update(slow)
    refs.update(other)

    assert refs.best_lap(SOURCE_FASTEST_CAR) is None
    for corner in TRACK.corners:
        best = refs.best_for(corner.number)
        assert best is not None
        assert best[0] is fast
        assert best[1] == SOURCE_SELF


def test_other_car_comparison_has_no_player_only_values() -> None:
    """다른 차 랩에는 휠 슬립이 없다. 휠 슬립은 내 차 랩과만 비교한다."""
    refs = _refs()
    me = _samples_with_slip(_trace(1))
    other = _trace(1, driver=DriverProfile(pace=1.02), car_index=1)
    refs.update(me)
    refs.update(other)

    corner = _corner(3)
    assert corner_metrics(me, corner).exit_wheel_slip is not None
    reference = refs.best_for(3)
    assert reference is not None and reference[1] == SOURCE_RECENT_CAR
    assert corner_metrics(reference[0], corner).exit_wheel_slip is None


def test_stored_reference_used_only_before_session_laps() -> None:
    """이번 세션 랩이 없으면 저장 기준, 하나라도 생기면 세션 랩을 쓴다(SCRUM-94)."""
    refs = _refs()
    stored = _trace(7, driver=DriverProfile(pace=1.02), car_index=0)
    refs.add_stored(stored)

    before = refs.best_for(3)
    assert before is not None
    assert before[0] is stored
    assert before[1] == SOURCE_SELF

    # 세션 랩이 느려도 저장 기준보다 우선한다.
    slow = _trace(1, driver=DriverProfile(pace=0.98), car_index=0)
    refs.update(slow)
    after = refs.best_for(3)
    assert after is not None
    assert after[0] is slow
    assert after[1] == SOURCE_SELF


def test_stored_other_car_lap_keeps_fastest_car_source() -> None:
    """세션 랩이 없을 때 쓰는 저장 기준의 다른 차 랩 출처는 `fastest_car`다."""
    refs = _refs()
    stored = _trace(7, driver=DriverProfile(pace=1.02), car_index=1)
    refs.add_stored(stored)

    best = refs.best_for(3)
    assert best is not None
    assert best[0] is stored
    assert best[1] == SOURCE_FASTEST_CAR


def test_prune_keeps_most_recent_lap_per_car() -> None:
    """prune() 뒤에도 차마다 가장 최근 랩이 기준이고 최고 기록은 남는다."""
    refs = _refs()
    fast = _trace(1, driver=DriverProfile(pace=1.02), car_index=0)
    slow = _trace(2, driver=DriverProfile(pace=0.98), car_index=0)
    # 내 최근 랩보다 느린 다른 차 → 모든 코너에서 내 최근 랩이 기준이다.
    other = _trace(1, driver=DriverProfile(pace=0.95), car_index=1)
    for trace in (fast, slow, other):
        refs.update(trace)

    refs.prune()

    best = refs.best_for(3)
    assert best is not None and best[0] is slow
    assert refs.best_lap(SOURCE_SELF) is fast
    assert refs.best_lap(SOURCE_FASTEST_CAR) is other


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


# ---------------------------------------------------------------------------
# 직렬화: 바퀴별 노면(SCRUM-93)


def _reference_with_surface(
    surface: tuple[int, ...] | None = (0, 1, 7, 7)
) -> ReferenceSet:
    references = ReferenceSet(TRACK.corners)
    trace = _trace(1)
    trace.samples = [
        dataclasses.replace(sample, surface=surface) for sample in trace.samples
    ]
    references.update(trace)
    return references


def test_roundtrip_keeps_wheel_surface() -> None:
    """저장·복원을 거쳐도 바퀴별 노면이 그대로 남는다."""
    references = _reference_with_surface()
    payload = json.loads(json.dumps(reference_to_dict(references)))

    loaded = reference_from_dict(payload)

    best = loaded.best_lap(SOURCE_SELF)
    assert best is not None
    assert best.samples
    assert all(sample.surface == (0, 1, 7, 7) for sample in best.samples)


def test_old_reference_without_surface_loads() -> None:
    """노면 키가 없던 옛 기준 랩 JSON도 읽고, 노면은 None이 된다."""
    payload = reference_to_dict(_reference_with_surface())
    for lap in payload["laps"]:
        for sample in lap["samples"]:
            sample.pop("surface", None)

    loaded = reference_from_dict(json.loads(json.dumps(payload)))

    best = loaded.best_lap(SOURCE_SELF)
    assert best is not None
    assert all(sample.surface is None for sample in best.samples)


@pytest.mark.parametrize(
    "surface",
    [[0, 1], [0, 1, 7, 7, 0], [0, 1, "7", 7], [0, True, 7, 7], "grass"],
)
def test_malformed_surface_loads_as_none(surface: object) -> None:
    """노면 형식이 틀린 옛 데이터는 오류 없이 None으로 읽는다."""
    payload = reference_to_dict(_reference_with_surface())
    payload["laps"][0]["samples"][0]["surface"] = surface

    loaded = reference_from_dict(payload)

    best = loaded.best_lap(SOURCE_SELF)
    assert best is not None
    assert best.samples[0].surface is None


# ---------------------------------------------------------------------------
# 직렬화: 접전 표시(SCRUM-124)


def test_roundtrip_keeps_traffic_flags() -> None:
    """저장·복원을 거쳐도 접전·순위 변동 표시가 남는다(다음 레이스 기준 제외)."""
    references = _refs()
    trace = _trace(1)
    corner = _corner(9)
    trace.samples = [
        dataclasses.replace(sample, in_traffic=True, position_changed=True)
        if corner.entry_m <= sample.dist_m <= corner.exit_m
        else sample
        for sample in trace.samples
    ]
    references.update(trace)

    payload = json.loads(json.dumps(reference_to_dict(references)))
    loaded = reference_from_dict(payload)

    best = loaded.best_lap(SOURCE_SELF)
    assert best is not None
    inside = [
        sample
        for sample in best.samples
        if corner.entry_m <= sample.dist_m <= corner.exit_m
    ]
    assert inside and all(
        sample.in_traffic and sample.position_changed for sample in inside
    )
    outside = [
        sample
        for sample in best.samples
        if not corner.entry_m <= sample.dist_m <= corner.exit_m
    ]
    assert outside and not any(
        sample.in_traffic or sample.position_changed for sample in outside
    )


def test_old_reference_without_traffic_keys_loads_as_clean() -> None:
    """접전 키가 없던 옛 기준 랩 JSON은 접전 없음(False)으로 읽는다."""
    payload = reference_to_dict(_reference_with_surface())
    for lap in payload["laps"]:
        for sample in lap["samples"]:
            sample.pop("in_traffic", None)
            sample.pop("position_changed", None)

    loaded = reference_from_dict(json.loads(json.dumps(payload)))

    best = loaded.best_lap(SOURCE_SELF)
    assert best is not None
    assert not any(
        sample.in_traffic or sample.position_changed for sample in best.samples
    )
