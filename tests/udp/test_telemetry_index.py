"""다시 보기 텔레메트리 인덱스·카탈로그·다운샘플러 테스트(SCRUM-134).

공용 conftest를 만들지 않는 규칙에 따라 패킷 생성기·합성 녹화 빌더를 이
파일 안에 둔다. 실제 게임 녹화 없이 `CarTelemetry`·`Motion`·`LapData`·
`CarStatus`·`CarDamage`·`Participants`·`Event` 패킷을 50Hz로 만들어
`.f1rec` 파일 한 개로 1회 스캔 인덱서·비교 차량·비공개 처리·LRU 스토어를
검증한다.
"""

from __future__ import annotations

import math
from array import array
from pathlib import Path
from typing import Any

import pytest

from f1re.udp.formats import f2025
from f1re.udp.packer import pack_packet
from f1re.udp.recording import RecordingWriter
from f1re.udp.schema import (
    MatrixField,
    RawField,
    ScalarField,
    StringField,
    StructField,
)
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId
from f1re.udp.telemetry_index import (
    TELEMETRY_CATALOG,
    TELEMETRY_PRESETS,
    MetricChannel,
    TelemetryIndexStore,
    build_telemetry_index,
    catalog_payload,
    downsample_channel,
    get_metric_spec,
    presets_payload,
)

SESSION_UID = 0x0F1_2026_1115_0001
# 녹화 첫 패킷의 수신 시각. 실제 게임처럼 0이 아닌 절대 시각(ns)을 쓴다.
BASE_NS = 1_700_000_000_000_000_000
# 합성 녹화의 패킷 간격(ms). 50Hz로 충분히 촘촘하다.
STEP_MS = 20
# 비공개 텔레메트리 참가자 인덱스(테스트용).
PRIVATE_CAR = 5


# ---------------------------------------------------------------------------
# 카탈로그·프리셋
# ---------------------------------------------------------------------------


def test_catalog_covers_categories_and_private_flags() -> None:
    cats = [c.id for c in TELEMETRY_CATALOG]
    assert cats == ["driving", "tyres_brakes", "energy_fuel", "lap_pace", "damage_setup"]
    assert get_metric_spec("speed").restricted_when_private is True
    assert get_metric_spec("g_lat").restricted_when_private is False
    assert get_metric_spec("gap_front_s").restricted_when_private is False
    assert get_metric_spec("gear").step is True
    payload = catalog_payload()
    assert len(payload) == 5
    tyre_keys = [m["key"] for m in payload[1]["metrics"]]
    assert "tyre_surface_temp_fl" in tyre_keys
    assert "brake_temp_rr" in tyre_keys


def test_catalog_wheel_groups_and_counts() -> None:
    """4륜 항목은 바퀴별 키 4개와 `wheel_group`을 갖고 카테고리 수가 맞는다."""
    counts = {category.id: len(category.metrics) for category in TELEMETRY_CATALOG}
    assert counts == {
        "driving": 11,
        "tyres_brakes": 20,
        "energy_fuel": 8,
        "lap_pace": 6,
        "damage_setup": 9,
    }
    for suffix in ("fl", "fr", "rl", "rr"):
        spec = get_metric_spec(f"tyre_pressure_{suffix}")
        assert spec is not None
        assert spec.wheel_group == "tyre_pressure"
        assert spec.wheel_label is not None


def test_presets_reference_valid_metric_keys() -> None:
    presets = presets_payload()
    assert len(presets) >= 4
    for preset in presets:
        assert preset["metrics"]
        for key in preset["metrics"]:
            assert get_metric_spec(key) is not None
    assert {preset["id"] for preset in TELEMETRY_PRESETS} == {
        "driving",
        "tyres_brakes",
        "energy_fuel",
        "lap_pace",
    }


# ---------------------------------------------------------------------------
# min/max 다운샘플러
# ---------------------------------------------------------------------------


