"""효과음 받기 도구: 네트워크 없이 MockTransport와 가짜 오디오로 확인한다."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest

from f1re.tts.radiofx import wav_to_pcm
from tools.fetch_assets import (
    BROWSER_USER_AGENT,
    decode_intro_audio,
    fetch_intro_sound,
)

TARGET_RATE = 24_000


def _make_wav(
    *,
    rate: int = TARGET_RATE,
    channels: int = 1,
    tone_seconds: float = 0.2,
    lead_seconds: float = 0.1,
    tail_seconds: float = 0.1,
    frequency: float = 1000.0,
    amplitude: float = 0.5,
) -> bytes:
    """앞뒤 무음이 있는 테스트용 WAV를 만든다."""
    t = np.arange(int(rate * tone_seconds)) / rate
    tone = np.round(amplitude * np.sin(2.0 * np.pi * frequency * t) * 32767.0)
    samples = np.concatenate(
        [
            np.zeros(int(rate * lead_seconds), dtype=np.int16),
            tone.astype(np.int16),
            np.zeros(int(rate * tail_seconds), dtype=np.int16),
        ]
    )
    if channels == 2:
        samples = np.repeat(samples, 2)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return buffer.getvalue()


def test_decode_trims_leading_and_trailing_silence() -> None:
    decoded = decode_intro_audio(_make_wav(tone_seconds=0.2))

    assert decoded.sample_rate == TARGET_RATE
    assert decoded.source_seconds == pytest.approx(0.4, abs=0.01)
    assert decoded.duration_seconds == pytest.approx(0.2, abs=0.01)
    assert len(decoded.pcm) // 2 == round(decoded.duration_seconds * TARGET_RATE)


def test_decode_converts_stereo_44k_to_mono_24k() -> None:
    data = _make_wav(rate=44_100, channels=2, tone_seconds=0.2)

    decoded = decode_intro_audio(data)

    assert decoded.sample_rate == TARGET_RATE
    assert decoded.duration_seconds == pytest.approx(0.2, abs=0.01)


def test_decode_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="디코딩"):
        decode_intro_audio(b"not audio")


def test_decode_rejects_all_silence() -> None:
    data = _make_wav(tone_seconds=0.0, lead_seconds=0.2, tail_seconds=0.2)

    with pytest.raises(ValueError, match="소리를"):
        decode_intro_audio(data)


def test_fetch_downloads_with_browser_agent_and_saves_wav(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    audio = _make_wav(tone_seconds=0.2)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=audio)

    target = tmp_path / "assets" / "radio-intro.wav"
    result = fetch_intro_sound(
        "https://example.test/radio.mp3",
        target,
        transport=httpx.MockTransport(handler),
    )

    assert result.downloaded is True
    assert result.source_bytes == len(audio)
    assert result.duration_seconds == pytest.approx(0.2, abs=0.01)
    assert len(requests) == 1
    assert requests[0].headers["User-Agent"] == BROWSER_USER_AGENT
    assert target.is_file()

    pcm, rate = wav_to_pcm(target.read_bytes())
    assert rate == TARGET_RATE
    assert len(pcm) // 2 == round(result.duration_seconds * TARGET_RATE)


def test_fetch_skips_existing_without_request(tmp_path: Path) -> None:
    target = tmp_path / "radio-intro.wav"
    target.write_bytes(b"keep me")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("이미 있으면 네트워크를 쓰면 안 된다")

    result = fetch_intro_sound(
        "https://example.test/radio.mp3",
        target,
        transport=httpx.MockTransport(handler),
    )

    assert result.downloaded is False
    assert target.read_bytes() == b"keep me"


def test_fetch_force_downloads_again(tmp_path: Path) -> None:
    target = tmp_path / "radio-intro.wav"
    target.write_bytes(b"old")

    result = fetch_intro_sound(
        "https://example.test/radio.mp3",
        target,
        force=True,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=_make_wav())
        ),
    )

    assert result.downloaded is True
    assert target.read_bytes().startswith(b"RIFF")


def test_fetch_http_error_raises(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        fetch_intro_sound(
            "https://example.test/radio.mp3",
            tmp_path / "radio-intro.wav",
            transport=transport,
        )
