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
from f1re.laps.traces import LapTrace, LapTraceRecorder, Sample
from f1re.pipeline import RacePipeline
from f1re.radio.queue import RadioQueue
from f1re.state.store import StateStore
from f1re.tracks.model import load_outline, load_track
from f1re.udp.parser import Packet
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId

SUZUKA_ID = 13


class _Clock:
    """시험에서 시간을 앞당길 수 있는 시계."""

    def __init__(self) -> None:
        self.now = time.time_ns()

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> int:
        self.now += int(seconds * 10**9)
        return self.now


class _Radio:
    """무전 큐만 있는 대역."""

    def __init__(self, clock: _Clock | None = None) -> None:
        self.queue = RadioQueue(clock=clock or time.time_ns)

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


def _session_body(
    track_id: int,
    session_type: int = 18,
    *,
    total_laps: int = 0,
) -> dict[str, Any]:
    return {
        "track_id": track_id,
        "session_type": session_type,
        "weather": 0,
        "track_length": 5807.0,
        "total_laps": total_laps,
        "safety_car_status": 0,
        "game_paused": 0,
    }


def _demo_lap_trace(lap: int = 2, *, pace: float = 1.0) -> LapTrace:
    track = load_track(SUZUKA_ID)
    outline = load_outline(SUZUKA_ID)
    assert track is not None and outline is not None
    samples = generate_lap(
        track, outline, DriverProfile(pace=pace), lap, random.Random(0)
    )
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
        pipeline._session_type = 18  # 타임 트라이얼
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
    coach = Coach(track, player_car_index=0, session_type=15)
    player_lap = _demo_lap_trace(lap=2)
    other_lap = dataclasses.replace(player_lap, car_index=3, lap=2)

    coach.add_reference_lap(other_lap)

    fastest = coach.references.best_lap(SOURCE_FASTEST_CAR)
    assert fastest is not None and fastest.car_index == 3
    assert coach.references.best_lap("self") is None
    assert coach.focus.corners == ()


def _lap_data_body(*, lap: int = 2, position: int = 8) -> dict[str, Any]:
    entry = {
        "current_lap_num": lap,
        "lap_distance": 1000.0,
        "car_position": position,
        "total_distance": (lap - 1) * 5807.0 + 1000.0,
        "delta_to_car_in_front_minutes_part": 0,
        "delta_to_car_in_front_ms_part": 600,
        "current_lap_invalid": 0,
        "driver_status": 4,
        "pit_status": 0,
    }
    return {"lap_data": [entry]}


# ---------------------------------------------------------------------------
# 접전(traffic) 판정(SCRUM-124)


def _lap_entry(
    *,
    index: int = 0,
    lap: int = 2,
    distance: float = 1000.0,
    lap_time_ms: int = 10_000,
    position: int = 8,
    gap_ms: int | None = 600,
    total_distance: float | None = None,
    driver_status: int = 4,
    result_status: int = 2,
) -> dict[str, Any]:
    """Lap Data 차량 한 대 항목. `lap=0`이면 기록하지 않는 빈 칸이다."""
    if lap <= 0:
        return {"current_lap_num": 0}
    entry: dict[str, Any] = {
        "current_lap_num": lap,
        "lap_distance": distance,
        "current_lap_time_in_ms": lap_time_ms,
        "car_position": position,
        "total_distance": (
            (lap - 1) * 5807.0 + distance
            if total_distance is None
            else total_distance
        ),
        "current_lap_invalid": 0,
        "driver_status": driver_status,
        "pit_status": 0,
        "result_status": result_status,
    }
    if gap_ms is not None:
        entry["delta_to_car_in_front_minutes_part"] = gap_ms // 60_000
        entry["delta_to_car_in_front_ms_part"] = gap_ms % 60_000
    return entry


def _entries(player: dict[str, Any], others: list[dict[str, Any]] | None = None) -> list:
    entries: list = [{"current_lap_num": 0} for _ in range(22)]
    entries[0] = player
    for offset, entry in enumerate(others or [], start=1):
        entries[offset] = entry
    return entries


def _telemetry_body() -> dict[str, Any]:
    entries: list = [{} for _ in range(22)]
    entries[0] = {
        "speed": 220.0,
        "throttle": 1.0,
        "brake": 0.0,
        "steer": 0.0,
        "gear": 7,
        "surface_type": [0, 0, 0, 0],
    }
    return {"car_telemetry_data": entries}


def _race_pipeline() -> RacePipeline:
    pipeline = RacePipeline(StateStore(), _Radio(), None, config=AppConfig())
    pipeline._session_uid = 1
    pipeline._player_index = 0
    pipeline._session_type = 15
    pipeline._total_laps = 20
    return pipeline


