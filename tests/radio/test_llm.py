"""Qwen LLM 무전 생성 계층. 실제 네트워크·시크릿은 쓰지 않는다.

`httpx.MockTransport`와 가짜 키만 쓴다. 타임아웃은 아주 짧은 설정과 느린
가짜 핸들러로 재현한다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest

from f1re.config import LLMConfig
from f1re.radio.llm import (
    FALLBACK_DISABLED,
    FALLBACK_EMPTY,
    FALLBACK_ERROR,
    FALLBACK_NON_KOREAN,
    FALLBACK_TIMEOUT,
    FALLBACK_UNEXPECTED_NUMBER,
    LLMRadioGenerator,
    LLMUsageTracker,
    RadioTextGenerator,
    SYSTEM_PROMPT,
    non_korean_characters,
    unexpected_number_tokens,
)
from f1re.radio.templates import Mood
from f1re.secrets import SecretsError

FAKE_KEY = "test-key-not-real"
MODEL = "qwen3.7-flash-2026-07-15"
ENDPOINT = (
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
)
FALLBACK = "앞차랑 0.6초야. 다음 직선에서 DRS 써."


def _config(**overrides: Any) -> LLMConfig:
    values: dict[str, Any] = {}
    values.update(overrides)
    return LLMConfig(**values)


def _generator(
    handler: httpx.MockTransport | Callable[[httpx.Request], Any],
    *,
    config: LLMConfig | None = None,
    key_provider: Callable[[], str] | str = FAKE_KEY,
    tracker: LLMUsageTracker | None = None,
) -> LLMRadioGenerator:
    transport = (
        handler
        if isinstance(handler, httpx.MockTransport)
        else httpx.MockTransport(handler)
    )
    return LLMRadioGenerator(
        config or _config(),
        key_provider=key_provider,
        transport=transport,
        tracker=tracker,
    )


def _completion(
    content: Any,
    *,
    usage: Mapping[str, Any] | None = None,
    model: str = MODEL,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        payload["usage"] = dict(usage)
    return payload


def _returning(
    payload: dict[str, Any],
    seen: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=payload)

    return handler


# ---------------------------------------------------------------------------
# 정상 경로와 요청 모양


async def test_generate_returns_text_and_sends_fixed_system_prompt() -> None:
    seen: list[httpx.Request] = []
    payload = _completion(
        "앞차랑 0.6초야. 다음 직선에서 DRS 써.",
        usage={
            "prompt_tokens": 42,
            "completion_tokens": 17,
            "prompt_tokens_details": {"cached_tokens": 30},
        },
    )
    gen = _generator(_returning(payload, seen))
    try:
        result = await gen.generate(
            "overtake",
            {"gap_s": 0.6, "corner": 5, "DRS": True},
            kb_text="T5 탈출 뒤 DRS 구간이 길다.",
            mood=Mood.HYPED,
            fallback_text=FALLBACK,
        )
    finally:
        await gen.aclose()

    assert result.text == "앞차랑 0.6초야. 다음 직선에서 DRS 써."
    assert result.used_fallback is False
    assert result.fallback_reason is None
    assert result.latency_ms >= 0
    assert result.prompt_tokens == 42
    assert result.completion_tokens == 17
    assert result.cached_tokens == 30
    assert result.model == MODEL

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["content-type"].startswith("application/json")

    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["max_tokens"] == 80
    assert body["temperature"] == pytest.approx(0.3)
    # 시스템 프롬프트는 모듈 상수 그대로이고, 가변 값은 user 메시지에만 있다.
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert body["messages"][1]["role"] == "user"
    user = body["messages"][1]["content"]
    assert "overtake" in user
    assert "hyped" in user
    assert '"gap_s": 0.6' in user
    assert '"corner": 5' in user
    assert "T5 탈출 뒤 DRS 구간이 길다." in user


async def test_system_prompt_is_identical_for_different_inputs() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("좋아. 그대로 해."))

    gen = _generator(handler)
    try:
        await gen.generate(
            "coaching", {"corner": 3}, kb_text="", fallback_text="대체 문장"
        )
        await gen.generate(
            "improvement",
            {"corner": 7},
            kb_text="T7 탈출",
            mood=Mood.WARM,
            fallback_text="대체 문장",
        )
    finally:
        await gen.aclose()

    systems = [body["messages"][0]["content"] for body in bodies]
    assert systems == [SYSTEM_PROMPT, SYSTEM_PROMPT]
    # 가변 값이 시스템 프롬프트에 새어 들어가지 않는다.
    assert "coaching" not in systems[0]
    assert '"corner"' not in systems[0]
    # user 메시지는 호출마다 달라진다.
    assert bodies[0]["messages"][1]["content"] != bodies[1]["messages"][1]["content"]


# ---------------------------------------------------------------------------
# 실패는 모두 fallback


async def test_timeout_returns_fallback() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=_completion("늦은 응답"))

    gen = _generator(handler, config=_config(timeout_s=0.01))
    try:
        result = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_TIMEOUT


async def test_http_error_returns_fallback() -> None:
    gen = _generator(
        lambda request: httpx.Response(500, json={"error": "boom"})
    )
    try:
        result = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_ERROR


async def test_network_error_returns_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 실패", request=request)

    gen = _generator(handler)
    try:
        result = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_ERROR


async def test_non_json_response_returns_fallback() -> None:
    gen = _generator(
        lambda request: httpx.Response(200, content=b"<html>not json</html>")
    )
    try:
        result = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.fallback_reason == FALLBACK_ERROR


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": "nope"},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": 3}}]},
    ],
)
async def test_bad_response_structure_returns_fallback(
    payload: dict[str, Any],
) -> None:
    gen = _generator(lambda request: httpx.Response(200, json=payload))
    try:
        result = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_ERROR


@pytest.mark.parametrize("content", ["", "   ", "\n\t ", None])
async def test_empty_content_returns_fallback(content: Any) -> None:
    gen = _generator(
        lambda request: httpx.Response(200, json=_completion(content))
    )
    try:
        result = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_EMPTY


async def test_missing_usage_leaves_token_counts_none() -> None:
    gen = _generator(
        lambda request: httpx.Response(200, json=_completion("좋아. 그대로 해."))
    )
    try:
        result = await gen.generate(
            "improvement", {"corner": 3}, fallback_text="대체"
        )
    finally:
        await gen.aclose()

    assert result.used_fallback is False
    assert result.prompt_tokens is None
    assert result.completion_tokens is None
    assert result.cached_tokens is None
    assert result.total_tokens is None


async def test_secrets_error_returns_fallback_without_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_completion("좋아."))

    def failing_provider() -> str:
        raise SecretsError("키를 읽지 못했습니다")

    gen = _generator(handler, key_provider=failing_provider)
    try:
        result = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_ERROR
    assert requests == []
    assert "키를 읽지 못했습니다" not in repr(result)


@pytest.mark.parametrize("provider", ["", lambda: "   "])
async def test_empty_key_returns_fallback(provider: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_completion("좋아."))

    gen = _generator(handler, key_provider=provider)
    try:
        result = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_ERROR
    assert requests == []


async def test_disabled_returns_fallback_without_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_completion("좋아."))

    gen = _generator(handler, config=_config(enabled=False))
    try:
        result = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_DISABLED
    assert requests == []


# ---------------------------------------------------------------------------
# 출력 검증: 숫자 환각


async def test_unexpected_number_blocks_output() -> None:
    # facts에는 0.4만 있는데 출력이 4를 말하면 0.4와 다른 숫자다.
    gen = _generator(
        lambda request: httpx.Response(
            200, json=_completion("앞차랑 4초야. 3번에서 붙어.")
        )
    )
    try:
        result = await gen.generate(
            "overtake",
            {"gap_s": 0.4, "corner": 3},
            fallback_text=FALLBACK,
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_UNEXPECTED_NUMBER


async def test_number_from_kb_text_is_allowed() -> None:
    gen = _generator(
        lambda request: httpx.Response(
            200, json=_completion("13번 코너 앞에서 DRS 써.")
        )
    )
    try:
        result = await gen.generate(
            "overtake",
            {"corner": 4},
            kb_text="13번 코너는 DRS 구간이다.",
            fallback_text=FALLBACK,
        )
    finally:
        await gen.aclose()

    assert result.used_fallback is False
    assert result.text == "13번 코너 앞에서 DRS 써."


def test_number_token_comparison_keeps_decimals_apart() -> None:
    assert unexpected_number_tokens("0.4초", facts={"gap_s": 0.4}) == ()
    assert unexpected_number_tokens("4초", facts={"gap_s": 0.4}) == ("4",)
    # 15와 15.0은 같은 값으로 본다.
    assert unexpected_number_tokens("15미터", facts={"meters": 15.0}) == ()
    # 문자열 안의 숫자도 입력으로 인정한다.
    assert unexpected_number_tokens(
        "0.6초", facts={"note": "앞차 0.6초"}
    ) == ()
    # 키 이름은 숫자 근거가 아니다.
    assert unexpected_number_tokens("99", facts={"99키": "값"}) == ("99",)
    # F1 용어의 숫자는 용어의 일부로 본다.
    assert unexpected_number_tokens("130R에서 T1", facts={}) == ()


# ---------------------------------------------------------------------------
# 출력 검증: 비한국어 문자


@pytest.mark.parametrize(
    "content",
    [
        "汉得好",
        "ひらがな",
        "カタカナ",
        "Привет",
        "مرحبا",
        "3번에서 漢字",
    ],
)
async def test_non_korean_script_blocks_output(content: str) -> None:
    gen = _generator(
        lambda request: httpx.Response(200, json=_completion(content))
    )
    try:
        result = await gen.generate(
            "coaching", {"corner": 3}, fallback_text=FALLBACK
        )
    finally:
        await gen.aclose()

    assert result.text == FALLBACK
    assert result.used_fallback is True
    assert result.fallback_reason == FALLBACK_NON_KOREAN


async def test_f1_latin_terms_are_allowed() -> None:
    # DRS·ERS·130R·T1은 F1 용어이므로 문자·숫자 필터를 모두 통과한다.
    content = "130R 앞에서 DRS랑 ERS 다 써. T1이야."
    gen = _generator(
        lambda request: httpx.Response(200, json=_completion(content))
    )
    try:
        result = await gen.generate(
            "overtake",
            {"corner": 4},
            kb_text="스즈카",
            fallback_text=FALLBACK,
        )
    finally:
        await gen.aclose()

    assert result.used_fallback is False
    assert result.text == content


def test_non_korean_character_detection() -> None:
    assert non_korean_characters("DRS랑 ERS, 130R에서 T1! 0.6초 (좋아)") == ()
    assert non_korean_characters("3번에서 漢字") == ("漢", "字")
    assert non_korean_characters("ひらがな") == ("ひ", "ら", "が", "な")


# ---------------------------------------------------------------------------
# 로깅·집계


async def test_usage_and_latency_are_logged_and_tracked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses = [
        httpx.Response(
            200,
            json=_completion(
                "좋아. 그대로 해.",
                usage={
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 8},
                },
            ),
        ),
        httpx.Response(500, json={"error": "boom"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    tracker = LLMUsageTracker()
    gen = _generator(handler, tracker=tracker)
    with caplog.at_level("INFO", logger="f1re.radio.llm"):
        first = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
        second = await gen.generate(
            "overtake", {"gap_s": 0.6}, fallback_text=FALLBACK
        )
    await gen.aclose()

    assert first.used_fallback is False
    assert second.used_fallback is True
    # 호출마다 한 줄씩 필요한 값이 모두 남는다.
    assert "kind=overtake" in caplog.text
    assert "model=" + MODEL in caplog.text
    assert "latency_ms=" in caplog.text
    assert "prompt_tokens=20" in caplog.text
    assert "completion_tokens=10" in caplog.text
    assert "cached_tokens=8" in caplog.text
    assert "used_fallback=False" in caplog.text
    assert "used_fallback=True" in caplog.text
    assert "fallback_reason=error" in caplog.text
    # 키와 생성 문장은 info 로그에 남기지 않는다(문장은 debug).
    assert FAKE_KEY not in caplog.text
    assert "좋아. 그대로 해." not in caplog.text

    stats = tracker.snapshot()
    assert stats.calls == 2
    assert stats.succeeded == 1
    assert stats.fallbacks == 1
    assert stats.prompt_tokens == 20
    assert stats.completion_tokens == 10
    assert stats.cached_tokens == 8
    assert stats.total_tokens == 30
    assert stats.total_latency_ms >= 0
    assert stats.fallback_reasons == {FALLBACK_ERROR: 1}


async def test_generator_supports_async_context_manager() -> None:
    gen = _generator(
        lambda request: httpx.Response(200, json=_completion("좋아. 그대로 해."))
    )
    async with gen as entered:
        assert entered is gen
        result = await entered.generate(
            "improvement", {"corner": 3}, fallback_text="대체"
        )
        assert result.used_fallback is False
    # 직접 만든 클라이언트는 aclose()에서 닫힌다.
    assert gen._client.is_closed


async def test_generator_repr_does_not_leak_key() -> None:
    gen = _generator(lambda request: httpx.Response(200, json=_completion("좋아.")))
    try:
        assert FAKE_KEY not in repr(gen)
    finally:
        await gen.aclose()


async def test_generator_can_be_used_as_replay_interface() -> None:
    gen = _generator(lambda request: httpx.Response(200, json=_completion("좋아.")))
    try:
        assert isinstance(gen, RadioTextGenerator)
    finally:
        await gen.aclose()
