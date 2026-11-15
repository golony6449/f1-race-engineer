"""서킷 전략 무전 문장 생성 테스트(SCRUM-130).

규칙 엔진이 고른 계획(`StrategyPlan`)을 Qwen LLM 무전 계층(`RadioTextGenerator`)에
넘겨 문장을 받고, LLM 비활성·타임아웃·오류·검증 실패면 템플릿으로 대체되는지
확인한다. 실제 네트워크는 쓰지 않고 가짜 생성기만 쓴다.

공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import asyncio
from typing import Any

from f1re.overtake.strategy import StrategyPlan
from f1re.radio.llm import (
    FALLBACK_TIMEOUT,
    LLMResult,
    RadioTextGenerator,
)
from f1re.radio.queue import RadioQueue
from f1re.radio.strategy import FALLBACK_TEMPLATE, StrategyNarrator
from f1re.radio.templates import Mood, all_phrases, template_slots
from f1re.tracks.kb import load_track_kb
from f1re.tracks.model import OvertakingSpot

NOW_NS = 1_000_000_000
SUZUKA_ID = 13

SPOT = OvertakingSpot(
    name="카시오 시케인",
    attack_corners=(16, 17),
    difficulty="low",
    prep_corners=(14, 15),
    attack_method=(
        "백 스트레이트 슬립스트림으로 붙은 뒤 16번 인코스 늦은 브레이킹"
    ),
    defense_line=(
        "브레이킹 존 전에 우측 인코스를 커버하고 17번을 각지게 돌아 재추월을 막는다"
    ),
    drs=False,
    entry_window_m=(5090.0, 5140.0),
)


def _facts(side: str = "attack", gap: float = 0.6) -> dict[str, Any]:
    return {
        "side": side,
        "spot": SPOT.name,
        "difficulty": SPOT.difficulty,
        "attack_corners": [16, 17],
        "prep_corners": [14, 15],
        "attack_corners_text": "16번, 17번",
        "prep_corners_text": "14번, 15번",
        "attack_method": SPOT.attack_method,
        "defense_line": SPOT.defense_line,
        "gap_s": gap,
        "distance_to_attack_m": 140.0,
        "lap": 2,
        "total_laps": 20,
        "remaining_laps": 18.0,
        "session_type": 15,
        "track_id": SUZUKA_ID,
    }


def _plan(
    *,
    side: str = "attack",
    gap: float = 0.6,
    template_key: str | None = None,
    remaining_laps: float | None = 18.0,
) -> StrategyPlan:
    return StrategyPlan(
        side=side,
        spot=SPOT,
        score=1.2,
        gap_s=gap,
        lap=2,
        remaining_laps=remaining_laps,
        facts=_facts(side, gap),
        template_key=template_key
        or ("strategy.attack" if side == "attack" else "strategy.defense"),
        ttl_s=20.0,
        corner=16,
    )


class _StubGenerator:
    """네트워크 없이 `RadioTextGenerator`를 흉내 내는 가짜 생성기."""

    def __init__(
        self,
        *,
        text: str | None = "카시오 시케인 공격! 16번 인코스 늦게!",
        used_fallback: bool = False,
        fallback_reason: str | None = None,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.text = text
        self.used_fallback = used_fallback
        self.fallback_reason = fallback_reason
        self.error = error
        self.gate = gate
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def generate(
        self,
        kind: str,
        facts: Any,
        *,
        kb_text: str = "",
        mood: Mood | str = Mood.CALM,
        fallback_text: str,
    ) -> LLMResult:
        self.calls.append(
            {
                "kind": kind,
                "facts": facts,
                "kb_text": kb_text,
                "mood": mood,
                "fallback_text": fallback_text,
            }
        )
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return LLMResult(
            text=self.text if self.text is not None else fallback_text,
            used_fallback=self.used_fallback,
            fallback_reason=self.fallback_reason,
            latency_ms=1,
            model="stub",
        )

    async def aclose(self) -> None:
        self.closed = True


def _template_texts(key: str, **slots: object) -> set[str]:
    return set(all_phrases(key, **slots))


def _slots(plan: StrategyPlan) -> dict[str, Any]:
    facts = plan.facts
    return {
        "spot": facts["spot"],
        "gap": f"{plan.gap_s:.1f}",
        "attack": facts["attack_corners_text"],
        "prep": facts["prep_corners_text"],
    }


# ---------------------------------------------------------------------------
# LLM 정상 경로


async def test_llm_text_is_used_with_facts_and_kb() -> None:
    stub = _StubGenerator()
    queue = RadioQueue(clock=lambda: NOW_NS)
    kb = load_track_kb(SUZUKA_ID)
    narrator = StrategyNarrator(
        stub, queue=queue, kb=kb, clock=lambda: NOW_NS
    )
    plan = _plan()

    message = await narrator.announce(plan)

    assert message is not None
    assert message.text == "카시오 시케인 공격! 16번 인코스 늦게!"
    assert message.kind == "overtake"
    assert message.lap == 2
    assert message.corner == 16
    assert queue.pending_count == 1

    call = stub.calls[0]
    assert call["kind"] == "overtake.attack"
    assert call["facts"] == plan.facts
    assert call["mood"] is Mood.CALM
    # KB는 준비 코너·공격 코너 섹션만 넣는다.
    assert "## 코너 14:" in call["kb_text"]
    assert "## 코너 15:" in call["kb_text"]
    assert "## 코너 16:" in call["kb_text"]
    assert "## 코너 17:" in call["kb_text"]
    assert "## 코너 1:" not in call["kb_text"]
    assert "## 코너 18:" not in call["kb_text"]

    evidence = message.evidence
    assert evidence["strategy_side"] == "attack"
    assert evidence["spot"] == "카시오 시케인"
    assert evidence["attack_corners"] == [16, 17]
    assert evidence["llm_used_fallback"] is False
    assert evidence["llm_fallback_reason"] is None


async def test_defense_plan_uses_defense_kind_and_template() -> None:
    stub = _StubGenerator(text="뒤차랑 0.8초. 카시오 인코스 커버!")
    narrator = StrategyNarrator(stub, clock=lambda: NOW_NS)
    plan = _plan(side="defense", gap=0.8)

    message = await narrator.announce(plan)

    assert message is not None
    assert stub.calls[0]["kind"] == "overtake.defense"
    assert message.evidence["strategy_side"] == "defense"
    assert message.text == "뒤차랑 0.8초. 카시오 인코스 커버!"


# ---------------------------------------------------------------------------
# 템플릿 폴백


async def test_template_fallback_without_generator() -> None:
    queue = RadioQueue(clock=lambda: NOW_NS)
    narrator = StrategyNarrator(queue=queue, clock=lambda: NOW_NS)
    plan = _plan()

    message = await narrator.announce(plan)

    assert message is not None
    assert message.text in _template_texts("strategy.attack", **_slots(plan))
    assert message.evidence["llm_used_fallback"] is True
    assert message.evidence["llm_fallback_reason"] == FALLBACK_TEMPLATE
    assert queue.pending_count == 1


async def test_llm_fallback_result_is_forwarded() -> None:
    """LLM 계층이 타임아웃으로 대체 문장을 주면 그 문장을 그대로 쓴다."""
    stub = _StubGenerator(
        text=None, used_fallback=True, fallback_reason=FALLBACK_TIMEOUT
    )
    narrator = StrategyNarrator(stub, clock=lambda: NOW_NS)
    plan = _plan()

    message = await narrator.announce(plan)

    assert message is not None
    assert message.text == stub.calls[0]["fallback_text"]
    assert message.text in _template_texts("strategy.attack", **_slots(plan))
    assert message.evidence["llm_used_fallback"] is True
    assert message.evidence["llm_fallback_reason"] == FALLBACK_TIMEOUT


async def test_generator_exception_falls_back_without_losing_radio() -> None:
    stub = _StubGenerator(error=RuntimeError("boom"))
    queue = RadioQueue(clock=lambda: NOW_NS)
    narrator = StrategyNarrator(stub, queue=queue, clock=lambda: NOW_NS)
    plan = _plan(side="defense", gap=0.7)

    message = await narrator.announce(plan)

    assert message is not None
    assert message.text == stub.calls[0]["fallback_text"]
    assert message.text in _template_texts("strategy.defense", **_slots(plan))
    assert message.evidence["llm_fallback_reason"] == FALLBACK_TEMPLATE
    assert queue.pending_count == 1


async def test_drs_attack_template_mentions_drs() -> None:
    narrator = StrategyNarrator(clock=lambda: NOW_NS)
    plan = _plan(template_key="strategy.attack_drs")

    message = await narrator.announce(plan)

    assert message is not None
    assert "DRS" in message.text
    assert message.text in _template_texts(
        "strategy.attack_drs", **_slots(plan)
    )


def test_announce_now_is_template_only_and_non_blocking() -> None:
    stub = _StubGenerator()
    queue = RadioQueue(clock=lambda: NOW_NS)
    narrator = StrategyNarrator(stub, queue=queue, clock=lambda: NOW_NS)
    plan = _plan()

    messages = narrator.announce_now([plan], now_ns=NOW_NS)

    assert len(messages) == 1
    assert stub.calls == []
    assert messages[0].text in _template_texts(
        "strategy.attack", **_slots(plan)
    )
    assert messages[0].evidence["llm_fallback_reason"] == FALLBACK_TEMPLATE
    assert queue.pending_count == 1


async def test_muted_overtake_kind_drops_strategy_radio() -> None:
    queue = RadioQueue(clock=lambda: NOW_NS)
    assert queue.set_muted("overtake", True)
    narrator = StrategyNarrator(queue=queue, clock=lambda: NOW_NS)

    message = await narrator.announce(_plan())

    assert message is None
    assert queue.pending_count == 0


# ---------------------------------------------------------------------------
# KB·감정 단계


def test_kb_text_only_includes_prep_and_attack_corners() -> None:
    kb = load_track_kb(SUZUKA_ID)
    assert kb is not None
    narrator = StrategyNarrator(kb=kb, clock=lambda: NOW_NS)

    text = narrator.kb_text(_plan())

    for number in (14, 15, 16, 17):
        assert f"## 코너 {number}:" in text
    for number in (1, 2, 9, 18):
        assert f"## 코너 {number}:" not in text


def test_kb_text_is_empty_without_kb() -> None:
    narrator = StrategyNarrator(clock=lambda: NOW_NS)

    assert narrator.kb_text(_plan()) == ""


def test_mood_for_plan() -> None:
    narrator = StrategyNarrator(clock=lambda: NOW_NS)

    assert narrator.mood_for_plan(_plan(gap=0.6)) is Mood.CALM
    assert narrator.mood_for_plan(_plan(gap=0.4)) is Mood.HYPED
    assert narrator.mood_for_plan(_plan(remaining_laps=2.0)) is Mood.HYPED
    assert (
        narrator.mood_for_plan(_plan(gap=0.6, remaining_laps=10.0))
        is Mood.CALM
    )


def test_narrator_is_radio_text_generator_compatible() -> None:
    """가짜 생성기를 `RadioTextGenerator` 규약으로 쓸 수 있는지 확인한다."""
    stub = _StubGenerator()

    assert isinstance(stub, RadioTextGenerator)


def test_uses_llm_flag_matches_generator() -> None:
    assert StrategyNarrator(_StubGenerator()).uses_llm is True
    assert StrategyNarrator().uses_llm is False


def test_template_slots_are_provided_by_plan() -> None:
    """템플릿이 요구하는 슬롯을 계획 facts가 모두 채운다."""
    narrator = StrategyNarrator(clock=lambda: NOW_NS)
    plan = _plan()

    slots = narrator._slots(plan)

    assert set(template_slots(plan.template_key)) <= set(slots)
    assert slots["spot"] == "카시오 시케인"
    assert slots["gap"] == "0.6"
    assert slots["attack"] == "16번, 17번"
    assert slots["prep"] == "14번, 15번"
