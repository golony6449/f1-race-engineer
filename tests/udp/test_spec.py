"""규격 정의가 EA 공식 문서의 바이트 수·이름 규칙과 맞는지 확인한다.

파서와 패커가 같은 정의를 쓰므로, 정의 자체가 공식 문서와 어긋나면 양쪽이
같은 실수를 공유한다. 그래서 여기서 공식 문서의 패킷별 크기 주석 값과
정의 크기 합을 직접 대조한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from f1re.udp import spec
from f1re.udp.formats import f2025
from f1re.udp.schema import StructField, StructDef

# EA 공식 "F1 25 Telemetry Output Structures.txt"의 패킷별 전체 바이트 수 주석.
OFFICIAL_PACKET_SIZES = {
    spec.PacketId.MOTION: 1349,
    spec.PacketId.SESSION: 753,
    spec.PacketId.LAP_DATA: 1285,
    spec.PacketId.EVENT: 45,
    spec.PacketId.PARTICIPANTS: 1284,
    spec.PacketId.CAR_SETUPS: 1133,
    spec.PacketId.CAR_TELEMETRY: 1352,
    spec.PacketId.CAR_STATUS: 1239,
    spec.PacketId.FINAL_CLASSIFICATION: 1042,
    spec.PacketId.LOBBY_INFO: 954,
    spec.PacketId.CAR_DAMAGE: 1041,
    spec.PacketId.SESSION_HISTORY: 1460,
    spec.PacketId.TYRE_SETS: 231,
    spec.PacketId.MOTION_EX: 273,
    spec.PacketId.TIME_TRIAL: 101,
    spec.PacketId.LAP_POSITIONS: 1131,
}

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


def _walk_fields(struct_def: StructDef) -> Iterator[object]:
    for field_def in struct_def.fields:
        yield field_def
        if isinstance(field_def, StructField):
            yield from _walk_fields(field_def.struct)


def test_header_size_is_29() -> None:
    header = spec.Header(
        packet_format=spec.PACKET_FORMAT,
        game_year=25,
        game_major=1,
        game_minor=0,
        packet_version=1,
        packet_id=spec.PacketId.CAR_TELEMETRY,
        session_uid=1,
        session_time=1.5,
        frame_id=2,
        overall_frame_id=3,
        player_car_index=0,
        secondary_player_car_index=255,
    )
    assert spec.HEADER_SIZE == 29
    assert len(header.pack()) == spec.HEADER_SIZE


def test_packet_sizes_match_official_bytes() -> None:
    assert spec.PACKET_SIZES == OFFICIAL_PACKET_SIZES
    for packet_id, official_size in OFFICIAL_PACKET_SIZES.items():
        definition = spec.FORMATS[spec.PACKET_FORMAT].PACKETS[packet_id]
        body_size = spec.HEADER_SIZE + definition.body.size
        assert body_size == official_size, f"{packet_id.name}: {body_size} != {official_size}"


def test_all_packet_ids_are_defined() -> None:
    assert len(spec.PacketId) == 16
    assert set(spec.PACKET_SIZES) == set(spec.PacketId)
    assert set(f2025.PACKETS) == set(spec.PacketId)


def test_formats_table_has_only_2025() -> None:
    assert spec.PACKET_FORMAT == 2025
    assert f2025.FORMAT == 2025
    assert spec.FORMATS == {2025: f2025}


def test_packet_rates_follow_official_periods() -> None:
    assert set(spec.PACKET_RATES_HZ) == set(spec.PacketId)

    menu_packets = [
        spec.PacketId.MOTION,
        spec.PacketId.LAP_DATA,
        spec.PacketId.CAR_TELEMETRY,
        spec.PacketId.CAR_STATUS,
        spec.PacketId.MOTION_EX,
    ]
    for packet_id in menu_packets:
        assert spec.PACKET_RATES_HZ[packet_id] == spec.MENU_RATE_HZ

    assert spec.PACKET_RATES_HZ[spec.PacketId.SESSION] == 2.0
    assert spec.PACKET_RATES_HZ[spec.PacketId.PARTICIPANTS] == 0.2
    assert spec.PACKET_RATES_HZ[spec.PacketId.CAR_SETUPS] == 2.0
    assert spec.PACKET_RATES_HZ[spec.PacketId.CAR_DAMAGE] == 10.0
    assert spec.PACKET_RATES_HZ[spec.PacketId.SESSION_HISTORY] == 20.0
    assert spec.PACKET_RATES_HZ[spec.PacketId.TYRE_SETS] == 20.0
    assert spec.PACKET_RATES_HZ[spec.PacketId.TIME_TRIAL] == 1.0
    assert spec.PACKET_RATES_HZ[spec.PacketId.LAP_POSITIONS] == 1.0
    assert spec.PACKET_RATES_HZ[spec.PacketId.LOBBY_INFO] == 2.0

    # 주기 송신이 아니라 사건이 생길 때만 보내는 패킷.
    assert spec.PACKET_RATES_HZ[spec.PacketId.EVENT] is None
    assert spec.PACKET_RATES_HZ[spec.PacketId.FINAL_CLASSIFICATION] is None


def test_event_union_size_is_largest_member() -> None:
    largest = max(struct_def.size for struct_def in f2025.EVENT_DETAILS.structs.values())
    assert f2025.EVENT_DETAILS_SIZE == 12
    assert largest == f2025.EVENT_DETAILS_SIZE


def test_body_field_names_are_snake_case() -> None:
    for definition in f2025.PACKETS.values():
        for field_def in _walk_fields(definition.body):
            assert SNAKE_CASE.fullmatch(field_def.key), (
                definition.name,
                field_def.name,
                field_def.key,
            )


def test_known_field_name_conversion() -> None:
    motion_keys = [field_def.key for field_def in f2025.CAR_MOTION_DATA.fields]
    assert "world_position_x" in motion_keys
    assert "g_force_lateral" in motion_keys

    lap_keys = [field_def.key for field_def in f2025.LAP_DATA.fields]
    assert "last_lap_time_in_ms" in lap_keys
    assert "sector1_time_ms_part" in lap_keys
    assert "num_unserved_drive_through_pens" in lap_keys

    session_keys = [
        field_def.key for field_def in spec.FORMATS[2025].PACKETS[spec.PacketId.SESSION].body.fields
    ]
    assert "sector2_lap_distance_start" in session_keys
    assert "ers_assist" in session_keys
    assert "drs_assist" in session_keys

    status_keys = [field_def.key for field_def in f2025.CAR_STATUS_DATA.fields]
    assert "ers_harvested_this_lap_mguk" in status_keys
    assert "vehicle_fia_flags" in status_keys
