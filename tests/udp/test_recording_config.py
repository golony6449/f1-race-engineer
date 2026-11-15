"""자동 녹화 설정(`[recording]`, `[recording.gcs]`)의 기본값과 검증(설계 4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from f1re.config import ConfigError, RecordingConfig, load_config


def test_load_recording_config_defaults() -> None:
    config = load_config()

    assert config.recording.enabled is True
    assert config.recording.dir == "data/recordings"
    assert config.recording.keep_local_count == 5
    assert config.recording.keep_cache_count == 3
    assert config.recording.inactivity_timeout_s == pytest.approx(600.0)
    assert config.recording.exclude_demo is True
    assert config.recording.min_packets == 100

    # GCS 백업 기본값(설계 4.3). 홈서버에 rclone이 없어도 설정 자체는 이 값이다.
    assert config.recording.gcs.enabled is True
    assert config.recording.gcs.rclone_remote == "gcs"
    assert config.recording.gcs.bucket == "f1-race-engineer"
    assert config.recording.gcs.prefix == "telemetry-raw"
    assert config.recording.gcs.rclone_config == "~/.config/rclone/rclone.conf"


def test_missing_recording_section_keeps_safe_defaults(tmp_path: Path) -> None:
    """`[recording]`이 없는 설정 파일은 GCS를 끈 안전한 기본값을 쓴다."""
    path = tmp_path / "config.toml"
    path.write_text("[global]\nudp_port = 30000\n", encoding="utf-8")

    config = load_config(path)

    assert config.recording == RecordingConfig()
    # 설정에 GCS 섹션이 없으면 업로드를 시도하지 않는다(로컬 녹화만).
    assert config.recording.enabled is True
    assert config.recording.gcs.enabled is False


def test_recording_partial_toml_keeps_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[recording]\n"
        "enabled = false\n"
        "keep_local_count = 2\n"
        "\n"
        "[recording.gcs]\n"
        'bucket = "my-bucket"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.recording.enabled is False
    assert config.recording.keep_local_count == 2
    # 적지 않은 값은 기본값을 유지한다.
    assert config.recording.keep_cache_count == 3
    assert config.recording.inactivity_timeout_s == pytest.approx(600.0)
    assert config.recording.gcs.bucket == "my-bucket"
    assert config.recording.gcs.rclone_remote == "gcs"


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("[recording]\nkeep_local_count = 0\n", "keep_local_count"),
        ("[recording]\nkeep_local_count = -1\n", "keep_local_count"),
        ("[recording]\nkeep_cache_count = -1\n", "keep_cache_count"),
        ("[recording]\ninactivity_timeout_s = 0\n", "inactivity_timeout_s"),
        ('[recording]\ninactivity_timeout_s = -5.0\n', "inactivity_timeout_s"),
        ("[recording]\ninactivity_timeout_s = inf\n", "inactivity_timeout_s"),
        ("[recording]\nmin_packets = 0\n", "min_packets"),
        ("[recording]\nmin_packets = -3\n", "min_packets"),
        ('[recording]\ndir = "  "\n', "dir"),
    ],
)
def test_load_recording_config_rejects_invalid_values(
    tmp_path: Path, toml: str, message: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(toml, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_recording_accepts_boundary_values(tmp_path: Path) -> None:
    """캐시 0개, 짧은 무수신 타임아웃은 정상 값이다."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[recording]\n"
        "keep_cache_count = 0\n"
        "inactivity_timeout_s = 0.5\n"
        "min_packets = 1\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.recording.keep_cache_count == 0
    assert config.recording.inactivity_timeout_s == pytest.approx(0.5)
    assert config.recording.min_packets == 1


def test_recording_section_must_be_a_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("recording = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="recording"):
        load_config(path)


def test_recording_gcs_section_must_be_a_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[recording]\ngcs = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"recording\.gcs"):
        load_config(path)
