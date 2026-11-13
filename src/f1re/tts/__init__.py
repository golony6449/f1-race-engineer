"""TTS: Qwen 클라이언트와 무전 효과.

웹 서버는 레이스 엔지니어가 꺼져 있어도 항상 떠 있다(SCRUM-82). 이 패키지를
`f1re.tts.qwen`처럼 하위 모듈로만 불러오는 경우까지 numpy(무전 효과 처리)와
httpx(Qwen)를 서버 시작 경로에서 끌어오지 않도록, 공개 이름은 처음 쓸 때
import한다(PEP 562 지연 import).
"""

from __future__ import annotations

import importlib
from typing import Any

# 공개 이름 → 실제 정의가 있는 모듈.
_EXPORTS: dict[str, str] = {
    "MAX_TEXT_CHARS": "f1re.tts.qwen",
    "QwenTTS": "f1re.tts.qwen",
    "TTSError": "f1re.tts.qwen",
    "TTSInvalidRequest": "f1re.tts.qwen",
    "TTSUnavailable": "f1re.tts.qwen",
    "QWEN_SAMPLE_RATE": "f1re.tts.radiofx",
    "RadioFxConfig": "f1re.tts.radiofx",
    "apply_radio_fx": "f1re.tts.radiofx",
    "apply_radio_fx_wav": "f1re.tts.radiofx",
    "pcm_to_wav": "f1re.tts.radiofx",
    "wav_to_pcm": "f1re.tts.radiofx",
}

__all__ = [
    "MAX_TEXT_CHARS",
    "QWEN_SAMPLE_RATE",
    "QwenTTS",
    "RadioFxConfig",
    "TTSError",
    "TTSInvalidRequest",
    "TTSUnavailable",
    "apply_radio_fx",
    "apply_radio_fx_wav",
    "pcm_to_wav",
    "wav_to_pcm",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
