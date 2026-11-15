"""추월 리액션 무전(SCRUM-125) — 레이스 전용.

추월 안내(`advisor.py`, 설계 6.5)와 달리 **이미 넘어선 직후** 짧게 반응한다.

무엇으로 판정하나
-----------------
F1 25 Event 패킷의 `OVTK`(overtakingVehicleIdx)만 센다. 게임이 실제 추월
순간에 보내는 사건이므로, 순위 값만 보고 판단할 때 생기는 오탐(스타트 초기
순위 할당, 피트 인/아웃, 리타이어로 인한 순위 상승)을 피할 수 있다.

언제 말하지 않나
----------------
- 레이스(15~17)가 아닌 세션(연습·예선·타임 트라이얼).
- 1랩(스타트 혼전). 2랩부터 반응한다.
- SC·VSC 중, 피트 레인 안(`pit_status` 1·2, `driver_status` 2·3·7).
- 무전 종류 `overtake`를 대시보드에서 끈 경우.

묶음 규칙
---------
- 10초 창 안에 2대 이상 넘으면 "한 번에 N대" 묶음 무전 1건으로 낸다.
- 단발은 `REACTION_DEBOUNCE_S`(2.5초)만 기다렸다 낸다. 그 안에 다음 추월이
  들어오면 단발 대신 묶음으로 합친다. 리액션은 늦으면 쓸모가 없으므로 10초를
  꽉 기다리지 않는다. 창(10초)을 넘긴 추월은 새 리액션으로 센다.
- 한 번 낸 묶음/단발은 대기 목록에서 지워, 다음 추월이 새로 시작된다.

감정 단계
---------
마지막 3랩이거나 앞차와 0.5초 이내 접전이면 `hyped`, 그 밖에는 `calm`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from f1re.radio.queue import RadioKind, RadioMessage, RadioQueue
from f1re.radio.templates import VariantRotator, is_last_laps, mood_for
from f1re.race_distance import remaining_race_laps
from f1re.udp.session_category import is_race_session

# 여러 추월을 한 무전으로 묶는 시간 창(초).
BUNDLE_WINDOW_S = 10.0
# 단발 리액션을 내보내기 전에 다음 추월을 기다리는 시간(초).
REACTION_DEBOUNCE_S = 2.5
# 추월 리액션 무전 유효 시간(초).
REACTION_TTL_S = 10.0
# 0.5초 이내 접전은 격앙 표현(hyped)을 쓴다(기존 추월 안내와 같은 값).
CLOSE_BATTLE_GAP_S = 0.5


@dataclass(frozen=True)
class ReactionContext:
    """추월 리액션을 낼지 판단하는 현재 상태.

    `lap`은 내 Lap Data의 현재 랩 번호다. `in_pit`은 피트 레인 안(진입·인랩·
    아웃랩 포함), `safety_car`는 SC·VSC 중인지다.
    """

    session_type: int | None = None
    lap: int | None = None
    in_pit: bool = False
    safety_car: bool = False
    gap_s: float | None = None
    position: int | None = None
    total_laps: int | None = None


class OvertakeReactionTracker:
    """OVTK 사건을 모아 추월 리액션 무전을 만든다.

    `note_overtake()`는 Event 패킷을 받을 때마다, `observe()`는 10Hz 판단
    주기마다 부른다. `queue`를 주면 만든 무전을 바로 넣는다(끈 종류는 큐가
    거른다). 세션이 바뀌면 새로 만들거나 `reset()`을 부른다.
    """

    def __init__(
        self,
        *,
        queue: RadioQueue | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._queue = queue
        self._clock = clock
        self._variants = VariantRotator()
        self._events_ns: list[int] = []

    # --- 조회 -------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        """아직 내보내지 않은 추월 사건 수(진단·테스트용)."""
        return len(self._events_ns)

    def reset(self) -> None:
        self._events_ns.clear()
        self._variants.reset()

    # --- 사건 -------------------------------------------------------------

    def note_overtake(self, now_ns: int | None = None) -> None:
        """내가 앞차를 넘었다는 OVTK 사건을 기록한다(내보내지는 않는다)."""
        now = int(now_ns) if now_ns is not None else int(self._clock())
        self._events_ns.append(now)
        # observe()가 오래 멈춰도 목록이 무한히 자라지 않게 한다.
        if len(self._events_ns) > 32:
            del self._events_ns[:-32]

    def observe(
        self,
        context: ReactionContext | None = None,
        now_ns: int | None = None,
    ) -> list[RadioMessage]:
        """지금 내보낼 리액션이 있으면 무전 하나를 돌려준다."""
        now = int(now_ns) if now_ns is not None else int(self._clock())
        ctx = context or ReactionContext()
        if not _eligible(ctx):
            # SC·피트·1랩·비레이스에서는 반응하지 않는다. 대기 중이던 사건도
            # 상황이 바뀐 뒤 늦게 나가지 않게 지운다.
            self._events_ns.clear()
            return []

        window_ns = int(BUNDLE_WINDOW_S * 10**9)
        self._events_ns = [
            stamp for stamp in self._events_ns if now - stamp <= window_ns
        ]
        if not self._events_ns:
            return []

        count = len(self._events_ns)
        if count < 2:
            debounce_ns = int(REACTION_DEBOUNCE_S * 10**9)
            if now - self._events_ns[0] < debounce_ns:
                return []
        return self._emit(ctx, count, now)

    # --- 무전 만들기 -------------------------------------------------------

    def _emit(
        self, context: ReactionContext, count: int, now_ns: int
    ) -> list[RadioMessage]:
        self._events_ns.clear()
        remaining = remaining_race_laps(
            context.session_type, context.total_laps, context.lap
        )
        gap = context.gap_s
        mood = mood_for(
            last_laps=is_last_laps(remaining),
            close_battle=gap is not None and gap <= CLOSE_BATTLE_GAP_S,
        )
        if count >= 2:
            key = "overtake.reaction_bundle"
            slots: dict[str, Any] = {"count": count}
        else:
            key = "overtake.reaction"
            slots = {}
        text = self._variants.render(key, mood=mood, **slots)
        evidence: dict[str, Any] = {
            "reaction": "bundle" if count >= 2 else "single",
            "count": count,
            "window_s": BUNDLE_WINDOW_S,
            "debounce_s": REACTION_DEBOUNCE_S,
            "session_type": context.session_type,
            "position": context.position,
            "gap_s": None if gap is None else round(gap, 3),
            "mood": mood.value,
        }
        message = RadioMessage.create(
            RadioKind.OVERTAKE.value,
            text,
            now_ns=now_ns,
            ttl_ns=int(REACTION_TTL_S * 10**9),
            evidence=evidence,
            lap=context.lap,
        )
        if self._queue is not None and not self._queue.push(message):
            return []
        return [message]


def _eligible(context: ReactionContext) -> bool:
    """레이스 2랩 이후, 피트·SC가 아닐 때만 리액션한다."""
    if not is_race_session(context.session_type):
        return False
    if context.lap is None or context.lap < 2:
        return False
    if context.in_pit or context.safety_car:
        return False
    return True


__all__ = [
    "BUNDLE_WINDOW_S",
    "CLOSE_BATTLE_GAP_S",
    "REACTION_DEBOUNCE_S",
    "REACTION_TTL_S",
    "OvertakeReactionTracker",
    "ReactionContext",
]
