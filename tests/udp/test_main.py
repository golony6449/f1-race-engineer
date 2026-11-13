"""`f1re serve --replay`: 같은 파이프라인을 돌리고 세션을 SQLite에 남긴다."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from f1re.main import main
from f1re.udp.packer import pack_packet
from f1re.udp.recording import RecordingWriter
from f1re.udp.spec import PACKET_FORMAT, Header, PacketId


def _event_datagram(code: str, *, uid: int, frame: int) -> bytes:
    header = Header(
        packet_format=PACKET_FORMAT,
        game_year=25,
        game_major=1,
        game_minor=0,
        packet_version=1,
        packet_id=int(PacketId.EVENT),
        session_uid=uid,
        session_time=frame / 60.0,
        frame_id=frame,
        overall_frame_id=frame,
        player_car_index=0,
        secondary_player_car_index=255,
    )
    body: dict[str, Any] = {
        "event_string_code": code,
        "event_details": {},
    }
    return pack_packet(header, body)


def test_help_lists_serve(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    assert "serve" in capsys.readouterr().out


def test_serve_engine_option_selects_start_state() -> None:
    from f1re.main import _parse_args

    assert _parse_args(["serve"]).engine is None
    assert _parse_args(["serve", "--engine", "on"]).engine == "on"
    assert _parse_args(["serve", "--engine", "off"]).engine == "off"
    with pytest.raises(SystemExit):
        _parse_args(["serve", "--engine", "maybe"])


def test_serve_replay_records_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_path = tmp_path / "session.f1rec"
    with RecordingWriter(recording_path) as writer:
        writer.write(1_000_000_000, _event_datagram("SSTA", uid=77, frame=1))
        writer.write(2_000_000_000, _event_datagram("SEND", uid=77, frame=2))

    db_path = tmp_path / "f1re.db"
    code = main(
        [
            "serve",
            "--replay",
            str(recording_path),
            "--db",
            str(db_path),
            "--speed",
            "1000000",
        ]
    )

    assert code == 0
    assert "리플레이 완료" in capsys.readouterr().out

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_uid = ?", (77,)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["started_at_ns"] == 1_000_000_000
    assert row["ended_at_ns"] == 2_000_000_000
    assert row["packets_received"] == 2
    assert row["unsupported_count"] == 0
