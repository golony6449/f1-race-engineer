"""합성 음성을 무전 오디오로 만드는 처리 순서.

합성 WAV → 말 속도(WSOLA) → 무전 효과 → 앞에 시작 효과음.
모든 무전이 이 경로를 지나므로 시작 효과음도 항상 붙는다.
"""

from __future__ import annotations

from f1re.config import TTSConfig, resolve_asset_path
from f1re.tts.radiofx import (
    IntroSoundConfig,
    RadioFxConfig,
    apply_radio_fx_wav,
    effect_version,
    prepend_intro_sound,
)
from f1re.tts.stretch import time_stretch_wav


def intro_from_tts(config: TTSConfig) -> IntroSoundConfig:
    """TTS 설정에서 시작 효과음 설정을 만든다. 상대 경로는 레포 루트 기준."""
    return IntroSoundConfig(
        path=resolve_asset_path(config.intro_sound_path),
        gap_ms=config.intro_gap_ms,
        gain=config.intro_gain,
    )


def render_radio_voice(
    wav_bytes: bytes,
    config: TTSConfig,
    *,
    fx_config: RadioFxConfig | None = None,
    intro: IntroSoundConfig | None = None,
) -> bytes:
    """합성 WAV를 무전용 WAV로 만든다."""
    resolved_intro = intro if intro is not None else intro_from_tts(config)
    stretched = time_stretch_wav(wav_bytes, config.speed)
    with_fx = apply_radio_fx_wav(stretched, config=fx_config)
    return prepend_intro_sound(with_fx, intro=resolved_intro, config=fx_config)


def radio_effect_version(
    config: TTSConfig,
    *,
    fx_config: RadioFxConfig | None = None,
    intro: IntroSoundConfig | None = None,
) -> str:
    """캐시 키에 넣을 효과 버전. 실제 렌더와 같은 입력으로 계산한다."""
    resolved_intro = intro if intro is not None else intro_from_tts(config)
    return effect_version(fx_config, intro=resolved_intro, speed=config.speed)
