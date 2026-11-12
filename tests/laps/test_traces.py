"""랩 샘플 기록과 무효 랩 판정(작업 14, SCRUM-64).

패킷은 실제 F1 25 규격대로 `pack_packet` → `parse_packet`을 거쳐 만든다. 필드
이름이 파서와 어긋나면 여기서 바로 드러난다.
"""

from __future__ import annotations

from typing import Any

import pytest

from f1re.laps.traces import LapTraceRecorder
from f1re.tracks.model import load_outline
from f1re.udp.formats import f2025
from f1re.udp.packer import pack_packet
from f1re.udp.parser import Packet, parse_packet
from f1re.udp.schema import (
    MatrixField,
    RawField,
    ScalarField,
    StringField,
    StructField,
    StructDef,
)
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId

SESSION_UID = 0x5EED_0000_0000_0001
OTHER_SESSION_UID = 0x5EED_0000_0000_0002
LAP_LENGTH_M = 1000.0
SESSION_TIME_TRIAL = 13
SESSION_RACE = 10


# ---------------------------------------------------------------------------
# 패킷 만들기 도우미


def _default_value(field_def: Any) -> Any:
    if isinstance(field_def, ScalarField):
        zero: int | float = 0.0 if field_def.fmt in "fd" else 0
        return zero if field_def.count == 1 else [zero] * field_def.count
    if isinstance(field_def, StringField):
        return ""
    if isinstance(field_def, RawField):
        return bytes(field_def.length)
    if isinstance(field_def, MatrixField):
        return [[0] * field_def.cols for _ in range(field_def.rows)]
    if isinstance(field_def, StructField):
        if field_def.count is None:
            return _new_struct(field_def.struct)
        return [_new_struct(field_def.struct) for _ in range(field_def.count)]
    raise TypeError(f"알 수 없는 필드 정의: {field_def!r}")


def _new_struct(struct_def: StructDef) -> dict[str, Any]:
    return {field_def.key: _default_value(field_def) for field_def in struct_def.fields}


def _new_body(packet_id: PacketId) -> dict[str, Any]:
    return _new_struct(f2025.PACKETS[packet_id].body)


def _make(
    packet_id: PacketId,
    body: dict[str, Any],
    frame: int,
    *,
    uid: int = SESSION_UID,
    player_car_index: int = 0,
) -> Packet:
    header = Header(
        packet_format=PACKET_FORMAT,
        game_year=25,
        game_major=1,
        game_minor=0,
        packet_version=1,
        packet_id=int(packet_id),
        session_uid=uid,
        session_time=frame / 60.0,
        frame_id=frame,
        overall_frame_id=frame,
        player_car_index=player_car_index,
        secondary_player_car_index=255,
    )
    return parse_packet(pack_packet(header, body))


def _session_packet(
    frame: int,
    *,
    session_type: int = SESSION_TIME_TRIAL,
    safety_car_status: int = 0,
    game_paused: int = 0,
    uid: int = SESSION_UID,
) -> Packet:
    body = _new_body(PacketId.SESSION)
    body.update(
        session_type=session_type,
        safety_car_status=safety_car_status,
        game_paused=game_paused,
        track_id=13,
        track_length=int(LAP_LENGTH_M),
    )
    return _make(PacketId.SESSION, body, frame, uid=uid)


def _lap_data_packet(
    frame: int,
    cars: dict[int, dict[str, Any]],
    *,
    uid: int = SESSION_UID,
) -> Packet:
    body = _new_body(PacketId.LAP_DATA)
    entries = list(body["lap_data"])
    for car_index, spec in cars.items():
        entry = _new_struct(f2025.LAP_DATA)
        entry.update(
            current_lap_num=spec["lap"],
            lap_distance=spec["dist"],
            current_lap_time_in_ms=spec["t_ms"],
            driver_status=spec.get("driver_status", 4),
            pit_status=spec.get("pit_status", 0),
            current_lap_invalid=spec.get("current_lap_invalid", 0),
        )
        entries[car_index] = entry
    body["lap_data"] = entries
    return _make(PacketId.LAP_DATA, body, frame, uid=uid)


def _telemetry_packet(
    frame: int,
    cars: dict[int, dict[str, Any]],
    *,
    uid: int = SESSION_UID,
) -> Packet:
    body = _new_body(PacketId.CAR_TELEMETRY)
    entries = list(body["car_telemetry_data"])
    for car_index, spec in cars.items():
        entry = _new_struct(f2025.CAR_TELEMETRY_DATA)
        entry.update(
            speed=spec.get("speed", 200),
            throttle=spec.get("throttle", 0.6),
            brake=spec.get("brake", 0.1),
            steer=spec.get("steer", 0.2),
            gear=spec.get("gear", 5),
        )
        entries[car_index] = entry
    body["car_telemetry_data"] = entries
    return _make(PacketId.CAR_TELEMETRY, body, frame, uid=uid)


