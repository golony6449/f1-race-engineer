"""위험 경고 규칙: 전이 1회 발화, 문턱값 단일 출처, SC·페널티·박스 무전.

공용 conftest를 만들지 않는 규칙에 따라 스냅샷 도우미는 이 파일 안에 둔다.
스냅샷 모양은 `StateStore.snapshot()`과 같다(패킷별 최신 body, 내 차 인덱스).
"""

from __future__ import annotations

from typing import Any

import pytest

from f1re.alerts.rules import (
    BOX_TTL_NS,
    SAFETY_TTL_NS,
    AlertRules,
    rules_from_config,
)
from f1re.alerts.thresholds import metric_threshold
from f1re.config import load_config
from f1re.radio.queue import Priority, RadioKind, RadioQueue
from f1re.radio.templates import TEMPLATES, Mood
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


def _urgent(key: str, **slots: object) -> list[str]:
    """그 틀의 urgent(급박) 표현 전부. 경고 문장은 표현이 돌아가며 바뀐다."""
    template = TEMPLATES[key]
    return [text.format(**slots) for text in template.texts_for(Mood.URGENT)]


# ---------------------------------------------------------------------------
# 위험 전이


def test_alert_fires_once_on_transition_to_danger() -> None:
    rules = _rules()

    # 경고(WARN)까지는 말하지 않는다(위험할 때만 음성).
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 50.0, 10.0])) == []

    danger = rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 80.0, 10.0]))
    # 레이스에서는 위험 경고와 함께 박스 지시가 한 번 나간다.
    assert _kinds(danger) == ["safety", "box"]
    assert danger[0].text in _urgent(
        "safety.tyre_wear", tyre="앞왼쪽", percent=80
    )
    assert danger[0].evidence["mood"] == Mood.URGENT.value
    assert danger[0].priority is Priority.SAFETY

    # 계속 위험이면 반복하지 않는다.
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 82.0, 10.0])) == []

    # 해제 뒤 다시 위험이 되면 또 한 번 말한다.
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 40.0, 10.0])) == []
    again = rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 90.0, 10.0]))
    assert len(again) == 2
    assert again[0].text in _urgent(
        "safety.tyre_wear", tyre="앞왼쪽", percent=90
    )
    assert again[1].text in _urgent("box.box_box")


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
    assert any(
        text in _urgent("safety.tyre_temp", tyre="앞왼쪽", celsius=121)
        for text in texts
    )
    assert any(
        text in _urgent("safety.tyre_temp", tyre="앞왼쪽", celsius=131)
        for text in texts
    )
    assert any(
        text in _urgent("safety.brake_temp", tyre="앞왼쪽", celsius=1001)
        for text in texts
    )
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
    assert any(
        text in _urgent("safety.damage", part="앞날개 오른쪽", percent=60)
        for text in texts
    )
    assert any(
        text in _urgent("safety.damage", part="바닥", percent=80)
        for text in texts
    )
    # 화면에서 빨개지는 파워유닛 마모도 같은 기준으로 경고한다.
    assert any(
        text in _urgent("safety.damage", part="터보차저", percent=91)
        for text in texts
    )
    assert all(message.priority is Priority.SAFETY for message in messages)


def test_fuel_short_alert() -> None:
    rules = _rules()
    # 남은 레이스 랩 = 50 - 10 + 1 = 41, 남은 연료 랩 = 12 → 여유 -29랩
    messages = rules.evaluate(_snapshot(status={"fuel_remaining_laps": 12.0}))
    assert len(messages) == 1
    assert messages[0].text in _urgent("safety.fuel_short", laps=29)
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
    assert len(sc) == 1 and sc[0].text in _urgent("safety.safety_car")
    assert rules.evaluate(_snapshot(session={"safety_car_status": 1})) == []

    assert rules.evaluate(_snapshot(session={"safety_car_status": 0})) == []
    vsc = rules.evaluate(_snapshot(session={"safety_car_status": 2}))
    assert len(vsc) == 1 and vsc[0].text in _urgent("safety.vsc")

    # 적기는 상태 필드가 없어 사건으로 받는다.
    red = rules.evaluate(
        _snapshot(session={"safety_car_status": 0}),
        [Event(EventKind.RED_FLAG)],
    )
    assert len(red) == 1 and red[0].text in _urgent("safety.red_flag")


