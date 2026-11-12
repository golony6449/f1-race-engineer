"""데모 송신기 테스트: UDP 송신 주기·파싱·세션 이벤트."""

from __future__ import annotations

import collections
import math
import socket
import threading

import pytest

from f1re.demo_sender.sender import DemoSender, main
from f1re.tracks.model import load_outline, load_track
from f1re.udp.parser import Packet, parse_packet
from f1re.udp.spec import PACKET_FORMAT, PACKET_SIZES, PacketId

HOST = "127.0.0.1"


class _CollectingSocket:
    """실제 네트워크 없이 보낸 바이트를 모으는 소켓 대역."""

    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def sendto(self, payload: bytes, address: tuple[str, int]) -> None:
        self.packets.append(payload)

    def close(self) -> None:
        pass


def _sender(
    *,
    track_id: int = 13,
    laps: int = 1,
    seed: int = 0,
    speed: float = 5.0,
    rate_hz: int = 60,
    sock: object | None = None,
    port: int = 20777,
) -> DemoSender:
    track = load_track(track_id)
    outline = load_outline(track_id)
    assert track is not None and outline is not None
    return DemoSender(
        track,
        outline,
        laps=laps,
        seed=seed,
        host=HOST,
        port=port,
        rate_hz=rate_hz,
        speed=speed,
        sock=sock,  # type: ignore[arg-type]
    )


def _capture_with_udp(rate_hz: int = 60) -> list[bytes]:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    receiver.bind((HOST, 0))
    receiver.settimeout(0.02)
    sender = _sender(rate_hz=rate_hz, port=receiver.getsockname()[1])
    received: list[bytes] = []
    stop = threading.Event()

    def collect() -> None:
        while not stop.is_set():
            try:
                payload, _address = receiver.recvfrom(65535)
            except socket.timeout:
                continue
            received.append(payload)

    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    try:
        sender.run(duration_s=1.0)
    finally:
        stop.set()
        thread.join(timeout=2.0)
        receiver.close()
    return received


def _parse_all(payloads: list[bytes]) -> list[Packet]:
    return [parse_packet(payload) for payload in payloads]


def test_sender_emits_parsable_packets_at_rates() -> None:
    """1초 분량을 소켓으로 받아 모두 파싱되고 공식 주기대로 오는지 본다."""
    payloads = _capture_with_udp()
    packets = _parse_all(payloads)
    counts = collections.Counter(PacketId(packet.header.packet_id) for packet in packets)

    assert all(packet.header.packet_format == PACKET_FORMAT for packet in packets)
    assert all(len(payload) == PACKET_SIZES[packet.header.packet_id] for payload, packet in zip(payloads, packets))

    # 메뉴 설정 주기(60Hz). 1초 동안 틱 0~59에서 한 번씩.
    for packet_id in (
        PacketId.MOTION,
        PacketId.LAP_DATA,
        PacketId.CAR_TELEMETRY,
        PacketId.CAR_STATUS,
        PacketId.MOTION_EX,
    ):
        assert counts[packet_id] == 60, (packet_id, counts[packet_id])
    assert counts[PacketId.CAR_DAMAGE] == 10
    assert counts[PacketId.SESSION_HISTORY] == 20
    assert counts[PacketId.TYRE_SETS] == 20
    assert counts[PacketId.SESSION] == 2
    assert counts[PacketId.PARTICIPANTS] == 1
    assert counts[PacketId.TIME_TRIAL] == 1
    assert counts[PacketId.EVENT] == 1  # SSTA
    assert counts[PacketId.LAP_POSITIONS] == 0


def test_sender_rate_override_changes_telemetry_period() -> None:
    """--rate 20이면 60Hz 패킷이 20Hz로 줄고 나머지 주기는 그대로다."""
    counts = collections.Counter(
        PacketId(packet.header.packet_id) for packet in _parse_all(_capture_with_udp(rate_hz=20))
    )
    for packet_id in (
        PacketId.MOTION,
        PacketId.LAP_DATA,
        PacketId.CAR_TELEMETRY,
        PacketId.CAR_STATUS,
        PacketId.MOTION_EX,
    ):
        assert counts[packet_id] == 20, (packet_id, counts[packet_id])
    assert counts[PacketId.SESSION] == 2
    assert counts[PacketId.CAR_DAMAGE] == 10


def test_packet_contents_are_plausible() -> None:
    packets = _parse_all(_capture_with_udp())
    by_id = {PacketId(packet.header.packet_id): packet for packet in packets}

    session = by_id[PacketId.SESSION].body
    assert session["session_type"] == 13  # Time Trial
    assert session["game_mode"] == 2
    assert session["track_id"] == 13
    assert session["track_length"] == 5807

    telemetry = by_id[PacketId.CAR_TELEMETRY].body["car_telemetry_data"][0]
    assert 0 < telemetry["speed"] <= 340
    assert 1 <= telemetry["gear"] <= 8
    assert telemetry["engine_rpm"] > 0

    lap = by_id[PacketId.LAP_DATA].body["lap_data"][0]
    assert lap["current_lap_num"] == 1
    assert lap["car_position"] == 1
    assert 0.0 <= lap["lap_distance"] <= 5807.0

    participants = by_id[PacketId.PARTICIPANTS].body
    assert participants["num_active_cars"] == 1
    assert participants["participants"][0]["name"] == "YOU"

    motion = by_id[PacketId.MOTION].body["car_motion_data"][0]
    assert math.isfinite(motion["world_position_x"])
    assert math.isfinite(motion["world_position_z"])
    assert -32768 <= motion["world_forward_dir_x"] <= 32767

    event = by_id[PacketId.EVENT].body
    assert event["event_string_code"] == "SSTA"


def test_full_session_emits_lap_and_session_end_events() -> None:
    """정해진 랩을 다 돌면 FTLP(베스트 랩)와 SEND 이벤트를 보낸다."""
    collector = _CollectingSocket()
    sender = _sender(sock=collector, speed=10_000.0)
    sender.run()

    packets = _parse_all(collector.packets)
    events = [
        packet.body["event_string_code"]
        for packet in packets
        if packet.header.packet_id == PacketId.EVENT
    ]
    assert events[0] == "SSTA"
    assert events[-1] == "SEND"
    assert events.count("FTLP") == 1
    assert sender.lap_results and sender.lap_results[0].time_ms > 0

    time_trial = [
        packet.body["player_session_best_data_set"]
        for packet in packets
        if packet.header.packet_id == PacketId.TIME_TRIAL
    ]
    assert time_trial[-1]["lap_time_in_ms"] == sender.lap_results[0].time_ms
    assert time_trial[-1]["valid"] == 1

    history = [
        packet.body
        for packet in packets
        if packet.header.packet_id == PacketId.SESSION_HISTORY
    ]
    assert history[-1]["num_laps"] == 1
    assert history[-1]["lap_history_data"][0]["lap_time_in_ms"] == sender.lap_results[0].time_ms


def test_main_rejects_unimplemented_modes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--mode", "gp"]) == 2
    assert "작업 5" in capsys.readouterr().err

    assert main(["--scenario", "scenario.yaml"]) == 2
    assert "작업 5" in capsys.readouterr().err


def test_sender_validates_arguments() -> None:
    track = load_track(13)
    outline = load_outline(13)
    assert track is not None and outline is not None
    with pytest.raises(ValueError):
        DemoSender(track, outline, laps=0)
    with pytest.raises(ValueError):
        DemoSender(track, outline, rate_hz=15)
    with pytest.raises(ValueError):
        DemoSender(track, outline, speed=0)
