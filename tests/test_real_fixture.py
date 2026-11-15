"""실제 F1 25 게임 녹화 조각 회귀 테스트.

`tests/fixtures/real/recording-6-suzuka-race-snippet.f1rec`는 2026-11-15
스즈카 실전 레이스 녹화(recording id 6)에서 잘라 낸 아주 짧은 연속 구간이다.
원본 163MB는 저장소에 넣지 않는다. 파서가 실제 게임 패킷을 모두 받아들이고
F1 25 레이스 세션(15)·스즈카(13)로 인식하는지 확인한다.
"""

from __future__ import annotations

from pathlib import Path

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
