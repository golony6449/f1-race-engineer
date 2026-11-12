"""설정 기본값과 로드 동작."""

from __future__ import annotations

from pathlib import Path

import pytest

from f1re.config import ConfigError, TTSConfig, load_config


def test_default_config_matches_global_constraints() -> None:
    config = load_config()

    # Global Constraints의 기본값
    assert config.focus_corner_count == 5
    assert config.focus_recent_laps == 3
    assert config.big_loss_s == pytest.approx(0.3)
    assert config.defer_s == pytest.approx(3.0)
    assert config.overtake_gap_s == pytest.approx(1.0)
    assert config.overtake_per_lap == 1
    assert config.udp_port == 20777

    # TTS 설정
    assert config.tts.model == "qwen3-tts-flash"
    assert config.tts.realtime_model == "qwen3-tts-flash-realtime"
    assert config.tts.voice == "Sohee"
    assert config.tts.timeout_s == pytest.approx(2.0)
    assert config.tts.secret_key == "qwen"
    assert config.tts.language_type == "Korean"
    assert config.tts.endpoint.startswith("https://dashscope-intl.aliyuncs.com/")


def test_partial_toml_keeps_other_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[global]\nudp_port = 30000\n\n[tts]\ntimeout_s = 0.5\nvoice = "Cherry"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.udp_port == 30000
    assert config.tts.timeout_s == pytest.approx(0.5)
    assert config.tts.voice == "Cherry"
    # 적지 않은 값은 기본값을 유지한다.
    assert config.focus_corner_count == 5
    assert config.tts.model == TTSConfig().model


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[global]\ntypo_key = 1\n\n[tts]\nfuture_option = "x"\n', encoding="utf-8"
    )

    config = load_config(path)

    assert config.focus_corner_count == 5
    assert config.tts.model == "qwen3-tts-flash"


def test_invalid_timeout_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[tts]\ntimeout_s = 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="timeout_s"):
        load_config(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="읽지 못했습니다"):
        load_config(tmp_path / "nope.toml")


def test_broken_toml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[tts\nmodel = ", encoding="utf-8")

    with pytest.raises(ConfigError, match="형식"):
        load_config(path)
