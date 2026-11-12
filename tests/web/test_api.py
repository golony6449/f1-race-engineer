"""웹 API: 상태 모양, 문턱값 표, WS 푸시, 정적 파일 서빙.

공용 conftest를 만들지 않는 규칙에 따라 패킷 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from f1re.alerts.thresholds import color_table
from f1re.config import load_config
from f1re.state.store import StateStore
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
from f1re.web.app import STALE_AFTER_MS, create_app

SESSION_UID = 0x0123_4567_89AB_CDEF
PLAYER_CAR_INDEX = 0

TYRE_POSITIONS = ["front_left", "front_right", "rear_left", "rear_right"]


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


def _player_body(
    packet_id: PacketId, list_key: str, **updates: Any
) -> dict[str, Any]:
    """패킷 본문의 내 차 항목만 바꾼다."""
    body = _default_body(packet_id)
    entries = list(body[list_key])
    entries[PLAYER_CAR_INDEX] = {**entries[PLAYER_CAR_INDEX], **updates}
    body[list_key] = entries
    return body


def _session_body(**updates: Any) -> dict[str, Any]:
    body = _default_body(PacketId.SESSION)
    body.update(updates)
    return body


class PacketBuilder:
    """테스트용 패킷 생성기. 실제 파서·수신 파이프라인을 그대로 통과시킨다."""

    def __init__(self, store: StateStore | None = None) -> None:
        self.store = store if store is not None else StateStore()
        self.frame = 0

    def apply(
        self,
        packet_id: PacketId,
        body: dict[str, Any],
        *,
        recv_ns: int | None = None,
    ) -> None:
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
        data = pack_packet(header, body)
        result = process_datagram(
            self.store,
            data,
            time.time_ns() if recv_ns is None else recv_ns,
        )
        assert result is not None, f"{packet_id.name} 패킷이 거부됐습니다"


def _populated_store(*, recv_ns: int | None = None) -> StateStore:
    """타이어·브레이크·손상·연료·ERS가 채워진 상태 저장소."""
    builder = PacketBuilder()
    when = time.time_ns() if recv_ns is None else recv_ns

    builder.apply(
        PacketId.SESSION,
        _session_body(total_laps=50, track_length=5807, track_id=13, session_type=13),
        recv_ns=when,
    )
    builder.apply(
        PacketId.LAP_DATA,
        _player_body(
            PacketId.LAP_DATA,
            "lap_data",
            current_lap_num=10,
            lap_distance=1200.0,
            pit_status=0,
        ),
        recv_ns=when,
    )
    # 배열 순서는 공식 패킷 순서(뒤왼쪽, 뒤오른쪽, 앞왼쪽, 앞오른쪽)다.
    builder.apply(
        PacketId.CAR_TELEMETRY,
        _player_body(
            PacketId.CAR_TELEMETRY,
            "car_telemetry_data",
            brakes_temperature=[380, 390, 410, 420],
            tyres_surface_temperature=[90, 91, 95, 96],
            tyres_inner_temperature=[100, 101, 105, 106],
            tyres_pressure=[22.1, 22.2, 22.5, 22.6],
        ),
        recv_ns=when,
    )
    builder.apply(
        PacketId.CAR_STATUS,
        _player_body(
            PacketId.CAR_STATUS,
            "car_status_data",
            fuel_in_tank=18.0,
            fuel_remaining_laps=10.0,
            actual_tyre_compound=17,
            tyres_age_laps=3,
            ers_store_energy=2_000_000.0,
            ers_deploy_mode=2,
        ),
        recv_ns=when,
    )
    builder.apply(
        PacketId.CAR_DAMAGE,
        _player_body(
            PacketId.CAR_DAMAGE,
            "car_damage_data",
            tyres_wear=[10.0, 11.0, 12.0, 13.0],
            front_left_wing_damage=30,
            front_right_wing_damage=60,
            rear_wing_damage=0,
            floor_damage=10,
            diffuser_damage=5,
            sidepod_damage=0,
            gear_box_damage=0,
            engine_damage=0,
            engine_mguh_wear=80,
            engine_es_wear=10,
            engine_ce_wear=10,
            engine_ice_wear=10,
            engine_mguk_wear=10,
            engine_tc_wear=10,
        ),
        recv_ns=when,
    )
    return builder.store


def _client(store: StateStore) -> TestClient:
    return TestClient(create_app(store))


def _severities(node: Any) -> list[str]:
    """응답 안의 모든 severity 문자열을 모은다."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "severity" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_severities(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_severities(item))
    return found


