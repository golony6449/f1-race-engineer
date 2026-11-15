"""Qwen LLM 무전 문장 생성 계층 (SCRUM-129).

판단과 숫자는 규칙 엔진이 정한다. 이 모듈은 주어진 사실(`facts`)과 KB 문맥만으로
짧은 한국어 무전 문장을 만드는 공용 계층이다. 급하지 않은 무전(추월·방어 전략
등)에 쓴다.

원칙:

- **시스템 프롬프트는 완전히 고정한다.** 가변 값(종류·감정·사실·KB)은 모두
  `user` 메시지에 넣어 프롬프트 캐시가 입력마다 깨지지 않게 한다.
- **LLM은 문장만 만든다.** 숫자를 계산하거나 추측하지 못하도록, 출력에 입력에
  없는 숫자 토큰이 있으면 문장을 버리고 `fallback_text`를 쓴다.
- **한국어 무전만 내보낸다.** 한자·가나·키릴·아랍 문자 등이 섞이면 버린다.
  F1 용어(`DRS`, `ERS`, `SC`, `VSC`, `130R`, `T1` 등)의 ASCII 영문은 허용한다.
- **실패는 예외로 올리지 않는다.** 타임아웃(`timeout_s`, 기본 1.5초)·키 조회
  실패·HTTP 오류·응답 구조 오류·빈 content는 모두 `fallback_text`와
  `fallback_reason`으로 돌려, 무전 큐가 막히지 않게 한다.
- 키 값은 로그·예외 메시지·`__repr__`에 넣지 않는다(`tts/qwen.py`와 같은 규칙).

실시간 경로와 리플레이(인메모리) 경로는 `RadioTextGenerator` 인터페이스로 같은
생성기를 쓸 수 있다. 이 모듈은 DB·큐·웹에 의존하지 않는 순수 클라이언트다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

import httpx

from f1re.config import LLMConfig, load_config
from f1re.radio.templates import Mood
from f1re.secrets import SecretsError, load_qwen_key

logger = logging.getLogger(__name__)

# 폴백 이유. 로그·집계·테스트가 같은 문자열을 쓴다.
FALLBACK_DISABLED = "disabled"
FALLBACK_TIMEOUT = "timeout"
FALLBACK_ERROR = "error"
FALLBACK_EMPTY = "empty"
FALLBACK_UNEXPECTED_NUMBER = "unexpected_number"
FALLBACK_NON_KOREAN = "non_korean_script"

# 시스템 프롬프트. **절대 가변 값을 넣지 않는다**(프롬프트 캐시).
# 사용자 메시지에 사실·KB·감정이 모두 들어간다.
SYSTEM_PROMPT = (
    "너는 F1 팀의 한국어 레이스 엔지니어다. 기사에게 짧은 팀 무전 문장을 만든다.\n"
    "규칙:\n"
    "1. 사용자 메시지에 주어진 사실(facts)과 KB 문맥만 사용한다. 없는 사실을 "
    "추측하거나 지어내지 않는다.\n"
    "2. 숫자는 사실에 적힌 값을 그대로만 쓴다. 계산하거나 새 숫자를 만들지 않는다.\n"
    "3. 짧은 반말 1~2문장으로 답한다. 무엇이 어땠는지 말하고 어떻게 하라고 이어 준다. "
    '예: "3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아."\n'
    "4. 문장만 출력한다. 인사말·설명·따옴표·목록을 붙이지 않는다.\n"
    "5. 한글과 F1 용어(DRS, ERS, SC, VSC 등), 숫자, 기본 문장부호만 쓴다. "
    "한자·일본어 가나·키릴 문자·아랍 문자 등 다른 문자는 쓰지 않는다.\n"
)

# 감정 단계별 문장 지시. 값 자체는 고정이지만 어느 단계인지는 user 메시지에 들어간다.
_MOOD_GUIDES: dict[str, str] = {
    Mood.CALM.value: "담담하게 알려 준다",
    Mood.URGENT.value: "급박하게, 짧게 끊어서 말한다",
    Mood.HYPED.value: "흥분해서 에너지를 실어 말한다",
    Mood.WARM.value: "밝고 따뜻하게 칭찬한다",
}

# 숫자 토큰. 정수·소수를 함께 잡고, "0.4"를 "0"과 "4"로 나누지 않는다.
# F1 용어(`130R`, `T1`)의 숫자는 용어의 일부이므로 숫자 주장으로 보지 않는다.
# 단, "15미터"·"0.4초"처럼 한글·문장부호와 붙은 숫자는 그대로 검사한다.
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])\d++(?:\.\d++)?(?![A-Za-z0-9])"
)

# 무전에 허용하는 기본 문장부호.
_ALLOWED_PUNCTUATION = frozenset(",.!?~:-/()%")


class LLMResponseError(Exception):
    """LLM 응답이 기대한 JSON 구조가 아닐 때. 값 자체는 담지 않는다."""


@dataclass(frozen=True)
class LLMResult:
    """LLM 무전 생성 한 번의 결과.

    `used_fallback`이 True면 `text`는 호출자가 준 `fallback_text`이고,
    `fallback_reason`이 왜 대체했는지 말한다. 토큰 수는 API가 주지 않으면
    None이다(폴백은 대부분 None).
    """

    text: str
    used_fallback: bool
    fallback_reason: str | None
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    model: str = ""

    @property
    def total_tokens(self) -> int | None:
        """프롬프트 + 완성 토큰. 둘 다 없으면 None."""
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


@dataclass(frozen=True)
class LLMUsageStats:
    """레이스/세션 동안의 LLM 사용량 집계.

    `fallback_reasons`는 이유별 대체 횟수다(예: `{"timeout": 2}`).
    """

    calls: int = 0
    succeeded: int = 0
    fallbacks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_latency_ms: int = 0
    fallback_reasons: Mapping[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMUsageTracker:
    """LLM 호출 결과를 세션/레이스 단위로 쌓는 집계기.

    여러 생성기가 같은 트래커를 공유할 수 있고, `snapshot()`은 그때까지의
    값을 복사해 돌려준다. 스레드 안전하다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls = 0
        self._succeeded = 0
        self._fallbacks = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cached_tokens = 0
        self._total_latency_ms = 0
        self._fallback_reasons: dict[str, int] = {}

    def record(self, result: LLMResult) -> None:
        """결과 한 건을 집계에 더한다. 폴백 호출도 호출 수에 포함한다."""
        with self._lock:
            self._calls += 1
            if result.used_fallback:
                self._fallbacks += 1
                reason = result.fallback_reason or "unknown"
                self._fallback_reasons[reason] = (
                    self._fallback_reasons.get(reason, 0) + 1
                )
            else:
                self._succeeded += 1
            self._prompt_tokens += result.prompt_tokens or 0
            self._completion_tokens += result.completion_tokens or 0
            self._cached_tokens += result.cached_tokens or 0
            self._total_latency_ms += max(0, result.latency_ms)

    def snapshot(self) -> LLMUsageStats:
        """지금까지의 집계를 복사해 돌려준다."""
        with self._lock:
            return LLMUsageStats(
                calls=self._calls,
                succeeded=self._succeeded,
                fallbacks=self._fallbacks,
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
                cached_tokens=self._cached_tokens,
                total_latency_ms=self._total_latency_ms,
                fallback_reasons=dict(self._fallback_reasons),
            )

    @property
    def stats(self) -> LLMUsageStats:
        """`snapshot()`과 같은 값(조회 편의용)."""
        return self.snapshot()

    def reset(self) -> None:
        """집계를 0으로 되돌린다(새 레이스/세션 시작)."""
        with self._lock:
            self._calls = 0
            self._succeeded = 0
            self._fallbacks = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._cached_tokens = 0
            self._total_latency_ms = 0
            self._fallback_reasons.clear()


