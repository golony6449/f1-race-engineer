"""추월 리액션 무전 테스트(SCRUM-125).

레이스 전용, 1랩·SC·피트 제외, 단발 디바운스, 10초 창 안 2대 이상 묶음을
확인한다. 공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import pytest

from f1re.overtake.reaction import (
    BUNDLE_WINDOW_S,
    REACTION_DEBOUNCE_S,
    OvertakeReactionTracker,
    ReactionContext,
)
from f1re.radio.queue import RadioKind, RadioQueue
from f1re.radio.templates import TEMPLATES

NOW_NS = 1_000_000_000
SESSION_TYPE_RACE = 15


def _ctx(
    *,
    session_type: int | None = SESSION_TYPE_RACE,
    lap: int | None = 2,
    in_pit: bool = False,
    safety_car: bool = False,
    gap_s: float | None = 1.2,
    position: int | None = 8,
    total_laps: int | None = None,
) -> ReactionContext:
    return ReactionContext(
        session_type=session_type,
        lap=lap,
        in_pit=in_pit,
        safety_car=safety_car,
        gap_s=gap_s,
        position=position,
        total_laps=total_laps,
    )


def _choices(key: str, mood: str, **slots: object) -> list[str]:
    return [text.format(**slots) for text in TEMPLATES[key].texts_for(mood)]


def _seconds(seconds: float) -> int:
    return int(seconds * 10**9)


# ---------------------------------------------------------------------------
# 단발


def test_single_overtake_reacts_after_short_debounce() -> None:
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)

    # 바로 내지 않고 짧게 기다린다(두 번째 추월을 묶기 위해).
    assert tracker.observe(_ctx(), NOW_NS) == []
    assert tracker.observe(
        _ctx(), NOW_NS + _seconds(REACTION_DEBOUNCE_S - 0.1)
    ) == []

    messages = tracker.observe(
        _ctx(), NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1)
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.kind == RadioKind.OVERTAKE
    assert message.evidence["reaction"] == "single"
    assert message.evidence["count"] == 1
    assert message.evidence["mood"] == "calm"
    assert message.lap == 2
    assert message.text in _choices("overtake.reaction", "calm")
    assert tracker.pending_count == 0


def test_hyped_for_close_battle() -> None:
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)

    messages = tracker.observe(
        _ctx(gap_s=0.4), NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1)
    )

    assert len(messages) == 1
    assert messages[0].evidence["mood"] == "hyped"
    assert messages[0].text in _choices("overtake.reaction", "hyped")


def test_hyped_for_last_three_laps() -> None:
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)

    messages = tracker.observe(
        _ctx(lap=48, total_laps=50, gap_s=1.2),
        NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1),
    )

    assert len(messages) == 1
    assert messages[0].evidence["mood"] == "hyped"


# ---------------------------------------------------------------------------
# 묶음


def test_two_overtakes_in_window_bundle_into_one_message() -> None:
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)

    # 디바운스가 지나기 전에 두 번째 추월이 들어오면 단발 대신 묶음 하나만 낸다.
    second_ns = NOW_NS + _seconds(3.0)
    tracker.note_overtake(second_ns)
    messages = tracker.observe(_ctx(), second_ns)

    assert len(messages) == 1
    message = messages[0]
    assert message.evidence["reaction"] == "bundle"
    assert message.evidence["count"] == 2
    assert message.text in _choices(
        "overtake.reaction_bundle", "calm", count=2
    )
    assert "2대" in message.text
    assert tracker.pending_count == 0


def test_three_overtakes_in_window_count_three() -> None:
    tracker = OvertakeReactionTracker()
    for offset in (0.0, 2.0, 4.0):
        tracker.note_overtake(NOW_NS + _seconds(offset))

    messages = tracker.observe(_ctx(), NOW_NS + _seconds(4.0))

    assert len(messages) == 1
    assert messages[0].evidence["count"] == 3
    assert messages[0].text in _choices(
        "overtake.reaction_bundle", "calm", count=3
    )


def test_events_older_than_window_are_not_bundled() -> None:
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)
    first = tracker.observe(
        _ctx(), NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1)
    )
    assert len(first) == 1

    # 10초 창을 넘긴 뒤의 추월은 새 리액션으로 센다.
    later_ns = NOW_NS + _seconds(BUNDLE_WINDOW_S + 5.0)
    tracker.note_overtake(later_ns)
    messages = tracker.observe(
        _ctx(), later_ns + _seconds(REACTION_DEBOUNCE_S + 0.1)
    )

    assert len(messages) == 1
    assert messages[0].evidence["reaction"] == "single"


# ---------------------------------------------------------------------------
# 레이스 전용·상황 제외


@pytest.mark.parametrize("session_type", [1, 5, 10, 18])
def test_no_reaction_outside_race_sessions(session_type: int) -> None:
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)

    messages = tracker.observe(
        _ctx(session_type=session_type),
        NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1),
    )

    assert messages == []
    assert tracker.pending_count == 0


def test_no_reaction_on_lap_one() -> None:
    """스타트 혼전·초기 순위 할당 오탐을 피하려고 1랩은 반응하지 않는다."""
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)

    messages = tracker.observe(
        _ctx(lap=1), NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1)
    )

    assert messages == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"safety_car": True},
        {"in_pit": True},
    ],
)
def test_no_reaction_under_safety_car_or_in_pit(overrides: dict) -> None:
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)

    messages = tracker.observe(
        _ctx(**overrides), NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1)
    )

    assert messages == []


def test_pending_events_are_dropped_when_situation_becomes_unsafe() -> None:
    """SC가 지나간 뒤 예전 추월을 늦게 반응하지 않는다."""
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)

    assert tracker.observe(_ctx(safety_car=True), NOW_NS + _seconds(1.0)) == []

    # SC가 끝난 뒤에도 대기 사건은 지워져 있다.
    assert (
        tracker.observe(
            _ctx(), NOW_NS + _seconds(REACTION_DEBOUNCE_S + 1.0)
        )
        == []
    )


def test_lap_missing_does_not_react() -> None:
    tracker = OvertakeReactionTracker()
    tracker.note_overtake(NOW_NS)

    assert (
        tracker.observe(
            _ctx(lap=None), NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1)
        )
        == []
    )


# ---------------------------------------------------------------------------
# 무전 계층


def test_queue_receives_reaction_and_muting_suppresses_it() -> None:
    queue = RadioQueue(clock=lambda: NOW_NS)
    tracker = OvertakeReactionTracker(queue=queue)
    tracker.note_overtake(NOW_NS)
    messages = tracker.observe(
        _ctx(), NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1)
    )

    assert len(messages) == 1
    assert queue.pending_count == 1
    assert queue.pending()[0].kind == RadioKind.OVERTAKE

    muted = RadioQueue(clock=lambda: NOW_NS)
    assert muted.set_muted(RadioKind.OVERTAKE, True)
    muted_tracker = OvertakeReactionTracker(queue=muted)
    muted_tracker.note_overtake(NOW_NS)
    assert (
        muted_tracker.observe(
            _ctx(), NOW_NS + _seconds(REACTION_DEBOUNCE_S + 0.1)
        )
        == []
    )
    assert muted.pending_count == 0
