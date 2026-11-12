"""위험 경고 규칙: 전이 1회 발화, 문턱값 단일 출처, SC·페널티·박스 무전.

공용 conftest를 만들지 않는 규칙에 따라 스냅샷 도우미는 이 파일 안에 둔다.
스냅샷 모양은 `StateStore.snapshot()`과 같다(패킷별 최신 body, 내 차 인덱스).
"""

from __future__ import annotations

from typing import Any

import pytest

from f1re.alerts.rules import BOX_TTL_NS, SAFETY_TTL_NS, AlertRules
from f1re.alerts.thresholds import metric_threshold
from f1re.radio.queue import Priority, RadioKind
from f1re.state.events import Event, EventKind

NOW = 1_000_000_000_000
PLAYER = 0

_SESSION = {"total_laps": 50, "safety_car_status": 0, "num_red_flag_periods": 0}
_LAP = {
    "current_lap_num": 10,
    "penalties": 0,
    "total_warnings": 0,
    "num_unserved_drive_through_pens": 0,
    "num_unserved_stop_go_pens": 0,
}
_STATUS = {"fuel_remaining_laps": 41.0}
_TELEMETRY = {
    "tyres_surface_temperature": [90, 90, 90, 90],
    "tyres_inner_temperature": [100, 100, 100, 100],
    "brakes_temperature": [400, 400, 400, 400],
}
_DAMAGE = {"tyres_wear": [10.0, 10.0, 10.0, 10.0]}


def _rules() -> AlertRules:
    return AlertRules(clock=lambda: NOW)


def _body(
    packet_name: str,
    list_key: str,
    values: dict[str, Any],
    player: int = PLAYER,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = [{} for _ in range(22)]
    entries[player] = dict(values)
    return {packet_name: {list_key: entries}}


def _snapshot(
    *,
    session_uid: int = 1,
    player: int | None = PLAYER,
    session: dict[str, Any] | None = None,
    lap: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    damage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """필요한 패킷만 담은 스냅샷. 넘긴 값은 기본값 위에 덮어쓴다."""
    index = player or 0
    packets: dict[str, Any] = {
        "session": {**_SESSION, **(session or {})},
        **_body("lap_data", "lap_data", {**_LAP, **(lap or {})}),
        **_body("car_status", "car_status_data", {**_STATUS, **(status or {})}, index),
        **_body(
            "car_telemetry",
            "car_telemetry_data",
            {**_TELEMETRY, **(telemetry or {})},
            index,
        ),
        **_body("car_damage", "car_damage_data", {**_DAMAGE, **(damage or {})}, index),
    }
    return {
        "session_uid": session_uid,
        "player_car_index": player,
        "game_paused": False,
        "packets": packets,
        "lap_validity": [],
        "diagnostics": {},
    }


def _wear(
    *,
    session: dict[str, Any] | None = None,
    session_uid: int = 1,
    **values: Any,
) -> dict[str, Any]:
    return _snapshot(damage=values, session=session, session_uid=session_uid)


def _kinds(messages: list) -> list[str]:
    return [message.kind for message in messages]


# ---------------------------------------------------------------------------
# 위험 전이


def test_alert_fires_once_on_transition_to_danger() -> None:
    rules = _rules()

    # 경고(WARN)까지는 말하지 않는다(위험할 때만 음성).
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 50.0, 10.0])) == []

    danger = rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 80.0, 10.0]))
    # 레이스에서는 위험 경고와 함께 박스 지시가 한 번 나간다.
    assert _kinds(danger) == ["safety", "box"]
    assert danger[0].text == "타이어 앞왼쪽 마모 80퍼센트."
    assert danger[0].priority is Priority.SAFETY

    # 계속 위험이면 반복하지 않는다.
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 82.0, 10.0])) == []

    # 해제 뒤 다시 위험이 되면 또 한 번 말한다.
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 40.0, 10.0])) == []
    again = rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 90.0, 10.0]))
    assert [message.text for message in again] == [
        "타이어 앞왼쪽 마모 90퍼센트.",
        "박스, 박스.",
    ]


