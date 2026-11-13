"""시나리오 주입 테스트: YAML 형식·주행 효과·그랑프리 패킷 값."""

from __future__ import annotations

import collections
import math
import random
from pathlib import Path

import pytest

from f1re.demo_sender.driving import (
    CornerEffect,
    DriverProfile,
    Sample,
    generate_lap,
)
from f1re.demo_sender.scenarios import (
    KINDS,
    SCENARIO_DIR,
    ScenarioError,
    ScenarioEvent,
    corner_effects,
    load_scenarios,
    parse_scenarios,
)
from f1re.demo_sender.sender import DemoSender, main
from f1re.tracks.model import Corner, Track, load_outline, load_track
from f1re.tracks.outline import TrackOutline
from f1re.udp.parser import Packet, parse_packet
from f1re.udp.spec import PACKET_SIZES, PacketId

SUZUKA_ID = 13
MONACO_ID = 5


class _CollectingSocket:
    """실제 네트워크 없이 보낸 바이트를 모으는 소켓 대역."""

    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def sendto(self, payload: bytes, address: tuple[str, int]) -> None:
        self.packets.append(payload)

    def close(self) -> None:
        pass


def _track_and_outline(track_id: int = SUZUKA_ID):
    track = load_track(track_id)
    outline = load_outline(track_id)
    assert track is not None and outline is not None
    return track, outline


def _sender(
    *,
    mode: str = "gp",
    laps: int = 2,
    scenarios: list[ScenarioEvent] | None = None,
    seed: int = 0,
    track_id: int = SUZUKA_ID,
    sock: _CollectingSocket | None = None,
) -> tuple[DemoSender, _CollectingSocket]:
    track, outline = _track_and_outline(track_id)
    collector = sock or _CollectingSocket()
    sender = DemoSender(
        track,
        outline,
        laps=laps,
        seed=seed,
        speed=10_000.0,
        mode=mode,
        scenarios=scenarios,
        sock=collector,  # type: ignore[arg-type]
    )
    return sender, collector


def _run(
    scenarios: list[ScenarioEvent] | None = None,
    *,
    mode: str = "gp",
    laps: int = 2,
    seed: int = 0,
    track_id: int = SUZUKA_ID,
    duration_s: float | None = None,
) -> list[Packet]:
    sender, collector = _sender(
        mode=mode, laps=laps, scenarios=scenarios, seed=seed, track_id=track_id
    )
    sender.run(duration_s=duration_s)
    return [parse_packet(payload) for payload in collector.packets]


def _tiny_track() -> tuple[Track, TrackOutline]:
    """시나리오 패킷 값만 빠르게 보기 위한 짧은 원형 서킷(랩 약 7초)."""
    length = 400.0
    radius = length / (2.0 * math.pi)
    points = []
    for index in range(120):
        angle = 2.0 * math.pi * index / 120
        points.append(
            (length * index / 120, radius * math.cos(angle), radius * math.sin(angle))
        )
    corners = [
        Corner(1, True, 60.0, 100.0, 140.0),
        Corner(2, True, 260.0, 300.0, 340.0),
    ]
    track = Track(track_id=99, name="테스트 서킷", length_m=length, corners=corners)
    return track, TrackOutline(points)


def _run_tiny(
    scenarios: list[ScenarioEvent] | None = None,
    *,
    laps: int = 2,
) -> list[Packet]:
    track, outline = _tiny_track()
    collector = _CollectingSocket()
    sender = DemoSender(
        track,
        outline,
        laps=laps,
        seed=0,
        speed=10_000.0,
        mode="gp",
        scenarios=scenarios,
        sock=collector,  # type: ignore[arg-type]
    )
    sender.run()
    return [parse_packet(payload) for payload in collector.packets]


def _of(packets: list[Packet], packet_id: PacketId) -> list[Packet]:
    return [packet for packet in packets if packet.header.packet_id == packet_id]


def _event_codes(packets: list[Packet]) -> list[str]:
    return [packet.body["event_string_code"] for packet in _of(packets, PacketId.EVENT)]


def _event(lap: int, kind: str, corner: int | None = None, **params: float | int) -> ScenarioEvent:
    return ScenarioEvent(lap=lap, kind=kind, corner=corner, params=dict(params))


