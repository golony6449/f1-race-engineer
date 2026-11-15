"""리플레이용 메모리 무전 로그·싱크: DB·TTS 없이 큐의 무전을 기록한다.

공용 conftest를 만들지 않는 규칙에 따라 가상 시계·도우미를 이 파일 안에 둔다.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from f1re.radio.queue import Priority, RadioKind, RadioMessage, RadioQueue
from f1re.udp.replay_radio import MemoryRadioLog, MemoryRadioSink
from f1re.web.report_api import _message_payload

# SQLite `radio_messages` 표(마이그레이션 002 + 006)와 같은 열 이름.
ROW_KEYS = {
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
}


# ---------------------------------------------------------------------------
# 도우미


class _Clock:
    """리플레이 패킷 `recv_ns`를 흉내 내는 가상 시계."""

    def __init__(self, now_ns: int = 0) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1_000_000_000)


def _message(
    text: str = "3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아.",
    *,
    kind: str = RadioKind.COACHING,
    priority: Priority = Priority.COACHING,
    created_ns: int = 0,
    expires_ns: int = 10**30,
    evidence: dict[str, Any] | None = None,
    lap: int | None = 3,
    corner: int | None = 3,
) -> RadioMessage:
    return RadioMessage(
        kind=kind,
        priority=priority,
        text=text,
        expires_ns=expires_ns,
        evidence={"meters": 5.2} if evidence is None else evidence,
        created_ns=created_ns,
        lap=lap,
        corner=corner,
    )


async def _wait_for(
    predicate: Callable[[], bool], timeout_s: float = 2.0
) -> None:
    """조건이 참이 될 때까지 이벤트 루프를 양보하며 기다린다."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("조건이 제한 시간 안에 만족되지 않았습니다")


# ---------------------------------------------------------------------------
# MemoryRadioSink


async def test_memory_radio_sink_records_without_tts_or_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """큐에 넣은 무전이 기록되며 DB 연결·네트워크 호출·파일이 생기지 않는다."""
    external_calls: list[str] = []

    def fail_connect(*args: Any, **kwargs: Any) -> Any:
        external_calls.append("sqlite3.connect")
        raise AssertionError("리플레이 중에는 DB를 열면 안 됩니다")

    def fail_client(*args: Any, **kwargs: Any) -> Any:
        external_calls.append("httpx.AsyncClient")
        raise AssertionError("리플레이 중에는 네트워크(Qwen TTS)를 쓰면 안 됩니다")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    monkeypatch.setattr(httpx, "AsyncClient", fail_client)
    # DB 파일이 작업 폴더에 생기는지도 본다.
    monkeypatch.chdir(tmp_path)

    clock = _Clock(1_000)
    queue = RadioQueue(clock=clock)
    log = MemoryRadioLog(session_uid=99)
    sink = MemoryRadioSink(queue, log, clock_ns=clock)

    sink.start()
    try:
        queue.push(
            _message(
                "타이어 온도가 높아. 한 랩 천천히 가.",
                kind=RadioKind.SAFETY,
                priority=Priority.SAFETY,
                evidence={"tyre": "FL", "surface_temp_c": 118},
            )
        )
        clock.advance(0.001)
        queue.push(_message("3번 코너에서 5미터 늦게 브레이크."))
        await _wait_for(lambda: len(log) >= 2)
    finally:
        await sink.aclose()

    rows = log.list_messages()
    # 화면은 최신 무전부터 보여 준다(`RadioLog.recent()`와 같은 순서).
    assert [row["text"] for row in rows] == [
        "3번 코너에서 5미터 늦게 브레이크.",
        "타이어 온도가 높아. 한 랩 천천히 가.",
    ]
    assert rows[0]["evidence"] == {"meters": 5.2}
    assert rows[1]["evidence"] == {"tyre": "FL", "surface_temp_c": 118}
    assert all(row["played"] is True for row in rows)
    # 리포트도 같은 버퍼를 근거로 삼는다.
    assert log.report_source() == rows
    # 외부 호출도, 임시 폴더의 파일도 없어야 한다.
    assert external_calls == []
    assert list(tmp_path.iterdir()) == []


def test_drain_respects_queue_priority_and_marks_played() -> None:
    """싱크는 실제 재생기처럼 우선순위 순서로 꺼내 재생됨으로 남긴다."""
    clock = _Clock()
    queue = RadioQueue(clock=clock)
    log = MemoryRadioLog()
    sink = MemoryRadioSink(queue, log, clock_ns=clock)

    queue.push(
        _message(
            "개선 알림",
            kind=RadioKind.IMPROVEMENT,
            priority=Priority.IMPROVEMENT,
        )
    )
    queue.push(
        _message("안전 경고", kind=RadioKind.SAFETY, priority=Priority.SAFETY)
    )

    assert sink.drain() == 2
    # 실제로 꺼낸 순서(안전 → 개선)와 화면 순서(최신 → 과거)를 함께 본다.
    assert [row["text"] for row in log.list_messages()] == [
        "개선 알림",
        "안전 경고",
    ]
    assert all(row["played"] is True for row in log.list_messages())


def test_expired_messages_are_recorded_as_unplayed_drops() -> None:
    """가상 시계가 TTL을 넘기면 큐가 버리고, 싱크가 이유와 함께 기록한다."""
    clock = _Clock()
    queue = RadioQueue(clock=clock)
    log = MemoryRadioLog()
    sink = MemoryRadioSink(queue, log, clock_ns=clock)

    message = _message("늦은 코칭", expires_ns=5_000_000_000)
    assert queue.push(message) is True

    clock.advance(6.0)
    assert sink.drain() == 0

    rows = log.list_messages()
    assert len(rows) == 1
    assert rows[0]["text"] == "늦은 코칭"
    assert rows[0]["played"] is False
    assert rows[0]["dropped_reason"] == "expired"
    assert rows[0]["recorded_at_ns"] == 6_000_000_000