def test_downsample_preserves_spikes_and_skips_nans() -> None:
    times = array("i", range(0, 1000, 10))  # 100개 샘플 (0..990ms)
    vals = array("f", [0.0] * 100)
    vals[25] = 100.0  # 짧은 브레이크 스파이크
    vals[50] = math.nan  # 미수신 구간
    ch = MetricChannel(times_ms=times, values=vals)
    spec = get_metric_spec("brake")
    points, downsampled = downsample_channel(ch, spec, 0, 990, max_points=20)
    assert downsampled is True
    assert len(points) <= 20
    assert any(p[0] == 250 and p[1] == 100.0 for p in points)
    assert all(math.isfinite(p[1]) for p in points)
    # 시간 오름차순 유지
    assert [p[0] for p in points] == sorted(p[0] for p in points)


def test_downsample_returns_raw_points_when_within_max_points() -> None:
    ch = MetricChannel(times_ms=array("i", [0, 100, 200]), values=array("f", [1.0, 2.0, 3.0]))
    spec = get_metric_spec("gear")
    points, downsampled = downsample_channel(ch, spec, 0, 200, max_points=10)
    assert downsampled is False
    assert points == [[0, 1.0], [100, 2.0], [200, 3.0]]


def test_downsample_step_metric_keeps_bucket_representatives() -> None:
    """`step=True` 이산 지표는 버킷마다 마지막 유효값(변화점)을 남긴다."""
    times = array("i", range(0, 1000, 10))
    vals = array("f", [1.0] * 100)
    vals[25] = 4.0
    for index in range(26, 100):
        vals[index] = 4.0
    ch = MetricChannel(times_ms=times, values=vals)
    points, downsampled = downsample_channel(
        ch, get_metric_spec("gear"), 0, 990, max_points=20
    )
    assert downsampled is True
    assert len(points) <= 20
    # 세 번째 버킷(인덱스 20~29)의 마지막 값은 4.0(290ms)이다.
    assert [290, 4.0] in points
    assert points[-1] == [990, 4.0]
    assert all(p[1] == 1.0 or p[1] == 4.0 for p in points)


def test_downsample_range_slice_and_all_nan() -> None:
    """구간 밖 점은 무시하고, 전부 NaN이면 빈 결과를 낸다."""
    ch = MetricChannel(
        times_ms=array("i", [0, 100, 200, 300]),
        values=array("f", [1.0, math.nan, 3.0, 4.0]),
    )
    points, downsampled = downsample_channel(
        ch, get_metric_spec("speed"), 100, 300, max_points=10
    )
    assert downsampled is False
    assert points == [[200, 3.0], [300, 4.0]]

    nan_ch = MetricChannel(
        times_ms=array("i", [0, 100]), values=array("f", [math.nan, math.nan])
    )
    assert downsample_channel(nan_ch, get_metric_spec("speed"), 0, 100, 10) == ([], False)


# ---------------------------------------------------------------------------
# 합성 녹화 생성기
# ---------------------------------------------------------------------------


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
        return [
            _default_struct_body(field_def.struct)
            for _ in range(field_def.count)
        ]
    raise TypeError(f"알 수 없는 필드 정의: {field_def!r}")


def _default_struct_body(struct_def: Any) -> dict[str, Any]:
    return {field.key: _default_value(field) for field in struct_def.fields}


def _default_body(packet_id: PacketId) -> dict[str, Any]:
    return _default_struct_body(f2025.PACKETS[packet_id].body)


class RecordingBuilder:
    """합성 `.f1rec` 레코드를 순서대로 만든다(테스트 파일 안에 둔다)."""

    def __init__(self, *, player_car_index: int = 0) -> None:
        self.player_car_index = player_car_index
        self.frame = 0
        self.records: list[tuple[int, bytes]] = []

    def add(self, packet_id: PacketId, body: dict[str, Any], t_ms: int) -> None:
        self.frame += 1
        header = Header(
            packet_format=PACKET_FORMAT,
            game_year=25,
            game_major=1,
            game_minor=0,
            packet_version=1,
            packet_id=int(packet_id),
            session_uid=SESSION_UID,
            session_time=t_ms / 1000.0,
            frame_id=self.frame,
            overall_frame_id=self.frame,
            player_car_index=self.player_car_index,
            secondary_player_car_index=255,
        )
        self.records.append(
            (BASE_NS + t_ms * 1_000_000, pack_packet(header, body))
        )

    def write(self, path: Path) -> Path:
        with RecordingWriter(path) as writer:
            for recv_ns, data in self.records:
                writer.write(recv_ns, data)
        return path


