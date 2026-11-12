"""상태 변화·Event 패킷에서 만드는 사건과 그 큐.

사건은 파이프라인 뒤쪽(랩 기록·코칭·경고)이 놓치지 않도록 `EventQueue`에
쌓인다. `StateStore.apply`가 만든 사건을 큐에 넣고 같은 목록을 돌려주므로,
호출자는 반환값을 바로 쓰거나 `EventQueue.drain()`으로 모아 갈 수 있다.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    """사건 종류. 값은 JSON·로그에 그대로 쓰는 소문자 이름이다."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    LAP_COMPLETE = "lap_complete"
    PIT_ENTER = "pit_enter"
    PIT_EXIT = "pit_exit"
    OVERTAKE = "overtake"
    PENALTY = "penalty"
    SAFETY_CAR = "safety_car"
    VSC = "vsc"
    RED_FLAG = "red_flag"
    WEATHER_CHANGE = "weather_change"
    FLASHBACK = "flashback"
    SESSION_RESTART = "session_restart"


@dataclass(frozen=True)
class Event:
    """사건 하나. `data`는 JSON으로 만들 수 있는 값만 담는다."""

    kind: EventKind
    car_index: int | None = None
    lap: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


class EventQueue:
    """여러 스레드가 넣고 한 곳에서 비워 가는 사건 큐."""

    def __init__(self) -> None:
        self._items: deque[Event] = deque()
        self._lock = threading.Lock()

    def push(self, event: Event) -> None:
        with self._lock:
            self._items.append(event)

    def drain(self) -> list[Event]:
        """쌓인 사건을 모두 꺼낸다. 꺼낸 뒤 큐는 비어 있다."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