def _corner(track, number: int):
    return next(corner for corner in track.corners if corner.number == number)


def _brake_start(samples: list[Sample], start_m: float, end_m: float) -> float | None:
    """구간 안에서 처음으로 강하게 브레이크를 밟는 랩 거리."""
    for sample in samples:
        if start_m <= sample.dist_m <= end_m and sample.brake >= 0.5:
            return sample.dist_m
    return None


# ---------------------------------------------------------------------------
# YAML 형식


def test_scenario_yaml_files_load() -> None:
    """레포에 둔 시나리오 파일이 모두 형식에 맞고 두 서킷의 코너만 가리킨다."""
    names = [
        "coaching.yaml",
        "overtake.yaml",
        "alerts.yaml",
        "lift_and_coast.yaml",
        "flags.yaml",
    ]
    events: list[ScenarioEvent] = []
    for name in names:
        loaded = load_scenarios(SCENARIO_DIR / name)
        assert loaded, name
        events.extend(loaded)

    assert {event.kind for event in events} >= {
        "late_brake",
        "early_brake",
        "early_turn_in",
        "steer_corrections",
        "late_throttle",
        "off_track",
        "close_to_car_ahead",
        "drs_available",
        "ahead_slow_exit",
        "tyre_age_gap",
        "tyre_wear_critical",
        "tyre_temp_critical",
        "brake_temp_critical",
        "fuel_short",
        "front_wing_damage",
        "penalty",
        "safety_car",
        "vsc",
        "yellow_flag",
        "blue_flag",
        "red_flag",
    }
    assert {event.kind for event in events} <= KINDS
    for track_id in (SUZUKA_ID, MONACO_ID):
        track, _ = _track_and_outline(track_id)
        corners = {corner.number for corner in track.corners}
        for event in events:
            if event.corner is not None:
                assert event.corner in corners, (track_id, event)
            assert event.lap >= 1


def test_parse_rejects_bad_scenarios() -> None:
    with pytest.raises(ScenarioError):
        parse_scenarios({"lap": 1, "kind": "late_brake"})
    with pytest.raises(ScenarioError):
        parse_scenarios([{"lap": 1, "kind": "no_such_kind"}])
    with pytest.raises(ScenarioError):
        parse_scenarios([{"lap": 0, "kind": "late_brake"}])
    with pytest.raises(ScenarioError):
        parse_scenarios([{"lap": 1.5, "kind": "late_brake"}])
    with pytest.raises(ScenarioError):
        parse_scenarios([{"lap": 1, "kind": "late_brake", "params": {"metres": 5}}])
    with pytest.raises(ScenarioError):
        parse_scenarios([{"lap": 1, "kind": "ahead_slow_exit"}])
    with pytest.raises(ScenarioError):
        parse_scenarios([{"lap": 1, "kind": "late_brake", "extra": 1}])
    # 깃발 시나리오는 구역(0~1)과 랩 수를 검증한다(SCRUM-80).
    with pytest.raises(ScenarioError):
        parse_scenarios(
            [{"lap": 1, "kind": "yellow_flag", "params": {"start": 0.6, "end": 0.2}}]
        )
    with pytest.raises(ScenarioError):
        parse_scenarios(
            [{"lap": 1, "kind": "yellow_flag", "params": {"start": -0.1}}]
        )
    with pytest.raises(ScenarioError):
        parse_scenarios(
            [{"lap": 1, "kind": "yellow_flag", "params": {"end": 1.5}}]
        )
    with pytest.raises(ScenarioError):
        parse_scenarios([{"lap": 1, "kind": "blue_flag", "params": {"laps": 0}}])
    with pytest.raises(ScenarioError):
        parse_scenarios([{"lap": 1, "kind": "red_flag", "params": {"laps": 1.5}}])


def test_parse_fills_default_params() -> None:
    events = parse_scenarios([{"lap": 2, "corner": 3, "kind": "late_brake"}])
    assert events == [
        ScenarioEvent(
            lap=2,
            kind="late_brake",
            corner=3,
            params={"meters": 10.0, "width_m": 1.5},
        )
    ]


