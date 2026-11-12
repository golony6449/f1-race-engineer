"""데모 시나리오 파일 해석과 주행 효과 변환.

시나리오는 "정해진 랩·코너에서 상황을 일부러 만드는" 목록이다. 형식은
YAML이며 최상위는 다음 항목의 목록이다.

```yaml
- lap: 2            # 1부터. 그 랩 내내(또는 랩 시작 시점부터) 적용한다.
  corner: 3         # 선택. 코너 종류에만 쓴다. 없으면 그 랩의 모든 코너.
  kind: late_brake
  params: {meters: 10}
```

종류는 `KINDS`에 모아 두었다. 주행 종류(`DRIVING_KINDS`)는 속도 곡선·샘플
생성에 주입하고, 상태 종류는 송신기가 Car Status·Damage·Telemetry·Session에
값을 덮어쓴다. 같은 파일을 여러 번 읽어도 결과가 같고, 시드가 같으면 송신
패킷 열도 같다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from f1re.demo_sender.driving import CornerEffect

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

# 계획의 kind 목록 + 작업 5에서 추가한 brake_temp_critical(브레이크 온도 위험).
DRIVING_KINDS = frozenset(
    {
        "late_brake",
        "early_brake",
        "early_turn_in",
        "steer_corrections",
        "late_throttle",
        "off_track",
    }
)
FIELD_KINDS = frozenset(
    {
        "close_to_car_ahead",
        "drs_available",
        "ahead_slow_exit",
        "tyre_age_gap",
    }
)
ALERT_KINDS = frozenset(
    {
        "tyre_wear_critical",
        "tyre_temp_critical",
        "brake_temp_critical",
        "fuel_short",
        "front_wing_damage",
        "penalty",
        "safety_car",
        "vsc",
    }
)
KINDS = DRIVING_KINDS | FIELD_KINDS | ALERT_KINDS

# 종류별 기본 params. 생략한 값은 여기서 채우고, 표에 없는 이름은 오타로 본다.
KIND_DEFAULTS: dict[str, dict[str, float | int]] = {
    "late_brake": {"meters": 10.0, "width_m": 1.5},
    "early_brake": {"meters": 10.0},
    "early_turn_in": {"meters": 5.0},
    "steer_corrections": {"count": 3, "amplitude": 0.15},
    "late_throttle": {"meters": 30.0},
    "off_track": {"width_m": 7.0},
    "close_to_car_ahead": {"seconds": 0.6},
    "drs_available": {"distance_m": 150, "laps": 1},
    "ahead_slow_exit": {"speed_factor": 0.7},
    "tyre_age_gap": {"laps": 8},
    "tyre_wear_critical": {"wear": 88.0},
    "tyre_temp_critical": {"surface_c": 118, "inner_c": 128},
    "brake_temp_critical": {"c": 1050},
    "fuel_short": {"margin_laps": 2.0},
    "front_wing_damage": {"percent": 60},
    "penalty": {"seconds": 5, "type": 1, "infringement": 1},
    "safety_car": {"laps": 2},
    "vsc": {"laps": 1},
}

# 코너 번호가 반드시 있어야 하는 종류. 나머지 주행 종류는 없으면 모든 코너에 건다.
REQUIRES_CORNER = frozenset({"ahead_slow_exit"})

_ALLOWED_KEYS = frozenset({"lap", "corner", "kind", "params"})


class ScenarioError(ValueError):
    """시나리오 파일 형식이 잘못됐을 때."""


@dataclass(frozen=True)
class ScenarioEvent:
    """시나리오 한 줄. `params`는 기본값을 채운 값이다."""

    lap: int
    kind: str
    corner: int | None = None
    params: Mapping[str, float | int] = field(default_factory=dict)

    def param(self, name: str) -> float | int:
        """params 값. 직접 만든 이벤트에서 생략했으면 종류별 기본값을 쓴다."""
        if name in self.params:
            return self.params[name]
        return KIND_DEFAULTS[self.kind][name]

    @property
    def is_driving(self) -> bool:
        return self.kind in DRIVING_KINDS


def _number(value: Any, what: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenarioError(f"{what}는 숫자여야 합니다: {value!r}")
    return value


def _positive_int(value: Any, what: str) -> int:
    number = _number(value, what)
    if int(number) != number or number < 1:
        raise ScenarioError(f"{what}는 1 이상의 정수여야 합니다: {value!r}")
    return int(number)


def parse_scenarios(raw: Any) -> list[ScenarioEvent]:
    """YAML로 읽은 값을 `ScenarioEvent` 목록으로 바꾸고 검증한다."""
    if not isinstance(raw, list):
        raise ScenarioError("시나리오 파일 최상위는 목록이어야 합니다")
    events: list[ScenarioEvent] = []
    for index, item in enumerate(raw):
        where = f"{index + 1}번째 시나리오"
        if not isinstance(item, Mapping):
            raise ScenarioError(f"{where}: 매핑(딕셔너리)이어야 합니다")
        unknown = set(item) - _ALLOWED_KEYS
        if unknown:
            raise ScenarioError(f"{where}: 알 수 없는 키 {sorted(unknown)}")
        if "kind" not in item or "lap" not in item:
            raise ScenarioError(f"{where}: lap과 kind가 필요합니다")
        lap = _positive_int(item["lap"], f"{where} lap")
        kind = item["kind"]
        if not isinstance(kind, str) or kind not in KINDS:
            raise ScenarioError(f"{where}: 알 수 없는 kind {kind!r}")
        corner = item.get("corner")
        if corner is not None:
            corner = _positive_int(corner, f"{where} corner")
        if corner is None and kind in REQUIRES_CORNER:
            raise ScenarioError(f"{where}: {kind}에는 corner가 필요합니다")

        raw_params = item.get("params") or {}
        if not isinstance(raw_params, Mapping):
            raise ScenarioError(f"{where}: params는 매핑이어야 합니다")
        defaults = KIND_DEFAULTS[kind]
        unknown_params = set(raw_params) - set(defaults)
        if unknown_params:
            raise ScenarioError(
                f"{where}: {kind}에 없는 params {sorted(unknown_params)}"
            )
        params: dict[str, float | int] = dict(defaults)
        for name, value in raw_params.items():
            params[name] = _number(value, f"{where} params.{name}")
        events.append(ScenarioEvent(lap=lap, kind=kind, corner=corner, params=params))
    return events


def load_scenarios(path: str | Path) -> list[ScenarioEvent]:
    """YAML 시나리오 파일을 읽는다. 형식 오류는 `ScenarioError`."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(f"시나리오 파일을 읽지 못했습니다: {file_path}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{file_path}: YAML을 해석하지 못했습니다") from exc
    return parse_scenarios(raw)


