"""SCRUM-144: 트랙 이탈 없는 레이트 브레이킹 전략을 코칭에서 보호한다.

2026-11-19 사용자 지적: 방어·추월을 위해 일부러 늦게 브레이킹했지만 코너를
깔끔히 돌아나간 경우에도 "브레이크가 늦었어" 무전이 나갔다. 규칙은 두 가지다.

- 코너 손실이 0 이하(기준과 같거나 더 빠름)이고 트랙을 벗어나지 않았으면
  집중 코너여도 원인 코칭을 하지 않는다. 설정 손실 하한이 0 이하로 잡혀
  있어도(테스트·직접 호출) 지킨다.
- 브레이크 지점만 늦고 정점 최저속·라인·스로틀 재개·조향 수정이 기준과
  같거나 나으면 `brake_late`를 원인으로 고르지 않는다. 정점 속도 저하·
  라인 이탈·스로틀 지연·조향 수정·트랙 이탈이 동반되면 기존처럼 브레이크
  늦음을 코칭한다.

공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Iterable

import pytest

from f1re.coaching.coach import (
    CAUSE_BRAKE_LATE,
    CAUSE_LINE_WIDE,
    CAUSE_OFF_TRACK,
    Coach,
    CoachConfig,
    pick_cause,
)
from f1re.coaching.focus import FocusConfig, corner_loss_ms, lap_time_points
from f1re.coaching.metrics import corner_metrics
from f1re.coaching.reference import ReferenceSet, trace_outline
from f1re.config import CoachingSensitivityConfig
from f1re.demo_sender.driving import (
    DriverProfile,
    Sample as DrivingSample,
    generate_lap,
)
from f1re.demo_sender.scenarios import corner_effects, parse_scenarios
from f1re.laps.traces import LapTrace, Sample
from f1re.tracks.model import Corner, Track, load_outline, load_track

SUZUKA_ID = 13
MONACO_ID = 5
_BASE_NS = time.time_ns()

_SUZUKA = load_track(SUZUKA_ID)
_SUZUKA_OUTLINE = load_outline(SUZUKA_ID)
_MONACO = load_track(MONACO_ID)
_MONACO_OUTLINE = load_outline(MONACO_ID)
assert (
    _SUZUKA is not None
    and _SUZUKA_OUTLINE is not None
    and _MONACO is not None
    and _MONACO_OUTLINE is not None
)
SUZUKA: Track = _SUZUKA
MONACO: Track = _MONACO
CORNER9: Corner = next(corner for corner in SUZUKA.corners if corner.number == 9)

# 코너 손실 하한을 두지 않는 옛 민감도. 손실 0 이하 억제 자체를 확인하려고
# 설정 손실 하한이 이 규칙을 가리지 않게 한다.
_RELAXED_SENSITIVITY = CoachingSensitivityConfig(
    brake_diff_m=3.0,
    turn_in_diff_m=3.0,
    min_speed_diff_kmh=2.0,
    min_corner_loss_ms=-10_000,
)


# ---------------------------------------------------------------------------
# 도우미


def _corner(track: Track, number: int) -> Corner:
    return next(corner for corner in track.corners if corner.number == number)


def _outline(track: Track):
    return _SUZUKA_OUTLINE if track.track_id == SUZUKA_ID else _MONACO_OUTLINE


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
    track: Track,
    lap: int = 1,
    *,
    effects: dict | None = None,
    car_index: int = 0,
) -> LapTrace:
    samples = generate_lap(
        track,
        _outline(track),
        DriverProfile(pace=1.0, consistency=1.0),
        lap,
        random.Random(0),
        corner_effects=effects or {},
    )
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=True,
        invalid_reason=None,
        samples=[_to_sample(sample) for sample in samples],
    )


def _effects(track: Track, events: Iterable[dict]) -> dict:
    numbers = [corner.number for corner in track.corners]
    return corner_effects(parse_scenarios(list(events)), numbers)


def _late_brake_effect(
    track: Track, corner: int, meters: float = 10.0, width_m: float = 1.5
) -> dict:
    """데모 시나리오의 늦은 브레이크 효과(브레이크 지점·라인 이동)."""
    return _effects(
        track,
        [
            {
                "lap": 1,
                "corner": corner,
                "kind": "late_brake",
                "params": {"meters": meters, "width_m": width_m},
            }
        ],
    )


def _late_brake_input(trace: LapTrace, corner: Corner, meters: float) -> LapTrace:
    """브레이크 입력만 `meters`만큼 지워 브레이크 지점만 늦춘 랩.

    시각·속도·조향은 건드리지 않으므로 정점·탈출·라인 지표는 기준과 같다.
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
    """코너 진입~탈출 시간을 `ms`만큼 늘려 양의 코너 손실을 만든다."""
    span = max(1e-6, corner.exit_m - corner.entry_m)
    return dataclasses.replace(
        trace,
        samples=[
            dataclasses.replace(
                sample,
                t_ms=sample.t_ms
                + round(ms * min(1.0, (sample.dist_m - corner.entry_m) / span)),
            )
            if sample.dist_m > corner.entry_m
            else sample
            for sample in trace.samples
        ],
    )