# ---------------------------------------------------------------------------
# 상태 API


def test_state_endpoint_shape() -> None:
    data = _client(_populated_store()).get("/api/state").json()
    assert "snapshot" in data
    assert data["snapshot"]["session_uid"] == SESSION_UID

    car = data["car"]
    assert car["car_index"] == PLAYER_CAR_INDEX
    assert car["has_data"] is True

    # 타이어 4개: 마모, 표면·내부 온도, 압력, 컴파운드, 사용 랩
    assert [tyre["position"] for tyre in car["tyres"]] == TYRE_POSITIONS
    front_left = car["tyres"][0]
    assert front_left["label"] == "앞왼쪽"
    assert front_left["wear"]["value"] == pytest.approx(12.0)
    assert front_left["wear"]["severity"] == "ok"
    assert front_left["surface_temp"]["value"] == pytest.approx(95.0)
    assert front_left["surface_temp"]["severity"] == "ok"
    assert front_left["inner_temp"]["value"] == pytest.approx(105.0)
    assert front_left["pressure"]["value"] == pytest.approx(22.5)
    assert front_left["compound"]["value"] == "C4"
    assert front_left["compound"]["code"] == 17
    assert front_left["age_laps"]["value"] == pytest.approx(3.0)
    # 뒤쪽은 패킷 배열의 0·1번이다.
    assert car["tyres"][2]["wear"]["value"] == pytest.approx(10.0)
    assert car["tyres"][3]["wear"]["value"] == pytest.approx(11.0)

    # 브레이크 온도 4개
    assert [brake["position"] for brake in car["brakes"]] == TYRE_POSITIONS
    assert car["brakes"][0]["temp"]["value"] == pytest.approx(410.0)
    assert car["brakes"][0]["temp"]["severity"] == "ok"
    assert car["brakes"][3]["temp"]["value"] == pytest.approx(390.0)

    # 손상 항목: 날개 좌우·뒷날개·바닥·디퓨저·사이드포드·기어박스·엔진·파워유닛 마모
    damage = {item["key"]: item for item in car["damage"]}
    assert set(damage) == {
        "front_left_wing",
        "front_right_wing",
        "rear_wing",
        "floor",
        "diffuser",
        "sidepod",
        "gearbox",
        "engine",
        "engine_mguh_wear",
        "engine_es_wear",
        "engine_ce_wear",
        "engine_ice_wear",
        "engine_mguk_wear",
        "engine_tc_wear",
    }
    assert damage["front_left_wing"]["value"]["severity"] == "warn"
    assert damage["front_right_wing"]["value"]["severity"] == "danger"
    assert damage["rear_wing"]["value"]["severity"] == "ok"
    assert damage["engine_mguh_wear"]["value"]["severity"] == "warn"
    assert damage["engine_mguh_wear"]["label"] == "MGU-H 마모"

    # 연료: 남은 양, 랩당 소모, 남은 랩, 남은 레이스 랩, 여유
    fuel = car["fuel"]
    assert fuel["remaining_kg"]["value"] == pytest.approx(18.0)
    assert fuel["per_lap_kg"]["value"] == pytest.approx(1.8)
    assert fuel["remaining_laps"]["value"] == pytest.approx(10.0)
    assert fuel["remaining_race_laps"]["value"] == pytest.approx(41.0)
    assert fuel["laps_margin"]["value"] == pytest.approx(-31.0)
    assert fuel["laps_margin"]["severity"] == "danger"

    # ERS: 잔량 %, 모드
    assert car["ers"]["store_pct"]["value"] == pytest.approx(50.0)
    assert car["ers"]["mode"]["value"] == "핫랩"
    assert car["ers"]["mode"]["code"] == 2

    # 모든 severity는 /api/thresholds 색 표의 키다(화면은 이 키로 색만 고른다).
    severities = set(_severities(car))
    assert severities <= set(color_table()["colors"])