async def test_start_is_idempotent_and_aclose_stops_recording() -> None:
    """`start()`를 두 번 불러도 루프는 하나이고, 닫은 뒤에는 기록하지 않는다."""
    clock = _Clock()
    queue = RadioQueue(clock=clock)
    log = MemoryRadioLog()
    sink = MemoryRadioSink(queue, log, clock_ns=clock, idle_s=0.001)

    sink.start()
    sink.start()
    assert sink.running is True
    queue.push(_message("첫 줄"))
    await _wait_for(lambda: len(log) == 1)

    await sink.aclose()
    assert sink.running is False

    queue.push(_message("닫힌 뒤"))
    await asyncio.sleep(0.01)
    assert len(log) == 1


async def test_aclose_records_messages_still_waiting_in_queue() -> None:
    """닫을 때 큐에 남은 무전도 잃지 않고 버퍼에 남긴다."""
    clock = _Clock()
    queue = RadioQueue(clock=clock)
    log = MemoryRadioLog()
    sink = MemoryRadioSink(queue, log, clock_ns=clock)

    queue.push(_message("남은 줄"))
    await sink.aclose()

    assert [row["text"] for row in log.list_messages()] == ["남은 줄"]


def test_sink_exposes_replay_radio_interface_without_tts() -> None:
    """`RacePipeline`이 기대하는 `queue`·`cache`를 제공하고 TTS를 붙이지 않는다."""
    queue = RadioQueue()
    log = MemoryRadioLog()
    sink = MemoryRadioSink(queue, log)

    assert sink.queue is queue
    assert sink.log is log
    assert sink.cache is None
    status = sink.status()
    assert status["voice"]["state"] == "offline"
    assert status["radio_page"]["connected"] is False
    assert status["radio_page"]["clients"] == 0


# ---------------------------------------------------------------------------
# MemoryRadioLog


def test_memory_radio_log_returns_sqlite_compatible_rows_newest_first() -> None:
    """행 모양이 `RadioLog.recent()`와 같아 기존 무전 기록·리포트 화면이 쓴다."""
    log = MemoryRadioLog(session_uid=42)
    first = _message(
        "첫 무전",
        created_ns=50,
        evidence={"loss_ms": 210, "corner": 3},
    )
    second = _message(
        "둘째 무전",
        kind=RadioKind.SAFETY,
        priority=Priority.SAFETY,
        created_ns=900_000,
        evidence={"tyres": ["soft", "soft"]},
        lap=4,
        corner=None,
    )

    log.append(first, created_at_ns=1_000_050)
    log.append(second, created_at_ns=2_000_000)

    rows = log.list_messages()
    assert [row["text"] for row in rows] == ["둘째 무전", "첫 무전"]
    assert set(rows[0]) == ROW_KEYS
    assert set(rows[1]) == ROW_KEYS
    assert rows[0]["id"] == 2
    assert rows[1]["id"] == 1
    assert rows[1]["session_uid"] == 42
    assert rows[1]["recorded_at_ns"] == 1_000_050
    assert rows[1]["lap"] == 3
    assert rows[1]["corner"] == 3
    assert rows[1]["kind"] == "coaching"
    assert rows[1]["priority"] == int(Priority.COACHING)
    # 결정(50ns)에서 재생 시작(1,000,050ns)까지 1ms.
    assert rows[1]["latency_ms"] == 1
    assert rows[1]["evidence"] == {"loss_ms": 210, "corner": 3}
    assert rows[1]["played"] is True
    assert rows[1]["dropped_reason"] is None
    # 기존 화면 변환기가 그대로 받는다.
    payload = _message_payload(rows[1])
    assert payload["kind_label"] == "코너 코칭"
    assert payload["recorded_at_ns"] == 1_000_050

    # 꺼낸 사본을 고쳐도 버퍼는 그대로다.
    rows[1]["text"] = "바뀜"
    rows[1]["evidence"]["loss_ms"] = 0
    assert log.list_messages()[1]["text"] == "첫 무전"
    assert log.list_messages()[1]["evidence"] == {"loss_ms": 210, "corner": 3}


def test_report_source_returns_same_rows_as_list_messages() -> None:
    log = MemoryRadioLog()
    log.append(_message("한 줄"), created_at_ns=10)

    assert log.report_source() == log.list_messages()
    assert log.report_source()[0]["text"] == "한 줄"


def test_list_messages_limit_returns_newest_rows() -> None:
    log = MemoryRadioLog()
    for index in range(3):
        log.append(_message(f"m{index}"), created_at_ns=index)

    assert [row["text"] for row in log.list_messages(limit=2)] == ["m2", "m1"]
    assert log.list_messages(limit=0) == []
    assert len(log.list_messages()) == 3


def test_clear_resets_messages_and_ids() -> None:
    log = MemoryRadioLog(session_uid=1)
    log.append(_message("한 줄"), created_at_ns=10)

    log.clear()
    assert log.list_messages() == []
    assert log.report_source() == []
    assert len(log) == 0

    log.append(_message("새 줄"), created_at_ns=20)
    rows = log.list_messages()
    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["text"] == "새 줄"


def test_session_uid_applies_to_later_messages() -> None:
    log = MemoryRadioLog()
    log.append(_message("세션 전"), created_at_ns=1)

    log.set_session_uid(7)
    log.append(_message("세션 후"), created_at_ns=2)
    assert log.list_messages()[0]["session_uid"] == 7
    assert log.list_messages()[1]["session_uid"] is None

    log.set_session_uid(None)
    log.append(_message("세션 없음"), created_at_ns=3)
    assert log.list_messages()[0]["session_uid"] is None
