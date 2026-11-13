"""SCRUM-84 파이프라인 단위 테스트(코너 자동 탐지·세션 재시작·플래시백).

실제 UDP 종단 테스트(`tests/test_pipeline_integration.py`)가 데모 서킷(스즈카)만
다루므로, 여기서는 리뷰 포커스 1·5의 경계를 직접 확인한다.

- 코너 데이터가 없는 서킷: 첫 유효 랩에서 코너를 자동 탐지해 트랙·코치를 만든다.
- sessionUID 변경: 이전 코치·기준을 버리고 새 세션으로 다시 만든다.
- 프레임·랩 거리 되감기(플래시백): 그 랩을 무효로 표시한다.
"""

from __future__ import annotations

import dataclasses
import random
import time
from typing import Any

from f1re.config import AppConfig
from f1re.coaching.coach import Coach
from f1re.coaching.reference import SOURCE_FASTEST_CAR
from f1re.demo_sender.driving import DriverProfile, generate_lap
from f1re.laps.traces import LapTrace, Sample
from f1re.pipeline import RacePipeline
from f1re.radio.queue import RadioQueue
from f1re.state.store import StateStore
from f1re.tracks.model import load_outline, load_track
from f1re.udp.parser import Packet
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId

SUZUKA_ID = 13


class _Radio:
    """무전 큐만 있는 대역. 테스트는 큐 내용을 직접 보지 않는다."""

    def __init__(self) -> None:
        self.queue = RadioQueue()

    def ensure_started(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _packet(
    packet_id: PacketId,
    body: dict[str, Any],
    *,
    uid: int = 1,
    frame: int = 0,
    player_index: int = 0,
) -> Packet:
    return Packet(
        header=Header(
            packet_format=PACKET_FORMAT,
            game_year=25,
            game_major=1,
            game_minor=0,
            packet_version=1,
            packet_id=int(packet_id),
            session_uid=uid,
            session_time=0.0,
            frame_id=frame,
            overall_frame_id=frame,
            player_car_index=player_index,
            secondary_player_car_index=255,
        ),
        body=body,
    )


def _session_body(track_id: int, session_type: int = 13) -> dict[str, Any]:
    return {
        "track_id": track_id,
        "session_type": session_type,
        "weather": 0,
        "track_length": 5807.0,
        "safety_car_status": 0,
        "game_paused": 0,
    }


def _demo_lap_trace(lap: int = 2) -> LapTrace:
    track = load_track(SUZUKA_ID)
    outline = load_outline(SUZUKA_ID)
    assert track is not None and outline is not None
    samples = generate_lap(track, outline, DriverProfile(), lap, random.Random(0))
    return LapTrace(
        car_index=0,
        lap=lap,
        valid=True,
        invalid_reason=None,
        samples=[
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
            for sample in samples
        ],
    )


async def test_auto_detected_track_builds_coaches_without_data_file() -> None:
    """데이터 파일이 없는 트랙 ID에서도 첫 유효 랩으로 코너·코치를 만든다."""
    pipeline = RacePipeline(
        StateStore(), _Radio(), None, config=AppConfig()
    )
    await pipeline.start()
    try:
        pipeline._session_uid = 1
        pipeline._player_index = 0
        pipeline._track_id = 99  # 데이터 파일이 없는 트랙 ID
        pipeline._session_type = 13  # 타임 트라이얼
        pipeline._track_length = 5807.0

        await pipeline._process_laps([_demo_lap_trace()])

        track = pipeline.track
        assert track is not None
        assert track.track_id == 99
        assert track.corners
        # 자동 탐지 코너는 공식 번호가 아니다(리뷰 포커스 5).
        assert all(corner.official is False for corner in track.corners)
        assert pipeline.coach is not None

        state = pipeline.track_state()
        assert state is not None
        assert len(state["corners"]) == len(track.corners)
        # 첫 랩이 곧 기준 랩이라 모든 코너 지표가 0ms 손실로 채워진다.
        assert state["corner_metrics"]
        assert all(
            item["loss_ms"] == 0 for item in state["corner_metrics"].values()
        )
    finally:
        await pipeline.aclose()


async def test_session_uid_change_rebuilds_pipeline() -> None:
    """sessionUID가 바뀌면 이전 코치를 버리고 새 세션용으로 다시 만든다."""
    pipeline = RacePipeline(
        StateStore(), _Radio(), None, config=AppConfig()
    )
    await pipeline.start()
    try:
        pipeline.on_packet(
            _packet(PacketId.SESSION, _session_body(SUZUKA_ID)), time.time_ns()
        )
        first_coach = pipeline.coach
        assert first_coach is not None

        pipeline.on_packet(
            _packet(
                PacketId.SESSION, _session_body(SUZUKA_ID), uid=2
            ),
            time.time_ns(),
        )
        assert pipeline.session_uid == 2
        assert pipeline.coach is not None
        assert pipeline.coach is not first_coach
    finally:
        await pipeline.aclose()


def test_add_reference_lap_updates_fastest_car_reference() -> None:
    """다른 차 랩은 집중 코너를 건드리지 않고 최속차 기준에만 들어간다."""
    track = load_track(SUZUKA_ID)
    assert track is not None
    coach = Coach(track, player_car_index=0, session_type=10)
    player_lap = _demo_lap_trace(lap=2)
    other_lap = dataclasses.replace(player_lap, car_index=3, lap=2)

    coach.add_reference_lap(other_lap)

    fastest = coach.references.best_lap(SOURCE_FASTEST_CAR)
    assert fastest is not None and fastest.car_index == 3
    assert coach.references.best_lap("self") is None
    assert coach.focus.corners == ()


async def test_flashback_marks_lap_invalid() -> None:
    """랩 거리 되감기·프레임 되감기를 플래시백으로 표시해 코칭에서 뺀다."""
    pipeline = RacePipeline(
        StateStore(), _Radio(), None, config=AppConfig()
    )
    await pipeline.start()
    try:
        pipeline.on_packet(
            _packet(PacketId.SESSION, _session_body(SUZUKA_ID)), time.time_ns()
        )
        entry = {
            "current_lap_num": 2,
            "lap_distance": 500.0,
            "driver_status": 4,
            "pit_status": 0,
            "current_lap_invalid": 0,
        }
        pipeline.on_packet(
            _packet(PacketId.LAP_DATA, {"lap_data": [entry]}), time.time_ns()
        )
        assert "flashback" not in pipeline._invalid_reasons

        # 같은 랩에서 거리가 뒤로 갔다 → 플래시백.
        rewind = dict(entry, lap_distance=100.0)
        pipeline.on_packet(
            _packet(PacketId.LAP_DATA, {"lap_data": [rewind]}), time.time_ns()
        )
        assert "flashback" in pipeline._invalid_reasons

        # 프레임 되감기도 같은 이유로 표시된다.
        pipeline._invalid_reasons.clear()
        pipeline.on_packet(
            _packet(PacketId.SESSION, _session_body(SUZUKA_ID), frame=100),
            time.time_ns(),
        )
        pipeline.on_packet(
            _packet(PacketId.SESSION, _session_body(SUZUKA_ID), frame=50),
            time.time_ns(),
        )
        assert "flashback" in pipeline._invalid_reasons
    finally:
        await pipeline.aclose()
