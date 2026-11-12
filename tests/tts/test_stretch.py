"""속도 처리(WSOLA): 길이·음 높이 유지, 1.0 통과."""

from __future__ import annotations

import numpy as np
import pytest

from f1re.tts.radiofx import pcm_to_wav, wav_to_pcm
from f1re.tts.stretch import time_stretch_pcm, time_stretch_wav

SAMPLE_RATE = 24_000


def _sine_pcm(
    frequency: float = 1000.0,
    seconds: float = 1.0,
    sample_rate: int = SAMPLE_RATE,
    amplitude: float = 0.3,
) -> bytes:
    """테스트용 16bit mono 사인 PCM을 만든다."""
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    signal = amplitude * np.sin(2.0 * np.pi * frequency * t)
    return np.round(signal * 32767.0).astype("<i2").tobytes()


def _dominant_frequency(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> float:
    """앞뒤 경계를 뺀 가운데 구간에서 가장 큰 주파수를 잰다."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    samples = samples[int(sample_rate * 0.1) : -int(sample_rate * 0.1)]
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
    frequencies = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate)
    return float(frequencies[int(np.argmax(spectrum))])


def test_speed_1_2_shortens_to_reciprocal() -> None:
    pcm = _sine_pcm(seconds=1.0)

    output = time_stretch_pcm(pcm, SAMPLE_RATE, 1.2)

    input_seconds = len(pcm) / 2 / SAMPLE_RATE
    output_seconds = len(output) / 2 / SAMPLE_RATE
    expected = input_seconds / 1.2
    assert output_seconds == pytest.approx(expected, rel=0.05)


def test_speed_1_2_keeps_pitch() -> None:
    pcm = _sine_pcm(frequency=1000.0, seconds=1.0)

    output = time_stretch_pcm(pcm, SAMPLE_RATE, 1.2)

    assert _dominant_frequency(output) == pytest.approx(1000.0, rel=0.03)


def test_speed_1_0_passes_input_through() -> None:
    pcm = _sine_pcm(seconds=0.25)

    assert time_stretch_pcm(pcm, SAMPLE_RATE, 1.0) == pcm


def test_speed_slower_lengthens_audio() -> None:
    pcm = _sine_pcm(seconds=0.5)

    output = time_stretch_pcm(pcm, SAMPLE_RATE, 0.8)

    input_seconds = len(pcm) / 2 / SAMPLE_RATE
    output_seconds = len(output) / 2 / SAMPLE_RATE
    assert output_seconds == pytest.approx(input_seconds / 0.8, rel=0.05)


def test_time_stretch_wav_keeps_format_and_rate() -> None:
    wav = pcm_to_wav(_sine_pcm(seconds=0.5), SAMPLE_RATE)

    output = time_stretch_wav(wav, 1.2)
    pcm, sample_rate = wav_to_pcm(output)

    assert sample_rate == SAMPLE_RATE
    assert len(pcm) < len(wav)
    assert output.startswith(b"RIFF")


def test_empty_input_returns_empty() -> None:
    assert time_stretch_pcm(b"", SAMPLE_RATE, 1.2) == b""


def test_short_input_returns_unchanged() -> None:
    pcm = _sine_pcm(seconds=0.01)  # 창(30ms)보다 짧다

    assert time_stretch_pcm(pcm, SAMPLE_RATE, 1.2) == pcm


def test_rejects_bad_speed() -> None:
    pcm = _sine_pcm(seconds=0.1)

    for speed in (0.0, -1.0, float("nan")):
        with pytest.raises(ValueError, match="speed"):
            time_stretch_pcm(pcm, SAMPLE_RATE, speed)


def test_rejects_bad_sample_rate_and_odd_bytes() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        time_stretch_pcm(b"\x00\x00", 0, 1.2)
    with pytest.raises(ValueError, match="짝수"):
        time_stretch_pcm(b"\x00", SAMPLE_RATE, 1.2)