def test_lap_data_marks_traffic_when_car_ahead_within_one_second() -> None:
    """앞차와 1초 이내면 코치 입력 샘플에 접전 표시가 붙는다."""
    pipeline = _race_pipeline()
    pipeline._on_lap_data(
        {"lap_data": _entries(_lap_entry(position=8, gap_ms=600))}
    )

    assert pipeline._player_in_traffic is True
    state = pipeline._car_state(_telemetry_body()["car_telemetry_data"][0])
    assert state is not None
    assert state["in_traffic"] is True


def test_lap_data_marks_traffic_when_car_behind_within_one_second() -> None:
    """바로 뒤 차가 1초 이내면(그 차의 앞차 간격) 접전으로 본다."""
    behind = _lap_entry(index=1, position=9, gap_ms=700)
    pipeline = _race_pipeline()
    pipeline._on_lap_data(
        {
            "lap_data": _entries(
                _lap_entry(position=8, gap_ms=5000), [behind]
            )
        }
    )

    assert pipeline._player_in_traffic is True


def test_lap_data_does_not_mark_traffic_outside_one_second() -> None:
    pipeline = _race_pipeline()
    pipeline._on_lap_data(
        {"lap_data": _entries(_lap_entry(position=8, gap_ms=1200))}
    )

    assert pipeline._player_in_traffic is False


def test_lap_data_does_not_mark_traffic_outside_race() -> None:
    """타임 트라이얼 등 비레이스 세션은 간격이 가까워도 접전으로 보지 않는다."""
    pipeline = _race_pipeline()
    pipeline._session_type = 18
    pipeline._on_lap_data(
        {"lap_data": _entries(_lap_entry(position=8, gap_ms=600))}
    )

    assert pipeline._player_in_traffic is False


def test_lap_data_position_change_marks_next_sample() -> None:
    """코너 안 순위 변동을 다음 코치 입력 샘플 하나에 표시한다."""
    pipeline = _race_pipeline()
    pipeline._on_lap_data(
        {"lap_data": _entries(_lap_entry(position=8, gap_ms=5000))}
    )
    assert pipeline._position_changed is False

    pipeline._on_lap_data(
        {"lap_data": _entries(_lap_entry(position=7, gap_ms=5000))}
    )

    assert pipeline._position_changed is True
    state = pipeline._car_state(_telemetry_body()["car_telemetry_data"][0])
    assert state is not None and state["position_changed"] is True


def test_recorder_marks_traffic_and_position_change_on_samples() -> None:
    """완료 랩 트레이스의 샘플에도 접전·순위 변동이 남아 기준 랩 제외에 쓰인다."""
    recorder = LapTraceRecorder()
    traces = []

    def feed(packet: Packet) -> None:
        traces.extend(recorder.on_packet(packet))

    feed(_packet(PacketId.SESSION, _session_body(SUZUKA_ID, session_type=15)))
    feed(
        _packet(
            PacketId.LAP_DATA,
            {"lap_data": _entries(_lap_entry(lap=1, position=8, gap_ms=600))},
        )
    )
    feed(_packet(PacketId.CAR_TELEMETRY, _telemetry_body()))
    feed(
        _packet(
            PacketId.LAP_DATA,
            {
                "lap_data": _entries(
                    _lap_entry(
                        lap=1, distance=1100.0, position=7, gap_ms=3000
                    )
                )
            },
        )
    )
    feed(_packet(PacketId.CAR_TELEMETRY, _telemetry_body()))
    # 다음 랩으로 넘어가면 이전 랩이 마감된다.
    feed(
        _packet(
            PacketId.LAP_DATA,
            {
                "lap_data": _entries(
                    _lap_entry(lap=2, distance=0.0, position=7, gap_ms=3000)
                )
            },
        )
    )

    trace = next(trace for trace in traces if trace.car_index == 0)
    assert len(trace.samples) == 2
    assert trace.samples[0].in_traffic is True
    assert trace.samples[0].position_changed is False
    assert trace.samples[1].in_traffic is False
    assert trace.samples[1].position_changed is True


async def test_pipeline_pushes_praise_for_personal_best() -> None:
    """완료 랩이 개인 최고를 갱신하면 칭찬 무전이 큐로 나간다(SCRUM-125)."""
    clock = _Clock()
    radio = _Radio(clock)
    pipeline = RacePipeline(
        StateStore(), radio, None, config=AppConfig(), clock=clock
    )
    await pipeline.start()
    try:
        pipeline.on_packet(
            _packet(
                PacketId.SESSION, _session_body(SUZUKA_ID, session_type=18)
            ),
            clock(),
        )
        pipeline.on_packet(
            _packet(PacketId.LAP_DATA, _lap_data_body(lap=18)), clock()
        )
        assert pipeline._praise is not None

        # 첫 유효 랩은 기준만 세운다.
        await pipeline._process_laps([_demo_lap_trace(lap=17, pace=0.98)])
        assert [
            message
            for message in radio.queue.pending()
            if message.evidence.get("praise")
        ] == []

        # 더 빠른 랩은 개인 최고 → 칭찬 무전 하나.
        await pipeline._process_laps([_demo_lap_trace(lap=18, pace=1.0)])
        praises = [
            message
            for message in radio.queue.pending()
            if message.evidence.get("praise")
        ]
        assert len(praises) == 1
        assert praises[0].evidence["praise"] == "personal_best"
        assert praises[0].kind == "improvement"
    finally:
        await pipeline.aclose()


