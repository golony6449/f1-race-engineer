"""SCRUM-82: 레이스 엔지니어 엔진 ON/OFF 수명 주기.

- 최초에는 상태 파일이 없으므로 OFF로 시작한다.
- ON이면 UDP 포트를 잡고, OFF면 소켓을 닫아 같은 포트로 다시 바인드할 수 있다.
- ON→OFF→ON을 반복해도 태스크·소켓이 쌓이지 않는다.
- 마지막 상태는 `data/engine-state.json` 모양의 파일에 남는다.
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import pytest

from f1re.engine import (
    ENGINE_OFF,
    ENGINE_ON,
    Engine,
    read_engine_state,
    write_engine_state,
)


def _free_udp_port() -> int:
    """지금 비어 있는 UDP 포트 하나(짧은 경합은 감수한다)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _can_bind(port: int) -> bool:
    """같은 UDP 포트로 새 소켓을 바인드할 수 있는지."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


async def _wait_until_bindable(port: int, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _can_bind(port):
            return True
        await asyncio.sleep(0.02)
    return False


def _pending_tasks() -> list[asyncio.Task[object]]:
    """지금 테스트 태스크 말고 살아 있는 태스크(엔진 누수 확인)."""
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]


def _engine(tmp_path: Path, port: int) -> Engine:
    return Engine(
        host="127.0.0.1",
        port=port,
        db_path=tmp_path / "f1re.db",
        state_path=tmp_path / "engine-state.json",
    )


# ---------------------------------------------------------------------------
# 상태 파일


def test_first_run_is_off_when_state_file_is_missing(tmp_path: Path) -> None:
    assert read_engine_state(tmp_path / "engine-state.json") == ENGINE_OFF


def test_state_file_round_trip_and_corrupt_file_falls_back_off(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine-state.json"

    write_engine_state(ENGINE_ON, path)
    assert read_engine_state(path) == ENGINE_ON

    write_engine_state(ENGINE_OFF, path)
    assert read_engine_state(path) == ENGINE_OFF

    # 파일이 깨져도 서버는 OFF로 시작한다.
    path.write_text("{깨진 json", encoding="utf-8")
    assert read_engine_state(path) == ENGINE_OFF

    with pytest.raises(ValueError):
        write_engine_state("maybe", path)


# ---------------------------------------------------------------------------
# 소켓·상태 파일 수명 주기


async def test_on_opens_udp_and_off_closes_it(tmp_path: Path) -> None:
    port = _free_udp_port()
    state_path = tmp_path / "engine-state.json"
    engine = _engine(tmp_path, port)

    assert engine.state == ENGINE_OFF
    assert engine.udp_port is None

    await engine.start()
    try:
        assert engine.state == ENGINE_ON
        assert engine.udp_port == port
        assert engine.store is not None
        assert engine.radio is not None
        assert engine.conn is not None
        # ON인 동안에는 같은 포트에 새 소켓을 바인드할 수 없다.
        assert not _can_bind(port)
        assert read_engine_state(state_path) == ENGINE_ON
    finally:
        await engine.stop()

    assert engine.state == ENGINE_OFF
    # 상태 저장소·무전 큐·지도·DB 참조를 모두 끊는다.
    assert engine.store is None
    assert engine.radio is None
    assert engine.conn is None
    assert engine.udp_port is None
    assert read_engine_state(state_path) == ENGINE_OFF
    # UDP 포트가 실제로 닫혀 같은 포트로 새 소켓을 바인드할 수 있다.
    assert await _wait_until_bindable(port)


async def test_shutdown_keeps_last_saved_state(tmp_path: Path) -> None:
    """서버 종료는 사용자가 끈 것이 아니다. 재시작하면 켜 둔 ON이 이어진다."""
    port = _free_udp_port()
    state_path = tmp_path / "engine-state.json"
    engine = _engine(tmp_path, port)

    await engine.start()
    assert read_engine_state(state_path) == ENGINE_ON

    await engine.aclose()  # systemctl restart 등 서버 종료 경로
    assert engine.state == ENGINE_OFF
    assert read_engine_state(state_path) == ENGINE_ON
    assert await _wait_until_bindable(port)


async def test_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    port = _free_udp_port()
    engine = _engine(tmp_path, port)

    await engine.start()
    await engine.start()  # 같은 상태로 다시 요청해도 성공하고 소켓은 하나다.
    assert engine.state == ENGINE_ON
    assert engine.udp_port == port

    await engine.stop()
    await engine.stop()
    assert engine.state == ENGINE_OFF
    assert await _wait_until_bindable(port)

    assert await engine.set_state(ENGINE_ON) == ENGINE_ON
    assert await engine.set_state(ENGINE_ON) == ENGINE_ON
    await engine.stop()
    assert engine.state == ENGINE_OFF

    with pytest.raises(ValueError):
        await engine.set_state("maybe")


async def test_on_off_cycles_do_not_leak_tasks_or_sockets(tmp_path: Path) -> None:
    port = _free_udp_port()
    engine = _engine(tmp_path, port)
    baseline = len(_pending_tasks())

    for _ in range(3):
        await engine.start()
        assert not _can_bind(port)
        await engine.stop()
        await asyncio.sleep(0.05)
        assert engine.state == ENGINE_OFF
        assert _pending_tasks() == [], "OFF인데 엔진 태스크가 남아 있습니다"
        assert await _wait_until_bindable(port)

    assert len(_pending_tasks()) == baseline
