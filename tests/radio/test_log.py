"""무전 기록: DB 마이그레이션, 기록 필드, 최근 목록."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from f1re import db
from f1re.radio.log import RadioLog
from f1re.radio.queue import Priority, RadioKind, RadioMessage


def _message(
    text: str = "3번, 브레이크 늦음. 5미터.",
    *,
    kind: str = RadioKind.COACHING,
    created_ns: int = 1_000,
    expires_ns: int = 10**30,
    evidence: dict[str, object] | None = None,
    lap: int | None = 3,
    corner: int | None = 3,
) -> RadioMessage:
    return RadioMessage(
        kind=kind,
        priority=Priority.COACHING,
        text=text,
        expires_ns=expires_ns,
        evidence=evidence if evidence is not None else {"meters": 5.2},
        created_ns=created_ns,
        lap=lap,
        corner=corner,
    )


def _open(tmp_path: Path) -> sqlite3.Connection:
    return db.open_db(tmp_path / "f1re.db")


def test_migration_adds_radio_messages_without_touching_sessions(
    tmp_path: Path,
) -> None:
    conn = _open(tmp_path)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        assert db.SCHEMA_VERSION >= 2
        versions = [
            row["version"]
            for row in conn.execute("SELECT version FROM schema_version ORDER BY version")
        ]
        assert versions == list(range(1, db.SCHEMA_VERSION + 1))

        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"sessions", "radio_messages"} <= tables

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(radio_messages)")
        }
        assert {
            "id",
            "session_uid",
            "recorded_at_ns",
            "lap",
            "corner",
            "kind",
            "priority",
            "text",
            "evidence",
            "played",
            "latency_ms",
            "dropped_reason",
        } <= columns

        session_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        assert "session_uid" in session_columns
    finally:
        conn.close()


def test_record_stores_every_field(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        log = RadioLog(conn, session_uid=42)
        message = _message(
            evidence={"meters": 5.2, "brake_start_m": 510.0},
            lap=3,
            corner=3,
        )

        row_id = log.record(message, played=True, latency_ms=87)

        assert row_id > 0
        rows = log.recent()
        assert len(rows) == 1
        row = rows[0]
        assert row["session_uid"] == 42
        assert row["recorded_at_ns"] == 1_000
        assert row["lap"] == 3
        assert row["corner"] == 3
        assert row["kind"] == "coaching"
        assert row["priority"] == int(Priority.COACHING)
        assert row["text"] == "3번, 브레이크 늦음. 5미터."
        assert row["evidence"] == {"meters": 5.2, "brake_start_m": 510.0}
        assert row["played"] is True
        assert row["latency_ms"] == 87
    finally:
        conn.close()


def test_record_not_played_and_missing_timestamp_uses_clock(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        log = RadioLog(conn, clock=lambda: 9_999)
        message = _message(created_ns=0, evidence={})

        log.record(message, played=False, latency_ms=None)

        row = log.recent()[0]
        assert row["recorded_at_ns"] == 9_999
        assert row["played"] is False
        assert row["latency_ms"] is None
        assert row["evidence"] == {}
    finally:
        conn.close()


def test_record_expired_message_with_reason(tmp_path: Path) -> None:
    """큐가 만료로 버린 무전도 played=False와 이유로 남는다(SCRUM-81)."""
    conn = _open(tmp_path)
    try:
        log = RadioLog(conn, session_uid=42)
        log.record(
            _message("타이어 4개 마모 88퍼센트.", kind=RadioKind.SAFETY),
            played=False,
            latency_ms=None,
            dropped_reason="expired",
        )
        log.record(_message("정상 재생"), played=True, latency_ms=12)

        rows = {row["text"]: row for row in log.recent()}
        expired = rows["타이어 4개 마모 88퍼센트."]
        played = rows["정상 재생"]
        assert expired["played"] is False
        assert expired["latency_ms"] is None
        assert expired["dropped_reason"] == "expired"
        assert played["played"] is True
        assert played["dropped_reason"] is None
    finally:
        conn.close()


def test_record_rejects_negative_latency(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        log = RadioLog(conn)
        with pytest.raises(ValueError):
            log.record(_message(), played=True, latency_ms=-1)
    finally:
        conn.close()


def test_recent_is_newest_first_and_honours_limit(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        log = RadioLog(conn, session_uid=1)
        for index in range(3):
            log.record(_message(f"문장 {index}", created_ns=100 + index))

        rows = log.recent(limit=2)

        assert [row["text"] for row in rows] == ["문장 2", "문장 1"]
        assert log.recent(limit=0) == []
        assert log.count() == 3
    finally:
        conn.close()


def test_recent_filters_by_session(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        first = RadioLog(conn, session_uid=1)
        second = RadioLog(conn, session_uid=2)
        first.record(_message("세션 1", created_ns=1))
        second.record(_message("세션 2", created_ns=2))

        assert [row["text"] for row in first.recent()] == ["세션 1"]
        assert [row["text"] for row in second.recent()] == ["세션 2"]
        assert [row["text"] for row in first.recent(all_sessions=True)] == [
            "세션 2",
            "세션 1",
        ]
        assert first.recent(session_uid=2)[0]["text"] == "세션 2"
        assert first.count(session_uid=2) == 1
    finally:
        conn.close()


def test_evidence_survives_json_roundtrip_with_korean(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        log = RadioLog(conn)
        evidence = {"코너": "7번", "meters": 12.5, "nested": {"ok": True}}
        log.record(_message(evidence=evidence))

        assert log.recent()[0]["evidence"] == evidence
    finally:
        conn.close()
