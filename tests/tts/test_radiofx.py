"""무전 효과: 길이 유지, 대역 제한, 잡음·클릭, 재현성."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from f1re.tts.radiofx import (
    RadioFxConfig,
    apply_radio_fx,
    apply_radio_fx_wav,
    wav_to_pcm,
)

SAMPLE_RATE = 24_000


def _amplitude_at(pcm: bytes, frequency: float, sample_rate: int = SAMPLE_RATE) -> float:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    # 시작·끝 클릭과 필터 과도 구간을 뺀다.
    samples = samples[int(sample_rate * 0.05) :]
    window = np.hanning(samples.size)
    spectrum = np.abs(np.fft.rfft(samples * window))
    frequencies = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate)
    index = int(np.argmin(np.abs(frequencies - frequency)))
    return float(spectrum[index])


def test_radiofx_keeps_length_and_limits_band(make_pcm: Callable[..., bytes]) -> None:
    pcm = make_pcm(frequencies=(1000.0, 5000.0), seconds=1.0, amplitude=0.25)

    output = apply_radio_fx(pcm, SAMPLE_RATE, config=RadioFxConfig(seed=0))

    assert len(output) == len(pcm)
    gain_1k = _amplitude_at(output, 1000.0) / _amplitude_at(pcm, 1000.0)
    gain_5k = _amplitude_at(output, 5000.0) / _amplitude_at(pcm, 5000.0)
    assert 20.0 * np.log10(gain_1k) > -3.0
    assert 20.0 * np.log10(gain_5k) < -12.0


def test_radiofx_adds_weak_noise_and_clicks(make_pcm: Callable[..., bytes]) -> None:
    silence = b"\x00\x00" * SAMPLE_RATE

    output = apply_radio_fx(
        silence,
        SAMPLE_RATE,
        config=RadioFxConfig(seed=7, noise_level=0.01, click_level=0.4),
    )
    samples = np.frombuffer(output, dtype="<i2").astype(np.float64) / 32768.0
    start = float(np.sqrt(np.mean(samples[: int(SAMPLE_RATE * 0.02)] ** 2)))
    middle = float(
        np.sqrt(np.mean(samples[int(SAMPLE_RATE * 0.4) : int(SAMPLE_RATE * 0.6)] ** 2))
    )
    end = float(np.sqrt(np.mean(samples[-int(SAMPLE_RATE * 0.02) :] ** 2)))

    # 잡음이 있고, 시작·끝 클릭이 중간보다 확실히 크다.
    assert middle > 0.0
    assert start > middle * 3.0
    assert end > middle * 3.0


def test_radiofx_is_reproducible_with_seed(make_pcm: Callable[..., bytes]) -> None:
    pcm = make_pcm(frequencies=(700.0,), seconds=0.3)

    first = apply_radio_fx(pcm, SAMPLE_RATE, config=RadioFxConfig(seed=1))
    second = apply_radio_fx(pcm, SAMPLE_RATE, config=RadioFxConfig(seed=1))
    other = apply_radio_fx(pcm, SAMPLE_RATE, config=RadioFxConfig(seed=2))

    assert first == second
    assert first != other


def test_radiofx_keeps_length_for_short_input() -> None:
    pcm = b"\x01\x00" * 10  # 필터 탭(257)보다 짧은 입력

    output = apply_radio_fx(pcm, SAMPLE_RATE, config=RadioFxConfig(seed=0))

    assert len(output) == len(pcm)


def test_radiofx_empty_input_returns_empty() -> None:
    assert apply_radio_fx(b"", SAMPLE_RATE) == b""


def test_radiofx_rejects_odd_pcm_length() -> None:
    with pytest.raises(ValueError, match="짝수"):
        apply_radio_fx(b"\x00", SAMPLE_RATE)


def test_radiofx_rejects_bad_band() -> None:
    with pytest.raises(ValueError):
        apply_radio_fx(b"\x00\x00", 1000, config=RadioFxConfig(highpass_hz=300.0, lowpass_hz=4000.0))


def test_apply_radio_fx_wav_keeps_format_and_length(make_wav: Callable[..., bytes]) -> None:
    wav = make_wav(seconds=0.2)
    original_pcm, original_rate = wav_to_pcm(wav)

    output = apply_radio_fx_wav(wav, config=RadioFxConfig(seed=0))
    output_pcm, output_rate = wav_to_pcm(output)

    assert output_rate == original_rate == SAMPLE_RATE
    assert len(output_pcm) == len(original_pcm)
    assert output.startswith(b"RIFF")


def test_wav_to_pcm_rejects_non_wav() -> None:
    with pytest.raises(ValueError):
        wav_to_pcm(b"not a wav")
