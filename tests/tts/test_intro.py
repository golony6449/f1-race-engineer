"""시작 효과음, 폴백, 효과 버전, 무전 처리 순서."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import numpy as np
import pytest

from f1re.config import TTSConfig, repo_root
from f1re.tts import radiofx
from f1re.tts.pipeline import (
    intro_from_tts,
    radio_effect_version,
    render_radio_voice,
)
from f1re.tts.radiofx import (
    IntroSoundConfig,
    RadioFxConfig,
    effect_version,
    pcm_to_wav,
    prepend_intro_sound,
    wav_to_pcm,
)

SAMPLE_RATE = 24_000


def _tone_pcm(
    frequency: float = 500.0,
    seconds: float = 0.1,
    amplitude: float = 0.5,
    sample_rate: int = SAMPLE_RATE,
) -> bytes:
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    signal = amplitude * np.sin(2.0 * np.pi * frequency * t)
    return np.round(signal * 32767.0).astype("<i2").tobytes()


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0


def _write_tone(path: Path, **kwargs: float) -> bytes:
    pcm = _tone_pcm(**kwargs)
    path.write_bytes(pcm_to_wav(pcm, SAMPLE_RATE))
    return pcm


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples**2)))


def test_prepend_intro_puts_effect_sound_then_gap_then_voice(tmp_path: Path) -> None:
    intro_pcm = _write_tone(tmp_path / "intro.wav", frequency=500.0, seconds=0.1)
    voice_pcm = _tone_pcm(frequency=1000.0, seconds=0.2, amplitude=0.25)
    intro = IntroSoundConfig(path=tmp_path / "intro.wav", gap_ms=150, gain=0.5)

    output = prepend_intro_sound(
        pcm_to_wav(voice_pcm, SAMPLE_RATE), intro=intro
    )

    pcm, sample_rate = wav_to_pcm(output)
    samples = _samples(pcm)
    intro_len = len(intro_pcm) // 2
    gap_len = int(SAMPLE_RATE * 0.15)
    voice_len = len(voice_pcm) // 2

    assert sample_rate == SAMPLE_RATE
    assert samples.size == intro_len + gap_len + voice_len
    # 효과음은 gain만 곱하고, 무전 필터·잡음·클릭은 다시 입히지 않는다.
    expected_intro = _samples(intro_pcm) * 0.5
    assert np.max(np.abs(samples[:intro_len] - expected_intro)) <= 2.0 / 32768.0
    # 효과음과 무전 사이는 정확히 무음이다.
    assert np.all(samples[intro_len : intro_len + gap_len] == 0.0)
    # 무전 음성은 손대지 않고 뒤에 붙인다.
    assert np.max(
        np.abs(samples[intro_len + gap_len :] - _samples(voice_pcm))
    ) <= 2.0 / 32768.0


@pytest.mark.parametrize("bad_intro", ["missing", "garbage"])
def test_broken_intro_falls_back_to_click_and_warns_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    bad_intro: str,
) -> None:
    monkeypatch.setattr(radiofx, "_intro_warning_logged", False)
    path = tmp_path / "intro.wav"
    if bad_intro == "garbage":
        path.write_bytes(b"not a wav")
    intro = IntroSoundConfig(path=path, gap_ms=100)
    voice_wav = pcm_to_wav(b"\x00\x00" * 2400, SAMPLE_RATE)

    with caplog.at_level(logging.WARNING, logger="f1re.tts.radiofx"):
        first = prepend_intro_sound(voice_wav, intro=intro)
        second = prepend_intro_sound(voice_wav, intro=intro)

    # 무전이 막히지 않고, 같은 클릭음 폴백이 나온다.
    assert first.startswith(b"RIFF")
    assert first == second
    pcm, _ = wav_to_pcm(first)
    samples = _samples(pcm)
    click_len = int(SAMPLE_RATE * RadioFxConfig().click_ms / 1000.0)
    assert samples.size == click_len + int(SAMPLE_RATE * 0.1) + 2400
    assert _rms(samples[:click_len]) > 0.0
    assert np.all(samples[click_len : click_len + int(SAMPLE_RATE * 0.1)] == 0.0)

    warnings = [
        record
        for record in caplog.records
        if "시작 효과음" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_effect_version_combines_settings_file_and_speed(tmp_path: Path) -> None:
    path = tmp_path / "intro.wav"
    _write_tone(path, frequency=500.0, seconds=0.1)
    intro = IntroSoundConfig(path=path, gap_ms=150, gain=0.8)

    base = effect_version(intro=intro, speed=1.2)
    file_digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]

    assert base == effect_version(intro=intro, speed=1.2)
    assert f"intro-{file_digest}" in base
    assert "speed-1.2" in base
    # 속도·효과 설정·파일 내용이 바뀌면 버전이 바뀐다.
    assert base != effect_version(intro=intro, speed=1.1)
    assert base != effect_version(
        RadioFxConfig(seed=1), intro=intro, speed=1.2
    )
    path.write_bytes(pcm_to_wav(_tone_pcm(seconds=0.2), SAMPLE_RATE))
    assert base != effect_version(intro=intro, speed=1.2)
    # 파일이 없으면 none으로 두고, 같은 입력이면 같은 값이다.
    missing = IntroSoundConfig(path=tmp_path / "none.wav")
    assert "intro-none" in effect_version(intro=missing, speed=1.2)
    assert effect_version(intro=missing, speed=1.2) == effect_version(
        intro=missing, speed=1.2
    )


def test_render_radio_voice_runs_speed_fx_then_intro(tmp_path: Path) -> None:
    intro_pcm = _write_tone(tmp_path / "intro.wav", frequency=500.0, seconds=0.1)
    voice_pcm = _tone_pcm(frequency=1000.0, seconds=0.6, amplitude=0.25)
    config = TTSConfig(
        speed=1.2,
        intro_sound_path=str(tmp_path / "intro.wav"),
        intro_gap_ms=150,
        intro_gain=0.5,
    )

    output = render_radio_voice(
        pcm_to_wav(voice_pcm, SAMPLE_RATE), config, fx_config=RadioFxConfig(seed=0)
    )

    pcm, sample_rate = wav_to_pcm(output)
    samples = _samples(pcm)
    intro_len = len(intro_pcm) // 2
    gap_len = int(SAMPLE_RATE * 0.15)
    expected_voice_len = len(voice_pcm) / 2 / 1.2
    voice_len = samples.size - intro_len - gap_len

    assert sample_rate == SAMPLE_RATE
    # 속도 1.2가 적용됐다(±5%).
    assert voice_len == pytest.approx(expected_voice_len, rel=0.05)
    # 앞에는 효과음, 그다음 무음, 그다음 무전 음성 순서다.
    expected_intro = _samples(intro_pcm) * 0.5
    assert np.max(np.abs(samples[:intro_len] - expected_intro)) <= 2.0 / 32768.0
    assert np.all(samples[intro_len : intro_len + gap_len] == 0.0)
    voice = samples[intro_len + gap_len :]
    assert _rms(voice) > 0.05


def test_render_radio_voice_survives_missing_intro(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(radiofx, "_intro_warning_logged", False)
    voice_wav = pcm_to_wav(_tone_pcm(seconds=0.2), SAMPLE_RATE)
    config = TTSConfig(
        speed=1.0,
        intro_sound_path=str(tmp_path / "missing.wav"),
    )

    with caplog.at_level(logging.WARNING, logger="f1re.tts.radiofx"):
        output = render_radio_voice(
            voice_wav, config, fx_config=RadioFxConfig(seed=0)
        )

    assert output.startswith(b"RIFF")
    assert any("시작 효과음" in record.getMessage() for record in caplog.records)


def test_radio_effect_version_matches_render_inputs(tmp_path: Path) -> None:
    intro_path = tmp_path / "intro.wav"
    _write_tone(intro_path, seconds=0.1)
    config = TTSConfig(
        speed=1.2,
        intro_sound_path=str(intro_path),
        intro_gap_ms=120,
        intro_gain=0.7,
    )
    fx = RadioFxConfig(seed=3)
    intro = intro_from_tts(config)

    assert radio_effect_version(config, fx_config=fx, intro=intro) == (
        effect_version(fx, intro=intro, speed=1.2)
    )


def test_intro_from_tts_resolves_relative_path() -> None:
    intro = intro_from_tts(TTSConfig())

    assert Path(intro.path) == repo_root() / "assets" / "radio-intro.wav"
    assert intro.gap_ms == pytest.approx(150.0)
    assert intro.gain == pytest.approx(0.8)
