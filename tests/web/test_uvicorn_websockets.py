"""실제 uvicorn 서버에서 WebSocket 업그레이드가 되는지 확인한다.

TestClient는 WebSocket을 흉내 내지만, 실제 uvicorn은 WebSocket 구현
(`uvicorn[standard]`의 websockets/wsproto)이 없으면 업그레이드 요청을 404로
거절한다(2026-11-13 PL 실측: `/ws/radio` HTTP 404, "No supported WebSocket
library detected"). 그래서 임의 포트에 uvicorn을 직접 띄우고 `websockets`
클라이언트로 `/ws/state`와 `/ws/radio` 핸드셰이크를 확인한다.

서버는 테스트 안에서 띄우고 끝에 반드시 내린다.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from types import TracebackType

import uvicorn
from websockets.asyncio.client import connect

from f1re.state.store import StateStore
from f1re.web.app import create_app


def _free_port() -> int:
    """지금 비어 있는 TCP 포트를 하나 고른다(짧은 경합은 감수한다)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _UvicornServer:
    """uvicorn을 별도 스레드에서 띄우는 도우미. 끝나면 스레드를 정리한다."""

    def __init__(self, store: StateStore, port: int) -> None:
        self.config = uvicorn.Config(
            create_app(store),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(
            target=self.server.run, name="test-uvicorn", daemon=True
        )

    def __enter__(self) -> _UvicornServer:
        self.thread.start()
        deadline = time.monotonic() + 10.0
        while not self.server.started and time.monotonic() < deadline:
            if not self.thread.is_alive():
                raise AssertionError("uvicorn이 시작하지 못했습니다")
            time.sleep(0.02)
        assert self.server.started, "uvicorn이 제 시간에 시작하지 못했습니다"
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10.0)
        assert not self.thread.is_alive(), "uvicorn이 제 시간에 종료되지 않았습니다"


async def test_real_uvicorn_accepts_state_and_radio_websockets() -> None:
    store = StateStore()
    port = _free_port()

    with _UvicornServer(store, port):
        async with connect(f"ws://127.0.0.1:{port}/ws/state") as websocket:
            payload = json.loads(
                await asyncio.wait_for(websocket.recv(), timeout=5.0)
            )
            # 상태 푸시에는 차량 상태·음성 상태·지도 차 위치가 함께 온다.
            assert "car" in payload
            assert payload["status"]["voice"]["label"] == "음성 오프라인"
            assert payload["cars"] == []
            assert payload["flag"] == "none"

        async with connect(f"ws://127.0.0.1:{port}/ws/radio") as websocket:
            payload = json.loads(
                await asyncio.wait_for(websocket.recv(), timeout=5.0)
            )
            assert payload["type"] == "status"
            assert payload["status"]["radio_page"]["connected"] is True
