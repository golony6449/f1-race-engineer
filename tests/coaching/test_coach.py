"""코너 탈출 직후 코칭 무전 테스트(작업 16, SCRUM-66).

설계 6.4의 판단 규칙을 데모 송신기 주행 샘플로 확인한다. 공용 conftest를 만들지
않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Iterable

import pytest

from f1re.coaching.coach import (
    CAUSE_BRAKE_EARLY,
    CAUSE_BRAKE_LATE,
    CAUSE_LINE_WIDE,
    CAUSE_MIN_SPEED_LOW,
    CAUSE_OFF_TRACK,
    Coach,
    CoachConfig,
    defer_until_straight,
    pick_cause,
)
from f1re.coaching.focus import FocusConfig
from f1re.coaching.metrics import corner_metrics
from f1re.coaching.reference import SOURCE_RECENT_CAR, ReferenceSet
from f1re.config import CoachingSensitivityConfig
from f1re.demo_sender.driving import DriverProfile, Sample as DrivingSample, generate_lap
from f1re.demo_sender.scenarios import (
    SCENARIO_DIR,
    corner_effects,
    events_for_lap,
    load_scenarios,
    parse_scenarios,
)
from f1re.laps.traces import LapTrace, Sample
from f1re.radio.queue import RadioKind, RadioQueue
from f1re.radio.templates import render
from f1re.tracks.model import Corner, Track, load_outline, load_track

SUZUKA_ID = 13
MONACO_ID = 5
# 실제 시각 기준. RadioQueue에 넣는 무전의 유효 시간을 진짜 시계로 판정하기 때문이다.
_BASE_NS = time.time_ns()

# SCRUM-124로 기본 민감도(브레이크 8m, 손실 100ms 하한)가 바뀌었다. 데모 송신기
# 시나리오의 효과는 코너 손실이 0에 가깝거나 음수라 새 기본값에서는 무전이 나가지
# 않는다. 이 파일은 원인 선택·문구·발화 시점 규칙을 확인하므로, 그 규칙만 따로
# 확인하도록 옛 민감도를 명시한다. 새 기본값과 접전 코너 제외는
# tests/coaching/test_traffic_and_sensitivity.py가 확인한다.
_RELAXED_SENSITIVITY = CoachingSensitivityConfig(
    brake_diff_m=3.0,
    turn_in_diff_m=3.0,
    min_speed_diff_kmh=2.0,
    # 옛 동작처럼 손실 하한을 두지 않는다(빠른 코너도 원인 확인용으로 발화).
    min_corner_loss_ms=-10_000,
)

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


# ---------------------------------------------------------------------------
# 도우미


def _corner(track: Track, number: int) -> Corner:
    return next(corner for corner in track.corners if corner.number == number)


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
    outline,
    lap: int,
    *,
    effects: dict | None = None,
    pace: float = 1.0,
    car_index: int = 0,
    valid: bool = True,
) -> LapTrace:
    samples = generate_lap(
        track,
        outline,
        DriverProfile(pace=pace, consistency=1.0),
        lap,
        random.Random(0),
        corner_effects=effects or {},
    )
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=valid,
        invalid_reason=None,
        samples=[_to_sample(sample) for sample in samples],
    )


def _suzuka(lap: int, **kwargs) -> LapTrace:
    return _trace(SUZUKA, _SUZUKA_OUTLINE, lap, **kwargs)


def _monaco(lap: int, **kwargs) -> LapTrace:
    return _trace(MONACO, _MONACO_OUTLINE, lap, **kwargs)


def _with_surface(
    trace: LapTrace, surface: tuple[int, ...] | None, predicate
) -> LapTrace:
    """조건에 맞는 샘플에만 바퀴별 노면을 넣은 랩."""
    return dataclasses.replace(
        trace,
        samples=[
            dataclasses.replace(sample, surface=surface)
            if predicate(sample)
            else sample
            for sample in trace.samples
        ],
    )


def _effects(track: Track, events: Iterable[dict]) -> dict:
    numbers = [corner.number for corner in track.corners]
    return corner_effects(parse_scenarios(list(events)), numbers)


def _late_brake(track: Track, corner: int, meters: float = 10.0) -> dict:
    return _effects(
        track,
        [
            {
                "lap": 1,
                "corner": corner,
                "kind": "late_brake",
                "params": {"meters": meters, "width_m": 1.5},
            }
        ],
    )


def _early_brake(track: Track, corner: int, meters: float = 10.0) -> dict:
    return _effects(
        track,
        [
            {
                "lap": 1,
                "corner": corner,
                "kind": "early_brake",
                "params": {"meters": meters},
            }
        ],
    )


def _coach(
    track: Track, *, focus: Iterable[int] = (), outline=None, **kwargs
) -> Coach:
    references = ReferenceSet(track.corners)
    baseline_outline = outline or (
        _SUZUKA_OUTLINE if track.track_id == SUZUKA_ID else _MONACO_OUTLINE
    )
    # 기준 랩은 차마다 가장 최근 유효 랩이다(SCRUM-94). 내 차 랩과 비교할
    # 기준은 다른 차(1번)의 가장 최근 랩으로 둔다.
    baseline = _trace(track, baseline_outline, 99, car_index=1)
    references.update(baseline)
    # 시나리오 효과의 작은 차이를 그대로 보기 위해 옛 민감도를 명시한다.
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


def _state(sample: Sample, lap: int, **overrides) -> dict:
    state = {
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
    state.update(overrides)
    return state


def _feed(
    coach: Coach,
    trace: LapTrace,
    lap: int | None = None,
    *,
    overrides: dict | None = None,
    until_m: float | None = None,
    rate_ns: int = 1_000_000,
) -> list[tuple[float, object]]:
    """랩 샘플을 코치에 넣고 (랩 거리, 무전) 목록을 돌려준다."""
    resolved_lap = trace.lap if lap is None else lap
    base_ns = _BASE_NS + resolved_lap * 200_000_000_000
    collected: list[tuple[float, object]] = []
    for sample in trace.samples:
        if until_m is not None and sample.dist_m > until_m:
            break
        state = _state(sample, resolved_lap, **(overrides or {}))
        now_ns = base_ns + sample.t_ms * rate_ns
        for message in coach.on_sample(state, sample.dist_m, now_ns):
            collected.append((sample.dist_m, message))
    return collected


def _messages(events: list[tuple[float, object]]) -> list:
    return [message for _dist, message in events]


# ---------------------------------------------------------------------------
# 언제 말하나


def test_message_only_after_focus_corner_exit() -> None:
    """집중 코너(9번) 탈출 지점을 지나기 전에는 말하지 않는다."""
    coach = _coach(SUZUKA, focus=[9])
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))
    corner = _corner(SUZUKA, 9)

    events = _feed(coach, lap)

    assert events, "9번 탈출 뒤 코칭 무전이 나와야 한다"
    corner_messages = [
        (dist, message) for dist, message in events if message.corner == 9
    ]
    assert len(corner_messages) == 1
    dist, message = corner_messages[0]
    assert dist >= corner.exit_m
    assert message.text == "9번에서 브레이크를 너무 일찍 밟았어. 5미터 더 참아도 돼."
    assert message.kind == RadioKind.COACHING


def test_big_loss_non_focus_corner_speaks() -> None:
    """집중 코너가 아니어도 0.3초 이상 잃은 코너는 말한다."""
    coach = _coach(SUZUKA)  # 집중 코너 없음
    slow = _suzuka(1, pace=0.96)

    messages = _messages(_feed(coach, slow))

    assert len(messages) == 1
    message = messages[0]
    assert message.corner == 7
    assert "7번" in message.text and "코너 속도" in message.text
    assert message.evidence["cause"] == CAUSE_MIN_SPEED_LOW
    assert message.evidence["loss_ms"] >= 300


def test_off_track_non_focus_corner_speaks_immediately() -> None:
    """손실이 없어도 네 바퀴가 모두 트랙 밖이면 집중 코너가 아니어도 말한다."""
    coach = _coach(SUZUKA)
    corner = _corner(SUZUKA, 5)
    off_track = _with_surface(
        _suzuka(1),
        (7, 7, 7, 7),
        lambda sample: corner.entry_m <= sample.dist_m <= corner.exit_m,
    )

    messages = _messages(_feed(coach, off_track))

    assert len(messages) == 1
    message = messages[0]
    assert message.corner == 5
    assert message.evidence["cause"] == CAUSE_OFF_TRACK
    assert message.evidence["amount"] == 4
    assert message.evidence["unit"] == "count"
    # 미터 수치를 말하지 않는다(SCRUM-93).
    assert "미터" not in message.text


def test_off_track_templates_do_not_mention_meters() -> None:
    """트랙 이탈 무전 표현(모든 감정 단계)에는 거리 수치가 없다."""
    from f1re.radio.templates import all_phrases

    phrases = all_phrases("coaching.off_track", corner=5, count=4)
    phrases.extend(
        all_phrases("coaching.off_track_ordinal", corner_label="5번째 코너", count=4)
    )

    assert phrases
    for text in phrases:
        assert "미터" not in text
        assert "{meters}" not in text


def test_line_wide_still_judged_within_line_dev_cap() -> None:
    """상한(50 m) 안의 라인 이탈 10 m는 지금처럼 line_wide 원인이 된다."""
    coach = _coach(SUZUKA, focus=[5])
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
    lap = _suzuka(1, effects=wide)

    messages = _messages(_feed(coach, lap))

    assert len(messages) == 1
    message = messages[0]
    assert message.corner == 5
    assert message.evidence["cause"] == CAUSE_LINE_WIDE
    assert "10미터" in message.text


def test_coordinate_mismatch_makes_no_line_or_off_track_cause() -> None:
    """실제 세션 표본처럼 기준 궤적과 수백 m 떨어진 위치여도 라인·이탈을 말하지 않는다.

    2026-11-14 스즈카 세션에서 차 19의 Motion 좌표(-720.6, -153.8)는 같은 랩
    거리(4276 m)의 저장소 윤곽(-717.5, 170.0)과 324 m 달랐고, 노면은 네 바퀴
    모두 아스팔트(0)였다. 좌표계가 다른 기준 랩과의 라인 거리는 버린다.
    """
    # 차 19의 두 좌표 차이 벡터. 모든 샘플을 이만큼 옮겨 좌표계 불일치를 만든다.
    offset_x, offset_z = -720.6 - (-717.5), -153.8 - 170.0
    base = _suzuka(1)
    shifted = dataclasses.replace(
        base,
        samples=[
            dataclasses.replace(
                sample,
                x=sample.x + offset_x,
                z=sample.z + offset_z,
                surface=(0, 0, 0, 0),
            )
            for sample in base.samples
        ],
    )
    coach = _coach(SUZUKA, focus=[1, 5, 9])

    messages = _messages(_feed(coach, shifted))

    causes = {message.evidence["cause"] for message in messages}
    assert CAUSE_LINE_WIDE not in causes
    assert CAUSE_OFF_TRACK not in causes


def test_no_message_on_clean_lap() -> None:
    """기준 랩과 같은 랩은 집중 코너여도 아무 말도 하지 않는다."""
    coach = _coach(SUZUKA, focus=[3, 5, 9])

    assert _feed(coach, _suzuka(1)) == []


def test_no_message_when_cause_is_below_threshold() -> None:
    """조향 수정 1회처럼 문턱(2회)에 못 미치는 근거는 말하지 않는다."""
    coach = _coach(SUZUKA, focus=[9])
    barely = _suzuka(
        1,
        effects=_effects(
            SUZUKA,
            [
                {
                    "lap": 1,
                    "corner": 9,
                    "kind": "steer_corrections",
                    "params": {"count": 1, "amplitude": 0.18},
                }
            ],
        ),
    )

    messages = _messages(_feed(coach, barely))

    assert messages == []


def test_not_twice_same_lap() -> None:
    """같은 랩에 같은 코너 지적을 두 번 하지 않는다(거리 지터에도)."""
    coach = _coach(SUZUKA, focus=[9])
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))
    corner = _corner(SUZUKA, 9)
    base_ns = 200_000_000_000

    messages = []
    jittered = False
    for sample in lap.samples:
        now_ns = base_ns + sample.t_ms * 1_000_000
        messages.extend(
            coach.on_sample(_state(sample, 1), sample.dist_m, now_ns)
        )
        if not jittered and sample.dist_m >= corner.exit_m:
            # 탈출 지점을 살짝 되돌렸다가 다시 지나간다(플래시백 아님).
            jittered = True
            back = dataclasses.replace(sample, dist_m=sample.dist_m - 0.5)
            messages.extend(
                coach.on_sample(_state(back, 1), back.dist_m, now_ns + 1)
            )
            messages.extend(
                coach.on_sample(_state(sample, 1), sample.dist_m, now_ns + 2)
            )

    assert jittered
    assert len([message for message in messages if message.corner == 9]) == 1


# ---------------------------------------------------------------------------
# 코너 탈출 직후 즉시 발화(SCRUM-94)


def test_coaching_speaks_right_after_corner_exit() -> None:
    """모나코 10번(시케인) 코칭은 다음 직선까지 미루지 않고 탈출 직후 나간다."""
    coach = _coach(MONACO, focus=[10])
    lap = _monaco(1, effects=_late_brake(MONACO, 10, meters=10.0))
    corner10 = _corner(MONACO, 10)
    corner11 = _corner(MONACO, 11)

    events = _feed(coach, lap)

    assert events, "10번 탈출 직후 코칭 무전이 나와야 한다"
    dist, message = events[0]
    assert message.corner == 10
    assert "10번에서 브레이크가 늦었어." in message.text
    assert corner10.exit_m <= dist < corner11.entry_m


def _shift_to_game_frame(trace: LapTrace, offset_m: float) -> LapTrace:
    """중심선 프레임 랩을 실제 게임 lap_distance처럼 +offset 만큼 옮긴다."""
    return dataclasses.replace(
        trace,
        samples=[
            dataclasses.replace(sample, dist_m=sample.dist_m + offset_m)
            for sample in trace.samples
        ],
    )


def test_corner_coaching_uses_game_lap_distance_frame() -> None:
    """실제 게임 lap_distance(+130m)에서도 9번 탈출 뒤에 9번 코칭이 나간다."""
    shifted = SUZUKA.in_lap_distance_frame()
    offset = SUZUKA.lap_distance_offset_m
    references = ReferenceSet(shifted.corners)
    references.update(_shift_to_game_frame(_suzuka(99, car_index=1), offset))
    coach = Coach(
        shifted,
        references=references,
        config=CoachConfig(sensitivity=_RELAXED_SENSITIVITY),
        focus_config=FocusConfig(count=5, recent_laps=3),
    )
    coach.seed_focus([9])
    lap = _shift_to_game_frame(_suzuka(1, effects=_early_brake(SUZUKA, 9)), offset)

    events = _feed(coach, lap)

    corner_messages = [
        (dist, message) for dist, message in events if message.corner == 9
    ]
    assert len(corner_messages) == 1
    dist, message = corner_messages[0]
    corner9 = next(corner for corner in shifted.corners if corner.number == 9)
    assert dist >= corner9.exit_m
    assert "9번" in message.text


def test_coaching_message_in_queue_immediately() -> None:
    """큐를 주면 코너 탈출 샘플에서 곧바로 큐에 들어간다(미룸 없음)."""
    queue = RadioQueue(clock=lambda: _BASE_NS)
    coach = _coach(MONACO, focus=[10], queue=queue)
    lap = _monaco(1, effects=_late_brake(MONACO, 10, meters=10.0))
    corner10 = _corner(MONACO, 10)

    _feed_until(coach, lap, corner10.exit_m + 1.0)

    assert queue.pending_count == 1
    assert queue.pending()[0].corner == 10


def _feed_until(coach: Coach, lap: LapTrace, until_m: float) -> list:
    base_ns = _BASE_NS + lap.lap * 200_000_000_000
    collected: list = []
    for sample in lap.samples:
        if sample.dist_m > until_m:
            break
        collected.extend(
            coach.on_sample(
                _state(sample, lap.lap),
                sample.dist_m,
                base_ns + sample.t_ms * 1_000_000,
            )
        )
    return collected


def _feed_sample(coach: Coach, lap: LapTrace, sample: Sample) -> list:
    base_ns = _BASE_NS + lap.lap * 200_000_000_000
    return coach.on_sample(
        _state(sample, lap.lap), sample.dist_m, base_ns + sample.t_ms * 1_000_000
    )


def test_coaching_skipped_when_next_corner_already_entered() -> None:
    """연속 코너에서 다음 코너로 들어간 뒤엔 앞선 코너 코칭을 만들지 않는다."""
    coach = _coach(SUZUKA, focus=[3])
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 3))
    corner3 = _corner(SUZUKA, 3)
    corner5 = _corner(SUZUKA, 5)

    # 3번 탈출 직전까지 정상 주행한 뒤, 패킷이 끊겨 3·4번 탈출을 한 번에
    # 지나 5번 구간으로 들어온 샘플 하나를 넣는다.
    _feed_until(coach, lap, corner3.exit_m - 1.0)
    jumped = next(
        sample for sample in lap.samples if sample.dist_m >= corner5.entry_m + 10.0
    )

    messages = _feed_sample(coach, lap, jumped)

    assert [message.corner for message in messages if message.corner in {3, 4}] == []


def test_pending_coaching_discarded_when_next_corner_entered() -> None:
    """재생 전 옛 코칭은 다음 코너로 들어가면 버린다(SCRUM-143)."""
    dropped: list[tuple[int | None, str]] = []
    queue = RadioQueue(
        clock=lambda: _BASE_NS,
        on_drop=lambda message, reason: dropped.append((message.corner, reason)),
    )
    coach = _coach(SUZUKA, focus=[3], queue=queue)
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 3))
    corner3 = _corner(SUZUKA, 3)
    corner5 = _corner(SUZUKA, 5)

    _feed_until(coach, lap, corner3.exit_m + 1.0)
    assert [message.corner for message in queue.pending()] == [3]

    jumped = next(
        sample for sample in lap.samples if sample.dist_m >= corner5.entry_m + 10.0
    )
    _feed_sample(coach, lap, jumped)

    assert queue.pending_count == 0
    assert dropped == [(3, "stale")]


def test_defer_until_straight_helper() -> None:
    """미룸 판정 함수는 다음 코너 브레이크까지 남은 시간으로 결정한다."""
    corner10 = _corner(MONACO, 10)
    corner11 = _corner(MONACO, 11)
    corner12 = _corner(MONACO, 12)

    at_chicane = defer_until_straight(
        corner10.exit_m - 1.0,
        110.0,
        MONACO.corners,
        MONACO.length_m,
        brake_lead_m=40.0,
    )
    assert at_chicane.defer is True
    assert at_chicane.next_corner == 11

    on_straight = defer_until_straight(
        corner11.exit_m + 1.0,
        120.0,
        MONACO.corners,
        MONACO.length_m,
        brake_lead_m=40.0,
    )
    assert on_straight.defer is False
    assert on_straight.next_corner == 12
    assert on_straight.seconds_to_brake is not None
    assert on_straight.seconds_to_brake >= 3.0


def test_defer_wraps_after_last_corner() -> None:
    """마지막 코너 뒤에는 랩 끝을 넘어 첫 코너 브레이크까지의 시간을 쓴다."""
    decision = defer_until_straight(
        MONACO.length_m - 20.0,
        120.0,
        MONACO.corners,
        MONACO.length_m,
        brake_lead_m=0.0,
    )

    assert decision.next_corner == MONACO.corners[0].number
    assert decision.gap_m is not None and decision.gap_m > 0.0


# ---------------------------------------------------------------------------
# 무엇을 말하나


def test_single_cause_message_has_where_what_howmuch() -> None:
    """원인 한 가지 + 위치(코너) + 수치를 모두 갖춘 문장 하나만 나간다."""
    coach = _coach(SUZUKA, focus=[9])
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))

    messages = _messages(_feed(coach, lap))

    assert len(messages) == 1
    message = messages[0]
    assert message.text == "9번에서 브레이크를 너무 일찍 밟았어. 5미터 더 참아도 돼."
    evidence = message.evidence
    assert evidence["cause"] == CAUSE_BRAKE_EARLY
    assert evidence["amount"] == 5
    assert evidence["unit"] == "m"
    assert evidence["focus"] is True
    # 기준은 1번 차의 가장 최근 랩이다(SCRUM-94).
    assert evidence["reference"] == SOURCE_RECENT_CAR
    assert "metrics" in evidence and "reference_metrics" in evidence


def _expression_choices(key: str, mood: str, **slots) -> list[str]:
    from f1re.radio.templates import TEMPLATES

    return [
        text.format(**slots)
        for text in TEMPLATES[key].texts_for(mood)
    ]


def test_last_laps_coaching_uses_hyped_wording() -> None:
    """레이스 마지막 3랩이면 격앙된 표현(hyped)으로 말한다(SCRUM-91)."""
    coach = _coach(SUZUKA, focus=[9])
    lap = _suzuka(2, effects=_early_brake(SUZUKA, 9))

    messages = _messages(
        _feed(
            coach,
            lap,
            overrides={"session_type": 15, "remaining_laps": 3},
        )
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.evidence["mood"] == "hyped"
    assert message.text in _expression_choices(
        "coaching.brake_early", "hyped", corner=9, meters=5
    )


def test_time_trial_is_not_hyped_even_near_the_end() -> None:
    """타임 트라이얼은 남은 랩이 적어도 격앙 표현을 쓰지 않는다."""
    coach = _coach(SUZUKA, focus=[9])
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))

    messages = _messages(
        _feed(
            coach,
            lap,
            overrides={"session_type": 18, "remaining_laps": 1},
        )
    )

    assert len(messages) == 1
    assert messages[0].evidence["mood"] == "calm"


def test_late_brake_is_the_cause_when_line_also_wide() -> None:
    """브레이크가 늦고 라인이 밖으로 밀리면 '브레이크 늦음'을 원인으로 고른다."""
    coach = _coach(MONACO, focus=[10])
    lap = _monaco(1, effects=_late_brake(MONACO, 10, meters=10.0))

    messages = _messages(_feed(coach, lap))

    assert len(messages) == 1
    assert messages[0].evidence["cause"] == CAUSE_BRAKE_LATE
    assert "브레이크가 늦었어" in messages[0].text


def test_pick_cause_returns_none_when_all_differences_are_small() -> None:
    baseline = _suzuka(4)
    corner = _corner(SUZUKA, 3)
    metrics = corner_metrics(baseline, corner)
    reference = corner_metrics(baseline, corner)

    assert pick_cause(metrics, reference) is None


def test_ordinal_wording_for_unofficial_corners() -> None:
    """자동 탐지 코너(official=False)는 "9번째 코너"로 말한다(리뷰 포커스 5)."""
    corners = [
        dataclasses.replace(corner, official=False) if corner.number == 9 else corner
        for corner in SUZUKA.corners
    ]
    unofficial = dataclasses.replace(SUZUKA, corners=corners)
    coach = _coach(unofficial, focus=[9], outline=_SUZUKA_OUTLINE)
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))

    messages = _messages(_feed(coach, lap))

    assert len(messages) == 1
    assert messages[0].text == (
        "9번째 코너에서 브레이크를 너무 일찍 밟았어. 5미터 더 참아도 돼."
    )
    assert messages[0].evidence["corner_official"] is False


# ---------------------------------------------------------------------------
# 코칭하면 안 되는 랩 (리뷰 포커스 4)


@pytest.mark.parametrize(
    "overrides",
    [
        {"lap_valid": False},
        {"invalid_reason": "outlap"},
        {"in_pit": True},
        {"safety_car": True},
        {"vsc": True},
        {"driver_status": 3},  # 아웃랩
        {"driver_status": 5},  # 트랙 이탈
        {"pit_status": 1},  # 피트 진입
    ],
)
def test_invalid_lap_is_not_coached(overrides: dict) -> None:
    coach = _coach(SUZUKA, focus=[9])
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))

    assert _feed(coach, lap, overrides=overrides) == []


def test_race_lap_1_is_not_coached_but_lap_2_is() -> None:
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))

    first = _coach(SUZUKA, focus=[9], session_type=15)
    assert _feed(first, lap, overrides={"session_type": 15}) == []

    second = _coach(SUZUKA, focus=[9], session_type=15)
    later = _suzuka(2, effects=_early_brake(SUZUKA, 9))
    messages = _messages(_feed(second, later, overrides={"session_type": 15}))
    assert [message.corner for message in messages] == [9]


def test_other_car_samples_are_ignored() -> None:
    coach = _coach(SUZUKA, focus=[9], player_car_index=0)
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))

    assert _feed(coach, lap, overrides={"car_index": 3}) == []
    assert _feed(coach, lap, overrides={"is_player": False}) == []


def test_queue_receives_messages_and_muting_filters_them() -> None:
    """큐를 주면 만든 무전을 바로 넣고, 끈 종류는 반환·대기에 남지 않는다."""
    queue = RadioQueue()
    coach = _coach(SUZUKA, focus=[9], queue=queue)
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))
    corner9 = _corner(SUZUKA, 9)

    messages = _feed_until(coach, lap, corner9.exit_m + 1.0)

    assert len(messages) == 1
    assert queue.pending_count == 1

    muted_queue = RadioQueue()
    assert muted_queue.set_muted(RadioKind.COACHING, True)
    muted_coach = _coach(SUZUKA, focus=[9], queue=muted_queue)
    assert _feed(muted_coach, lap) == []
    assert muted_queue.pending_count == 0

    # 안전 경고는 끌 수 없다(설계 6.7).
    assert muted_queue.set_muted(RadioKind.SAFETY, True) is False


# ---------------------------------------------------------------------------
# 집중 코너 개선 알림


def test_focus_improved_message_announces_next_corner() -> None:
    """집중 코너를 고치면 "N번 좋아졌어. 다음은 M번 보자."로 한 번 알린다."""
    coach = _coach(SUZUKA)
    affected = (6, 5, 11, 3, 1, 10)
    for lap in (1, 2, 3):
        effects = _effects(
            SUZUKA,
            [
                {
                    "lap": lap,
                    "corner": number,
                    "kind": "early_brake",
                    "params": {"meters": 10.0},
                }
                for number in affected
            ],
        )
        assert coach.on_lap_completed(_suzuka(lap, effects=effects)) == []

    assert coach.focus_corners[0] == 6

    fixed_effects = _effects(
        SUZUKA,
        [
            {
                "lap": 4,
                "corner": number,
                "kind": "early_brake",
                "params": {"meters": 10.0},
            }
            for number in affected
            if number != 6
        ],
    )
    messages = coach.on_lap_completed(_suzuka(4, effects=fixed_effects))

    assert len(messages) == 1
    message = messages[0]
    assert message.kind == RadioKind.IMPROVEMENT
    assert message.corner == 6
    assert message.text == "오~ 6번 좋아졌어~ 그렇지~ 다음은 10번 보자."
    assert message.evidence["mood"] == "warm"
    assert 6 not in coach.focus_corners
    assert 10 in coach.focus_corners


def test_focus_improved_without_replacement_uses_short_message() -> None:
    coach = _coach(SUZUKA, focus=[3])
    coach.on_lap_completed(_suzuka(1, effects=_early_brake(SUZUKA, 3)))

    messages = coach.on_lap_completed(_suzuka(2))

    assert len(messages) == 1
    assert messages[0].text == "오~ 3번 좋아졌어~"


def test_improvement_uses_ordinal_wording_for_unofficial_corners() -> None:
    corners = [
        dataclasses.replace(corner, official=False) if corner.number == 3 else corner
        for corner in SUZUKA.corners
    ]
    unofficial = dataclasses.replace(SUZUKA, corners=corners)
    coach = _coach(unofficial, focus=[3], outline=_SUZUKA_OUTLINE)
    coach.on_lap_completed(_suzuka(1, effects=_early_brake(SUZUKA, 3)))

    messages = coach.on_lap_completed(_suzuka(2))

    assert len(messages) == 1
    assert messages[0].text == "오~ 3번째 코너 좋아졌어~"


def test_improvement_ttl_covers_main_straight_only() -> None:
    """개선 알림은 1번 코너까지(6초)만 유효하다(SCRUM-143)."""
    coach = _coach(SUZUKA, focus=[3])
    coach.on_lap_completed(_suzuka(1, effects=_early_brake(SUZUKA, 3)))

    messages = coach.on_lap_completed(_suzuka(2), now_ns=1_000)

    assert len(messages) == 1
    message = messages[0]
    assert message.created_ns == 1_000
    assert message.expires_ns - message.created_ns == 6 * 10**9


def test_only_one_focus_improvement_per_lap() -> None:
    """한 랩에 코너 여러 곳이 좋아져도 개선 알림은 하나만 낸다(SCRUM-143)."""
    coach = _coach(SUZUKA, focus=[3, 5])
    bad = _effects(
        SUZUKA,
        [
            {
                "lap": 1,
                "corner": number,
                "kind": "early_brake",
                "params": {"meters": 10.0},
            }
            for number in (3, 5)
        ],
    )
    coach.on_lap_completed(_suzuka(1, effects=bad))
    expected = coach.focus_corners[0]

    messages = coach.on_lap_completed(_suzuka(2))

    assert len(messages) == 1
    # 이전 집중 코너 순서(손실이 큰 순)의 첫 코너만 알린다.
    assert messages[0].corner == expected


def test_improvement_dropped_when_first_corner_already_entered() -> None:
    """1번 코너 진입 뒤까지 남은 개선 알림은 버린다(SCRUM-143)."""
    dropped: list[tuple[str, str]] = []
    queue = RadioQueue(
        on_drop=lambda message, reason: dropped.append((message.kind, reason))
    )
    coach = _coach(SUZUKA, focus=[3], queue=queue)
    coach.on_lap_completed(_suzuka(1, effects=_early_brake(SUZUKA, 3)))
    messages = coach.on_lap_completed(_suzuka(2))
    assert len(messages) == 1
    assert queue.pending_count == 1

    # 다음 랩에서 1번 코너 구간(중심선 510m)으로 들어왔다.
    sample = _suzuka(3).samples[0]
    coach.on_sample(_state(sample, 3), _corner(SUZUKA, 1).entry_m + 10.0, _BASE_NS)

    assert queue.pending_count == 0
    assert dropped == [(RadioKind.IMPROVEMENT, "stale")]


def test_improvement_not_emitted_when_lap_processing_is_late() -> None:
    """다음 랩에서 이미 1번 코너를 지난 뒤 완료 랩을 처리하면 내지 않는다."""
    coach = _coach(SUZUKA, focus=[3])
    coach.on_lap_completed(_suzuka(1, effects=_early_brake(SUZUKA, 3)))

    # 완료 랩 처리 전에 코치가 이미 다음 랩 1번 코너를 지난 샘플을 받았다.
    later = _suzuka(3)
    sample = next(
        sample
        for sample in later.samples
        if sample.dist_m >= _corner(SUZUKA, 1).entry_m + 10.0
    )
    coach.on_sample(_state(sample, 3), sample.dist_m, _BASE_NS)

    assert coach.on_lap_completed(_suzuka(2)) == []
    # 집중 코너 갱신은 그대로 반영된다.
    assert 3 not in coach.focus_corners


# ---------------------------------------------------------------------------
# 지연·미리 만들기


def test_decision_latency_under_100ms() -> None:
    """코너 탈출 샘플에서 무전을 결정하기까지 100ms 안에 끝난다."""
    coach = _coach(SUZUKA, focus=[9])
    lap = _suzuka(1, effects=_early_brake(SUZUKA, 9))
    corner = _corner(SUZUKA, 9)
    base_ns = 200_000_000_000

    elapsed = None
    for sample in lap.samples:
        if sample.dist_m < corner.exit_m - 3.0:
            coach.on_sample(_state(sample, 1), sample.dist_m, base_ns + sample.t_ms * 1_000_000)
            continue
        started = time.perf_counter()
        messages = coach.on_sample(
            _state(sample, 1), sample.dist_m, base_ns + sample.t_ms * 1_000_000
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        if messages:
            break

    assert elapsed is not None
    assert elapsed < 100.0, f"코너 탈출 → 무전 결정 {elapsed:.1f}ms"


class _FakeCache:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    async def prefetch(self, texts, voice=None):
        self.calls.append((list(texts), voice))
        return {"requested": len(list(texts))}


async def test_prefetch_covers_track_phrases() -> None:
    """세션 시작 prefetch에 그 서킷 코너 문장과 고정 경고가 들어간다."""
    coach = _coach(SUZUKA, focus=[9])
    cache = _FakeCache()

    await coach.prefetch(cache, "Katerina")

    assert len(cache.calls) == 1
    texts, voice = cache.calls[0]
    assert voice == "Katerina"
    assert "3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아." in texts
    assert "9번 코너 속도가 기준보다 5킬로 느려. 더 빠르게 돌 수 있어." in texts
    assert "3번 좋아졌어." in texts
    assert render("safety.safety_car") in texts
    # 개선 알림의 긴 문장(다음 코너 포함)과 추월 리액션·칭찬 고정 문구도
    # 미리 만든다(SCRUM-141).
    assert render("improvement.corner_improved", corner=3, next_corner=4) in texts
    assert render("overtake.reaction") in texts
    assert render("overtake.reaction_bundle", count=2) in texts
    assert render("praise.clean_lap") in texts
    assert len(texts) == len(set(texts))


async def test_prefetch_uses_ordinal_phrases_for_unofficial_corners() -> None:
    corners = [
        dataclasses.replace(corner, official=False) for corner in SUZUKA.corners
    ]
    coach = _coach(dataclasses.replace(SUZUKA, corners=corners))
    cache = _FakeCache()

    await coach.prefetch(cache)

    texts, voice = cache.calls[0]
    assert voice is None
    assert "3번째 코너에서 브레이크가 늦었어. 5미터 더 일찍 밟아." in texts
    # 비공식 코너도 다음 코너까지 "N번째 코너"로 말하는 긴 개선 문장을 만든다.
    assert (
        "3번째 코너 좋아졌어. 다음은 4번째 코너 보자." in texts
    )


# ---------------------------------------------------------------------------
# 데모 시나리오 리플레이


def _replay_demo(
    track: Track, outline, laps: int = 5, *, focus: Iterable[int] = ()
) -> list[tuple[int, object]]:
    """coaching.yaml 시나리오를 랩 1부터 돌려 (랩, 무전) 목록을 만든다."""
    coach = _coach(track, focus=focus)
    events = load_scenarios(SCENARIO_DIR / "coaching.yaml")
    numbers = [corner.number for corner in track.corners]
    emitted: list[tuple[int, object]] = []
    for lap in range(1, laps + 1):
        effects = corner_effects(events_for_lap(events, lap), numbers)
        trace = _trace(track, outline, lap, effects=effects)
        for _dist, message in _feed(coach, trace):
            emitted.append((lap, message))
        base_ns = (lap + 1) * 200_000_000_000
        for message in coach.on_lap_completed(
            trace, now_ns=base_ns + trace.samples[-1].t_ms * 1_000_000
        ):
            emitted.append((lap, message))
    return emitted


@pytest.mark.parametrize(
    ("track", "outline"),
    [(SUZUKA, _SUZUKA_OUTLINE), (MONACO, _MONACO_OUTLINE)],
)
def test_demo_coaching_scenario_emits_readable_messages(track: Track, outline) -> None:
    """스즈카·모나코 coaching 시나리오 리플레이에서 나간 무전이 말투 규칙을 지킨다."""
    emitted = _replay_demo(track, outline, laps=4, focus=(3, 5, 7, 9))

    assert emitted, "리플레이에서 코칭 무전이 나와야 한다"
    for _lap, message in emitted:
        assert message.kind in {RadioKind.COACHING, RadioKind.IMPROVEMENT}
        # calm은 마침표, warm은 물결표, 급박·격앙은 느낌표로 끝난다.
        assert message.text[-1] in ".~!"
        assert message.text.strip()
        # 무전은 짧은 반말 문장이다. 길면 말이 늦게 끝나 운전을 방해한다.
        assert len(message.text) <= 60
        assert message.evidence
        assert message.evidence["mood"] in {"calm", "hyped", "warm"}
