"""`f1re` 명령줄 진입점.

    f1re serve [--host HOST] [--port PORT] [--record DIR] [--replay FILE]
               [--db PATH] [--speed 배율] [--duration 초]
               [--web-host HOST] [--web-port PORT] [--no-web]
               [--engine on|off]

`--replay`가 있으면 녹화 파일을 같은 파이프라인(녹화 → 파싱 → 상태 → 사건)에
흘려보내고, 없으면 UDP 20777(설정값)에서 게임 패킷을 받는다. 세션 시작·종료는
SQLite `sessions` 표에 남긴다.

실시간 수신(`--replay` 없음)에서는 같은 프로세스가 uvicorn 웹 대시보드도 띄운다
(기본 `[web]` 설정, `0.0.0.0:8093`). 화면은 서버 API를 읽기만 한다(설계 6.9).
웹 서버는 항상 뜨고, 게임 중에만 필요한 런타임(엔진)은 화면의 설정 모달이나
`POST /api/engine`으로 켜고 끈다(SCRUM-82). 마지막 상태는
`data/engine-state.json`에 남으며, 파일이 없으면 OFF로 시작한다.
`--engine on|off`는 그 시작 상태를 덮어쓴다.
리플레이는 개발·테스트용이라 웹 서버를 띄우지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from pathlib import Path
from typing import Any

from f1re.config import ConfigError, load_config
from f1re.db import open_db
from f1re.engine import (
    ENGINE_OFF,
    ENGINE_ON,
    Engine,
    default_engine_state_path,
    finalize_session,
    persist_events,
    read_engine_state,
    write_engine_state,
)
from f1re.state.events import EventKind
from f1re.state.store import StateStore
from f1re.udp.recording import (
    RecordingWriter,
    new_recording_path,
    replay,
)
from f1re.udp.receiver import process_datagram

# 세션 유지 시간(`--duration`)을 확인하는 주기(초).
_WAIT_INTERVAL_S = 1.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="f1re",
        description="F1 25 UDP 텔레메트리 기반 한국어 AI 레이스 엔지니어",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser(
        "serve", help="UDP 수신 또는 녹화 리플레이를 시작한다"
    )
    serve.add_argument("--host", default="0.0.0.0", help="수신 주소(기본 0.0.0.0)")
    serve.add_argument(
        "--port", type=int, default=None, help="UDP 포트(기본: 설정 파일, 20777)"
    )
    serve.add_argument(
        "--record", type=Path, default=None, metavar="DIR", help="원본 녹화 폴더"
    )
    serve.add_argument(
        "--replay", type=Path, default=None, metavar="FILE", help="리플레이할 .f1rec"
    )
    serve.add_argument(
        "--db", type=Path, default=None, metavar="PATH", help="SQLite 파일 경로"
    )
    serve.add_argument(
        "--speed", type=float, default=1.0, help="리플레이 재생 속도 배율"
    )
    serve.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="이 시간(초) 뒤 종료한다(테스트·시연용)",
    )
    serve.add_argument(
        "--web-host",
        default=None,
        help="웹 대시보드 주소(기본: 설정 [web] host, 0.0.0.0)",
    )
    serve.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="웹 대시보드 포트(기본: 설정 [web] port, 8093). 0이면 빈 포트",
    )
    serve.add_argument(
        "--no-web", action="store_true", help="웹 대시보드를 띄우지 않는다"
    )
    serve.add_argument(
        "--engine",
        choices=(ENGINE_ON, ENGINE_OFF),
        default=None,
        help=(
            "시작할 때의 엔진 상태. 생략하면 data/engine-state.json의 마지막 "
            "상태를 쓰고, 파일이 없으면 off"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    return 2  # pragma: no cover - 서브커맨드는 argparse가 보장한다.


def _serve(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"설정을 읽지 못했습니다: {exc}", file=sys.stderr)
        return 2

    port = config.udp_port if args.port is None else args.port
    if not 0 <= port <= 65535:
        print("--port는 0~65535여야 합니다.", file=sys.stderr)
        return 2
    if not math.isfinite(args.speed) or args.speed <= 0:
        print("--speed는 0보다 큰 유한한 값이어야 합니다.", file=sys.stderr)
        return 2
    if args.duration is not None and (
        not math.isfinite(args.duration) or args.duration < 0
    ):
        print("--duration은 0 이상이어야 합니다.", file=sys.stderr)
        return 2

    web: tuple[str, int] | None = None
    if not args.no_web:
        web_host = config.web.host if args.web_host is None else args.web_host
        web_port = config.web.port if args.web_port is None else args.web_port
        if not 0 <= web_port <= 65535:
            print("--web-port는 0~65535여야 합니다.", file=sys.stderr)
            return 2
        web = (web_host, web_port)

    writer: RecordingWriter | None = None
    recorded_file: Path | None = None
    if args.record is not None:
        recorded_file = new_recording_path(args.record)
        writer = RecordingWriter(recorded_file)
        print(f"원본 녹화: {recorded_file}")

    conn: Any | None = None
    try:
        if args.replay is not None:
            conn = open_db(args.db)
            return _run_replay(
                StateStore(),
                conn,
                path=args.replay,
                speed=args.speed,
                writer=writer,
                recorded_file=recorded_file or args.replay,
            )

        # 마지막 상태를 기억한다. 파일이 없으면(최초) OFF로 시작한다(SCRUM-82).
        state_path = default_engine_state_path()
        start_state = (
            args.engine
            if args.engine is not None
            else read_engine_state(state_path)
        )
        if args.engine is not None:
            # 명령줄 덮어쓰기는 재시작 뒤에도 유지한다.
            write_engine_state(start_state, state_path)
        try:
            asyncio.run(
                _run_live(
                    config,
                    host=args.host,
                    port=port,
                    writer=writer,
                    recorded_file=recorded_file,
                    duration_s=args.duration,
                    web=web,
                    start_state=start_state,
                    db_path=args.db,
                    state_path=state_path,
                )
            )
        except KeyboardInterrupt:
            print("레이스 엔지니어를 중단했습니다.")
        return 0
    finally:
        if writer is not None:
            writer.close()
        if conn is not None:
            conn.close()


def _run_replay(
    store: StateStore,
    conn: Any,
    *,
    path: Path,
    speed: float,
    writer: RecordingWriter | None,
    recorded_file: Path | None,
) -> int:
    print(f"리플레이: {path} ({speed:g}배)")
    count = replay(
        path,
        lambda recv_ns, data: process_datagram(store, data, recv_ns, writer),
        speed=speed,
    )
    events = persist_events(conn, store, recorded_file)
    finalize_session(conn, store, recorded_file)

    snapshot = store.snapshot()
    diagnostics = snapshot["diagnostics"]
    lap_count = sum(1 for event in events if event.kind is EventKind.LAP_COMPLETE)
    print(
        f"리플레이 완료: 데이터그램 {count}개, 랩 완료 {lap_count}회, "
        f"미지원 {diagnostics['unsupported_count']}개, "
        f"오류 {diagnostics['error_count']}개"
    )
    return 0


async def _run_live(
    config: Any,
    *,
    host: str,
    port: int,
    writer: RecordingWriter | None,
    recorded_file: Path | None,
    duration_s: float | None,
    web: tuple[str, int] | None,
    start_state: str,
    db_path: Path | None,
    state_path: Path,
) -> None:
    """웹 서버를 먼저 띄우고, 저장된 시작 상태에 따라 엔진을 켠다."""
    engine = Engine(
        config,
        host=host,
        port=port,
        db_path=db_path,
        recorder=writer,
        recorded_file=recorded_file,
        state_path=state_path,
        initial_state=start_state,
    )
    web_server = _start_web_server(engine, web)
    try:
        if start_state == ENGINE_ON:
            try:
                await engine.start()
            except Exception as exc:
                # 포트 사용 중 등. 웹은 계속 떠서 화면에서 다시 켤 수 있게 한다.
                print(
                    "레이스 엔지니어를 켜지 못했습니다: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                write_engine_state(ENGINE_OFF, state_path)
        if engine.state == ENGINE_ON:
            address = engine.udp_address or (host, port)
            print(f"UDP 수신 대기: {address[0]}:{address[1]} (Ctrl+C로 중단)")
        else:
            print(
                "레이스 엔지니어: 꺼짐 "
                "(대시보드 우측 아래 설정에서 켜면 UDP 수신을 시작합니다)"
            )

        started = time.monotonic()
        while True:
            interval = _WAIT_INTERVAL_S
            if duration_s is not None:
                remaining = duration_s - (time.monotonic() - started)
                if remaining <= 0:
                    break
                interval = min(interval, remaining)
            await asyncio.sleep(interval)
    finally:
        # 종료는 사용자가 끈 것이 아니다. 마지막 저장 상태(ON/OFF)는 그대로 둔다.
        await engine.aclose()
        await _stop_web_server(web_server)
        await asyncio.sleep(0)
    print("레이스 엔지니어를 멈췄습니다.")


def _start_web_server(
    engine: Engine, web: tuple[str, int] | None
) -> tuple[Any, asyncio.Task[None]] | None:
    """같은 이벤트 루프에 uvicorn 대시보드를 띄운다. `web`이 없으면 아무것도 안 한다.

    웹 서버는 엔진과 무관하게 항상 뜬다. 엔진이 켜질 때 무전 서비스와 DB
    연결을 `Engine`이 만들고, 화면 API는 요청마다 그 런타임을 읽는다.
    """
    if web is None:
        return None
    # 웹을 쓰지 않을 때(리플레이)는 fastapi·uvicorn을 불러오지 않는다.
    import uvicorn

    from f1re.web.app import create_app

    host, port = web
    config = uvicorn.Config(
        create_app(engine=engine),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="f1re-web")
    if port:
        print(f"웹 대시보드: http://{_browse_host(host)}:{port}")
    else:
        print(f"웹 대시보드: http://{_browse_host(host)}:<빈 포트>")
    return server, task


async def _stop_web_server(
    web_server: tuple[Any, asyncio.Task[None]] | None,
) -> None:
    if web_server is None:
        return
    server, task = web_server
    server.should_exit = True
    try:
        await task
    except SystemExit:
        # uvicorn은 포트를 잡지 못하면 sys.exit(1)을 낸다. 원인은 uvicorn 로그에 남는다.
        print(
            "웹 대시보드를 시작하지 못했습니다. 포트 사용 여부를 확인하세요.",
            file=sys.stderr,
        )
    except KeyboardInterrupt:
        # Ctrl+C가 이미 전파되는 중이다. 웹 서버 종료는 여기서 끝낸다.
        pass


def _browse_host(host: str) -> str:
    """브라우저에 안내할 주소. 모든 인터페이스 바인딩은 localhost로 보여 준다."""
    return "localhost" if host in {"", "0.0.0.0", "::"} else host


if __name__ == "__main__":
    raise SystemExit(main())
