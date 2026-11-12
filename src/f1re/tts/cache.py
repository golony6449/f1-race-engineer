"""음성 캐시: 캐시 우선, 없는 문장만 Qwen 스트리밍.

설계 6.8의 서버 쪽 음성 경로다.

- 캐시 키 = sha256(문장 + 보이스 + 무전 효과 버전). 효과 설정·효과음 파일·말
  속도가 바뀌면 `radio_effect_version()`이 달라져 새 파일을 만든다.
- 캐시 파일은 `render_radio_voice()`를 거친 WAV다. 말 속도, 무전 효과, 시작
  효과음이 이미 들어 있어 재생할 때 다시 손대지 않는다.
- 캐시 미스는 `QwenTTS.stream()`의 첫 조각부터 내보낸다. 시작 효과음(과 무음)을
  먼저 내보내고 바로 첫 음성 조각을 잇는다. 전체를 받은 뒤에 `render_radio_voice()`
  로 저장본을 만들어 캐시에 넣는다(스트리밍 조각 자체에는 무전 효과를 입히지 않는다).
- API 실패·타임아웃(기본 2초)이면 `TTSUnavailable`을 올리고 `status`를
  "offline"으로 바꾼다. 재생 계층은 캐시에 있는 다른 문장만 내보내면 된다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from f1re.config import TTSConfig, load_config, repo_root
from f1re.tts.pipeline import (
    intro_from_tts,
    radio_effect_version,
    render_radio_voice,
)
from f1re.tts.qwen import QwenTTS, TTSError, TTSInvalidRequest, TTSUnavailable
from f1re.tts.radiofx import (
    QWEN_SAMPLE_RATE,
    IntroSoundConfig,
    RadioFxConfig,
    pcm_to_wav,
    prepend_intro_sound,
    wav_to_pcm,
)

logger = logging.getLogger(__name__)

# 캐시 폴더 기본 위치. `data/`는 .gitignore에 있다.
DEFAULT_CACHE_RELPATH = Path("data") / "tts-cache"


class VoiceStatus(StrEnum):
    """음성 경로 상태. 화면(작업 12)의 "음성 온라인/오프라인" 표시에 쓴다."""

    ONLINE = "online"
    OFFLINE = "offline"


@dataclass(frozen=True)
class PrefetchResult:
    """미리 만들기 결과."""

    requested: int
    cached: int
    synthesized: int
    failed: int


class AudioSource:
    """재생할 오디오 하나. PCM 조각을 24kHz 16bit mono로 낸다.

    소스는 한 번만 소비할 수 있다. 끊을 때는 `aclose()`를 부른다.
    """

    def __init__(
        self,
        text: str,
        chunks: AsyncIterator[bytes],
        *,
        cached: bool,
        sample_rate: int = QWEN_SAMPLE_RATE,
    ) -> None:
        self.text = text
        self.cached = cached
        self.sample_rate = sample_rate
        self._chunks = chunks
        self._closed = False

    async def iter_pcm(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            if chunk:
                yield chunk

    async def read_pcm(self) -> bytes:
        parts = [chunk async for chunk in self.iter_pcm()]
        return b"".join(parts)

    async def read_wav(self) -> bytes:
        return pcm_to_wav(await self.read_pcm(), self.sample_rate)

    async def aclose(self) -> None:
        """남은 스트림을 닫는다(재생 중단). 여러 번 불러도 된다."""
        if self._closed:
            return
        self._closed = True
        closer = getattr(self._chunks, "aclose", None)
        if closer is not None:
            await closer()


class VoiceCache:
    """문장별 무전 음성 캐시와 Qwen 스트리밍 재생."""

    def __init__(
        self,
        tts: QwenTTS,
        *,
        config: TTSConfig | None = None,
        cache_dir: str | Path | None = None,
        fx_config: RadioFxConfig | None = None,
        intro: IntroSoundConfig | None = None,
    ) -> None:
        self._tts = tts
        self._config = config if config is not None else load_config().tts
        self._fx_config = fx_config
        self._intro = intro if intro is not None else intro_from_tts(self._config)
        self._cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else repo_root() / DEFAULT_CACHE_RELPATH
        )
        self._effect_version = radio_effect_version(
            self._config, fx_config=fx_config, intro=self._intro
        )
        self._status = VoiceStatus.ONLINE
        self._last_error: str | None = None
        self._intro_pcm: bytes | None = None

    # --- 상태 -------------------------------------------------------------

    @property
    def status(self) -> VoiceStatus:
        return self._status

    @property
    def offline(self) -> bool:
        return self._status is VoiceStatus.OFFLINE

    @property
    def last_error(self) -> str | None:
        """마지막 API 실패 이유. 비밀값은 담기지 않는다."""
        return self._last_error

    @property
    def effect_version(self) -> str:
        return self._effect_version

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    # --- 캐시 키·경로 ------------------------------------------------------

    def cache_key(self, text: str, voice: str | None = None) -> str:
        """sha256(문장 + 보이스 + 무전 효과 버전) 16진 문자열."""
        resolved_text = self._validated_text(text)
        resolved_voice = self._resolved_voice(voice)
        payload = f"{resolved_text}\x00{resolved_voice}\x00{self._effect_version}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def cache_path(self, text: str, voice: str | None = None) -> Path:
        return self._cache_dir / f"{self.cache_key(text, voice)}.wav"

    def is_cached(self, text: str, voice: str | None = None) -> bool:
        return self.cache_path(text, voice).is_file()

    # --- 재생 소스 --------------------------------------------------------

    async def get_or_synthesize(
        self, text: str, voice: str | None = None
    ) -> AudioSource:
        """캐시에 있으면 파일에서, 없으면 Qwen 스트리밍으로 재생 소스를 만든다.

        캐시 미스에서 API가 실패하면 첫 조각을 내보내기 전에 `TTSUnavailable`이
        올라온다(시작 효과음도 내보내지 않는다). 실패는 `status`에 남는다.
        """
        resolved_voice = self._resolved_voice(voice)
        path = self._cache_dir / f"{self.cache_key(text, resolved_voice)}.wav"
        cached = await self._read_cached(path)
        if cached is not None:
            pcm, sample_rate = cached
            return AudioSource(
                text,
                _single_chunk(pcm),
                cached=True,
                sample_rate=sample_rate,
            )
        return AudioSource(
            text,
            self._stream_and_store(text, resolved_voice, path),
            cached=False,
        )

    async def prefetch(
        self, texts: Iterable[str], voice: str | None = None
    ) -> PrefetchResult:
        """문장 목록을 미리 만들어 캐시에 넣는다(세션 시작, 설계 6.8).

        이미 있는 문장은 건너뛴다. API가 실패하면 그 자리에서 멈추고 남은 문장은
        `failed`로 센다. 실패해도 예외를 올리지 않는다(세션 시작이 막히면 안 됨).
        """
        unique: list[str] = []
        for text in texts:
            if isinstance(text, str) and text.strip() and text not in unique:
                unique.append(text)
        resolved_voice = self._resolved_voice(voice)
        cached = synthesized = 0
        failed = 0
        for index, text in enumerate(unique):
            path = self._cache_dir / f"{self.cache_key(text, resolved_voice)}.wav"
            if path.is_file():
                cached += 1
                continue
            try:
                wav = await self._tts.synthesize(text, resolved_voice)
            except TTSUnavailable as exc:
                self._set_offline(exc)
                failed += len(unique) - index
                break
            except TTSError:
                # 잘못된 문장 등 호출 쪽 문제. 나머지는 계속 시도한다.
                failed += 1
                continue
            try:
                rendered = await asyncio.to_thread(
                    render_radio_voice,
                    wav,
                    self._config,
                    fx_config=self._fx_config,
                    intro=self._intro,
                )
            except Exception:
                logger.exception("미리 만든 무전 음성을 렌더하지 못했습니다: %s", text)
                failed += 1
                continue
            try:
                await asyncio.to_thread(self._store, path, rendered)
            except OSError:
                logger.exception("무전 음성을 캐시에 저장하지 못했습니다: %s", path)
                failed += 1
                continue
            self._set_online()
            synthesized += 1
        return PrefetchResult(
            requested=len(unique),
            cached=cached,
            synthesized=synthesized,
            failed=failed,
        )

    # --- 내부: 캐시 파일 ---------------------------------------------------

    async def _read_cached(self, path: Path) -> tuple[bytes, int] | None:
        if not path.is_file():
            return None
        try:
            wav = await asyncio.to_thread(path.read_bytes)
        except OSError:
            logger.warning("캐시 파일을 읽지 못해 다시 만듭니다: %s", path)
            return None
        try:
            pcm, sample_rate = wav_to_pcm(wav)
        except ValueError:
            # 깨진 파일은 지우고 스트리밍으로 다시 만든다.
            logger.warning("캐시 파일이 깨져 다시 만듭니다: %s", path)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return pcm, sample_rate

    def _store(self, path: Path, data: bytes) -> None:
        """임시 파일에 쓴 뒤 바꿔치기해 반쯤 쓰인 캐시를 읽지 않게 한다."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    # --- 내부: 스트리밍 ----------------------------------------------------

    async def _stream_and_store(
        self, text: str, voice: str, path: Path
    ) -> AsyncIterator[bytes]:
        stream = self._tts.stream(text, voice)
        try:
            first = await self._first_chunk(stream)
        except BaseException:
            await _aclose(stream)
            raise
        self._set_online()
        collected = bytearray(first)
        try:
            yield self._intro_pcm_bytes()
            yield first
            try:
                async for chunk in stream:
                    if not chunk:
                        continue
                    collected.extend(chunk)
                    yield chunk
            except TTSUnavailable as exc:
                self._set_offline(exc)
                raise
        finally:
            await _aclose(stream)

        # 전체를 받은 뒤에만 저장본을 만든다. 중간에 끊긴 문장은 캐시하지 않는다.
        try:
            wav = pcm_to_wav(bytes(collected), QWEN_SAMPLE_RATE)
            rendered = await asyncio.to_thread(
                render_radio_voice,
                wav,
                self._config,
                fx_config=self._fx_config,
                intro=self._intro,
            )
        except Exception:
            logger.exception("무전 음성을 렌더하지 못했습니다: %s", text)
            return
        try:
            await asyncio.to_thread(self._store, path, rendered)
        except OSError:
            logger.exception("무전 음성을 캐시에 저장하지 못했습니다: %s", path)

    async def _first_chunk(self, stream: AsyncIterator[bytes]) -> bytes:
        try:
            while True:
                chunk = await anext(stream)
                if chunk:
                    return chunk
        except StopAsyncIteration as exc:
            error = TTSUnavailable("Qwen TTS가 빈 스트림을 돌려줬습니다")
            self._set_offline(error)
            raise error from exc
        except TTSUnavailable as exc:
            self._set_offline(exc)
            raise
        except TTSError:
            raise

    def _intro_pcm_bytes(self) -> bytes:
        """시작 효과음 + 무음. 첫 스트리밍 조각 앞에 그대로 내보낸다."""
        if self._intro_pcm is None:
            empty_wav = pcm_to_wav(b"", QWEN_SAMPLE_RATE)
            with_intro = prepend_intro_sound(
                empty_wav, intro=self._intro, config=self._fx_config
            )
            pcm, _ = wav_to_pcm(with_intro)
            self._intro_pcm = pcm
        return self._intro_pcm

    # --- 내부: 공통 --------------------------------------------------------

    def _validated_text(self, text: str) -> str:
        if not isinstance(text, str) or not text.strip():
            raise TTSInvalidRequest("합성할 문장이 비어 있습니다")
        return text

    def _resolved_voice(self, voice: str | None) -> str:
        resolved = voice if voice is not None else self._config.voice
        if not isinstance(resolved, str) or not resolved.strip():
            raise TTSInvalidRequest("보이스 이름이 비어 있습니다")
        return resolved

    def _set_offline(self, error: BaseException) -> None:
        self._status = VoiceStatus.OFFLINE
        self._last_error = str(error)

    def _set_online(self) -> None:
        if self._status is VoiceStatus.OFFLINE:
            logger.info("Qwen TTS가 다시 응답합니다. 음성 온라인")
        self._status = VoiceStatus.ONLINE
        self._last_error = None


async def _single_chunk(pcm: bytes) -> AsyncIterator[bytes]:
    if pcm:
        yield pcm


async def _aclose(stream: object) -> None:
    closer = getattr(stream, "aclose", None)
    if closer is not None:
        await closer()