@runtime_checkable
class RadioTextGenerator(Protocol):
    """실시간·리플레이가 함께 쓰는 무전 문장 생성기 인터페이스."""

    async def generate(
        self,
        kind: str,
        facts: Mapping[str, Any],
        *,
        kb_text: str = "",
        mood: Mood | str = Mood.CALM,
        fallback_text: str,
    ) -> LLMResult:
        """사실과 KB 문맥으로 무전 문장을 만든다. 실패하면 fallback_text."""
        ...

    async def aclose(self) -> None:
        """자원을 정리한다(여러 번 불러도 안전)."""
        ...


def _constant_provider(value: str) -> Callable[[], str]:
    """키 문자열을 공급 함수로 감싼다. 함수 repr에 값이 드러나지 않는다."""

    def provider() -> str:
        return value

    return provider


def _mood_value(mood: Mood | str) -> str:
    """감정 단계를 문자열로 맞춘다. 모르는 값은 calm으로 본다."""
    if isinstance(mood, Mood):
        return mood.value
    try:
        return Mood(mood).value
    except (TypeError, ValueError):
        return Mood.CALM.value


def _facts_json(facts: Mapping[str, Any]) -> str:
    """사실을 결정적인 JSON 문자열로 만든다(user 메시지용)."""
    try:
        return json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(facts)