# ---------------------------------------------------------------------------
# 주행 효과


def test_late_brake_shifts_brake_point() -> None:
    """지정 랩·코너의 브레이크 시작이 params.m만큼 뒤로 가고 라인 밖으로 밀린다."""
    track, outline = _track_and_outline()
    corner = _corner(track, 3)
    previous_exit = _corner(track, 2).exit_m
    driver = DriverProfile(consistency=1.0)

    baseline = generate_lap(track, outline, driver, 1, random.Random(0))
    events = [_event(1, "late_brake", corner=3, meters=10.0)]
    effects = corner_effects(events, [item.number for item in track.corners])
    scenario = generate_lap(
        track, outline, driver, 1, random.Random(0), corner_effects=effects
    )

    base_start = _brake_start(baseline, previous_exit, corner.apex_m)
    scenario_start = _brake_start(scenario, previous_exit, corner.apex_m)
    assert base_start is not None and scenario_start is not None
    shift = scenario_start - base_start
    assert 5.0 <= shift <= 20.0, shift

    # 정점(최저 속도)도 뒤로 밀리고, 라인은 코너 바깥으로 나간다.
    base_slowest = min(
        (sample for sample in baseline if corner.entry_m <= sample.dist_m <= corner.exit_m),
        key=lambda sample: sample.speed_kmh,
    )
    scenario_slowest = min(
        (sample for sample in scenario if corner.entry_m <= sample.dist_m <= corner.exit_m),
        key=lambda sample: sample.speed_kmh,
    )
    assert scenario_slowest.dist_m > base_slowest.dist_m
    inside = [
        sample for sample in scenario if corner.entry_m <= sample.dist_m <= corner.exit_m
    ]
    assert max(abs(sample.line_offset_m) for sample in inside) >= 1.0
    assert all(sample.line_offset_m == 0.0 for sample in baseline)

    # 지정하지 않은 코너는 그대로다.
    other = _corner(track, 5)
    assert all(
        sample.line_offset_m == 0.0
        for sample in scenario
        if other.entry_m <= sample.dist_m <= other.exit_m
    )


def test_late_brake_works_on_both_laps_and_tracks() -> None:
    """모나코에서도 같은 효과가 나고, 효과는 지정한 랩에만 들어간다."""
    track, outline = _track_and_outline(MONACO_ID)
    corner = _corner(track, 3)
    previous_exit = _corner(track, 2).exit_m
    driver = DriverProfile(consistency=1.0)
    effects = corner_effects(
        [_event(2, "late_brake", corner=3, meters=8.0)],
        [item.number for item in track.corners],
    )
    baseline = generate_lap(track, outline, driver, 2, random.Random(3))
    scenario = generate_lap(
        track, outline, driver, 2, random.Random(3), corner_effects=effects
    )
    assert _brake_start(scenario, previous_exit, corner.apex_m) > _brake_start(
        baseline, previous_exit, corner.apex_m
    )


def _local_curvature(outline: TrackOutline, dist_m: float) -> float:
    """중심선 두 점의 진행 방향 차이로 그 지점의 곡률(1/m)을 구한다."""
    step = 5.0
    x0, z0 = outline.position_at(dist_m - step)
    x1, z1 = outline.position_at(dist_m)
    x2, z2 = outline.position_at(dist_m + step)
    first = math.atan2(z1 - z0, x1 - x0)
    second = math.atan2(z2 - z1, x2 - x1)
    delta = (second - first + math.pi) % (2.0 * math.pi) - math.pi
    return delta / step


