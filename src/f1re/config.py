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
class LLMConfig:
    """Qwen LLM 무전 문장 생성 설정(SCRUM-129).

    판단과 숫자는 규칙 엔진이 정하고, LLM은 주어진 사실(facts)과 KB 문맥만으로
    짧은 한국어 무전 문장을 만든다. 급하지 않은 무전(추월·방어 전략 등)에 쓴다.
    요청이 `timeout_s` 안에 끝나지 않거나 출력 검증에 걸리면 미리 만든
    fallback 문장으로 대체하므로 무전 큐가 막히지 않는다.
    """

    enabled: bool = True
    model: str = "qwen3.7-flash-2026-07-15"
    endpoint: str = (
        "https://dashscope-intl.aliyuncs.com"
        "/compatible-mode/v1/chat/completions"
    )
    timeout_s: float = 1.5
    secret_key: str = "qwen"
    max_tokens: int = 80
    temperature: float = 0.3


@dataclass(frozen=True)
class WebConfig:
    """대시보드 웹 서버 설정. 화면(태블릿·옆 모니터)이 접속하는 주소다."""

    host: str = "0.0.0.0"
    port: int = 8093


@dataclass(frozen=True)
class RadioConfig:
    """무전 전달 계층 설정(설계 6.7).

    안전 경고·박스 지시는 앞 무전이 재생되는 동안 기다려도 만료되지 않도록
    충분한 유효 시간을 둔다(SCRUM-81). 코칭·추월처럼 시점이 중요한 무전의
    유효 시간은 각 모듈이 정한다.
    """

    safety_ttl_s: float = 30.0
    box_ttl_s: float = 30.0


@dataclass(frozen=True)
class RecordingGcsConfig:
    """녹화 파일 GCS 백업 설정(설계 4.3). 업로드는 시스템 `rclone` CLI가 한다.

    기본값은 GCS를 끈 상태다. `[recording.gcs]` 섹션이 없는 설정 파일로
    실행하면 로컬 녹화만 하고 업로드는 시도하지 않는다.
    """

    enabled: bool = False
    rclone_remote: str = "gcs"
    bucket: str = ""
    prefix: str = "telemetry-raw"
    rclone_config: str = "~/.config/rclone/rclone.conf"


@dataclass(frozen=True)
class RecordingConfig:
    """세션별 원시 텔레메트리 자동 녹화 설정(설계 4.3).

    `keep_local_count`는 GCS 업로드가 끝난 녹화 중 로컬에 남길 최신 개수,
    `keep_cache_count`는 GCS 온디맨드 복원 캐시(`.cache/`)의 상한이다.
    `inactivity_timeout_s` 동안 패킷이 없으면 녹화를 마감하고, 같은 세션이
    다시 시작되면 `segment`를 늘려 새 파일로 이어 받는다.
    """

    enabled: bool = True
    dir: str = "data/recordings"
    keep_local_count: int = 5
    keep_cache_count: int = 3
    inactivity_timeout_s: float = 600.0
    exclude_demo: bool = True
    min_packets: int = 100
    gcs: RecordingGcsConfig = dataclasses.field(default_factory=RecordingGcsConfig)


@dataclass(frozen=True)
class AppConfig:
    """앱 전체 설정. `[global]` 섹션 값은 평평하게 둔다."""

    tts: TTSConfig = dataclasses.field(default_factory=TTSConfig)
    web: WebConfig = dataclasses.field(default_factory=WebConfig)
    radio: RadioConfig = dataclasses.field(default_factory=RadioConfig)
    recording: RecordingConfig = dataclasses.field(default_factory=RecordingConfig)
    llm: LLMConfig = dataclasses.field(default_factory=LLMConfig)
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
    radio_values = raw.get("radio", {})
    recording_values = raw.get("recording", {})
    llm_values = raw.get("llm", {})
    if (
        not isinstance(global_values, dict)
        or not isinstance(tts_values, dict)
        or not isinstance(web_values, dict)
        or not isinstance(radio_values, dict)
        or not isinstance(recording_values, dict)
        or not isinstance(llm_values, dict)
    ):
        raise ConfigError(
            "[global], [tts], [web], [radio], [recording], [llm]는 "
            "표(table)여야 합니다"
        )

    config = AppConfig(
        tts=_build(TTSConfig, tts_values, "tts"),
        web=_build(WebConfig, web_values, "web"),
        radio=_build(RadioConfig, radio_values, "radio"),
        recording=_build_recording(recording_values),
        llm=_build(LLMConfig, llm_values, "llm"),
        **_known_fields(
            AppConfig,
            global_values,
            "global",
            skip={"tts", "web", "radio", "recording", "llm"},
        ),
    )
    _validate(config)
    return config


def _build(cls: type, values: dict[str, Any], section: str) -> Any:
    return cls(**_known_fields(cls, values, section))


def _build_recording(values: dict[str, Any]) -> RecordingConfig:
    """`[recording]`과 그 아래 `[recording.gcs]` 표를 데이터클래스로 만든다."""
    fields = _known_fields(RecordingConfig, values, "recording")
    gcs_values = values.get("gcs")
    if gcs_values is not None:
        if not isinstance(gcs_values, dict):
            raise ConfigError("[recording.gcs]는 표(table)여야 합니다")
        fields["gcs"] = _build(RecordingGcsConfig, gcs_values, "recording.gcs")
    return RecordingConfig(**fields)


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
    if (
        not math.isfinite(config.radio.safety_ttl_s)
        or config.radio.safety_ttl_s <= 0
    ):
        raise ConfigError("radio.safety_ttl_s는 0보다 큰 유한한 수여야 합니다")
    if not math.isfinite(config.radio.box_ttl_s) or config.radio.box_ttl_s <= 0:
        raise ConfigError("radio.box_ttl_s는 0보다 큰 유한한 수여야 합니다")

    # 자동 녹화 설정(설계 4.3). 잘못된 값은 녹화 시작 전에 막는다.
    recording = config.recording
    if not recording.dir.strip():
        raise ConfigError("recording.dir이 비어 있습니다")
    if recording.keep_local_count < 1:
        raise ConfigError("recording.keep_local_count는 1 이상이어야 합니다")
    if recording.keep_cache_count < 0:
        raise ConfigError("recording.keep_cache_count는 0 이상이어야 합니다")
    if (
        not math.isfinite(recording.inactivity_timeout_s)
        or recording.inactivity_timeout_s <= 0
    ):
        raise ConfigError(
            "recording.inactivity_timeout_s는 0보다 큰 유한한 수여야 합니다"
        )
    if recording.min_packets < 1:
        raise ConfigError("recording.min_packets는 1 이상이어야 합니다")

    # LLM 무전 문장 생성 설정(SCRUM-129).
    llm = config.llm
    if not llm.model.strip():
        raise ConfigError("llm.model이 비어 있습니다")
    if not llm.endpoint.strip():
        raise ConfigError("llm.endpoint가 비어 있습니다")
    if not llm.secret_key.strip():
        raise ConfigError("llm.secret_key가 비어 있습니다")
    if not math.isfinite(llm.timeout_s) or llm.timeout_s <= 0:
        raise ConfigError("llm.timeout_s는 0보다 큰 유한한 수여야 합니다")
    if llm.max_tokens < 1:
        raise ConfigError("llm.max_tokens는 1 이상이어야 합니다")
    if not math.isfinite(llm.temperature) or not 0.0 <= llm.temperature <= 2.0:
        raise ConfigError("llm.temperature는 0.0~2.0이어야 합니다")
