"""LLM 무전 생성 설정 로드·검증 (SCRUM-129)."""

from __future__ import annotations

from pathlib import Path

import pytest

from f1re.config import ConfigError, LLMConfig, load_config

ENDPOINT = (
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_default_config_has_llm_defaults() -> None:
    config = load_config()

    assert config.llm.enabled is True
    assert config.llm.model == "qwen3.7-flash-2026-07-15"
    assert config.llm.endpoint == ENDPOINT
    assert config.llm.timeout_s == pytest.approx(1.5)
    assert config.llm.secret_key == "qwen"
    assert config.llm.max_tokens == 80
    assert config.llm.temperature == pytest.approx(0.3)


def test_missing_llm_section_keeps_defaults(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "[global]\nudp_port = 30000\n"))

    assert config.udp_port == 30000
    assert config.llm == LLMConfig()


def test_llm_values_are_loaded(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            '[llm]\n'
            "enabled = false\n"
            'model = "qwen-test"\n'
            'endpoint = "https://example.test/v1/chat/completions"\n'
            "timeout_s = 0.8\n"
            'secret_key = "qwen"\n'
            "max_tokens = 40\n"
            "temperature = 1.1\n",
        )
    )

    assert config.llm.enabled is False
    assert config.llm.model == "qwen-test"
    assert config.llm.endpoint == "https://example.test/v1/chat/completions"
    assert config.llm.timeout_s == pytest.approx(0.8)
    assert config.llm.max_tokens == 40
    assert config.llm.temperature == pytest.approx(1.1)


def test_unknown_llm_keys_are_ignored(tmp_path: Path) -> None:
    config = load_config(
        _write(tmp_path, '[llm]\nfuture_option = "x"\nmodel = "qwen-test"\n')
    )

    assert config.llm.model == "qwen-test"
    assert config.llm.timeout_s == pytest.approx(1.5)


@pytest.mark.parametrize("temperature", [0.0, 2.0])
def test_temperature_bounds_are_accepted(
    tmp_path: Path, temperature: float
) -> None:
    config = load_config(_write(tmp_path, f"[llm]\ntemperature = {temperature}\n"))

    assert config.llm.temperature == pytest.approx(temperature)


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("[llm]\ntimeout_s = 0\n", "llm.timeout_s"),
        ("[llm]\ntimeout_s = -0.1\n", "llm.timeout_s"),
        ("[llm]\nmax_tokens = 0\n", "llm.max_tokens"),
        ("[llm]\nmax_tokens = -5\n", "llm.max_tokens"),
        ("[llm]\ntemperature = -0.1\n", "llm.temperature"),
        ("[llm]\ntemperature = 2.1\n", "llm.temperature"),
        ('[llm]\nmodel = ""\n', "llm.model"),
        ('[llm]\nendpoint = ""\n', "llm.endpoint"),
        ('[llm]\nsecret_key = ""\n', "llm.secret_key"),
    ],
)
def test_invalid_llm_settings_are_rejected(
    tmp_path: Path, toml: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, toml))


def test_llm_must_be_a_table(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="llm"):
        load_config(_write(tmp_path, "llm = 3\n"))