def test_line_offset_goes_to_corner_outside() -> None:
    """라인 이탈은 회전 중심 반대쪽(코너 바깥)으로 간다."""
    track, outline = _track_and_outline()
    corner = _corner(track, 3)
    effects = corner_effects(
        [_event(1, "late_brake", corner=3, meters=10.0)],
        [item.number for item in track.corners],
    )
    samples = generate_lap(
        track,
        outline,
        DriverProfile(consistency=1.0),
        1,
        random.Random(0),
        corner_effects=effects,
    )
    inside = [
        sample for sample in samples if corner.entry_m <= sample.dist_m <= corner.exit_m
    ]
    worst = max(inside, key=lambda sample: abs(sample.line_offset_m))
    assert worst.line_offset_m != 0.0

    curvature = _local_curvature(outline, worst.dist_m)
    assert abs(curvature) > 1e-5
    radius = 1.0 / abs(curvature)
    heading = outline.heading_at(worst.dist_m)
    normal_x, normal_z = -math.sin(heading), math.cos(heading)
    if curvature < 0:
        normal_x, normal_z = -normal_x, -normal_z
    center_x, center_z = outline.position_at(worst.dist_m)
    center_x += radius * normal_x
    center_z += radius * normal_z
    assert math.dist((worst.x, worst.z), (center_x, center_z)) > radius + 0.5

    # 반대쪽(코너 안쪽)으로 갔다면 중심까지의 거리가 반지름보다 작아진다.
    inward_distance = math.dist(
        (
            outline.position_at(worst.dist_m)[0],
            outline.position_at(worst.dist_m)[1],
        ),
        (center_x, center_z),
    )
    assert math.isclose(inward_distance, radius, rel_tol=0.05)


def test_off_track_moves_car_outside_line() -> None:
    track, outline = _track_and_outline()
    corner = _corner(track, 5)
    effects = corner_effects(
        [_event(1, "off_track", corner=5, width_m=7.0)],
        [item.number for item in track.corners],
    )
    samples = generate_lap(
        track,
        outline,
        DriverProfile(consistency=1.0),
        1,
        random.Random(0),
        corner_effects=effects,
    )
    inside = [
        sample for sample in samples if corner.entry_m <= sample.dist_m <= corner.exit_m
    ]
    assert max(abs(sample.line_offset_m) for sample in inside) >= 6.5


def test_late_throttle_delays_throttle_on() -> None:
    track, outline = _track_and_outline()
    corner = _corner(track, 3)
    driver = DriverProfile(consistency=1.0)
    baseline = generate_lap(track, outline, driver, 1, random.Random(0))
    effects = corner_effects(
        [_event(1, "late_throttle", corner=3, meters=35.0)],
        [item.number for item in track.corners],
    )
    scenario = generate_lap(
        track, outline, driver, 1, random.Random(0), corner_effects=effects
    )

    def throttle_on(samples: list[Sample]) -> float:
        for sample in samples:
            if corner.apex_m <= sample.dist_m <= corner.exit_m and sample.throttle > 0.5:
                return sample.dist_m
        raise AssertionError("스로틀을 다시 밟는 지점이 없습니다")

    assert throttle_on(scenario) - throttle_on(baseline) >= 20.0


def test_early_turn_in_moves_turn_in_earlier() -> None:
    track, outline = _track_and_outline()
    corner = _corner(track, 3)
    driver = DriverProfile(consistency=1.0)
    baseline = generate_lap(track, outline, driver, 1, random.Random(0))
    effects = corner_effects(
        [_event(1, "early_turn_in", corner=3, meters=15.0)],
        [item.number for item in track.corners],
    )
    scenario = generate_lap(
        track, outline, driver, 1, random.Random(0), corner_effects=effects
    )

    previous_exit = _corner(track, 2).exit_m

    def turn_in(samples: list[Sample]) -> float:
        for sample in samples:
            if (
                previous_exit <= sample.dist_m <= corner.apex_m
                and abs(sample.steer) >= 0.15
            ):
                return sample.dist_m
        raise AssertionError("턴인 지점을 찾지 못했습니다")

    assert turn_in(scenario) < turn_in(baseline) - 5.0


def test_steer_corrections_add_wobble() -> None:
    track, outline = _track_and_outline()
    corner = _corner(track, 3)
    driver = DriverProfile(consistency=1.0)
    baseline = generate_lap(track, outline, driver, 1, random.Random(0))
    effects = corner_effects(
        [_event(1, "steer_corrections", corner=3, count=3, amplitude=0.18)],
        [item.number for item in track.corners],
    )
    scenario = generate_lap(
        track, outline, driver, 1, random.Random(0), corner_effects=effects
    )

    def steer_range(samples: list[Sample]) -> float:
        values = [
            sample.steer
            for sample in samples
            if corner.entry_m <= sample.dist_m <= corner.exit_m
        ]
        return max(values) - min(values)

    assert steer_range(scenario) > steer_range(baseline) + 0.1


