"""무전 한 줄 큐: 우선순위, 유효 시간, 끊기, 종류별 끄기.

설계 6.7의 전달 계층이다. 모든 무전은 이 큐 하나를 지나며, 재생기는
`next()`로 가장 높은 우선순위의 무전을 꺼내 말한다.

- 우선순위: 안전·위험 경고 > 박스 지시 > 코너 코칭 > 추월 > 개선 알림.
- 말하는 중에 더 높은 우선순위 무전이 오면 `interrupt_if_higher()`가 True를
  돌려준다. 재생기는 지금 소리를 끊고 `complete()` 뒤 `next()`로 새 무전을 꺼낸다.
- 낮은 우선순위 무전은 대기하고, 유효 시간이 지나면 `next()`가 버린다.
- 안전 경고는 끌 수 없다(`set_muted`가 False를 돌려주고 아무것도 바꾸지 않는다).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class Priority(IntEnum):
    """작을수록 먼저 나간다. 설계 6.7의 순서 그대로다."""

    SAFETY = 0
    BOX = 1
    COACHING = 2
    OVERTAKE = 3
    IMPROVEMENT = 4


class RadioKind(StrEnum):
    """무전 종류. 대시보드의 끄기 단위이자 템플릿 분류다."""

    SAFETY = "safety"
    BOX = "box"
    COACHING = "coaching"
    OVERTAKE = "overtake"
    IMPROVEMENT = "improvement"


PRIORITY_BY_KIND: dict[str, Priority] = {
    RadioKind.SAFETY: Priority.SAFETY,
    RadioKind.BOX: Priority.BOX,
    RadioKind.COACHING: Priority.COACHING,
    RadioKind.OVERTAKE: Priority.OVERTAKE,
    RadioKind.IMPROVEMENT: Priority.IMPROVEMENT,
}

# 끌 수 있는 종류. 안전 경고는 설계 6.7(마스터 판단)에 따라 뺄 수 없다.
MUTABLE_KINDS = frozenset(RadioKind) - {RadioKind.SAFETY}


@dataclass(frozen=True)
class RadioMessage:
    """무전 한 줄.

    `evidence`에는 그 문장을 말하게 된 근거 수치를 담는다(예:
    `{"meters": 5.2, "brake_start_m": 510.0}`). 무전 기록이 그대로 저장한다.
    `created_ns`·`lap`·`corner`는 기록 화면용이며 없으면 0/None이다.
    """

    kind: str
    priority: Priority
    text: str
    expires_ns: int
    evidence: dict[str, Any] = field(default_factory=dict)
    created_ns: int = 0
    lap: int | None = None
    corner: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("무전 종류가 비어 있습니다")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("무전 문장이 비어 있습니다")
        # IntEnum이 아닌 정수도 받아 비교가 어긋나지 않게 맞춘다.
        object.__setattr__(self, "priority", Priority(self.priority))

    def expired(self, now_ns: int) -> bool:
        """`now_ns`에 이미 유효 시간이 지났는가."""
        return now_ns >= self.expires_ns

    @classmethod
    def create(
        cls,
        kind: str,
        text: str,
        *,
        now_ns: int,
        ttl_ns: int,
        evidence: dict[str, Any] | None = None,
        lap: int | None = None,
        corner: int | None = None,
    ) -> RadioMessage:
        """종류로 우선순위를 정하고 유효 시간을 `now_ns + ttl_ns`로 채운다."""
        try:
            priority = PRIORITY_BY_KIND[RadioKind(kind)]
        except ValueError as exc:
            raise ValueError(f"모르는 무전 종류: {kind}") from exc
        if ttl_ns < 0:
            raise ValueError("ttl_ns는 0 이상이어야 합니다")
        return cls(
            kind=str(kind),
            priority=priority,
            text=text,
            expires_ns=now_ns + ttl_ns,
            evidence=dict(evidence or {}),
            created_ns=now_ns,
            lap=lap,
            corner=corner,
        )


@dataclass(frozen=True)
class _Entry:
    seq: int
    msg: RadioMessage


class RadioQueue:
    """여러 스레드가 넣고 재생기 한 곳이 꺼내 가는 무전 큐."""

    def __init__(self, *, clock: Callable[[], int] = time.time_ns) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: list[_Entry] = []
        self._next_seq = 0
        self._muted: set[str] = set()
        self._current: RadioMessage | None = None

    # --- 넣기 -------------------------------------------------------------

    def push(self, msg: RadioMessage) -> bool:
        """큐에 넣는다. 끈 종류거나 이미 유효 시간이 지났으면 False."""
        with self._lock:
            if msg.kind in self._muted:
                return False
            if msg.expired(self._clock()):
                return False
            self._pending.append(_Entry(self._next_seq, msg))
            self._next_seq += 1
            self._pending.sort(key=self._order_key)
            return True

    # --- 꺼내기 -----------------------------------------------------------

    def next(self, now_ns: int) -> RadioMessage | None:
        """가장 높은 우선순위의 유효한 무전을 꺼내 '재생 중'으로 표시한다.

        같은 우선순위면 먼저 넣은 것이 먼저 나간다(FIFO). 유효 시간이 지난
        무전은 꺼내지 않고 버린다.
        """
        with self._lock:
            self._drop_expired(now_ns)
            if not self._pending:
                return None
            msg = self._pending.pop(0).msg
            self._current = msg
            return msg

    def complete(self) -> None:
        """지금 재생 중인 무전이 끝났음을 알린다(끊겼을 때도 부른다)."""
        with self._lock:
            self._current = None

    def interrupt_if_higher(self, msg: RadioMessage) -> bool:
        """`msg`가 지금 재생 중인 무전보다 우선순위가 높은가.

        True면 재생기는 소리를 끊고 `complete()`한 뒤, 이 무전을 `push()`하고
        `next()`로 꺼내 말한다. 재생 중인 무전이 없거나, 끈 종류이거나, 유효
        시간이 지난 무전이면 False다. 같은 우선순위는 끊지 않는다.
        """
        with self._lock:
            if msg.kind in self._muted:
                return False
            if msg.expired(self._clock()):
                return False
            if self._current is None:
                return False
            return msg.priority < self._current.priority

    # --- 끄기 -------------------------------------------------------------

    def set_muted(self, kind: str, muted: bool) -> bool:
        """종류 하나를 켜거나 끈다. 안전 경고는 무시하고 False를 돌려준다."""
        try:
            resolved = RadioKind(kind)
        except ValueError as exc:
            raise ValueError(f"모르는 무전 종류: {kind}") from exc
        if resolved not in MUTABLE_KINDS:
            return False
        with self._lock:
            if muted:
                self._muted.add(resolved.value)
                # 이미 대기 중인 같은 종류도 내보내지 않는다.
                self._pending = [
                    entry for entry in self._pending if entry.msg.kind != resolved.value
                ]
            else:
                self._muted.discard(resolved.value)
            return True

    def is_muted(self, kind: str) -> bool:
        with self._lock:
            return kind in self._muted

    # --- 조회 -------------------------------------------------------------

    @property
    def muted_kinds(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._muted)

    @property
    def current(self) -> RadioMessage | None:
        with self._lock:
            return self._current

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def pending(self) -> tuple[RadioMessage, ...]:
        """대기 중인 무전을 우선순위 순서로 돌려준다(진단·테스트용)."""
        with self._lock:
            return tuple(entry.msg for entry in self._pending)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._current = None

    # --- 내부 -------------------------------------------------------------

    @staticmethod
    def _order_key(entry: _Entry) -> tuple[int, int]:
        return (int(entry.msg.priority), entry.seq)

    def _drop_expired(self, now_ns: int) -> None:
        self._pending = [
            entry for entry in self._pending if not entry.msg.expired(now_ns)
        ]

    def __iter__(self) -> Iterator[RadioMessage]:
        return iter(self.pending())