async def test_pipeline_reacts_to_player_overtake_in_race() -> None:
    """레이스에서 내 OVTK 사건이 오면 잠시 뒤 추월 리액션이 큐로 나간다."""
    clock = _Clock()
    radio = _Radio(clock)
    pipeline = RacePipeline(
        StateStore(), radio, None, config=AppConfig(), clock=clock
    )
    await pipeline.start()
    try:
        pipeline.on_packet(
            _packet(
                PacketId.SESSION,
                _session_body(SUZUKA_ID, session_type=15, total_laps=20),
            ),
            clock(),
        )
        # 레이스 주말 기준 키에는 팀이 필요하다. 참가자 패킷으로 채운다.
        participants = [{"team_id": 1}] + [{"team_id": 2}] * 21
        pipeline.on_packet(
            _packet(PacketId.PARTICIPANTS, {"participants": participants}),
            clock(),
        )
        pipeline.on_packet(
            _packet(PacketId.LAP_DATA, _lap_data_body(lap=2)), clock()
        )
        assert pipeline._reaction is not None

        pipeline._on_event(
            {
                "event_string_code": "OVTK",
                "event_details": {
                    "overtaking_vehicle_idx": 0,
                    "being_overtaken_vehicle_idx": 1,
                },
            },
            clock(),
        )
        assert pipeline._reaction.pending_count == 1

        clock.advance(3.0)
        pipeline._evaluate_overtake()

        reactions = [
            message
            for message in radio.queue.pending()
            if message.evidence.get("reaction")
        ]
        assert len(reactions) == 1
        assert reactions[0].kind == "overtake"
        assert reactions[0].evidence["reaction"] == "single"
    finally:
        await pipeline.aclose()


async def test_pipeline_ignores_overtake_of_retired_or_pit_car() -> None:
    """순위만 올라간 경우(리타이어·피트)에는 추월 리액션을 하지 않는다."""
    clock = _Clock()
    radio = _Radio(clock)
    pipeline = RacePipeline(
        StateStore(), radio, None, config=AppConfig(), clock=clock
    )
    await pipeline.start()
    try:
        pipeline.on_packet(
            _packet(
                PacketId.SESSION,
                _session_body(SUZUKA_ID, session_type=15, total_laps=20),
            ),
            clock(),
        )
        participants = [{"team_id": 1}] + [{"team_id": 2}] * 21
        pipeline.on_packet(
            _packet(PacketId.PARTICIPANTS, {"participants": participants}),
            clock(),
        )
        # 1번 차는 리타이어(result_status 7) 상태다(driver_status는 주행 중으로 둬 result_status 필터를 검증).
        entries = _lap_data_body(lap=2)["lap_data"]
        entries.append({"result_status": 7, "driver_status": 4})
        pipeline.on_packet(
            _packet(PacketId.LAP_DATA, {"lap_data": entries}), clock()
        )
        assert pipeline._reaction is not None

        pipeline._on_event(
            {
                "event_string_code": "OVTK",
                "event_details": {
                    "overtaking_vehicle_idx": 0,
                    "being_overtaken_vehicle_idx": 1,
                },
            },
            clock(),
        )

        assert pipeline._reaction.pending_count == 0
    finally:
        await pipeline.aclose()


async def test_pipeline_ignores_overtake_event_outside_race() -> None:
    """비레이스 세션에서는 OVTK 사건을 받아도 리액션을 만들지 않는다."""
    clock = _Clock()
    radio = _Radio(clock)
    pipeline = RacePipeline(
        StateStore(), radio, None, config=AppConfig(), clock=clock
    )
    await pipeline.start()
    try:
        pipeline.on_packet(
            _packet(
                PacketId.SESSION, _session_body(SUZUKA_ID, session_type=18)
            ),
            clock(),
        )
        pipeline.on_packet(
            _packet(PacketId.LAP_DATA, _lap_data_body(lap=2)), clock()
        )
        assert pipeline._reaction is not None

        pipeline._on_event(
            {
                "event_string_code": "OVTK",
                "event_details": {
                    "overtaking_vehicle_idx": 0,
                    "being_overtaken_vehicle_idx": 1,
                },
            },
            clock(),
        )

        assert pipeline._reaction.pending_count == 0
        assert [
            message
            for message in radio.queue.pending()
            if message.evidence.get("reaction")
        ] == []
    finally:
        await pipeline.aclose()


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
