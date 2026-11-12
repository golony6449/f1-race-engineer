"""Qwen TTS 클라이언트. httpx.MockTransport와 가짜 키만 쓴다."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx
import pytest

from f1re.config import TTSConfig, load_config
from f1re.secrets import SecretsError
from f1re.tts.qwen import (
    MAX_TEXT_CHARS,
    QwenTTS,
    TTSInvalidRequest,
    TTSUnavailable,
)

FAKE_KEY = "test-key-not-real"
AUDIO_URL = "https://files.example/audio.wav"
# PL 실측(2026-11-12): 실제 응답의 audio.url은 http:// OSS 서명 주소로 온다.
HTTP_AUDIO_URL = (
    "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com"
    "/tts/sample.wav?Expires=1766113409&Signature=fake-signature"
)
ENDPOINT = (
    "https://dashscope-intl.aliyuncs.com"
    "/api/v1/services/aigc/multimodal-generation/generation"
)


def _config(**overrides: Any) -> TTSConfig:
    values: dict[str, Any] = {"timeout_s": 2.0}
    values.update(overrides)
    return TTSConfig(**values)


def _tts(
    handler: httpx.MockTransport | Any,
    *,
    config: TTSConfig | None = None,
    key_provider: Any = FAKE_KEY,
) -> QwenTTS:
    transport = handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)
    return QwenTTS(
        config or _config(),
        key_provider=key_provider,
        transport=transport,
    )


def _sse(*payloads: dict[str, Any]) -> bytes:
    return "".join(
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n" for payload in payloads
    ).encode("utf-8")


async def test_qwen_synthesize_sends_text_and_voice(make_wav: Any) -> None:
    audio = make_wav()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "status_code": 200,
                    "code": "",
                    "output": {"audio": {"data": "", "url": AUDIO_URL}},
                },
            )
        return httpx.Response(200, content=audio)

    tts = _tts(handler)
    result = await tts.synthesize("3번, 브레이크 늦음. 5미터.")

    assert result == audio
    assert len(seen) == 2
    post, download = seen
    assert str(post.url) == ENDPOINT
    assert json.loads(post.content) == {
        "model": "qwen3-tts-flash",
        "input": {
            "text": "3번, 브레이크 늦음. 5미터.",
            "voice": "Sohee",
            "language_type": "Korean",
        },
    }
    assert post.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert "x-dashscope-sse" not in post.headers
    # 서명된 오디오 URL(다른 호스트)에는 API 키를 보내지 않는다.
    assert str(download.url) == AUDIO_URL
    assert "authorization" not in download.headers


async def test_qwen_synthesize_uses_request_voice(make_wav: Any) -> None:
    audio = make_wav()
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "status_code": 200,
                    "code": "",
                    "output": {"audio": {"data": "", "url": AUDIO_URL}},
                },
            )
        return httpx.Response(200, content=audio)

    tts = _tts(handler)
    await tts.synthesize("박스, 박스.", voice="Cherry")

    assert bodies[0]["input"]["voice"] == "Cherry"


async def test_key_is_not_in_error_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "status_code": 401,
                "code": "InvalidApiKey",
                "message": "the provided key is invalid",
                "request_id": "req-1",
            },
        )

    tts = _tts(handler)
    with caplog.at_level("DEBUG", logger="f1re"):
        with pytest.raises(TTSUnavailable) as caught:
            await tts.synthesize("테스트")

    message = str(caught.value)
    assert FAKE_KEY not in message
    assert "InvalidApiKey" in message
    assert "req-1" in message
    # API의 message 원문은 예외에 넣지 않는다.
    assert "the provided key is invalid" not in message
    assert FAKE_KEY not in repr(tts)
    assert FAKE_KEY not in caplog.text


async def test_http_error_is_tts_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"status_code": 500, "code": "InternalError"})

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="HTTP 500"):
        await tts.synthesize("테스트")


async def test_non_json_response_is_tts_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="JSON"):
        await tts.synthesize("테스트")


async def test_missing_audio_url_is_tts_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status_code": 200, "output": {"audio": {}}})

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="audio.url"):
        await tts.synthesize("테스트")


async def test_non_wav_download_is_tts_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"output": {"audio": {"data": "", "url": AUDIO_URL}}},
            )
        return httpx.Response(200, content=b"not a wav")

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="WAV"):
        await tts.synthesize("테스트")


async def test_empty_download_is_tts_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"output": {"audio": {"data": "", "url": AUDIO_URL}}},
            )
        return httpx.Response(200, content=b"")

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="비어"):
        await tts.synthesize("테스트")


async def test_http_audio_url_is_downloaded_as_is(make_wav: Any) -> None:
    audio = make_wav()
    downloads: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"output": {"audio": {"data": "", "url": HTTP_AUDIO_URL}}},
            )
        downloads.append(request)
        return httpx.Response(200, content=audio)

    tts = _tts(handler)
    result = await tts.synthesize("박스, 박스.")

    assert result == audio
    # https를 먼저 시도하지 않고 API가 준 http URL을 그대로 받는다.
    assert [str(request.url) for request in downloads] == [HTTP_AUDIO_URL]
    assert "authorization" not in downloads[0].headers


async def test_http_audio_url_succeeds_when_https_would_hang(make_wav: Any) -> None:
    audio = make_wav()
    downloads: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"output": {"audio": {"data": "", "url": HTTP_AUDIO_URL}}},
            )
        downloads.append(request)
        if request.url.scheme == "https":
            # https 선시도가 있으면 TLS 핸드셰이크가 멈춘 상황을 흉내 낸다.
            await asyncio.sleep(3600)
        return httpx.Response(200, content=audio)

    # 전체 2초 마감은 그대로 두고, http URL로 그 안에 끝나야 한다.
    tts = _tts(handler, config=_config(timeout_s=2.0))
    started = time.perf_counter()
    result = await tts.synthesize("박스, 박스.")
    elapsed = time.perf_counter() - started

    assert result == audio
    assert [str(request.url) for request in downloads] == [HTTP_AUDIO_URL]
    assert elapsed < 1.0


async def test_audio_url_with_unsupported_scheme_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"output": {"audio": {"data": "", "url": "file:///tmp/a.wav"}}},
        )

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="http/https"):
        await tts.synthesize("테스트")


async def test_http_download_error_is_tts_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"output": {"audio": {"data": "", "url": AUDIO_URL}}},
            )
        return httpx.Response(404, json={"code": "NotFound", "request_id": "r2"})

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="HTTP 404"):
        await tts.synthesize("테스트")


async def test_timeout_raises_tts_unavailable_quickly() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200)

    tts = _tts(handler, config=_config(timeout_s=0.05))
    started = time.perf_counter()
    with pytest.raises(TTSUnavailable) as caught:
        await tts.synthesize("테스트")
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
    assert "0.05" in str(caught.value)
    assert tts.timeout_s == pytest.approx(0.05)


def test_default_timeout_is_two_seconds() -> None:
    config = load_config()
    assert config.tts.timeout_s == pytest.approx(2.0)

    tts = _tts(lambda request: httpx.Response(200), config=config.tts)
    assert tts.timeout_s == pytest.approx(2.0)
    # 전체 마감(asyncio.timeout)과 HTTP 클라이언트 타임아웃 모두 설정값을 쓴다.
    assert tts._client.timeout.connect == pytest.approx(2.0)
    assert tts._client.timeout.read == pytest.approx(2.0)


async def test_secret_error_becomes_tts_unavailable() -> None:
    def explode() -> str:
        raise SecretsError("도우미 없음")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("키를 읽지 못했으면 요청하지 않는다")

    tts = _tts(handler, key_provider=explode)
    with pytest.raises(TTSUnavailable, match="API 키"):
        await tts.synthesize("테스트")


async def test_empty_key_is_tts_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("빈 키로 요청하지 않는다")

    tts = _tts(handler, key_provider="   ")
    with pytest.raises(TTSUnavailable, match="비어"):
        await tts.synthesize("테스트")


async def test_empty_and_too_long_text_are_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("잘못된 입력은 요청하지 않는다")

    tts = _tts(handler)
    with pytest.raises(TTSInvalidRequest):
        await tts.synthesize("   ")
    with pytest.raises(TTSInvalidRequest, match=str(MAX_TEXT_CHARS)):
        await tts.synthesize("가" * (MAX_TEXT_CHARS + 1))


async def test_stream_parses_sse_pcm_chunks() -> None:
    chunks = [b"\x01\x00" * 10, b"\x02\x00" * 5]
    body = _sse(
        {"output": {"audio": {"data": base64.b64encode(chunks[0]).decode()}}},
        {"output": {"audio": {"data": base64.b64encode(chunks[1]).decode()}}},
        {
            "status_code": 200,
            "code": "",
            "output": {
                "audio": {"data": "", "url": AUDIO_URL},
                "finish_reason": "stop",
            },
        },
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    tts = _tts(handler)
    received = [chunk async for chunk in tts.stream("앞차 0.6. 다음 직선 DRS.", "Cherry")]

    assert received == chunks
    request = seen[0]
    assert json.loads(request.content)["input"] == {
        "text": "앞차 0.6. 다음 직선 DRS.",
        "voice": "Cherry",
        "language_type": "Korean",
    }
    assert request.headers["x-dashscope-sse"] == "enable"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"


async def test_stream_error_event_is_tts_unavailable() -> None:
    body = _sse({"status_code": 500, "code": "InternalError", "request_id": "req-9"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="InternalError"):
        async for _chunk in tts.stream("테스트"):
            pass


async def test_stream_without_audio_is_tts_unavailable() -> None:
    body = _sse({"output": {"audio": {"data": "", "url": AUDIO_URL}}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="받지 못했습니다"):
        async for _chunk in tts.stream("테스트"):
            pass


async def test_stream_bad_base64_is_tts_unavailable() -> None:
    body = _sse({"output": {"audio": {"data": "!!!!"}}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    tts = _tts(handler)
    with pytest.raises(TTSUnavailable, match="base64"):
        async for _chunk in tts.stream("테스트"):
            pass


async def test_stream_timeout_before_first_chunk() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200)

    tts = _tts(handler, config=_config(timeout_s=0.05))
    started = time.perf_counter()
    with pytest.raises(TTSUnavailable, match="0.05"):
        async for _chunk in tts.stream("테스트"):
            pass

    assert time.perf_counter() - started < 1.0


async def test_aclose_closes_owned_client_only() -> None:
    tts = _tts(lambda request: httpx.Response(200))
    await tts.aclose()
    assert tts._client.is_closed

    shared = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    injected = QwenTTS(_config(), key_provider=FAKE_KEY, client=shared)
    await injected.aclose()
    assert not shared.is_closed
    await shared.aclose()
