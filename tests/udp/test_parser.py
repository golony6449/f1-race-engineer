"""파서·패커 왕복과 거부 규칙.

대표값 body를 정의에서 만들어 16종 모두 왕복시키고, 길이·형식·패킷 ID가
틀린 입력이 `UnsupportedPacket`만 내는지 확인한다. 도우미는 이 파일 안에
둔다(공용 conftest를 만들지 않는다).
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from f1re.udp import spec
from f1re.udp.formats import f2025
from f1re.udp.packer import pack_packet
from f1re.udp.parser import UnsupportedPacket, parse_packet
from f1re.udp.schema import (
    MatrixField,
    PacketDef,
    RawField,
    ScalarField,
    StringField,
    StructDef,
    StructField,
)

SESSION_UID = 0x0123_4567_89AB_CDEF


def make_header(packet_id: int, *, packet_format: int = spec.PACKET_FORMAT, **overrides: Any) -> spec.Header:
    values: dict[str, Any] = {
        "packet_format": packet_format,
        "game_year": 25,
        "game_major": 1,
        "game_minor": 3,
        "packet_version": 1,
        "packet_id": int(packet_id),
        "session_uid": SESSION_UID,
        "session_time": 12.5,  # float32로 정확히 표현되는 값
        "frame_id": 1234,
        "overall_frame_id": 5678,
        "player_car_index": 0,
        "secondary_player_car_index": 255,
    }
    values.update(overrides)
    return spec.Header(**values)


def generated_struct(struct_def: StructDef) -> dict[str, Any]:
    """필드마다 다른 값으로 body를 채운다. 배열 길이·순서를 함께 검증한다."""
    counter = [0]

    def number(fmt: str) -> int | float:
        counter[0] += 1
        index = counter[0]
        if fmt == "f":
            return index + 0.5  # float32로 정확히 표현된다
        if fmt == "d":
            return index + 0.25
        if fmt == "b":
            return index % 100 + 1  # int8 범위
        return index % 200 + 1  # uint8 이상 범위

    def build(struct_def: StructDef) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for field_def in struct_def.fields:
            if isinstance(field_def, ScalarField):
                if field_def.count == 1:
                    body[field_def.key] = number(field_def.fmt)
                else:
                    body[field_def.key] = [
                        number(field_def.fmt) for _ in range(field_def.count)
                    ]
            elif isinstance(field_def, StringField):
                counter[0] += 1
                body[field_def.key] = f"S{counter[0]}"
            elif isinstance(field_def, RawField):
                counter[0] += 1
                body[field_def.key] = bytes(
                    (counter[0] + index) % 256 for index in range(field_def.length)
                )
            elif isinstance(field_def, MatrixField):
                body[field_def.key] = [
                    [number(field_def.fmt) for _ in range(field_def.cols)]
                    for _ in range(field_def.rows)
                ]
            elif field_def.count is None:
                body[field_def.key] = build(field_def.struct)
            else:
                body[field_def.key] = [
                    build(field_def.struct) for _ in range(field_def.count)
                ]
        return body

    return build(struct_def)


def generated_body(definition: PacketDef) -> dict[str, Any]:
    body = generated_struct(definition.body)
    if definition.event is not None:
        body["event_string_code"] = "FTLP"
        body["event_details"] = {"vehicle_idx": 3, "lap_time": 82.5}
    return body


def assert_same(actual: Any, expected: Any) -> None:
    if isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-6, abs=1e-6)
    elif isinstance(expected, dict):
        assert set(actual) == set(expected)
        for key in expected:
            assert_same(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for actual_item, expected_item in zip(actual, expected, strict=True):
            assert_same(actual_item, expected_item)
    else:
        assert actual == expected


def packed(definition: PacketDef, header: spec.Header, body: dict[str, Any]) -> bytes:
    data = pack_packet(header, body)
    assert len(data) == spec.PACKET_SIZES[spec.PacketId(header.packet_id)]
    return data


PACKET_IDS = list(spec.PacketId)
PACKET_ID_NAMES = [packet_id.name for packet_id in PACKET_IDS]


@pytest.mark.parametrize("packet_id", PACKET_IDS, ids=PACKET_ID_NAMES)
def test_roundtrip_all_packet_types(packet_id: spec.PacketId) -> None:
    header = make_header(packet_id)
    body = generated_body(f2025.PACKETS[packet_id])

    packet = parse_packet(packed(f2025.PACKETS[packet_id], header, body))

    assert packet.header == header
    assert_same(packet.body, body)


def test_roundtrip_car_telemetry_values() -> None:
    header = make_header(spec.PacketId.CAR_TELEMETRY)
    definition = f2025.PACKETS[spec.PacketId.CAR_TELEMETRY]
    body = generated_body(definition)
    body["car_telemetry_data"][0] = {
        "speed": 312,
        "throttle": 0.95,
        "steer": -0.25,
        "brake": 0.0,
        "clutch": 0,
        "gear": 7,
        "engine_rpm": 11500,
        "drs": 1,
        "rev_lights_percent": 80,
        "rev_lights_bit_value": 0x7FFF,
        "brakes_temperature": [600, 610, 580, 590],
        "tyres_surface_temperature": [95, 96, 94, 93],
        "tyres_inner_temperature": [100, 101, 99, 98],
        "engine_temperature": 110,
        "tyres_pressure": [22.5, 22.4, 21.9, 22.1],
        "surface_type": [0, 1, 0, 0],
    }
    body["mfd_panel_index"] = 255
    body["mfd_panel_index_secondary_player"] = 255
    body["suggested_gear"] = -1

    parsed = parse_packet(packed(definition, header, body))

    car = parsed.body["car_telemetry_data"][0]
    assert car["speed"] == 312
    assert car["throttle"] == pytest.approx(0.95, rel=1e-6)
    assert car["gear"] == 7
    assert car["engine_rpm"] == 11500
    assert car["brakes_temperature"] == [600, 610, 580, 590]
    assert car["tyres_pressure"] == pytest.approx([22.5, 22.4, 21.9, 22.1], rel=1e-6)
    assert parsed.body["mfd_panel_index"] == 255
    assert parsed.body["suggested_gear"] == -1


def test_roundtrip_utf8_participant_name() -> None:
    header = make_header(spec.PacketId.PARTICIPANTS)
    definition = f2025.PACKETS[spec.PacketId.PARTICIPANTS]
    body = generated_body(definition)
    body["num_active_cars"] = 2
    body["participants"][0]["name"] = "박성흠"
    body["participants"][1]["name"] = "A" * 32  # NUL 없이 꽉 찬 이름

    parsed = parse_packet(packed(definition, header, body))

    assert parsed.body["participants"][0]["name"] == "박성흠"
    assert parsed.body["participants"][1]["name"] == "A" * 32


def test_event_fastest_lap_values() -> None:
    header = make_header(spec.PacketId.EVENT)
    definition = f2025.PACKETS[spec.PacketId.EVENT]
    body = generated_body(definition)
    body["event_string_code"] = "FTLP"
    body["event_details"] = {"vehicle_idx": 4, "lap_time": 91.234}

    parsed = parse_packet(packed(definition, header, body))

    assert parsed.body["event_string_code"] == "FTLP"
    assert parsed.body["event_details"]["vehicle_idx"] == 4
    assert parsed.body["event_details"]["lap_time"] == pytest.approx(91.234, rel=1e-6)


@pytest.mark.parametrize("code", sorted(f2025.EVENT_DETAILS.structs))
def test_event_roundtrip_all_known_details(code: str) -> None:
    header = make_header(spec.PacketId.EVENT)
    definition = f2025.PACKETS[spec.PacketId.EVENT]
    body = generated_body(definition)
    body["event_string_code"] = code
    body["event_details"] = generated_struct(f2025.EVENT_DETAILS.structs[code])

    parsed = parse_packet(packed(definition, header, body))

    assert parsed.body["event_string_code"] == code
    assert_same(parsed.body["event_details"], body["event_details"])


@pytest.mark.parametrize("code", sorted(f2025.EVENT_DETAILS.empty_codes))
def test_event_known_code_without_details(code: str) -> None:
    header = make_header(spec.PacketId.EVENT)
    definition = f2025.PACKETS[spec.PacketId.EVENT]
    body = generated_body(definition)
    body["event_string_code"] = code
    body["event_details"] = {}

    parsed = parse_packet(packed(definition, header, body))

    assert parsed.body["event_details"] == {}


def test_event_unknown_code_keeps_raw_bytes() -> None:
    header = make_header(spec.PacketId.EVENT)
    definition = f2025.PACKETS[spec.PacketId.EVENT]
    raw = bytes(range(f2025.EVENT_DETAILS_SIZE))
    body = generated_body(definition)
    body["event_string_code"] = "ZZZZ"
    body["event_details"] = {"raw": raw}

    data = packed(definition, header, body)
    parsed = parse_packet(data)

    assert parsed.body["event_details"] == {"raw": raw}
    assert pack_packet(parsed.header, parsed.body) == data


@pytest.mark.parametrize("packet_id", PACKET_IDS, ids=PACKET_ID_NAMES)
def test_reject_wrong_length(packet_id: spec.PacketId) -> None:
    definition = f2025.PACKETS[packet_id]
    data = packed(definition, make_header(packet_id), generated_body(definition))

    for bad_data in (data[:-1], data + b"\x00"):
        with pytest.raises(UnsupportedPacket) as excinfo:
            parse_packet(bad_data)
        assert excinfo.value.reason == "length"


@pytest.mark.parametrize("length", list(range(spec.HEADER_SIZE)))
def test_reject_shorter_than_header(length: int) -> None:
    with pytest.raises(UnsupportedPacket) as excinfo:
        parse_packet(b"\x00" * length)
    assert excinfo.value.reason == "length"


def test_reject_unknown_format() -> None:
    definition = f2025.PACKETS[spec.PacketId.CAR_TELEMETRY]
    body = generated_body(definition)
    data = make_header(spec.PacketId.CAR_TELEMETRY, packet_format=2026).pack() + definition.pack_body(body)

    with pytest.raises(UnsupportedPacket) as excinfo:
        parse_packet(data)

    assert excinfo.value.reason == "format"
    assert excinfo.value.packet_format == 2026


def test_reject_2026_season_pack_sized_packet() -> None:
    # 2026 Season Pack의 Car Telemetry는 1448바이트다. 형식 검사가 먼저라
    # 길이가 달라도 reason은 "format"이어야 한다.
    data = make_header(spec.PacketId.CAR_TELEMETRY, packet_format=2026).pack() + bytes(
        1448 - spec.HEADER_SIZE
    )

    with pytest.raises(UnsupportedPacket) as excinfo:
        parse_packet(data)

    assert excinfo.value.reason == "format"


@pytest.mark.parametrize("packet_id", [16, 99, 255])
def test_reject_unknown_packet_id(packet_id: int) -> None:
    data = make_header(packet_id).pack() + bytes(
        spec.PACKET_SIZES[spec.PacketId.CAR_TELEMETRY] - spec.HEADER_SIZE
    )

    with pytest.raises(UnsupportedPacket) as excinfo:
        parse_packet(data)

    assert excinfo.value.reason == "packet_id"
    assert excinfo.value.packet_id == packet_id


def test_parse_only_raises_unsupported_packet_on_garbage() -> None:
    rng = random.Random(51)
    for _ in range(300):
        length = rng.randrange(0, 400)
        data = bytes(rng.randrange(256) for _ in range(length))
        try:
            parse_packet(data)
        except UnsupportedPacket as exc:
            assert exc.reason in {"length", "format", "packet_id"}


def test_packer_rejects_unsupported_format_and_packet_id() -> None:
    with pytest.raises(UnsupportedPacket) as format_error:
        pack_packet(make_header(spec.PacketId.CAR_TELEMETRY, packet_format=2026), {})
    assert format_error.value.reason == "format"

    with pytest.raises(UnsupportedPacket) as id_error:
        pack_packet(make_header(16), {})
    assert id_error.value.reason == "packet_id"
