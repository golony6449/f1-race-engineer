"""설정 파일(`config/default.toml`)을 읽어 데이터클래스로 만든다."""

from __future__ import annotations

import dataclasses
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """설정 파일이 없거나 값이 잘못됐을 때."""


@dataclass(frozen=True)
class TTSConfig:
    """Qwen TTS 클라이언트 설정."""

    model: str = "qwen3-tts-flash"
    realtime_model: str = "qwen3-tts-flash-realtime"
    voice: str = "Katerina"
    speed: float = 1.2
    timeout_s: float = 2.0
    secret_key: str = "qwen"
    endpoint: str = (
        "https://dashscope-intl.aliyuncs.com"
        "/api/v1/services/aigc/multimodal-generation/generation"
    )
    language_type: str = "Korean"
    intro_sound_url: str = (
        "https://www.myinstants.com/media/sounds/formula-1-radio-notification.mp3"
    )
    intro_sound_path: str = "assets/radio-intro.wav"
    intro_gap_ms: float = 150.0
    intro_gain: float = 0.8


@dataclass(frozen=True)
class WebConfig:
    """대시보드 웹 서버 설정. 화면(태블릿·옆 모니터)이 접속하는 주소다."""

    host: str = "0.0.0.0"
    port: int = 8093


@dataclass(frozen=True)
class AppConfig:
    """앱 전체 설정. `[global]` 섹션 값은 평평하게 둔다."""

    tts: TTSConfig = dataclasses.field(default_factory=TTSConfig)
    web: WebConfig = dataclasses.field(default_factory=WebConfig)
    focus_corner_count: int = 5
    focus_recent_laps: int = 3
    big_loss_s: float = 0.3
    defer_s: float = 3.0
    overtake_gap_s: float = 1.0
    overtake_per_lap: int = 1
    udp_port: int = 20777


def repo_root() -> Path:
    """레포 루트. 설정에 적은 상대 경로(assets/ 등)의 기준이다."""
    return Path(__file__).resolve().parents[2]


def resolve_asset_path(path: str | Path) -> Path:
    """설정의 경로를 레포 루트 기준 절대 경로로 바꾼다."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else repo_root() / candidate


def default_config_path() -> Path:
    """레포의 `config/default.toml` 경로. 소스에서 실행할 때를 기준으로 한다."""
    return repo_root() / "config" / "default.toml"


def load_config(path: str | Path | None = None) -> AppConfig:
    """TOML 설정을 읽는다. `path`가 없으면 레포 기본 설정을 읽는다."""
    config_path = Path(path) if path is not None else default_config_path()
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"설정 파일을 읽지 못했습니다: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"설정 파일 형식이 잘못됐습니다: {config_path}") from exc

    global_values = raw.get("global", {})
    tts_values = raw.get("tts", {})
    web_values = raw.get("web", {})
    if (
        not isinstance(global_values, dict)
        or not isinstance(tts_values, dict)
        or not isinstance(web_values, dict)
    ):
        raise ConfigError("[global], [tts], [web]은 표(table)여야 합니다")

    config = AppConfig(
        tts=_build(TTSConfig, tts_values, "tts"),
        web=_build(WebConfig, web_values, "web"),
        **_known_fields(AppConfig, global_values, "global", skip={"tts", "web"}),
    )
    _validate(config)
    return config


def _build(cls: type, values: dict[str, Any], section: str) -> Any:
    return cls(**_known_fields(cls, values, section))


def _known_fields(
    cls: type, values: dict[str, Any], section: str, *, skip: set[str] | None = None
) -> dict[str, Any]:
    """데이터클래스에 있는 키만 골라 낸다. 모르는 키는 무시한다."""
    names = {field.name for field in dataclasses.fields(cls)} - (skip or set())
    return {key: value for key, value in values.items() if key in names}


def _validate(config: AppConfig) -> None:
    if config.tts.timeout_s <= 0:
        raise ConfigError("tts.timeout_s는 0보다 커야 합니다")
    if not math.isfinite(config.tts.speed) or config.tts.speed <= 0:
        raise ConfigError("tts.speed는 0보다 큰 유한한 수여야 합니다")
    if not math.isfinite(config.tts.intro_gap_ms) or config.tts.intro_gap_ms < 0:
        raise ConfigError("tts.intro_gap_ms는 0 이상이어야 합니다")
    if not math.isfinite(config.tts.intro_gain) or config.tts.intro_gain < 0:
        raise ConfigError("tts.intro_gain은 0 이상이어야 합니다")
    if not 1 <= config.udp_port <= 65535:
        raise ConfigError("global.udp_port는 1~65535여야 합니다")
    if not config.web.host.strip():
        raise ConfigError("web.host가 비어 있습니다")
    if not 1 <= config.web.port <= 65535:
        raise ConfigError("web.port는 1~65535여야 합니다")
    if config.focus_corner_count < 1:
        raise ConfigError("global.focus_corner_count는 1 이상이어야 합니다")
    if config.focus_recent_laps < 1:
        raise ConfigError("global.focus_recent_laps는 1 이상이어야 합니다")
    if not config.tts.model:
        raise ConfigError("tts.model이 비어 있습니다")
    if not config.tts.voice:
        raise ConfigError("tts.voice가 비어 있습니다")