def _slow_apex(trace: LapTrace, corner: Corner, kmh: float = 6.0) -> LapTrace:
    """코너 구간 속도를 `kmh`만큼 낮춰 정점 최저속 저하를 만든다."""
    return dataclasses.replace(
        trace,
        samples=[
            dataclasses.replace(sample, speed=max(0.0, sample.speed - kmh))
            if corner.entry_m <= sample.dist_m <= corner.exit_m
            else sample
            for sample in trace.samples
        ],
    )


def _with_surface(
    trace: LapTrace, surface: tuple[int, ...], corner: Corner
) -> LapTrace:
    """코너 구간 샘플에 바퀴별 노면을 넣는다."""
    return dataclasses.replace(
        trace,
        samples=[
            dataclasses.replace(sample, surface=surface)
            if corner.entry_m <= sample.dist_m <= corner.exit_m
            else sample
            for sample in trace.samples
        ],
    )


def _mark_traffic(trace: LapTrace, corner: Corner) -> LapTrace:
    """코너 구간 샘플에 접전 표시를 붙인다."""
    return dataclasses.replace(
        trace,
        samples=[
            dataclasses.replace(sample, in_traffic=True)
            if corner.entry_m <= sample.dist_m <= corner.exit_m
            else sample
            for sample in trace.samples
        ],
    )


def _baseline(track: Track) -> LapTrace:
    """코치가 기준으로 쓰는 다른 차(1번)의 깨끗한 랩."""
    return _trace(track, 99, car_index=1)


