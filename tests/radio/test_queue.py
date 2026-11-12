"""무전 큐: 우선순위, 유효 시간, 끊기, 종류별 끄기."""

from __future__ import annotations

import pytest

from f1re.radio.queue import (
    PRIORITY_BY_KIND,
    Priority,
    RadioKind,
    RadioMessage,
    RadioQueue,
)

FAR = 10**30


def _msg(
    kind: str,
    text: str,
    *,
    priority: Priority | None = None,
    expires_ns: int = FAR,
    created_ns: int = 0,
    evidence: dict[str, object] | None = None,
    lap: int | None = None,
    corner: int | None = None,
) -> RadioMessage:
    return RadioMessage(
        kind=kind,
        priority=priority if priority is not None else PRIORITY_BY_KIND[RadioKind(kind)],
        text=text,
        expires_ns=expires_ns,
        evidence=evidence or {},
        created_ns=created_ns,
        lap=lap,
        corner=corner,
    )


def test_priority_order() -> None:
    queue = RadioQueue()
    queue.push(_msg(RadioKind.IMPROVEMENT, "개선"))
    queue.push(_msg(RadioKind.OVERTAKE, "추월"))
    queue.push(_msg(RadioKind.SAFETY, "경고"))
    queue.push(_msg(RadioKind.COACHING, "코칭"))
    queue.push(_msg(RadioKind.BOX, "박스"))

    order: list[str] = []
    while (message := queue.next(now_ns=1)) is not None:
        order.append(message.kind)
        queue.complete()

    assert order == ["safety", "box", "coaching", "overtake", "improvement"]


def test_same_priority_is_fifo() -> None:
    queue = RadioQueue()
    queue.push(_msg(RadioKind.COACHING, "첫째", created_ns=1))
    queue.push(_msg(RadioKind.COACHING, "둘째", created_ns=2))

    assert queue.next(1).text == "첫째"
    queue.complete()
    assert queue.next(1).text == "둘째"


def test_push_rejects_expired_message() -> None:
    queue = RadioQueue()
    assert queue.push(_msg(RadioKind.COACHING, "지난 말", expires_ns=1)) is False
    assert queue.next(now_ns=2) is None
    assert queue.pending_count == 0


def test_expired_messages_dropped() -> None:
    queue = RadioQueue()
    queue.push(_msg(RadioKind.COACHING, "곧 만료", expires_ns=100))
    queue.push(_msg(RadioKind.SAFETY, "아직 유효", expires_ns=FAR))

    assert queue.next(now_ns=100).text == "아직 유효"
    queue.complete()
    assert queue.next(now_ns=100) is None


def test_higher_priority_interrupts() -> None:
    queue = RadioQueue()
    queue.push(_msg(RadioKind.COACHING, "코칭"))
    assert queue.next(1).text == "코칭"

    safety = _msg(RadioKind.SAFETY, "경고")
    assert queue.interrupt_if_higher(safety) is True
    queue.complete()
    queue.push(safety)
    assert queue.next(1).text == "경고"


@pytest.mark.parametrize("kind", [RadioKind.COACHING, RadioKind.OVERTAKE, RadioKind.IMPROVEMENT])
def test_lower_or_equal_priority_does_not_interrupt(kind: str) -> None:
    queue = RadioQueue()
    queue.push(_msg(RadioKind.COACHING, "코칭"))
    assert queue.next(1) is not None

    assert queue.interrupt_if_higher(_msg(kind, "낮은 말")) is False


def test_interrupt_without_current_returns_false() -> None:
    queue = RadioQueue()
    assert queue.interrupt_if_higher(_msg(RadioKind.SAFETY, "경고")) is False


def test_interrupt_ignores_muted_or_expired_message() -> None:
    queue = RadioQueue()
    queue.push(_msg(RadioKind.COACHING, "코칭"))
    queue.next(1)
    queue.set_muted(RadioKind.OVERTAKE, True)

    assert queue.interrupt_if_higher(_msg(RadioKind.OVERTAKE, "추월")) is False
    assert queue.interrupt_if_higher(_msg(RadioKind.SAFETY, "경고", expires_ns=1)) is False


def test_safety_cannot_be_muted() -> None:
    queue = RadioQueue()

    assert queue.set_muted(RadioKind.SAFETY, True) is False
    assert queue.muted_kinds == frozenset()
    assert queue.push(_msg(RadioKind.SAFETY, "경고")) is True
    assert queue.next(1).text == "경고"


def test_muting_kind_blocks_push_and_drops_pending() -> None:
    queue = RadioQueue()
    queue.push(_msg(RadioKind.COACHING, "먼저 온 코칭"))

    assert queue.set_muted(RadioKind.COACHING, True) is True
    assert queue.is_muted(RadioKind.COACHING)
    assert queue.pending_count == 0
    assert queue.push(_msg(RadioKind.COACHING, "끈 코칭")) is False
    assert queue.next(1) is None


def test_unmute_accepts_messages_again() -> None:
    queue = RadioQueue()
    queue.set_muted(RadioKind.IMPROVEMENT, True)
    assert queue.push(_msg(RadioKind.IMPROVEMENT, "개선")) is False

    assert queue.set_muted(RadioKind.IMPROVEMENT, False) is True
    assert queue.push(_msg(RadioKind.IMPROVEMENT, "개선")) is True


def test_set_muted_rejects_unknown_kind() -> None:
    queue = RadioQueue()
    with pytest.raises(ValueError):
        queue.set_muted("telemetry", True)


def test_create_fills_priority_expiry_and_evidence() -> None:
    message = RadioMessage.create(
        RadioKind.COACHING,
        "3번, 브레이크 늦음. 5미터.",
        now_ns=1_000,
        ttl_ns=2_000_000_000,
        evidence={"meters": 5.2},
        lap=3,
        corner=3,
    )

    assert message.priority is Priority.COACHING
    assert message.expires_ns == 1_000 + 2_000_000_000
    assert message.created_ns == 1_000
    assert message.evidence == {"meters": 5.2}
    assert message.lap == 3
    assert message.corner == 3


def test_create_rejects_unknown_kind_and_negative_ttl() -> None:
    with pytest.raises(ValueError):
        RadioMessage.create("telemetry", "문장", now_ns=1, ttl_ns=1)
    with pytest.raises(ValueError):
        RadioMessage.create(RadioKind.SAFETY, "경고", now_ns=1, ttl_ns=-1)


def test_message_rejects_empty_text_and_accepts_plain_int_priority() -> None:
    with pytest.raises(ValueError):
        _msg(RadioKind.SAFETY, "   ")

    message = RadioMessage(
        kind=RadioKind.SAFETY,
        priority=0,  # type: ignore[arg-type]
        text="경고",
        expires_ns=FAR,
    )
    assert message.priority is Priority.SAFETY


def test_pending_and_clear() -> None:
    queue = RadioQueue()
    queue.push(_msg(RadioKind.IMPROVEMENT, "개선"))
    queue.push(_msg(RadioKind.SAFETY, "경고"))

    assert [m.text for m in queue.pending()] == ["경고", "개선"]
    assert queue.pending_count == 2

    queue.next(1)
    queue.clear()
    assert queue.pending_count == 0
    assert queue.current is None
    assert queue.next(1) is None
