"""SCRUM-124: 코칭 민감도·손실 하한·레이스 접전 코너 제외 테스트.

2026-11-15 실제 F1 25 레이스 녹화(recording-6.f1rec) 분석으로 기본값을 정했다.
- 브레이크·턴인 3~6m 차이는 대부분 0.1초 미만 손실이라 코칭하지 않는다(기본 8m).
- 원인 문턱을 넘어도 코너 손실이 100ms 미만이면 코칭하지 않는다.
- 레이스에서 앞/뒤차 1초 이내이거나 코너 안에서 순위가 바뀐 코너다.

데모 송신기 시나리오는 손실이 0에 가까워 새 기본값에서 무전이 나가지 않는다.
그래서 이 파일은 샘플 시각을 코너 안에서 선형으로 늘려 원하는 손실을 만들고,
브레이크 입력을 지워 원하는 브레이크 지점 차이를 만든다. 공용 conftest를 만들지
않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Iterable

import pytest

from f1re.coaching.coach import CAUSE_BRAKE_LATE, Coach, CoachConfig, pick_cause
from f1re.coaching.focus import FocusConfig, FocusSet
from f1re.coaching.metrics import corner_metrics
from f1re.coaching.reference import SOURCE_RECENT_CAR, SOURCE_SELF, ReferenceSet
from f1re.config import CoachingSensitivityConfig, load_config
from f1re.demo_sender.driving import (
    DriverProfile,
    Sample as DrivingSample,
    generate_lap,
)
from f1re.laps.traces import LapTrace, Sample, corner_has_traffic
from f1re.tracks.model import Corner, Track, load_outline, load_track

SUZUKA_ID = 13
_BASE_NS = time.time_ns()

_SUZUKA = load_track(SUZUKA_ID)
_SUZUKA_OUTLINE = load_outline(SUZUKA_ID)
assert _SUZUKA is not None and _SUZUKA_OUTLINE is not None
TRACK: Track = _SUZUKA
OUTLINE = _SUZUKA_OUTLINE
CORNER9: Corner = next(corner for corner in TRACK.corners if corner.number == 9)


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


def _trace(
    lap: int = 1,
    *,
    car_index: int = 0,
    pace: float = 1.0,
) -> LapTrace:
    samples = generate_lap(
        TRACK,
        OUTLINE,
        DriverProfile(pace=pace, consistency=1.0),
        lap,
        random.Random(0),
    )
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=True,
        invalid_reason=None,
        samples=[_to_sample(sample) for sample in samples],
    )


def _late_brake(trace: LapTrace, corner: Corner, meters: float) -> LapTrace:
    """브레이크 시작을 `meters`만큼 늦춘 랩(브레이크 입력을 그만큼 지운다).

    시각은 건드리지 않으므로 손실은 `_delay`가 따로 만든다.
    """
    ordered = sorted(trace.samples, key=lambda sample: sample.dist_m)
    start = next(
        (
            sample.dist_m
            for sample in ordered
            if corner.entry_m - 150.0 <= sample.dist_m <= corner.apex_m
            and sample.brake >= 0.1
        ),
        None,
    )
    if start is None:
        return trace
    return dataclasses.replace(
        trace,
        samples=[
            dataclasses.replace(sample, brake=0.0)
            if start <= sample.dist_m < start + meters and sample.brake >= 0.1
            else sample
            for sample in trace.samples
        ],
    )


def _delay(trace: LapTrace, corner: Corner, ms: float) -> LapTrace:
    """코너 진입~탈출에서 시간을 선형으로 늘려 코너 손실을 `ms`로 만든다.

    진입 시각은 그대로 두고 탈출 시각을 `ms`만큼 늦춘다. 탈출 뒤 샘플도 같이
    늦춰 다음 코너들의 손실은 0으로 유지한다.
    """
    span = max(1e-6, corner.exit_m - corner.entry_m)
    samples = []
    for sample in trace.samples:
        if sample.dist_m <= corner.entry_m:
            samples.append(sample)
            continue
        ratio = min(1.0, (sample.dist_m - corner.entry_m) / span)
        samples.append(
            dataclasses.replace(sample, t_ms=sample.t_ms + round(ms * ratio))
        )
    return dataclasses.replace(trace, samples=samples)


def _slow_apex(trace: LapTrace, corner: Corner, kmh: float = 6.0) -> LapTrace:
    """코너 구간 속도를 `kmh`만큼 낮춰 정점 최저속 저하를 만든다(SCRUM-144).

    브레이크 지점만 늦은 랩은 레이트 브레이킹 전략으로 보고 코칭하지 않는다.
    늦은 브레이킹이 실제로 코너를 망친 경우(정점 속도 저하)를 만들 때 쓴다.
    시각은 건드리지 않으므로 코너 손실은 `_delay`가 따로 만든다.
    """
    return dataclasses.replace(
        trace,
        samples=[
            dataclasses.replace(sample, speed=max(0.0, sample.speed - kmh))
            if corner.entry_m <= sample.dist_m <= corner.exit_m
            else sample
            for sample in trace.samples
        ],
    )


def _mark(
    trace: LapTrace,
    corner: Corner,
    *,
    in_traffic: bool = False,
    position_changed: bool = False,
) -> LapTrace:
    """코너 구간 샘플에 접전·순위 변동 표시를 붙인다."""
    return dataclasses.replace(
        trace,
        samples=[
            dataclasses.replace(
                sample,
                in_traffic=in_traffic,
                position_changed=position_changed,
            )
            if corner.entry_m <= sample.dist_m <= corner.exit_m
            else sample
            for sample in trace.samples
        ],
    )


def _coach(
    *,
    session_type: int | None = None,
    focus: Iterable[int] = (9,),
) -> Coach:
    references = ReferenceSet(
        TRACK.corners, session_type=session_type
    )
    # 기준 랩은 다른 차(1번)의 랩이다. 데모 주행은 랩마다 같으므로 손실은 우리가
    # 만든 시각 지연만큼이다. 타임 트라이얼은 다른 차 기준을 쓰지 않으므로
    # 내 차 랩을 기준으로 넣는다(ReferenceSet의 타임 트라이얼 규칙).
    references.update(
        _trace(99, car_index=0 if session_type == 18 else 1)
    )
    coach = Coach(
        TRACK,
        references=references,
        focus_config=FocusConfig(count=5, recent_laps=3),
        session_type=session_type,
    )
    if focus:
        coach.seed_focus(focus)
    return coach


def _state(sample: Sample, lap: int) -> dict:
    return {
        "t_ms": sample.t_ms,
        "speed": sample.speed,
        "throttle": sample.throttle,
        "brake": sample.brake,
        "steer": sample.steer,
        "gear": sample.gear,
        "x": sample.x,
        "z": sample.z,
        "surface": sample.surface,
        "in_traffic": sample.in_traffic,
        "position_changed": sample.position_changed,
        "lap_num": lap,
    }


def _feed(coach: Coach, trace: LapTrace) -> list:
    """랩 샘플을 코치에 넣고 그 랩에서 나간 무전을 돌려준다."""
    base_ns = _BASE_NS + trace.lap * 200_000_000_000
    messages: list = []
    for sample in trace.samples:
        now_ns = base_ns + sample.t_ms * 1_000_000
        messages.extend(coach.on_sample(_state(sample, trace.lap), sample.dist_m, now_ns))
    return messages


def _corner_diff(trace: LapTrace, reference: LapTrace, corner: Corner) -> float | None:
    metrics = corner_metrics(trace, corner)
    reference_metrics = corner_metrics(reference, corner)
    if metrics.brake_start_m is None or reference_metrics.brake_start_m is None:
        return None
    return metrics.brake_start_m - reference_metrics.brake_start_m


# ---------------------------------------------------------------------------
# 민감도와 손실 하한


def test_default_config_matches_sensitivity_defaults() -> None:
    """`config/default.toml`의 `[coaching]`과 코드 기본값이 같다."""
    config = load_config()

    assert config.coaching == CoachingSensitivityConfig()
    assert config.coaching.brake_diff_m == 8.0
    assert config.coaching.turn_in_diff_m == 8.0
    assert config.coaching.min_corner_loss_ms == 100
    assert CoachConfig.from_app_config(config).sensitivity == config.coaching


def test_brake_difference_of_three_to_six_meters_is_not_coached() -> None:
    """브레이크 지점 차이가 3~6m뿐이면 원인이 되어도 코칭하지 않는다."""
    reference = _trace(99, car_index=1)
    lap = _delay(_late_brake(_trace(), CORNER9, 4.0), CORNER9, 150.0)
    diff = _corner_diff(lap, reference, CORNER9)
    assert diff is not None and 3.0 <= diff <= 6.0, diff
    # 손실은 100ms를 넘는다. 원인 문턱(8m)이 막는지 확인한다.
    assert pick_cause(corner_metrics(lap, CORNER9), corner_metrics(reference, CORNER9)) is None

    assert _feed(_coach(), lap) == []


def test_cause_below_eight_meters_still_speaks_when_loss_is_large() -> None:
    """문턱(8m)을 넘는 12m 차이는 손실이 충분하면 지금처럼 코칭한다.

    SCRUM-144: 브레이크 지점만 늦은 경우는 레이트 브레이킹 전략으로 보아
    코칭하지 않으므로, 정점 최저속 저하(동반 악화)를 함께 만든다.
    """
    lap = _slow_apex(
        _delay(_late_brake(_trace(), CORNER9, 12.0), CORNER9, 150.0), CORNER9
    )

    messages = _feed(_coach(), lap)

    assert len(messages) == 1
    message = messages[0]
    assert message.corner == 9
    assert message.evidence["cause"] == CAUSE_BRAKE_LATE
    assert message.evidence["loss_ms"] >= 100


def test_corner_loss_under_100_ms_is_not_coached() -> None:
    """원인 문턱을 넘어도 코너 손실이 100ms 미만이면 말하지 않는다."""
    reference = _trace(99, car_index=1)
    # SCRUM-144의 레이트 브레이킹 보호에 걸리지 않도록 정점 속도 저하를 준다.
    lap = _slow_apex(
        _delay(_late_brake(_trace(), CORNER9, 12.0), CORNER9, 60.0), CORNER9
    )
    # 원인(브레이크 12m 늦음)은 문턱을 넘는다. 손실 하한만 막는다.
    cause = pick_cause(
        corner_metrics(lap, CORNER9, None),
        corner_metrics(reference, CORNER9, None),
    )
    assert cause is not None and cause.key == CAUSE_BRAKE_LATE

    assert _feed(_coach(), lap) == []


def test_corner_loss_at_100_ms_speaks() -> None:
    """손실이 정확히 100ms 이상이면 코칭한다."""
    lap = _slow_apex(
        _delay(_late_brake(_trace(), CORNER9, 12.0), CORNER9, 120.0), CORNER9
    )

    messages = _feed(_coach(), lap)

    assert len(messages) == 1
    assert messages[0].evidence["loss_ms"] >= 100


# ---------------------------------------------------------------------------
# 레이스 접전 코너


@pytest.mark.parametrize("flag", ["in_traffic", "position_changed"])
def test_race_traffic_corner_is_not_coached(flag: str) -> None:
    """레이스에서 1초 이내 접전이거나 순위가 바뀐 코너는 코칭하지 않는다.

    SCRUM-144: 레이트 브레이킹 보호가 아니라 접전 제외가 막는지 확인하려고
    정점 속도 저하를 동반한(코칭 조건을 넘는) 랩을 쓴다.
    """
    lap = _slow_apex(
        _delay(_late_brake(_trace(lap=2), CORNER9, 12.0), CORNER9, 150.0),
        CORNER9,
    )
    lap = _mark(lap, CORNER9, **{flag: True})
    assert corner_has_traffic(lap.samples, CORNER9)

    assert _feed(_coach(session_type=15), lap) == []


@pytest.mark.parametrize("flag", ["in_traffic", "position_changed"])
def test_traffic_corner_is_coached_outside_race(flag: str) -> None:
    """타임 트라이얼 등 비레이스 세션은 같은 표시가 있어도 제외하지 않는다."""
    lap = _slow_apex(
        _delay(_late_brake(_trace(), CORNER9, 12.0), CORNER9, 150.0), CORNER9
    )
    lap = _mark(lap, CORNER9, **{flag: True})

    messages = _feed(_coach(session_type=18), lap)

    assert len(messages) == 1
    assert messages[0].corner == 9


def test_traffic_exclusion_resets_repeat_streak() -> None:
    """접전으로 건너뛴 랩은 반복 실수 연속 횟수를 끊는다."""
    coach = _coach(session_type=15)
    normal = _slow_apex(
        _delay(_late_brake(_trace(lap=2), CORNER9, 12.0), CORNER9, 150.0),
        CORNER9,
    )

    first = _feed(coach, normal)
    second = _feed(coach, dataclasses.replace(normal, lap=3))
    assert first[0].evidence["streak"] == 1
    assert second[0].evidence["streak"] == 2

    traffic = _mark(
        dataclasses.replace(normal, lap=4), CORNER9, in_traffic=True
    )
    assert _feed(coach, traffic) == []

    fifth = _feed(coach, dataclasses.replace(normal, lap=5))
    assert fifth[0].evidence["streak"] == 1


def test_loss_floor_resets_repeat_streak() -> None:
    """손실 하한에 걸려 코칭하지 않은 랩도 연속 실수 횟수를 끊는다."""
    coach = _coach(session_type=15)
    normal = _slow_apex(
        _delay(_late_brake(_trace(lap=2), CORNER9, 12.0), CORNER9, 150.0),
        CORNER9,
    )
    _feed(coach, normal)
    _feed(coach, dataclasses.replace(normal, lap=3))

    quiet = _slow_apex(
        _delay(_late_brake(_trace(lap=4), CORNER9, 12.0), CORNER9, 40.0),
        CORNER9,
    )
    assert _feed(coach, quiet) == []

    fifth = _feed(coach, dataclasses.replace(normal, lap=5))
    assert fifth[0].evidence["streak"] == 1


# ---------------------------------------------------------------------------
# 기준 랩·집중 코너 제외


def test_reference_skips_traffic_corner_lap_in_race() -> None:
    """접전 코너 랩은 그 코너 기준 후보에서 빠지고, 다른 코너는 그대로 쓴다."""
    fast = _mark(_trace(lap=2), CORNER9, in_traffic=True)
    slow = _trace(lap=2, car_index=1, pace=0.98)
    references = ReferenceSet(TRACK.corners, session_type=15)
    references.update(fast)
    references.update(slow)

    # 9번은 접전이었던 빠른 랩을 건너뛰고 느린 랩을 쓴다.
    corner9 = references.best_for(9)
    assert corner9 is not None
    assert corner9[0] is slow
    assert corner9[1] == SOURCE_RECENT_CAR
    # 접전이 아니었던 다른 코너는 빠른 랩을 그대로 쓴다.
    corner3 = references.best_for(3)
    assert corner3 is not None and corner3[0] is fast
    assert corner3[1] == SOURCE_SELF


def test_reference_has_no_corner_when_only_candidate_was_traffic() -> None:
    """그 코너의 후보가 접전 랩뿐이면 기준을 만들지 않는다(코칭도 하지 않는다)."""
    references = ReferenceSet(TRACK.corners, session_type=15)
    references.update(_mark(_trace(lap=2), CORNER9, in_traffic=True))

    assert references.best_for(9) is None
    assert references.best_for(3) is not None


def test_reference_does_not_skip_traffic_outside_race() -> None:
    """비레이스(타임 트라이얼)에서는 접전 표시를 무시한다."""
    fast = _mark(_trace(lap=2), CORNER9, in_traffic=True)
    references = ReferenceSet(TRACK.corners, session_type=18)
    references.update(fast)

    best = references.best_for(9)
    assert best is not None and best[0] is fast
    assert best[1] == SOURCE_SELF


def test_stored_traffic_reference_is_skipped_in_race() -> None:
    """지난 세션에서 저장한 기준 랩도 접전 코너 표시를 지키면 후보에서 뺀다."""
    references = ReferenceSet(TRACK.corners, session_type=15)
    stored = _mark(_trace(7), CORNER9, in_traffic=True)
    references.add_stored(stored)

    assert references.best_for(9) is None
    assert references.best_for(3) is not None


def test_focus_ignores_traffic_corner_loss_in_race() -> None:
    """레이스 집중 코너 계산은 접전 코너 손실을 빼고 평균을 낸다."""
    references = ReferenceSet(TRACK.corners, session_type=15)
    references.update(_trace(99, car_index=1))
    clean = _trace(lap=1)
    traffic = _mark(
        _delay(_trace(lap=2), CORNER9, 800.0), CORNER9, in_traffic=True
    )
    focus = FocusSet(
        TRACK.corners,
        references,
        config=FocusConfig(count=5, recent_laps=3, min_loss_ms=50),
        session_type=15,
    )

    focus.update([clean, traffic])

    assert focus.losses().get(9, 0) < 50
    assert 9 not in focus.corners


def test_focus_counts_traffic_corner_loss_outside_race() -> None:
    """비레이스에서는 같은 접전 표시가 있어도 집중 코너 손실에 넣는다."""
    references = ReferenceSet(TRACK.corners, session_type=18)
    references.update(_trace(99, car_index=0))
    clean = _trace(lap=1)
    traffic = _mark(
        _delay(_trace(lap=2), CORNER9, 800.0), CORNER9, in_traffic=True
    )
    focus = FocusSet(
        TRACK.corners,
        references,
        config=FocusConfig(count=5, recent_laps=3, min_loss_ms=50),
        session_type=18,
    )

    focus.update([clean, traffic])

    assert focus.losses().get(9, 0) >= 100
    assert 9 in focus.corners
