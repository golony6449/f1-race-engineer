"""음성 캐시: 캐시 키, 스트리밍 폴백, 미리 만들기, 오프라인 상태."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from f1re.config import TTSConfig
from f1re.radio.templates import prefetch_phrases
from f1re.tracks import load_track
from f1re.tts.cache import (
    VoiceCache,
    VoiceStatus,
)
from f1re.tts.pipeline import render_radio_voice
from f1re.tts.qwen import TTSInvalidRequest, TTSUnavailable
from f1re.tts.radiofx import (
    IntroSoundConfig,
    RadioFxConfig,
    pcm_to_wav,
    prepend_intro_sound,
    wav_to_pcm,
)

SAMPLE_RATE = 24_000


def _tone_pcm(
    frequency: float = 440.0,
    seconds: float = 0.1,
    amplitude: float = 0.25,
) -> bytes:
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    signal = amplitude * np.sin(2.0 * np.pi * frequency * t)
    return np.round(signal * 32767.0).astype("<i2").tobytes()


def _tone_wav(frequency: float = 440.0, seconds: float = 0.1) -> bytes:
    return pcm_to_wav(_tone_pcm(frequency, seconds), SAMPLE_RATE)


class FakeTTS:
    """QwenTTS 대역. 스트리밍 조각과 실패 지점을 정할 수 있다."""

    def __init__(
        self,
        *,
        chunks: list[bytes] | None = None,
        wav: bytes | None = None,
        stream_error: Exception | None = None,
        synthesize_error: Exception | None = None,
        fail_after_chunks: int | None = None,
    ) -> None:
        self.chunks = list(chunks or [_tone_pcm()])
        self.wav = wav if wav is not None else _tone_wav()
        self.stream_error = stream_error
        self.synthesize_error = synthesize_error
        self.fail_after_chunks = fail_after_chunks
        self.stream_calls: list[tuple[str, str | None]] = []
        self.synthesize_calls: list[tuple[str, str | None]] = []

    async def stream(self, text: str, voice: str | None = None) -> Any:
        self.stream_calls.append((text, voice))
        if self.stream_error is not None:
            raise self.stream_error
        for index, chunk in enumerate(self.chunks):
            yield chunk
            if (
                self.fail_after_chunks is not None
                and index + 1 >= self.fail_after_chunks
            ):
                raise TTSUnavailable("Qwen TTS 스트림이 중간에 끊겼습니다")

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        self.synthesize_calls.append((text, voice))
        if self.synthesize_error is not None:
            raise self.synthesize_error
        return self.wav


def _make_cache(
    tmp_path: Path, tts: FakeTTS
) -> tuple[VoiceCache, TTSConfig, IntroSoundConfig]:
    intro_path = tmp_path / "intro.wav"
    intro_path.write_bytes(_tone_wav(frequency=800.0, seconds=0.05))
    config = TTSConfig(
        speed=1.0,
        intro_sound_path=str(intro_path),
        intro_gap_ms=50.0,
        intro_gain=0.5,
    )
    intro = IntroSoundConfig(path=intro_path, gap_ms=50.0, gain=0.5)
    cache = VoiceCache(
        tts,  # type: ignore[arg-type]
        config=config,
        cache_dir=tmp_path / "cache",
        intro=intro,
    )
    return cache, config, intro


def _expected_intro_pcm(intro: IntroSoundConfig) -> bytes:
    empty_wav = pcm_to_wav(b"", SAMPLE_RATE)
    with_intro = prepend_intro_sound(empty_wav, intro=intro)
    pcm, _ = wav_to_pcm(with_intro)
    return pcm


async def test_cache_hit_no_api_call(tmp_path: Path) -> None:
    tts = FakeTTS(stream_error=AssertionError("캐시 적중인데 API를 불렀습니다"))
    cache, _, _ = _make_cache(tmp_path, tts)
    text = "3번, 브레이크 늦음. 5미터."
    cached_pcm = _tone_pcm(frequency=500.0, seconds=0.2)
    path = cache.cache_path(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm_to_wav(cached_pcm, SAMPLE_RATE))

    source = await cache.get_or_synthesize(text)

    assert source.cached is True
    assert source.sample_rate == SAMPLE_RATE
    assert await source.read_pcm() == cached_pcm
    assert tts.stream_calls == []
    assert tts.synthesize_calls == []


async def test_cache_miss_streams_intro_first_and_stores_rendered_voice(
    tmp_path: Path,
) -> None:
    first = _tone_pcm(frequency=440.0, seconds=0.1)
    second = _tone_pcm(frequency=660.0, seconds=0.1)
    tts = FakeTTS(chunks=[first, second])
    cache, config, intro = _make_cache(tmp_path, tts)
    text = "7번 탈출, 스로틀 늦음. 10미터."

    source = await cache.get_or_synthesize(text)

    assert source.cached is False
    chunks = [chunk async for chunk in source.iter_pcm()]
    # 시작 효과음이 첫 조각으로 나가고, 그다음 스트리밍 조각이 이어진다.
    assert chunks[0] == _expected_intro_pcm(intro)
    assert chunks[1:] == [first, second]

    # 저장본은 전체를 받은 뒤 render_radio_voice()를 거친 WAV다.
    stored = cache.cache_path(text).read_bytes()
    expected = render_radio_voice(
        pcm_to_wav(first + second, SAMPLE_RATE), config, intro=intro
    )
    assert stored == expected
    stored_pcm, _ = wav_to_pcm(stored)
    assert stored_pcm != first + second  # 말 속도·무전 효과가 들어갔다

    # 다시 부르면 캐시에서 바로 나가고 API를 부르지 않는다.
    again = await cache.get_or_synthesize(text)
    assert again.cached is True
    assert await again.read_pcm() == stored_pcm
    assert len(tts.stream_calls) == 1


async def test_api_failure_falls_back_to_cache_only_and_flags_offline(
    tmp_path: Path,
) -> None:
    tts = FakeTTS(
        stream_error=TTSUnavailable("Qwen TTS 요청이 2초 안에 끝나지 않았습니다")
    )
    cache, _, _ = _make_cache(tmp_path, tts)
    cached_text = "박스, 박스."
    cached_pcm = _tone_pcm(frequency=300.0, seconds=0.1)
    path = cache.cache_path(cached_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm_to_wav(cached_pcm, SAMPLE_RATE))

    # 캐시에 있는 문장은 API가 죽어도 그대로 나간다.
    cached_source = await cache.get_or_synthesize(cached_text)
    assert await cached_source.read_pcm() == cached_pcm
    assert cache.status is VoiceStatus.ONLINE

    # 캐시에 없는 문장은 첫 조각 전에 실패해 아무 소리도 내지 않는다.
    source = await cache.get_or_synthesize("새 문장")
    received: list[bytes] = []
    with pytest.raises(TTSUnavailable):
        async for chunk in source.iter_pcm():
            received.append(chunk)

    assert received == []
    assert cache.offline is True
    assert cache.status is VoiceStatus.OFFLINE
    assert cache.last_error is not None and "2초" in cache.last_error
    assert cache.is_cached("새 문장") is False

    # API가 돌아오면 다음 스트리밍은 성공하고 상태도 온라인으로 돌아온다.
    tts.stream_error = None
    tts.chunks = [_tone_pcm(frequency=700.0, seconds=0.1)]
    recovered = await cache.get_or_synthesize("새 문장")
    assert [chunk async for chunk in recovered.iter_pcm()]
    assert cache.offline is False
    assert cache.last_error is None
    assert cache.is_cached("새 문장") is True


async def test_prefetch_track_phrases(tmp_path: Path) -> None:
    tts = FakeTTS()
    cache, _, _ = _make_cache(tmp_path, tts)
    track = load_track(13)
    assert track is not None

    phrases = prefetch_phrases([corner.number for corner in track.corners])
    assert "1번, 브레이크 늦음. 5미터." in phrases
    assert "18번, 브레이크 늦음. 5미터." in phrases

    result = await cache.prefetch(phrases)

    assert result.requested == len(phrases)
    assert result.synthesized == len(phrases)
    assert result.cached == 0
    assert result.failed == 0
    assert all(cache.is_cached(phrase) for phrase in phrases)
    assert len(tts.synthesize_calls) == len(phrases)

    # 두 번째 호출은 전부 캐시 적중이라 API를 부르지 않는다.
    again = await cache.prefetch(phrases)
    assert (again.cached, again.synthesized, again.failed) == (len(phrases), 0, 0)
    assert len(tts.synthesize_calls) == len(phrases)


async def test_prefetch_stops_when_api_unavailable(tmp_path: Path) -> None:
    tts = FakeTTS(synthesize_error=TTSUnavailable("Qwen TTS 네트워크 오류"))
    cache, _, _ = _make_cache(tmp_path, tts)

    result = await cache.prefetch(["첫 문장", "둘째 문장", "셋째 문장"])

    assert result.requested == 3
    assert result.synthesized == 0
    assert result.failed == 3
    assert cache.offline is True
    # 첫 실패에서 멈춘다. 같은 장애로 세 번 기다리지 않는다.
    assert [text for text, _ in tts.synthesize_calls] == ["첫 문장"]
    assert cache.is_cached("첫 문장") is False


async def test_cache_key_uses_text_voice_and_effect_version(tmp_path: Path) -> None:
    tts = FakeTTS()
    cache, config, intro = _make_cache(tmp_path, tts)

    key = cache.cache_key("문장", "Katerina")

    assert key == cache.cache_key("문장", "Katerina")
    assert len(key) == 64 and all(char in "0123456789abcdef" for char in key)
    assert key != cache.cache_key("다른 문장", "Katerina")
    assert key != cache.cache_key("문장", "Sohee")
    # 보이스를 생략하면 설정의 보이스를 쓴다.
    assert cache.cache_key("문장") == cache.cache_key("문장", config.voice)

    # 효과 버전(무전 효과 설정)이 바뀌면 같은 문장도 다른 캐시를 쓴다.
    other = VoiceCache(
        tts,  # type: ignore[arg-type]
        config=config,
        cache_dir=tmp_path / "other",
        intro=intro,
        fx_config=RadioFxConfig(version=2),
    )
    assert other.cache_key("문장", "Katerina") != key
    assert other.cache_path("문장").parent == tmp_path / "other"


async def test_corrupt_cache_file_is_regenerated(tmp_path: Path) -> None:
    tts = FakeTTS(chunks=[_tone_pcm(frequency=440.0, seconds=0.1)])
    cache, _, _ = _make_cache(tmp_path, tts)
    text = "3번, 브레이크 늦음. 5미터."
    path = cache.cache_path(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a wav")

    source = await cache.get_or_synthesize(text)
    assert source.cached is False
    assert [chunk async for chunk in source.iter_pcm()]

    assert path.read_bytes().startswith(b"RIFF")
    assert len(tts.stream_calls) == 1


async def test_midstream_failure_emits_received_chunks_but_does_not_cache(
    tmp_path: Path,
) -> None:
    first = _tone_pcm(frequency=440.0, seconds=0.1)
    second = _tone_pcm(frequency=660.0, seconds=0.1)
    tts = FakeTTS(chunks=[first, second], fail_after_chunks=2)
    cache, _, _ = _make_cache(tmp_path, tts)
    text = "문장"

    source = await cache.get_or_synthesize(text)
    received: list[bytes] = []
    with pytest.raises(TTSUnavailable):
        async for chunk in source.iter_pcm():
            received.append(chunk)

    assert received[0] == _expected_intro_pcm(
        IntroSoundConfig(
            path=tmp_path / "intro.wav", gap_ms=50.0, gain=0.5
        )
    )
    assert received[1:] == [first, second]
    assert cache.offline is True
    assert cache.is_cached(text) is False


async def test_empty_text_rejected_and_empty_prefetch_is_noop(tmp_path: Path) -> None:
    tts = FakeTTS()
    cache, _, _ = _make_cache(tmp_path, tts)

    with pytest.raises(TTSInvalidRequest):
        await cache.get_or_synthesize("   ")

    result = await cache.prefetch(["", "  "])
    assert result == type(result)(requested=0, cached=0, synthesized=0, failed=0)
    assert tts.synthesize_calls == []
