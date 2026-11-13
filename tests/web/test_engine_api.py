"""SCRUM-82: 엔진 OFF/ON일 때의 웹 API·WebSocket 동작.

OFF: `/health`·`/`·정적 파일·`/api/engine`만 동작하고 나머지 API는
503 `{"engine": "off"}`, WS는 engine-off 메시지를 보내고 닫는다.
ON: 기존 API·WS가 모두 동작한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from f1re.engine import ENGINE_OFF, ENGINE_ON, Engine, read_engine_state
from f1re.web.app import create_app

# OFF에서 503이어야 하는 API들(화면·health·engine API 제외).
_ENGINE_APIS = (
    "/api/state",
    "/api/thresholds",
    "/api/track",
    "/api/radio/log",
    "/api/report",
    "/api/lift-coast",
)


def _engine(tmp_path: Path) -> Engine:
    return Engine(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "f1re.db",
        state_path=tmp_path / "engine-state.json",
    )


def _client(engine: Engine) -> TestClient:
    return TestClient(create_app(engine=engine))


def _receive_until(websocket: Any, kind: str, limit: int = 30) -> list[dict]:
    received: list[dict] = []
    for _ in range(limit):
        payload = websocket.receive_json()
        received.append(payload)
        if payload.get("type") == kind:
            return received
    raise AssertionError(f"{kind} 메시지를 받지 못했습니다: {received}")


# ---------------------------------------------------------------------------
# OFF


def test_off_serves_only_health_pages_and_engine_api(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with _client(engine) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "engine": "off"}

        # 화면·정적 파일은 엔진이 꺼져 있어도 뜬다(설정에서 켤 수 있어야 한다).
        assert client.get("/").status_code == 200
        assert client.get("/radio").status_code == 200
        assert client.get("/static/dashboard.js").status_code == 200
        assert client.get("/static/style.css").status_code == 200
        assert client.get("/static/radio.js").status_code == 200

        # 엔진 API는 OFF에서도 동작한다. 같은 상태로 다시 요청해도 성공.
        assert client.get("/api/engine").json() == {"state": "off"}
        same = client.post("/api/engine", json={"state": "off"})
        assert same.status_code == 200
        assert same.json() == {"state": "off"}

        for path in _ENGINE_APIS:
            response = client.get(path)
            assert response.status_code == 503, path
            assert response.json() == {"engine": "off"}, path
        mute = client.post(
            "/api/radio/mute", json={"kind": "box", "muted": True}
        )
        assert mute.status_code == 503
        assert mute.json() == {"engine": "off"}

        bad = client.post("/api/engine", json={"state": "maybe"})
        assert bad.status_code == 400


def test_off_websockets_send_engine_off_and_close(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with _client(engine) as client:
        with client.websocket_connect("/ws/state") as websocket:
            assert websocket.receive_json() == {
                "type": "engine",
                "state": "off",
            }
        with client.websocket_connect("/ws/radio") as websocket:
            assert websocket.receive_json() == {
                "type": "engine",
                "state": "off",
            }


# ---------------------------------------------------------------------------
# ON / OFF 전환


def test_engine_api_starts_and_stops_runtime(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with _client(engine) as client:
        started = client.post("/api/engine", json={"state": "on"})
        assert started.status_code == 200
        assert started.json() == {"state": "on"}
        assert engine.state == ENGINE_ON
        assert engine.udp_port is not None
        assert client.get("/health").json() == {"status": "ok", "engine": "on"}

        state = client.get("/api/state")
        assert state.status_code == 200
        assert "car" in state.json()

        for path in _ENGINE_APIS:
            response = client.get(path)
            assert response.status_code == 200, path

        stopped = client.post("/api/engine", json={"state": "off"})
        assert stopped.status_code == 200
        assert stopped.json() == {"state": "off"}
        assert engine.state == ENGINE_OFF
        assert engine.udp_port is None
        assert client.get("/health").json() == {"status": "ok", "engine": "off"}
        assert client.get("/api/state").status_code == 503


def test_state_file_remembers_last_state(tmp_path: Path) -> None:
    state_path = tmp_path / "engine-state.json"
    engine = Engine(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "f1re.db",
        state_path=state_path,
    )
    with _client(engine) as client:
        client.post("/api/engine", json={"state": "on"})
        assert state_path.exists()
        client.post("/api/engine", json={"state": "off"})

    # 서버를 다시 띄운 상황: 저장된 상태를 그대로 읽는다.
    assert read_engine_state(state_path) == ENGINE_OFF


def test_turning_off_notifies_open_state_websocket(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with _client(engine) as client:
        client.post("/api/engine", json={"state": "on"})
        with client.websocket_connect("/ws/state") as websocket:
            first = websocket.receive_json()
            assert "car" in first

            client.post("/api/engine", json={"state": "off"})
            received = _receive_until(websocket, "engine")
            assert {
                "type": "engine",
                "state": "off",
            } in received


def test_turning_off_notifies_open_radio_websocket(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with _client(engine) as client:
        client.post("/api/engine", json={"state": "on"})
        with client.websocket_connect("/ws/radio") as websocket:
            initial = websocket.receive_json()
            assert initial["type"] == "status"

            client.post("/api/engine", json={"state": "off"})
            received = _receive_until(websocket, "engine")
            assert {
                "type": "engine",
                "state": "off",
            } in received


def test_engine_created_with_on_start_state_actually_starts(tmp_path: Path) -> None:
    """SCRUM-85: 시작 상태가 ON이어도 `start()`가 런타임·UDP를 실제로 만든다."""
    engine = Engine(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "f1re.db",
        state_path=tmp_path / "engine-state.json",
        initial_state=ENGINE_ON,
    )
    # 런타임을 만들기 전에는 OFF로 보여야 한다(화면·API가 거짓 ON을 보지 않게).
    assert engine.state == ENGINE_OFF
    with _client(engine) as client:
        client.portal.call(engine.start)
        assert engine.state == ENGINE_ON
        assert engine.runtime is not None
        assert engine.udp_port is not None
        assert client.get("/api/state").status_code == 200
        with client.websocket_connect("/ws/state") as websocket:
            payload = websocket.receive_json()
            assert payload.get("type") != "engine"
            assert "car" in payload