def _motion_packet(
    frame: int,
    positions: dict[int, tuple[float, float]],
    *,
    uid: int = SESSION_UID,
) -> Packet:
    body = _new_body(PacketId.MOTION)
    entries = list(body["car_motion_data"])
    for car_index, (x, z) in positions.items():
        entry = _new_struct(f2025.CAR_MOTION_DATA)
        entry.update(world_position_x=x, world_position_z=z)
        entries[car_index] = entry
    body["car_motion_data"] = entries
    return _make(PacketId.MOTION, body, frame, uid=uid)


def _car_status_packet(
    frame: int,
    fuel_kg: float,
    *,
    player_car_index: int = 0,
    uid: int = SESSION_UID,
) -> Packet:
    body = _new_body(PacketId.CAR_STATUS)
    entries = list(body["car_status_data"])
    entry = _new_struct(f2025.CAR_STATUS_DATA)
    entry.update(fuel_in_tank=fuel_kg, fuel_capacity=110.0)
    entries[player_car_index] = entry
    body["car_status_data"] = entries
    return _make(PacketId.CAR_STATUS, body, frame, uid=uid)


def _motion_ex_packet(
    frame: int,
    slip: tuple[float, float, float, float],
    *,
    uid: int = SESSION_UID,
) -> Packet:
    body = _new_body(PacketId.MOTION_EX)
    body["wheel_slip_ratio"] = list(slip)
    return _make(PacketId.MOTION_EX, body, frame, uid=uid)


class _Harness:
    """한 틱씩 패킷을 만들어 기록기에 넣는다."""

    def __init__(
        self, recorder: LapTraceRecorder | None = None, *, outline=None
    ) -> None:
        self.recorder = recorder or LapTraceRecorder(outline=outline)
        self.frame = 0
        self.sent: list[Packet] = []
        self.completed = []

    def feed(
        self,
        cars: dict[int, dict[str, Any]],
        *,
        session: dict[str, Any] | None = None,
        fuel_kg: float | None = None,
        slip: tuple[float, float, float, float] | None = None,
        frame: int | None = None,
        uid: int = SESSION_UID,
    ) -> None:
        tick = self.frame if frame is None else frame
        packets = [
            _session_packet(tick, uid=uid, **(session or {})),
            _lap_data_packet(tick, cars, uid=uid),
            _motion_packet(
                tick,
                {
                    index: (index * 1000.0 + spec["dist"], spec["dist"] * 2.0)
                    for index, spec in cars.items()
                },
                uid=uid,
            ),
            _telemetry_packet(tick, cars, uid=uid),
        ]
        if fuel_kg is not None:
            packets.append(_car_status_packet(tick, fuel_kg, uid=uid))
        if slip is not None:
            packets.append(_motion_ex_packet(tick, slip, uid=uid))
        for packet in packets:
            self.sent.append(packet)
            self.completed.extend(self.recorder.on_packet(packet))
        self.frame = tick + 1

    def run_lap(
        self,
        lap: int,
        *,
        ticks: int = 20,
        step_m: float = 50.0,
        step_ms: int = 1000,
        car_indices: tuple[int, ...] = (0,),
        **kwargs: Any,
    ) -> None:
        for tick in range(ticks):
            cars = {
                index: {"lap": lap, "dist": tick * step_m, "t_ms": tick * step_ms}
                for index in car_indices
            }
            self.feed(cars, **kwargs)


def _completed(traces, lap: int, car_index: int = 0) -> Any:
    matched = [
        trace
        for trace in traces
        if trace.lap == lap and trace.car_index == car_index
    ]
    assert matched, f"{lap}랩 기록이 없습니다: {[trace.lap for trace in traces]}"
    return matched[-1]


# ---------------------------------------------------------------------------
# 샘플 기록