def test_corner_effect_merges_and_validates_corners() -> None:
    merged = corner_effects(
        [
            _event(1, "late_brake", corner=3, meters=5.0),
            _event(1, "early_brake", corner=3, meters=2.0),
            _event(1, "off_track", corner=3, width_m=7.0),
        ],
        [3],
    )
    assert isinstance(merged[3], CornerEffect)
    assert merged[3].brake_shift_m == pytest.approx(3.0)
    assert merged[3].off_track is True

    with pytest.raises(ScenarioError):
        corner_effects([_event(1, "late_brake", corner=99)], [3])


def test_seed_reproducible_with_scenarios() -> None:
    """같은 시드·시나리오면 패킷 열이 바이트까지 같다."""
    scenarios = [
        _event(1, "late_brake", corner=3, meters=10.0),
        _event(1, "fuel_short", margin_laps=2.0),
        _event(1, "safety_car", laps=1),
        _event(1, "close_to_car_ahead", seconds=0.6),
    ]

    def capture(seed: int) -> list[bytes]:
        sender, collector = _sender(laps=2, scenarios=scenarios, seed=seed)
        sender.run(duration_s=3.0)
        return collector.packets

    first = capture(0)
    same = capture(0)
    other = capture(1)
    assert first == same
    assert first != other


# ---------------------------------------------------------------------------
# 그랑프리 패킷 값


def test_gp_packets_parse_and_have_20_cars() -> None:
    sender, collector = _sender(mode="gp", laps=1)
    sender.run(duration_s=1.0)
    packets = [parse_packet(payload) for payload in collector.packets]
    for payload, packet in zip(collector.packets, packets):
        assert len(payload) == PACKET_SIZES[packet.header.packet_id]
        assert packet.header.packet_format == 2025
    counts = collections.Counter(PacketId(packet.header.packet_id) for packet in packets)
    assert counts[PacketId.TIME_TRIAL] == 0
    assert counts[PacketId.SESSION] == 2
    assert counts[PacketId.PARTICIPANTS] == 1
    assert counts[PacketId.LAP_DATA] == 60

    session = _of(packets, PacketId.SESSION)[0].body
    assert session["session_type"] == 10
    assert session["game_mode"] == 1
    assert session["rule_set"] == 1
    assert session["total_laps"] == 1

    participants = _of(packets, PacketId.PARTICIPANTS)[0].body
    assert participants["num_active_cars"] == 20
    assert participants["participants"][0]["name"] == "YOU"
    assert participants["participants"][1]["ai_controlled"] == 1
    assert participants["participants"][19]["ai_controlled"] == 1

    lap_data = _of(packets, PacketId.LAP_DATA)[0].body["lap_data"]
    positions = sorted(entry["car_position"] for entry in lap_data[:20])
    assert positions == list(range(1, 21))
    assert sum(1 for entry in lap_data[:20] if entry["car_position"] == 20) == 1

    telemetry = _of(packets, PacketId.CAR_TELEMETRY)[0].body["car_telemetry_data"]
    assert len(telemetry) == 22
    assert telemetry[0]["speed"] > 0
    assert telemetry[19]["speed"] > 0


def test_close_to_car_ahead_gap_under_1s() -> None:
    packets = _run_tiny([_event(2, "close_to_car_ahead", seconds=0.6)], laps=2)
    found = None
    for packet in _of(packets, PacketId.LAP_DATA):
        entry = packet.body["lap_data"][0]
        if entry["current_lap_num"] != 2:
            continue
        delta_ms = (
            entry["delta_to_car_in_front_minutes_part"] * 60_000
            + entry["delta_to_car_in_front_ms_part"]
        )
        if delta_ms > 0:
            found = delta_ms
            break
    assert found is not None
    assert 0 < found < 1000, found


