"""무전 큐: 우선순위, 유효 시간, 끊기, 종류별 끄기."""

from __future__ import annotations

import pytest

from f1re.radio.queue import (
    COACHING_TTL_NS,
    DROP_REASON_EXPIRED,
    DROP_REASON_SUPERSEDED,
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
    # 코칭은 새 무전이 오면 옛 무전을 버리므로(SCRUM-94) 같은 우선순위의
    # 다른 종류로 FIFO를 확인한다.
    queue = RadioQueue()
    queue.push(_msg(RadioKind.IMPROVEMENT, "첫째", created_ns=1))
    queue.push(_msg(RadioKind.IMPROVEMENT, "둘째", created_ns=2))

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
        "3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아.",
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


# ---------------------------------------------------------------------------
# 코칭 5초 유효 시간과 새 코칭 우선(SCRUM-94)


def test_coaching_ttl_is_capped_to_five_seconds() -> None:
    """코칭 무전은 만든 시각부터 5초를 넘기지 않는다. 다른 종류는 그대로다."""
    coaching = RadioMessage.create(
        RadioKind.COACHING, "3번에서 브레이크가 늦었어.", now_ns=1_000, ttl_ns=60 * 10**9
    )
    assert COACHING_TTL_NS == 5 * 10**9
    assert coaching.expires_ns == 1_000 + COACHING_TTL_NS

    safety = RadioMessage.create(
        RadioKind.SAFETY, "경고", now_ns=1_000, ttl_ns=60 * 10**9
    )
    assert safety.expires_ns == 1_000 + 60 * 10**9


def test_coaching_expires_after_five_seconds() -> None:
    """5초 안에 말을 시작하지 못한 코칭은 버리고 이유를 남긴다."""
    dropped: list[tuple[str, str]] = []
    queue = RadioQueue(
        clock=lambda: 0,
        on_drop=lambda msg, reason: dropped.append((msg.text, reason)),
    )
    message = RadioMessage.create(
        RadioKind.COACHING, "코칭", now_ns=0, ttl_ns=60 * 10**9
    )
    assert queue.push(message) is True

    assert queue.next(COACHING_TTL_NS - 1) is not None
    queue.complete()
    assert queue.next(COACHING_TTL_NS) is None
    assert dropped == []

    queue.push(
        RadioMessage.create(RadioKind.COACHING, "곧 만료", now_ns=0, ttl_ns=60 * 10**9)
    )
    assert queue.next(COACHING_TTL_NS) is None
    assert dropped == [("곧 만료", DROP_REASON_EXPIRED)]


def test_new_coaching_drops_pending_coaching_only() -> None:
    """새 코칭이 오면 대기 중인 옛 코칭만 버리고, 다른 종류는 남긴다."""
    dropped: list[tuple[str, str]] = []
    queue = RadioQueue(on_drop=lambda msg, reason: dropped.append((msg.text, reason)))
    queue.push(_msg(RadioKind.COACHING, "옛 코칭"))
    queue.push(_msg(RadioKind.SAFETY, "경고"))
    queue.push(_msg(RadioKind.BOX, "박스"))
    queue.push(_msg(RadioKind.OVERTAKE, "추월"))
    queue.push(_msg(RadioKind.IMPROVEMENT, "개선"))

    assert queue.push(_msg(RadioKind.COACHING, "새 코칭")) is True

    assert [msg.text for msg in queue.pending()] == [
        "경고",
        "박스",
        "새 코칭",
        "추월",
        "개선",
    ]
    assert dropped == [("옛 코칭", DROP_REASON_SUPERSEDED)]
    assert DROP_REASON_SUPERSEDED == "superseded"


def test_new_coaching_does_not_cut_current_message() -> None:
    """재생 중인 무전은 끊지 않고, 대기 중인 옛 코칭만 버린다."""
    dropped: list[str] = []
    queue = RadioQueue(on_drop=lambda msg, reason: dropped.append(msg.text))
    queue.push(_msg(RadioKind.COACHING, "재생 중"))
    assert queue.next(1).text == "재생 중"
    queue.push(_msg(RadioKind.COACHING, "대기 중이던 코칭"))

    assert queue.push(_msg(RadioKind.COACHING, "새 코칭")) is True

    assert queue.current is not None and queue.current.text == "재생 중"
    assert [msg.text for msg in queue.pending()] == ["새 코칭"]
    assert dropped == ["대기 중이던 코칭"]


def test_non_coaching_push_keeps_pending_coaching() -> None:
    queue = RadioQueue()
    queue.push(_msg(RadioKind.COACHING, "코칭"))
    assert queue.push(_msg(RadioKind.SAFETY, "경고")) is True

    assert [msg.text for msg in queue.pending()] == ["경고", "코칭"]


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


def test_expired_messages_are_reported_to_drop_handler() -> None:
    """만료로 버린 무전은 이유와 함께 처리기에 알린다(SCRUM-81)."""
    dropped: list[tuple[str, str]] = []
    queue = RadioQueue(
        clock=lambda: 0,
        on_drop=lambda msg, reason: dropped.append((msg.text, reason)),
    )
    queue.push(_msg(RadioKind.COACHING, "곧 만료", expires_ns=100))
    queue.push(_msg(RadioKind.SAFETY, "아직 유효", expires_ns=FAR))

    assert queue.next(now_ns=100).text == "아직 유효"
    queue.complete()
    assert queue.next(now_ns=100) is None

    assert dropped == [("곧 만료", DROP_REASON_EXPIRED)]
    assert DROP_REASON_EXPIRED == "expired"


def test_push_reports_already_expired_message_but_not_muted() -> None:
    dropped: list[tuple[str, str]] = []
    queue = RadioQueue(on_drop=lambda msg, reason: dropped.append((msg.text, reason)))
    queue.set_muted(RadioKind.COACHING, True)

    assert queue.push(_msg(RadioKind.COACHING, "끈 코칭", expires_ns=1)) is False
    assert dropped == []

    assert queue.push(_msg(RadioKind.SAFETY, "지난 경고", expires_ns=1)) is False
    assert dropped == [("지난 경고", DROP_REASON_EXPIRED)]


def test_drop_handler_can_be_removed() -> None:
    dropped: list[str] = []
    queue = RadioQueue()
    queue.set_drop_handler(lambda msg, reason: dropped.append(msg.text))
    queue.set_drop_handler(None)

    queue.push(_msg(RadioKind.COACHING, "곧 만료", expires_ns=1))
    assert queue.next(now_ns=1) is None
    assert dropped == []


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