def _session_body(safety_car_status: int) -> dict[str, Any]:
    body = _default_body(PacketId.SESSION)
    body.update(
        weather=0,
        track_id=13,
        session_type=15,
        total_laps=5,
        game_paused=0,
        safety_car_status=safety_car_status,
        num_marshal_zones=0,
    )
    return body


def _participants_body() -> dict[str, Any]:
    body = _default_body(PacketId.PARTICIPANTS)
    body["num_active_cars"] = 22
    for car_index, entry in enumerate(body["participants"]):
        entry["name"] = f"드라이버 {car_index}"
        entry["team_id"] = car_index % 10
        entry["ai_controlled"] = 0
        entry["your_telemetry"] = 0 if car_index == PRIVATE_CAR else 1
    return body


def _safety_car_status(t_ms: int) -> int:
    if 400 <= t_ms < 800:
        return 1  # 풀 세이프티 카
    if 1200 <= t_ms < 1600:
        return 2  # VSC
    return 0


def _lap_data_body(t_ms: int) -> dict[str, Any]:
    body = _default_body(PacketId.LAP_DATA)
    for car_index, entry in enumerate(body["lap_data"]):
        lap = 1
        if car_index in (1, 2, 3) and t_ms >= 500:
            lap = 2
        if car_index == 0 and t_ms >= 1500:
            lap = 2
        entry.update(
            current_lap_num=lap,
            lap_distance=100.0 + car_index * 10.0 + t_ms * 0.05,
            current_lap_time_in_ms=t_ms,
            delta_to_car_in_front_minutes_part=0,
            delta_to_car_in_front_ms_part=1200
            if car_index == PRIVATE_CAR
            else 500,
            delta_to_race_leader_minutes_part=0,
            delta_to_race_leader_ms_part=2500 + car_index,
            car_position=3 if car_index == 0 else (car_index % 20) + 1,
            pit_status=1 if (car_index == 0 and 700 <= t_ms < 900) else 0,
            result_status=1,
        )
    return body