def test_red_flag_period_count_detected_from_snapshot() -> None:
    rules = _rules()
    assert rules.evaluate(_snapshot(session={"num_red_flag_periods": 0})) == []
    messages = rules.evaluate(_snapshot(session={"num_red_flag_periods": 1}))
    assert len(messages) == 1
    assert messages[0].text in _urgent("safety.red_flag")
    assert rules.evaluate(_snapshot(session={"num_red_flag_periods": 1})) == []


def test_penalty_messages() -> None:
    rules = _rules()
    assert rules.evaluate(_snapshot(lap={"penalties": 0})) == []

    first = rules.evaluate(_snapshot(lap={"penalties": 5}))
    assert len(first) == 1
    assert first[0].text in _urgent("safety.penalty", seconds=5)
    assert first[0].evidence["penalty_seconds"] == 5
    assert rules.evaluate(_snapshot(lap={"penalties": 5})) == []

    second = rules.evaluate(_snapshot(lap={"penalties": 10}))
    assert len(second) == 1
    assert second[0].text in _urgent("safety.penalty", seconds=10)


def test_penalty_event_does_not_repeat_from_snapshot() -> None:
    rules = _rules()
    assert rules.evaluate(_snapshot(lap={"penalties": 0})) == []

    from_event = rules.evaluate(
        _snapshot(lap={"penalties": 0}),
        [Event(EventKind.PENALTY, car_index=PLAYER, data={"time": 5})],
    )
    assert len(from_event) == 1
    assert from_event[0].text in _urgent("safety.penalty", seconds=5)
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
    assert box.text in _urgent("box.box_box")
    assert box.priority is Priority.BOX
    assert box.evidence["reason"] == "tyre_wear"

    # 계속 위험인 동안에는 박스를 반복하지 않는다.
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 82.0, 10.0])) == []
    # 해제 뒤 다시 위험이면 다시 알린다. 같은 틀·단계라 직전 표현은 피한다.
    assert rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 40.0, 10.0])) == []
    repeated = rules.evaluate(_wear(tyres_wear=danger))
    assert _kinds(repeated) == ["safety", "box"]
    assert repeated[1].text != box.text
    assert repeated[1].text in _urgent("box.box_box")


def test_box_for_unserved_pit_penalty() -> None:
    rules = _rules()
    assert rules.evaluate(_snapshot()) == []
    messages = rules.evaluate(
        _snapshot(lap={"num_unserved_stop_go_pens": 1})
    )
    assert len(messages) == 1
    assert messages[0].text in _urgent("box.box_box")
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


# ---------------------------------------------------------------------------
# 바퀴별 경고 묶기와 유효 시간 (SCRUM-81)


class _FakeClock:
    """테스트용 시계. 재생 시간을 직접 흘려보낸다."""

    def __init__(self, now_ns: int = NOW) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance_s(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1_000_000_000)


def _play_all(
    queue: RadioQueue, clock: _FakeClock, seconds: float = 3.0
) -> list[str]:
    """무전을 하나씩 꺼내 `seconds`초씩 재생한 것으로 치고 문장을 모은다."""
    played: list[str] = []
    while (message := queue.next(clock())) is not None:
        played.append(message.text)
        clock.advance_s(seconds)
        queue.complete()
    return played


def test_wheel_danger_merged_into_one_message() -> None:
    """네 바퀴가 한 평가 주기에 함께 위험해지면 무전 한 건이다."""
    rules = _rules()
    messages = rules.evaluate(_wear(tyres_wear=[88.0, 88.0, 88.0, 88.0]))

    assert len(messages) == 2
    assert messages[0].text in _urgent(
        "safety.tyre_wear_bundle", tyres="4개", percent=88
    )
    assert messages[1].text in _urgent("box.box_box")
    safety = messages[0]
    assert safety.evidence["tyres"] == "4개"
    assert safety.evidence["value"] == pytest.approx(88.0)
    # 근거에는 문장에 넣지 않은 바퀴까지 값이 모두 남는다.
    assert [item["tyre"] for item in safety.evidence["values"]] == [
        "뒤왼쪽",
        "뒤오른쪽",
        "앞왼쪽",
        "앞오른쪽",
    ]
    assert [item["value"] for item in safety.evidence["values"]] == [88.0] * 4