def test_alert_uses_dashboard_threshold() -> None:
    """화면 색이 빨간색이 되는 순간이 곧 경고 조건이다(문턱값 단일 출처)."""
    threshold = metric_threshold("tyre_wear")

    below = _rules()
    assert (
        below.evaluate(
            _wear(tyres_wear=[10.0, 10.0, threshold.danger - 0.1, 10.0])
        )
        == []
    )

    at_threshold = _rules()
    messages = at_threshold.evaluate(
        _wear(tyres_wear=[10.0, 10.0, threshold.danger, 10.0])
    )
    safety = [message for message in messages if message.kind == "safety"]
    assert len(safety) == 1
    assert safety[0].evidence["danger"] == pytest.approx(threshold.danger)
    assert safety[0].evidence["metric"] == "tyre_wear"
    assert safety[0].evidence["value"] == pytest.approx(threshold.danger)


def test_temperature_and_brake_alerts() -> None:
    rules = _rules()
    messages = rules.evaluate(
        _snapshot(
            telemetry={
                "tyres_surface_temperature": [90, 90, 121, 90],
                "tyres_inner_temperature": [100, 100, 131, 100],
                "brakes_temperature": [400, 400, 1001, 400],
            }
        )
    )
    texts = [message.text for message in messages]
    assert "타이어 앞왼쪽 온도 121도." in texts
    assert "타이어 앞왼쪽 온도 131도." in texts
    assert "브레이크 앞왼쪽 온도 1001도." in texts
    metrics = {message.evidence["metric"] for message in messages}
    assert metrics == {"tyre_temp", "tyre_inner_temp", "brake_temp"}


def test_damage_alerts() -> None:
    rules = _rules()
    messages = rules.evaluate(
        _snapshot(
            damage={
                "front_right_wing_damage": 60,
                "floor_damage": 80,
                "engine_tc_wear": 91,
            }
        )
    )
    texts = [message.text for message in messages]
    assert "앞날개 오른쪽 손상 60퍼센트." in texts
    assert "바닥 손상 80퍼센트." in texts
    # 화면에서 빨개지는 파워유닛 마모도 같은 기준으로 경고한다.
    assert "터보차저 손상 91퍼센트." in texts
    assert all(message.priority is Priority.SAFETY for message in messages)


def test_fuel_short_alert() -> None:
    rules = _rules()
    # 남은 레이스 랩 = 50 - 10 + 1 = 41, 남은 연료 랩 = 12 → 여유 -29랩
    messages = rules.evaluate(_snapshot(status={"fuel_remaining_laps": 12.0}))
    assert [message.text for message in messages] == ["연료 마이너스 29랩."]
    assert messages[0].evidence["metric"] == "fuel_laps_margin"
    assert messages[0].evidence["value"] == pytest.approx(-29.0)

    # 정확히 0이면 아직 부족은 아니다(WARN). 말하지 않는다.
    exact = _rules()
    assert exact.evaluate(_snapshot(status={"fuel_remaining_laps": 41.0})) == []


def test_no_alerts_without_player_car() -> None:
    rules = _rules()
    assert rules.evaluate({"session_uid": None, "player_car_index": None, "packets": {}}) == []
    assert rules.evaluate({"session_uid": 1, "player_car_index": 255, "packets": {}}) == []


def test_session_change_resets_transitions() -> None:
    rules = _rules()
    wear = [10.0, 10.0, 80.0, 10.0]
    first = rules.evaluate(_wear(tyres_wear=wear, session_uid=1))
    assert len([m for m in first if m.kind == "safety"]) == 1
    assert rules.evaluate(_wear(tyres_wear=wear, session_uid=1)) == []
    # 세션이 바뀌면 새 세션의 위험 상태를 다시 알린다.
    second = rules.evaluate(_wear(tyres_wear=wear, session_uid=2))
    assert len([m for m in second if m.kind == "safety"]) == 1


# ---------------------------------------------------------------------------
# SC·VSC·적기·페널티


def test_safety_car_vsc_and_red_flag() -> None:
    rules = _rules()
    assert rules.evaluate(_snapshot(session={"safety_car_status": 0})) == []

    sc = rules.evaluate(_snapshot(session={"safety_car_status": 1}))
    assert [message.text for message in sc] == ["세이프티 카."]
    assert rules.evaluate(_snapshot(session={"safety_car_status": 1})) == []

    assert rules.evaluate(_snapshot(session={"safety_car_status": 0})) == []
    vsc = rules.evaluate(_snapshot(session={"safety_car_status": 2}))
    assert [message.text for message in vsc] == ["버추얼 세이프티 카."]

    # 적기는 상태 필드가 없어 사건으로 받는다.
    red = rules.evaluate(
        _snapshot(session={"safety_car_status": 0}),
        [Event(EventKind.RED_FLAG)],
    )
    assert [message.text for message in red] == ["적기. 적기."]