def build_user_prompt(
    kind: str,
    facts: Mapping[str, Any],
    *,
    kb_text: str = "",
    mood: Mood | str = Mood.CALM,
) -> str:
    """가변 값만 담은 user 메시지를 만든다.

    `SYSTEM_PROMPT`는 고정이므로, 입력이 바뀌어도 캐시되는 앞부분이 깨지지
    않게 모든 가변 값은 이 함수가 만드는 user 메시지 하나에 모은다.
    """
    mood_value = _mood_value(mood)
    guide = _MOOD_GUIDES.get(mood_value, "")
    context = kb_text.strip() if isinstance(kb_text, str) else str(kb_text).strip()
    return (
        f"무전 종류: {kind}\n"
        f"감정: {mood_value} ({guide})\n"
        f"사실(facts):\n{_facts_json(facts)}\n"
        f"KB 문맥:\n{context or '(없음)'}\n"
        "위 사실과 KB 문맥만으로 짧은 한국어 무전 문장 1~2개를 만들어라."
    )


def _decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def _facts_value_text(facts: Mapping[str, Any]) -> str:
    """`facts`의 모든 값(중첩 포함)을 숫자 검사용 문자열로 모은다.

    키 이름은 숫자 근거가 아니므로 넣지 않는다. 문자열 값은 그대로, 그 밖의
    값은 JSON 표기로 넣는다.
    """
    parts: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            parts.append(value)
        else:
            try:
                parts.append(json.dumps(value, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                parts.append(str(value))

    collect(facts)
    return "\n".join(parts)


def unexpected_number_tokens(
    text: str, *, facts: Mapping[str, Any], kb_text: str = ""
) -> tuple[str, ...]:
    """출력의 숫자 토큰 중 입력에 없는 것을 처음 나온 순서로 돌려준다.

    입력은 `facts`의 모든 값과 `kb_text`에 등장하는 숫자 토큰이다. 토큰은
    정규식으로 통째로 잡으므로 `0.4`는 `0.4`이고, 출력의 `4`는 입력에 `0.4`만
    있으면 없는 숫자로 판정한다. `15`와 `15.0`처럼 값이 같은 표기는 허용한다
    (`Decimal` 비교). `130R`·`T1` 같은 F1 용어의 숫자는 용어의 일부로 본다.
    """
    source = _facts_value_text(facts) + "\n" + (
        kb_text if isinstance(kb_text, str) else str(kb_text)
    )
    allowed_raw: set[str] = set()
    allowed_values: set[Decimal] = set()
    for token in _NUMBER_RE.findall(source):
        allowed_raw.add(token)
        value = _decimal(token)
        if value is not None:
            allowed_values.add(value)

    unexpected: list[str] = []
    for token in _NUMBER_RE.findall(text):
        if token in allowed_raw:
            continue
        value = _decimal(token)
        if value is not None and value in allowed_values:
            continue
        if token not in unexpected:
            unexpected.append(token)
    return tuple(unexpected)


def _is_hangul(ch: str) -> bool:
    code = ord(ch)
    return (
        0xAC00 <= code <= 0xD7A3  # 한글 음절
        or 0x1100 <= code <= 0x11FF  # 한글 자모
        or 0x3130 <= code <= 0x318F  # 한글 호환 자모
        or 0xA960 <= code <= 0xA97F  # 한글 자모 확장-A
        or 0xD7B0 <= code <= 0xD7FF  # 한글 자모 확장-B
    )


def non_korean_characters(text: str) -> tuple[str, ...]:
    """한국어 무전에 허용하지 않는 문자를 처음 나온 순서로 돌려준다.

    한글·ASCII 영문(F1 용어)·숫자·공백·기본 문장부호(`, . ! ? ~ : - / ( ) %`)만
    허용한다. 한자(CJK)·가나·키릴·아랍 문자 등이 하나라도 있으면 걸린다.
    """
    found: list[str] = []
    for ch in text:
        if ch in _ALLOWED_PUNCTUATION or ch.isspace():
            continue
        if ch.isascii() and ch.isalnum():
            continue
        if _is_hangul(ch):
            continue
        if ch not in found:
            found.append(ch)
    return tuple(found)


def _int_or_none(value: Any) -> int | None:
    """토큰 수 필드를 정수로 읽는다. bool·음수·비정수는 None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


class LLMRadioGenerator:
    """Qwen OpenAI 호환 chat completions로 무전 문장을 만든다.

    키 공급 함수·HTTP 전송·클라이언트·사용량 트래커를 주입할 수 있어 테스트와
    리플레이에서 네트워크 없이 같은 경로를 쓴다. 직접 만든 `httpx.AsyncClient`는
    `aclose()`/`async with`에서 닫고, 주입받은 클라이언트는 닫지 않는다.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        key_provider: Callable[[], str] | str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        tracker: LLMUsageTracker | None = None,
    ) -> None:
        self._config = config if config is not None else load_config().llm
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

        self._tracker = tracker if tracker is not None else LLMUsageTracker()

    def __repr__(self) -> str:
        # 키 공급 함수는 repr에 넣지 않는다.
        return (
            f"LLMRadioGenerator(model={self._config.model!r}, "
            f"timeout_s={self._config.timeout_s!r}, "
            f"enabled={self._config.enabled!r})"
        )

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def timeout_s(self) -> float:
        return self._config.timeout_s

    @property
    def tracker(self) -> LLMUsageTracker:
        return self._tracker

    async def __aenter__(self) -> LLMRadioGenerator:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """직접 만든 HTTP 클라이언트를 닫는다. 주입받은 클라이언트는 닫지 않는다."""
        if self._owns_client:
            await self._client.aclose()

    async def generate(
        self,
        kind: str,
        facts: Mapping[str, Any],
        *,
        kb_text: str = "",
        mood: Mood | str = Mood.CALM,
        fallback_text: str,
    ) -> LLMResult:
        """사실과 KB 문맥으로 짧은 한국어 무전 문장을 만든다.

        API를 부르지 않아야 하거나(`enabled=False`) 실패하면 `fallback_text`를
        그대로 돌려주고 `used_fallback=True`로 표시한다. 예외를 올리지 않는다.
        """
        started = time.perf_counter()
        if not self._config.enabled:
            return self._result(
                kind, fallback_text, True, FALLBACK_DISABLED, started
            )

        try:
            authorization = self._authorization()
        except SecretsError:
            logger.debug("LLM API 키를 읽지 못해 fallback 문장을 씁니다")
            return self._result(kind, fallback_text, True, FALLBACK_ERROR, started)

        body = self._request_body(
            build_user_prompt(kind, facts, kb_text=kb_text, mood=mood)
        )
        try:
            async with asyncio.timeout(self._config.timeout_s):
                response = await self._client.post(
                    self._config.endpoint,
                    json=body,
                    headers={
                        "Authorization": authorization,
                        "Content-Type": "application/json",
                    },
                )
                (
                    content,
                    prompt_tokens,
                    completion_tokens,
                    cached_tokens,
                    model,
                ) = self._completion(response)
        except TimeoutError:
            return self._result(
                kind, fallback_text, True, FALLBACK_TIMEOUT, started
            )
        except httpx.TimeoutException:
            return self._result(
                kind, fallback_text, True, FALLBACK_TIMEOUT, started
            )
        except (httpx.HTTPError, LLMResponseError) as exc:
            # 응답 본문·API message는 남기지 않는다(키가 섞일 수 있다).
            logger.debug("LLM 요청 실패: %s", type(exc).__name__)
            return self._result(kind, fallback_text, True, FALLBACK_ERROR, started)

        text = " ".join(content.split())
        if not text:
            return self._result(kind, fallback_text, True, FALLBACK_EMPTY, started)

        bad_characters = non_korean_characters(text)
        if bad_characters:
            logger.debug("LLM 출력에 비한국어 문자: %r", bad_characters)
            return self._result(
                kind, fallback_text, True, FALLBACK_NON_KOREAN, started
            )

        unexpected = unexpected_number_tokens(text, facts=facts, kb_text=kb_text)
        if unexpected:
            logger.debug("LLM 출력에 입력에 없는 숫자: %r", unexpected)
            return self._result(
                kind, fallback_text, True, FALLBACK_UNEXPECTED_NUMBER, started
            )

        return self._result(
            kind,
            text,
            False,
            None,
            started,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            model=model,
        )

    # --- 요청 준비 -------------------------------------------------------

    def _authorization(self) -> str:
        try:
            key = self._key_provider()
        except SecretsError as exc:
            raise SecretsError("Qwen LLM API 키를 읽지 못했습니다") from exc
        except Exception as exc:  # 공급 함수의 오류도 폴백으로 처리한다.
            raise SecretsError("Qwen LLM API 키를 읽지 못했습니다") from exc
        if not isinstance(key, str) or not key.strip():
            raise SecretsError("Qwen LLM API 키가 비어 있습니다")
        return f"Bearer {key.strip()}"

    def _request_body(self, user_prompt: str) -> dict[str, Any]:
        return {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self._config.max_tokens,
            "temperature": self._config.temperature,
        }

    # --- 응답 처리 -------------------------------------------------------

    def _completion(
        self, response: httpx.Response
    ) -> tuple[str, int | None, int | None, int | None, str]:
        """응답에서 문장·토큰 수·모델 이름을 꺼낸다.

        HTTP 오류·JSON 오류·구조 오류는 `LLMResponseError`로 올린다. content가
        없거나 문자열이 아니어도 구조 오류다(빈 문자열은 호출 쪽에서 `empty`).
        """
        # 주입한 가짜 클라이언트도 쓸 수 있게 `is_success` 대신 상태 코드를 본다.
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            raise LLMResponseError(f"HTTP {status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMResponseError("응답이 JSON이 아닙니다") from exc
        if not isinstance(payload, dict):
            raise LLMResponseError("응답 JSON이 객체가 아닙니다")

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("응답에 choices가 없습니다")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMResponseError("choices[0]이 객체가 아닙니다")
        message = first.get("message")
        if not isinstance(message, dict):
            raise LLMResponseError("응답에 message가 없습니다")
        if "content" not in message:
            raise LLMResponseError("응답에 content가 없습니다")
        content = message["content"]
        if content is None:
            content = ""
        elif not isinstance(content, str):
            raise LLMResponseError("응답 content가 문자열이 아닙니다")

        prompt_tokens, completion_tokens, cached_tokens = self._usage(payload)
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            model = self._config.model
        return (
            content,
            prompt_tokens,
            completion_tokens,
            cached_tokens,
            model,
        )

    @staticmethod
    def _usage(
        payload: Mapping[str, Any],
    ) -> tuple[int | None, int | None, int | None]:
        """OpenAI 호환 usage를 읽는다. 캐시 토큰은 두 모양을 모두 받는다."""
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None, None, None
        prompt_tokens = _int_or_none(usage.get("prompt_tokens"))
        completion_tokens = _int_or_none(usage.get("completion_tokens"))
        cached_tokens: int | None = None
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached_tokens = _int_or_none(details.get("cached_tokens"))
        if cached_tokens is None:
            cached_tokens = _int_or_none(usage.get("cached_tokens"))
        return prompt_tokens, completion_tokens, cached_tokens

    # --- 결과 만들기 -----------------------------------------------------

    def _result(
        self,
        kind: str,
        text: str,
        used_fallback: bool,
        fallback_reason: str | None,
        started: float,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cached_tokens: int | None = None,
        model: str | None = None,
    ) -> LLMResult:
        """결과를 만들고 로그·집계를 남긴다. 생성 문장은 debug로만 남긴다."""
        result = LLMResult(
            text=text,
            used_fallback=used_fallback,
            fallback_reason=fallback_reason,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            model=model or self._config.model,
        )
        self._tracker.record(result)
        logger.info(
            "LLM 무전 생성 kind=%s model=%s latency_ms=%d prompt_tokens=%s "
            "completion_tokens=%s cached_tokens=%s used_fallback=%s "
            "fallback_reason=%s",
            kind,
            result.model,
            result.latency_ms,
            result.prompt_tokens,
            result.completion_tokens,
            result.cached_tokens,
            result.used_fallback,
            result.fallback_reason,
        )
        logger.debug("LLM 무전 문장: %s", result.text)
        return result


__all__ = [
    "FALLBACK_DISABLED",
    "FALLBACK_EMPTY",
    "FALLBACK_ERROR",
    "FALLBACK_NON_KOREAN",
    "FALLBACK_TIMEOUT",
    "FALLBACK_UNEXPECTED_NUMBER",
    "LLMRadioGenerator",
    "LLMResult",
    "LLMUsageStats",
    "LLMUsageTracker",
    "RadioTextGenerator",
    "SYSTEM_PROMPT",
    "build_user_prompt",
    "non_korean_characters",
    "unexpected_number_tokens",
]
