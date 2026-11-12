"""가장 빠른 차 기준과 레이스 세션 규칙 테스트(작업 17, SCRUM-67).

설계 6.3·6.4를 데모 송신기 주행 샘플로 확인한다. 공용 conftest를 만들지 않는
규칙에 따라 도우미는 이 파일 안에 둔다.

- 기준 랩 = 내 최고 유효 랩. AI 상대 세션에서는 세션 최속차의 최고 랩도 후보로
  두고 코너마다 그 코너가 더 빠른 쪽을 기준으로 쓴다.
- 최속차와 비교할 때는 양쪽에 다 있는 값(위치·속도·페달·스티어)만 쓴다. 휠 슬립
  처럼 내 차에만 있는 값은 내 최고 랩과만 비교한다.
- 타임 트라이얼은 AI 상대가 없으므로 최속차 기준을 두지 않는다.
- 레이스 세션은 1랩(출발 혼전)을 기준·집중 코너 산정에서 빼고, 유효 랩이 2랩
  쌓인 뒤부터 집중 코너를 정한다(리뷰 포커스 4).
"""

from __future__ import annotations

import dataclasses
import random

from f1re.coaching.coach import Coach, pick_cause
from f1re.coaching.focus import (
    RACE_FOCUS_MIN_VALID_LAPS,
    FocusConfig,
    FocusSet,
)
from f1re.coaching.metrics import corner_metrics
from f1re.coaching.reference import (
    NON_AI_SESSION_TYPES,
    SOURCE_FASTEST_CAR,
    SOURCE_SELF,
    ReferenceSet,
)
from f1re.demo_sender.driving import DriverProfile, Sample as DrivingSample, generate_lap
from f1re.demo_sender.scenarios import corner_effects, parse_scenarios
from f1re.laps.traces import LapTrace, Sample
from f1re.tracks.model import Corner, Track, load_outline, load_track
from f1re.tracks.outline import TrackOutline

SUZUKA_ID = 13
SESSION_TYPE_RACE = 10
SESSION_TYPE_TIME_TRIAL = 13

_TRACK = load_track(SUZUKA_ID)
_OUTLINE = load_outline(SUZUKA_ID)
assert _TRACK is not None and _OUTLINE is not None
TRACK: Track = _TRACK
OUTLINE: TrackOutline = _OUTLINE
CORNER_NUMBERS = [corner.number for corner in TRACK.corners]

# 레이스 집중 코너 산정 테스트에서 손실을 만드는 코너.
AFFECTED = (6, 5, 11, 3, 1, 10)


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
    effects: dict | None = None,
    pace: float = 1.0,
    car_index: int = 0,
    valid: bool = True,
    invalid_reason: str | None = None,
    wheel_slip: tuple[float, ...] | None = None,
) -> LapTrace:
    samples = [
        _to_sample(sample)
        for sample in generate_lap(
            TRACK,
            OUTLINE,
            DriverProfile(pace=pace, consistency=1.0),
            lap,
            random.Random(0),
            corner_effects=effects or {},
        )
    ]
    if wheel_slip is not None:
        samples = [
            dataclasses.replace(sample, wheel_slip=wheel_slip) for sample in samples
        ]
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=valid,
        invalid_reason=invalid_reason,
        samples=samples,
    )


def _early_brake_effects(
    numbers: tuple[int, ...] = AFFECTED, meters: float = 10.0
) -> dict:
    events = parse_scenarios(
        [
            {
                "lap": 1,
                "corner": number,
                "kind": "early_brake",
                "params": {"meters": meters},
            }
            for number in numbers
        ]
    )
    return corner_effects(events, CORNER_NUMBERS)


# ---------------------------------------------------------------------------
# 가장 빠른 차 기준 (설계 6.3)


def test_fastest_car_reference_used_per_corner() -> None:
    """AI 상대 레이스 세션: 코너마다 내 최고 랩과 최속차 최고 랩 중 빠른 쪽."""
    references = ReferenceSet(TRACK.corners, session_type=SESSION_TYPE_RACE)
    me = _trace(2)
    # 전체 랩은 더 빠르지만 5번 코너만 이른 브레이크로 느린 AI 차.
    fastest_car = _trace(
        2,
        effects=_early_brake_effects((5,)),
        pace=1.02,
        car_index=1,
    )
    assert fastest_car.samples[-1].t_ms < me.samples[-1].t_ms
    references.update(me)
    references.update(fastest_car)

    assert references.best_lap(SOURCE_FASTEST_CAR) is fastest_car

    # 5번: 내가 더 빠르므로 내 랩, 9번: 최속차가 더 빠르므로 최속차 랩.
    corner5 = references.best_for(5)
    corner9 = references.best_for(9)
    assert corner5 is not None and corner5[1] == SOURCE_SELF
    assert corner5[0] is me
    assert corner9 is not None and corner9[1] == SOURCE_FASTEST_CAR
    assert corner9[0] is fastest_car


