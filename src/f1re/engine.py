"""레이스 엔지니어 엔진 ON/OFF 수명 주기(SCRUM-82).

웹 서버(uvicorn)는 항상 떠 있고, 이 모듈의 :class:`Engine`이 게임 중에만 필요한
런타임만 켜고 끈다.

- **ON**: 상태 저장소, SQLite 연결, 무전 서비스(경고·재생 루프, Qwen TTS,
  음성 캐시), UDP 수신 소켓, 세션 진행 DB 반영 루프, 서킷 지도 상태를 만든다.
- **OFF**: UDP 소켓을 닫고 루프를 모두 취소한 뒤, 상태 저장소·무전 큐·메모리
  음성 캐시·서킷 지도·DB 연결의 참조를 끊고 `gc.collect()`를 부른다. 디스크의
  음성 캐시 파일과 DB 파일은 지우지 않는다.

무거운 모듈(numpy를 쓰는 음성 캐시 등)은 `start()` 안에서만 import한다. 웹 앱과
`main`의 시작 경로는 엔진이 꺼져 있어도 이 모듈만 가볍게 읽는다.

마지막 상태는 작은 JSON 파일(`data/engine-state.json`)에 남겨 재시작 뒤에도
유지한다. 파일이 없거나 잘못됐으면 **OFF**로 시작한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from f1re.config import AppConfig, repo_root
from f1re.db import (
    finish_session,
    open_db,
    save_session,
    update_session_progress,
)
from f1re.state.events import Event, EventKind
from f1re.state.store import StateStore

logger = logging.getLogger(__name__)

ENGINE_ON = "on"
ENGINE_OFF = "off"
ENGINE_STATES: tuple[str, str] = (ENGINE_ON, ENGINE_OFF)

# 마지막 ON/OFF 상태를 기억하는 파일. `data/`는 .gitignore에 있다.
ENGINE_STATE_RELPATH = Path("data") / "engine-state.json"

# 실행 중 세션 진행 상황을 DB에 반영하는 주기(초). 예전 `main`의 값과 같다.
PERSIST_INTERVAL_S = 1.0


@dataclass(frozen=True)
class Runtime:
    """엔진이 켜져 있는 동안만 사는 런타임 묶음.

    OFF가 되면 :class:`Engine`이 이 객체의 참조를 끊는다. 웹 라우터는 요청마다
    `Engine.runtime`을 읽으므로, 꺼진 뒤에는 이 객체에 닿을 수 없다.
    """

    store: StateStore
    radio: Any
    conn: sqlite3.Connection | None
    map: Any


def default_engine_state_path() -> Path:
    """상태 파일 기본 경로(`<레포>/data/engine-state.json`)."""
    return repo_root() / ENGINE_STATE_RELPATH


def read_engine_state(path: str | Path | None = None) -> str:
    """저장된 마지막 상태. 파일이 없거나 내용이 잘못됐으면 OFF."""
    state_path = default_engine_state_path() if path is None else Path(path)
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ENGINE_OFF
    state = raw.get("state") if isinstance(raw, dict) else None
    return state if state in ENGINE_STATES else ENGINE_OFF


def write_engine_state(state: str, path: str | Path | None = None) -> None:
    """마지막 상태를 파일에 남긴다. 실패해도 서버는 계속 돈다."""
    if state not in ENGINE_STATES:
        raise ValueError(f"알 수 없는 엔진 상태: {state!r}")
    state_path = default_engine_state_path() if path is None else Path(path)
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps({"state": state}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, state_path)
    except OSError:
        logger.warning("엔진 상태를 저장하지 못했습니다: %s", state_path)


class Engine:
    """게임 중 런타임을 켜고 끄는 관리자.

    ``runtime``을 넘기면 이미 만들어진 저장소·서비스(테스트·단독 앱)에 붙는
    고정 엔진이 된다. 이 모드는 상태만 바꾸고 자원의 수명은 호출자(app lifespan)가
    맡는다. 넘기지 않으면 이 객체가 런타임 전체를 소유한다.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        runtime: Runtime | None = None,
        initial_state: str = ENGINE_OFF,
        host: str = "0.0.0.0",
        port: int = 20777,
        db_path: str | Path | None = None,
        recorder: Any | None = None,
        recorded_file: Path | None = None,
        state_path: str | Path | None = None,
        persist_state: bool = True,
        radio_factory: Callable[[StateStore, Any], Any] | None = None,
    ) -> None:
        if runtime is None and initial_state not in ENGINE_STATES:
            raise ValueError(f"알 수 없는 엔진 상태: {initial_state!r}")
        self._config = config
        self._host = host
        self._port = port
        self._db_path = db_path
        self._recorder = recorder
        self._recorded_file = recorded_file
        self._state_path = state_path
        self._persist_state = persist_state and runtime is None
        self._radio_factory = radio_factory

        self._managed = runtime is None
        self._runtime = runtime
        # 소유 엔진은 런타임이 실제로 만들어지기 전까지 OFF다. 시작 상태
        # (`initial_state`)가 ON이어도 `start()`가 런타임을 만들어야 ON이 된다
        # (SCRUM-85: 상태만 ON으로 두면 `start()`가 바로 돌아가 UDP가 열리지 않았다).
        self._state = ENGINE_ON if runtime is not None else ENGINE_OFF
        self._initial_state = initial_state
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._off_event = asyncio.Event()
        self._state_lock = threading.RLock()
        self._transition_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # 상태 조회

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def managed(self) -> bool:
        """이 객체가 런타임 전체를 소유하는지(고정 엔진이면 False)."""
        return self._managed

    @property
    def runtime(self) -> Runtime | None:
        with self._state_lock:
            return self._runtime

    @property
    def radio(self) -> Any | None:
        runtime = self.runtime
        return None if runtime is None else runtime.radio

    @property
    def store(self) -> StateStore | None:
        runtime = self.runtime
        return None if runtime is None else runtime.store

    @property
    def conn(self) -> sqlite3.Connection | None:
        runtime = self.runtime
        return None if runtime is None else runtime.conn

    @property
    def map_state(self) -> Any | None:
        runtime = self.runtime
        return None if runtime is None else runtime.map

    @property
    def off_event(self) -> asyncio.Event:
        """다음 OFF 전환 때 설정되는 이벤트. WebSocket들이 이걸 기다린다."""
        with self._state_lock:
            return self._off_event

    @property
    def udp_address(self) -> tuple[str, int] | None:
        """지금 열려 있는 UDP 소켓 주소. 꺼져 있으면 None."""
        transport = self._transport
        if transport is None:
            return None
        sockname = transport.get_extra_info("sockname")
        if not sockname:
            return None
        return str(sockname[0]), int(sockname[1])

    @property
    def udp_port(self) -> int | None:
        address = self.udp_address
        return None if address is None else address[1]

    # ------------------------------------------------------------------
    # 전환

    async def set_state(self, state: str) -> str:
        """ON/OFF로 전환하고 바뀐 상태를 돌려준다. 같은 상태면 그대로 성공."""
        if state not in ENGINE_STATES:
            raise ValueError(f"알 수 없는 엔진 상태: {state!r}")
        if state == ENGINE_ON:
            await self.start()
        else:
            await self.stop()
        return self.state

    async def start(self) -> None:
        """런타임을 만든다. 이미 켜져 있으면 아무것도 하지 않는다."""
        async with self._transition():
            if self.state == ENGINE_ON:
                return
            if not self._managed:
                self._set_fixed_state(ENGINE_ON)
                return
            await self._start_managed()

    async def stop(self) -> None:
        """런타임을 정리한다. 이미 꺼져 있으면 아무것도 하지 않는다.

        사용자가 끈 것이므로 마지막 상태를 OFF로 저장한다.
        """
        await self._stop(persist=True)

    async def _stop(self, *, persist: bool) -> None:
        async with self._transition():
            if self.state == ENGINE_OFF:
                return
            with self._state_lock:
                self._state = ENGINE_OFF
                off_event = self._off_event
                runtime = self._runtime
                transport = self._transport
                tasks = list(self._tasks)
                self._tasks = []
            # 열려 있던 WebSocket들이 engine-off 메시지를 보내고 닫을 틈을 준다.
            off_event.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            if not self._managed:
                # 고정 엔진은 자원 수명을 호출자가 맡는다. 상태만 끈다.
                if persist and self._persist_state:
                    write_engine_state(ENGINE_OFF, self._state_path)
                return

            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
                await asyncio.sleep(0)
            if runtime is not None:
                # 마지막 세션 진행·종료를 DB에 남긴 뒤 연결을 닫는다.
                _safe_persist(runtime, self._recorded_file)
                _safe_finalize(runtime, self._recorded_file)
                with contextlib.suppress(Exception):
                    await runtime.radio.aclose()
                if runtime.conn is not None:
                    with contextlib.suppress(sqlite3.Error):
                        runtime.conn.close()

            with self._state_lock:
                self._runtime = None
                self._transport = None
            if persist and self._persist_state:
                write_engine_state(ENGINE_OFF, self._state_path)
            # 상태 저장소·무전 큐·메모리 음성 캐시·지도 객체를 실제로 회수한다.
            gc.collect()

    async def aclose(self) -> None:
        """서버 종료 경로. 런타임은 정리하되 마지막 저장 상태는 바꾸지 않는다.

        `systemctl restart` 등으로 서버를 다시 띄웠을 때 사용자가 켜 둔 ON 상태가
        그대로 이어지도록, 종료는 "사용자가 끈 것"으로 보지 않는다.
        """
        await self._stop(persist=False)

    # ------------------------------------------------------------------
    # 내부

    def _transition(self) -> asyncio.Lock:
        if self._transition_lock is None:
            self._transition_lock = asyncio.Lock()
        return self._transition_lock

    def _set_fixed_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state
            if state == ENGINE_ON:
                self._off_event = asyncio.Event()

    async def _start_managed(self) -> None:
        store: StateStore | None = None
        conn: sqlite3.Connection | None = None
        radio: Any | None = None
        transport: asyncio.DatagramTransport | None = None
        tasks: list[asyncio.Task[None]] = []
        try:
            store = StateStore()
            conn = open_db(self._db_path)
            radio = self._build_radio(store, conn)
            radio.ensure_started()

            transport, _protocol = await self._start_receiver(store)
            from f1re.web.map_api import TrackMap

            runtime = Runtime(
                store=store,
                radio=radio,
                conn=conn,
                map=TrackMap(conn),
            )
            tasks.append(
                asyncio.create_task(
                    self._persist_loop(runtime), name="f1re-engine-persist"
                )
            )
        except BaseException:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
            if radio is not None:
                with contextlib.suppress(Exception):
                    await radio.aclose()
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            raise

        with self._state_lock:
            self._runtime = runtime
            self._transport = transport
            self._tasks = tasks
            self._off_event = asyncio.Event()
            self._state = ENGINE_ON
        if self._persist_state:
            write_engine_state(ENGINE_ON, self._state_path)

    def _build_radio(self, store: StateStore, conn: sqlite3.Connection) -> Any:
        if self._radio_factory is not None:
            return self._radio_factory(store, conn)
        # numpy를 쓰는 음성 캐시는 ON이 될 때 처음 import한다.
        from f1re.web.radio_api import build_radio_service

        return build_radio_service(store, log_conn=conn)

    async def _start_receiver(
        self, store: StateStore
    ) -> tuple[asyncio.DatagramTransport, Any]:
        from f1re.udp.receiver import start_receiver

        return await start_receiver(
            store, host=self._host, port=self._port, recorder=self._recorder
        )

    async def _persist_loop(self, runtime: Runtime) -> None:
        """세션 진행 상황을 1초마다 DB에 반영한다(예전 `main`의 루프)."""
        while True:
            await asyncio.sleep(PERSIST_INTERVAL_S)
            try:
                persist_events(
                    runtime.conn, runtime.store, self._recorded_file
                )
            except Exception:  # DB 오류 하나로 엔진이 멈추지 않게 한다.
                logger.exception("세션 진행 상황을 DB에 반영하지 못했습니다")