def test_red_flag_period_count_detected_from_snapshot() -> None:
    rules = _rules()
    assert rules.evaluate(_snapshot(session={"num_red_flag_periods": 0})) == []
    messages = rules.evaluate(_snapshot(session={"num_red_flag_periods": 1}))
    assert [message.text for message in messages] == ["적기. 적기."]
    assert rules.evaluate(_snapshot(session={"num_red_flag_periods": 1})) == []


def test_penalty_messages() -> None:
    rules = _rules()
    assert rules.evaluate(_snapshot(lap={"penalties": 0})) == []

    first = rules.evaluate(_snapshot(lap={"penalties": 5}))
    assert [message.text for message in first] == ["페널티 5초."]
    assert first[0].evidence["penalty_seconds"] == 5
    assert rules.evaluate(_snapshot(lap={"penalties": 5})) == []

    second = rules.evaluate(_snapshot(lap={"penalties": 10}))
    assert [message.text for message in second] == ["페널티 10초."]


def test_penalty_event_does_not_repeat_from_snapshot() -> None:
    rules = _rules()
    assert rules.evaluate(_snapshot(lap={"penalties": 0})) == []

    from_event = rules.evaluate(
        _snapshot(lap={"penalties": 0}),
        [Event(EventKind.PENALTY, car_index=PLAYER, data={"time": 5})],
    )
    assert [message.text for message in from_event] == ["페널티 5초."]
    # 같은 페널티가 Lap Data에 반영돼도 두 번 말하지 않는다.
    assert rules.evaluate(_snapshot(lap={"penalties": 5})) == []


# ---------------------------------------------------------------------------
# 박스 지시


def test_box_message() -> None:
    rules = _rules()
    danger = [10.0, 10.0, 80.0, 10.0]

    messages = rules.evaluate(_wear(tyres_wear=danger))
    assert _kinds(messages) == ["safety", "box"]
    box = messages[1]
    assert box.text == "박스, 박스."
    assert box.priority is Priority.BOX
    assert box.evidence["reason"] == "tyre_wear"

    # 계속 위험인 동안에는 박스를 반복하지 않는다.
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 82.0, 10.0])) == []
    # 해제 뒤 다시 위험이면 다시 알린다.
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 40.0, 10.0])) == []
    assert _kinds(rules.evaluate(_wear(tyres_wear=danger))) == ["safety", "box"]


def test_box_for_unserved_pit_penalty() -> None:
    rules = _rules()
    assert rules.evaluate(_snapshot()) == []
    messages = rules.evaluate(
        _snapshot(lap={"num_unserved_stop_go_pens": 1})
    )
    assert [message.text for message in messages] == ["박스, 박스."]
    assert messages[0].evidence["reason"] == "pit_penalty"
    assert rules.evaluate(_snapshot(lap={"num_unserved_stop_go_pens": 1})) == []


def test_no_box_in_time_trial() -> None:
    rules = _rules()
    messages = rules.evaluate(
        _wear(tyres_wear=[10.0, 10.0, 80.0, 10.0], session={"total_laps": 0})
    )
    # 타임 트라이얼에는 박스가 없다. 타이어 경고만 나간다.
    assert _kinds(messages) == ["safety"]


# ---------------------------------------------------------------------------
# 무전 모양


def test_message_priority_evidence_and_ttl() -> None:
    rules = _rules()
    messages = rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 80.0, 10.0]))
    safety = messages[0]

    assert safety.kind == RadioKind.SAFETY.value
    assert safety.priority is Priority.SAFETY
    assert safety.created_ns == NOW
    assert safety.expires_ns == NOW + SAFETY_TTL_NS
    assert safety.lap == 10
    assert safety.evidence["unit"] == "%"
    assert safety.evidence["tyre"] == "앞왼쪽"

    box = messages[1]
    assert box.expires_ns == NOW + BOX_TTL_NS
