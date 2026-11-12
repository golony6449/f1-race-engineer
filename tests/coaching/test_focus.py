"""집중 코너 선택과 개선 감지 테스트(작업 16, SCRUM-66).

설계 6.4의 집중 코너 규칙을 데모 송신기 주행 샘플로 확인한다. 공용 conftest를
만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from f1re.coaching.focus import (
    FocusChange,
    FocusConfig,
    FocusReason,
    FocusSet,
    corner_loss_ms,
    lap_time_points,
)
from f1re.coaching.reference import ReferenceSet, corner_time_loss_ms
from f1re.demo_sender.driving import DriverProfile, Sample as DrivingSample, generate_lap
from f1re.demo_sender.scenarios import corner_effects, parse_scenarios
from f1re.laps.traces import LapTrace, Sample
from f1re.tracks.model import load_outline, load_track

SUZUKA_ID = 13

_TRACK = load_track(SUZUKA_ID)
_OUTLINE = load_outline(SUZUKA_ID)
assert _TRACK is not None and _OUTLINE is not None
TRACK = _TRACK
OUTLINE = _OUTLINE
CORNER_NUMBERS = [corner.number for corner in TRACK.corners]

# 집중 코너 테스트에서 손실을 만드는 코너. 이른 브레이크는 랩 시간을 늘린다.
AFFECTED = (6, 5, 11, 3, 1, 10)


# ---------------------------------------------------------------------------
# 도우미


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


def _early_brake_effects(meters: float = 10.0) -> dict:
    events = parse_scenarios(
        [
            {
                "lap": 1,
                "corner": number,
                "kind": "early_brake",
                "params": {"meters": meters},
            }
            for number in AFFECTED
        ]
    )
    return corner_effects(events, CORNER_NUMBERS)


def _trace(
    lap: int,
    *,
    effects: dict | None = None,
    pace: float = 1.0,
    car_index: int = 0,
    valid: bool = True,
    invalid_reason: str | None = None,
    seed: int = 0,
) -> LapTrace:
    samples = generate_lap(
        TRACK,
        OUTLINE,
        DriverProfile(pace=pace, consistency=1.0),
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


def _reference() -> tuple[ReferenceSet, LapTrace]:
    """기준 랩 하나(주입 없는 가장 빠른 랩)를 넣은 기준 집합."""
    references = ReferenceSet(TRACK.corners)
    baseline = _trace(99)
    references.update(baseline)
    return references, baseline


def _effects() -> dict:
    return _early_brake_effects()


def _expected_ranking(
    references: ReferenceSet, laps: list[LapTrace], *, minimum: int = 50
) -> list[int]:
    """테스트가 직접 계산한 평균 손실 순위(집중 코너 기대값)."""
    ranking: list[tuple[int, int]] = []
    for corner in TRACK.corners:
        reference = references.best_for(corner.number)
        assert reference is not None
        losses = [corner_time_loss_ms(lap, reference[0], corner) for lap in laps]
        mean = int(round(sum(losses) / len(losses)))
        if mean >= minimum:
            ranking.append((corner.number, mean))
    ranking.sort(key=lambda item: (-item[1], item[0]))
    return [number for number, _loss in ranking]


def _focus_set(
    references: ReferenceSet, **kwargs
) -> FocusSet:
    return FocusSet(
        TRACK.corners,
        references,
        config=FocusConfig(count=5, recent_laps=3, **kwargs),
    )


# ---------------------------------------------------------------------------
# 집중 코너 선택


def test_focus_top5_from_last_3_laps() -> None:
    """최근 3랩 평균 손실이 큰 코너 5개를 고른다."""
    references, _baseline = _reference()
    focus = _focus_set(references)
    laps = [_trace(lap, effects=_effects()) for lap in (1, 2, 3)]

    changes = focus.update(laps)

    expected = _expected_ranking(references, laps)[:5]
    assert len(expected) == 5
    assert list(focus.corners) == expected
    assert {change.corner for change in changes} == set(expected)
    assert all(change.reason is FocusReason.ADDED for change in changes)


def test_focus_uses_only_recent_laps() -> None:
    """최근 3랩만 본다. 고친 코너는 3랩이 지나면 목록에서 빠진다."""
    references, _baseline = _reference()
    focus = _focus_set(references)
    focus.update([_trace(lap, effects=_effects()) for lap in (1, 2, 3)])
    assert focus.corners

    for lap in (4, 5, 6):
        focus.update([_trace(lap)])

    assert focus.corners == ()


def test_focus_waits_until_enough_valid_laps() -> None:
    """레이스에서 2~3랩 쌓인 뒤 시작하는 규칙을 min_valid_laps로 표현한다."""
    references, _baseline = _reference()
    focus = _focus_set(references, min_valid_laps=3)

    assert focus.update([_trace(1, effects=_effects())]) == []
    assert focus.update([_trace(2, effects=_effects())]) == []
    assert focus.corners == ()

    focus.update([_trace(3, effects=_effects())])
    assert focus.corners


def test_invalid_and_ai_laps_are_ignored() -> None:
    """무효 랩(아웃랩·피트·SC·레이스 1랩)과 다른 차 랩은 집중 코너 산정에서 뺀다."""
    references, _baseline = _reference()
    focus = _focus_set(references)

    focus.update(
        [
            _trace(1, effects=_effects(), valid=False, invalid_reason="outlap"),
            _trace(2, effects=_effects(), car_index=3),
        ]
    )

    assert focus.corners == ()


def test_seed_focus_starts_from_saved_corners() -> None:
    """저장된 집중 코너가 있으면 첫 갱신 전에도 그 목록으로 시작한다(작업 19)."""
    references, _baseline = _reference()
    focus = _focus_set(references)

    focus.seed([7, 3, 7, 99])

    assert focus.corners == (7, 3)
    assert focus.contains(7)


def test_seed_focus_does_not_announce_improvement_on_first_clean_lap() -> None:
    """세션 첫 랩이 저장 기준과 같다고 저장 집중 코너를 '좋아짐'으로 알리지 않는다."""
    references, _baseline = _reference()
    focus = _focus_set(references)
    focus.seed([3, 5])

    changes = focus.update([_trace(1)])

    assert [change for change in changes if change.reason is FocusReason.IMPROVED] == []


def test_focus_replaced_when_improved() -> None:
    """집중 코너의 최신 손실이 문턱 아래로 줄면 빼고 다음 코너를 넣는다."""
    references, _baseline = _reference()
    focus = _focus_set(references)
    focus.update([_trace(lap, effects=_effects()) for lap in (1, 2, 3)])
    before = list(focus.corners)
    assert before[0] == 6

    # 6번 코너의 이른 브레이크만 뺀 랩. 나머지 다섯 코너 손실은 그대로다.
    fixed_effects = _early_brake_effects()
    fixed_effects.pop(6)
    changes = focus.update([_trace(4, effects=fixed_effects)])

    improved = [change for change in changes if change.reason is FocusReason.IMPROVED]
    assert [change.corner for change in improved] == [6]
    assert improved[0].next_corner is not None
    assert 6 not in focus.corners
    assert improved[0].next_corner in focus.corners
    assert focus.corners == tuple(
        number for number in before if number != 6
    ) + (improved[0].next_corner,)


def test_focus_uses_fastest_car_reference_per_corner() -> None:
    """AI 랩이 더 빠른 코너는 그 랩을 기준으로 손실을 잰다(작업 15·17 연결)."""
    references, _baseline = _reference()
    # 6번 코너만 아주 느린 내 랩을 최근 랩으로 넣는다.
    slow_effects = _early_brake_effects()
    slow = _trace(1, effects=slow_effects, pace=0.99, car_index=0)
    references.update(slow)
    fastest = _trace(1, pace=1.02, car_index=1)
    references.update(fastest)

    focus = _focus_set(references)
    focus.update([slow])

    assert 6 in focus.corners


# ---------------------------------------------------------------------------
# 손실 계산 동등성


def test_corner_loss_matches_reference_module() -> None:
    """빠른 손실 계산이 작업 15의 corner_time_loss_ms와 같은 값을 낸다(±1ms)."""
    references, baseline = _reference()
    slow = _trace(1, effects=_effects(), pace=0.98)
    points = lap_time_points(slow)
    reference_points = lap_time_points(baseline)

    for corner in TRACK.corners:
        fast = corner_loss_ms(points, reference_points, corner)
        slow_value = corner_time_loss_ms(slow, baseline, corner)
        assert fast == pytest.approx(slow_value, abs=1), (corner.number, fast, slow_value)


def test_lap_time_points_are_sorted_by_distance() -> None:
    trace = _trace(1)
    shuffled = dataclasses.replace(
        trace, samples=list(reversed(trace.samples))
    )

    points = lap_time_points(shuffled)

    distances = [point[0] for point in points]
    assert distances == sorted(distances)


def test_focus_config_reads_app_config() -> None:
    """설정 파일의 집중 코너 수·최근 랩 수를 그대로 쓴다."""
    from f1re.config import load_config

    config = load_config()
    focus_config = FocusConfig.from_app_config(config)

    assert focus_config.count == config.focus_corner_count
    assert focus_config.recent_laps == config.focus_recent_laps
