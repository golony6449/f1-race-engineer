"""Qwen TTS 클라이언트 (Alibaba Cloud Model Studio 국제 리전).

- 일반 합성: HTTP POST → `output.audio.url`의 WAV를 내려받는다.
- 스트리밍: 같은 엔드포인트에 `X-DashScope-SSE: enable`을 주고, SSE의
  `output.audio.data`(base64 PCM, 24kHz 16bit mono) 조각을 순서대로 낸다.
- 실시간 WebSocket 모델(`qwen3-tts-flash-realtime`)은 이번 구현 범위 밖이다.
  단가·방식은 `docs/qwen-tts-notes.md` 참고.

실패·타임아웃은 모두 `TTSUnavailable`로 올려, 무전 큐가 막히지 않고
뒤 작업(작업 11 캐시)이 "음성 오프라인"으로 처리할 수 있게 한다.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from f1re.config import TTSConfig, load_config
from f1re.secrets import SecretsError, load_qwen_key

# 공식 문서 기준 입력 텍스트 최대 길이.
MAX_TEXT_CHARS = 600


class TTSError(Exception):
    """TTS 관련 오류의 공통 부모."""


class TTSUnavailable(TTSError):
    """API 실패·타임아웃·빈 응답. 뒤 작업이 캐시 전용 모드로 전환한다."""


class TTSInvalidRequest(TTSError, ValueError):
    """호출 쪽 잘못(빈 문장, 너무 긴 문장 등)."""


def _constant_provider(value: str) -> Callable[[], str]:
    """키 문자열을 공급 함수로 감싼다. 함수 repr에 값이 드러나지 않는다."""

    def provider() -> str:
        return value

    return provider


class QwenTTS:
    """Qwen TTS 클라이언트. 키 공급 함수와 HTTP 전송을 주입할 수 있다."""

    def __init__(
        self,
        config: TTSConfig | None = None,
        *,
        key_provider: Callable[[], str] | str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config if config is not None else load_config().tts
        if key_provider is None:
            self._key_provider: Callable[[], str] = lambda: load_qwen_key(
                self._config.secret_key
            )
        elif isinstance(key_provider, str):
            self._key_provider = _constant_provider(key_provider)
        else:
            self._key_provider = key_provider

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout_s),
                transport=transport,
            )
            self._owns_client = True

    def __repr__(self) -> str:
        # 키 공급 함수는 repr에 넣지 않는다.
        return (
            f"QwenTTS(model={self._config.model!r}, voice={self._config.voice!r}, "
            f"timeout_s={self._config.timeout_s!r})"
        )

    @property
    def timeout_s(self) -> float:
        return self._config.timeout_s

    async def __aenter__(self) -> QwenTTS:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """직접 만든 HTTP 클라이언트를 닫는다. 주입받은 클라이언트는 닫지 않는다."""
        if self._owns_client:
            await self._client.aclose()

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """문장을 합성해 WAV(24kHz, 16bit mono) 바이트로 돌려준다.

        전체 요청(POST + 오디오 다운로드)이 `timeout_s` 안에 끝나야 한다.
        """
        text = self._validated_text(text)
        voice = self._resolved_voice(voice)
        authorization = self._authorization()
        body = self._request_body(text, voice)
        audio = b""
        try:
            async with asyncio.timeout(self._config.timeout_s):
                response = await self._client.post(
                    self._config.endpoint,
                    json=body,
                    headers={"Authorization": authorization},
                )
                self._raise_http_error(response, "합성")
                payload = self._response_json(response, "합성")
                audio_url = self._audio_url(payload)
                audio = await self._download_audio(audio_url)
        except TimeoutError as exc:
            raise TTSUnavailable(self._timeout_message()) from exc
        except httpx.TimeoutException as exc:
            raise TTSUnavailable(self._timeout_message()) from exc
        except httpx.HTTPError as exc:
            raise TTSUnavailable("Qwen TTS 네트워크 오류") from exc

        if not audio:
            raise TTSUnavailable("Qwen TTS 응답 오디오가 비어 있습니다")
        if not audio.startswith(b"RIFF"):
            raise TTSUnavailable("Qwen TTS 응답이 WAV가 아닙니다")
        return audio

    async def stream(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        """문장을 SSE로 합성해 PCM 조각(24kHz, 16bit mono)을 순서대로 낸다.

        첫 오디오 조각까지 `timeout_s`가 걸린다. 첫 조각이 온 뒤에는 전체
        스트림에 별도 마감 시간을 두지 않고, 각 읽기마다 httpx read 타임아웃이
        적용된다.
        """
        text = self._validated_text(text)
        voice = self._resolved_voice(voice)
        authorization = self._authorization()
        body = self._request_body(text, voice)
        headers = {
            "Authorization": authorization,
            "X-DashScope-SSE": "enable",
            "Accept": "text/event-stream",
        }
        try:
            async with asyncio.timeout(self._config.timeout_s) as deadline:
                async with self._client.stream(
                    "POST", self._config.endpoint, json=body, headers=headers
                ) as response:
                    self._raise_http_error(response, "스트리밍")
                    got_audio = False
                    async for chunk in self._iter_sse_audio(response):
                        if not got_audio:
                            got_audio = True
                            # 첫 조각을 받은 뒤에는 전체 스트림 마감을 풀어,
                            # 긴 문장의 뒷부분이 잘리지 않게 한다.
                            deadline.reschedule(None)
                        yield chunk
                    if not got_audio:
                        raise TTSUnavailable(
                            "Qwen TTS 스트림에서 오디오를 받지 못했습니다"
                        )
        except TimeoutError as exc:
            raise TTSUnavailable(self._timeout_message()) from exc
        except httpx.TimeoutException as exc:
            raise TTSUnavailable(self._timeout_message()) from exc
        except httpx.HTTPError as exc:
            raise TTSUnavailable("Qwen TTS 네트워크 오류") from exc

    # --- 요청 준비 -------------------------------------------------------

    def _validated_text(self, text: str) -> str:
        if not isinstance(text, str) or not text.strip():
            raise TTSInvalidRequest("합성할 문장이 비어 있습니다")
        if len(text) > MAX_TEXT_CHARS:
            raise TTSInvalidRequest(f"문장이 너무 깁니다 (최대 {MAX_TEXT_CHARS}자)")
        return text

    def _resolved_voice(self, voice: str | None) -> str:
        resolved = voice if voice is not None else self._config.voice
        if not isinstance(resolved, str) or not resolved.strip():
            raise TTSInvalidRequest("보이스 이름이 비어 있습니다")
        return resolved

    def _authorization(self) -> str:
        try:
            key = self._key_provider()
        except SecretsError as exc:
            raise TTSUnavailable("Qwen TTS API 키를 읽지 못했습니다") from exc
        if not isinstance(key, str) or not key.strip():
            raise TTSUnavailable("Qwen TTS API 키가 비어 있습니다")
        return f"Bearer {key.strip()}"

    def _request_body(self, text: str, voice: str) -> dict[str, Any]:
        return {
            "model": self._config.model,
            "input": {
                "text": text,
                "voice": voice,
                "language_type": self._config.language_type,
            },
        }

    def _timeout_message(self) -> str:
        return f"Qwen TTS 요청이 {self._config.timeout_s:g}초 안에 끝나지 않았습니다"

    # --- 응답 처리 -------------------------------------------------------

    def _raise_http_error(self, response: httpx.Response, what: str) -> None:
        if response.is_success:
            return
        code = request_id = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            code = payload.get("code") or None
            request_id = payload.get("request_id") or None
        detail = f"HTTP {response.status_code}"
        if code:
            detail += f", code={code}"
        if request_id:
            detail += f", request_id={request_id}"
        raise TTSUnavailable(f"Qwen TTS {what} 오류 ({detail})")

    def _response_json(self, response: httpx.Response, what: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise TTSUnavailable(f"Qwen TTS {what} 응답이 JSON이 아닙니다") from exc
        if not isinstance(payload, dict):
            raise TTSUnavailable(f"Qwen TTS {what} 응답 형식이 잘못됐습니다")
        code = payload.get("code")
        if code:
            raise TTSUnavailable(
                self._api_error_message(code, payload.get("request_id"))
            )
        return payload

    def _audio_url(self, payload: dict[str, Any]) -> str:
        output = payload.get("output")
        audio = output.get("audio") if isinstance(output, dict) else None
        url = audio.get("url") if isinstance(audio, dict) else None
        if not isinstance(url, str) or not url:
            raise TTSUnavailable("Qwen TTS 응답에 audio.url이 없습니다")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise TTSUnavailable("Qwen TTS 오디오 URL이 http/https가 아닙니다")
        return url

    async def _download_audio(self, url: str) -> bytes:
        """서명된 오디오 URL에서 WAV를 받는다. Authorization은 보내지 않는다.

        실제 응답의 URL은 `http://` OSS 서명 주소로 온다. 같은 주소의 https를
        먼저 시도하고(OSS는 보통 https도 된다), 실패하면 원래 http URL로 받는다.
        """
        parts = urlsplit(url)
        if parts.scheme == "http":
            https_url = urlunsplit(parts._replace(scheme="https"))
            try:
                upgraded = await self._client.get(https_url)
                if upgraded.is_success and upgraded.content.startswith(b"RIFF"):
                    return upgraded.content
            except httpx.HTTPError:
                # https가 안 되면 원래 http로 넘어간다.
                pass
        response = await self._client.get(url)
        self._raise_http_error(response, "오디오 다운로드")
        return response.content

    def _api_error_message(self, code: Any, request_id: Any) -> str:
        # API의 message는 넣지 않는다. 코드와 요청 ID만 남긴다.
        detail = f"code={code}"
        if request_id:
            detail += f", request_id={request_id}"
        return f"Qwen TTS API 오류 ({detail})"

    async def _iter_sse_audio(self, response: httpx.Response) -> AsyncIterator[bytes]:
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                chunk = self._sse_event_audio(data_lines)
                data_lines = []
                if chunk:
                    yield chunk
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line.startswith(("event:", "id:", "retry:")):
                continue
            else:
                data_lines.append(line)
        chunk = self._sse_event_audio(data_lines)
        if chunk:
            yield chunk

    def _sse_event_audio(self, data_lines: list[str]) -> bytes | None:
        if not data_lines:
            return None
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            return None
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise TTSUnavailable("Qwen TTS 스트림 응답이 JSON이 아닙니다") from exc
        if not isinstance(payload, dict):
            raise TTSUnavailable("Qwen TTS 스트림 응답 형식이 잘못됐습니다")
        code = payload.get("code")
        if code:
            raise TTSUnavailable(
                self._api_error_message(code, payload.get("request_id"))
            )
        output = payload.get("output")
        audio = output.get("audio") if isinstance(output, dict) else None
        encoded = audio.get("data") if isinstance(audio, dict) else None
        if not isinstance(encoded, str) or not encoded:
            # 마지막 이벤트는 data가 비어 있고 url만 온다.
            return None
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TTSUnavailable(
                "Qwen TTS 스트림 오디오(base64)가 잘못됐습니다"
            ) from exc
