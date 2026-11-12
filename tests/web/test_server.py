"""`f1re serve`가 같은 프로세스에서 웹 대시보드까지 띄우는지 확인한다."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx

from f1re.main import main


def _free_port() -> int:
    """지금 비어 있는 TCP 포트를 하나 고른다(짧은 경합은 감수한다)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_dashboard(web_port: int, deadline_s: float = 10.0) -> httpx.Response:
    deadline = time.monotonic() + deadline_s
    last_error: Exception | None = None
    with httpx.Client(trust_env=False, timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                return client.get(f"http://127.0.0.1:{web_port}/")
            except httpx.TransportError as exc:
                last_error = exc
                time.sleep(0.05)
    raise AssertionError(f"웹 대시보드가 뜨지 않았습니다: {last_error}")


def test_serve_live_starts_web_dashboard(tmp_path: Path) -> None:
    web_port = _free_port()
    args = [
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--duration",
        "1.5",
        "--db",
        str(tmp_path / "f1re.db"),
        "--web-host",
        "127.0.0.1",
        "--web-port",
        str(web_port),
    ]
    thread = threading.Thread(target=main, args=(args,), daemon=True)
    thread.start()
    try:
        response = _wait_for_dashboard(web_port)
        assert response.status_code == 200
        assert "/static/dashboard.js" in response.text
    finally:
        thread.join(timeout=15)
    assert not thread.is_alive(), "serve가 제 시간에 종료되지 않았습니다"
