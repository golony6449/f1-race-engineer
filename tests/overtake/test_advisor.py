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
from f1re.tracks.model import Corner, Track, load_outline, load_track

SUZUKA_ID = 13
SESSION_TYPE_RACE = 10
SESSION_TYPE_TIME_TRIAL = 13
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
) -> dict[str, Any]:
    return {
        "current_lap_num": lap,
        "lap_distance": distance,
        "car_position": position,
        "total_distance": total,
        "delta_to_car_in_front_ms_part": 0,
        "delta_to_car_in_front_minutes_part": 0,
        "current_lap_invalid": 0,
        "driver_status": 0,
        "pit_status": 0,
    }


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
        },
        "lap_validity": [],
        "diagnostics": {},
    }


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
