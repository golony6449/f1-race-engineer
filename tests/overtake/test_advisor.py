"""추월 선택지 무전 테스트(작업 18, SCRUM-68).

설계 6.5·6.7과 데모 시나리오(`demo_sender/scenarios/overtake.yaml`)를 기준으로
확인한다. 공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.

- AI 상대 세션에서 앞차와의 간격이 `overtake_gap_s`(기본 1.0초) 이하일 때만 켜지고,
  간격이 벌어지거나 추월하면 꺼진다. 타임 트라이얼에서는 말하지 않는다.
- 선택지는 DRS(활성 거리·ERS 포함), 앞차의 느린 코너 탈출, 타이어 나이 우위이며
  점수 순 상위 1~2개만 말한다.
- 한 랩에 추월 무전은 `overtake_per_lap`(기본 1)회다.
- 무전 종류는 overtake이며 문장은 `radio/templates.py`의 추월 틀에서 만든다.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from f1re.coaching.metrics import corner_metrics
from f1re.demo_sender.driving import CornerEffect, DriverProfile, generate_lap
from f1re.demo_sender.scenarios import SCENARIO_DIR, load_scenarios
from f1re.laps.traces import LapTrace, Sample
from f1re.overtake.advisor import ERS_STORE_MAX_J, OvertakeAdvisor, OvertakeConfig
from f1re.radio.queue import RadioKind, RadioQueue
from f1re.radio.templates import TEMPLATES
from f1re.tracks.model import Corner, Track, load_outline, load_track

SUZUKA_ID = 13
# F1 25 공식 세션 종류(EA UDP 문서, SCRUM-131): 15=레이스, 18=타임 트라이얼.
SESSION_TYPE_RACE = 15
SESSION_TYPE_TIME_TRIAL = 18
PLAYER = 0
AHEAD = 1
OTHER_AHEAD = 3
NOW_NS = 1_000_000_000

_TRACK = load_track(SUZUKA_ID)
_OUTLINE = load_outline(SUZUKA_ID)
assert _TRACK is not None and _OUTLINE is not None
TRACK: Track = _TRACK

SCENARIO_PATH = SCENARIO_DIR / "overtake.yaml"


# ---------------------------------------------------------------------------
# 도우미: 스냅샷(StateStore.snapshot() 모양)


def _lap_data_entry(
    *,
    lap: int = 0,
    distance: float = 0.0,
    position: int = 0,
    total: float = 0.0,
    driver_status: int = 4,
    pit_status: int = 0,
    result_status: int = 2,
) -> dict[str, Any]:
    return {
        "current_lap_num": lap,
        "lap_distance": distance,
        "car_position": position,
        "total_distance": total,
        "delta_to_car_in_front_ms_part": 0,
        "delta_to_car_in_front_minutes_part": 0,
        "current_lap_invalid": 0,
        "driver_status": driver_status,
        "pit_status": pit_status,
        "result_status": result_status,
        "penalties": 0,
        "total_warnings": 0,
        "corner_cutting_warnings": 0,
        "num_unserved_drive_through_pens": 0,
        "num_unserved_stop_go_pens": 0,
    }


def _car_status(
    *,
    drs_allowed: int = 0,
    drs_distance: int = 0,
    tyre_age: int = 6,
    compound: int = 18,
    ers_energy: float = ERS_STORE_MAX_J * 0.9,
    ers_mode: int = 2,
    front_left_wing_damage: int = 0,
    front_right_wing_damage: int = 0,
) -> dict[str, Any]:
    return {
        "drs_allowed": drs_allowed,
        "drs_activation_distance": drs_distance,
        "actual_tyre_compound": compound,
        "tyres_age_laps": tyre_age,
        "ers_store_energy": ers_energy,
        "ers_deploy_mode": ers_mode,
        "front_left_wing_damage": front_left_wing_damage,
        "front_right_wing_damage": front_right_wing_damage,
    }


def _telemetry(
    *,
    player_speed: float = 250.0,
    player_throttle: float = 1.0,
    ahead_speed: float = 240.0,
    ahead_throttle: float = 0.9,
    ahead_index: int | None = AHEAD,
) -> dict[str, Any]:
    """Car Telemetry 본문. 추월 성공 기여 요인 비교용."""
    entries: list[dict[str, Any]] = [
        {"speed": 0.0, "throttle": 0.0} for _ in range(22)
    ]
    entries[PLAYER] = {
        "speed": player_speed,
        "throttle": player_throttle,
    }
    if ahead_index is not None:
        entries[ahead_index] = {
            "speed": ahead_speed,
            "throttle": ahead_throttle,
        }
    return {"car_telemetry_data": entries}


def _snapshot(
    *,
    gap_s: float = 0.6,
    lap: int = 2,
    player_distance: float = 1000.0,
    session_type: int = SESSION_TYPE_RACE,
    safety_car: int = 0,
    player_status: dict[str, Any] | None = None,
    ahead_status: dict[str, Any] | None = None,
    ahead_index: int | None = AHEAD,
    player_position: int = 10,
    previous_ahead_behind: bool = False,
    telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """차 22대 상태를 갖춘 스냅샷을 만든다.

    `gap_s`는 내 Lap Data의 `delta_to_car_in_front`(앞차와의 시간 간격)다.
    `previous_ahead_behind=True`면 원래 앞차(AHEAD)가 내 뒤로 간 상태를 만든다
    (추월 직후). 이때 `ahead_index`가 새 앞차다.
    """
    entries = [_lap_data_entry() for _ in range(22)]
    player_total = (lap - 1) * TRACK.length_m + player_distance
    entries[PLAYER] = _lap_data_entry(
        lap=lap,
        distance=player_distance,
        position=player_position,
        total=player_total,
    )
    gap_ms = int(round(gap_s * 1000.0))
    entries[PLAYER]["delta_to_car_in_front_minutes_part"] = gap_ms // 60_000
    entries[PLAYER]["delta_to_car_in_front_ms_part"] = gap_ms % 60_000

    if previous_ahead_behind:
        entries[AHEAD] = _lap_data_entry(
            lap=lap,
            distance=max(0.0, player_distance - 80.0),
            position=player_position + 1,
            total=player_total - 80.0,
        )
    if ahead_index is not None:
        entries[ahead_index] = _lap_data_entry(
            lap=lap,
            distance=player_distance,
            position=player_position - 1,
            total=player_total + max(1.0, gap_s * 55.0),
        )

    statuses = [_car_status() for _ in range(22)]
    statuses[PLAYER] = _car_status(**(player_status or {}))
    if ahead_index is not None:
        statuses[ahead_index] = _car_status(**(ahead_status or {}))

    return {
        "session_uid": 1,
        "player_car_index": PLAYER,
        "game_paused": False,
        "packets": {
            "session": {
                "session_type": session_type,
                "track_id": SUZUKA_ID,
                "safety_car_status": safety_car,
            },
            "lap_data": {"lap_data": entries},
            "car_status": {"car_status_data": statuses},
            "car_telemetry": telemetry or {},
        },
        "lap_validity": [],
        "diagnostics": {},
    }


def _after_pass(
    *, telemetry: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """추월 직후 스냅샷. 원래 앞차(AHEAD)가 내 뒤에 있고 새 앞차는 없다."""
    return _snapshot(
        gap_s=5.0,
        ahead_index=None,
        previous_ahead_behind=True,
        telemetry=telemetry,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 도우미: 랩 트레이스(작업 14의 모든 차 LapTrace)


def _trace(
    *,
    car_index: int = PLAYER,
    lap: int = 2,
    brake_shift_corner: int | None = None,
    brake_shift_m: float = 30.0,
) -> LapTrace:
    """데모 주행 모델로 만든 랩 하나.

    `brake_shift_corner`를 주면 그 코너에서 늦게 브레이크를 밟아 탈출 속도가
    낮아진다(앞차의 느린 탈출 비교용).
    """
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


def _corner(number: int) -> Corner:
    return next(corner for corner in TRACK.corners if corner.number == number)


def _advisor(**kwargs: Any) -> OvertakeAdvisor:
    return OvertakeAdvisor(OvertakeConfig(**kwargs))


def _message_texts(messages: list[Any]) -> list[str]:
    return [message.text for message in messages]


# ---------------------------------------------------------------------------
# 켜짐·꺼짐 (설계 6.5)


def test_activates_within_1s_and_deactivates() -> None:
    """1.0초 이하에서 켜지고, 간격이 벌어지면 꺼지며 다음 랩에 다시 켜진다."""
    advisor = _advisor()
    drs = {"drs_allowed": 1, "drs_distance": 150}

    messages = advisor.evaluate(
        _snapshot(gap_s=0.8, player_status=drs), None, NOW_NS
    )
    assert advisor.active
    assert messages and messages[0].kind == RadioKind.OVERTAKE.value

    # 간격이 벌어지면 끈다.
    messages = advisor.evaluate(
        _snapshot(gap_s=1.2, player_status=drs), None, NOW_NS + 10**9
    )
    assert messages == []
    assert not advisor.active

    # 다음 랩에 다시 1초 이하로 붙으면 다시 켜진다.
    messages = advisor.evaluate(
        _snapshot(gap_s=0.9, lap=3, player_status=drs), None, NOW_NS + 2 * 10**9
    )
    assert advisor.active
    assert len(messages) == 1


def test_overtake_turns_assist_off() -> None:
    """앞차를 추월해 그 차가 뒤로 가면 끈다(새 앞차가 1초 안이어도)."""
    advisor = _advisor()
    drs = {"drs_allowed": 1, "drs_distance": 150}
    advisor.evaluate(_snapshot(gap_s=0.8, player_status=drs), None, NOW_NS)
    assert advisor.active

    # 원래 앞차(AHEAD)는 내 뒤로 갔고, 새 앞차(OTHER_AHEAD)가 0.5초 앞이다.
    after = _snapshot(
        gap_s=0.5,
        player_status=drs,
        ahead_index=OTHER_AHEAD,
        previous_ahead_behind=True,
    )
    assert advisor.evaluate(after, None, NOW_NS + 10**9) == []
    assert not advisor.active

    # 다음 호출에서 새 앞차와의 간격은 여전히 1초 이하다. 추월 무전은 이미
    # 한 번 나갔으므로 이번 랩에는 말하지 않지만, 추월 안내는 다시 켜진다.
    assert advisor.evaluate(after, None, NOW_NS + 2 * 10**9) == []
    assert advisor.active


def test_no_overtake_in_time_trial() -> None:
    """타임 트라이얼은 AI 상대가 없으므로 추월 안내를 하지 않는다."""
    advisor = _advisor()
    messages = advisor.evaluate(
        _snapshot(
            gap_s=0.6,
            session_type=SESSION_TYPE_TIME_TRIAL,
            player_status={"drs_allowed": 1, "drs_distance": 150},
        ),
        {PLAYER: [_trace()]},
        NOW_NS,
    )

    assert messages == []
    assert not advisor.active


@pytest.mark.parametrize("session_type", [15, 16, 17])
def test_overtake_active_in_race_family(session_type: int) -> None:
    """F1 25 레이스(15~17)에서는 추월 안내가 켜진다(SCRUM-131)."""
    advisor = _advisor()
    messages = advisor.evaluate(
        _snapshot(
            gap_s=0.8,
            session_type=session_type,
            player_status={"drs_allowed": 1, "drs_distance": 150},
        ),
        None,
        NOW_NS,
    )

    assert advisor.active
    assert messages


def test_no_overtake_under_safety_car() -> None:
    """SC·VSC 중에는 추월 금지라 말하지 않는다."""
    advisor = _advisor()
    messages = advisor.evaluate(
        _snapshot(
            gap_s=0.6,
            safety_car=1,
            player_status={"drs_allowed": 1, "drs_distance": 150},
        ),
        None,
        NOW_NS,
    )

    assert messages == []
    assert not advisor.active


# ---------------------------------------------------------------------------
# 선택지 (설계 6.5)


def test_drs_and_ers_option() -> None:
    """DRS가 허용되고 ERS가 충분하면 "다음 직선에서 DRS랑 ERS 다 써."라고 말한다."""
    advisor = _advisor()
    messages = advisor.evaluate(
        _snapshot(
            gap_s=0.6,
            player_status={
                "drs_allowed": 1,
                "drs_distance": 150,
                "ers_energy": ERS_STORE_MAX_J * 0.9,
                "ers_mode": 2,
            },
        ),
        None,
        NOW_NS,
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.text == "앞차랑 0.6초야. 다음 직선에서 DRS랑 ERS 다 써."
    assert message.priority.name == "OVERTAKE"
    assert message.evidence["drs_activation_distance_m"] == 150
    assert message.evidence["ers_store_pct"] == pytest.approx(90.0)
    assert message.evidence["gap_s"] == pytest.approx(0.6)

    # ERS가 거의 없으면 DRS만 말한다.
    low_ers = _advisor()
    messages = low_ers.evaluate(
        _snapshot(
            gap_s=0.6,
            player_status={
                "drs_allowed": 1,
                "drs_distance": 150,
                "ers_energy": ERS_STORE_MAX_J * 0.1,
            },
        ),
        None,
        NOW_NS,
    )
    assert _message_texts(messages) == ["앞차랑 0.6초야. 다음 직선에서 DRS 써."]

    # DRS 활성 지점이 너무 멀면(기본 600m 초과) 아직 "다음 직선"이라고 말하지 않는다.
    far_drs = _advisor()
    messages = far_drs.evaluate(
        _snapshot(
            gap_s=0.6,
            player_status={"drs_allowed": 1, "drs_distance": 1200},
        ),
        None,
        NOW_NS,
    )
    assert messages == []


def test_gap_change_is_evidence() -> None:
    """간격 변화(초당 초)를 근거 수치로 남긴다(설계 6.5 판단 재료)."""
    advisor = _advisor()
    # 첫 호출: DRS가 아직 허용되지 않아 말할 선택지가 없지만 간격은 기록한다.
    assert advisor.evaluate(_snapshot(gap_s=0.9), None, NOW_NS) == []
    # 0.5초 뒤 간격이 0.6초로 줄었다. 초당 -0.6초씩 붙는 중이다.
    messages = advisor.evaluate(
        _snapshot(
            gap_s=0.6,
            player_status={"drs_allowed": 1, "drs_distance": 150},
        ),
        None,
        NOW_NS + 500_000_000,
    )

    assert len(messages) == 1
    assert messages[0].evidence["gap_change_s_per_s"] == pytest.approx(-0.6)


def test_ahead_slow_exit_option_names_corner() -> None:
    """앞차가 특정 코너 탈출이 느리면 그 코너를 이름으로 말한다."""
    corner = _corner(9)
    # 9번 코너 진입 200m 전. 다음 코너가 시야(기본 600m) 안에 있다.
    distance = corner.entry_m - 200.0
    traces = {
        PLAYER: [_trace(car_index=PLAYER)],
        AHEAD: [_trace(car_index=AHEAD, brake_shift_corner=9)],
    }
    # 근거 확인: 앞차의 9번 탈출 속도가 내 것보다 확실히 낮다.
    player_exit = corner_metrics(traces[PLAYER][0], corner).exit_speed
    ahead_exit = corner_metrics(traces[AHEAD][0], corner).exit_speed
    assert player_exit - ahead_exit >= 4.0

    advisor = _advisor()
    messages = advisor.evaluate(
        _snapshot(gap_s=0.6, player_distance=distance), traces, NOW_NS
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.text == (
        "앞차랑 0.6초야. 앞차가 9번 탈출이 느려. "
        "9번에서 바짝 붙어서 직선에서 넘어."
    )
    assert message.corner == 9
    assert message.evidence["corner"] == 9
    assert message.evidence["ahead_exit_speed_kmh"] == pytest.approx(
        round(ahead_exit, 1)
    )
    assert message.evidence["player_exit_speed_kmh"] == pytest.approx(
        round(player_exit, 1)
    )


def test_tyre_age_wait_option() -> None:
    """내 타이어가 앞차보다 충분히 새것이면 기다리는 선택을 말한다."""
    advisor = _advisor()
    messages = advisor.evaluate(
        _snapshot(
            gap_s=0.6,
            player_status={"tyre_age": 6, "compound": 18},
            ahead_status={"tyre_age": 13, "compound": 16},
        ),
        None,
        NOW_NS,
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.text == (
        "앞차랑 0.6초야. 우리 타이어가 7랩 더 새거야. 2랩만 기다렸다가 공격해."
    )
    assert message.evidence["tyre_advantage_laps"] == 7
    assert message.evidence["player_tyre_age_laps"] == 6
    assert message.evidence["ahead_tyre_age_laps"] == 13

    # 나이 차이가 문턱(기본 4랩)보다 작으면 말하지 않는다.
    quiet = _advisor()
    assert (
        quiet.evaluate(
            _snapshot(
                gap_s=0.6,
                player_status={"tyre_age": 10},
                ahead_status={"tyre_age": 12},
            ),
            None,
            NOW_NS,
        )
        == []
    )


def test_max_two_options_and_once_per_lap() -> None:
    """선택지는 상위 2개까지만, 기본 설정에서는 랩당 한 번만 말한다."""
    corner = _corner(9)
    distance = corner.entry_m - 200.0
    traces = {
        PLAYER: [_trace(car_index=PLAYER)],
        AHEAD: [_trace(car_index=AHEAD, brake_shift_corner=9)],
    }
    snapshot = _snapshot(
        gap_s=0.6,
        player_distance=distance,
        player_status={
            "drs_allowed": 1,
            "drs_distance": 150,
            "tyre_age": 6,
            "compound": 18,
        },
        ahead_status={"tyre_age": 13, "compound": 16},
    )

    # 세 선택지가 모두 조건을 만족해도 기본(랩당 1회)에서는 한 번만 말한다.
    advisor = _advisor()
    first = advisor.evaluate(snapshot, traces, NOW_NS)
    assert len(first) == 1
    assert advisor.evaluate(snapshot, traces, NOW_NS + 10**8) == []

    # 랩당 2회로 늘리면 상위 2개까지 말하고, 세 번째는 나가지 않는다.
    twice = _advisor(per_lap=2)
    pair = twice.evaluate(snapshot, traces, NOW_NS)
    assert len(pair) == 2
    assert pair[0].text == "앞차랑 0.6초야. 다음 직선에서 DRS랑 ERS 다 써."
    assert pair[1].text == (
        "앞차랑 0.6초야. 앞차가 9번 탈출이 느려. "
        "9번에서 바짝 붙어서 직선에서 넘어."
    )
    assert twice.evaluate(snapshot, traces, NOW_NS + 10**8) == []


# ---------------------------------------------------------------------------
# 감정 단계 (SCRUM-91)


def _choices(key: str, mood: str, **slots: object) -> list[str]:
    return [
        text.format(**slots)
        for text in TEMPLATES[key].texts_for(mood)
    ]


def test_close_battle_under_half_second_uses_hyped_wording() -> None:
    """간격 0.5초 이하 추월 무전은 격앙된 표현(hyped)으로 말한다."""
    advisor = _advisor()
    messages = advisor.evaluate(
        _snapshot(
            gap_s=0.5,
            player_status={"drs_allowed": 1, "drs_distance": 150},
        ),
        None,
        NOW_NS,
    )

    assert len(messages) == 1
    assert messages[0].evidence["mood"] == "hyped"
    assert messages[0].text in _choices(
        "overtake.drs_ers", "hyped", gap="0.5"
    )


def test_last_three_laps_uses_hyped_wording() -> None:
    """레이스 마지막 3랩이면 간격이 0.6초여도 격앙 표현을 쓴다."""
    advisor = _advisor()
    snapshot = _snapshot(
        gap_s=0.6,
        lap=48,
        player_status={"drs_allowed": 1, "drs_distance": 150},
    )
    snapshot["packets"]["session"]["total_laps"] = 50

    messages = advisor.evaluate(snapshot, None, NOW_NS)

    assert len(messages) == 1
    assert messages[0].evidence["mood"] == "hyped"


def test_normal_overtake_uses_calm_wording() -> None:
    """레이스 중반, 간격이 0.5초를 넘으면 calm 표현을 쓴다."""
    advisor = _advisor()
    messages = advisor.evaluate(
        _snapshot(
            gap_s=0.6,
            player_status={"drs_allowed": 1, "drs_distance": 150},
        ),
        None,
        NOW_NS,
    )

    assert len(messages) == 1
    assert messages[0].evidence["mood"] == "calm"


def test_short_race_last_laps_uses_hyped_wording() -> None:
    """5랩 단축 레이스도 남은 랩 3 이하면 격앙 표현을 쓴다."""
    advisor = _advisor()
    snapshot = _snapshot(
        gap_s=0.6,
        lap=3,
        player_status={"drs_allowed": 1, "drs_distance": 150},
    )
    snapshot["packets"]["session"]["total_laps"] = 5

    messages = advisor.evaluate(snapshot, None, NOW_NS)

    assert len(messages) == 1
    assert messages[0].evidence["mood"] == "hyped"


@pytest.mark.parametrize(
    ("session_type", "total_laps"),
    [
        (1, 50),  # FP1: 레이스 세션이 아니다.
        (5, 50),  # Q1: 레이스 세션이 아니다.
        (SESSION_TYPE_RACE, 0),  # 레이스지만 total_laps가 없다.
    ],
)
def test_no_last_lap_wording_without_race_distance(
    session_type: int, total_laps: int
) -> None:
    """레이스 거리가 없으면 48랩째여도 '마지막 랩들'로 보지 않는다."""
    advisor = _advisor()
    snapshot = _snapshot(
        gap_s=0.6,
        lap=48,
        session_type=session_type,
        player_status={"drs_allowed": 1, "drs_distance": 150},
    )
    snapshot["packets"]["session"]["total_laps"] = total_laps

    messages = advisor.evaluate(snapshot, None, NOW_NS)

    assert len(messages) == 1
    assert messages[0].evidence["mood"] == "calm"


# ---------------------------------------------------------------------------
# 무전 계층 연동 (설계 6.7)


def test_overtake_kind_can_be_muted() -> None:
    """대시보드에서 추월 무전을 끄면 큐에 들어가지 않고 결과에도 없다."""
    queue = RadioQueue(clock=lambda: NOW_NS)
    assert queue.set_muted(RadioKind.OVERTAKE.value, True)
    advisor = OvertakeAdvisor(queue=queue)

    messages = advisor.evaluate(
        _snapshot(
            gap_s=0.6,
            player_status={"drs_allowed": 1, "drs_distance": 150},
        ),
        None,
        NOW_NS,
    )

    assert messages == []
    assert queue.pending_count == 0


def test_demo_overtake_scenario_options_are_supported() -> None:
    """데모 overtake.yaml의 시나리오 종류가 모두 선택지 재료로 쓰인다."""
    events = load_scenarios(SCENARIO_PATH)
    kinds = {event.kind for event in events}

    assert kinds == {
        "close_to_car_ahead",
        "drs_available",
        "tyre_age_gap",
        "ahead_slow_exit",
    }


# ---------------------------------------------------------------------------
# 코너 데이터가 없는 서킷(리뷰 포커스 5)


def test_unofficial_corners_use_ordinal_wording() -> None:
    """자동 탐지 코너만 있으면 "9번째 코너"처럼 공식 번호가 아님을 알린다."""
    corner = _corner(9)
    auto_track = Track(
        track_id=SUZUKA_ID,
        name="스즈카(자동 탐지)",
        length_m=TRACK.length_m,
        corners=[
            Corner(
                number=index + 1,
                official=False,
                entry_m=item.entry_m,
                apex_m=item.apex_m,
                exit_m=item.exit_m,
            )
            for index, item in enumerate(TRACK.corners)
        ],
    )
    traces = {
        PLAYER: [_trace(car_index=PLAYER)],
        AHEAD: [_trace(car_index=AHEAD, brake_shift_corner=9)],
    }
    advisor = OvertakeAdvisor(track=auto_track)
    messages = advisor.evaluate(
        _snapshot(gap_s=0.6, player_distance=corner.entry_m - 200.0),
        traces,
        NOW_NS,
    )

    assert len(messages) == 1
    assert messages[0].text == (
        "앞차랑 0.6초야. 앞차가 9번째 코너 탈출이 느려. "
        "9번째 코너에서 바짝 붙어서 직선에서 넘어."
    )
    assert messages[0].evidence["corner_official"] is False


# ---------------------------------------------------------------------------
# 추월 성공 긍정 코칭(SCRUM-145)

PASS_AT_NS = NOW_NS + 10**9


def _success_sequence(
    *,
    player_status: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    traces: dict[int, list[LapTrace]] | None = None,
    distance: float = 1000.0,
) -> tuple[OvertakeAdvisor, dict[str, Any], list[Any]]:
    """추월 직전 2프레임 → 추월 감지 → 안정 확인(1.6초)까지 돌린다."""
    advisor = _advisor()
    for offset, gap in ((0, 0.6), (500_000_000, 0.55)):
        advisor.evaluate(
            _snapshot(
                gap_s=gap,
                player_distance=distance,
                player_status=player_status,
                telemetry=telemetry,
            ),
            traces,
            NOW_NS + offset,
        )
    after = _after_pass(
        player_status=player_status,
        telemetry=telemetry,
        player_distance=distance,
    )
    assert advisor.evaluate(after, traces, PASS_AT_NS) == []
    # 안정 시간(1.5초)이 지나기 전에는 말하지 않는다.
    assert advisor.evaluate(after, traces, PASS_AT_NS + 500_000_000) == []
    messages = advisor.evaluate(after, traces, PASS_AT_NS + 1_600_000_000)
    return advisor, after, messages


def test_overtake_success_praises_once_with_drs_evidence() -> None:
    """추월 직전 DRS를 썼으면 안정 확인 뒤 한 번만 칭찬한다."""
    advisor, after, messages = _success_sequence(
        player_status={"drs_allowed": 1, "drs_distance": 150},
        telemetry=_telemetry(),
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.kind == RadioKind.OVERTAKE.value
    assert message.priority.name == "OVERTAKE"
    assert message.evidence["overtake_success"] == "drs"
    assert message.evidence["overtaken_car_index"] == AHEAD
    assert message.evidence["mood"] == "warm"
    assert message.evidence["drs_samples"] == 3
    assert message.text in _choices("overtake.success_drs", "warm")

    # 추월 1건당 1회: 시간이 더 지나도 다시 말하지 않는다.
    assert advisor.evaluate(after, None, PASS_AT_NS + 3_000_000_000) == []


def test_overtake_success_with_drs_and_ers_uses_both_template() -> None:
    """DRS와 ERS를 함께 썼으면 두 가지를 함께 짚는 틀을 쓴다."""
    _, _, messages = _success_sequence(
        player_status={
            "drs_allowed": 1,
            "drs_distance": 150,
            "ers_mode": 3,
        },
        telemetry=_telemetry(),
    )

    assert len(messages) == 1
    assert messages[0].evidence["overtake_success"] == "drs_ers"
    assert messages[0].evidence["drs_used"] is True
    assert messages[0].evidence["ers_used"] is True


def test_overtake_success_requires_evidence() -> None:
    """DRS·텔레메트리·랩 트레이스 근거가 없으면 억지 칭찬을 하지 않는다."""
    _, _, messages = _success_sequence()

    assert messages == []


def test_overtake_success_ignores_zeroed_opponent_telemetry() -> None:
    """다른 차 텔레메트리가 0으로 오는 세션에서는 우위를 지어내지 않는다."""
    _, _, messages = _success_sequence(
        telemetry=_telemetry(ahead_speed=0.0, ahead_throttle=0.0)
    )

    assert messages == []


def test_overtake_success_uses_exit_speed_evidence() -> None:
    """직전 코너 탈출 속도 우위가 가장 큰 근거면 그 요인으로 칭찬한다."""
    corner = _corner(9)
    traces = {
        PLAYER: [_trace(car_index=PLAYER)],
        AHEAD: [_trace(car_index=AHEAD, brake_shift_corner=9)],
    }
    player_exit = corner_metrics(traces[PLAYER][0], corner).exit_speed
    ahead_exit = corner_metrics(traces[AHEAD][0], corner).exit_speed
    assert player_exit - ahead_exit >= 5.0

    _, _, messages = _success_sequence(
        distance=corner.exit_m + 50.0,
        traces=traces,
        telemetry=_telemetry(ahead_throttle=1.0),
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.evidence["overtake_success"] == "exit"
    assert message.evidence["corner"] == 9
    assert message.corner == 9
    assert message.evidence["exit_advantage_kmh"] == pytest.approx(
        round(player_exit - ahead_exit, 1)
    )
    assert "탈출" in message.text


def test_overtake_success_silent_when_overtaken_car_pitted() -> None:
    """추월 대상 차가 피트 레인에 있었으면 피트인 변동으로 보고 칭찬하지 않는다."""
    advisor = _advisor()
    telemetry = _telemetry()
    advisor.evaluate(_snapshot(gap_s=0.6, telemetry=telemetry), None, NOW_NS)
    pitting = _snapshot(gap_s=0.6, telemetry=telemetry)
    pitting["packets"]["lap_data"]["lap_data"][AHEAD]["pit_status"] = 1
    advisor.evaluate(pitting, None, NOW_NS + 500_000_000)

    after = _after_pass(telemetry=telemetry)
    results = [
        advisor.evaluate(after, None, PASS_AT_NS + offset)
        for offset in (0, 500_000_000, 1_600_000_000, 2_500_000_000)
    ]
    assert all(result == [] for result in results)


def test_overtake_success_silent_on_reovertake() -> None:
    """재역전이 나오면 그 추월은 칭찬하지 않는다."""
    advisor = _advisor()
    telemetry = _telemetry()
    drs = {"drs_allowed": 1, "drs_distance": 150}
    advisor.evaluate(
        _snapshot(gap_s=0.6, player_status=drs, telemetry=telemetry),
        None,
        NOW_NS,
    )
    after = _after_pass(telemetry=telemetry, player_status=drs)
    assert advisor.evaluate(after, None, PASS_AT_NS) == []

    # 그 차가 다시 앞으로 나왔다(재역전).
    reovertake = _after_pass(telemetry=telemetry, player_status=drs)
    entries = reovertake["packets"]["lap_data"]["lap_data"]
    entries[AHEAD]["total_distance"] = entries[PLAYER]["total_distance"] + 100.0
    entries[AHEAD]["car_position"] = entries[PLAYER]["car_position"] - 1
    assert advisor.evaluate(reovertake, None, PASS_AT_NS + 300_000_000) == []

    # 재역전한 채로 유지되면 안정 시간이 지나도 칭찬하지 않는다.
    assert advisor.evaluate(reovertake, None, PASS_AT_NS + 800_000_000) == []
    assert advisor.evaluate(reovertake, None, PASS_AT_NS + 2_000_000_000) == []


def test_overtake_success_silent_on_off_track() -> None:
    """추월 직후 트랙 이탈로 랩이 무효가 되면 칭찬하지 않는다."""
    advisor = _advisor()
    telemetry = _telemetry()
    advisor.evaluate(_snapshot(gap_s=0.6, telemetry=telemetry), None, NOW_NS)
    after = _after_pass(telemetry=telemetry)
    assert advisor.evaluate(after, None, PASS_AT_NS) == []

    off_track = _after_pass(telemetry=telemetry)
    off_track["packets"]["lap_data"]["lap_data"][PLAYER]["current_lap_invalid"] = 1
    assert advisor.evaluate(off_track, None, PASS_AT_NS + 300_000_000) == []

    # 이후 깨끗해져도 이미 버린 추월은 칭찬하지 않는다.
    assert advisor.evaluate(after, None, PASS_AT_NS + 1_000_000_000) == []
    assert advisor.evaluate(after, None, PASS_AT_NS + 2_000_000_000) == []


def test_overtake_success_silent_on_penalty_or_damage() -> None:
    """페널티·앞날개 손상이 늘면 접촉으로 보고 칭찬하지 않는다."""
    telemetry = _telemetry()

    # (a) 추월 직후 페널티 누적.
    advisor = _advisor()
    advisor.evaluate(_snapshot(gap_s=0.6, telemetry=telemetry), None, NOW_NS)
    after = _after_pass(telemetry=telemetry)
    assert advisor.evaluate(after, None, PASS_AT_NS) == []
    penalty = _after_pass(telemetry=telemetry)
    penalty["packets"]["lap_data"]["lap_data"][PLAYER]["penalties"] = 5
    assert advisor.evaluate(penalty, None, PASS_AT_NS + 300_000_000) == []
    assert advisor.evaluate(after, None, PASS_AT_NS + 2_000_000_000) == []

    # (b) 추월 직후 앞날개 손상 증가(접촉).
    advisor = _advisor()
    advisor.evaluate(_snapshot(gap_s=0.6, telemetry=telemetry), None, NOW_NS)
    assert advisor.evaluate(after, None, PASS_AT_NS) == []
    damage = _after_pass(
        telemetry=telemetry, player_status={"front_left_wing_damage": 25}
    )
    assert advisor.evaluate(damage, None, PASS_AT_NS + 300_000_000) == []
    assert advisor.evaluate(after, None, PASS_AT_NS + 2_000_000_000) == []


def test_overtake_success_respects_muted_kind() -> None:
    """대시보드에서 추월 무전을 끄면 추월 칭찬도 나가지 않는다."""
    queue = RadioQueue(clock=lambda: NOW_NS)
    assert queue.set_muted(RadioKind.OVERTAKE.value, True)
    advisor = OvertakeAdvisor(
        OvertakeConfig(), queue=queue, clock=lambda: NOW_NS
    )

    advisor.evaluate(
        _snapshot(
            gap_s=0.6,
            player_status={"drs_allowed": 1, "drs_distance": 150},
            telemetry=_telemetry(),
        ),
        None,
        NOW_NS,
    )
    after = _after_pass(
        player_status={"drs_allowed": 1, "drs_distance": 150},
        telemetry=_telemetry(),
    )
    advisor.evaluate(after, None, PASS_AT_NS)
    assert advisor.evaluate(after, None, PASS_AT_NS + 500_000_000) == []
    assert advisor.evaluate(after, None, PASS_AT_NS + 1_600_000_000) == []
    assert queue.pending_count == 0
