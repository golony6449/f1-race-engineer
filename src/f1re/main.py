"""`f1re` 명령줄 진입점.

    f1re serve [--host HOST] [--port PORT] [--record DIR] [--replay FILE]
               [--db PATH] [--speed 배율] [--duration 초]

`--replay`가 있으면 녹화 파일을 같은 파이프라인(녹화 → 파싱 → 상태 → 사건)에
흘려보내고, 없으면 UDP 20777(설정값)에서 게임 패킷을 받는다. 세션 시작·종료는
SQLite `sessions` 표에 남긴다.
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
from f1re.db import (
    finish_session,
    open_db,
    save_session,
    update_session_progress,
)
from f1re.state.events import Event, EventKind
from f1re.state.store import StateStore
from f1re.udp.recording import (
    RecordingWriter,
    new_recording_path,
    replay,
)
from f1re.udp.receiver import process_datagram, start_receiver

# 실행 중 세션 진행 상황을 DB에 반영하는 주기(초).
_PERSIST_INTERVAL_S = 1.0


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

    conn = open_db(args.db)
    writer: RecordingWriter | None = None
    recorded_file: Path | None = None
    if args.record is not None:
        recorded_file = new_recording_path(args.record)
        writer = RecordingWriter(recorded_file)
        print(f"원본 녹화: {recorded_file}")

    store = StateStore()
    try:
        if args.replay is not None:
            return _run_replay(
                store,
                conn,
                path=args.replay,
                speed=args.speed,
                writer=writer,
                recorded_file=recorded_file or args.replay,
            )
        try:
            asyncio.run(
                _run_live(
                    store,
                    conn,
                    host=args.host,
                    port=port,
                    writer=writer,
                    recorded_file=recorded_file,
                    duration_s=args.duration,
                )
            )
        except KeyboardInterrupt:
            print("UDP 수신을 중단했습니다.")
        return 0
    finally:
        if writer is not None:
            writer.close()
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
    events = _persist_events(conn, store, recorded_file)
    _finalize_session(conn, store, recorded_file)

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
    store: StateStore,
    conn: Any,
    *,
    host: str,
    port: int,
    writer: RecordingWriter | None,
    recorded_file: Path | None,
    duration_s: float | None,
) -> None:
    transport, _protocol = await start_receiver(
        store, host=host, port=port, recorder=writer
    )
    sockname = transport.get_extra_info("sockname") or (host, port)
    print(f"UDP 수신 대기: {sockname[0]}:{sockname[1]} (Ctrl+C로 중단)")
    started = time.monotonic()
    try:
        while True:
            interval = _PERSIST_INTERVAL_S
            if duration_s is not None:
                remaining = duration_s - (time.monotonic() - started)
                if remaining <= 0:
                    break
                interval = min(interval, remaining)
            await asyncio.sleep(interval)
            _persist_events(conn, store, recorded_file)
    finally:
        transport.close()
        _persist_events(conn, store, recorded_file)
        _finalize_session(conn, store, recorded_file)
        await asyncio.sleep(0)
    print("UDP 수신을 멈췄습니다.")


def _persist_events(
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


def _finalize_session(
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


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


if __name__ == "__main__":
    raise SystemExit(main())