# ---------------------------------------------------------------------------
# 세션 DB 기록(예전 `main.py`에서 옮겼다. 리플레이 경로도 같은 함수를 쓴다)


def persist_events(
    conn: Any, store: StateStore, recorded_file: Path | None
) -> list[Event]:
    """세션 시작·종료 사건을 DB에 반영하고, 꺼낸 사건 목록을 돌려준다."""
    events = store.events.drain()
    for event in events:
        uid = event.data.get("session_uid")
        if uid is None:
            continue
        recv_ns = int(event.data.get("recv_ns") or time.time_ns())
        if event.kind in (EventKind.SESSION_START, EventKind.SESSION_RESTART):
            previous = event.data.get("previous_session_uid")
            if previous is not None:
                finish_session(conn, int(previous), ended_at_ns=recv_ns)
            save_session(
                conn,
                int(uid),
                started_at_ns=recv_ns,
                recorded_file=str(recorded_file) if recorded_file else None,
            )
        elif event.kind is EventKind.SESSION_END:
            finish_session(conn, int(uid), ended_at_ns=recv_ns)
    return events


def finalize_session(
    conn: Any, store: StateStore, recorded_file: Path | None
) -> None:
    """마지막 세션 행에 서킷·세션 종류와 수신 카운터를 채운다."""
    snapshot = store.snapshot()
    diagnostics = snapshot["diagnostics"]
    uid = snapshot["session_uid"]
    if uid is None:
        return
    session_body = snapshot["packets"].get("session") or {}
    save_session(
        conn,
        int(uid),
        started_at_ns=int(diagnostics.get("session_started_ns") or time.time_ns()),
        track_id=_as_int(session_body.get("track_id")),
        session_type=_as_int(session_body.get("session_type")),
        recorded_file=str(recorded_file) if recorded_file else None,
    )
    update_session_progress(
        conn,
        int(uid),
        packets_received=int(diagnostics.get("total_packets") or 0),
        unsupported_count=int(diagnostics.get("unsupported_count") or 0),
    )


def _safe_persist(runtime: Runtime, recorded_file: Path | None) -> None:
    try:
        persist_events(runtime.conn, runtime.store, recorded_file)
    except Exception:  # 종료 경로에서는 기록 실패로 멈추지 않는다.
        logger.exception("세션 사건을 DB에 반영하지 못했습니다")


def _safe_finalize(runtime: Runtime, recorded_file: Path | None) -> None:
    try:
        finalize_session(runtime.conn, runtime.store, recorded_file)
    except Exception:
        logger.exception("세션 요약을 DB에 반영하지 못했습니다")


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


__all__ = [
    "ENGINE_OFF",
    "ENGINE_ON",
    "ENGINE_STATES",
    "ENGINE_STATE_RELPATH",
    "PERSIST_INTERVAL_S",
    "Engine",
    "Runtime",
    "default_engine_state_path",
    "finalize_session",
    "persist_events",
    "read_engine_state",
    "write_engine_state",
]
