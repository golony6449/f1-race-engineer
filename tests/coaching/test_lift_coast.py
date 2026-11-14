"""리프트 앤 코스트 측정·추천·필요할 때 무전 테스트(SCRUM-71).

공용 `conftest.py`를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
데모 송신기의 스즈카·모나코 주행 샘플과 `lift_and_coast.yaml` 시나리오로
"지시 → 리프트 → 피드백" 순서를 확인한다.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Iterable, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from f1re.coaching.lift_coast import (
    LiftCoastCoach,
    LiftCoastConfig,
    LiftCoastState,
    LiftZone,
    measure_coasting,
    rank_lift_zones,
)
from f1re.coaching.metrics import default_coaching_thresholds
from f1re.demo_sender.driving import (
    DriverProfile,
    Sample as DrivingSample,
    generate_lap,
)
from f1re.demo_sender.scenarios import SCENARIO_DIR, load_scenarios
from f1re.laps.traces import LapTrace, Sample
from f1re.radio.queue import RadioKind
from f1re.state.store import StateStore
from f1re.tracks.model import Corner, Track, load_outline, load_track
from f1re.web.lift_coast_api import create_lift_coast_router

SUZUKA_ID = 13
MONACO_ID = 5
# 실제 시각 기준. RadioQueue에 넣는 무전의 유효 시간을 진짜 시계로 판정한다.
_BASE_NS = time.time_ns()

_SUZUKA = load_track(SUZUKA_ID)
_SUZUKA_OUTLINE = load_outline(SUZUKA_ID)
_MONACO = load_track(MONACO_ID)
_MONACO_OUTLINE = load_outline(MONACO_ID)
assert (
    _SUZUKA is not None
    and _SUZUKA_OUTLINE is not None
    and _MONACO is not None
    and _MONACO_OUTLINE is not None
)
SUZUKA: Track = _SUZUKA
MONACO: Track = _MONACO

# 데모 시나리오(lift_and_coast.yaml)의 값. 파일 값을 그대로 시험에 쓴다.
SCENARIO_EVENTS = load_scenarios(SCENARIO_DIR / "lift_and_coast.yaml")
FUEL_MARGIN_LAPS = -float(SCENARIO_EVENTS[0].param("margin_laps"))
BRAKE_TEMP_C = float(SCENARIO_EVENTS[1].param("c"))
FRONT_TYRE_TEMP_C = 118.0


# ---------------------------------------------------------------------------
# 도우미


def _corner(track: Track, number: int) -> Corner:
    return next(corner for corner in track.corners if corner.number == number)


def _to_sample(sample: DrivingSample, fuel_kg: float | None = None) -> Sample:
    return Sample(
        dist_m=sample.dist_m,
        t_ms=sample.t_ms,
        speed=sample.speed_kmh,
        throttle=sample.throttle,
        brake=sample.brake,
        steer=sample.steer,
        gear=sample.gear,
        x=sample.x,
        z=sample.z,
        fuel_kg=fuel_kg,
    )


def _trace(
    track: Track,
    outline,
    lap: int,
    *,
    pace: float = 1.0,
    car_index: int = 0,
    valid: bool = True,
    fuel_start_kg: float | None = None,
    fuel_per_lap_kg: float | None = None,
) -> LapTrace:
    samples = generate_lap(
        track,
        outline,
        DriverProfile(pace=pace, consistency=1.0),
        lap,
        random.Random(0),
    )
    if fuel_start_kg is not None and fuel_per_lap_kg is not None:
        last = max(sample.dist_m for sample in samples) or 1.0
        converted = [
            _to_sample(
                sample,
                fuel_kg=fuel_start_kg - fuel_per_lap_kg * sample.dist_m / last,
            )
            for sample in samples
        ]
    else:
        converted = [_to_sample(sample) for sample in samples]
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=valid,
        invalid_reason=None,
        samples=converted,
    )


def _suzuka(lap: int, **kwargs) -> LapTrace:
    return _trace(SUZUKA, _SUZUKA_OUTLINE, lap, **kwargs)


def _monaco(lap: int, **kwargs) -> LapTrace:
    return _trace(MONACO, _MONACO_OUTLINE, lap, **kwargs)


def _state(sample: Sample, lap: int, **overrides) -> dict:
    state = {
        "t_ms": sample.t_ms,
        "speed": sample.speed,
        "throttle": sample.throttle,
        "brake": sample.brake,
        "steer": sample.steer,
        "gear": sample.gear,
        "x": sample.x,
        "z": sample.z,
        "lap_num": lap,
    }
    state.update(overrides)
    return state


def _feed(
    coach: LiftCoastCoach,
    trace: LapTrace,
    lap: int | None = None,
    *,
    overrides: dict | None = None,
    until_m: float | None = None,
    rate_ns: int = 1_000_000,
) -> list[tuple[float, object]]:
    """랩 샘플을 코치에 넣고 (랩 거리, 무전) 목록을 돌려준다."""
    resolved_lap = trace.lap if lap is None else lap
    base_ns = _BASE_NS + resolved_lap * 200_000_000_000
    collected: list[tuple[float, object]] = []
    for sample in trace.samples:
        if until_m is not None and sample.dist_m > until_m:
            break
        state = _state(sample, resolved_lap, **(overrides or {}))
        now_ns = base_ns + sample.t_ms * rate_ns
        for message in coach.on_sample(state, sample.dist_m, now_ns):
            collected.append((sample.dist_m, message))
    return collected


def _messages(events: Sequence[tuple[float, object]]) -> list:
    return [message for _dist, message in events]


def _complete(coach: LiftCoastCoach, trace: LapTrace) -> None:
    now_ns = (
        _BASE_NS
        + trace.lap * 200_000_000_000
        + trace.samples[-1].t_ms * 1_000_000
    )
    coach.on_lap_completed(trace, now_ns=now_ns)


def _coach(
    track: Track = SUZUKA,
    *,
    session_type: int = 10,
    state: LiftCoastState | None = None,
    config: LiftCoastConfig | None = None,
) -> LiftCoastCoach:
    return LiftCoastCoach(
        track,
        state=state,
        config=config,
        session_type=session_type,
    )


def _apply_lift(
    trace: LapTrace,
    track: Track,
    corner_number: int,
    distance_m: float,
) -> LapTrace:
    """코너 브레이크 지점 앞 `distance_m`만큼 스로틀을 뗀 랩을 만든다."""
    corner = _corner(track, corner_number)
    brake_on = default_coaching_thresholds().brake_on
    samples = list(trace.samples)
    onset = next(
        (
            sample
            for sample in samples
            if corner.entry_m - 200.0 <= sample.dist_m <= corner.entry_m + 50.0
            and sample.brake >= brake_on
        ),
        None,
    )
    assert onset is not None, f"{corner_number}번 코너 브레이크 지점을 찾지 못했습니다"
    start = onset.dist_m - distance_m
    lifted = [
        dataclasses.replace(sample, throttle=0.0)
        if start <= sample.dist_m <= onset.dist_m
        else sample
        for sample in samples
    ]
    return dataclasses.replace(trace, samples=lifted)


def _alert_overrides(lap: int, *, active: bool = True) -> dict:
    """lift_and_coast.yaml을 랩마다 차 상태로 옮긴다.

    연료 부족은 회복되지 않으므로 2랩부터 계속 들고 간다. 브레이크 온도 위험은
    시나리오대로 3랩부터다.
    """
    if not active:
        return {
            "fuel_laps_margin": 1.0,
            "brake_temp": 400.0,
            "tyre_temps": [90.0, 90.0, 95.0, 95.0],
        }
    overrides: dict = {}
    if lap >= 2:
        overrides["fuel_laps_margin"] = FUEL_MARGIN_LAPS
    if lap >= 3:
        overrides["brake_temp"] = BRAKE_TEMP_C
    return overrides


def _run_lap(
    coach: LiftCoastCoach,
    trace: LapTrace,
    lap: int,
    *,
    active: bool = True,
    lift_zones: Iterable[LiftZone] = (),
) -> list[tuple[float, object]]:
    for zone in lift_zones:
        trace = _apply_lift(
            trace, coach.track, zone.corner, zone.target_m + 10.0
        )
    events = _feed(coach, trace, lap, overrides=_alert_overrides(lap, active=active))
    _complete(coach, trace)
    return events


# ---------------------------------------------------------------------------
# 추천 순위


def test_recommendations_choose_long_straight_heavy_braking_zones() -> None:
    """스즈카 상위에 1·16·11 중 2개 이상, 모나코 상위에 1·10 중 1개 이상."""
    suzuka_zones = rank_lift_zones(SUZUKA, _suzuka(1))
    suzuka_top = [zone.corner for zone in suzuka_zones]

    assert len(suzuka_zones) == 3
    assert len({1, 16, 11} & set(suzuka_top)) >= 2
    # 130R처럼 거의 풀스로틀로 지나는 구간은 리프트해도 얻을 게 없다.
    assert 15 not in suzuka_top

    monaco_zones = rank_lift_zones(MONACO, _monaco(1))
    monaco_top = [zone.corner for zone in monaco_zones]

    assert len({1, 10} & set(monaco_top)) >= 1


def test_recommendation_target_is_ten_meter_rounded_and_bounded() -> None:
    zones = rank_lift_zones(SUZUKA, _suzuka(1))

    for zone in zones:
        assert zone.target_m % 10 == 0
        assert 30.0 <= zone.target_m <= 100.0
        assert zone.lift_m <= zone.brake_m
        assert zone.straight_m > 0.0
    # 무전 문장은 거리 슬롯이 하나라 상위 구간이 같은 목표를 쓴다.
    assert len({zone.target_m for zone in zones}) == 1


def test_recommendations_need_a_lap_with_samples() -> None:
    empty = LapTrace(0, 1, True, None, [])

    assert rank_lift_zones(SUZUKA, empty) == []


# ---------------------------------------------------------------------------
# 측정


def test_measures_coasting_distance_and_time() -> None:
    """스로틀을 뗀 지점부터 브레이크 문턱까지의 거리·시간을 잰다."""
    lifted = _apply_lift(_suzuka(1), SUZUKA, 1, 40.0)

    measurements = measure_coasting(lifted, SUZUKA)
    corner1 = next(m for m in measurements if m.corner == 1)

    assert 35.0 <= corner1.distance_m <= 45.0
    assert corner1.time_ms > 0
    assert corner1.lift_m < corner1.brake_m


def test_lap_fuel_consumption_is_recorded_from_sample_fuel() -> None:
    coach = _coach()
    trace = _suzuka(1, fuel_start_kg=20.0, fuel_per_lap_kg=1.8)

    coach.on_lap_completed(trace)

    assert len(coach.fuel_laps) == 1
    record = coach.fuel_laps[0]
    assert record.used_kg is not None
    assert record.used_kg == pytest.approx(1.8, abs=0.01)

    snapshot = coach.state.snapshot()
    assert snapshot["fuel"]["last_used_kg"] == pytest.approx(1.8, abs=0.1)
    assert snapshot["fuel"]["recent"][-1]["lap"] == 1


def test_lap_without_fuel_samples_records_nothing() -> None:
    coach = _coach()

    coach.on_lap_completed(_suzuka(1))

    assert coach.fuel_laps == ()
    assert coach.state.snapshot()["fuel"]["recent"] == []


# ---------------------------------------------------------------------------
# 지시 → 리프트 → 피드백 (데모 lift_and_coast.yaml)


def test_demo_scenario_instruction_lift_feedback_order() -> None:
    """연료 부족·브레이크 온도 위험으로 지시하고, 다음 랩 리프트에 피드백한다."""
    events = load_scenarios(SCENARIO_DIR / "lift_and_coast.yaml")
    assert [event.kind for event in events] == ["fuel_short", "brake_temp_critical"]
    coach = _coach()

    # 1랩(출발 혼전)은 추천에서 뺀다.
    assert _run_lap(coach, _suzuka(1), 1) == []
    assert coach.recommendations == ()

    # 2랩: 연료 부족이 시작되지만 아직 추천 구간이 없어 지시하지 않는다.
    assert _run_lap(coach, _suzuka(2), 2) == []
    zones = coach.recommendations
    assert zones, "2랩을 마치면 추천 구간이 있어야 한다"

    # 3랩: 브레이크 온도 위험. 추천 구간 리프트 지시가 한 번 나간다.
    lap3_events = _run_lap(coach, _suzuka(3), 3)
    lap3 = _messages(lap3_events)
    assert len(lap3) == 1
    instruction = lap3[0]
    assert instruction.kind == RadioKind.COACHING
    assert instruction.text.startswith("연료가 2랩 모자라.")
    assert "발 떼" in instruction.text and "미터" in instruction.text
    assert instruction.lap == 3
    assert instruction.evidence["reason"] == "fuel_laps_margin"
    assert {zone.corner for zone in zones} <= set(
        instruction.evidence["corners"]
    )

    # 4랩: 지시받은 구간마다 리프트 → 탈출 직후 "리프트 좋아" 피드백.
    lap4_events = _run_lap(coach, _suzuka(4), 4, lift_zones=zones)
    lap4 = _messages(lap4_events)
    feedback = [message for message in lap4 if "리프트 좋아" in message.text]
    assert len(feedback) == len(zones)
    for message in feedback:
        assert message.kind == RadioKind.IMPROVEMENT
        assert message.lap == 4
        assert message.evidence["status"] == "good"
        assert message.evidence["measured_m"] >= message.evidence["target_m"]

    # 5랩: 여유 회복 → "연료 회복됐어. 이제 리프트 안 해도 돼." 한 번.
    lap5 = _messages(_run_lap(coach, _suzuka(5), 5, active=False))
    stops = [
        message
        for message in lap5
        if message.text == "연료 회복됐어. 이제 리프트 안 해도 돼."
    ]
    assert len(stops) == 1
    assert stops[0].kind == RadioKind.IMPROVEMENT
    assert stops[0].lap == 5

    # 6랩: 이미 멈췄으니 다시 말하지 않는다.
    assert _messages(_run_lap(coach, _suzuka(6), 6, active=False)) == []

    # 순서: 지시(3랩) → 피드백(4랩) → 정지(5랩).
    assert instruction.lap < min(message.lap for message in feedback) < stops[0].lap


def test_short_lift_feedback_says_how_much_more() -> None:
    """코스팅이 목표보다 짧으면 남은 거리를 말한다."""
    coach = _coach()
    _run_lap(coach, _suzuka(1), 1)
    _run_lap(coach, _suzuka(2), 2)
    zones = coach.recommendations
    assert zones
    _run_lap(coach, _suzuka(3), 3)

    # 첫 추천 구간만 10m 리프트하고 나머지는 하지 않는다.
    first = zones[0]
    trace = _apply_lift(_suzuka(4), SUZUKA, first.corner, 10.0)
    lap4 = _messages(
        _feed(coach, trace, 4, overrides=_alert_overrides(4))
    )
    _complete(coach, trace)

    short = [message for message in lap4 if message.corner == first.corner]
    assert len(short) == 1
    message = short[0]
    assert "리프트가 짧았어" in message.text
    assert message.evidence["status"] == "short"
    assert message.evidence["short_m"] == pytest.approx(
        first.target_m - message.evidence["measured_m"], abs=1.0
    )


def test_feedback_not_repeated_in_same_lap() -> None:
    """같은 랩에 같은 구간 피드백을 두 번 하지 않는다(거리 지터에도)."""
    coach = _coach()
    _run_lap(coach, _suzuka(1), 1)
    _run_lap(coach, _suzuka(2), 2)
    zones = coach.recommendations
    _run_lap(coach, _suzuka(3), 3)
    first = zones[0]

    trace = _apply_lift(_suzuka(4), SUZUKA, first.corner, first.target_m + 10.0)
    corner = _corner(SUZUKA, first.corner)
    messages = []
    jittered = False
    base_ns = _BASE_NS + 4 * 200_000_000_000
    for sample in trace.samples:
        overrides = _alert_overrides(4)
        messages.extend(
            coach.on_sample(
                _state(sample, 4, **overrides),
                sample.dist_m,
                base_ns + sample.t_ms * 1_000_000,
            )
        )
        if not jittered and sample.dist_m >= corner.exit_m:
            jittered = True
            back = dataclasses.replace(sample, dist_m=sample.dist_m - 0.5)
            messages.extend(
                coach.on_sample(
                    _state(back, 4, **overrides),
                    back.dist_m,
                    base_ns + sample.t_ms * 1_000_000 + 1,
                )
            )

    assert jittered
    repeated = [message for message in messages if message.corner == first.corner]
    assert len(repeated) == 1


# ---------------------------------------------------------------------------
# 여유가 있을 때 / 레이스가 아닐 때


def test_no_message_when_margins_are_healthy() -> None:
    """연료·브레이크·타이어 여유가 있으면 무전이 한 건도 나가지 않는다."""
    coach = _coach()

    emitted = []
    for lap in range(1, 5):
        emitted.extend(_run_lap(coach, _suzuka(lap), lap, active=False))

    assert emitted == []
    assert coach.recommendations, "추천 계산은 해 두어야 한다"


def test_no_lift_messages_outside_race_session() -> None:
    """타임 트라이얼에서는 위험해도 리프트 지시·피드백을 말하지 않는다."""
    coach = _coach(session_type=13)

    emitted = []
    for lap in range(1, 5):
        emitted.extend(_run_lap(coach, _suzuka(lap), lap))

    assert emitted == []
    assert coach.recommendations


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "fuel_laps_margin": 1.0,
                "brake_temp": BRAKE_TEMP_C,
                "tyre_temps": [90.0, 90.0, 95.0, 95.0],
            },
            f"브레이크가 {int(BRAKE_TEMP_C)}도야.",
        ),
        (
            {
                "fuel_laps_margin": 1.0,
                "brake_temp": 400.0,
                "tyre_temps": [90.0, 90.0, FRONT_TYRE_TEMP_C, FRONT_TYRE_TEMP_C],
            },
            f"앞 타이어가 {int(FRONT_TYRE_TEMP_C)}도야.",
        ),
    ],
)
def test_temperature_only_reason_uses_temperature_template(
    overrides: dict, expected: str
) -> None:
    """연료는 여유가 있어도 브레이크·앞타이어 온도만으로 지시할 수 있다."""
    coach = _coach()
    _run_lap(coach, _suzuka(1), 1, active=False)
    _run_lap(coach, _suzuka(2), 2, active=False)
    assert coach.recommendations

    trace = _suzuka(3)
    messages = _messages(_feed(coach, trace, 3, overrides=overrides))
    _complete(coach, trace)

    assert len(messages) == 1
    assert messages[0].kind == RadioKind.COACHING
    assert expected in messages[0].text
    assert "발 떼" in messages[0].text and "미터" in messages[0].text


def test_invalid_lap_is_not_instructed() -> None:
    coach = _coach()
    _run_lap(coach, _suzuka(1), 1)
    _run_lap(coach, _suzuka(2), 2)

    lap3 = _suzuka(3)
    events = _feed(
        coach,
        lap3,
        3,
        overrides={**_alert_overrides(3), "lap_valid": False},
    )

    assert events == []


# ---------------------------------------------------------------------------
# 자동 탐지 코너("N번째 코너")


def test_ordinal_wording_for_unofficial_corners() -> None:
    corners = [
        dataclasses.replace(corner, official=False) for corner in SUZUKA.corners
    ]
    unofficial = dataclasses.replace(SUZUKA, corners=corners)
    coach = _coach(unofficial)

    _run_lap(coach, _suzuka(1), 1)
    _run_lap(coach, _suzuka(2), 2)
    zones = coach.recommendations
    assert zones
    lap3 = _messages(_run_lap(coach, _suzuka(3), 3))
    assert len(lap3) == 1
    assert "번째 코너" in lap3[0].text

    first = zones[0]
    trace = _apply_lift(_suzuka(4), SUZUKA, first.corner, first.target_m + 10.0)
    lap4 = _messages(_feed(coach, trace, 4, overrides=_alert_overrides(4)))
    _complete(coach, trace)

    good = [message for message in lap4 if "리프트 좋아" in message.text]
    assert good
    assert any(
        "번째 코너 리프트 좋아. 그대로 해." in message.text for message in good
    )


# ---------------------------------------------------------------------------
# 공유 상태·API


def test_state_and_api_expose_recommendations_and_coasting() -> None:
    state = LiftCoastState()
    coach = _coach(state=state)

    _run_lap(coach, _suzuka(1), 1)
    _run_lap(coach, _suzuka(2), 2)
    zones = coach.recommendations
    _run_lap(coach, _suzuka(3), 3)
    _run_lap(coach, _suzuka(4), 4, lift_zones=zones)

    snapshot = state.snapshot()
    assert snapshot["recommended"]
    assert snapshot["need"]["active"] is True
    assert snapshot["need"]["reasons"] == ["fuel_laps_margin", "brake_temp"]
    assert snapshot["recent_coasting"]
    assert snapshot["instructed"]["lap"] == 3

    app = FastAPI()
    app.include_router(create_lift_coast_router(state))
    client = TestClient(app)
    response = client.get("/api/lift-coast")

    assert response.status_code == 200
    payload = response.json()
    assert payload["recommended"] == snapshot["recommended"]
    assert payload["recent_coasting"] == snapshot["recent_coasting"]
    assert payload["instructed"] == snapshot["instructed"]


def test_api_route_is_registered_in_app() -> None:
    from f1re.web.app import create_app

    app = create_app(StateStore())

    # `app.routes`는 포함된 라우터를 평평하게 펴지 않으므로 OpenAPI 스키마로 본다.
    assert "/api/lift-coast" in app.openapi()["paths"]