def _telemetry_body(t_ms: int) -> dict[str, Any]:
    body = _default_body(PacketId.CAR_TELEMETRY)
    for car_index, entry in enumerate(body["car_telemetry_data"]):
        entry.update(
            speed=200 + (t_ms // 100) if car_index == 0 else 150 + car_index,
            throttle=float(t_ms % 100),
            brake=0.0,
            steer=0.0,
            gear=4,
            engine_rpm=9000,
            drs=0,
            clutch=0,
            engine_temperature=95,
            # F1 25 배열 순서: [RL, RR, FL, FR]
            tyres_surface_temperature=[90, 91, 92, 93],
            tyres_inner_temperature=[80, 81, 82, 83],
            tyres_pressure=[22.0, 22.5, 23.0, 23.5],
            brakes_temperature=[400, 410, 420, 430],
        )
    return body


def _motion_body(t_ms: int) -> dict[str, Any]:
    body = _default_body(PacketId.MOTION)
    for car_index, entry in enumerate(body["car_motion_data"]):
        entry.update(
            g_force_lateral=0.5 + car_index * 0.1 + t_ms / 10000.0,
            g_force_longitudinal=-0.3,
            g_force_vertical=0.1,
        )
    return body


def _status_body() -> dict[str, Any]:
    body = _default_body(PacketId.CAR_STATUS)
    for entry in body["car_status_data"]:
        entry.update(
            fuel_in_tank=50.0,
            fuel_remaining_laps=10.0,
            ers_store_energy=3_000_000.0,
            ers_deployed_this_lap=1_000_000.0,
            ers_harvested_this_lap_mguk=500_000.0,
            ers_harvested_this_lap_mguh=300_000.0,
            ers_deploy_mode=2,
            front_brake_bias=57,
        )
    return body


def _damage_body() -> dict[str, Any]:
    body = _default_body(PacketId.CAR_DAMAGE)
    for entry in body["car_damage_data"]:
        entry.update(
            tyres_wear=[10.0, 11.0, 12.0, 13.0],
            front_left_wing_damage=5,
            front_right_wing_damage=6,
            rear_wing_damage=1,
            floor_damage=2,
            diffuser_damage=3,
            sidepod_damage=4,
            gear_box_damage=2,
            engine_damage=1,
        )
    return body


def _event_body(code: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _default_body(PacketId.EVENT)
    body["event_string_code"] = code
    if details is not None:
        body["event_details"] = details
    return body


# 시각(ms) → 이벤트 패킷. 세션 공통 SC/VSC/RDFL·내 차 pit/penalty/overtake/
# flashback과, 화면에 넣으면 안 되는 다른 차 penalty·lap_complete 스팸을 섞는다.
TIMED_EVENTS: dict[int, list[tuple[str, dict[str, Any] | None]]] = {
    380: [("SCAR", {"safety_car_type": 1, "event_type": 0})],
    820: [("SCAR", {"safety_car_type": 1, "event_type": 2})],
    860: [
        (
            "PENA",
            {
                "penalty_type": 1,
                "infringement_type": 0,
                "vehicle_idx": 2,
                "other_vehicle_idx": 0,
                "time": 5,
                "lap_num": 1,
                "places_gained": 0,
            },
        )
    ],
    900: [
        (
            "PENA",
            {
                "penalty_type": 1,
                "infringement_type": 0,
                "vehicle_idx": 0,
                "other_vehicle_idx": 2,
                "time": 5,
                "lap_num": 1,
                "places_gained": 0,
            },
        )
    ],
    1000: [("OVTK", {"overtaking_vehicle_idx": 0, "being_overtaken_vehicle_idx": 3})],
    1100: [
        (
            "FLBK",
            {"flashback_frame_identifier": 3, "flashback_session_time": 0.9},
        )
    ],
    1760: [("RDFL", None)],
}


def build_race_recording(path: Path, *, duration_ms: int = 2000) -> Path:
    """합성 F1 25 레이스 녹화를 만든다.

    - 400~800ms 풀 SC, 1200~1600ms VSC, 1760ms 적기
    - 내 차 700~900ms 피트, 900ms 페널티, 1000ms 추월, 1100ms 플래시백
    - 내 차는 1500ms에 2랩, 다른 차 1~3은 500ms에 2랩(스팸 후보)
    """
    builder = RecordingBuilder()
    t_ms = 0
    while t_ms < duration_ms:
        if t_ms % 200 == 0:
            builder.add(PacketId.SESSION, _session_body(_safety_car_status(t_ms)), t_ms)
        if t_ms % 500 == 0:
            builder.add(PacketId.PARTICIPANTS, _participants_body(), t_ms)
        builder.add(PacketId.LAP_DATA, _lap_data_body(t_ms), t_ms)
        builder.add(PacketId.CAR_TELEMETRY, _telemetry_body(t_ms), t_ms)
        builder.add(PacketId.MOTION, _motion_body(t_ms), t_ms)
        builder.add(PacketId.CAR_STATUS, _status_body(), t_ms)
        builder.add(PacketId.CAR_DAMAGE, _damage_body(), t_ms)
        for code, details in TIMED_EVENTS.get(t_ms, []):
            builder.add(PacketId.EVENT, _event_body(code, details), t_ms)
        t_ms += STEP_MS
    return builder.write(path)


def recording_row(recording_id: int = 1, *, duration_ms: int = 2000) -> dict[str, Any]:
    return {
        "id": recording_id,
        "session_uid": SESSION_UID,
        "segment": 1,
        "category": "race",
        "session_type": 15,
        "track_id": 13,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# 1회 스캔 인덱서
# ---------------------------------------------------------------------------


def test_build_telemetry_index_extracts_series_map_frames_and_markers(
    tmp_path: Path,
) -> None:
    path = build_race_recording(tmp_path / "race.f1rec")
    index = build_telemetry_index(path, recording_row())

    assert index.recording_id == 1
    assert index.player_index == 0
    assert index.duration_ms >= 1900
    assert index.track_id == 13
    assert index.track["track_id"] == 13
    assert index.track["outline"]  # 스즈카 데이터 파일 윤곽

    # 내 차 60Hz 메트릭 시계열(패킷 종류별 채널).
    assert index.series is not None
    for key in ("speed", "throttle", "brake", "gear", "g_lat", "gap_front_s"):
        channel = index.series.channel(key)
        assert channel is not None, key
        assert len(channel) >= 50, key
    assert index.series.channel("fuel_kg") is not None
    assert index.series.channel("tyre_wear_fl") is not None
    # F1 25 배열 순서 [RL, RR, FL, FR] → 화면 키 _fl=2, _fr=3, _rl=0, _rr=1
    assert index.series.channel("tyre_surface_temp_fl").values[0] == 92.0
    assert index.series.channel("tyre_surface_temp_fr").values[0] == 93.0
    assert index.series.channel("tyre_surface_temp_rl").values[0] == 90.0
    assert index.series.channel("tyre_surface_temp_rr").values[0] == 91.0
    assert index.series.channel("brake_temp_rr").values[0] == 410.0

    # 10Hz 지도 프레임.
    frames = index.map_frames
    assert len(frames) >= 10
    assert frames[0]["t_ms"] == 0
    assert len(frames[0]["cars"]) == 22
    assert any(car["car"] == 0 and car["x"] is not None for car in frames[0]["cars"])
    frame = index.map_frame_at(555)
    assert frame["t_ms"] <= 555
    assert set(frame["flags"]) >= {"red", "sc", "vsc"}
    assert len(index.map_frames_range(0, 500)) < len(frames)

    # 마커: 세션 공통 구간 + 내 차 사건만. 다른 차 20대 lap_complete는 없어야 한다.
    markers = index.markers
    kinds = {event["kind"] for event in markers["events"]}
    assert {"penalty", "overtake", "flashback", "lap_complete"} <= kinds
    # 다른 차 20대 lap_complete는 차량별 경량 맵에만 있고 기본 마커엔 없다.
    assert all(
        event["car_index"] in (0, None) for event in markers["events"]
    )
    assert any(
        event["car_index"] == 3
        for event in index.markers_for_car(3)["events"]
    )
    assert not any(
        event["kind"] == "overtake" and event["car_index"] not in (0, None)
        for event in markers["events"]
    )
    interval_kinds = {interval["kind"] for interval in markers["intervals"]}
    assert {"safety_car", "vsc", "red_flag", "pit"} <= interval_kinds
    pit_bands = [
        interval for interval in markers["intervals"] if interval["kind"] == "pit"
    ]
    assert pit_bands and pit_bands[0]["car_index"] == 0
    assert any(band["start_ms"] >= 690 for band in pit_bands)
    # 내 차 랩 경계(L1=0, 1500ms에 L2).
    assert markers["laps"][0] == {"lap": 1, "start_ms": 0}
    assert any(
        item["lap"] == 2 and item["start_ms"] >= 1400 for item in markers["laps"]
    )
    # 무전 마커는 시간 오름차순이다(발화가 없어도 형식은 지킨다).
    radio_times = [item["t_ms"] for item in markers["radio"]]
    assert radio_times == sorted(radio_times)

    # 참가자 22대.
    assert len(index.participants) == 22
    assert index.participants[0]["is_player"] is True
    assert index.participants[PRIVATE_CAR]["your_telemetry"] == 0

    # 요약 묶음은 카탈로그·프리셋·마커를 모두 갖는다.
    summary = index.summary_payload()
    assert summary["recording"]["id"] == 1
    assert len(summary["catalog"]) == 5
    assert summary["presets"]
    assert summary["markers"] is markers


def test_query_series_clamps_validates_and_downsamples(tmp_path: Path) -> None:
    path = build_race_recording(tmp_path / "race.f1rec")
    index = build_telemetry_index(path, recording_row())

    result = index.query_series(
        ["speed", "gear"], -500, index.duration_ms + 5000, max_points=25, compare_car=0
    )
    assert result["from_ms"] == 0
    assert result["to_ms"] == index.duration_ms
    assert result["compare_car"] is None  # 내 차 지정은 비교 없음으로 정규화
    assert result["series"]["speed"]["downsampled"] is True
    assert len(result["series"]["speed"]["series"]) <= 25
    assert result["series"]["gear"]["step"] is True

    # 뒤집힌 구간은 두 값을 바꿔 받아들인다.
    flipped = index.query_series(["speed"], 1500, 500, max_points=800)
    assert (flipped["from_ms"], flipped["to_ms"]) == (500, 1500)

    with pytest.raises(ValueError):
        index.query_series(["not_a_metric"], 0, 100)


async def test_compare_car_respects_your_telemetry_privacy(tmp_path: Path) -> None:
    path = build_race_recording(tmp_path / "race.f1rec")
    store = TelemetryIndexStore()
    row = recording_row()
    assert await store.open_recording(1, row, path) == "building"
    index = await store.wait_ready(1)

    await store.ensure_compare_car(index, PRIVATE_CAR)
    result = index.query_series(
        ["speed", "gap_front_s", "g_lat"],
        0,
        index.duration_ms,
        max_points=800,
        compare_car=PRIVATE_CAR,
    )
    assert result["compare_car"] == PRIVATE_CAR
    assert result["compare_your_telemetry"] == 0

    # CarTelemetry 원천은 비공개 → 비교 시계열 억제.
    speed = result["series"]["speed"]
    assert speed["compare_series"] is None
    assert speed["compare_restricted"] is True
    # LapData·Motion 원천은 정상 비교.
    for key in ("gap_front_s", "g_lat"):
        metric = result["series"][key]
        assert metric["compare_series"]
        assert metric["compare_restricted"] is False
    assert result["series"]["gap_front_s"]["compare_series"][0][1] == pytest.approx(1.2)
    assert result["series"]["g_lat"]["compare_series"][0][1] != 0.5

    # 공개 차량(3)은 CarTelemetry 비교도 내려온다(비교 캐시는 1대만 유지).
    await store.ensure_compare_car(index, 3)
    public = index.query_series(
        ["speed"], 0, index.duration_ms, max_points=800, compare_car=3
    )
    assert public["series"]["speed"]["compare_series"]
    assert public["series"]["speed"]["compare_restricted"] is False
    await store.aclose()


async def test_ensure_compare_car_ignores_invalid_and_player(
    tmp_path: Path,
) -> None:
    path = build_race_recording(tmp_path / "race.f1rec", duration_ms=400)
    store = TelemetryIndexStore()
    await store.open_recording(1, recording_row(duration_ms=400), path)
    index = await store.wait_ready(1)
    try:
        await store.ensure_compare_car(index, index.player_index)
        assert index.compare_cache is None
        await store.ensure_compare_car(index, 99)
        await store.ensure_compare_car(index, -1)
        assert index.compare_cache is None
    finally:
        await store.aclose()


async def test_telemetry_index_store_lru_eviction_and_protected_ids(
    tmp_path: Path,
) -> None:
    path_one = build_race_recording(tmp_path / "one.f1rec", duration_ms=400)
    path_two = build_race_recording(tmp_path / "two.f1rec", duration_ms=400)
    store = TelemetryIndexStore()

    assert (
        await store.open_recording(1, recording_row(1, duration_ms=400), path_one)
        == "building"
    )
    await store.wait_ready(1)
    assert store.get_ready(1) is not None
    assert store.protected_recording_ids() == {1}

    assert (
        await store.open_recording(2, recording_row(2, duration_ms=400), path_two)
        == "building"
    )
    await store.wait_ready(2)
    assert store.get_ready(2) is not None
    # 새 녹화가 준비되면 직전 캐시 1건은 버린다(LRU=1).
    assert store.get_ready(1) is None
    assert store.protected_recording_ids() == {2}

    ready = store.status_or_summary(2)
    assert ready["status"] == "ready"
    assert ready["recording_id"] == 2
    assert "catalog" in ready and "markers" in ready
    assert store.status_or_summary(999)["status"] == "idle"
    await store.aclose()


async def test_open_recording_merges_concurrent_requests(tmp_path: Path) -> None:
    """같은 녹화의 동시 open은 하나의 빌드 태스크로 합쳐진다."""
    path = build_race_recording(tmp_path / "race.f1rec", duration_ms=600)
    store = TelemetryIndexStore()
    row = recording_row(duration_ms=600)
    first = await store.open_recording(7, row, path)
    second = await store.open_recording(7, row, path)
    assert first == "building"
    assert second == "building"
    assert len(store._tasks) == 1
    await store.wait_ready(7)
    # 이미 준비된 녹화는 다시 빌드하지 않는다.
    assert await store.open_recording(7, row, path) == "ready"
    await store.aclose()
