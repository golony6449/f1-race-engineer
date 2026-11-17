"""SCRUM-126: 무전 화면의 세션 종류·코너 거리·마지막 수신 상태.

`/api/state`가 내려 주는 `session`·`corner` 값과, 무전 페이지가 그 값을
표시하고 수신 끊김/오래됨/오류를 구분하는 로직을 검증한다. 공용 conftest를
만들지 않는 규칙에 따라 패킷·가짜 파이프라인 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from f1re.engine import Engine, Runtime
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
from f1re.web.app import corner_info, create_app, session_info
from f1re.web.radio_api import RadioService

SESSION_UID = 0x0123_4567_89AB_CDEF
PLAYER_CAR_INDEX = 0

# 코너 3개짜리 가짜 트랙. 거리 구간은 1번 100~200, 2번 400~500, 3번 800~900.
TRACK_STATE: dict[str, Any] = {
    "track_id": 13,
    "name": "테스트 서킷",
    "length_m": 1000.0,
    "corners": [
        {
            "number": 1,
            "official": True,
            "entry_m": 100.0,
            "apex_m": 150.0,
            "exit_m": 200.0,
        },
        {
            "number": 2,
            "official": True,
            "entry_m": 400.0,
            "apex_m": 450.0,
            "exit_m": 500.0,
        },
        {
            "number": 3,
            "official": True,
            "entry_m": 800.0,
            "apex_m": 850.0,
            "exit_m": 900.0,
        },
    ],
}


# ---------------------------------------------------------------------------
# 패킷 도우미(tests/web/test_api.py와 같은 방식, 이 파일 안에만 둔다)


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
    """테스트용 패킷 생성기. 실제 파서·수신 경로를 그대로 통과시킨다."""

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
        data = pack_packet(header, body)
        result = process_datagram(self.store, data, time.time_ns())
        assert result is not None, f"{packet_id.name} 패킷이 거부됐습니다"


def _store(
    *,
    session_type: int | None = 15,
    lap_distance: float | None = 450.0,
) -> StateStore:
    """세션 패킷과 내 차 랩 거리만 채운 상태 저장소."""
    store = StateStore()
    builder = PacketBuilder(store)
    if session_type is not None:
        builder.apply(
            PacketId.SESSION,
            _session_body(
                session_type=session_type,
                track_id=13,
                track_length=1000,
                total_laps=50,
            ),
        )
    if lap_distance is not None:
        builder.apply(
            PacketId.LAP_DATA,
            _player_body(
                PacketId.LAP_DATA,
                "lap_data",
                current_lap_num=2,
                lap_distance=lap_distance,
            ),
        )
    return store


class _FakePipeline:
    """`RacePipeline.track_state()`만 흉내 내는 테스트용 파이프라인."""

    def __init__(self, track_state: dict[str, Any] | None) -> None:
        self._track_state = track_state

    def track_state(self) -> dict[str, Any] | None:
        return self._track_state


def _client(
    store: StateStore, track_state: dict[str, Any] | None = TRACK_STATE
) -> TestClient:
    engine = Engine(
        runtime=Runtime(
            store=store,
            radio=RadioService(store),
            conn=None,
            map=None,
            pipeline=_FakePipeline(track_state),
        )
    )
    return TestClient(create_app(engine=engine))


# ---------------------------------------------------------------------------
# 세션 종류


@pytest.mark.parametrize(
    ("session_type", "label", "type_label", "category"),
    [
        (1, "연습 · P1", "P1", "practice"),
        (5, "예선 · Q1", "Q1", "qualifying"),
        (12, "스프린트 슛아웃 · SS3", "SS3", "qualifying"),
        (15, "레이스", "레이스", "race"),
        (16, "레이스 · 레이스 2", "레이스 2", "race"),
        (18, "타임 트라이얼", "타임 트라이얼", "time_trial"),
    ],
)
def test_state_session_uses_korean_session_labels(
    session_type: int, label: str, type_label: str, category: str
) -> None:
    """SCRUM-131의 `session_category` 체계를 화면용 한국어 이름으로 내려 준다."""
    payload = _client(_store(session_type=session_type)).get("/api/state").json()

    session = payload["session"]
    assert session["session_type"] == session_type
    assert session["label"] == label
    assert session["session_type_label"] == type_label
    assert session["session_category"] == category


def test_state_session_is_empty_before_session_packet() -> None:
    """SESSION 패킷이 아직 없으면 화면이 `—`를 쓸 수 있게 모두 null이다."""
    payload = _client(_store(session_type=None)).get("/api/state").json()

    assert payload["session"] == {
        "session_type": None,
        "session_type_label": None,
        "session_category": None,
        "label": None,
    }


def test_session_info_marks_unknown_type() -> None:
    session = session_info(
        {"packets": {"session": {"session_type": 99}}}
    )

    assert session["session_type"] == 99
    assert session["session_type_label"] == "알 수 없음"
    assert session["session_category"] == "unknown"
    assert session["label"] is None


# ---------------------------------------------------------------------------
# 코너·남은 거리


def _corner_at(lap_distance: float) -> dict[str, Any]:
    snapshot = {
        "player_car_index": PLAYER_CAR_INDEX,
        "packets": {
            "lap_data": {
                "lap_data": [
                    {"lap_distance": lap_distance},
                    {},
                ]
            }
        },
    }
    return corner_info(snapshot, TRACK_STATE)


def test_corner_inside_corner_reports_current_and_next() -> None:
    """2번 코너(400~500m) 안: 현재 2번, 다음 3번, 남은 거리 350m."""
    corner = _corner_at(450.0)

    assert corner == {
        "track_loaded": True,
        "current_corner": 2,
        "next_corner": 3,
        "distance_to_next_corner_m": 350.0,
    }


def test_corner_on_straight_has_no_current_corner() -> None:
    """직선(300m): 현재 코너 없음, 다음 2번까지 100m."""
    corner = _corner_at(300.0)

    assert corner == {
        "track_loaded": True,
        "current_corner": None,
        "next_corner": 2,
        "distance_to_next_corner_m": 100.0,
    }


def test_corner_wraps_around_to_first_corner_next_lap() -> None:
    """마지막 코너를 지난 뒤(950m): 다음 랩 1번까지 150m."""
    corner = _corner_at(950.0)

    assert corner["current_corner"] is None
    assert corner["next_corner"] == 1
    assert corner["distance_to_next_corner_m"] == pytest.approx(150.0)


def test_corner_inside_last_corner_also_wraps() -> None:
    """마지막 코너(800~900m) 안: 현재 3번, 다음은 다음 랩 1번까지 250m."""
    corner = _corner_at(850.0)

    assert corner["current_corner"] == 3
    assert corner["next_corner"] == 1
    assert corner["distance_to_next_corner_m"] == pytest.approx(250.0)


def test_corner_distance_is_rounded_to_two_decimals() -> None:
    corner = _corner_at(301.234)

    assert corner["next_corner"] == 2
    assert corner["distance_to_next_corner_m"] == pytest.approx(98.77)


def test_corner_without_track_is_null() -> None:
    snapshot = {"player_car_index": PLAYER_CAR_INDEX, "packets": {}}
    assert corner_info(snapshot, None) == {
        "track_loaded": False,
        "current_corner": None,
        "next_corner": None,
        "distance_to_next_corner_m": None,
    }
    # 모양이 잘못된 트랙 상태도 예외 없이 같은 null 묶음이다.
    assert corner_info(snapshot, {"length_m": "x", "corners": "bad"}) == {
        "track_loaded": False,
        "current_corner": None,
        "next_corner": None,
        "distance_to_next_corner_m": None,
    }


def test_corner_without_lap_data_keeps_track_loaded() -> None:
    """트랙은 알지만 내 차 랩 거리가 아직 없으면 값만 비운다."""
    corner = corner_info({"player_car_index": None, "packets": {}}, TRACK_STATE)

    assert corner == {
        "track_loaded": True,
        "current_corner": None,
        "next_corner": None,
        "distance_to_next_corner_m": None,
    }


def test_state_corner_uses_pipeline_track_state() -> None:
    """/api/state가 활성 트랙의 코너 목록으로 현재/다음 코너를 내려 준다."""
    payload = _client(_store(lap_distance=450.0)).get("/api/state").json()

    assert payload["corner"] == {
        "track_loaded": True,
        "current_corner": 2,
        "next_corner": 3,
        "distance_to_next_corner_m": 350.0,
    }


def test_state_corner_is_null_without_pipeline_track() -> None:
    """파이프라인이 없거나 트랙을 모르면 트랙 미확인(null)으로 답한다."""
    payload = (
        _client(_store(lap_distance=450.0), track_state=None)
        .get("/api/state")
        .json()
    )

    assert payload["corner"] == {
        "track_loaded": False,
        "current_corner": None,
        "next_corner": None,
        "distance_to_next_corner_m": None,
    }


def test_state_corner_without_lap_data() -> None:
    payload = (
        _client(_store(lap_distance=None)).get("/api/state").json()
    )

    assert payload["corner"]["track_loaded"] is True
    assert payload["corner"]["current_corner"] is None
    assert payload["corner"]["next_corner"] is None
    assert payload["corner"]["distance_to_next_corner_m"] is None


def test_engine_off_state_api_stays_503() -> None:
    """엔진 OFF에서는 기존처럼 503이라 화면이 엔진 꺼짐 안내를 유지한다."""
    client = TestClient(create_app(engine=Engine(), db_path=":memory:"))

    response = client.get("/api/state")

    assert response.status_code == 503
    assert response.json() == {"engine": "off"}


# ---------------------------------------------------------------------------
# 무전 페이지 표시·수신 상태


def _radio_html(client: TestClient) -> str:
    response = client.get("/radio")
    assert response.status_code == 200
    return response.text


def _radio_css(html: str) -> str:
    match = re.search(r"<style>(.*?)</style>", html, re.S)
    assert match, "무전 페이지에 인라인 스타일이 없습니다"
    return match.group(1)


def test_radio_page_shows_session_corner_and_last_receive() -> None:
    html = _radio_html(_client(StateStore()))

    for element_id in (
        "session-value",
        "current-corner-value",
        "next-corner-value",
        "next-corner-distance",
        "data-status-age",
    ):
        assert f'id="{element_id}"' in html
    for label in ("세션", "현재 코너", "다음 코너", "다음 코너까지"):
        assert label in html


def test_radio_page_is_responsive_for_phone_widths() -> None:
    css = _radio_css(_radio_html(_client(StateStore())))

    context = re.search(r"\n\s*\.race-context \{(.*?)\}", css, re.S)
    assert context, ".race-context 규칙이 없습니다"
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in context.group(1)

    # 좁은 화면(640px 이하)에서는 2x2로 접는다.
    narrow = re.search(r"@media \(max-width: 640px\) \{(.*?)\n      \}", css, re.S)
    assert narrow, "640px 이하 규칙이 없습니다"
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in narrow.group(1)

    # 휴대폰 폭(480px 이하)에서는 여백·글자 크기를 줄여 겹침을 막는다.
    phone = re.search(r"@media \(max-width: 480px\) \{(.*?)\n      \}", css, re.S)
    assert phone, "480px 이하 규칙이 없습니다"
    rules = phone.group(1)
    for selector in (".race-context", ".context-cell", "main", "button"):
        assert selector in rules
    assert "overflow-wrap: anywhere;" in css


def test_radio_script_tracks_last_receive_age_and_error_state() -> None:
    script = _client(StateStore()).get("/static/radio.js").text

    # 마지막 정상 응답·패킷 수신 시각을 따로 들고 0.25초마다 초를 다시 센다.
    assert "let lastSuccessMs = null;" in script
    assert "let lastPacketMs = null;" in script
    assert "const DATA_TICK_MS = 250;" in script
    assert "setInterval(dataTick, DATA_TICK_MS)" in script
    assert "마지막 수신 ${seconds.toFixed(1)}초 전" in script
    assert "lastSuccessMs - connection.data_age_ms" in script

    # fetch 실패·HTTP 오류·3초 무응답은 빨간 error 상태로 바꾼다.
    assert "const DATA_TIMEOUT_MS = 3000;" in script
    assert "markDataError();" in script
    assert 'dataStatusNode.dataset.state = "error";' in script
    assert "서버 응답 없음" in script
    assert "if (engineState === \"on\") markDataError();" in script
    # 예전처럼 catch에서 아무것도 하지 않고 마지막 표시를 방치하지 않는다.
    assert "} catch (error) {\n    // 서버가 죽었거나" in script

    # 세션 종류와 코너·남은 거리(소수 둘째 자리) 표시.
    assert "session.label || \"—\"" in script
    assert '? "직선"' in script
    assert "corner.distance_to_next_corner_m.toFixed(2)" in script
    assert "clearRaceContext();" in script


def test_radio_script_keeps_stale_and_engine_off_states() -> None:
    script = _client(StateStore()).get("/static/radio.js").text

    # stale은 서버가 준 connection.stale로, 엔진 꺼짐은 기존 503 처리로 구분한다.
    assert "connection.stale" in script
    assert "response.status === 503" in script
    assert 'setEngineState("off");' in script
    assert "clearDataStatus();" in script
