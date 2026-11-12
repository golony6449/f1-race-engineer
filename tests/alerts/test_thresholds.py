"""위험 기준 단일 출처: 문턱값 판정, 설정 연동, 색 표.

공용 conftest를 만들지 않는 규칙에 따라 설정 읽기 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import json
import math
import tomllib
from pathlib import Path

import pytest

from f1re.alerts.thresholds import (
    Direction,
    Severity,
    color_table,
    default_thresholds,
    load_thresholds,
    metric_threshold,
    severity,
)
from f1re.config import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "default.toml"

# 리프트 앤 코스트(SCRUM-71)가 WARN 문턱값을 읽어 가는 지표들.
LIFT_AND_COAST_METRICS = ("brake_temp", "tyre_temp", "tyre_wear", "fuel_laps_margin")

HIGH_METRICS = tuple(
    name
    for name, spec in default_thresholds().metrics.items()
    if spec.direction is Direction.HIGH
)
LOW_METRICS = tuple(
    name
    for name, spec in default_thresholds().metrics.items()
    if spec.direction is Direction.LOW
)


def _config_thresholds() -> dict:
    """`config/default.toml`의 `[thresholds]`를 그대로 읽는다."""
    with CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)["thresholds"]


# ---------------------------------------------------------------------------
# 경계값 판정


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, Severity.OK),
        (24.99, Severity.OK),
        (25.0, Severity.WARN),
        (49.99, Severity.WARN),
        (50.0, Severity.DANGER),
        (100.0, Severity.DANGER),
    ],
)
def test_front_wing_damage_warn_25_danger_50(
    value: float, expected: Severity
) -> None:
    """계획 작업 8이 정한 앞날개 손상 경고 25% / 위험 50%."""
    assert severity("front_wing_damage", value, {}) is expected


def test_tyre_wear_thresholds_come_from_config() -> None:
    raw = _config_thresholds()["tyre_wear"]
    warn = float(raw["warn"])
    danger = float(raw["danger"])

    assert severity("tyre_wear", warn - 0.1, {}) is Severity.OK
    assert severity("tyre_wear", warn, {}) is Severity.WARN
    assert severity("tyre_wear", danger - 0.1, {}) is Severity.WARN
    assert severity("tyre_wear", danger, {}) is Severity.DANGER


@pytest.mark.parametrize("metric", HIGH_METRICS)
def test_high_metrics_follow_config_bands(metric: str) -> None:
    raw = _config_thresholds()[metric]
    warn = float(raw["warn"])
    danger = float(raw["danger"])

    assert severity(metric, warn - 1.0, {}) is Severity.OK
    assert severity(metric, warn, {}) is Severity.WARN
    assert severity(metric, (warn + danger) / 2.0, {}) is Severity.WARN
    assert severity(metric, danger, {}) is Severity.DANGER


@pytest.mark.parametrize("metric", LOW_METRICS)
def test_low_metrics_follow_config_bands(metric: str) -> None:
    raw = _config_thresholds()[metric]
    warn = float(raw["warn"])
    danger = float(raw["danger"])

    assert severity(metric, warn, {}) is Severity.OK
    assert severity(metric, (warn + danger) / 2.0, {}) is Severity.WARN
    # 경계값 자체는 아직 DANGER가 아니다(설계 6.6: 남은 랩 < 남은 레이스 랩).
    assert severity(metric, danger, {}) is Severity.WARN
    assert severity(metric, danger - 0.01, {}) is Severity.DANGER


def test_fuel_laps_margin_danger_only_when_negative() -> None:
    assert severity("fuel_laps_margin", -1.0, {}) is Severity.DANGER
    assert severity("fuel_laps_margin", -0.01, {}) is Severity.DANGER
    assert severity("fuel_laps_margin", 0.0, {}) is Severity.WARN
    assert severity("fuel_laps_margin", 0.5, {}) is Severity.OK


def test_fuel_margin_can_be_derived_from_context() -> None:
    assert (
        severity(
            "fuel_laps_margin",
            None,
            {"fuel_remaining_laps": 9.0, "remaining_race_laps": 10.0},
        )
        is Severity.DANGER
    )
    assert (
        severity(
            "fuel_laps_margin",
            None,
            {"fuel_remaining_laps": 10.2, "remaining_race_laps": 10.0},
        )
        is Severity.WARN
    )
    assert (
        severity(
            "fuel_laps_margin",
            None,
            {"fuel_remaining_laps": 10.6, "remaining_race_laps": 10.0},
        )
        is Severity.OK
    )
    # 문맥이 모자라면 값을 계산하지 않는다.
    assert (
        severity("fuel_laps_margin", None, {"fuel_remaining_laps": 10.0})
        is Severity.NO_DATA
    )


@pytest.mark.parametrize("metric", tuple(default_thresholds().metrics))
def test_none_and_non_finite_are_no_data(metric: str) -> None:
    assert severity(metric, None, {}) is Severity.NO_DATA
    assert severity(metric, math.nan, {}) is Severity.NO_DATA
    assert severity(metric, math.inf, {}) is Severity.NO_DATA


def test_unknown_metric_raises() -> None:
    with pytest.raises(KeyError):
        severity("not_a_metric", 1.0, {})
    with pytest.raises(KeyError):
        metric_threshold("not_a_metric")


# ---------------------------------------------------------------------------
# 색 표


def test_color_table_matches_severity() -> None:
    """표에 적힌 문턱값과 `severity` 판정 결과가 같은 지점에서 바뀐다."""
    table = color_table()
    for name, entry in table["metrics"].items():
        warn = entry["warn"]
        danger = entry["danger"]
        if entry["direction"] == Direction.HIGH.value:
            assert severity(name, warn - 1.0, {}) is Severity.OK
            assert severity(name, warn, {}) is Severity.WARN
            assert severity(name, danger, {}) is Severity.DANGER
        else:
            assert severity(name, warn, {}) is Severity.OK
            assert severity(name, danger, {}) is Severity.WARN
            assert severity(name, danger - 0.01, {}) is Severity.DANGER

    # 화면에서 빨간색이 되는 단계가 곧 음성 경고 조건이다.
    assert table["alert_severity"] == Severity.DANGER.value
    assert table["colors"][Severity.DANGER.value] == _config_thresholds()["colors"][
        Severity.DANGER.value
    ]


def test_config_file_is_the_only_threshold_source() -> None:
    """색 표의 모든 문턱값·색이 설정 파일 값과 같다."""
    raw = _config_thresholds()
    table = color_table()

    assert set(table["metrics"]) == set(raw) - {"colors"}
    for name, entry in table["metrics"].items():
        assert entry["warn"] == pytest.approx(float(raw[name]["warn"]))
        assert entry["danger"] == pytest.approx(float(raw[name]["danger"]))
    assert table["colors"] == dict(raw["colors"])


def test_color_table_covers_car_state_screen_metrics() -> None:
    """설계 6.9 차량 상태 화면이 색을 입힐 지표가 모두 표에 있다."""
    expected = {
        "tyre_wear",
        "tyre_temp",
        "tyre_inner_temp",
        "brake_temp",
        "front_left_wing_damage",
        "front_right_wing_damage",
        "rear_wing_damage",
        "floor_damage",
        "diffuser_damage",
        "sidepod_damage",
        "gearbox_damage",
        "engine_damage",
        "engine_mguh_wear",
        "engine_es_wear",
        "engine_ce_wear",
        "engine_ice_wear",
        "engine_mguk_wear",
        "engine_tc_wear",
        "engine_temp",
        "fuel_laps_margin",
    }
    assert expected <= set(color_table()["metrics"])


def test_color_table_is_json_serializable() -> None:
    payload = json.dumps(color_table(), ensure_ascii=False)
    assert "tyre_wear" in payload
    assert "alert_severity" in payload


def test_color_table_returns_a_fresh_copy() -> None:
    first = color_table()
    first["colors"]["danger"] = "#000000"
    first["metrics"]["tyre_wear"]["warn"] = 0.0

    second = color_table()
    assert second["colors"]["danger"] != "#000000"
    assert second["metrics"]["tyre_wear"]["warn"] != 0.0


# ---------------------------------------------------------------------------
# 설정 검증과 리프트 앤 코스트용 WARN 문턱값


@pytest.mark.parametrize("metric", LIFT_AND_COAST_METRICS)
def test_lift_and_coast_can_read_warn_thresholds(metric: str) -> None:
    raw = _config_thresholds()[metric]
    assert "warn" in raw

    threshold = metric_threshold(metric)
    assert math.isfinite(threshold.warn)
    assert threshold.warn != threshold.danger


def test_load_thresholds_rejects_missing_section(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[global]\nudp_port = 20777\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="thresholds"):
        load_thresholds(path)


def test_load_thresholds_rejects_inverted_bands(tmp_path: Path) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    broken = text.replace(
        "[thresholds.front_wing_damage]\nwarn = 25.0\ndanger = 50.0",
        "[thresholds.front_wing_damage]\nwarn = 60.0\ndanger = 50.0",
    )
    assert broken != text
    path = tmp_path / "config.toml"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(ConfigError, match="front_wing_damage"):
        load_thresholds(path)