def test_safety_car_event_and_session_flag() -> None:
    packets = _run_tiny([_event(1, "safety_car", laps=1)], laps=2)
    event_packets = _of(packets, PacketId.EVENT)
    scar = [packet.body for packet in event_packets if packet.body["event_string_code"] == "SCAR"]
    assert scar
    assert scar[0]["event_details"]["event_type"] == 1
    assert scar[0]["event_details"]["safety_car_type"] == 0
    assert any(item["event_details"]["event_type"] == 3 for item in scar)

    statuses = [packet.body["safety_car_status"] for packet in _of(packets, PacketId.SESSION)]
    assert 1 in statuses
    assert statuses[-1] == 0


def test_vsc_sets_virtual_safety_car() -> None:
    packets = _run_tiny([_event(1, "vsc", laps=1)], laps=2)
    scar = [
        packet.body
        for packet in _of(packets, PacketId.EVENT)
        if packet.body["event_string_code"] == "SCAR"
    ]
    assert scar[0]["event_details"]["safety_car_type"] == 1
    statuses = [packet.body["safety_car_status"] for packet in _of(packets, PacketId.SESSION)]
    assert 2 in statuses


def test_yellow_flag_scenario_sets_marshal_zone() -> None:
    """SCRUM-80: yellow_flag가 지정 구간을 옐로 마셜 구역으로 보낸다."""
    packets = _run_tiny(
        [_event(1, "yellow_flag", start=0.25, end=0.5, laps=1)], laps=2
    )
    sessions = _of(packets, PacketId.SESSION)
    yellow = []
    for packet in sessions:
        body = packet.body
        count = body["num_marshal_zones"]
        entries = body["marshal_zones"][:count]
        if any(entry["zone_flag"] == 3 for entry in entries):
            yellow.append((body, entries))
    assert yellow, "옐로 마셜 구역이 있는 Session 패킷이 없습니다"

    body, entries = yellow[0]
    assert [(entry["zone_start"], entry["zone_flag"]) for entry in entries] == [
        (0.0, 0),
        (0.25, 3),
        (0.5, 0),
    ]
    assert body["safety_car_status"] == 0

    # 1랩만 유지하고 다음 랩에는 구역을 지운다.
    last = sessions[-1].body
    assert last["num_marshal_zones"] == 0


def test_blue_flag_scenario_sets_player_fia_flag() -> None:
    """SCRUM-80: blue_flag가 내 차 Car Status의 vehicle_fia_flags를 2로 만든다."""
    packets = _run_tiny([_event(1, "blue_flag", laps=1)], laps=2)
    statuses = _of(packets, PacketId.CAR_STATUS)
    flags = [
        packet.body["car_status_data"][0]["vehicle_fia_flags"]
        for packet in statuses
    ]
    assert 2 in flags
    assert flags[-1] == 0, "1랩이 끝나면 블루 깃발이 꺼져야 합니다"


def test_red_flag_scenario_emits_rdfl() -> None:
    """SCRUM-80: red_flag가 Event RDFL을 보내고, 랩 범위를 넘으면 더 보내지 않는다."""
    packets = _run_tiny([_event(1, "red_flag", laps=1)], laps=2)
    assert _event_codes(packets).count("RDFL") == 1


def test_flag_scenarios_are_reproducible() -> None:
    """깃발 시나리오도 같은 시드면 패킷 열이 바이트까지 같다."""
    scenarios = [
        _event(1, "yellow_flag", start=0.1, end=0.3, laps=1),
        _event(1, "blue_flag", laps=1),
        _event(1, "red_flag", laps=1),
    ]

    def capture(seed: int) -> list[bytes]:
        sender, collector = _sender(laps=2, scenarios=scenarios, seed=seed)
        sender.run(duration_s=5.0)
        return collector.packets

    first = capture(0)
    assert first == capture(0)
    assert first != capture(1)


def test_front_wing_damage_value_in_car_damage() -> None:
    packets = _run([_event(1, "front_wing_damage", percent=60)], laps=1, duration_s=1.0)
    damage = _of(packets, PacketId.CAR_DAMAGE)[0].body["car_damage_data"][0]
    assert damage["front_left_wing_damage"] == 60
    assert damage["front_right_wing_damage"] == 60


