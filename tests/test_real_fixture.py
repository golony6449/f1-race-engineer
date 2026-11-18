"""실제 F1 25 게임 녹화 조각 회귀 테스트.

`tests/fixtures/real/recording-6-suzuka-race-snippet.f1rec`는 2026-11-15
스즈카 실전 레이스 녹화(recording id 6)에서 잘라 낸 아주 짧은 연속 구간이다.
원본 163MB는 저장소에 넣지 않는다. 파서가 실제 게임 패킷을 모두 받아들이고
F1 25 레이스 세션(15)·스즈카(13)로 인식하는지 확인한다.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from f1re.tracks.model import load_outline, load_track
from f1re.tracks.outline import TrackOutline
from f1re.udp.parser import parse_packet
from f1re.udp.recording import iter_recording, scan_recording
from f1re.udp.session_category import session_category
from f1re.udp.spec import PACKET_SIZES, PacketId

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "real"
    / "recording-6-suzuka-race-snippet.f1rec"
)

REQUIRED_PACKETS = frozenset(
    {
        PacketId.SESSION,
        PacketId.LAP_DATA,
        PacketId.MOTION,
        PacketId.CAR_TELEMETRY,
        PacketId.CAR_STATUS,
        PacketId.CAR_DAMAGE,
    }
)


def test_real_fixture_parses() -> None:
    """픽스처의 모든 패킷이 오류 없이 파싱되고 F1 25 레이스·스즈카로 인식된다."""
    records = list(iter_recording(FIXTURE))

    assert records
    assert scan_recording(FIXTURE)[0] == len(records)

    seen: set[PacketId] = set()
    session_uids: set[int] = set()
    sessions: list[dict[str, object]] = []
    previous_ns = records[0][0]
    for recv_ns, data in records:
        assert recv_ns >= previous_ns  # 연속 구간이므로 수신 시각이 뒤로 가지 않는다
        previous_ns = recv_ns

        packet = parse_packet(data)
        assert packet.header.packet_format == 2025
        packet_id = PacketId(packet.header.packet_id)
        assert len(data) == PACKET_SIZES[packet_id]
        assert 0 <= packet.header.player_car_index < 22

        seen.add(packet_id)
        session_uids.add(packet.header.session_uid)
        if packet_id is PacketId.SESSION:
            sessions.append(packet.body)

    assert REQUIRED_PACKETS <= seen
    assert len(session_uids) == 1
    assert sessions

    for body in sessions:
        assert body["session_type"] == 15
        assert body["track_id"] == 13
        assert body["track_length"] == 5809
        assert session_category(body["session_type"]) == "race"


def test_real_fixture_contains_short_race_window() -> None:
    """픽스처는 원본의 짧은 한 구간이며, 녹화 메타데이터가 실제 레이스와 맞는다."""
    records = list(iter_recording(FIXTURE))
    duration_s = (records[-1][0] - records[0][0]) / 1e9

    assert len(records) <= 100
    assert 0.05 <= duration_s <= 3.0

    headers = [parse_packet(data).header for _recv_ns, data in records]
    session_headers = [
        header for header in headers if header.packet_id == PacketId.SESSION
    ]
    assert session_headers
    assert len({header.session_uid for header in headers}) == 1
    assert session_headers[-1].session_time >= headers[0].session_time


# `docs/spec/real-game-check.md`의 점군 정합 변환: 중심선 = 1.0024·R·게임 + t.
# z축 반전 + x축 기준 -1.23° 회전 + 1.0024배 + 평행이동이다.
_ICP_SCALE = 1.0024
_ICP_COS = 0.99977
_ICP_SIN = -0.02139
_ICP_TX = -131.08
_ICP_TZ = 28.26


def _game_to_centerline(x: float, z: float) -> tuple[float, float]:
    return (
        _ICP_SCALE * (_ICP_COS * x + _ICP_SIN * z) + _ICP_TX,
        _ICP_SCALE * (_ICP_SIN * x - _ICP_COS * z) + _ICP_TZ,
    )


def _nearest_centerline(
    outline: TrackOutline, x: float, z: float
) -> tuple[float, float]:
    """(중심선까지 횡방향 거리, 중심선 랩 거리)를 돌려준다."""
    best_lateral = math.inf
    best_distance = 0.0
    points = outline.points
    for index in range(len(points) - 1):
        first, second = points[index], points[index + 1]
        vx, vz = second[1] - first[1], second[2] - first[2]
        length2 = vx * vx + vz * vz
        if length2 <= 0.0:
            continue
        t = ((x - first[1]) * vx + (z - first[2]) * vz) / length2
        t = min(1.0, max(0.0, t))
        px, pz = first[1] + t * vx, first[2] + t * vz
        lateral = math.dist((x, z), (px, pz))
        if lateral < best_lateral:
            best_lateral = lateral
            best_distance = first[0] + t * (second[0] - first[0])
    return best_lateral, best_distance


def test_real_fixture_lap_distance_maps_to_expected_suzuka_section() -> None:
    """실녹화의 게임 lap_distance가 130m 보정 뒤 실제 주행 구간과 맞는다.

    픽스처 구간(세션 62.66초)은 헤어핀(11번) 탈출 직후다. Motion (x, z)를
    `docs/spec/real-game-check.md`의 정합 변환으로 중심선에 투영하면 약
    2,868~2,876m이고, 게임 lap_distance 3,006~3,015m에서
    `lap_distance_offset_m`(130m)을 뺀 값과 15m 안에서 맞는다. 보정하지 않으면
    같은 지점이 200R(12번) 코너 구간으로 잘못 판정된다.
    """
    track = load_track(13)
    outline = load_outline(13)
    assert track is not None and outline is not None

    pairs: list[tuple[float, float, float]] = []
    latest_motion: dict[str, object] | None = None
    for _recv_ns, data in iter_recording(FIXTURE):
        packet = parse_packet(data)
        index = packet.header.player_car_index
        packet_id = PacketId(packet.header.packet_id)
        if packet_id is PacketId.MOTION:
            entries = packet.body.get("car_motion_data")
            if isinstance(entries, list) and index < len(entries):
                latest_motion = entries[index]
        elif packet_id is PacketId.LAP_DATA and latest_motion is not None:
            entries = packet.body.get("lap_data")
            if not isinstance(entries, list) or index >= len(entries):
                continue
            entry = entries[index]
            distance = entry.get("lap_distance")
            x = latest_motion.get("world_position_x")
            z = latest_motion.get("world_position_z")
            if isinstance(distance, (int, float)) and isinstance(
                x, (int, float)
            ) and isinstance(z, (int, float)):
                pairs.append((float(distance), float(x), float(z)))

    assert pairs
    reported_corners = set()
    for lap_distance, x, z in pairs:
        center_x, center_z = _game_to_centerline(x, z)
        lateral, centerline_m = _nearest_centerline(outline, center_x, center_z)
        assert lateral < 10.0, f"중심선에서 {lateral:.1f}m 벗어났습니다"
        assert track.lap_distance_offset_m + centerline_m == pytest.approx(
            lap_distance, abs=15.0
        )
        track_distance = track.track_distance(lap_distance)
        assert 2860.0 <= track_distance < 2900.0
        assert track.corner_at(lap_distance, lap_distance=True) is None
        raw_corner = track.corner_at(lap_distance)
        assert raw_corner is not None and raw_corner.number == 12
        reported_corners.add(raw_corner.number)

    assert reported_corners == {12}
