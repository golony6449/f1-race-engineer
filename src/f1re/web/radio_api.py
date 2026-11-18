"""무전 페이지(`/radio`)와 무전 전달 WebSocket.

게임 PC 브라우저는 소리만 내는 `/radio` 페이지를 띄운다(설계 6.10). 이 모듈이
그 페이지가 붙는 `WS /ws/radio`와, 무전 큐를 실제 음성으로 바꿔 내보내는
`RadioService`를 맡는다. 대시보드 라우터와 충돌하지 않도록 라우터를 따로 두고
`web/app.py`는 등록만 한다.

음성 경로(설계 6.8, Review Focus 3):

- `VoiceCache.get_or_synthesize()`가 캐시 적중이면 파일에서, 미스면 Qwen
  스트리밍으로 PCM 조각을 낸다.
- Qwen API 실패·타임아웃이면 `TTSUnavailable`이 올라오고 상태가 "음성 오프라인"이
  된다. 큐는 멈추지 않고, 캐시에 있는 문장은 계속 재생되며, 합성하지 못한 무전은
  `played=False`로 기록되고 건너뛴다.
- 자막(`caption`)은 첫 음성 조각을 확보한 뒤 오디오와 함께 보낸다(SCRUM-141).
  합성에 실패해 소리를 내지 못하는 무전은 자막도 내보내지 않아, 화면에 자막만
  남고 무음으로 넘어가는 불일치가 없다. `done(played=False)`와 `last` 상태는
  마지막으로 실제 재생한 무전을 그대로 가리킨다.
- 무전 페이지가 연결되지 않았으면 음성을 만들지 않고 큐를 비우며
  `played=False`로 기록한다(상태 "무전 페이지 미연결"). 큐가 막히지 않는다.

음성은 조각을 보낸 대로 브라우저가 이어 붙인다. 서버는 보낸 PCM의 재생
길이(바이트 / 2 / sample_rate)를 누적해 실제 경과가 재생 위치보다 앞서지
않도록 조각 사이에서 기다린다(페이싱, `PACE_MARGIN_S`). 마지막 조각 뒤에도
남은 재생 시간만큼 기다린 뒤 `complete()`·`done`을 보내므로, 다음 무전은
앞 무전이 끝난 뒤에야 나간다(설계 6.7 동시 발화 금지).

WebSocket 메시지(JSON, 서버 → 브라우저):

- ``{"type": "status", "status": {...}, "last": {...}|null}``
- ``{"type": "caption", "text": ..., "kind": ..., "priority": ..., "evidence": {...}}``
- ``{"type": "audio", "pcm": "<base64>", "sample_rate": 24000, "channels": 1,
  "format": "s16le", "seq": 0}``
- ``{"type": "interrupt"}``: 지금 재생 중인 무전을 즉시 멈춘다(더 높은
  우선순위 무전이 와서 끊을 때). 브라우저는 재생 중인 조각을 모두 멈춘다.
- ``{"type": "done", "text": ..., "played": true, "interrupted": false,
  "latency_ms": 12}``

모든 무전은 `RadioLog`에 남긴다(재생 여부·지연 포함). 큐가 유효 시간이 지나
버린 무전도 `dropped_reason`과 함께 남긴다(SCRUM-81). 기록용 DB 연결은
호출자가 넘기고, 세션이 바뀌면 그 세션 UID로 새 기록기를 만든다.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from f1re.alerts.rules import AlertRules, rules_from_config
from f1re.engine import ENGINE_OFF, ENGINE_ON
from f1re.radio.log import RadioLog
from f1re.radio.queue import RadioMessage, RadioQueue
from f1re.state.store import StateStore

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 경고 판정 주기(초). 코너 탈출 판단(100ms)과 같은 수준으로 둔다.
ALERT_POLL_S = 0.1

# 재생할 무전이 없을 때 쉬는 시간(초).
IDLE_S = 0.02

# 무전 페이지가 없을 때 큐를 비우는 주기(초).
NO_PAGE_S = 0.05

# 재생 위치보다 최대 이만큼(초)까지 미리 보낸다. 브라우저가 조각을 이어 붙일
# 여유이자, 서버가 실제 시계보다 앞서 나가지 않게 잡는 최대 선행 폭이다.
PACE_MARGIN_S = 0.3

# 재생을 기다리는 동안 끊김(더 높은 우선순위·페이지 이탈)을 확인하는 주기(초).
PACE_SLICE_S = 0.05

# 클라이언트 한 명에게 쌓아 둘 메시지 수. 느린 브라우저가 서버를 막지 않게 한다.
CLIENT_QUEUE_SIZE = 256

# 음성 상태 자동 확인 주기(초). Qwen 호출은 비용이 들어 게임 중일 때만 한다.
VOICE_CHECK_S = 30.0

# 최근 이 시간(초) 안에 패킷을 받았으면 텔레메트리 수신 중으로 본다.
TELEMETRY_FRESH_S = 5.0

# 오디오 형식. VoiceCache가 내는 PCM과 같다.
SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_FORMAT = "s16le"

STATUS_VOICE_ONLINE = {"state": "online", "label": "음성 온라인"}
STATUS_VOICE_OFFLINE = {"state": "offline", "label": "음성 오프라인"}
STATUS_RADIO_CONNECTED = {"state": "connected", "label": "무전 페이지 연결됨"}
STATUS_RADIO_DISCONNECTED = {"state": "disconnected", "label": "무전 페이지 미연결"}


def default_status() -> dict[str, Any]:
    """서비스가 없거나 엔진이 꺼져 있을 때의 기본 상태(음성 오프라인·미연결).

    음성 캐시가 없으므로 실패 이유(`reason`)도 없다. `status()`와 같은 모양을
    유지해 화면이 두 응답을 같이 다룰 수 있게 한다.
    """
    return {
        "voice": dict(STATUS_VOICE_OFFLINE, reason=None),
        "radio_page": {
            **STATUS_RADIO_DISCONNECTED,
            "connected": False,
            "clients": 0,
        },
    }


@dataclass
class _Client:
    """무전 페이지 하나. 서비스가 이 큐에 넣고 WebSocket이 꺼내 보낸다."""

    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
    )


class RadioService:
    """경고 판정 → 무전 큐 → 음성 스트리밍을 한 곳에서 돌린다."""

    def __init__(
        self,
        store: StateStore,
        *,
        queue: RadioQueue | None = None,
        cache: Any | None = None,
        rules: AlertRules | None = None,
        log: RadioLog | None = None,
        log_conn: Any | None = None,
        tts: Any | None = None,
        clock: Callable[[], int] = time.time_ns,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        alert_poll_s: float = ALERT_POLL_S,
        idle_s: float = IDLE_S,
        voice_check_s: float = VOICE_CHECK_S,
    ) -> None:
        self._store = store
        self._queue = queue if queue is not None else RadioQueue(clock=clock)
        # 큐가 유효 시간이 지나 버린 무전도 기록한다(SCRUM-81).
        self._queue.set_drop_handler(self._record_dropped)
        self._cache = cache
        self._rules = rules if rules is not None else AlertRules(clock=clock)
        self._log = log
        self._log_conn = log_conn
        self._tts = tts
        self._clock = clock
        self._sleep = sleep
        self._alert_poll_s = alert_poll_s
        self._idle_s = idle_s
        self._voice_check_s = voice_check_s
        # 수동·자동 확인이 겹쳐 Qwen을 두 번 부르지 않게 한다(SCRUM-92).
        self._voice_check_lock = asyncio.Lock()

        self._lock = threading.Lock()
        self._clients: dict[int, _Client] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._log_cache: dict[int | None, RadioLog] = {}
        self._session_uid: int | None = None
        self._last_caption: dict[str, Any] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # 수명 주기

    def ensure_started(self) -> None:
        """경고·재생 루프를 띄운다. 이미 돌고 있으면 아무것도 하지 않는다."""
        if any(not task.done() for task in self._tasks):
            return
        self._tasks = [
            asyncio.create_task(self._alert_loop(), name="f1re-radio-alerts"),
            asyncio.create_task(self._play_loop(), name="f1re-radio-play"),
            asyncio.create_task(
                self._voice_check_loop(), name="f1re-radio-voice-check"
            ),
        ]

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._tts is not None:
            with contextlib.suppress(Exception):
                await self._tts.aclose()

    # ------------------------------------------------------------------
    # 상태

    def status(self) -> dict[str, Any]:
        """대시보드·무전 페이지가 함께 쓰는 음성·무전 페이지 상태."""
        voice_online = self._cache is not None and not bool(
            getattr(self._cache, "offline", False)
        )
        voice = dict(
            STATUS_VOICE_ONLINE if voice_online else STATUS_VOICE_OFFLINE
        )
        # 오프라인 이유를 화면에 보여 준다(SCRUM-92). `TTSUnavailable` 메시지에는
        # API 키가 담기지 않는다(`tts/qwen.py`).
        voice["reason"] = (
            None
            if voice_online or self._cache is None
            else getattr(self._cache, "last_error", None)
        )
        clients = self._client_count()
        return {
            "voice": voice,
            "radio_page": {
                **(
                    STATUS_RADIO_CONNECTED
                    if clients
                    else STATUS_RADIO_DISCONNECTED
                ),
                "connected": bool(clients),
                "clients": clients,
            },
        }

    async def check_voice(self) -> dict[str, Any]:
        """음성 상태를 지금 다시 확인한다(SCRUM-92).

        캐시가 없으면 아무것도 부르지 않고 지금 상태를 돌려준다. 캐시가 있으면
        짧은 문장을 한 번 합성하고(`VoiceCache.check`) 결과를 화면에 알린다.
        동시에 여러 번 눌려도 Qwen 호출이 겹치지 않도록 잠금 안에서 확인한다.
        """
        if self._cache is None:
            return self.status()
        async with self._voice_check_lock:
            await self._cache.check()
            self._broadcast_status()
            return self.status()

    @property
    def queue(self) -> RadioQueue:
        return self._queue

    @property
    def cache(self) -> Any | None:
        """음성 캐시(`VoiceCache`). 세션 시작 프리페치가 쓴다(SCRUM-84)."""
        return self._cache

    @property
    def last_caption(self) -> dict[str, Any] | None:
        return self._last_caption

    # ------------------------------------------------------------------
    # 클라이언트

    def add_client(self) -> _Client:
        client = _Client()
        with self._lock:
            self._clients[id(client)] = client
        self._send_to(client, self._status_payload())
        return client

    def remove_client(self, client: _Client) -> None:
        with self._lock:
            self._clients.pop(id(client), None)

    def _client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _has_clients(self) -> bool:
        return self._client_count() > 0

    def _send_to(self, client: _Client, payload: dict[str, Any]) -> None:
        try:
            client.queue.put_nowait(payload)
        except asyncio.QueueFull:
            # 느린 클라이언트는 가장 오래된 것부터 버려 재생이 막히지 않게 한다.
            with contextlib.suppress(asyncio.QueueEmpty):
                client.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                client.queue.put_nowait(payload)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            self._send_to(client, payload)

    def _status_payload(self) -> dict[str, Any]:
        return {
            "type": "status",
            "status": self.status(),
            "last": self._last_caption,
        }

    def _broadcast_status(self) -> None:
        if self._has_clients():
            self._broadcast(self._status_payload())

    # ------------------------------------------------------------------
    # 루프

    async def _alert_loop(self) -> None:
        while True:
            try:
                snapshot = self._store.snapshot()
                uid = snapshot.get("session_uid")
                self._session_uid = uid if isinstance(uid, int) else None
                for message in self._rules.evaluate(snapshot):
                    self._queue.push(message)
            except Exception:  # 판정 버그가 재생 루프를 멈추지 않게 한다.
                logger.exception("위험 경고 판정 중 오류")
            await asyncio.sleep(self._alert_poll_s)

    async def _voice_check_loop(self) -> None:
        """음성이 오프라인일 때 게임 중이면 주기적으로 다시 확인한다(SCRUM-92).

        확인 한 번이 곧 Qwen 호출이라 비용이 든다. 캐시가 오프라인이고 최근
        패킷을 받고 있을 때만 부른다. 오류는 남기고 루프는 계속 돈다.
        """
        while True:
            try:
                if self._voice_check_needed():
                    await self.check_voice()
            except Exception:  # 확인 버그가 자동 복구 루프를 멈추지 않게 한다.
                logger.exception("음성 상태 자동 확인 중 오류")
            # 재생 루프의 페이싱과 섞이지 않도록 실제 시계로 쉰다.
            await asyncio.sleep(self._voice_check_s)

    def _voice_check_needed(self) -> bool:
        """지금 자동 확인을 할 조건인지. 캐시 오프라인 + 텔레메트리 수신 중."""
        cache = self._cache
        if cache is None or not bool(getattr(cache, "offline", False)):
            return False
        return self._telemetry_active()

    def _telemetry_active(self) -> bool:
        """최근 `TELEMETRY_FRESH_S`초 안에 패킷을 받았는지."""
        diagnostics = self._store.snapshot().get("diagnostics") or {}
        last_recv_ns = diagnostics.get("last_recv_ns")
        if not isinstance(last_recv_ns, int):
            return False
        fresh_ns = int(TELEMETRY_FRESH_S * 1_000_000_000)
        return self._clock() - last_recv_ns <= fresh_ns

    async def _play_loop(self) -> None:
        while True:
            try:
                if not self._has_clients():
                    self._discard_without_page()
                    await asyncio.sleep(NO_PAGE_S)
                    continue
                message = self._queue.next(self._clock())
                if message is None:
                    await asyncio.sleep(self._idle_s)
                    continue
                await self._speak(message)
            except asyncio.CancelledError:
                raise
            except Exception:  # 재생 버그 하나로 무전 전체가 멈추지 않게 한다.
                logger.exception("무전 재생 중 오류")
                await asyncio.sleep(self._idle_s)

    def _discard_without_page(self) -> None:
        """무전 페이지가 없으면 음성을 만들지 않고 큐를 비운다(기록은 남긴다)."""
        while (message := self._queue.next(self._clock())) is not None:
            self._queue.complete()
            self._record(message, played=False, latency_ms=None)

    async def _speak(self, message: RadioMessage) -> None:
        # 엔진이 꺼져 있는 서버 시작 경로에서 httpx(Qwen)를 불러오지 않도록
        # 실제로 말할 때 import한다(SCRUM-82).
        from f1re.tts.qwen import TTSUnavailable

        played = False
        interrupted = False
        latency_ms: int | None = None
        source: Any | None = None
        started_ns: int | None = None
        played_s = 0.0
        caption = {
            "type": "caption",
            "text": message.text,
            "kind": message.kind,
            "priority": int(message.priority),
            "evidence": message.evidence,
            "lap": message.lap,
            "corner": message.corner,
        }
        # 자막은 실제로 소리를 낼 수 있을 때만 내보낸다(SCRUM-141). 합성 실패로
        # 소리를 내지 못하는 무전의 자막만 화면에 남는 불일치를 없앤다.
        caption_sent = False
        try:
            if self._cache is None:
                raise TTSUnavailable("음성 캐시가 설정되지 않았습니다")
            source = await self._cache.get_or_synthesize(message.text)
            sample_rate = int(getattr(source, "sample_rate", SAMPLE_RATE) or SAMPLE_RATE)
            seq = 0
            async for chunk in source.iter_pcm():
                if not chunk:
                    continue
                reason = self._stop_reason(message)
                if reason is None and started_ns is not None:
                    # 조각 사이 페이싱. 재생 위치보다 PACE_MARGIN_S 이상
                    # 앞서 보내지 않아 브라우저에 소리가 쌓이지 않게 한다.
                    reason = await self._pace(
                        started_ns, played_s - PACE_MARGIN_S, message
                    )
                if reason is not None:
                    interrupted = True
                    self._broadcast_interrupt(reason)
                    break
                if started_ns is None:
                    started_ns = self._clock()
                if latency_ms is None:
                    latency_ms = max(
                        0,
                        int((self._clock() - message.created_ns) / 1_000_000),
                    )
                if not caption_sent:
                    # 첫 음성 조각을 확보한 뒤 자막과 오디오를 함께 보낸다.
                    caption_sent = True
                    self._last_caption = caption
                    self._broadcast(caption)
                played = True
                self._broadcast(
                    {
                        "type": "audio",
                        "seq": seq,
                        "pcm": base64.b64encode(chunk).decode("ascii"),
                        "sample_rate": sample_rate,
                        "channels": CHANNELS,
                        "format": SAMPLE_FORMAT,
                    }
                )
                played_s += len(chunk) / 2 / sample_rate
                seq += 1
            if not interrupted and started_ns is not None:
                # 마지막 조각의 남은 재생 시간만큼 기다린 뒤 끝났다고 알린다.
                # 다음 무전은 그 뒤에야 큐에서 나가므로 소리가 겹치지 않는다.
                reason = await self._pace(started_ns, played_s, message)
                if reason is not None:
                    interrupted = True
                    self._broadcast_interrupt(reason)
        except TTSUnavailable as exc:
            # 음성 API 실패·타임아웃이면 그 문장만 건너뛰고 큐는 계속 돈다.
            logger.warning(
                "무전 음성을 만들지 못해 건너뜁니다(%s): %s",
                type(exc).__name__,
                message.text,
            )
        finally:
            if source is not None:
                with contextlib.suppress(Exception):
                    await source.aclose()
            self._queue.complete()
            self._record(message, played=played, latency_ms=latency_ms)
            self._broadcast(
                {
                    "type": "done",
                    "text": message.text,
                    "played": played,
                    "interrupted": interrupted,
                    "latency_ms": latency_ms,
                }
            )
            self._broadcast_status()

    def _stop_reason(self, message: RadioMessage) -> str | None:
        """지금 재생을 끊어야 하는 이유. 없으면 None.

        ``"priority"``는 더 높은 우선순위 무전이 대기 중이라는 뜻이고,
        ``"detached"``는 무전 페이지가 사라졌다는 뜻이다.
        """
        if not self._has_clients():
            return "detached"
        pending = self._queue.pending()
        if pending and pending[0].priority < message.priority:
            # 말하는 중에 더 높은 우선순위가 왔다. 끊고 다음으로 넘어간다.
            return "priority"
        return None

    def _broadcast_interrupt(self, reason: str) -> None:
        """브라우저가 재생 중인 조각을 즉시 멈추게 한다.

        페이지가 사라졌으면 받을 곳이 없으므로 보내지 않는다.
        """
        if reason == "priority":
            self._broadcast({"type": "interrupt"})

    async def _pace(
        self, started_ns: int, target_s: float, message: RadioMessage
    ) -> str | None:
        """보낸 PCM의 재생 위치 ``target_s``초까지 실제 시계를 맞춘다.

        ``PACE_SLICE_S``씩 나눠 기다리며 그 사이에 더 높은 우선순위 무전이
        오거나 페이지가 사라지면 :meth:`_stop_reason`의 이유를 돌려준다.
        기다릴 필요가 없으면 바로 None을 돌려준다.
        """
        while True:
            elapsed_s = (self._clock() - started_ns) / 1_000_000_000
            remaining_s = target_s - elapsed_s
            if remaining_s <= 0:
                return None
            reason = self._stop_reason(message)
            if reason is not None:
                return reason
            await self._sleep(min(remaining_s, PACE_SLICE_S))

    # ------------------------------------------------------------------
    # 기록

    def _record(
        self,
        message: RadioMessage,
        *,
        played: bool,
        latency_ms: int | None,
        dropped_reason: str | None = None,
    ) -> None:
        try:
            log = self._log_for_session()
            if log is not None:
                log.record(
                    message,
                    played=played,
                    latency_ms=latency_ms,
                    dropped_reason=dropped_reason,
                )
        except Exception:  # DB 오류가 재생을 막지 않는다.
            logger.exception("무전 기록을 남기지 못했습니다: %s", message.text)

    def _record_dropped(self, message: RadioMessage, reason: str) -> None:
        """큐가 버린 무전도 `played=False`와 이유로 기록한다(SCRUM-81)."""
        self._record(
            message,
            played=False,
            latency_ms=None,
            dropped_reason=reason,
        )

    def _log_for_session(self) -> RadioLog | None:
        if self._log is not None:
            return self._log
        if self._log_conn is None:
            return None
        uid = self._session_uid
        log = self._log_cache.get(uid)
        if log is None:
            log = RadioLog(self._log_conn, session_uid=uid, clock=self._clock)
            self._log_cache[uid] = log
        return log


def build_radio_service(
    store: StateStore, *, log_conn: Any | None = None
) -> RadioService:
    """실서비스용 무전 서비스. Qwen TTS와 음성 캐시, DB 기록을 붙인다.

    TTS 클라이언트 생성은 API 키를 읽지 않는다(첫 합성 때 읽는다). 서버가
    떠 있을 때는 무전이 없으므로 네트워크를 쓰지 않는다.
    """
    from f1re.config import load_config
    from f1re.tts.cache import VoiceCache
    from f1re.tts.qwen import QwenTTS

    config = load_config()
    tts = QwenTTS(config.tts)
    cache = VoiceCache(tts, config=config.tts)
    return RadioService(
        store,
        cache=cache,
        rules=rules_from_config(),
        log_conn=log_conn,
        tts=tts,
    )


# ---------------------------------------------------------------------------
# 라우터


router = APIRouter()


@router.get("/radio", include_in_schema=False)
def radio_page() -> FileResponse:
    """소리만 내는 무전 페이지(게임 PC 브라우저). 화면은 태블릿으로 본다."""
    return FileResponse(
        STATIC_DIR / "radio.html",
        media_type="text/html; charset=utf-8",
    )


@router.post("/api/voice/check")
async def voice_check(request: Request) -> dict[str, Any]:
    """음성 상태를 지금 다시 확인한다(SCRUM-92).

    캐시가 있으면 Qwen을 한 번 호출한다. 엔진이 꺼져 있거나 무전 서비스가
    없으면 호출하지 않고 기본(오프라인·미연결) 상태를 200으로 돌려준다.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is not None and engine.state != ENGINE_ON:
        return {"status": default_status()}
    service: RadioService | None = (
        engine.radio if engine is not None else None
    ) or getattr(request.app.state, "radio", None)
    if service is None:
        return {"status": default_status()}
    return {"status": await service.check_voice()}