def test_front_wing_both_sides_merged_into_one_message() -> None:
    rules = _rules()
    messages = rules.evaluate(
        _snapshot(
            damage={
                "front_left_wing_damage": 55,
                "front_right_wing_damage": 60,
            }
        )
    )
    safety = [message for message in messages if message.kind == "safety"]
    assert len(safety) == 1
    assert safety[0].text in _urgent(
        "safety.front_wing_both", percent=60
    )
    assert safety[0].evidence["value"] == pytest.approx(60.0)
    assert [side["value"] for side in safety[0].evidence["sides"]] == [55.0, 60.0]
    assert safety[0].evidence["new_sides"] == ["왼쪽", "오른쪽"]


def test_temperature_and_brake_bundles_use_worst_value() -> None:
    rules = _rules()
    messages = rules.evaluate(
        _snapshot(
            telemetry={
                "tyres_surface_temperature": [121, 122, 90, 90],
                "tyres_inner_temperature": [100, 100, 100, 100],
                "brakes_temperature": [1050, 1040, 400, 400],
            }
        )
    )
    texts = [message.text for message in messages]
    assert any(
        text in _urgent("safety.tyre_temp_bundle", tyres="뒤 두 개", celsius=122)
        for text in texts
    )
    assert any(
        text
        in _urgent("safety.brake_temp_bundle", celsius=1050, tyres="뒤 두 바퀴")
        for text in texts
    )


def test_partial_wheel_danger_speaks_only_new_wheels() -> None:
    rules = _rules()
    rear = rules.evaluate(_wear(tyres_wear=[88.0, 88.0, 10.0, 10.0]))
    assert len(rear) == 2
    assert rear[0].text in _urgent(
        "safety.tyre_wear_bundle", tyres="뒤 두 개", percent=88
    )
    assert rear[1].text in _urgent("box.box_box")

    # 뒤 두 바퀴는 이미 DANGER다. 새로 위험해진 앞 두 바퀴만 알린다.
    front = rules.evaluate(_wear(tyres_wear=[88.0, 88.0, 90.0, 90.0]))
    assert len(front) == 1
    assert front[0].text in _urgent(
        "safety.tyre_wear_bundle", tyres="앞 두 개", percent=90
    )
    assert [item["value"] for item in front[0].evidence["values"]] == [
        88,
        88,
        90,
        90,
    ]

    # 붙어 있지 않은 바퀴 둘은 이름을 그대로 나열한다.
    diagonal_rules = _rules()
    diagonal = diagonal_rules.evaluate(
        _wear(tyres_wear=[88.0, 10.0, 10.0, 88.0])
    )
    assert diagonal[0].text in _urgent(
        "safety.tyre_wear_bundle", tyres="뒤왼쪽·앞오른쪽", percent=88
    )


def test_wheel_bundle_keeps_transition_once_rule() -> None:
    rules = _rules()
    first = rules.evaluate(_wear(tyres_wear=[88.0] * 4))
    assert [message.kind for message in first] == ["safety", "box"]

    # 계속 DANGER면 다시 말하지 않는다.
    assert rules.evaluate(_wear(tyres_wear=[89.0] * 4)) == []

    # 한 바퀴가 풀렸다가 다시 위험이 되면 그 바퀴만 말한다.
    assert rules.evaluate(_wear(tyres_wear=[10.0, 88.0, 88.0, 88.0])) == []
    again = rules.evaluate(_wear(tyres_wear=[90.0, 88.0, 88.0, 88.0]))
    assert len(again) == 1
    assert again[0].text in _urgent(
        "safety.tyre_wear", tyre="뒤왼쪽", percent=90
    )