def test_fastest_car_comparison_ignores_player_only_fields() -> None:
    """휠 슬립은 내 차에만 있다. 최속차 비교는 휠 슬립을 쓰지 않는다."""
    references = ReferenceSet(TRACK.corners, session_type=SESSION_TYPE_RACE)
    me = _trace(2, wheel_slip=(0.4, 0.5, 0.6, 0.7))
    fastest_car = _trace(2, pace=1.02, car_index=1)
    references.update(me)
    references.update(fastest_car)

    corner = _corner(9)
    reference = references.best_for(corner.number)
    assert reference is not None and reference[1] == SOURCE_FASTEST_CAR
    player_metrics = corner_metrics(me, corner)
    reference_metrics = corner_metrics(reference[0], corner)
    assert player_metrics.exit_wheel_slip is not None
    assert reference_metrics.exit_wheel_slip is None

    # 공통 값이 모두 같고 휠 슬립만 크게 다르면 원인을 고르지 않는다.
    same = dataclasses.replace(player_metrics, exit_wheel_slip=None)
    noisy = dataclasses.replace(player_metrics, exit_wheel_slip=99.0)
    assert pick_cause(noisy, same) is None


def test_time_trial_has_no_fastest_car() -> None:
    """타임 트라이얼은 AI 상대가 없어 다른 차 랩이 들어와도 최속차 기준을 안 둔다."""
    assert SESSION_TYPE_TIME_TRIAL in NON_AI_SESSION_TYPES

    references = ReferenceSet(TRACK.corners, session_type=SESSION_TYPE_TIME_TRIAL)
    me = _trace(1)
    other_car = _trace(1, pace=1.02, car_index=1)
    references.update(me)
    references.update(other_car)

    assert references.best_lap(SOURCE_FASTEST_CAR) is None
    for corner in TRACK.corners:
        best = references.best_for(corner.number)
        assert best is not None
        assert best[1] == SOURCE_SELF
        assert best[0] is me

    # Coach도 세션 종류를 기준 집합에 넘겨 같은 규칙을 쓴다.
    coach = Coach(TRACK, session_type=SESSION_TYPE_TIME_TRIAL)
    coach.on_lap_completed(other_car)
    assert coach.references.best_lap(SOURCE_FASTEST_CAR) is None


# ---------------------------------------------------------------------------
# 레이스 세션 규칙 (설계 6.4, 리뷰 포커스 4)


def test_race_lap1_excluded_and_start_after_2_laps() -> None:
    """레이스 1랩은 기준·집중 코너에서 빠지고, 유효 랩 2랩이 쌓인 뒤 시작한다."""
    coach = Coach(TRACK, session_type=SESSION_TYPE_RACE)
    assert coach.focus.min_valid_laps == RACE_FOCUS_MIN_VALID_LAPS == 2

    lap1 = _trace(1)  # 기록 표시는 유효하지만 레이스 1랩(출발 혼전)이다.
    coach.on_lap_completed(lap1)
    assert coach.references.best_lap(SOURCE_SELF) is None
    assert coach.focus_corners == ()

    lap2 = _trace(2)
    coach.on_lap_completed(lap2)
    assert coach.references.best_lap(SOURCE_SELF) is lap2
    assert coach.focus_corners == ()  # 유효 랩이 아직 1랩뿐이다.

    lap3 = _trace(3, effects=_early_brake_effects(meters=20.0))
    coach.on_lap_completed(lap3)
    assert coach.focus_corners  # 유효 랩 2랩이 쌓여 집중 코너를 정한다.


def test_race_lap1_never_becomes_reference_even_if_marked_valid() -> None:
    """레이스 1랩은 `valid=True`로 들어와도 어떤 기준도 되지 않는다."""
    references = ReferenceSet(TRACK.corners, session_type=SESSION_TYPE_RACE)
    lap1 = _trace(1)
    lap2 = _trace(2, pace=0.99)

    references.update(lap1)
    references.update(lap2)

    best = references.best_for(3)
    assert best is not None
    assert best[0] is lap2
    assert references.best_lap(SOURCE_FASTEST_CAR) is None
    # 다른 차의 레이스 1랩도 최속차 후보가 되지 않는다.
    references.update(_trace(1, pace=1.05, car_index=1))
    assert references.best_lap(SOURCE_FASTEST_CAR) is None


def test_time_trial_focus_starts_from_first_lap() -> None:
    """타임 트라이얼은 레이스와 달리 첫 유효 랩부터 집중 코너를 정한다."""
    references = ReferenceSet(TRACK.corners, session_type=SESSION_TYPE_TIME_TRIAL)
    baseline = _trace(99)
    references.update(baseline)
    focus = FocusSet(
        TRACK.corners,
        references,
        config=FocusConfig(count=5, recent_laps=3),
        session_type=SESSION_TYPE_TIME_TRIAL,
    )
    assert focus.min_valid_laps == 1

    changes = focus.update([_trace(1, effects=_early_brake_effects(meters=20.0))])

    assert changes
    assert focus.corners
