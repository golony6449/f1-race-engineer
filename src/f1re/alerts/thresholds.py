"""위험 기준 단일 출처. 화면 색과 음성 경고가 같은 문턱값을 쓴다.

설계 6.6: 위험 문턱값과 화면 색 기준을 서버의 설정 하나
(`config/default.toml`의 `[thresholds]`)로 두고, 대시보드 색과 음성 경고가
같은 값을 쓴다. 화면에서 빨간색이 되는 순간이 음성 경고 조건이다.

- 문턱값은 설정 파일에만 있다. 이 모듈은 그 값을 읽어 판정만 한다.
- `color_table()`이 돌려주는 표(색 + 문턱값)를 화면이 그대로 쓴다. 화면은
  문턱값을 따로 계산하지 않는다.
- 위험 경고(작업 12)는 `color_table()["alert_severity"]`(DANGER)에서만 무전을
  낸다.
- 리프트 앤 코스트(SCRUM-71)는 `metric_threshold()`로 WARN 값을 읽어 간다.

판정 규칙:
- 값이 없거나(None) 유한하지 않으면 `NO_DATA`.
- `HIGH`(높을수록 위험): `value >= danger`면 DANGER, `value >= warn`면 WARN.
- `LOW`(낮을수록 위험): `value < danger`면 DANGER, `value < warn`면 WARN.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from f1re.config import ConfigError, default_config_path


class Severity(StrEnum):
    """위험 단계. 값은 JSON 키이자 화면 색 표(`colors`)의 키다."""

    OK = "ok"
    WARN = "warn"
    DANGER = "danger"
    NO_DATA = "no_data"


class Direction(StrEnum):
    """값이 커질수록 위험한지(HIGH), 작아질수록 위험한지(LOW)."""

    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class MetricSpec:
    """지표의 의미. 방향·단위·표시 이름은 코드에, 문턱값은 설정에 둔다."""

    label: str
    unit: str
    direction: Direction


@dataclass(frozen=True)
class MetricThreshold:
    """지표 하나의 문턱값. HIGH면 warn < danger, LOW면 danger < warn."""

    name: str
    label: str
    unit: str
    direction: Direction
    warn: float
    danger: float


@dataclass(frozen=True)
class ThresholdTable:
    """설정에서 읽은 전체 표. `colors`는 Severity 값 → 색."""

    colors: dict[str, str]
    metrics: dict[str, MetricThreshold]


# 지표 정의. 여기 없는 지표는 설정에 있어도 오류다(오타를 조용히 넘기지 않는다).
# 단위: 손상·마모 %, 온도 °C, 연료 여유 랩.
_METRICS: dict[str, MetricSpec] = {
    # 손상·마모
    "front_wing_damage": MetricSpec("앞날개 손상(좌우 최대)", "%", Direction.HIGH),
    "front_left_wing_damage": MetricSpec("앞날개 왼쪽 손상", "%", Direction.HIGH),
    "front_right_wing_damage": MetricSpec("앞날개 오른쪽 손상", "%", Direction.HIGH),
    "rear_wing_damage": MetricSpec("뒷날개 손상", "%", Direction.HIGH),
    "floor_damage": MetricSpec("바닥 손상", "%", Direction.HIGH),
    "diffuser_damage": MetricSpec("디퓨저 손상", "%", Direction.HIGH),
    "sidepod_damage": MetricSpec("사이드포드 손상", "%", Direction.HIGH),
    "gearbox_damage": MetricSpec("기어박스 손상", "%", Direction.HIGH),
    "engine_damage": MetricSpec("엔진 손상", "%", Direction.HIGH),
    "engine_mguh_wear": MetricSpec("MGU-H 마모", "%", Direction.HIGH),
    "engine_es_wear": MetricSpec("에너지 저장장치 마모", "%", Direction.HIGH),
    "engine_ce_wear": MetricSpec("제어 전자장치 마모", "%", Direction.HIGH),
    "engine_ice_wear": MetricSpec("내연기관 마모", "%", Direction.HIGH),
    "engine_mguk_wear": MetricSpec("MGU-K 마모", "%", Direction.HIGH),
    "engine_tc_wear": MetricSpec("터보차저 마모", "%", Direction.HIGH),
    "tyre_wear": MetricSpec("타이어 마모", "%", Direction.HIGH),
    # 온도
    "tyre_temp": MetricSpec("타이어 표면 온도", "°C", Direction.HIGH),
    "tyre_inner_temp": MetricSpec("타이어 내부 온도", "°C", Direction.HIGH),
    "brake_temp": MetricSpec("브레이크 온도", "°C", Direction.HIGH),
    "engine_temp": MetricSpec("엔진 온도", "°C", Direction.HIGH),
    # 연료: 남은 연료 랩 - 남은 레이스 랩. 작을수록 위험하다.
    "fuel_laps_margin": MetricSpec("연료 여유 랩", "랩", Direction.LOW),
}

_COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}")


def load_thresholds(path: str | Path | None = None) -> ThresholdTable:
    """TOML 설정의 `[thresholds]`를 읽는다. 없으면 레포 기본 설정을 읽는다."""
    config_path = Path(path) if path is not None else default_config_path()
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"설정 파일을 읽지 못했습니다: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"설정 파일 형식이 잘못됐습니다: {config_path}") from exc

    section = raw.get("thresholds")
    if not isinstance(section, dict):
        raise ConfigError("[thresholds] 절이 없습니다")

    colors = _load_colors(section.get("colors"))
    unknown = set(section) - {"colors"} - set(_METRICS)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ConfigError(f"[thresholds]에 정의되지 않은 지표가 있습니다: {names}")

    metrics: dict[str, MetricThreshold] = {}
    for name, spec in _METRICS.items():
        entry = section.get(name)
        if not isinstance(entry, dict):
            raise ConfigError(f"[thresholds.{name}] 절이 없습니다")
        warn = _number(entry.get("warn"), f"thresholds.{name}.warn")
        danger = _number(entry.get("danger"), f"thresholds.{name}.danger")
        if spec.direction is Direction.HIGH and not warn < danger:
            raise ConfigError(
                f"thresholds.{name}: high 지표는 warn < danger여야 합니다"
            )
        if spec.direction is Direction.LOW and not danger < warn:
            raise ConfigError(
                f"thresholds.{name}: low 지표는 danger < warn이어야 합니다"
            )
        metrics[name] = MetricThreshold(
            name=name,
            label=spec.label,
            unit=spec.unit,
            direction=spec.direction,
            warn=warn,
            danger=danger,
        )
    return ThresholdTable(colors=colors, metrics=metrics)


@lru_cache(maxsize=1)
def default_thresholds() -> ThresholdTable:
    """레포 기본 설정의 문턱값 표. 프로세스에서 한 번만 읽는다."""
    return load_thresholds()


def metric_threshold(metric: str) -> MetricThreshold:
    """지표 하나의 문턱값. 리프트 앤 코스트는 `.warn`을 쓴다."""
    try:
        return default_thresholds().metrics[metric]
    except KeyError:
        raise KeyError(f"문턱값이 정의되지 않은 지표입니다: {metric}") from None


def severity(
    metric: str,
    value: float | None,
    ctx: dict[str, Any] | None = None,
) -> Severity:
    """지표 값의 위험 단계를 설정 문턱값으로 판정한다.

    `ctx`는 값이 없을 때 계산에 쓸 문맥이다. 지금은 `fuel_laps_margin`에서
    `fuel_remaining_laps`와 `remaining_race_laps`가 있으면 그 차이로 값을
    계산한다. 그 밖의 문맥은 판정에 쓰지 않는다.
    """
    threshold = metric_threshold(metric)
    resolved = _resolve_value(metric, value, ctx or {})
    if resolved is None:
        return Severity.NO_DATA
    if threshold.direction is Direction.HIGH:
        if resolved >= threshold.danger:
            return Severity.DANGER
        if resolved >= threshold.warn:
            return Severity.WARN
    else:
        if resolved < threshold.danger:
            return Severity.DANGER
        if resolved < threshold.warn:
            return Severity.WARN
    return Severity.OK


def color_table() -> dict[str, Any]:
    """화면이 그대로 쓰는 색·문턱값 표. JSON으로 직렬화할 수 있다.

    형식::

        {
          "colors": {"ok": "#..", "warn": "#..", "danger": "#..", "no_data": "#.."},
          "alert_severity": "danger",   # 이 단계가 음성 경고 조건이다
          "metrics": {
            "tyre_wear": {
              "label": "타이어 마모", "unit": "%", "direction": "high",
              "warn": 50.0, "danger": 75.0,
            },
            ...
          },
        }
    """
    table = default_thresholds()
    return {
        "colors": dict(table.colors),
        "alert_severity": Severity.DANGER.value,
        "metrics": {
            name: {
                "label": metric.label,
                "unit": metric.unit,
                "direction": metric.direction.value,
                "warn": metric.warn,
                "danger": metric.danger,
            }
            for name, metric in table.metrics.items()
        },
    }


def _load_colors(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError("[thresholds.colors] 절이 없습니다")
    expected = {item.value for item in Severity}
    unknown = set(value) - expected
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ConfigError(f"[thresholds.colors]에 모르는 단계가 있습니다: {names}")
    colors: dict[str, str] = {}
    for level in Severity:
        name = level.value
        color = value.get(name)
        if not isinstance(color, str) or not _COLOR_PATTERN.fullmatch(color):
            raise ConfigError(
                f"thresholds.colors.{name}는 #RRGGBB 형식이어야 합니다"
            )
        colors[name] = color
    return colors


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name}는 숫자여야 합니다")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{name}는 유한한 수여야 합니다")
    return number


def _resolve_value(
    metric: str, value: float | None, ctx: dict[str, Any]
) -> float | None:
    if value is not None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
    if metric == "fuel_laps_margin":
        fuel = _context_number(ctx.get("fuel_remaining_laps"))
        remaining = _context_number(ctx.get("remaining_race_laps"))
        if fuel is not None and remaining is not None:
            return fuel - remaining
    return None


def _context_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