def test_records_all_cars_by_lap_distance() -> None:
    """차마다 랩 거리 순서로 샘플을 남기고, 내 차만 연료·휠 슬립을 기록한다."""
    harness = _Harness()
    cars = {0: {}, 1: {}}
    for tick in range(30):
        harness.feed(
            {
                0: {"lap": 1, "dist": tick * 20.0, "t_ms": tick * 200},
                1: {"lap": 1, "dist": tick * 20.0, "t_ms": tick * 200},
            },
            fuel_kg=42.5,
            slip=(0.1, 0.2, 0.3, 0.4),
        )
    harness.feed({0: {"lap": 2, "dist": 0.0, "t_ms": 0}, 1: {"lap": 2, "dist": 0.0, "t_ms": 0}})

    trace0 = _completed(harness.completed, 1, car_index=0)
    trace1 = _completed(harness.completed, 1, car_index=1)
    assert trace0.car_index == 0 and trace1.car_index == 1
    assert trace0.valid and trace1.valid
    assert trace0.invalid_reason is None
    assert len(trace0.samples) == 30 and len(trace1.samples) == 30

    distances = [sample.dist_m for sample in trace0.samples]
    assert distances == sorted(distances)
    assert distances == [tick * 20.0 for tick in range(30)]
    assert [sample.t_ms for sample in trace0.samples] == [tick * 200 for tick in range(30)]
    assert trace0.samples[3].speed == 200.0
    assert trace0.samples[3].steer == pytest.approx(0.2)
    assert trace0.samples[3].gear == 5

    # 위치는 Motion의 세계 좌표를 그대로 쓴다.
    assert (trace0.samples[3].x, trace0.samples[3].z) == (60.0, 120.0)
    assert (trace1.samples[3].x, trace1.samples[3].z) == (1060.0, 120.0)

    # 휠 슬립·연료는 내 차(0번)만 있다. 첫 샘플은 Car Status·Motion Ex가 오기 전이라 없다.
    assert trace0.samples[0].wheel_slip is None
    assert trace0.samples[0].fuel_kg is None
    assert trace0.samples[3].wheel_slip == pytest.approx((0.1, 0.2, 0.3, 0.4))
    assert trace0.samples[5].fuel_kg == 42.5
    assert all(sample.wheel_slip is None for sample in trace1.samples)
    assert all(sample.fuel_kg is None for sample in trace1.samples)


def test_position_falls_back_to_outline_without_motion() -> None:
    """Motion이 없으면 중심선 보간 위치를 쓴다."""
    outline = load_outline(13)
    assert outline is not None
    recorder = LapTraceRecorder(outline=outline)
    for tick in range(10):
        distance = 100.0 + tick * 10.0
        recorder.on_packet(
            _lap_data_packet(
                tick, {0: {"lap": 1, "dist": distance, "t_ms": tick * 500}}
            )
        )
        recorder.on_packet(_telemetry_packet(tick, {0: {}}))
    traces = recorder.on_packet(
        _lap_data_packet(10, {0: {"lap": 2, "dist": 0.0, "t_ms": 0}})
    )

    lap1 = _completed(traces, 1)
    assert len(lap1.samples) == 10
    for sample in lap1.samples:
        assert (sample.x, sample.z) == pytest.approx(
            outline.position_at(sample.dist_m)
        )


# ---------------------------------------------------------------------------
# 무효 랩


INVALID_CASES = [
    ("outlap", {"driver_status": 3}, {}, "outlap"),
    ("pit", {"pit_status": 1}, {}, "pit"),
    ("off_track", {"driver_status": 5}, {}, "off_track"),
    ("game_invalid", {"current_lap_invalid": 1}, {}, "off_track"),
    ("safety_car", {}, {"safety_car_status": 1}, "safety_car"),
    ("vsc", {}, {"safety_car_status": 2}, "vsc"),
    ("paused", {}, {"game_paused": 1}, "paused"),
    ("race_lap_1", {}, {"session_type": SESSION_RACE}, "race_lap_1"),
]


@pytest.mark.parametrize(
    ("lap_flag", "session_flag", "expected"),
    [case[1:] for case in INVALID_CASES],
    ids=[case[0] for case in INVALID_CASES],
)
def test_invalid_lap_reasons(
    lap_flag: dict[str, Any], session_flag: dict[str, Any], expected: str
) -> None:
    """아웃랩·피트·트랙 이탈·SC/VSC·일시정지·레이스 1랩은 무효로 표시한다."""
    harness = _Harness()
    base_session = {
        "session_type": SESSION_RACE
        if expected == "race_lap_1"
        else SESSION_TIME_TRIAL
    }
    for tick in range(20):
        spec = {"lap": 1, "dist": tick * 50.0, "t_ms": tick * 1000}
        session = dict(base_session)
        if tick == 10:
            spec.update(lap_flag)
            session.update(session_flag)
        harness.feed({0: spec}, session=session)
    # 랩 번호가 오르면 1랩이 마감된다.
    harness.feed({0: {"lap": 2, "dist": 0.0, "t_ms": 0}}, session=base_session)

    trace = _completed(harness.completed, 1)
    assert trace.valid is False
    assert trace.invalid_reason == expected
    assert trace.samples, "무효 랩도 샘플은 남는다"

    # 무효 랩을 내도 기록은 계속되고 다음 랩은 정상으로 나온다.
    harness.run_lap(2, ticks=5, session=base_session)
    harness.feed({0: {"lap": 3, "dist": 0.0, "t_ms": 0}}, session=base_session)
    lap2 = _completed(harness.completed, 2)
    assert lap2.valid is True
    assert lap2.invalid_reason is None


