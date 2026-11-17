"""서킷 추월 지점 기반 공격·방어 전략 규칙 엔진 테스트(SCRUM-130).

스즈카(`track_id == 13`)의 `overtaking_spots`·`entry_window_m`과 실시간 레이스
상황(간격·DRS·ERS·타이어·남은 랩·SC/VSC·피트)으로 공격·방어 계획을 고르는지
확인한다. 공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.

- 공격: 앞차 1.0초 이내 + 추월 지점 무전 창 안에서만.
- 방어: 뒤차 1.0초 이내 + 같은 무전 창 안에서만.
- 앞·뒤 동시 접근: 좁은 쪽 우선, 동률이면 방어 우선.
- 공격·방어 빈도는 `overtake_per_lap`을 각각 따로 센다.
- 레이스(15~17)가 아니거나 SC/VSC·피트·리타이어면 말하지 않는다.
- `overtaking_spots`가 없는 트랙(모나코)은 계획을 만들지 않고, 기존
  `OvertakeAdvisor` 추월 무전이 그대로 동작한다.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from f1re.config import AppConfig
from f1re.demo_sender.driving import CornerEffect, DriverProfile, generate_lap
from f1re.laps.traces import LapTrace, Sample
from f1re.overtake.advisor import ERS_STORE_MAX_J, OvertakeAdvisor, OvertakeConfig
from f1re.overtake.strategy import (
    OvertakeStrategy,
    StrategyConfig,
)
from f1re.tracks.model import Track, load_outline, load_track

SUZUKA_ID = 13
MONACO_ID = 5
# F1 25 공식 세션 종류(SCRUM-131): 1=연습, 15=레이스, 18=타임 트라이얼.
SESSION_TYPE_PRACTICE = 1
SESSION_TYPE_RACE = 15
SESSION_TYPE_TIME_TRIAL = 18
PLAYER = 0
AHEAD = 1
BEHIND = 2
NOW_NS = 1_000_000_000

_TRACK = load_track(SUZUKA_ID)
_MONACO = load_track(MONACO_ID)
_OUTLINE = load_outline(SUZUKA_ID)
assert _TRACK is not None and _MONACO is not None and _OUTLINE is not None
TRACK: Track = _TRACK
MONACO: Track = _MONACO

# 스즈카 추월 지점 무전 창(데이터 그대로): 카시오 5090~5140, 130R 4300~4700.
CASIO_WINDOW_M = 5100.0
FAST_CORNER_WINDOW_M = 4500.0


# ---------------------------------------------------------------------------
# 도우미: 스냅샷(StateStore.snapshot() 모양)


def _lap_entry(
    *,
    lap: int = 2,
    distance: float = 0.0,
    position: int = 0,
    total: float = 0.0,
    gap_s: float | None = None,
    driver_status: int = 4,
    pit_status: int = 0,
    result_status: int = 2,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "current_lap_num": lap,
        "lap_distance": distance,
        "car_position": position,
        "total_distance": total,
        "current_lap_invalid": 0,
        "driver_status": driver_status,
        "pit_status": pit_status,
        "result_status": result_status,
    }
    if gap_s is not None:
        gap_ms = int(round(gap_s * 1000.0))
        entry["delta_to_car_in_front_minutes_part"] = gap_ms // 60_000
        entry["delta_to_car_in_front_ms_part"] = gap_ms % 60_000
    return entry


def _car_status(
    *,
    drs_allowed: int = 0,
    drs_distance: int = 0,
    tyre_age: int = 6,
    compound: int = 18,
    ers_energy: float = ERS_STORE_MAX_J * 0.9,
    ers_mode: int = 2,
) -> dict[str, Any]:
    return {
        "drs_allowed": drs_allowed,
        "drs_activation_distance": drs_distance,
        "actual_tyre_compound": compound,
        "tyres_age_laps": tyre_age,
        "ers_store_energy": ers_energy,
        "ers_deploy_mode": ers_mode,
    }


def _snapshot(
    *,
    player_distance: float = CASIO_WINDOW_M,
    lap: int = 2,
    session_type: int = SESSION_TYPE_RACE,
    safety_car: int = 0,
    gap_ahead: float | None = 0.6,
    gap_behind: float | None = None,
    behind: bool = False,
    player_position: int = 10,
    player_status: dict[str, Any] | None = None,
    ahead_status: dict[str, Any] | None = None,
    behind_status: dict[str, Any] | None = None,
    total_laps: int = 20,
    uid: int = 1,
    track_id: int = SUZUKA_ID,
    player_driver_status: int = 4,
    player_pit_status: int = 0,
    player_result_status: int = 2,
) -> dict[str, Any]:
    """차 22대 상태를 갖춘 스냅샷을 만든다.

    앞차 간격은 내 Lap Data의 `delta_to_car_in_front`, 뒤차 간격은 그 차의
    같은 필드다(바로 앞이 나).
    """
    entries = [_lap_entry() for _ in range(22)]
    player_total = (lap - 1) * TRACK.length_m + player_distance
    entries[PLAYER] = _lap_entry(
        lap=lap,
        distance=player_distance,
        position=player_position,
        total=player_total,
        gap_s=gap_ahead,
        driver_status=player_driver_status,
        pit_status=player_pit_status,
        result_status=player_result_status,
    )
    if gap_ahead is not None:
        entries[AHEAD] = _lap_entry(
            lap=lap,
            distance=player_distance + 5.0,
            position=max(1, player_position - 1),
            total=player_total + 100.0,
        )
    if behind and gap_behind is not None:
        entries[BEHIND] = _lap_entry(
            lap=lap,
            distance=max(0.0, player_distance - 5.0),
            position=player_position + 1,
            total=max(0.0, player_total - 100.0),
            gap_s=gap_behind,
        )

    statuses = [_car_status() for _ in range(22)]
    statuses[PLAYER] = {**_car_status(), **(player_status or {})}
    statuses[AHEAD] = {**_car_status(), **(ahead_status or {})}
    statuses[BEHIND] = {**_car_status(), **(behind_status or {})}

    return {
        "session_uid": uid,
        "player_car_index": PLAYER,
        "game_paused": False,
        "packets": {
            "session": {
                "session_type": session_type,
                "track_id": track_id,
                "safety_car_status": safety_car,
                "total_laps": total_laps,
            },
            "lap_data": {"lap_data": entries},
            "car_status": {"car_status_data": statuses},
        },
    }


# ---------------------------------------------------------------------------
# 도우미: 랩 트레이스(앞차의 느린 탈출 비교)


def _trace(
    *,
    car_index: int = PLAYER,
    lap: int = 2,
    brake_shift_corner: int | None = None,
    brake_shift_m: float = 40.0,
) -> LapTrace:
    effects = (
        {}
        if brake_shift_corner is None
        else {
            brake_shift_corner: CornerEffect(
                brake_shift_m=brake_shift_m, wide_m=1.5
            )
        }
    )
    samples = [
        Sample(
            dist_m=sample.dist_m,
            t_ms=sample.t_ms,
            speed=sample.speed_kmh,
            throttle=sample.throttle,
            brake=sample.brake,
            steer=sample.steer,
            gear=sample.gear,
            x=sample.x,
            z=sample.z,
        )
        for sample in generate_lap(
            TRACK,
            _OUTLINE,
            DriverProfile(pace=1.0, consistency=1.0),
            lap,
            random.Random(0),
            corner_effects=effects,
        )
    ]
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=True,
        invalid_reason=None,
        samples=samples,
    )


def _strategy(**kwargs: Any) -> OvertakeStrategy:
    return OvertakeStrategy(TRACK, StrategyConfig(**kwargs))


# ---------------------------------------------------------------------------
# 공격


def test_attack_plan_in_casio_window() -> None:
    """카시오 무전 창(5090~5140m)에서 앞차 0.6초면 공격 계획을 고른다."""
    plans = _strategy().evaluate(
        _snapshot(player_distance=CASIO_WINDOW_M, gap_ahead=0.6), None, NOW_NS
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.side == "attack"
    assert plan.spot.name == "카시오 시케인"
    assert plan.template_key == "strategy.attack"
    assert plan.corner == 16
    assert plan.gap_s == pytest.approx(0.6)
    facts = plan.facts
    assert facts["attack_corners"] == [16, 17]
    assert facts["prep_corners"] == [14, 15]
    assert facts["attack_corners_text"] == "16번, 17번"
    assert facts["prep_corners_text"] == "14번, 15번"
    assert facts["distance_to_attack_m"] == pytest.approx(140.0)
    assert facts["remaining_laps"] == pytest.approx(19.0)
    assert "인코스" in facts["attack_method"]
    assert facts["defense_line"]


def test_attack_waits_for_radio_window() -> None:
    """무전 창 밖(랩 중간)에서는 앞차가 가까워도 말하지 않는다."""
    strategy = _strategy()

    assert strategy.evaluate(_snapshot(player_distance=2000.0), None, NOW_NS) == []
    # 창이 아닌 다른 지점(스푼 3500~3645 밖)도 마찬가지다.
    assert strategy.evaluate(_snapshot(player_distance=3400.0), None, NOW_NS) == []


def test_attack_plan_in_wrapping_window() -> None:
    """결승선을 넘는 1번 코너 창(5800~430m)에서 DRS 공격 계획을 고른다."""
    plans = _strategy().evaluate(
        _snapshot(
            player_distance=5800.0,
            gap_ahead=0.6,
            player_status={"drs_allowed": 1, "drs_activation_distance": 100},
        ),
        None,
        NOW_NS,
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.spot.name == "1번 코너"
    assert plan.template_key == "strategy.attack_drs"
    assert plan.facts["method"] == "drs_late_brake"
    assert plan.facts["drs_allowed"] == 1


def test_attack_facts_include_ers_tyres_and_slow_exit() -> None:
    """ERS·타이어 나이·앞차 느린 탈출이 판단 근거로 남는다."""
    traces = {
        PLAYER: [_trace(car_index=PLAYER)],
        AHEAD: [_trace(car_index=AHEAD, brake_shift_corner=14)],
    }
    plans = _strategy().evaluate(
        _snapshot(
            player_distance=CASIO_WINDOW_M,
            gap_ahead=0.6,
            player_status={
                "tyres_age_laps": 4,
                "ers_store_energy": ERS_STORE_MAX_J * 0.9,
            },
            ahead_status={"tyres_age_laps": 12},
        ),
        traces,
        NOW_NS,
    )

    assert len(plans) == 1
    facts = plans[0].facts
    assert facts["tyre_advantage_laps"] == 8
    assert facts["ers_store_pct"] == pytest.approx(90.0)
    assert facts["ahead_slow_corner"] == 14
    assert facts["exit_deficit_kmh"] >= 4.0
    assert facts["player_exit_speed_kmh"] > facts["ahead_exit_speed_kmh"]


def test_high_risk_spot_is_skipped_unless_dominant() -> None:
    """130R은 `avoid_reason`이 있어 기본적으로 공격하지 않는다(압도적일 때만)."""
    strategy = _strategy()
    normal = _snapshot(
        player_distance=FAST_CORNER_WINDOW_M,
        gap_ahead=0.6,
        player_status={"tyres_age_laps": 6},
        ahead_status={"tyres_age_laps": 10},
    )
    assert strategy.evaluate(normal, None, NOW_NS) == []

    dominant = _snapshot(
        player_distance=FAST_CORNER_WINDOW_M,
        gap_ahead=0.25,
        player_status={"tyres_age_laps": 2},
        ahead_status={"tyres_age_laps": 14},
    )
    plans = strategy.evaluate(dominant, None, NOW_NS)
    assert len(plans) == 1
    assert plans[0].spot.name == "130R"
    facts = plans[0].facts
    assert plans[0].side == "attack"
    assert facts["difficulty"] == "very_high"


# ---------------------------------------------------------------------------
# 방어


def test_defense_plan_when_car_behind_within_one_second() -> None:
    """뒤차가 1.0초 이내면 다음 추월 지점의 방어 계획을 고른다."""
    plans = _strategy().evaluate(
        _snapshot(
            player_distance=CASIO_WINDOW_M,
            gap_ahead=5.0,
            behind=True,
            gap_behind=0.8,
        ),
        None,
        NOW_NS,
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.side == "defense"
    assert plan.spot.name == "카시오 시케인"
    assert plan.template_key == "strategy.defense"
    assert plan.gap_s == pytest.approx(0.8)
    assert plan.facts["defense_line"]
    assert plan.facts["attack_corners_text"] == "16번, 17번"


def test_no_defense_when_car_behind_is_farther_than_one_second() -> None:
    plans = _strategy().evaluate(
        _snapshot(
            player_distance=CASIO_WINDOW_M,
            gap_ahead=5.0,
            behind=True,
            gap_behind=1.2,
        ),
        None,
        NOW_NS,
    )

    assert plans == []


def test_defense_waits_for_radio_window() -> None:
    plans = _strategy().evaluate(
        _snapshot(
            player_distance=2000.0,
            gap_ahead=5.0,
            behind=True,
            gap_behind=0.5,
        ),
        None,
        NOW_NS,
    )

    assert plans == []


# ---------------------------------------------------------------------------
# 앞·뒤 동시 접근 우선순위


def test_closer_side_wins_when_both_approach() -> None:
    strategy = _strategy()
    attack = strategy.evaluate(
        _snapshot(gap_ahead=0.6, behind=True, gap_behind=0.9), None, NOW_NS
    )
    assert attack and attack[0].side == "attack"

    defense = strategy.evaluate(
        _snapshot(lap=3, gap_ahead=0.9, behind=True, gap_behind=0.6),
        None,
        NOW_NS + 1,
    )
    assert defense and defense[0].side == "defense"


def test_tie_prefers_defense() -> None:
    """간격이 같으면 방어를 먼저 고른다."""
    plans = _strategy().evaluate(
        _snapshot(gap_ahead=0.6, behind=True, gap_behind=0.6), None, NOW_NS
    )

    assert plans and plans[0].side == "defense"


# ---------------------------------------------------------------------------
# 빈도: 공격·방어를 따로 센다


def test_attack_and_defense_counts_are_separate_per_lap() -> None:
    strategy = _strategy()
    first = strategy.evaluate(
        _snapshot(gap_ahead=0.5, behind=False), None, NOW_NS
    )
    assert first and first[0].side == "attack"
    # 같은 랩에서 공격은 이미 1회다. 방어는 아직 0회라 나간다.
    second = strategy.evaluate(
        _snapshot(gap_ahead=5.0, behind=True, gap_behind=0.5),
        None,
        NOW_NS + 1,
    )
    assert second and second[0].side == "defense"
    # 둘 다 소진됐다.
    assert (
        strategy.evaluate(
            _snapshot(gap_ahead=0.4, behind=True, gap_behind=0.4),
            None,
            NOW_NS + 2,
        )
        == []
    )
    assert strategy.attack_messages_this_lap == 1
    assert strategy.defense_messages_this_lap == 1


def test_new_lap_resets_both_counts() -> None:
    strategy = _strategy()
    assert strategy.evaluate(
        _snapshot(gap_ahead=0.5, behind=False), None, NOW_NS
    )
    assert (
        strategy.evaluate(
            _snapshot(lap=3, gap_ahead=0.5, behind=False), None, NOW_NS + 1
        )
        != []
    )


def test_exhausted_attack_falls_back_to_defense() -> None:
    """공격 횟수가 찼어도 같은 순간의 방어 기회는 놓치지 않는다."""
    strategy = _strategy()
    assert strategy.evaluate(
        _snapshot(gap_ahead=0.5, behind=False), None, NOW_NS
    )
    plans = strategy.evaluate(
        _snapshot(gap_ahead=0.4, behind=True, gap_behind=0.7),
        None,
        NOW_NS + 1,
    )

    assert plans and plans[0].side == "defense"


def test_config_from_app_config_uses_overtake_settings_for_both() -> None:
    config = AppConfig(overtake_gap_s=0.8, overtake_per_lap=2)

    strategy_config = StrategyConfig.from_app_config(config)

    assert strategy_config.attack_gap_s == pytest.approx(0.8)
    assert strategy_config.defense_gap_s == pytest.approx(0.8)
    assert strategy_config.attack_per_lap == 2
    assert strategy_config.defense_per_lap == 2


# ---------------------------------------------------------------------------
# 제외 조건


@pytest.mark.parametrize("session_type", [1, 5, 18])
def test_no_strategy_outside_race(session_type: int) -> None:
    plans = _strategy().evaluate(
        _snapshot(session_type=session_type, gap_ahead=0.5, behind=True),
        None,
        NOW_NS,
    )

    assert plans == []


@pytest.mark.parametrize("session_type", [15, 16, 17])
def test_strategy_active_in_race_family(session_type: int) -> None:
    plans = _strategy().evaluate(
        _snapshot(session_type=session_type, gap_ahead=0.5), None, NOW_NS
    )

    assert plans and plans[0].side == "attack"


@pytest.mark.parametrize("safety_car", [1, 2])
def test_no_strategy_under_safety_car_or_vsc(safety_car: int) -> None:
    plans = _strategy().evaluate(
        _snapshot(safety_car=safety_car, gap_ahead=0.5, behind=True),
        None,
        NOW_NS,
    )

    assert plans == []


@pytest.mark.parametrize(
    ("driver_status", "pit_status"),
    [(2, 0), (3, 0), (7, 0), (4, 1), (4, 2)],
)
def test_no_strategy_in_pit(
    driver_status: int, pit_status: int
) -> None:
    plans = _strategy().evaluate(
        _snapshot(
            gap_ahead=0.5,
            player_driver_status=driver_status,
            player_pit_status=pit_status,
        ),
        None,
        NOW_NS,
    )

    assert plans == []


def test_no_strategy_when_player_retired() -> None:
    plans = _strategy().evaluate(
        _snapshot(gap_ahead=0.5, player_result_status=7), None, NOW_NS
    )

    assert plans == []


# ---------------------------------------------------------------------------
# 세션 전환·하위 호환


def test_session_change_resets_counts() -> None:
    strategy = _strategy()
    assert strategy.evaluate(_snapshot(gap_ahead=0.5), None, NOW_NS)
    assert (
        strategy.evaluate(
            _snapshot(gap_ahead=0.5, uid=2), None, NOW_NS + 1
        )
        != []
    )


def test_monaco_without_overtaking_spots_has_no_strategy_plans() -> None:
    strategy = OvertakeStrategy(MONACO)

    plans = strategy.evaluate(_snapshot(gap_ahead=0.5), None, NOW_NS)

    assert MONACO.overtaking_spots == ()
    assert plans == []


def test_advisor_still_speaks_on_track_without_spots() -> None:
    """추월 지점이 없는 트랙에서는 기존 OvertakeAdvisor 추월 무전이 그대로 난다."""
    advisor = OvertakeAdvisor(OvertakeConfig(), track=MONACO)

    messages = advisor.evaluate(
        _snapshot(
            player_distance=1000.0,
            gap_ahead=0.6,
            track_id=MONACO_ID,
            player_status={"drs_allowed": 1, "drs_activation_distance": 150},
        ),
        None,
        NOW_NS,
    )

    assert len(messages) == 1
    assert messages[0].kind == "overtake"
    assert "DRS" in messages[0].text