def _coach(
    track: Track,
    *,
    focus: Iterable[int] = (),
    sensitivity: CoachingSensitivityConfig | None = None,
    session_type: int | None = None,
) -> Coach:
    references = ReferenceSet(track.corners, session_type=session_type)
    references.update(_baseline(track))
    coach = Coach(
        track,
        references=references,
        config=CoachConfig(
            sensitivity=sensitivity or CoachingSensitivityConfig()
        ),
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
    base_ns = _BASE_NS + trace.lap * 200_000_000_000
    messages: list = []
    for sample in trace.samples:
        now_ns = base_ns + sample.t_ms * 1_000_000
        messages.extend(
            coach.on_sample(_state(sample, trace.lap), sample.dist_m, now_ns)
        )
    return messages


def _loss(trace: LapTrace, reference: LapTrace, corner: Corner) -> int:
    return corner_loss_ms(
        lap_time_points(trace), lap_time_points(reference), corner
    )


# ---------------------------------------------------------------------------
# 코너 손실이 0 이하인 집중 코너


def test_focused_corner_with_negative_loss_does_not_coach() -> None:
    """트랙 이탈 없이 손실이 음수(기준보다 빠름)면 집중 코너여도 말하지 않는다.

    모나코 10번 늦은 브레이크 데모는 손실이 음수이고 탈출 스로틀이 늦다.
    손실 하한을 없앤 민감도에서도 이 규칙이 지켜져야 한다.
    """
    coach = _coach(
        MONACO, focus=[10], sensitivity=_RELAXED_SENSITIVITY
    )
    corner10 = _corner(MONACO, 10)
    lap = _trace(
        MONACO, 1, effects=_late_brake_effect(MONACO, 10, meters=10.0)
    )

    assert _loss(lap, _baseline(MONACO), corner10) <= 0
    assert _feed(coach, lap) == []


def test_focused_corner_with_zero_loss_does_not_coach() -> None:
    """손실이 정확히 0인 집중 코너도 코칭하지 않는다(라인 이탈이어도).

    라인만 10m 밀린 랩은 원인 판정에서 line_wide가 나오지만, 시간을 잃지
    않았으므로 원인 코칭을 내지 않는다.
    """
    coach = _coach(SUZUKA, focus=[5], sensitivity=_RELAXED_SENSITIVITY)
    corner5 = _corner(SUZUKA, 5)
    wide = _effects(
        SUZUKA,
        [
            {
                "lap": 1,
                "corner": 5,
                "kind": "off_track",
                "params": {"width_m": 10.0},
            }
        ],
    )
    lap = _trace(SUZUKA, 1, effects=wide)

    reference = _baseline(SUZUKA)
    assert _loss(lap, reference, corner5) == 0
    ref_path = trace_outline(reference)
    cause = pick_cause(
        corner_metrics(lap, corner5, ref_path),
        corner_metrics(reference, corner5, ref_path),
        _RELAXED_SENSITIVITY,
    )
    assert cause is not None and cause.key == CAUSE_LINE_WIDE

    assert _feed(coach, lap) == []


def test_zero_loss_lap_resets_repeat_streak() -> None:
    """손실 0 이하로 억제된 랩은 연속 실수 횟수를 끊는다(SCRUM-144)."""
    coach = _coach(SUZUKA, focus=[9])
    normal = _slow_apex(
        _delay(
            _late_brake_input(_trace(SUZUKA, 1), CORNER9, 12.0),
            CORNER9,
            150.0,
        ),
        CORNER9,
    )

    first = _feed(coach, normal)
    second = _feed(coach, dataclasses.replace(normal, lap=2))
    assert first[0].evidence["streak"] == 1
    assert second[0].evidence["streak"] == 2

    # 시간 지연 없는 같은 랩은 손실이 0이라 코칭하지 않고 연속 횟수를 끊는다.
    quiet = _slow_apex(
        _late_brake_input(_trace(SUZUKA, 3), CORNER9, 12.0), CORNER9
    )
    assert _loss(
        quiet, _baseline(SUZUKA), CORNER9
    ) == 0
    assert _feed(coach, quiet) == []

    fourth = _feed(coach, dataclasses.replace(normal, lap=4))
    assert fourth[0].evidence["streak"] == 1


# ---------------------------------------------------------------------------
# 브레이크 지점만 늦은 경우(동반 악화 없음)


def test_only_late_brake_is_not_a_cause() -> None:
    """브레이크 지점만 늦고 정점·라인·스로틀·조향이 같으면 원인이 아니다."""
    reference = corner_metrics(_baseline(SUZUKA), CORNER9)
    assert reference.brake_start_m is not None
    late_only = dataclasses.replace(
        reference, brake_start_m=reference.brake_start_m + 12.0
    )

    assert pick_cause(late_only, reference) is None


def test_late_brake_with_time_loss_but_no_damage_is_not_coached() -> None:
    """손실이 있어도 브레이크 지점만 늦었고 코너를 깔끔히 돌았으면 무전이 없다."""
    coach = _coach(SUZUKA, focus=[9])
    lap = _delay(
        _late_brake_input(_trace(SUZUKA, 1), CORNER9, 12.0), CORNER9, 150.0
    )

    assert _loss(lap, _baseline(SUZUKA), CORNER9) >= 100
    assert _feed(coach, lap) == []


@pytest.mark.parametrize(
    ("damage", "expected_cause"),
    [
        ("min_speed", CAUSE_BRAKE_LATE),
        ("line_wide", CAUSE_BRAKE_LATE),
        ("late_throttle", CAUSE_BRAKE_LATE),
        ("throttle_missing", CAUSE_BRAKE_LATE),
        ("steer_corrections", CAUSE_BRAKE_LATE),
        ("off_track", CAUSE_OFF_TRACK),
    ],
)
def test_late_brake_with_companion_damage_keeps_cause(
    damage: str, expected_cause: str
) -> None:
    """늦은 브레이크에 에이펙스·탈출·라인·이탈 악화가 동반되면 기존대로 코칭한다."""
    reference = corner_metrics(_baseline(SUZUKA), CORNER9)
    assert reference.brake_start_m is not None
    late = dataclasses.replace(
        reference, brake_start_m=reference.brake_start_m + 12.0
    )

    if damage == "min_speed":
        late = dataclasses.replace(late, min_speed=reference.min_speed - 6.0)
    elif damage == "line_wide":
        late = dataclasses.replace(
            late, max_line_dev_m=reference.max_line_dev_m + 3.0
        )
    elif damage == "late_throttle":
        assert reference.throttle_on_m is not None
        late = dataclasses.replace(
            late, throttle_on_m=reference.throttle_on_m + 10.0
        )
    elif damage == "throttle_missing":
        assert reference.throttle_on_m is not None
        late = dataclasses.replace(late, throttle_on_m=None)
    elif damage == "steer_corrections":
        late = dataclasses.replace(
            late,
            steer_corrections=reference.steer_corrections
            + CoachingSensitivityConfig().steer_corrections,
        )
    else:
        late = dataclasses.replace(late, off_track=True)

    cause = pick_cause(late, reference)
    assert cause is not None and cause.key == expected_cause


def test_late_brake_with_apex_speed_drop_speaks() -> None:
    """늦은 브레이크로 정점 최저속이 떨어지면 '브레이크가 늦었어'를 말한다."""
    coach = _coach(SUZUKA, focus=[9])
    lap = _slow_apex(
        _delay(
            _late_brake_input(_trace(SUZUKA, 1), CORNER9, 12.0),
            CORNER9,
            150.0,
        ),
        CORNER9,
    )

    messages = _feed(coach, lap)

    assert len(messages) == 1
    message = messages[0]
    assert message.evidence["cause"] == CAUSE_BRAKE_LATE
    assert "브레이크가 늦었어" in message.text


def test_late_brake_with_off_track_speaks_even_without_loss() -> None:
    """늦은 브레이크 뒤 트랙을 벗어났으면 손실이 0이어도 이탈을 코칭한다.

    SCRUM-144의 손실 0 이하 억제는 트랙 이탈을 예외로 둔다. 다만 SCRUM-124의
    손실 하한은 그대로 적용되므로(기본 100ms), 이 테스트는 하한을 없앤
    민감도로 억제 예외 자체를 확인한다(기존 이탈 테스트와 같은 조건).
    """
    corner5 = _corner(SUZUKA, 5)
    coach = _coach(
        SUZUKA, focus=[5], sensitivity=_RELAXED_SENSITIVITY
    )
    lap = _with_surface(
        _late_brake_input(_trace(SUZUKA, 1), corner5, 12.0),
        (7, 7, 7, 7),
        corner5,
    )

    assert _loss(lap, _baseline(SUZUKA), corner5) == 0
    messages = _feed(coach, lap)

    assert len(messages) == 1
    assert messages[0].corner == 5
    assert messages[0].evidence["cause"] == CAUSE_OFF_TRACK


# ---------------------------------------------------------------------------
# 배틀·트래픽


def test_race_traffic_late_brake_is_not_coached() -> None:
    """레이스 접전 중 늦은 브레이킹은 내 실수가 아니므로 코칭하지 않는다."""
    coach = _coach(SUZUKA, focus=[9], session_type=15)
    lap = _slow_apex(
        _delay(
            _late_brake_input(_trace(SUZUKA, 2), CORNER9, 12.0),
            CORNER9,
            150.0,
        ),
        CORNER9,
    )
    lap = _mark_traffic(lap, CORNER9)

    assert _feed(coach, lap) == []
