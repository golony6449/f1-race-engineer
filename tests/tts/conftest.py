"""TTS 테스트 공용 도우미. 실제 네트워크·시크릿은 쓰지 않는다."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
import pytest

from f1re.tts.radiofx import pcm_to_wav


@pytest.fixture
def make_pcm() -> Callable[..., bytes]:
    def make(
        frequencies: Sequence[float] = (440.0,),
        seconds: float = 0.25,
        sample_rate: int = 24_000,
        amplitude: float = 0.25,
    ) -> bytes:
        t = np.arange(int(sample_rate * seconds)) / sample_rate
        signal = np.zeros_like(t)
        for frequency in frequencies:
            signal += amplitude * np.sin(2.0 * np.pi * frequency * t)
        signal = np.clip(signal, -1.0, 1.0)
        return np.round(signal * 32767.0).astype("<i2").tobytes()

    return make


@pytest.fixture
def make_wav(make_pcm: Callable[..., bytes]) -> Callable[..., bytes]:
    def make(
        frequencies: Sequence[float] = (440.0,),
        seconds: float = 0.25,
        sample_rate: int = 24_000,
        amplitude: float = 0.25,
    ) -> bytes:
        pcm = make_pcm(
            frequencies=frequencies,
            seconds=seconds,
            sample_rate=sample_rate,
            amplitude=amplitude,
        )
        return pcm_to_wav(pcm, sample_rate)

    return make
