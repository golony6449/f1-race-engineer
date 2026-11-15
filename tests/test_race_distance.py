"""레이스 거리·연료 여유 랩 회귀 테스트(SCRUM-95).

F1 25 UDP의 Car Status `m_fuelRemainingLaps`는 게임 MFD에 보이는 **남은 레이스
거리 대비 여유 랩(+/-)** 이다. 이 값을 남은 레이스 랩에서 또 빼면 실제 주행에서
"연료가 부족하다"는 오경보가 난다. 세션 종류(FP·Q·레이스·TT)와 레이스 길이
(5랩·풀)에 따라 경고·코칭·화면이 같은 기준을 쓰는지 확인한다.

공용 `conftest.py`를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

from typing import Any

import pytest

from f1re.alerts.rules import AlertRules
from f1re.alerts.thresholds import Severity, severity
from f1re.race_distance import (
    fuel_laps_margin,
    race_distance_known,
    remaining_race_laps,
)
from f1re.radio.templates import is_last_laps
from f1re.web.app import player_summary

NOW = 1_000_000_000_000
PLAYER = 0

# 공식 세션 종류(EA F1 25 UDP 문서).
PRACTICE = 1
QUALIFYING = 5
RACE = 10
TIME_TRIAL = 13


# ---------------------------------------------------------------------------
# 도우미


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
    session_type: int = RACE,
    total_laps: int = 50,
    lap: int = 10,
    fuel_laps: float = 0.0,
) -> dict[str, Any]:
    packets: dict[str, Any] = {
        "session": {
            "session_type": session_type,
            "total_laps": total_laps,
            "safety_car_status": 0,
            "num_red_flag_periods": 0,
        },
        **_body(
            "lap_data",
            "lap_data",
            {
                "current_lap_num": lap,
                "penalties": 0,
                "num_unserved_drive_through_pens": 0,
                "num_unserved_stop_go_pens": 0,
            },
        ),
        **_body(
            "car_status",
            "car_status_data",
            {"fuel_remaining_laps": fuel_laps},
        ),
        **_body("car_telemetry", "car_telemetry_data", {}),
        **_body("car_damage", "car_damage_data", {}),
    }
    return {
        "session_uid": 1,
        "player_car_index": PLAYER,
        "game_paused": False,
        "packets": packets,
        "lap_validity": [],
        "diagnostics": {},
    }


def _fuel_cell(
    session_type: int,
    total_laps: int,
    lap: int,
    fuel_laps: float,
) -> dict[str, Any]:
    packets = {
        "session": {"session_type": session_type, "total_laps": total_laps},
        **_body("lap_data", "lap_data", {"current_lap_num": lap}),
        **_body(
            "car_status",
            "car_status_data",
            {"fuel_in_tank": 18.0, "fuel_remaining_laps": fuel_laps},
        ),
        **_body("car_telemetry", "car_telemetry_data", {}),
        **_body("car_damage", "car_damage_data", {}),
    }
    summary = player_summary({"player_car_index": PLAYER, "packets": packets})
    return summary["fuel"]


# ---------------------------------------------------------------------------
# 기준 계산 한곳


def test_fuel_laps_margin_is_mfd_value() -> None:
    """게임 MFD 값이 이미 여유 랩이다. 그대로 쓰고 다시 빼지 않는다."""
    assert fuel_laps_margin(-2.0) == pytest.approx(-2.0)
    assert fuel_laps_margin(0.0) == pytest.approx(0.0)
    assert fuel_laps_margin(3.5) == pytest.approx(3.5)
    assert fuel_laps_margin(None) is None
    assert fuel_laps_margin(True) is None
    assert fuel_laps_margin(float("nan")) is None


@pytest.mark.parametrize(
    ("session_type", "total_laps", "lap", "expected"),
    [
        # 5랩 단축 레이스: 현재 랩을 포함한다.
        (RACE, 5, 1, 5.0),
        (RACE, 5, 4, 2.0),
        (RACE, 5, 5, 1.0),
        # 완주 뒤 값이 0으로 잘리고 음수가 되지 않는다.
        (RACE, 5, 6, 0.0),
        # 풀 레이스.
        (RACE, 50, 50, 1.0),
        # 레이스가 아닌 세션에는 레이스 거리가 없다.
        (PRACTICE, 50, 10, None),
        (QUALIFYING, 50, 10, None),
        (TIME_TRIAL, 0, 10, None),
        # 레이스라도 total_laps가 0·없으면 셀 수 없다.
        (RACE, 0, 10, None),
        (RACE, None, 10, None),
        # 현재 랩이 없으면 셀 수 없다.
        (RACE, 50, 0, None),
        (RACE, 50, None, None),
    ],
)
def test_remaining_race_laps_by_session_and_length(
    session_type: int | None,
    total_laps: int | None,
    lap: int | None,
    expected: float | None,
) -> None:
    assert remaining_race_laps(session_type, total_laps, lap) == expected


@pytest.mark.parametrize(
    ("session_type", "total_laps", "expected"),
    [
        (RACE, 5, True),
        (RACE, 50, True),
        (PRACTICE, 50, False),
        (QUALIFYING, 50, False),
        (TIME_TRIAL, 0, False),
        (RACE, 0, False),
        (RACE, None, False),
        (None, 50, False),
    ],
)
def test_race_distance_known(
    session_type: int | None, total_laps: int | None, expected: bool
) -> None:
    assert race_distance_known(session_type, total_laps) is expected


def test_last_laps_needs_positive_remaining() -> None:
    """남은 랩 0 이하는 레이스 거리가 없는 세션의 결과이므로 마지막 랩이 아니다."""
    assert is_last_laps(3) is True
    assert is_last_laps(1) is True
    assert is_last_laps(0) is False
    assert is_last_laps(-1) is False
    assert is_last_laps(None) is False


# ---------------------------------------------------------------------------
# 경고: 실제 오경보 표본과 세션 종류별 동작


def test_recorded_false_alarm_sample_is_not_danger() -> None:
    """실제 기록 표본: MFD 여유 0.0랩, 남은 레이스 2랩(49/50랩).

    예전에는 남은 레이스 랩 2를 또 빼 -2랩이라고 말했다. 이제는 0.0랩이므로
    경고(WARN)이고, 위험(DANGER) 무전은 나가지 않는다.
    """
    snapshot = _snapshot(total_laps=50, lap=49, fuel_laps=0.0)

    assert severity("fuel_laps_margin", 0.0) is Severity.WARN
    assert _rules().evaluate(snapshot) == []


@pytest.mark.parametrize("session_type", [PRACTICE, QUALIFYING, TIME_TRIAL])
def test_fuel_alert_suppressed_in_non_race_session(session_type: int) -> None:
    """레이스가 아닌 세션에서는 MFD 여유가 크게 모자라도 연료 경고를 내지 않는다."""
    assert _rules().evaluate(
        _snapshot(session_type=session_type, fuel_laps=-10.0)
    ) == []


@pytest.mark.parametrize("total_laps", [0, None])
def test_fuel_alert_suppressed_without_total_laps(
    total_laps: int | None,
) -> None:
    """레이스라도 레이스 거리를 모르면 연료 부족을 말하지 않는다."""
    assert _rules().evaluate(
        _snapshot(total_laps=total_laps, fuel_laps=-10.0)  # type: ignore[arg-type]
    ) == []


def test_fuel_alert_in_short_race() -> None:
    """5랩 레이스 4랩째, MFD 여유 -1.5랩이면 위험 무전이 나간다."""
    messages = _rules().evaluate(
        _snapshot(total_laps=5, lap=4, fuel_laps=-1.5)
    )

    assert len(messages) == 1
    assert messages[0].evidence["value"] == pytest.approx(-1.5)
    assert messages[0].evidence["remaining_race_laps"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 대시보드: 값은 그대로 보여 주고 남은 레이스 랩만 세션에 따라 달라진다


@pytest.mark.parametrize(
    ("session_type", "total_laps", "lap", "margin", "remaining"),
    [
        (RACE, 5, 4, -1.0, 2.0),
        (RACE, 50, 10, -1.0, 41.0),
        (PRACTICE, 50, 10, -1.0, None),
        (QUALIFYING, 50, 10, -1.0, None),
        (TIME_TRIAL, 0, 10, -1.0, None),
        (RACE, 0, 10, 2.0, None),
    ],
)
def test_dashboard_fuel_shows_mfd_margin(
    session_type: int,
    total_laps: int,
    lap: int,
    margin: float,
    remaining: float | None,
) -> None:
    fuel = _fuel_cell(session_type, total_laps, lap, margin)

    assert fuel["laps_margin"]["value"] == pytest.approx(margin)
    assert fuel["laps_margin"]["severity"] == (
        "danger" if margin < 0.0 else "ok"
    )
    assert fuel["remaining_race_laps"]["value"] == remaining
