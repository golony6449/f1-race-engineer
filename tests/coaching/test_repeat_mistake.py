"""반복 실수 강조 무전 테스트(SCRUM-125).

같은 코너·같은 원인이 3랩 연속이면 일반 코칭 대신 강한 어조의 반복 실수
강조 틀을 쓰고, 다른 원인이 나오거나 코너를 건너뛰면 연속 횟수가 끊기는지
확인한다. 공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Iterable

import pytest

from f1re.coaching.coach import REPEAT_STREAK_LAPS, Coach, CoachConfig
from f1re.coaching.focus import FocusConfig
from f1re.coaching.reference import ReferenceSet
from f1re.config import CoachingSensitivityConfig
from f1re.demo_sender.driving import DriverProfile, Sample as DrivingSample, generate_lap
from f1re.demo_sender.scenarios import corner_effects, parse_scenarios
from f1re.laps.traces import LapTrace, Sample
from f1re.radio.templates import TEMPLATES
from f1re.tracks.model import Corner, Track, load_outline, load_track

SUZUKA_ID = 13
_BASE_NS = time.time_ns()

_SUZUKA = load_track(SUZUKA_ID)
_SUZUKA_OUTLINE = load_outline(SUZUKA_ID)
assert _SUZUKA is not None and _SUZUKA_OUTLINE is not None
SUZUKA: Track = _SUZUKA
OUTLINE = _SUZUKA_OUTLINE

# 반복 실수 규칙 자체를 확인하기 위해 옛 민감도·손실 하한을 쓴다. 데모 효과는
# 코너 손실이 0에 가까워 SCRUM-124 기본값(브레이크 8m, 100ms 하한)에서는 무전이
# 나가지 않는다. 새 기본값·접전 제외는 test_traffic_and_sensitivity.py가 확인한다.
_RELAXED_SENSITIVITY = CoachingSensitivityConfig(
    brake_diff_m=3.0,
    turn_in_diff_m=3.0,
    min_speed_diff_kmh=2.0,
    # 옛 동작처럼 손실 하한을 두지 않는다(빠른 코너도 원인 확인용으로 발화).
    min_corner_loss_ms=-10_000,
)


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


def _trace(track: Track, lap: int, *, effects: dict | None = None) -> LapTrace:
    samples = generate_lap(
        track,
        OUTLINE,
        DriverProfile(pace=1.0, consistency=1.0),
        lap,
        random.Random(0),
        corner_effects=effects or {},
    )
    return LapTrace(
        car_index=0,
        lap=lap,
        valid=True,
        invalid_reason=None,
        samples=[_to_sample(sample) for sample in samples],
    )


def _effects(track: Track, events: Iterable[dict]) -> dict:
    numbers = [corner.number for corner in track.corners]
    return corner_effects(parse_scenarios(list(events)), numbers)


def _early_brake(corner: int, meters: float = 10.0) -> dict:
    return _effects(
        SUZUKA,
        [
            {
                "lap": 1,
                "corner": corner,
                "kind": "early_brake",
                "params": {"meters": meters},
            }
        ],
    )


def _late_brake(corner: int, meters: float = 10.0) -> dict:
    return _effects(
        SUZUKA,
        [
            {
                "lap": 1,
                "corner": corner,
                "kind": "late_brake",
                "params": {"meters": meters, "width_m": 1.5},
            }
        ],
    )


def _delay_corner(trace: LapTrace, corner_number: int, ms: float) -> LapTrace:
    """코너 진입~탈출 시간을 `ms`만큼 늘려 양의 코너 손실을 만든다.

    SCRUM-144부터 트랙 이탈 없이 손실이 0 이하인 코너는 집중 코너여도
    코칭하지 않는다. 데모의 늦은 브레이크 시나리오는 손실이 음수라, 원인이
    바뀌는지 확인하려면 이 도우미로 발화 조건을 넘겨야 한다.
    """
    corner = next(c for c in SUZUKA.corners if c.number == corner_number)
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


def _coach(track: Track = SUZUKA, *, focus: Iterable[int] = (9,), **kwargs) -> Coach:
    references = ReferenceSet(track.corners)
    # 기준은 다른 차(1번)의 가장 최근 랩이다(SCRUM-94).
    references.update(_trace(track, 99))
    kwargs.setdefault("config", CoachConfig(sensitivity=_RELAXED_SENSITIVITY))
    coach = Coach(
        track,
        references=references,
        focus_config=FocusConfig(count=5, recent_laps=3),
        **kwargs,
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


def _feed(
    coach: Coach,
    trace: LapTrace,
    *,
    overrides: dict | None = None,
) -> list:
    """랩 샘플을 코치에 넣고 그 랩에서 나간 무전을 돌려준다."""
    base_ns = _BASE_NS + trace.lap * 200_000_000_000
    messages: list = []
    for sample in trace.samples:
        state = _state(sample, trace.lap)
        if overrides:
            state.update(overrides)
        now_ns = base_ns + sample.t_ms * 1_000_000
        messages.extend(coach.on_sample(state, sample.dist_m, now_ns))
    return messages


def _choices(key: str, mood: str, **slots: object) -> list[str]:
    return [text.format(**slots) for text in TEMPLATES[key].texts_for(mood)]


# ---------------------------------------------------------------------------
# 3랩 연속 강조


def test_third_consecutive_same_cause_uses_repeat_template() -> None:
    coach = _coach()
    effects = _early_brake(9)

    first = _feed(coach, _trace(SUZUKA, 1, effects=effects))
    second = _feed(coach, _trace(SUZUKA, 2, effects=effects))
    third = _feed(coach, _trace(SUZUKA, 3, effects=effects))

    assert len(first) == 1 and first[0].evidence["streak"] == 1
    assert first[0].evidence["repeated"] is False
    assert len(second) == 1 and second[0].evidence["streak"] == 2
    assert second[0].evidence["repeated"] is False

    assert len(third) == 1
    message = third[0]
    assert message.evidence["streak"] == REPEAT_STREAK_LAPS
    assert message.evidence["repeated"] is True
    assert message.evidence["mood"] == "urgent"
    assert "3랩째" in message.text
    assert message.text in _choices(
        "coaching.repeat_brake_early",
        "urgent",
        corner_label="9번",
        lap_streak=3,
        meters=message.evidence["amount"],
    )


def test_fourth_lap_keeps_emphasizing_with_streak_four() -> None:
    """4랩째도 같은 원인이면 다시 강조한다(연속 횟수는 계속 센다)."""
    coach = _coach()
    effects = _early_brake(9)
    for lap in (1, 2, 3):
        _feed(coach, _trace(SUZUKA, lap, effects=effects))

    fourth = _feed(coach, _trace(SUZUKA, 4, effects=effects))

    assert len(fourth) == 1
    message = fourth[0]
    assert message.evidence["streak"] == 4
    assert message.evidence["repeated"] is True
    assert message.evidence["mood"] == "urgent"
    assert "4랩째" in message.text


def test_different_cause_resets_streak() -> None:
    """같은 코너라도 원인이 바뀌면 연속 횟수를 1부터 다시 센다."""
    coach = _coach()
    _feed(coach, _trace(SUZUKA, 1, effects=_early_brake(9)))
    _feed(coach, _trace(SUZUKA, 2, effects=_early_brake(9)))

    third = _feed(
        coach,
        _delay_corner(
            _trace(SUZUKA, 3, effects=_late_brake(9)), 9, 150.0
        ),
    )

    assert len(third) == 1
    message = third[0]
    assert message.evidence["cause"] == "brake_late"
    assert message.evidence["streak"] == 1
    assert message.evidence["repeated"] is False
    assert message.evidence["mood"] == "calm"
    assert "3랩째" not in message.text


def test_improved_lap_resets_streak() -> None:
    """코너가 개선돼 코칭이 나가지 않으면 연속 횟수가 끊긴다."""
    coach = _coach()
    _feed(coach, _trace(SUZUKA, 1, effects=_early_brake(9)))
    _feed(coach, _trace(SUZUKA, 2, effects=_early_brake(9)))

    relaxed = _feed(coach, _trace(SUZUKA, 3))
    assert relaxed == []

    again = _feed(coach, _trace(SUZUKA, 4, effects=_early_brake(9)))
    assert len(again) == 1
    assert again[0].evidence["streak"] == 1
    assert again[0].evidence["repeated"] is False


def test_skipped_lap_resets_streak() -> None:
    """랩을 한 번 건너뛰면(3랩을 넘기고 5랩) 연속으로 보지 않는다."""
    coach = _coach()
    _feed(coach, _trace(SUZUKA, 1, effects=_early_brake(9)))
    _feed(coach, _trace(SUZUKA, 2, effects=_early_brake(9)))

    # 3랩을 코치에 넣지 않고 4랩을 넣는다.
    fourth = _feed(coach, _trace(SUZUKA, 4, effects=_early_brake(9)))

    assert len(fourth) == 1
    assert fourth[0].evidence["streak"] == 1
    assert fourth[0].evidence["repeated"] is False


def test_repeat_uses_ordinal_label_for_unofficial_corner() -> None:
    """자동 탐지 코너는 반복 강조에서도 "9번째 코너"로 말한다."""
    import dataclasses

    corners = [
        dataclasses.replace(corner, official=False) if corner.number == 9 else corner
        for corner in SUZUKA.corners
    ]
    unofficial = dataclasses.replace(SUZUKA, corners=corners)
    coach = _coach(unofficial)
    effects = _early_brake(9)
    for lap in (1, 2, 3):
        messages = _feed(coach, _trace(SUZUKA, lap, effects=effects))

    message = messages[0]
    assert message.evidence["corner_official"] is False
    assert "9번째 코너" in message.text
    assert message.text in _choices(
        "coaching.repeat_brake_early",
        "urgent",
        corner_label="9번째 코너",
        lap_streak=3,
        meters=message.evidence["amount"],
    )


def test_repeat_in_last_laps_uses_hyped_wording() -> None:
    """레이스 마지막 3랩이면 반복 강조도 격앙 표현(hyped)을 쓴다."""
    coach = _coach(session_type=15)
    effects = _early_brake(9)
    overrides = {"session_type": 15, "remaining_laps": 3}
    for lap in (2, 3, 4):
        messages = _feed(
            coach,
            _trace(SUZUKA, lap, effects=effects),
            overrides=overrides,
        )

    message = messages[0]
    assert message.evidence["streak"] == 3
    assert message.evidence["mood"] == "hyped"
    assert message.text in _choices(
        "coaching.repeat_brake_early",
        "hyped",
        corner_label="9번",
        lap_streak=3,
        meters=message.evidence["amount"],
    )


@pytest.mark.parametrize(
    "cause_key",
    [
        "coaching.repeat_brake_late",
        "coaching.repeat_brake_early",
        "coaching.repeat_turn_in_early",
        "coaching.repeat_turn_in_late",
        "coaching.repeat_steer_corrections",
        "coaching.repeat_min_speed_low",
        "coaching.repeat_late_throttle",
        "coaching.repeat_line_wide",
        "coaching.repeat_off_track",
    ],
)
def test_repeat_templates_exist_in_coaching_kind(cause_key: str) -> None:
    from f1re.radio.queue import RadioKind

    assert TEMPLATES[cause_key].category is RadioKind.COACHING
    assert "lap_streak" in TEMPLATES[cause_key].text