def events_for_lap(events: Iterable[ScenarioEvent], lap: int) -> list[ScenarioEvent]:
    """그 랩에 걸린 시나리오만 고른다(파일 순서 유지)."""
    return [event for event in events if event.lap == lap]


def corner_effects(
    events: Iterable[ScenarioEvent],
    corner_numbers: Sequence[int],
) -> dict[int, CornerEffect]:
    """주행 종류 시나리오를 `generate_lap`이 쓰는 코너 효과로 바꾼다.

    `corner`가 없는 주행 시나리오는 그 랩의 모든 코너에 건다. 트랙에 없는
    코너 번호를 가리키면 `ScenarioError`다.
    """
    numbers = tuple(corner_numbers)
    known = set(numbers)
    effects: dict[int, CornerEffect] = {}
    for event in events:
        if not event.is_driving:
            continue
        targets = numbers if event.corner is None else (event.corner,)
        for number in targets:
            if number not in known:
                raise ScenarioError(
                    f"{event.lap}랩 {event.kind}: 트랙에 {number}번 코너가 없습니다"
                )
            effect = _event_effect(event)
            effects[number] = (
                effects[number].merged(effect) if number in effects else effect
            )
    return effects


def _event_effect(event: ScenarioEvent) -> CornerEffect:
    if event.kind == "late_brake":
        return CornerEffect(
            brake_shift_m=float(event.param("meters")),
            wide_m=float(event.param("width_m")),
        )
    if event.kind == "early_brake":
        return CornerEffect(brake_shift_m=-float(event.param("meters")))
    if event.kind == "early_turn_in":
        return CornerEffect(turn_in_early_m=float(event.param("meters")))
    if event.kind == "steer_corrections":
        return CornerEffect(
            steer_corrections=int(event.param("count")),
            steer_amplitude=float(event.param("amplitude")),
        )
    if event.kind == "late_throttle":
        return CornerEffect(throttle_delay_m=float(event.param("meters")))
    if event.kind == "off_track":
        return CornerEffect(wide_m=float(event.param("width_m")), off_track=True)
    raise ScenarioError(f"주행 시나리오가 아닙니다: {event.kind}")
