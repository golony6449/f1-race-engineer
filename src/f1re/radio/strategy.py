"""서킷 기반 공격·방어 전략 무전 문장 생성(SCRUM-130).

`overtake/strategy.py`가 고른 `StrategyPlan`의 구조화된 사실(`facts`)과 그
지점의 준비·공격 코너 KB 섹션을 Qwen LLM 무전 계층(`RadioTextGenerator`)에
넘겨 짧은 한국어 무전을 만든다. LLM이 꺼져 있거나 타임아웃·오류·출력 검증
실패면 같은 계획의 템플릿 문장으로 대체한다.

- **LLM은 문장만 만든다.** 지점·코너·간격·전략은 규칙 엔진이 정하고, 그 값이
  `facts`로 프롬프트에 들어간다(`radio/llm.py`의 숫자·문자 검증을 그대로 탄다).
- **KB 문맥**: `TrackKB.render()`로 준비 코너와 공격 코너 섹션만 넣는다. KB가
  없는 트랙이면 빈 문자열이다.
- **비블로킹**: `announce()`의 await 안에서만 네트워크를 기다린다. 파이프라인은
  LLM을 쓸 때 `announce_all()`을 별도 asyncio 태스크로 띄우고, 템플릿만 쓸
  때는 `announce_now()`로 즉시 큐에 넣는다. 수신 루프·10Hz 판단 주기는 LLM
  응답을 기다리지 않는다.
- **키는 다루지 않는다.** `RadioTextGenerator`가 자기 설정으로 읽고, 실패는
  `LLMResult.used_fallback`/`fallback_reason`으로만 돌려준다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

from f1re.overtake.strategy import StrategyPlan
from f1re.radio.llm import LLMResult, RadioTextGenerator
from f1re.radio.queue import RadioKind, RadioMessage, RadioQueue
from f1re.radio.templates import (
    CLOSE_OVERTAKE_GAP_S,
    Mood,
    VariantRotator,
    is_last_laps,
    mood_for,
)
from f1re.tracks.kb import TrackKB

logger = logging.getLogger(__name__)

# LLM을 쓰지 않고 템플릿으로만 말했을 때 근거(evidence)에 남기는 이유.
FALLBACK_TEMPLATE = "template"


class StrategyNarrator:
    """공격·방어 계획을 한국어 무전 문장으로 바꿔 큐에 넣는다.

    `generator`가 없으면 템플릿 문장만 쓴다. `queue`를 주면 만든 무전을 바로
    `RadioQueue`에 넣고(끈 종류는 큐가 거른다), 없으면 결과만 돌려준다.
    """

    def __init__(
        self,
        generator: RadioTextGenerator | None = None,
        *,
        queue: RadioQueue | None = None,
        kb: TrackKB | None = None,
        variants: VariantRotator | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._generator = generator
        self._queue = queue
        self._kb = kb
        self._variants = variants or VariantRotator()
        self._clock = clock

    # --- 조회 -------------------------------------------------------------

    @property
    def uses_llm(self) -> bool:
        """LLM 생성기를 쓸 수 있는가(파이프라인이 태스크로 띄울지 결정)."""
        return self._generator is not None

    @property
    def kb(self) -> TrackKB | None:
        return self._kb

    # --- 문장 -------------------------------------------------------------

    def mood_for_plan(self, plan: StrategyPlan) -> Mood:
        """마지막 3랩·0.5초 이내 접전이면 격앙, 그 밖에는 calm."""
        return mood_for(
            last_laps=is_last_laps(plan.remaining_laps),
            close_battle=plan.gap_s <= CLOSE_OVERTAKE_GAP_S,
        )

    def fallback_text(
        self, plan: StrategyPlan, mood: Mood | str | None = None
    ) -> str:
        """LLM 없이 쓸 템플릿 문장(감정 단계별 표현 돌려 쓰기)."""
        resolved = self.mood_for_plan(plan) if mood is None else mood
        return self._variants.render(
            plan.template_key, mood=resolved, **self._slots(plan)
        )

    def kb_text(self, plan: StrategyPlan) -> str:
        """준비 코너 + 공격 코너 KB 섹션. KB가 없으면 빈 문자열."""
        if self._kb is None:
            return ""
        numbers = [*plan.spot.prep_corners, *plan.spot.attack_corners]
        return self._kb.render(numbers)

    async def announce(
        self, plan: StrategyPlan, *, now_ns: int | None = None
    ) -> RadioMessage | None:
        """계획 하나를 LLM(또는 템플릿)으로 말하고 큐에 넣는다.

        LLM 생성기는 예외를 올리지 않는 규약이지만, 다른 구현이 올려도 무전이
        사라지지 않게 여기서도 템플릿으로 대체한다.
        """
        now = int(now_ns) if now_ns is not None else int(self._clock())
        mood = self.mood_for_plan(plan)
        fallback = self.fallback_text(plan, mood)
        text = fallback
        used_fallback = True
        reason: str | None = FALLBACK_TEMPLATE

        if self._generator is not None:
            result: LLMResult | None = None
            try:
                result = await self._generator.generate(
                    plan.llm_kind,
                    plan.facts,
                    kb_text=self.kb_text(plan),
                    mood=mood,
                    fallback_text=fallback,
                )
            except Exception:  # 생성기 버그가 무전 전달을 막지 않게 한다.
                logger.exception("전략 무전 LLM 생성 중 오류(템플릿으로 대체)")
                result = None
            if result is not None:
                text = result.text
                used_fallback = result.used_fallback
                reason = result.fallback_reason
        return self._emit(plan, text, mood, now, used_fallback, reason)

    async def announce_all(
        self,
        plans: Sequence[StrategyPlan],
        *,
        now_ns: int | None = None,
    ) -> list[RadioMessage]:
        """계획 목록을 차례로 말한다. 큐가 거른 무전은 결과에서 뺀다."""
        messages: list[RadioMessage] = []
        for plan in plans:
            message = await self.announce(plan, now_ns=now_ns)
            if message is not None:
                messages.append(message)
        return messages

    def announce_now(
        self,
        plans: Sequence[StrategyPlan],
        *,
        now_ns: int | None = None,
    ) -> list[RadioMessage]:
        """LLM 없이 템플릿 문장으로 즉시 큐에 넣는다(동기 경로).

        파이프라인은 LLM 생성기가 없을 때 이 경로를 써서 10Hz 판단 주기 안에서
        바로 무전을 넣는다. 네트워크를 기다리지 않는다.
        """
        now = int(now_ns) if now_ns is not None else int(self._clock())
        messages: list[RadioMessage] = []
        for plan in plans:
            mood = self.mood_for_plan(plan)
            message = self._emit(
                plan,
                self.fallback_text(plan, mood),
                mood,
                now,
                True,
                FALLBACK_TEMPLATE,
            )
            if message is not None:
                messages.append(message)
        return messages

    # --- 내부 -------------------------------------------------------------

    @staticmethod
    def _slots(plan: StrategyPlan) -> dict[str, Any]:
        facts = plan.facts
        return {
            "spot": str(facts.get("spot") or plan.spot.name),
            "gap": f"{plan.gap_s:.1f}",
            "attack": str(facts.get("attack_corners_text") or ""),
            "prep": str(facts.get("prep_corners_text") or ""),
        }

    def _emit(
        self,
        plan: StrategyPlan,
        text: str,
        mood: Mood,
        now_ns: int,
        used_fallback: bool,
        reason: str | None,
    ) -> RadioMessage | None:
        evidence: dict[str, Any] = dict(plan.facts)
        evidence.update(
            {
                "strategy_side": plan.side,
                "template": plan.template_key,
                "score": round(plan.score, 3),
                "mood": mood.value,
                "llm_used_fallback": used_fallback,
                "llm_fallback_reason": reason,
            }
        )
        message = RadioMessage.create(
            RadioKind.OVERTAKE.value,
            text,
            now_ns=now_ns,
            ttl_ns=int(plan.ttl_s * 1_000_000_000),
            evidence=evidence,
            lap=plan.lap,
            corner=plan.corner,
        )
        # 대시보드에서 추월 무전을 끈 경우 큐가 거부한다(설계 6.7).
        if self._queue is not None and not self._queue.push(message):
            return None
        return message


__all__ = ["FALLBACK_TEMPLATE", "StrategyNarrator"]
