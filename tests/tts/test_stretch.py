"""속도 처리(WSOLA): 길이·음 높이 유지, 끝 꼬리 보존, 1.0 통과."""

from __future__ import annotations

import numpy as np
import pytest

from f1re.tts.radiofx import pcm_to_wav, wav_to_pcm
from f1re.tts.stretch import TAIL_PAD_MS, time_stretch_pcm, time_stretch_wav

SAMPLE_RATE = 24_000
TAIL_PAD_SECONDS = TAIL_PAD_MS / 1000.0


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


def _sine_with_fading_tail(
    frequency: float = 1000.0,
    seconds: float = 0.6,
    sample_rate: int = SAMPLE_RATE,
    amplitude: float = 0.3,
    fade_ms: float = 50.0,
) -> bytes:
    """끝의 `fade_ms` 전까지 진폭을 유지하고 그 구간에서 0으로 잦아드는 사인.

    Qwen 원본처럼 끝 여백이 거의 없는 음성을 흉내 낸다(SCRUM-90).
    """
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    envelope = np.ones_like(t)
    fade = int(sample_rate * fade_ms / 1000.0)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    signal = amplitude * envelope * np.sin(2.0 * np.pi * frequency * t)
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
    # 변환 전에 붙인 끝 여백도 함께 늘어난 길이에 들어간다.
    expected = (input_seconds + TAIL_PAD_SECONDS) / 1.2
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
    assert output_seconds == pytest.approx(
        (input_seconds + TAIL_PAD_SECONDS) / 0.8, rel=0.05
    )


def test_time_stretch_wav_keeps_format_and_rate() -> None:
    wav = pcm_to_wav(_sine_pcm(seconds=0.5), SAMPLE_RATE)

    output = time_stretch_wav(wav, 1.2)
    pcm, sample_rate = wav_to_pcm(output)
    input_pcm, _ = wav_to_pcm(wav)

    assert sample_rate == SAMPLE_RATE
    expected = (len(input_pcm) / 2 / SAMPLE_RATE + TAIL_PAD_SECONDS) / 1.2
    assert len(pcm) / 2 / SAMPLE_RATE == pytest.approx(expected, rel=0.05)
    assert output.startswith(b"RIFF")


def test_empty_input_returns_empty() -> None:
    assert time_stretch_pcm(b"", SAMPLE_RATE, 1.2) == b""


def test_short_input_is_stretched_after_tail_padding() -> None:
    # 창(30ms)보다 짧은 입력도 끝 여백을 붙이면 늘일 재료가 생긴다.
    pcm = _sine_pcm(seconds=0.01)

    output = time_stretch_pcm(pcm, SAMPLE_RATE, 1.2)

    expected = (0.01 + TAIL_PAD_SECONDS) / 1.2
    assert len(output) / 2 / SAMPLE_RATE == pytest.approx(expected, rel=0.05)


def test_speed_1_2_keeps_voice_tail_and_ends_with_silence() -> None:
    # 끝 50ms 전까지 소리가 이어진 뒤 잦아드는 원본. 끝 여백이 없던 예전
    # WSOLA는 이 꼬리(약 30~50ms)를 통째로 버려 출력이 소리 중간에 끊겼다.
    pcm = _sine_with_fading_tail(seconds=0.6)
    voiced_end = 0.6 / 1.2  # 원본 소리가 끝나는 출력 시각

    output = time_stretch_pcm(pcm, SAMPLE_RATE, 1.2)
    samples = np.frombuffer(output, dtype="<i2").astype(np.float64) / 32768.0

    # (a) 출력은 붙인 여백만큼 길어지고 끝은 완전한 무음이다.
    expected_seconds = (0.6 + TAIL_PAD_SECONDS) / 1.2
    assert len(output) / 2 / SAMPLE_RATE == pytest.approx(
        expected_seconds, rel=0.05
    )
    assert np.all(samples[-int(0.1 * SAMPLE_RATE) :] == 0.0)
    # (b) 원본 마지막 소리 구간(잦아드는 50ms)의 에너지가 출력에 남아 있다.
    voiced_tail = samples[
        int((voiced_end - 0.05) * SAMPLE_RATE) : int(voiced_end * SAMPLE_RATE)
    ]
    assert float(np.sqrt(np.mean(voiced_tail**2))) > 0.05


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
