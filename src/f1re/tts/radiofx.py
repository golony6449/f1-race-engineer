"""F1 무전 효과: 대역 필터, 약한 잡음, 시작·끝 클릭음."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import logging
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Qwen TTS가 내보내는 PCM 형식 (24kHz, 16bit, mono).
QWEN_SAMPLE_RATE = 24_000

logger = logging.getLogger(__name__)

# 시작 효과음을 읽지 못했다는 경고는 프로세스에서 한 번만 남긴다.
_intro_warning_logged = False


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


@dataclass(frozen=True)
class IntroSoundConfig:
    """무전 앞에 붙이는 시작 효과음 설정.

    `path`가 없거나 파일을 읽지 못하면 지금의 시작 클릭음으로 대신한다.
    """

    path: str | Path | None = None
    gap_ms: float = 150.0
    gain: float = 0.8


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


def prepend_intro_sound(
    wav_bytes: bytes,
    *,
    intro: IntroSoundConfig,
    config: RadioFxConfig | None = None,
) -> bytes:
    """무전 WAV 앞에 시작 효과음과 `gap_ms` 무음을 붙여 돌려준다.

    효과음에는 무전 효과를 다시 입히지 않는다. 파일이 없거나 읽지 못하면
    지금의 시작 클릭음으로 대신하고 경고를 한 번만 남긴다. 효과음 문제로
    무전이 막히면 안 되므로 예외를 올리지 않는다.
    """
    config = config or RadioFxConfig()
    pcm, sample_rate = wav_to_pcm(wav_bytes)
    voice = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0

    try:
        intro_samples = _load_intro_samples(intro, sample_rate)
    except (OSError, ValueError) as exc:
        _warn_intro_fallback(exc)
        intro_samples = _click(config.click_ms, sample_rate, config.click_level)
    else:
        intro_samples = intro_samples * intro.gain

    gap_samples = max(0, int(round(sample_rate * intro.gap_ms / 1000.0)))
    output = np.concatenate(
        [intro_samples, np.zeros(gap_samples, dtype=np.float64), voice]
    )
    output = np.clip(output, -1.0, 1.0)
    return pcm_to_wav(
        np.round(output * 32767.0).astype("<i2").tobytes(), sample_rate
    )


def effect_version(
    config: RadioFxConfig | None = None,
    *,
    intro: IntroSoundConfig | None = None,
    speed: float = 1.0,
) -> str:
    """캐시 키에 넣을 효과 버전 문자열.

    무전 효과 설정, 시작 효과음 파일의 sha256 앞 12자, 말 속도를 조합한다.
    효과음 파일이 없으면 `none`으로 두어, 나중에 파일이 생기면 버전이 바뀐다.
    """
    config = config or RadioFxConfig()
    payload = {
        "fx": dataclasses.asdict(config),
        "intro_gap_ms": None if intro is None else float(intro.gap_ms),
        "intro_gain": None if intro is None else float(intro.gain),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    return (
        f"fx-{digest}-intro-{_intro_file_digest(intro)}-speed-{float(speed):g}"
    )


def _load_intro_samples(
    intro: IntroSoundConfig, sample_rate: int
) -> np.ndarray:
    """효과음 WAV를 읽어 대상 샘플레이트의 -1..1 샘플로 돌려준다."""
    if intro.path is None:
        raise ValueError("시작 효과음 경로가 없습니다")
    data = Path(intro.path).read_bytes()
    pcm, source_rate = wav_to_pcm(data)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    if samples.size == 0:
        raise ValueError("시작 효과음이 비어 있습니다")
    return _resample(samples, source_rate, sample_rate)


def _resample(
    samples: np.ndarray, source_rate: int, target_rate: int
) -> np.ndarray:
    """선형 보간으로 샘플레이트를 바꾼다. 보통은 같은 24kHz라 그대로 통과한다."""
    if source_rate == target_rate or samples.size == 0:
        return samples
    target_size = max(1, int(round(samples.size * target_rate / source_rate)))
    source_x = np.arange(samples.size, dtype=np.float64)
    target_x = np.linspace(0.0, samples.size - 1.0, target_size)
    return np.interp(target_x, source_x, samples)


def _warn_intro_fallback(reason: object) -> None:
    """효과음 폴백 경고를 프로세스당 한 번만 남긴다."""
    global _intro_warning_logged
    if _intro_warning_logged:
        return
    _intro_warning_logged = True
    logger.warning(
        "시작 효과음을 읽지 못해 기존 시작 클릭음으로 대신합니다: %s", reason
    )


def _intro_file_digest(intro: IntroSoundConfig | None) -> str:
    """효과음 파일 내용의 sha256 앞 12자. 없으면 `none`."""
    if intro is None or intro.path is None:
        return "none"
    try:
        data = Path(intro.path).read_bytes()
    except OSError:
        return "none"
    return hashlib.sha256(data).hexdigest()[:12]
