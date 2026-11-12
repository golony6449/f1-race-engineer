"""서킷 지도 API(작업 10, SCRUM-60): 윤곽·코너, 차 위치, 깃발 상태.

공용 conftest를 만들지 않는 규칙에 따라 패킷 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from f1re import db
from f1re.state.store import StateStore
from f1re.tracks.model import load_outline, load_track
from f1re.tracks.outline import TrackOutline
from f1re.udp.formats import f2025
from f1re.udp.packer import pack_packet
from f1re.udp.receiver import process_datagram
from f1re.udp.schema import (
    MatrixField,
    RawField,
    ScalarField,
    StringField,
    StructDef,
    StructField,
)
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId
from f1re.web.app import create_app

SESSION_UID = 0x0BAD_C0DE_1234_5678
PLAYER_CAR_INDEX = 0
SUZUKA_ID = 13


# ---------------------------------------------------------------------------
# 패킷 도우미


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


def _session_body(**updates: Any) -> dict[str, Any]:
    body = _default_body(PacketId.SESSION)
    body.update(updates)
    return body


def _participants_body(teams: dict[int, int]) -> dict[str, Any]:
    body = _default_body(PacketId.PARTICIPANTS)
    entries = list(body["participants"])
    for car_index, team_id in teams.items():
        entries[car_index] = {
            **entries[car_index],
            "team_id": team_id,
            "name": f"CAR {car_index}",
            "race_number": car_index + 1,
        }
    body["participants"] = entries
    return body


def _lap_data_body(entries: dict[int, dict[str, Any]]) -> dict[str, Any]:
    body = _default_body(PacketId.LAP_DATA)
    lap_entries = list(body["lap_data"])
    for car_index, values in entries.items():
        lap_entries[car_index] = {**lap_entries[car_index], **values}
    body["lap_data"] = lap_entries
    return body


class PacketBuilder:
    """테스트용 패킷 생성기. 실제 파서·수신 파이프라인을 그대로 통과시킨다."""

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.frame = 0

    def apply(self, packet_id: PacketId, body: dict[str, Any]) -> None:
        self.frame += 1
        header = Header(
            packet_format=PACKET_FORMAT,
            game_year=25,
            game_major=1,
            game_minor=0,
            packet_version=1,
            packet_id=int(packet_id),
            session_uid=SESSION_UID,
            session_time=self.frame / 60.0,
            frame_id=self.frame,
            overall_frame_id=self.frame,
            player_car_index=PLAYER_CAR_INDEX,
            secondary_player_car_index=255,
        )
        result = process_datagram(
            self.store, pack_packet(header, body), self.frame * 1_000_000
        )
        assert result is not None, f"{packet_id.name} 패킷이 거부됐습니다"


def _client(store: StateStore, conn: Any = None) -> TestClient:
    return TestClient(create_app(store, conn=conn))


def _with_session(track_id: int, **session_updates: Any) -> tuple[StateStore, PacketBuilder]:
    store = StateStore()
    builder = PacketBuilder(store)
    builder.apply(
        PacketId.SESSION,
        _session_body(track_id=track_id, track_length=5807, **session_updates),
    )
    return store, builder


def _populate_gp(store: StateStore) -> PacketBuilder:
    """내 차(3위)와 앞차(2위)·뒷차(4위)가 달리는 그랑프리 상태."""
    builder = PacketBuilder(store)
    builder.apply(
        PacketId.PARTICIPANTS, _participants_body({0: 0, 1: 8, 2: 1})
    )
    builder.apply(
        PacketId.LAP_DATA,
        _lap_data_body(
            {
                0: {
                    "current_lap_num": 3,
                    "lap_distance": 500.0,
                    "total_distance": 2 * 5807 + 500.0,
                    "car_position": 3,
                },
                1: {
                    "current_lap_num": 3,
                    "lap_distance": 530.0,
                    "total_distance": 2 * 5807 + 530.0,
                    "car_position": 2,
                },
                2: {
                    "current_lap_num": 3,
                    "lap_distance": 900.0,
                    "total_distance": 2 * 5807 + 900.0,
                    "car_position": 4,
                },
            }
        ),
    )
    return builder


# ---------------------------------------------------------------------------
# /api/track


def test_track_api_returns_packaged_outline_corners_and_placeholders() -> None:
    store, _builder = _with_session(SUZUKA_ID)
    data = _client(store).get("/api/track").json()

    assert data["track_id"] == SUZUKA_ID
    assert data["name"] == "스즈카 서킷"
    assert data["outline_source"] == "data"
    assert data["length_m"] == pytest.approx(5807.0, rel=0.01)

    outline = load_outline(SUZUKA_ID)
    track = load_track(SUZUKA_ID)
    assert outline is not None and track is not None
    assert data["outline"], "윤곽 점이 비어 있습니다"
    assert len(data["outline"]) == len(outline.points)
    distances = [point[0] for point in data["outline"]]
    assert distances == sorted(distances)
    assert len(set(distances)) == len(distances)

    assert len(data["corners"]) == len(track.corners)
    for corner, official in zip(data["corners"], track.corners, strict=True):
        assert corner["number"] == official.number
        assert corner["official"] is True
        assert corner["entry_m"] == pytest.approx(official.entry_m)
        assert corner["apex_m"] == pytest.approx(official.apex_m)
        assert corner["exit_m"] == pytest.approx(official.exit_m)
        expected = outline.position_at(official.apex_m)
        assert (corner["x"], corner["z"]) == pytest.approx(expected)

    # 집중 코너·코너 지표는 작업 15·16이 채운다. 지금은 빈 자리만 있다.
    assert data["focus_corners"] == []
    assert data["corner_metrics"] == {}
    assert data["flag"] == "none"
    assert data["team_colors"]["0"] == "#00d2be"
    assert data["team_colors"]["8"] == "#ff8000"


def test_track_api_uses_learned_outline_for_unknown_track(tmp_path: Path) -> None:
    """코너 데이터도 중심선도 없는 서킷은 저장된 학습 윤곽을 쓴다(리뷰 포커스 5)."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        learned = TrackOutline(
            [
                (0.0, 0.0, 0.0),
                (100.0, 100.0, 0.0),
                (200.0, 100.0, 100.0),
                (300.0, 0.0, 100.0),
            ]
        )
        db.save_track_outline(conn, 99, learned, learned_at_ns=123)
        store, _builder = _with_session(99)
        data = _client(store, conn).get("/api/track").json()

        assert data["track_id"] == 99
        assert data["outline_source"] == "learned"
        assert data["name"] is None
        assert data["length_m"] == pytest.approx(learned.lap_length_m)
        assert [[point[0], point[1], point[2]] for point in data["outline"]] == [
            [dist, x, z] for dist, x, z in learned.points
        ]
        assert data["corners"] == []
    finally:
        conn.close()