@router.websocket("/ws/radio")
async def ws_radio(websocket: WebSocket) -> None:
    """무전 페이지 하나와 연결해 음성 조각·자막·상태를 보낸다.

    엔진이 꺼져 있으면 연결하지 않고 ``{"type": "engine", "state": "off"}``를
    보낸 뒤 닫는다. 켜져 있는 동안 엔진이 꺼지면 같은 메시지를 보내고 닫는다
    (SCRUM-82).
    """
    await websocket.accept()
    engine = getattr(websocket.app.state, "engine", None)
    if engine is not None and engine.state != ENGINE_ON:
        await _send_engine_off(websocket)
        return

    service: RadioService | None = (
        engine.radio if engine is not None else None
    ) or getattr(websocket.app.state, "radio", None)
    if service is None:
        await websocket.send_json(
            {
                "type": "status",
                "status": default_status(),
                "last": None,
            }
        )
        with contextlib.suppress(Exception):
            await websocket.close()
        return

    service.ensure_started()
    client = service.add_client()
    sender = asyncio.create_task(_send_loop(websocket, client))
    receiver = asyncio.create_task(_receive_loop(websocket))
    tasks = [sender, receiver]
    if engine is not None:
        # 엔진 OFF 전환을 기다렸다가 알리고 끝난다. FIRST_COMPLETED가 되면
        # 아래 finally가 나머지 루프를 정리하고 WebSocket을 닫는다.
        tasks.append(
            asyncio.create_task(
                _engine_off_watcher(engine.off_event, websocket),
                name="f1re-ws-engine-off",
            )
        )
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        service.remove_client(client)
        with contextlib.suppress(Exception):
            await websocket.close()


async def _engine_off_watcher(event: asyncio.Event, websocket: WebSocket) -> None:
    """엔진이 꺼질 때까지 기다렸다가 engine-off 메시지를 보낸다."""
    await event.wait()
    await _send_engine_off(websocket)


async def _send_engine_off(websocket: WebSocket) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_json({"type": "engine", "state": ENGINE_OFF})
    with contextlib.suppress(Exception):
        await websocket.close()


async def _send_loop(websocket: WebSocket, client: _Client) -> None:
    """서비스가 넣은 메시지를 브라우저로 보낸다."""
    try:
        while True:
            payload = await client.queue.get()
            await websocket.send_json(payload)
    except (WebSocketDisconnect, RuntimeError, OSError):
        return


async def _receive_loop(websocket: WebSocket) -> None:
    """브라우저가 보내는 것은 없지만, 연결이 끊긴 것을 바로 알아챈다."""
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError, OSError):
        return