def test_empty_state_keeps_skeleton_and_radio_status() -> None:
    data = _client(StateStore()).get("/api/state").json()
    car = data["car"]

    assert car["car_index"] is None
    assert car["has_data"] is False
    # 값이 없어도 칸이 사라지지 않는다.
    assert [tyre["position"] for tyre in car["tyres"]] == TYRE_POSITIONS
    assert len(car["brakes"]) == 4
    assert len(car["damage"]) == 14
    assert all(tyre["wear"]["value"] is None for tyre in car["tyres"])
    assert all(tyre["wear"]["severity"] == "no_data" for tyre in car["tyres"])

    assert data["connection"]["stale"] is True
    assert data["connection"]["data_age_ms"] is None
    assert data["connection"]["stale_after_ms"] == STALE_AFTER_MS

    # 음성·무전 페이지 상태는 서버가 실제 값으로 내려 준다(설계 6.9-5).
    # 음성 캐시 없는 기본 앱이므로 오프라인, 무전 페이지는 아직 미연결이다.
    assert data["status"]["voice"] == {"state": "offline", "label": "음성 오프라인"}
    assert data["status"]["radio_page"]["state"] == "disconnected"
    assert data["status"]["radio_page"]["label"] == "무전 페이지 미연결"
    assert data["status"]["radio_page"]["connected"] is False


def test_stale_state_keeps_last_values() -> None:
    old = time.time_ns() - 10_000_000_000
    data = _client(_populated_store(recv_ns=old)).get("/api/state").json()

    assert data["connection"]["stale"] is True
    assert data["connection"]["data_age_ms"] >= 9_000
    # 수신이 끊겨도 마지막 값은 그대로 내려 온다(화면이 흐리게 유지한다).
    assert data["car"]["tyres"][0]["wear"]["value"] == pytest.approx(12.0)
    assert data["car"]["fuel"]["remaining_kg"]["value"] == pytest.approx(18.0)


def test_thresholds_endpoint_returns_color_table() -> None:
    data = _client(StateStore()).get("/api/thresholds").json()

    assert data == color_table()
    assert data["alert_severity"] == "danger"
    assert set(data["colors"]) == {"ok", "warn", "danger", "no_data"}
    assert data["metrics"]["tyre_wear"]["warn"] == pytest.approx(50.0)
    assert data["metrics"]["tyre_wear"]["danger"] == pytest.approx(75.0)


def test_web_config_defaults() -> None:
    config = load_config()

    assert config.web.host == "0.0.0.0"
    assert config.web.port == 8080


# ---------------------------------------------------------------------------
# WebSocket


def test_ws_pushes_updates() -> None:
    store = StateStore()
    builder = PacketBuilder(store)
    client = _client(store)

    with client.websocket_connect("/ws/state") as websocket:
        first = websocket.receive_json()
        assert first["car"]["tyres"][0]["wear"]["value"] is None

        builder.apply(
            PacketId.CAR_DAMAGE,
            _player_body(
                PacketId.CAR_DAMAGE,
                "car_damage_data",
                tyres_wear=[0.0, 0.0, 55.0, 0.0],
            ),
        )
        second = websocket.receive_json()
        # 화면 순서의 첫 타이어는 앞왼쪽(패킷 2번)이다.
        assert second["car"]["tyres"][0]["wear"]["value"] == pytest.approx(55.0)
        assert second["car"]["tyres"][0]["wear"]["severity"] == "warn"
        assert second["connection"]["stale"] is False


# ---------------------------------------------------------------------------
# 정적 프런트


def test_dashboard_and_static_files_are_served() -> None:
    client = _client(StateStore())

    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/static/dashboard.js" in response.text
    assert "/static/style.css" in response.text

    references = set(re.findall(r'(?:src|href)="(/static/[^"]+)"', response.text))
    assert references, "대시보드 HTML이 정적 파일을 참조하지 않습니다"
    for reference in sorted(references):
        static = client.get(reference)
        assert static.status_code == 200, reference
        assert static.content, reference

    # dashboard.js가 불러오는 모듈(carstate.js)도 서빙된다.
    dashboard_js = client.get("/static/dashboard.js").text
    imports = re.findall(r"""from\s+["'](\./[^"']+)["']""", dashboard_js)
    assert imports, "dashboard.js가 carstate.js를 불러오지 않습니다"
    for relative in imports:
        path = "/static/" + relative.removeprefix("./")
        assert client.get(path).status_code == 200, path
