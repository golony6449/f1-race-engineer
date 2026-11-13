"""무전 기록: 나간 무전 전부를 SQLite에 남긴다.

설계 6.7은 무전마다 시각, 랩, 코너, 종류, 문장, 근거 수치, 실제 재생 여부와
지연을 남기라고 한다. 재생에 실패한 무전도 `played=False`로 남겨, 세션 뒤
화면(작업 20)에서 왜 말하지 못했는지 볼 수 있게 한다. 큐가 유효 시간이 지나
버린 무전은 `dropped_reason="expired"`로 남긴다(SCRUM-81).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from f1re.radio.queue import RadioMessage

# `recent()`에서 세션 조건을 생략했을 때와 "전체"를 구분하는 표식.
_ALL_SESSIONS = object()


class RadioLog:
    """무전 기록 쓰기·조회. DB 연결은 호출자가 열고 닫는다."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        session_uid: int | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._conn = conn
        self._session_uid = session_uid
        self._clock = clock

    def record(
        self,
        msg: RadioMessage,
        played: bool = False,
        latency_ms: int | None = None,
        dropped_reason: str | None = None,
    ) -> int:
        """무전 한 줄을 기록하고 행 ID를 돌려준다.

        `latency_ms`는 무전 결정부터 실제 재생 시작까지의 지연이다. 재생하지
        못했으면 `played=False`로 두고 `latency_ms=None`으로 남긴다.
        `dropped_reason`은 재생되지 않은 이유다(예: 큐에서 유효 시간 만료).
        """
        if latency_ms is not None and latency_ms < 0:
            raise ValueError("latency_ms는 0 이상이어야 합니다")
        recorded_at_ns = msg.created_ns if msg.created_ns else self._clock()
        evidence = json.dumps(
            msg.evidence, ensure_ascii=False, sort_keys=True
        )
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO radio_messages (
                    session_uid, recorded_at_ns, lap, corner, kind, priority,
                    text, evidence, played, latency_ms, dropped_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._session_uid,
                    int(recorded_at_ns),
                    msg.lap,
                    msg.corner,
                    msg.kind,
                    int(msg.priority),
                    msg.text,
                    evidence,
                    1 if played else 0,
                    latency_ms,
                    dropped_reason,
                ),
            )
        return int(cursor.lastrowid or 0)

    def recent(
        self,
        limit: int = 50,
        *,
        session_uid: int | None | object = _ALL_SESSIONS,
        all_sessions: bool = False,
    ) -> list[dict[str, Any]]:
        """최근 무전을 새것부터 돌려준다.

        `session_uid`를 주면 그 세션만, 생략하면 이 기록기의 세션(있으면)만,
        `all_sessions=True`면 세션 구분 없이 돌려준다.
        """
        if limit <= 0:
            return []
        sql = "SELECT * FROM radio_messages"
        params: list[Any] = []
        if not all_sessions:
            target = (
                self._session_uid if session_uid is _ALL_SESSIONS else session_uid
            )
            if target is not None:
                sql += " WHERE session_uid = ?"
                params.append(int(target))
        sql += " ORDER BY recorded_at_ns DESC, id DESC LIMIT ?"
        params.append(min(int(limit), 1000))
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count(self, *, session_uid: int | None | object = _ALL_SESSIONS) -> int:
        """기록 행 수."""
        target = self._session_uid if session_uid is _ALL_SESSIONS else session_uid
        if target is None:
            row = self._conn.execute("SELECT COUNT(*) FROM radio_messages").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM radio_messages WHERE session_uid = ?",
                (int(target),),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        raw_evidence = row["evidence"]
        try:
            evidence = json.loads(raw_evidence) if raw_evidence else {}
        except (TypeError, ValueError):
            evidence = {}
        return {
            "id": int(row["id"]),
            "session_uid": row["session_uid"],
            "recorded_at_ns": int(row["recorded_at_ns"]),
            "lap": row["lap"],
            "corner": row["corner"],
            "kind": row["kind"],
            "priority": int(row["priority"]),
            "text": row["text"],
            "evidence": evidence,
            "played": bool(row["played"]),
            "latency_ms": row["latency_ms"],
            "dropped_reason": row["dropped_reason"],
        }
