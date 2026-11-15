"""리플레이 전용 메모리 무전 로그·싱크(설계 4.6).

웹 대시보드 "다시 보기"는 실서버의 `Engine`·`f1re.db`·Qwen TTS와 완전히
분리된 메모리 전용 런타임으로 돈다. `RacePipeline`·`AlertRules`가 만든 무전은
`RadioQueue`를 거쳐 이 모듈의 `MemoryRadioSink`가 꺼내고, `MemoryRadioLog`가
SQLite `radio_messages` 표와 같은 모양의 딕셔너리로 버퍼에 쌓는다. 그래서
리플레이 중에도 대시보드의 무전 기록·세션 리포트가 실시간과 같은 화면을
유지하면서, DB 쓰기와 TTS API 호출은 전혀 일어나지 않는다.

시계는 리플레이 패킷의 `recv_ns`를 넘겨받는 가상 시계(`clock_ns`)다. 큐의
TTL·우선순위 판정과 기록 시각이 모두 이 시계를 쓰므로, 배속·시크로 시간을
건너뛰어도 실시간과 같은 규칙이 유지된다(계획 Review Focus 5).

`MemoryRadioSink`는 `RacePipeline`이 기대하는 최소 무전 서비스이기도 하다.
`queue`(파이프라인이 무전을 넣는 큐)와 `cache=None`(TTS 프리페치 금지),
`status()`(기존 `state_payload()` 모양)를 제공한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from f1re.radio.queue import RadioMessage, RadioQueue

logger = logging.getLogger(__name__)

# 싱크가 큐를 확인하는 주기(초). 실제 음성 재생이 없어 짧게 둔다.
DRAIN_INTERVAL_S = 0.02

# 리플레이에는 음성 합성·무전 페이지가 없다. `state_payload()`의 `status`와
# 같은 모양을 유지해 화면이 실시간과 같은 컴포넌트를 쓴다.
_REPLAY_VOICE_STATUS: dict[str, Any] = {
    "state": "offline",
    "label": "음성 오프라인",
    "reason": None,
}
_REPLAY_RADIO_PAGE_STATUS: dict[str, Any] = {
    "state": "disconnected",
    "label": "무전 페이지 미연결",
    "connected": False,
    "clients": 0,
}


class MemoryRadioLog:
    """리플레이 무전을 메모리에만 쌓는 기록 버퍼.

    행은 SQLite `radio_messages` 표(마이그레이션 002 + 006)와 같은 열 이름을
    가진 딕셔너리다. 기존 `radio_log_payload()`·`report_payload()`가
    `RadioLog.recent()` 행을 다루는 방식 그대로 읽을 수 있다.
    """

    def __init__(self, *, session_uid: int | None = None) -> None:
        self._session_uid = session_uid
        self._rows: list[dict[str, Any]] = []
        self._next_id = 1
        self._lock = threading.Lock()

    # --- 쓰기 -------------------------------------------------------------

    def append(self, message: RadioMessage, *, created_at_ns: int) -> None:
        """무전 한 줄을 '재생됨'으로 버퍼에 넣는다(DB·TTS 없음).

        `created_at_ns`는 가상 시계의 재생 시작 시각이다. 싱크는 큐에서 꺼낸
        직후의 가상 시각을 넘겨, 결정 시각(`message.created_ns`)과의 차이를
        `latency_ms`로 남긴다.
        """
        with self._lock:
            row = self._build_row(
                message,
                created_at_ns=created_at_ns,
                played=True,
                dropped_reason=None,
            )
            row["id"] = self._next_id
            self._next_id += 1
            self._rows.append(row)

    def append_dropped(
        self,
        message: RadioMessage,
        *,
        created_at_ns: int,
        reason: str,
    ) -> None:
        """큐가 재생하지 못하고 버린 무전을 `played=False`로 남긴다.

        실서버 `RadioLog`가 `dropped_reason`을 남기는 것과 같은 규칙이다
        (예: 코칭 TTL 5초 만료 `"expired"`, 새 코칭에 밀림 `"superseded"`).
        """
        with self._lock:
            row = self._build_row(
                message,
                created_at_ns=created_at_ns,
                played=False,
                dropped_reason=reason,
            )
            row["id"] = self._next_id
            self._next_id += 1
            self._rows.append(row)

    def clear(self) -> None:
        """버퍼를 비우고 행 ID를 처음부터 다시 매긴다(시크·새 리플레이)."""
        with self._lock:
            self._rows.clear()
            self._next_id = 1

    def set_session_uid(self, session_uid: int | None) -> None:
        """이후에 들어오는 무전에 붙일 세션 UID. 이미 쌓인 행은 그대로 둔다."""
        with self._lock:
            self._session_uid = session_uid

    # --- 읽기 -------------------------------------------------------------

    @property
    def session_uid(self) -> int | None:
        with self._lock:
            return self._session_uid

    def list_messages(self, limit: int | None = None) -> list[dict[str, Any]]:
        """최근 무전부터 돌려준다(`RadioLog.recent()`와 같은 순서).

        `limit`을 주면 최신 N개만 돌려준다. 돌려주는 행은 깊은 사본이라
        화면 쪽에서 고쳐도 버퍼가 바뀌지 않는다.
        """
        with self._lock:
            if limit is None:
                rows = self._rows
            else:
                count = max(0, int(limit))
                rows = self._rows[-count:] if count else []
            return [copy.deepcopy(row) for row in reversed(rows)]

    def report_source(self) -> list[dict[str, Any]]:
        """세션 리포트가 근거로 삼을 무전 행 전부(최신부터).

        `report_api`가 `RadioLog.recent()` 행을 다루는 모양과 같아, 리플레이
        리포트도 실시간과 같은 계산을 그대로 쓸 수 있다.
        """
        return self.list_messages()

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    # --- 내부 -------------------------------------------------------------

    def _build_row(
        self,
        message: RadioMessage,
        *,
        created_at_ns: int,
        played: bool,
        dropped_reason: str | None,
    ) -> dict[str, Any]:
        """행 하나를 만든다. 호출자가 `_lock`을 잡은 채 부른다."""
        recorded_at_ns = int(created_at_ns)
        created_ns = int(message.created_ns or 0)
        latency_ms = (
            max(0, (recorded_at_ns - created_ns) // 1_000_000)
            if created_ns > 0
            else None
        )
        return {
            "id": 0,  # 호출자가 채운다.
            "session_uid": self._session_uid,
            "recorded_at_ns": recorded_at_ns,
            "lap": message.lap,
            "corner": message.corner,
            "kind": str(message.kind),
            "priority": int(message.priority),
            "text": message.text,
            "evidence": copy.deepcopy(dict(message.evidence)),
            "played": played,
            "latency_ms": latency_ms,
            "dropped_reason": dropped_reason,
        }


class MemoryRadioSink:
    """`RadioQueue`의 무전을 꺼내 `MemoryRadioLog`에만 적재하는 재생기.

    실서버의 `RadioService`와 달리 Qwen TTS·음성 조각·DB 기록이 없다. 무전은
    큐에서 나오는 즉시 '재생된 것'으로 버퍼에 남는다. `start()`는 백그라운드
    적재 루프를 띄우고, `aclose()`는 남은 무전을 한 번 더 적재한 뒤 루프를
    멈춘다. 시크처럼 지연 없이 패킷을 고속 재생하는 경로는 `drain()`을 직접
    불러 백그라운드 루프와 무관하게 즉시 반영할 수 있다.
    """

    def __init__(
        self,
        queue: RadioQueue,
        log: MemoryRadioLog,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        idle_s: float = DRAIN_INTERVAL_S,
    ) -> None:
        self._queue = queue
        self._log = log
        self._clock_ns = clock_ns
        self._sleep = sleep
        self._idle_s = idle_s
        self._tasks: list[asyncio.Task[None]] = []
        # 큐가 TTL 만료로 버린 무전도 실시간처럼 이유와 함께 남긴다.
        self._queue.set_drop_handler(self._record_dropped)

    # --- 수명 주기 --------------------------------------------------------

    def start(self) -> None:
        """백그라운드 적재 루프를 띄운다. 이미 돌고 있으면 아무것도 하지 않는다."""
        if self.running:
            return
        self._tasks = [
            asyncio.create_task(self._run(), name="f1re-replay-radio")
        ]

    async def aclose(self) -> None:
        """남은 무전을 적재하고 루프를 멈춘다(여러 번 불러도 안전)."""
        # 멈추기 전에 큐에 남은 무전을 잃지 않는다.
        self.drain()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    # --- 적재 -------------------------------------------------------------

    def drain(self) -> int:
        """지금 큐에 있는 무전을 우선순위 순서로 모두 꺼내 적재한다.

        가상 시계를 그대로 넘기므로 TTL·우선순위 판정이 리플레이 시간과
        일치한다. 꺼내 적재한 개수를 돌려준다(버려진 무전은 별도 기록).
        """
        recorded = 0
        while True:
            now_ns = self._clock_ns()
            message = self._queue.next(now_ns)
            if message is None:
                return recorded
            self._queue.complete()
            self._log.append(message, created_at_ns=now_ns)
            recorded += 1

    async def _run(self) -> None:
        while True:
            try:
                self.drain()
            except Exception:  # 기록 버그 하나로 리플레이가 멈추지 않게 한다.
                logger.exception("리플레이 무전을 기록하지 못했습니다")
            await self._sleep(self._idle_s)

    def _record_dropped(self, message: RadioMessage, reason: str) -> None:
        """큐가 버린 무전을 가상 시계 시각으로 기록한다."""
        self._log.append_dropped(
            message,
            created_at_ns=self._clock_ns(),
            reason=reason,
        )

    # --- `RacePipeline`이 기대하는 무전 서비스 인터페이스 ------------------

    @property
    def queue(self) -> RadioQueue:
        return self._queue

    @property
    def log(self) -> MemoryRadioLog:
        return self._log

    @property
    def cache(self) -> None:
        """TTS 캐시 없음. `RacePipeline`이 문장 프리페치(Qwen 호출)를 건너뛴다."""
        return None

    def status(self) -> dict[str, Any]:
        """`state_payload()`의 `status` 칸. 리플레이에는 음성·무전 페이지가 없다."""
        return {
            "voice": dict(_REPLAY_VOICE_STATUS),
            "radio_page": dict(_REPLAY_RADIO_PAGE_STATUS),
        }