def test_followup_radio_survives_playback_and_priority() -> None:
    """데모 표본: 바퀴 경고가 한 건으로 묶이고 뒤 무전이 만료되지 않는다.

    무전 하나가 3초씩 재생되는 동안(총 24초) 박스·페널티·SC·연료·VSC가
    앞 무전 뒤에서 기다려도 유효 시간이 지나지 않는다. 큐는 우선순위대로
    꺼내므로 안전 경고가 모두 나간 뒤 박스가 나간다(설계 6.7).
    """
    clock = _FakeClock()
    rules = AlertRules(clock=clock)
    queue = RadioQueue(clock=clock)

    # 랩 1: 앞날개 좌우 60% 동시 손상 → 한 무전.
    lap1 = rules.evaluate(
        _snapshot(
            damage={
                "front_left_wing_damage": 60,
                "front_right_wing_damage": 60,
            }
        )
    )
    assert len(lap1) == 1
    assert lap1[0].text in _urgent("safety.front_wing_both", percent=60)
    for message in lap1:
        assert queue.push(message)

    # 랩 2: 네 바퀴 타이어 마모 88% → 한 무전 + 박스.
    lap2 = rules.evaluate(_wear(tyres_wear=[88.0, 88.0, 88.0, 88.0]))
    assert len(lap2) == 2
    assert lap2[0].text in _urgent(
        "safety.tyre_wear_bundle", tyres="4개", percent=88
    )
    assert lap2[1].text in _urgent("box.box_box")
    for message in lap2:
        assert queue.push(message)

    # 랩 3: 브레이크 네 바퀴 1050도 → 한 무전, 세이프티 카, 페널티 5초.
    lap3 = rules.evaluate(
        _snapshot(
            damage={"tyres_wear": [88.0] * 4},
            telemetry={"brakes_temperature": [1050, 1050, 1050, 1050]},
            lap={"penalties": 5},
            session={"safety_car_status": 1},
        )
    )
    lap3_texts = [message.text for message in lap3]
    assert any(
        text
        in _urgent("safety.brake_temp_bundle", celsius=1050, tyres="네 바퀴")
        for text in lap3_texts
    )
    assert any(text in _urgent("safety.safety_car") for text in lap3_texts)
    assert any(
        text in _urgent("safety.penalty", seconds=5) for text in lap3_texts
    )
    for message in lap3:
        assert queue.push(message)

    # 랩 4: 연료 마이너스.
    lap4 = rules.evaluate(
        _snapshot(
            damage={"tyres_wear": [88.0] * 4},
            status={"fuel_remaining_laps": 12.0},
        )
    )
    assert len(lap4) == 1
    assert lap4[0].text in _urgent("safety.fuel_short", laps=29)
    for message in lap4:
        assert queue.push(message)

    # 랩 5: 버추얼 세이프티 카.
    lap5 = rules.evaluate(
        _snapshot(
            damage={"tyres_wear": [88.0] * 4},
            session={"safety_car_status": 2},
        )
    )
    assert len(lap5) == 1
    assert lap5[0].text in _urgent("safety.vsc")
    for message in lap5:
        assert queue.push(message)

    played = _play_all(queue, clock, seconds=3.0)

    expected = [
        _urgent("safety.front_wing_both", percent=60),
        _urgent("safety.tyre_wear_bundle", tyres="4개", percent=88),
        _urgent("safety.brake_temp_bundle", celsius=1050, tyres="네 바퀴"),
        _urgent("safety.safety_car"),
        _urgent("safety.penalty", seconds=5),
        _urgent("safety.fuel_short", laps=29),
        _urgent("safety.vsc"),
        _urgent("box.box_box"),
    ]
    assert len(played) == len(expected)
    for text, choices in zip(played, expected, strict=True):
        assert text in choices, (text, choices)
    assert queue.pending_count == 0


def test_safety_and_box_ttls_come_from_config_and_are_long_enough() -> None:
    assert SAFETY_TTL_NS >= 30_000_000_000
    assert BOX_TTL_NS >= 30_000_000_000

    config = load_config()
    rules = rules_from_config(clock=lambda: NOW)
    assert rules.safety_ttl_ns == round(config.radio.safety_ttl_s * 1_000_000_000)
    assert rules.box_ttl_ns == round(config.radio.box_ttl_s * 1_000_000_000)
    assert rules.safety_ttl_ns >= 30_000_000_000
    assert rules.box_ttl_ns >= 30_000_000_000


def test_alert_rules_accept_injected_ttls() -> None:
    rules = AlertRules(
        clock=lambda: NOW,
        safety_ttl_ns=45_000_000_000,
        box_ttl_ns=40_000_000_000,
    )
    messages = rules.evaluate(_wear(tyres_wear=[10.0, 10.0, 80.0, 10.0]))
    assert messages[0].expires_ns == NOW + 45_000_000_000
    assert messages[1].expires_ns == NOW + 40_000_000_000
