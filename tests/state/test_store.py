"""상태 저장소: 최신 스냅샷, 진단, 세션 재시작·플래시백·일시정지.

공용 conftest를 만들지 않는 규칙에 따라 패킷 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import json
from typing import Any

from f1re.state.events import EventKind
from f1re.state.store import StateStore
from f1re.udp.formats import f2025
from f1re.udp.packer import pack_packet
from f1re.udp.parser import Packet, parse_packet
from f1re.udp.schema import (
    MatrixField,
    RawField,
    ScalarField,
    StringField,
    StructDef,
    StructField,
)
from f1re.udp.spec import PACKET_FORMAT, PACKET_SIZES, Header, PacketId

SESSION_UID = 0x0123_4567_89AB_CDEF


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
            return _default_struct_body(field_def.struct)
        return [_default_struct_body(field_def.struct) for _ in range(field_def.count)]
    raise TypeError(f"알 수 없는 필드 정의: {field_def!r}")


def _default_struct_body(struct_def: StructDef) -> dict[str, Any]:
    return {field.key: _default_value(field) for field in struct_def.fields}


def _default_body(packet_id: PacketId) -> dict[str, Any]:
    return _default_struct_body(f2025.PACKETS[packet_id].body)


class PacketBuilder:
    """테스트용 패킷 생성기. 프레임 ID를 순서대로 올리거나 지정한다."""

    def __init__(self, uid: int = SESSION_UID, player_car_index: int = 0) -> None:
        self.uid = uid
        self.player_car_index = player_car_index
        self.frame = 0

    def packet(
        self,
        packet_id: PacketId,
        body: dict[str, Any],
        *,
        frame: int | None = None,
        uid: int | None = None,
        player_car_index: int | None = None,
    ) -> Packet:
        self.frame = self.frame + 1 if frame is None else frame
        header = Header(
            packet_format=PACKET_FORMAT,
            game_year=25,
            game_major=1,
            game_minor=0,
            packet_version=1,
            packet_id=int(packet_id),
            session_uid=self.uid if uid is None else uid,
            session_time=self.frame / 60.0,
            frame_id=self.frame,
            overall_frame_id=self.frame,
            player_car_index=(
                self.player_car_index
                if player_car_index is None
                else player_car_index
            ),
            secondary_player_car_index=255,
        )
        return parse_packet(pack_packet(header, body))

    def session(
        self,
        *,
        frame: int | None = None,
        uid: int | None = None,
        **overrides: Any,
    ) -> Packet:
        body = _default_body(PacketId.SESSION)
        body.update(weather=0, track_id=13, session_type=13, game_paused=0)
        body.update(overrides)
        return self.packet(PacketId.SESSION, body, frame=frame, uid=uid)

    def lap(
        self,
        *,
        car: int = 0,
        lap: int = 1,
        distance: float = 0.0,
        last_lap_ms: int = 0,
        pit_status: int = 0,
        frame: int | None = None,
        uid: int | None = None,
        **overrides: Any,
    ) -> Packet:
        body = _default_body(PacketId.LAP_DATA)
        entry = body["lap_data"][car]
        entry.update(
            current_lap_num=lap,
            lap_distance=distance,
            last_lap_time_in_ms=last_lap_ms,
            pit_status=pit_status,
        )
        entry.update(overrides)
        return self.packet(PacketId.LAP_DATA, body, frame=frame, uid=uid)

    def event(
        self,
        code: str,
        details: dict[str, Any] | None = None,
        *,
        frame: int | None = None,
        uid: int | None = None,
    ) -> Packet:
        body = {
            "event_string_code": code,
            "event_details": details or {},
        }
        return self.packet(PacketId.EVENT, body, frame=frame, uid=uid)


def _kinds(events: list[Any]) -> list[EventKind]:
    return [event.kind for event in events]


def test_apply_keeps_latest_body_and_diagnostics() -> None:
    builder = PacketBuilder()
    store = StateStore()

    events = store.apply(builder.session(track_id=13), 1_000_000_000)
    assert _kinds(events) == [EventKind.SESSION_START]
    store.apply(builder.lap(lap=1, distance=100.0), 1_010_000_000)
    store.apply(builder.session(track_id=5), 1_020_000_000)

    snapshot = store.snapshot()
    assert snapshot["session_uid"] == SESSION_UID
    assert snapshot["player_car_index"] == 0
    assert snapshot["packets"]["session"]["track_id"] == 5
    assert len(snapshot["packets"]["lap_data"]["lap_data"]) == 22
    assert snapshot["packets"]["lap_data"]["lap_data"][0]["current_lap_num"] == 1

    diagnostics = snapshot["diagnostics"]
    assert diagnostics["last_packet_id"] == int(PacketId.SESSION)
    assert diagnostics["last_format"] == PACKET_FORMAT
    assert diagnostics["last_len"] == PACKET_SIZES[PacketId.SESSION]
    assert diagnostics["last_recv_ns"] == 1_020_000_000
    assert diagnostics["session_uid"] == SESSION_UID
    assert diagnostics["session_started_ns"] == 1_000_000_000
    assert diagnostics["unsupported_count"] == 0

    # 화면·API로 그대로 보낼 수 있어야 한다.
    json.dumps(snapshot, allow_nan=False)


def test_snapshot_turns_raw_event_bytes_into_hex() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.event("ZZZZ", {"raw": b"\x01\x02\x03"}), 1_000)

    snapshot = store.snapshot()
    assert snapshot["packets"]["event"]["event_details"]["raw"].startswith("010203")
    json.dumps(snapshot, allow_nan=False)


def test_snapshot_turns_non_finite_floats_into_none() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(
        builder.session(sector2_lap_distance_start=float("nan")), 1_000
    )

    snapshot = store.snapshot()
    assert snapshot["packets"]["session"]["sector2_lap_distance_start"] is None
    json.dumps(snapshot, allow_nan=False)


def test_diagnostics_packets_per_sec() -> None:
    builder = PacketBuilder()
    store = StateStore()
    base = 5_000_000_000
    for index in range(100):
        store.apply(builder.event("RDFL"), base + index * 5_000_000)

    assert store.diagnostics.total_packets == 100
    assert store.diagnostics.packets_per_sec == 100.0

    # 1초 창을 벗어나면 이전 패킷은 세지 않는다.
    store.apply(builder.event("RDFL"), base + 1_700_000_000)
    assert store.diagnostics.packets_per_sec == 1.0


def test_diagnostics_counts_unsupported_and_errors() -> None:
    from f1re.udp.parser import UnsupportedPacket

    store = StateStore()
    store.record_unsupported(
        UnsupportedPacket("format", length=45, packet_format=2026, packet_id=3),
        1_000,
    )
    store.record_unsupported(UnsupportedPacket("length", length=7), 2_000)
    store.record_error(RuntimeError("boom"), 3_000)

    diagnostics = store.diagnostics
    assert diagnostics.unsupported_count == 2
    assert diagnostics.error_count == 1
    assert diagnostics.last_unsupported_reason == "length"
    assert diagnostics.last_unsupported_format == 2026
    assert diagnostics.last_format == 2026
    assert diagnostics.last_error == "RuntimeError: boom"
    assert diagnostics.total_packets == 3


def test_session_restart_emits_event_and_resets_state() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.session(track_id=13), 1_000)
    store.apply(builder.lap(lap=5, distance=100.0), 2_000)
    store.events.drain()

    events = store.apply(
        builder.session(track_id=5, uid=SESSION_UID + 1), 3_000
    )

    assert EventKind.SESSION_RESTART in _kinds(events)
    assert EventKind.SESSION_START not in _kinds(events)
    snapshot = store.snapshot()
    assert snapshot["session_uid"] == SESSION_UID + 1
    assert snapshot["packets"]["session"]["track_id"] == 5
    assert "lap_data" not in snapshot["packets"]
    assert snapshot["lap_validity"] == []
    assert snapshot["diagnostics"]["session_started_ns"] == 3_000


def test_session_end_then_new_uid_is_session_start() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.session(), 1_000)

    events = store.apply(builder.event("SEND"), 2_000)
    assert _kinds(events) == [EventKind.SESSION_END]

    events = store.apply(builder.session(uid=SESSION_UID + 1), 3_000)
    assert _kinds(events) == [EventKind.SESSION_START]


def test_flashback_event_invalidates_current_lap() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.lap(lap=1, distance=1_000.0), 1_000)

    events = store.apply(
        builder.event(
            "FLBK",
            {"flashback_frame_identifier": 1, "flashback_session_time": 1.0},
        ),
        2_000,
    )
    assert EventKind.FLASHBACK in _kinds(events)
    status = store.snapshot()["lap_validity"][0]
    assert status["valid"] is False
    assert status["invalid_reason"] == "flashback_event"

    # 완료된 랩은 무효로 남고, 다음 랩은 새로 유효하게 시작한다.
    events = store.apply(
        builder.lap(lap=2, distance=0.0, last_lap_ms=90_000), 3_000
    )
    completed = [event for event in events if event.kind is EventKind.LAP_COMPLETE]
    assert len(completed) == 1
    assert completed[0].lap == 1
    assert completed[0].data["valid"] is False
    assert completed[0].data["invalid_reason"] == "flashback_event"
    assert store.snapshot()["lap_validity"][0]["valid"] is True


def test_frame_id_rewind_emits_flashback_once_per_episode() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.lap(lap=1, distance=100.0, frame=10), 1_000)

    events = store.apply(builder.lap(lap=1, distance=200.0, frame=5), 2_000)
    assert _kinds(events).count(EventKind.FLASHBACK) == 1

    events = store.apply(builder.lap(lap=1, distance=300.0, frame=6), 3_000)
    assert EventKind.FLASHBACK not in _kinds(events)

    # 프레임이 최댓값을 다시 넘으면 상황이 끝난 것으로 본다.
    store.apply(builder.lap(lap=1, distance=400.0, frame=11), 4_000)
    events = store.apply(builder.lap(lap=1, distance=500.0, frame=7), 5_000)
    assert _kinds(events).count(EventKind.FLASHBACK) == 1


def test_lap_distance_backwards_emits_flashback_and_invalidates_lap() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.lap(lap=1, distance=1_000.0, frame=1), 1_000)

    events = store.apply(builder.lap(lap=1, distance=500.0, frame=2), 2_000)

    assert EventKind.FLASHBACK in _kinds(events)
    status = store.snapshot()["lap_validity"][0]
    assert status["valid"] is False
    assert status["invalid_reason"] == "lap_distance_rewind"


def test_lap_complete_from_lap_number_and_fastest_lap_deduped() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.lap(lap=1, distance=1_000.0, frame=1), 1_000)

    events = store.apply(
        builder.lap(lap=2, distance=0.0, last_lap_ms=90_000, frame=2), 2_000
    )
    completed = [event for event in events if event.kind is EventKind.LAP_COMPLETE]
    assert len(completed) == 1
    assert completed[0].lap == 1
    assert completed[0].data["lap_time_ms"] == 90_000
    assert completed[0].data["valid"] is True
    assert completed[0].data["source"] == "lap_data"

    # 같은 랩의 FTLP가 뒤늦게 와도 중복으로 세지 않는다.
    events = store.apply(
        builder.event("FTLP", {"vehicle_idx": 0, "lap_time": 90.0}, frame=3),
        3_000,
    )
    assert [event for event in events if event.kind is EventKind.LAP_COMPLETE] == []

    events = store.apply(
        builder.lap(lap=3, distance=0.0, last_lap_ms=91_000, frame=4), 4_000
    )
    completed = [event for event in events if event.kind is EventKind.LAP_COMPLETE]
    assert [(event.lap, event.data["lap_time_ms"]) for event in completed] == [
        (2, 91_000)
    ]

    events = store.apply(
        builder.event("FTLP", {"vehicle_idx": 0, "lap_time": 91.0}, frame=5),
        5_000,
    )
    assert [event for event in events if event.kind is EventKind.LAP_COMPLETE] == []

    all_laps = [
        event
        for event in store.events.drain()
        if event.kind is EventKind.LAP_COMPLETE
    ]
    assert [event.lap for event in all_laps] == [1, 2]


def test_pit_overtake_and_penalty_events() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.lap(lap=1, distance=0.0, pit_status=0), 1_000)

    events = store.apply(builder.lap(lap=1, distance=10.0, pit_status=1), 2_000)
    assert _kinds(events) == [EventKind.PIT_ENTER]
    assert events[0].car_index == 0

    events = store.apply(builder.lap(lap=1, distance=20.0, pit_status=0), 3_000)
    assert _kinds(events) == [EventKind.PIT_EXIT]

    events = store.apply(
        builder.event(
            "OVTK",
            {
                "overtaking_vehicle_idx": 0,
                "being_overtaken_vehicle_idx": 3,
            },
        ),
        4_000,
    )
    assert _kinds(events) == [EventKind.OVERTAKE]
    assert events[0].car_index == 0
    assert events[0].data["being_overtaken_vehicle_idx"] == 3

    events = store.apply(
        builder.event(
            "PENA",
            {
                "penalty_type": 1,
                "infringement_type": 2,
                "vehicle_idx": 0,
                "other_vehicle_idx": 3,
                "time": 5,
                "lap_num": 1,
                "places_gained": 0,
            },
        ),
        5_000,
    )
    assert _kinds(events) == [EventKind.PENALTY]
    assert events[0].data["penalty_type"] == 1


def test_safety_car_vsc_red_flag_and_weather_change() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.session(weather=0), 1_000)

    events = store.apply(builder.session(weather=1), 2_000)
    assert _kinds(events) == [EventKind.WEATHER_CHANGE]
    assert events[0].data == {
        "session_uid": SESSION_UID,
        "recv_ns": 2_000,
        "previous": 0,
        "current": 1,
    }

    events = store.apply(
        builder.event("SCAR", {"safety_car_type": 2, "event_type": 0}), 3_000
    )
    assert _kinds(events) == [EventKind.VSC]

    events = store.apply(
        builder.event("SCAR", {"safety_car_type": 1, "event_type": 0}), 4_000
    )
    assert _kinds(events) == [EventKind.SAFETY_CAR]

    events = store.apply(builder.event("RDFL"), 5_000)
    assert _kinds(events) == [EventKind.RED_FLAG]


def test_pause_invalidates_current_lap() -> None:
    builder = PacketBuilder()
    store = StateStore()
    store.apply(builder.lap(lap=1, distance=100.0), 1_000)

    events = store.apply(builder.session(game_paused=1), 2_000)
    assert events == []
    status = store.snapshot()["lap_validity"][0]
    assert status["valid"] is False
    assert status["invalid_reason"] == "paused"

    store.apply(builder.session(game_paused=0), 3_000)
    assert store.snapshot()["lap_validity"][0]["valid"] is False

    events = store.apply(
        builder.lap(lap=2, distance=0.0, last_lap_ms=90_000), 4_000
    )
    completed = [event for event in events if event.kind is EventKind.LAP_COMPLETE]
    assert completed[0].data["valid"] is False
    assert completed[0].data["invalid_reason"] == "paused"
    assert store.snapshot()["lap_validity"][0]["valid"] is True