def test_tyre_and_brake_alert_values() -> None:
    packets = _run(
        [
            _event(1, "tyre_wear_critical", wear=88.0),
            _event(1, "tyre_temp_critical", surface_c=118, inner_c=128),
            _event(1, "brake_temp_critical", c=1050),
        ],
        laps=1,
        duration_s=1.0,
    )
    damage = _of(packets, PacketId.CAR_DAMAGE)[0].body["car_damage_data"][0]
    assert all(wear >= 88.0 for wear in damage["tyres_wear"])
    telemetry = _of(packets, PacketId.CAR_TELEMETRY)[0].body["car_telemetry_data"][0]
    assert all(value >= 118 for value in telemetry["tyres_surface_temperature"])
    assert all(value >= 128 for value in telemetry["tyres_inner_temperature"])
    assert all(value >= 1000 for value in telemetry["brakes_temperature"])


def test_fuel_short_makes_negative_margin() -> None:
    packets = _run(
        [_event(1, "fuel_short", margin_laps=2.0)], laps=5, duration_s=1.0
    )
    status = _of(packets, PacketId.CAR_STATUS)[0].body["car_status_data"][0]
    # 1랩 시작: 남은 레이스 5랩, 연료는 2랩 모자라 3랩 분량이어야 한다.
    assert status["fuel_remaining_laps"] < 5.0 - 1.0
    assert status["fuel_remaining_laps"] <= 3.01
    assert status["fuel_in_tank"] <= 3.0 * 1.8 + 0.1


def test_drs_and_tyre_age_scenario() -> None:
    packets = _run_tiny(
        [
            _event(1, "drs_available", distance_m=150, laps=1),
            _event(1, "tyre_age_gap", laps=8),
        ],
        laps=2,
    )
    statuses = _of(packets, PacketId.CAR_STATUS)
    lap1 = next(
        packet.body["car_status_data"][0]
        for packet in statuses
        if packet.body["car_status_data"][0]["tyres_age_laps"] == 8
    )
    assert lap1["drs_allowed"] == 1
    assert lap1["drs_activation_distance"] == 150
    assert lap1["tyres_age_laps"] == 8
    assert _of(packets, PacketId.CAR_TELEMETRY)[0].body["car_telemetry_data"][0]["drs"] == 1

    last = statuses[-1].body["car_status_data"][0]
    assert last["drs_allowed"] == 0


def test_penalty_event_and_lap_data() -> None:
    packets = _run_tiny(
        [_event(2, "penalty", seconds=5, type=1, infringement=1)], laps=2
    )
    codes = _event_codes(packets)
    assert "PENA" in codes
    penalty = next(
        packet.body
        for packet in _of(packets, PacketId.EVENT)
        if packet.body["event_string_code"] == "PENA"
    )
    assert penalty["event_details"]["vehicle_idx"] == 0
    assert penalty["event_details"]["time"] == 5

    lap2 = next(
        packet.body["lap_data"][0]
        for packet in _of(packets, PacketId.LAP_DATA)
        if packet.body["lap_data"][0]["current_lap_num"] == 2
    )
    assert lap2["penalties"] == 5
    assert lap2["total_warnings"] == 1


def test_main_loads_scenario_and_gp_with_stub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from f1re.demo_sender import sender as sender_module

    scenario = tmp_path / "custom.yaml"
    scenario.write_text("- lap: 1\n  kind: brake_temp_critical\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class _Stub:
        lap_times_ms = [90_000]
        lap_results: list[object] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def summary_line(self) -> str:
            return "stub"

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(sender_module, "DemoSender", _Stub)
    assert main(["--mode", "gp", "--scenario", str(scenario), "--laps", "4"]) == 0
    assert captured["mode"] == "gp"
    assert captured["laps"] == 4
    assert [event.kind for event in captured["scenarios"]] == ["brake_temp_critical"]
    assert captured["ran"] is True
    assert "시나리오" in capsys.readouterr().out


def test_sender_rejects_unknown_scenario_corner() -> None:
    track, outline = _track_and_outline(MONACO_ID)
    with pytest.raises(ScenarioError):
        DemoSender(
            track,
            outline,
            mode="gp",
            scenarios=[_event(1, "late_brake", corner=99)],
            sock=_CollectingSocket(),  # type: ignore[arg-type]
        )
