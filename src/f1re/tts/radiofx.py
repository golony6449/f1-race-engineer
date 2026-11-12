"""F1 무전 효과: 대역 필터, 약한 잡음, 시작·끝 클릭음."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

import numpy as np

# Qwen TTS가 내보내는 PCM 형식 (24kHz, 16bit, mono).
QWEN_SAMPLE_RATE = 24_000


@dataclass(frozen=True)
class RadioFxConfig:
    """무전 효과 파라미터. 캐시 키에는 `version`을 쓴다(작업 11)."""

    highpass_hz: float = 300.0
    lowpass_hz: float = 3400.0
    noise_level: float = 0.02
    click_level: float = 0.35
    click_ms: float = 15.0
    drive: float = 1.2
    seed: int | None = 0
    taps: int = 257
    version: int = 1


def _bandpass_taps(
    sample_rate: int, low_hz: float, high_hz: float, taps: int
) -> np.ndarray:
    """해밍 창을 쓴 FIR 대역 통과 필터 계수. 중심 주파수 이득을 1로 맞춘다."""
    if taps < 3 or taps % 2 == 0:
        raise ValueError("taps는 3 이상의 홀수여야 합니다")
    if not 0 < low_hz < high_hz < sample_rate / 2:
        raise ValueError("대역은 0 < low < high < Nyquist를 만족해야 합니다")

    n = np.arange(taps) - (taps - 1) / 2.0
    low = low_hz / sample_rate
    high = high_hz / sample_rate
    # 이상적인 대역 통과 임펄스 응답 = 저역 통과(high) - 저역 통과(low)
    h = 2.0 * high * np.sinc(2.0 * high * n) - 2.0 * low * np.sinc(2.0 * low * n)
    h *= np.hamming(taps)

    center = float(np.sqrt(low_hz * high_hz))
    reference = np.exp(-2j * np.pi * center / sample_rate * n)
    gain = float(np.abs(np.dot(h, reference)))
    if gain == 0.0:
        raise ValueError("대역 통과 필터 이득이 0입니다")
    return h / gain


def _click(duration_ms: float, sample_rate: int, level: float) -> np.ndarray:
    """시작 클릭음. 1.2kHz 톤에 빠른 감쇠를 입힌 짧은 소리."""
    count = max(1, int(sample_rate * duration_ms / 1000.0))
    t = np.arange(count) / sample_rate
    decay = np.exp(-t / max(t[-1] / 4.0, 1e-9))
    return level * decay * np.cos(2.0 * np.pi * 1200.0 * t)


def apply_radio_fx(
    pcm: bytes, sample_rate: int, *, config: RadioFxConfig | None = None
) -> bytes:
    """16bit mono PCM에 무전 효과를 입혀 같은 길이의 PCM으로 돌려준다."""
    config = config or RadioFxConfig()
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("sample_rate는 양의 정수여야 합니다")
    if len(pcm) % 2 != 0:
        raise ValueError("16bit PCM 길이는 짝수여야 합니다")
    if not pcm:
        return b""

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0

    # 포화(라디오 특유의 눌린 소리) → 잡음 → 클릭음 순서로 넣고 한 번에 거른다.
    samples = np.tanh(config.drive * samples)
    rng = np.random.default_rng(config.seed)
    samples = samples + rng.normal(0.0, config.noise_level, samples.size)

    click = _click(config.click_ms, sample_rate, config.click_level)
    count = min(click.size, samples.size)
    if count:
        samples[:count] += click[:count]
        # 끝 클릭음은 감쇠를 뒤집어 마지막 샘플에서 크게 끝난다.
        samples[samples.size - count :] += click[:count][::-1]

    taps = _bandpass_taps(sample_rate, config.highpass_hz, config.lowpass_hz, config.taps)
    # numpy의 mode="same"은 입력이 필터보다 짧으면 길이를 늘린다. 항상 같은 길이가
    # 되도록 전체 합성곱을 가운데 기준으로 잘라 낸다.
    filtered = np.convolve(samples, taps, mode="full")
    offset = taps.size // 2
    samples = filtered[offset : offset + samples.size]

    samples = np.clip(samples, -1.0, 1.0)
    return np.round(samples * 32767.0).astype("<i2").tobytes()


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """16bit mono PCM을 WAV 파일 바이트로 감싼다."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    """WAV 바이트에서 (16bit mono PCM, sample_rate)을 꺼낸다."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError("WAV 파일을 읽지 못했습니다") from exc
    if channels != 1 or width != 2:
        raise ValueError("16bit mono WAV만 지원합니다")
    return frames, sample_rate


def apply_radio_fx_wav(
    wav_bytes: bytes, *, config: RadioFxConfig | None = None
) -> bytes:
    """WAV 바이트를 읽어 무전 효과를 입힌 WAV 바이트로 돌려준다."""
    pcm, sample_rate = wav_to_pcm(wav_bytes)
    return pcm_to_wav(apply_radio_fx(pcm, sample_rate, config=config), sample_rate)