def test_track_api_without_track_data_returns_empty_skeleton() -> None:
    data = _client(StateStore()).get("/api/track").json()

    assert data["track_id"] is None
    assert data["outline_source"] == "none"
    assert data["outline"] == []
    assert data["corners"] == []
    assert data["focus_corners"] == []
    assert data["corner_metrics"] == {}
    assert data["flag"] == "none"
    # 팀 색은 트랙과 무관하게 항상 내려 준다(차 점을 먼저 그릴 수 있게).
    assert data["team_colors"]


# ---------------------------------------------------------------------------
# /api/state·/ws/state의 차 위치


def test_car_positions_from_lap_distance_without_motion() -> None:
    """Motion 패킷이 없어도 랩 거리로 윤곽 위를 보간해 모든 차를 그린다."""
    store, _builder = _with_session(SUZUKA_ID)
    _populate_gp(store)
    data = _client(store).get("/api/state").json()

    cars = {car["car"]: car for car in data["cars"]}
    assert set(cars) == {0, 1, 2}

    outline = load_outline(SUZUKA_ID)
    assert outline is not None
    expected = {0: 500.0, 1: 530.0, 2: 900.0}
    for car_index, distance in expected.items():
        car = cars[car_index]
        assert car["dist_m"] == pytest.approx(distance)
        assert car["x"] is not None and car["z"] is not None
        assert (car["x"], car["z"]) == pytest.approx(
            outline.position_at(distance)
        )

    # 팀 색·내 차 강조·앞차 표시.
    assert cars[0]["is_player"] is True
    assert cars[0]["team_id"] == 0
    assert cars[1]["team_id"] == 8
    assert cars[2]["team_id"] == 1
    assert cars[0]["is_target"] is False
    assert cars[1]["is_target"] is True, "3위의 바로 앞(2위)이 추월 대상이어야 합니다"
    assert cars[2]["is_target"] is False

    # 계획의 필수 필드가 모두 있다.
    for car in cars.values():
        assert {"car", "dist_m", "x", "z", "team_id", "is_player", "is_target"} <= set(
            car
        )


def test_car_positions_without_outline_have_no_xy() -> None:
    """윤곽이 아직 없으면 위치만 비우고 차 목록은 유지한다(화면이 기다린다)."""
    store, _builder = _with_session(99)
    _populate_gp(store)
    data = _client(store).get("/api/state").json()

    assert len(data["cars"]) == 3
    assert all(car["x"] is None and car["z"] is None for car in data["cars"])


def test_ws_state_pushes_car_positions() -> None:
    store = StateStore()
    client = _client(store)
    builder = PacketBuilder(store)

    with client.websocket_connect("/ws/state") as websocket:
        first = websocket.receive_json()
        assert first["cars"] == []

        builder.apply(PacketId.SESSION, _session_body(track_id=SUZUKA_ID))
        _populate_gp(store)

        payload = websocket.receive_json()
        for _ in range(5):
            if payload["cars"]:
                break
            payload = websocket.receive_json()

        assert len(payload["cars"]) == 3
        player = next(car for car in payload["cars"] if car["is_player"])
        assert player["x"] is not None


@pytest.mark.parametrize(
    ("status", "expected"),
    [(0, "none"), (1, "safety_car"), (2, "vsc")],
)
def test_flag_state_follows_safety_car_status(status: int, expected: str) -> None:
    store, _builder = _with_session(SUZUKA_ID, safety_car_status=status)
    client = _client(store)

    assert client.get("/api/state").json()["flag"] == expected
    assert client.get("/api/track").json()["flag"] == expected
