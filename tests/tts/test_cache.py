"""음성 캐시: 캐시 키, 스트리밍 폴백, 미리 만들기, 오프라인 상태."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from f1re.config import TTSConfig
from f1re.radio.templates import prefetch_phrases
from f1re.tracks import load_track
from f1re.tts.cache import (
    PROBE_TEXT,
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
    text = "3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아."
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
    text = "7번 탈출에서 가속이 늦었어. 10미터 더 일찍 밟아."

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
    timeout_message = "Qwen TTS 요청이 5초 안에 끝나지 않았습니다"
    tts = FakeTTS(
        stream_error=TTSUnavailable(timeout_message),
        # 스트리밍과 일반 합성이 모두 실패해야 오프라인으로 바뀐다(SCRUM-141).
        synthesize_error=TTSUnavailable(timeout_message),
    )
    cache, _, _ = _make_cache(tmp_path, tts)
    cached_text = "박스, 박스. 들어와."
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
    assert cache.last_error is not None and "5초" in cache.last_error
    assert cache.is_cached("새 문장") is False

    # API가 돌아오면 다음 스트리밍은 성공하고 상태도 온라인으로 돌아온다.
    tts.stream_error = None
    tts.synthesize_error = None
    tts.chunks = [_tone_pcm(frequency=700.0, seconds=0.1)]
    recovered = await cache.get_or_synthesize("새 문장")
    assert [chunk async for chunk in recovered.iter_pcm()]
    assert cache.offline is False
    assert cache.last_error is None
    assert cache.is_cached("새 문장") is True


async def test_stream_failure_falls_back_to_synthesize_and_stores(
    tmp_path: Path,
) -> None:
    """일시적인 스트리밍 실패는 일반 합성으로 이어 말하고 캐시에 넣는다."""
    wav = _tone_wav(frequency=520.0, seconds=0.2)
    tts = FakeTTS(
        stream_error=TTSUnavailable("Qwen TTS 요청이 5초 안에 끝나지 않았습니다"),
        wav=wav,
    )
    cache, config, intro = _make_cache(tmp_path, tts)
    text = "5번 좋아졌어. 다음은 7번 보자."

    source = await cache.get_or_synthesize(text)
    assert source.cached is False
    chunks = [chunk async for chunk in source.iter_pcm()]

    # 폴백은 전체 합성 WAV를 렌더해 시작 효과음까지 포함한 PCM을 한 번에 낸다.
    expected = render_radio_voice(wav, config, intro=intro)
    expected_pcm, _ = wav_to_pcm(expected)
    assert b"".join(chunks) == expected_pcm
    assert chunks[0].startswith(_expected_intro_pcm(intro))

    assert tts.synthesize_calls == [(text, config.voice)]
    assert cache.status is VoiceStatus.ONLINE
    assert cache.last_error is None

    # 폴백으로 만든 음성도 캐시에 남아 다음에는 API를 부르지 않는다.
    assert cache.is_cached(text) is True
    again = await cache.get_or_synthesize(text)
    assert again.cached is True
    assert b"".join([chunk async for chunk in again.iter_pcm()]) == expected_pcm
    assert len(tts.synthesize_calls) == 1


async def test_invalid_request_is_not_retried_with_synthesize(tmp_path: Path) -> None:
    """호출 쪽 잘못(빈 문장 등)은 폴백하지 않고 오프라인으로도 바꾸지 않는다."""
    tts = FakeTTS(stream_error=TTSInvalidRequest("합성할 문장이 비어 있습니다"))
    cache, _, _ = _make_cache(tmp_path, tts)

    source = await cache.get_or_synthesize("문장")
    with pytest.raises(TTSInvalidRequest):
        async for _chunk in source.iter_pcm():
            pass

    assert tts.synthesize_calls == []
    assert cache.status is VoiceStatus.ONLINE
    assert cache.last_error is None
    assert cache.is_cached("문장") is False


async def test_fallback_failure_flags_offline_and_emits_nothing(
    tmp_path: Path,
) -> None:
    """스트리밍과 폴백이 모두 실패할 때만 오프라인·무음으로 넘어간다."""
    tts = FakeTTS(
        stream_error=TTSUnavailable("Qwen TTS 스트림 타임아웃"),
        synthesize_error=TTSUnavailable("Qwen TTS 합성 타임아웃"),
    )
    cache, config, _ = _make_cache(tmp_path, tts)
    text = "새 문장"

    source = await cache.get_or_synthesize(text)
    received: list[bytes] = []
    with pytest.raises(TTSUnavailable, match="합성 타임아웃"):
        async for chunk in source.iter_pcm():
            received.append(chunk)

    assert received == []
    assert tts.synthesize_calls == [(text, config.voice)]
    assert cache.status is VoiceStatus.OFFLINE
    assert cache.last_error == "Qwen TTS 합성 타임아웃"
    assert cache.is_cached(text) is False


async def test_prefetch_track_phrases(tmp_path: Path) -> None:
    tts = FakeTTS()
    cache, _, _ = _make_cache(tmp_path, tts)
    track = load_track(13)
    assert track is not None

    phrases = prefetch_phrases([corner.number for corner in track.corners])
    assert "1번에서 브레이크가 늦었어. 5미터 더 일찍 밟아." in phrases
    assert "18번에서 브레이크가 늦었어. 5미터 더 일찍 밟아." in phrases

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
        fx_config=RadioFxConfig(version=RadioFxConfig().version + 1),
    )
    assert other.cache_key("문장", "Katerina") != key
    assert other.cache_path("문장").parent == tmp_path / "other"


def test_default_effect_version_rebuilds_old_cache(tmp_path: Path) -> None:
    # SCRUM-90: 끝 여백·끝 무음이 생겨 SCRUM-90 이전 버전(1)의 캐시 WAV를
    # 다시 만들도록 기본 효과 버전을 올렸다.
    tts = FakeTTS()
    cache, config, intro = _make_cache(tmp_path, tts)
    old = VoiceCache(
        tts,  # type: ignore[arg-type]
        config=config,
        cache_dir=tmp_path / "old",
        intro=intro,
        fx_config=RadioFxConfig(version=1),
    )

    assert RadioFxConfig().version != 1
    assert cache.effect_version != old.effect_version
    assert cache.cache_key("문장") != old.cache_key("문장")
    assert cache.cache_path("문장") != old.cache_path("문장")


async def test_corrupt_cache_file_is_regenerated(tmp_path: Path) -> None:
    tts = FakeTTS(chunks=[_tone_pcm(frequency=440.0, seconds=0.1)])
    cache, _, _ = _make_cache(tmp_path, tts)
    text = "3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아."
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


# ---------------------------------------------------------------------------
# 오프라인 전환 로그·상태 확인(SCRUM-92)


async def test_offline_warning_only_on_transition(tmp_path: Path, caplog) -> None:
    """오프라인 전환 순간에만 warning을 남기고, 같은 장애가 반복되면 남기지 않는다."""
    tts = FakeTTS(synthesize_error=TTSUnavailable("Qwen TTS 네트워크 오류"))
    cache, _, _ = _make_cache(tmp_path, tts)

    with caplog.at_level(logging.WARNING, logger="f1re.tts.cache"):
        await cache.prefetch(["첫 문장"])
        await cache.prefetch(["둘째 문장"])

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert warnings == [
        "Qwen TTS 실패로 음성 오프라인: Qwen TTS 네트워크 오류"
    ]

    # 온라인으로 돌아왔다가 다시 실패하면 그 전환 때 한 번 더 남긴다.
    tts.synthesize_error = None
    await cache.prefetch(["셋째 문장"])
    assert cache.status is VoiceStatus.ONLINE

    tts.synthesize_error = TTSUnavailable("Qwen TTS 네트워크 오류")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="f1re.tts.cache"):
        await cache.prefetch(["넷째 문장"])
    assert len(
        [r for r in caplog.records if r.levelno == logging.WARNING]
    ) == 1


async def test_check_synthesizes_probe_without_caching(tmp_path: Path) -> None:
    """확인은 설정 보이스로 짧은 문장 한 번만 합성하고 캐시 파일을 만들지 않는다."""
    tts = FakeTTS()
    cache, config, _ = _make_cache(tmp_path, tts)

    status = await cache.check()

    assert status is VoiceStatus.ONLINE
    assert cache.status is VoiceStatus.ONLINE
    assert tts.synthesize_calls == [(PROBE_TEXT, config.voice)]
    assert tts.stream_calls == []
    assert cache.is_cached(PROBE_TEXT) is False


async def test_check_flags_offline_and_recovers(tmp_path: Path) -> None:
    tts = FakeTTS(synthesize_error=TTSUnavailable("Qwen TTS 네트워크 오류"))
    cache, _, _ = _make_cache(tmp_path, tts)

    assert await cache.check() is VoiceStatus.OFFLINE
    assert cache.offline is True
    assert cache.last_error == "Qwen TTS 네트워크 오류"

    tts.synthesize_error = None
    assert await cache.check() is VoiceStatus.ONLINE
    assert cache.offline is False
    assert cache.last_error is None
    assert len(tts.synthesize_calls) == 2


async def test_check_reraises_unexpected_error(tmp_path: Path) -> None:
    """TTSUnavailable이 아닌 오류는 상태를 바꾸지 않고 그대로 올린다."""
    tts = FakeTTS(synthesize_error=RuntimeError("예상 밖 오류"))
    cache, _, _ = _make_cache(tmp_path, tts)

    with pytest.raises(RuntimeError, match="예상 밖 오류"):
        await cache.check()

    assert cache.status is VoiceStatus.ONLINE
    assert cache.last_error is None