def test_valid_lap_has_no_reason() -> None:
    harness = _Harness()
    harness.run_lap(1)
    harness.feed({0: {"lap": 2, "dist": 0.0, "t_ms": 0}})
    trace = _completed(harness.completed, 1)
    assert trace.valid is True
    assert trace.invalid_reason is None
    assert len(trace.samples) == 20


def test_lap_distance_rewind_marks_flashback() -> None:
    harness = _Harness()
    for tick in range(10):
        harness.feed({0: {"lap": 1, "dist": tick * 100.0, "t_ms": tick * 2000}})
    # 같은 랩에서 랩 거리가 뒤로 갔다(플래시백).
    harness.feed({0: {"lap": 1, "dist": 400.0, "t_ms": 9000}})
    for tick in range(10, 20):
        harness.feed({0: {"lap": 1, "dist": tick * 100.0, "t_ms": tick * 2000}})
    harness.feed({0: {"lap": 2, "dist": 0.0, "t_ms": 0}})

    trace = _completed(harness.completed, 1)
    assert trace.valid is False
    assert trace.invalid_reason == "flashback"


def test_frame_rewind_marks_flashback() -> None:
    harness = _Harness()
    for tick in range(10):
        harness.feed({0: {"lap": 1, "dist": tick * 100.0, "t_ms": tick * 2000}})
    # frameIdentifier가 되감겼다.
    harness.feed({0: {"lap": 1, "dist": 900.0, "t_ms": 18000}}, frame=0)
    for tick in range(10, 20):
        harness.feed(
            {0: {"lap": 1, "dist": tick * 100.0, "t_ms": tick * 2000}},
            frame=tick,
        )
    harness.feed({0: {"lap": 2, "dist": 0.0, "t_ms": 0}}, frame=20)

    trace = _completed(harness.completed, 1)
    assert trace.valid is False
    assert trace.invalid_reason == "flashback"


def test_flashback_event_marks_flashback() -> None:
    harness = _Harness()
    harness.run_lap(1, ticks=5)
    body = _new_body(PacketId.EVENT)
    body["event_string_code"] = "FLBK"
    body["event_details"] = {
        "flashback_frame_identifier": 3,
        "flashback_session_time": 0.05,
    }
    harness.completed.extend(
        harness.recorder.on_packet(_make(PacketId.EVENT, body, harness.frame))
    )
    harness.run_lap(1, ticks=5)
    harness.feed({0: {"lap": 2, "dist": 0.0, "t_ms": 0}})

    trace = _completed(harness.completed, 1)
    assert trace.valid is False
    assert trace.invalid_reason == "flashback"


def test_session_restart_finishes_lap_invalid_and_continues() -> None:
    harness = _Harness()
    harness.run_lap(1, ticks=5)
    # sessionUID가 바뀌면 진행 중인 랩을 무효로 마감하고 상태를 비운다.
    harness.feed(
        {0: {"lap": 1, "dist": 0.0, "t_ms": 0}},
        session={"session_type": SESSION_TIME_TRIAL},
        uid=OTHER_SESSION_UID,
    )
    trace = _completed(harness.completed, 1)
    assert trace.valid is False
    assert trace.invalid_reason == "session_restart"
    assert len(trace.samples) == 5

    # 새 세션에서도 기록은 계속된다.
    for tick in range(1, 5):
        harness.feed(
            {0: {"lap": 1, "dist": tick * 50.0, "t_ms": tick * 1000}},
            uid=OTHER_SESSION_UID,
        )
    harness.feed(
        {0: {"lap": 2, "dist": 0.0, "t_ms": 0}}, uid=OTHER_SESSION_UID
    )
    new_lap = [
        trace
        for trace in harness.completed
        if trace.lap == 1 and trace.valid
    ]
    assert new_lap, "새 세션의 랩이 정상 기록되어야 한다"
