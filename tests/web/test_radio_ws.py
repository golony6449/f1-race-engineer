"""무전 페이지와 `WS /ws/radio`: 음성 조각·자막·상태 전달, 끊김·오프라인 상태.

공용 conftest를 만들지 않는 규칙에 따라 가짜 음성 캐시·기록기는 이 파일 안에 둔다.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi.testclient import TestClient

from f1re.radio.queue import RadioKind, RadioMessage
from f1re.state.store import StateStore
from f1re.tts.cache import AudioSource
from f1re.tts.qwen import TTSUnavailable
from f1re.web.app import create_app
from f1re.web.radio_api import RadioService

SAMPLE_RATE = 24_000
CHUNKS = [b"\x01\x00" * 240, b"\x02\x00" * 240]


# ---------------------------------------------------------------------------
# 가짜 음성 캐시·기록기


async def _chunk_stream(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class FakeCache:
    """Qwen 없이 캐시 적중/실패를 흉내 내는 음성 캐시."""

    def __init__(
        self,
        *,
        chunks: list[bytes] | None = None,
        fail_texts: set[str] | None = None,
    ) -> None:
        self.chunks = list(chunks if chunks is not None else CHUNKS)
        self.fail_texts = fail_texts or set()
        self.offline = False
        self.last_error: str | None = None
        self.calls: list[str] = []

    async def get_or_synthesize(
        self, text: str, voice: str | None = None
    ) -> AudioSource:
        self.calls.append(text)
        if text in self.fail_texts:
            self.offline = True
            self.last_error = "Qwen TTS 대역 오류"
            raise TTSUnavailable("Qwen TTS 대역 오류")
        self.offline = False
        self.last_error = None
        return AudioSource(text, _chunk_stream(self.chunks), cached=True)


class FakeLog:
    """RadioLog 대역. 어떤 무전이 재생됐는지 순서대로 남긴다."""

    def __init__(self) -> None:
        self.records: list[tuple[str, bool, int | None, str | None]] = []

    def record(
        self,
        message: RadioMessage,
        played: bool = False,
        latency_ms: int | None = None,
        dropped_reason: str | None = None,
    ) -> int:
        self.records.append(
            (message.text, played, latency_ms, dropped_reason)
        )
        return len(self.records)


class SlowCache:
    """첫 조각을 내보낸 뒤 잠깐 멈추는 캐시. 재생 중 끊기를 시험한다."""

    def __init__(self) -> None:
        self.offline = False
        self.last_error: str | None = None
        self.first_chunk = asyncio.Event()

    async def get_or_synthesize(
        self, text: str, voice: str | None = None
    ) -> AudioSource:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"\x01\x00" * 240
            if text == "박스, 박스. 들어와.":
                self.first_chunk.set()
                # 다음 조각이 오기 전에 더 높은 우선순위 무전이 도착한다.
                await asyncio.sleep(0.2)
                yield b"\x02\x00" * 240

        return AudioSource(text, chunks(), cached=True)


class GatedRules:
    """시험에서 켤 때만 경고를 내는 규칙 대역."""

    def __init__(self, message: RadioMessage) -> None:
        self.message = message
        self.enabled = False
        self.sent = False

    def evaluate(self, snapshot: dict[str, Any], events: Any = ()) -> list[RadioMessage]:
        if not self.enabled or self.sent:
            return []
        self.sent = True
        return [self.message]


class FakeClock:
    """테스트용 시계. 주입한 `sleep`이 부를 때만 시간이 흐른다."""

    def __init__(self, start_ns: int | None = None) -> None:
        self.now_ns = time.time_ns() if start_ns is None else start_ns
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[int], None] | None = None

    def __call__(self) -> int:
        return self.now_ns

    async def sleep(self, delay_s: float) -> None:
        self.sleeps.append(delay_s)
        self.now_ns += int(delay_s * 1_000_000_000)
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))
        await asyncio.sleep(0)


def _pcm(seconds: float, value: int = 1) -> bytes:
    """`seconds`초 길이의 24kHz 16bit mono PCM 한 조각."""
    return bytes([value, 0]) * int(SAMPLE_RATE * seconds)


class TimedCache:
    """문장마다 정해진 재생 길이의 조각을 내는 캐시. 페이싱 시험용."""

    def __init__(self, chunks_by_text: dict[str, list[bytes]]) -> None:
        self.chunks_by_text = chunks_by_text
        self.offline = False
        self.last_error: str | None = None

    async def get_or_synthesize(
        self, text: str, voice: str | None = None
    ) -> AudioSource:
        try:
            chunks = self.chunks_by_text[text]
        except KeyError as exc:
            raise AssertionError(f"준비하지 않은 문장: {text}") from exc
        return AudioSource(text, _chunk_stream(chunks), cached=True)


class RecordingService(RadioService):
    """브로드캐스트 순서와 시각을 남기는 서비스. 페이싱·끊기 시험용."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.payloads: list[dict[str, Any]] = []
        self.audio_times: list[tuple[int, int]] = []

    def _broadcast(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)
        if payload.get("type") == "audio":
            self.audio_times.append((self._clock(), int(payload["seq"])))
        super()._broadcast(payload)


def _message(
    text: str = "박스, 박스. 들어와.",
    *,
    kind: RadioKind = RadioKind.BOX,
    ttl_ns: int = 10_000_000_000,
) -> RadioMessage:
    return RadioMessage.create(
        kind=kind.value,
        text=text,
        now_ns=time.time_ns(),
        ttl_ns=ttl_ns,
        evidence={"reason": "test"},
        lap=10,
    )


def _service(
    store: StateStore,
    *,
    cache: Any = None,
    log: FakeLog | None = None,
    rules: Any = None,
) -> RadioService:
    return RadioService(
        store,
        cache=cache if cache is not None else FakeCache(),
        log=log if log is not None else FakeLog(),
        rules=rules,
    )


def _receive_until(websocket: Any, message_type: str, limit: int = 20) -> list[dict]:
    """`message_type`이 나올 때까지 받아 모은다."""
    received: list[dict] = []
    for _ in range(limit):
        payload = websocket.receive_json()
        received.append(payload)
        if payload.get("type") == message_type:
            return received
    raise AssertionError(f"{message_type} 메시지를 받지 못했습니다: {received}")


def _wait_disconnected(client: TestClient, deadline_s: float = 3.0) -> dict:
    """무전 페이지가 끊긴 것으로 바뀔 때까지 상태를 확인한다."""
    deadline = time.monotonic() + deadline_s
    status: dict = {}
    while time.monotonic() < deadline:
        status = client.get("/api/state").json()["status"]
        if not status["radio_page"]["connected"]:
            return status
        time.sleep(0.02)
    return status


# ---------------------------------------------------------------------------
# 페이지·정적 파일


def test_radio_page_is_served() -> None:
    store = StateStore()
    app = create_app(store, radio=_service(store))
    with TestClient(app) as client:
        response = client.get("/radio")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "/static/radio.js" in response.text
        # 처음 한 번 누르는 "무전 연결" 버튼과 백그라운드 탭 유지 안내.
        assert "무전 연결" in response.text
        assert "항상 활성" in response.text

        script = client.get("/static/radio.js")
        assert script.status_code == 200
        assert "/ws/radio" in script.text


# ---------------------------------------------------------------------------
# WS 음성·자막·상태


def test_radio_ws_delivers_audio_and_caption() -> None:
    store = StateStore()
    cache = FakeCache()
    log = FakeLog()
    service = _service(store, cache=cache, log=log)
    app = create_app(store, radio=service)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/radio") as websocket:
            initial = websocket.receive_json()
            assert initial["type"] == "status"
            assert initial["status"]["voice"] == {
                "state": "online",
                "label": "음성 온라인",
            }
            assert initial["status"]["radio_page"]["connected"] is True
            assert initial["status"]["radio_page"]["label"] == "무전 페이지 연결됨"
            assert initial["last"] is None

            service.queue.push(_message())
            received = _receive_until(websocket, "done")

            caption = next(p for p in received if p["type"] == "caption")
            assert caption["text"] == "박스, 박스. 들어와."
            assert caption["kind"] == "box"
            assert caption["lap"] == 10

            audio = [p for p in received if p["type"] == "audio"]
            assert [base64.b64decode(p["pcm"]) for p in audio] == CHUNKS
            assert [p["seq"] for p in audio] == [0, 1]
            assert all(p["sample_rate"] == SAMPLE_RATE for p in audio)
            assert all(p["channels"] == 1 for p in audio)
            assert all(p["format"] == "s16le" for p in audio)

            done = received[-1]
            assert done["played"] is True
            assert done["interrupted"] is False
            assert done["latency_ms"] is not None
            assert done["latency_ms"] >= 0

        # 재생했다는 기록이 남는다(시각·재생 여부·지연).
        assert len(log.records) == 1
        assert log.records[0][0] == "박스, 박스. 들어와."
        assert log.records[0][1] is True
        assert log.records[0][2] is not None

        # 페이지를 닫으면 대시보드가 미연결로 바뀐다.
        status = _wait_disconnected(client)
        assert status["radio_page"]["connected"] is False
        assert status["radio_page"]["label"] == "무전 페이지 미연결"

        # 마지막 자막은 새로 접속한 페이지에 다시 보여 준다.
        with client.websocket_connect("/ws/radio") as websocket:
            initial = websocket.receive_json()
            assert initial["last"]["text"] == "박스, 박스. 들어와."


def test_radio_disconnected_status_visible() -> None:
    """무전 페이지가 붙기 전·끊긴 뒤 대시보드에 '무전 페이지 미연결'이 보인다."""
    store = StateStore()
    service = _service(store)
    app = create_app(store, radio=service)

    with TestClient(app) as client:
        before = client.get("/api/state").json()["status"]["radio_page"]
        assert before == {
            "state": "disconnected",
            "label": "무전 페이지 미연결",
            "connected": False,
            "clients": 0,
        }

        with client.websocket_connect("/ws/radio") as websocket:
            connected = websocket.receive_json()["status"]["radio_page"]
            assert connected["connected"] is True
            assert connected["label"] == "무전 페이지 연결됨"

        after = _wait_disconnected(client)
        assert after["radio_page"]["label"] == "무전 페이지 미연결"
        assert after["radio_page"]["connected"] is False


def test_alert_loop_sends_message_to_page() -> None:
    """경고 판정 → 큐 → WebSocket까지 실제 경로로 한 줄이 흐르는지."""
    store = StateStore()
    message = _message("앞왼쪽 타이어 마모가 80퍼센트야.", kind=RadioKind.SAFETY)
    rules = GatedRules(message)
    service = _service(store, rules=rules)
    app = create_app(store, radio=service)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/radio") as websocket:
            assert websocket.receive_json()["type"] == "status"
            rules.enabled = True
            received = _receive_until(websocket, "done")

            caption = next(p for p in received if p["type"] == "caption")
            assert caption["text"] == "앞왼쪽 타이어 마모가 80퍼센트야."
            assert caption["kind"] == "safety"
            audio = [p for p in received if p["type"] == "audio"]
            assert len(audio) == len(CHUNKS)


# ---------------------------------------------------------------------------
# 장애 상태(Review Focus 3)


def test_tts_offline_keeps_queue_moving_and_flags_offline() -> None:
    store = StateStore()
    cache = FakeCache(fail_texts={"합성 실패"})
    log = FakeLog()
    service = _service(store, cache=cache, log=log)
    app = create_app(store, radio=service)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/radio") as websocket:
            assert websocket.receive_json()["type"] == "status"
            service.queue.push(_message("합성 실패", kind=RadioKind.SAFETY))
            service.queue.push(_message("박스, 박스. 들어와.", kind=RadioKind.BOX))

            received: list[dict] = []
            while len([p for p in received if p["type"] == "done"]) < 2:
                received.append(websocket.receive_json())

            done = [p for p in received if p["type"] == "done"]
            assert done[0]["played"] is False
            assert done[0]["latency_ms"] is None
            # 첫 문장이 실패해도 다음 문장은 캐시에서 정상 재생된다.
            assert done[1]["played"] is True

            offline = [
                p
                for p in received
                if p["type"] == "status"
                and p["status"]["voice"]["label"] == "음성 오프라인"
            ]
            assert offline, "음성 오프라인 상태가 페이지로 가지 않았습니다"

        # 상태 API에도 같은 값이 보인다(캐시가 나중에 성공하면 온라인으로 돌아온다).
        state = client.get("/api/state").json()["status"]
        assert state["voice"]["state"] == "online"

    assert [record[0] for record in log.records] == ["합성 실패", "박스, 박스. 들어와."]
    assert log.records[0][1] is False
    assert log.records[1][1] is True


async def test_higher_priority_interrupts_current_radio() -> None:
    """말하는 중에 더 높은 우선순위가 오면 끊고 바꾼다(설계 6.7)."""
    store = StateStore()
    cache = SlowCache()
    log = FakeLog()
    service = _service(store, cache=cache, log=log)

    service.ensure_started()
    service.add_client()
    service.queue.push(_message("박스, 박스. 들어와.", kind=RadioKind.BOX))
    await asyncio.wait_for(cache.first_chunk.wait(), timeout=2.0)
    service.queue.push(_message("앞왼쪽 타이어 마모가 80퍼센트야.", kind=RadioKind.SAFETY))

    deadline = time.monotonic() + 3.0
    while len(log.records) < 2 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await service.aclose()

    assert [record[0] for record in log.records] == [
        "박스, 박스. 들어와.",
        "앞왼쪽 타이어 마모가 80퍼센트야.",
    ]
    assert service.queue.pending_count == 0


async def test_second_radio_starts_after_first_playback_ends() -> None:
    """둘째 무전 첫 조각은 첫째 재생 길이 뒤에 나간다(설계 6.7 동시 발화 금지)."""
    clock = FakeClock()
    store = StateStore()
    cache = TimedCache(
        {
            "첫째": [_pcm(0.5, 1), _pcm(0.5, 2)],
            "둘째": [_pcm(0.5, 3)],
        }
    )
    log = FakeLog()
    service = RecordingService(
        store, cache=cache, log=log, clock=clock, sleep=clock.sleep
    )
    service.ensure_started()
    service.add_client()
    service.queue.push(_message("첫째"))
    service.queue.push(_message("둘째"))

    deadline = time.monotonic() + 3.0
    while len(log.records) < 2 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await service.aclose()

    assert [record[0] for record in log.records] == ["첫째", "둘째"]
    # 첫째 조각 둘(seq 0, 1) 뒤에 둘째 첫 조각(seq 0)이 나간다.
    assert [seq for _, seq in service.audio_times] == [0, 1, 0]
    first_ns = service.audio_times[0][0]
    second_ns = service.audio_times[2][0]
    # 첫째 재생 길이는 0.5초 조각 두 개 = 1.0초. 둘째는 그 뒤에 시작한다.
    assert second_ns - first_ns >= 1_000_000_000
    assert clock.sleeps, "재생 길이만큼 기다리는 페이싱이 없었습니다"


async def test_priority_interrupt_sends_interrupt_and_speaks_safety() -> None:
    """재생 중 SAFETY가 오면 interrupt를 보내고 끊은 뒤 SAFETY를 말한다."""
    clock = FakeClock()
    store = StateStore()
    cache = TimedCache(
        {
            "박스, 박스. 들어와.": [_pcm(0.5, 1), _pcm(0.5, 2)],
            "앞왼쪽 타이어 마모가 80퍼센트야.": [_pcm(0.25, 9)],
        }
    )
    log = FakeLog()
    safety = _message("앞왼쪽 타이어 마모가 80퍼센트야.", kind=RadioKind.SAFETY)
    service = RecordingService(
        store, cache=cache, log=log, clock=clock, sleep=clock.sleep
    )
    pushed = False

    def push_safety(call: int) -> None:
        nonlocal pushed
        if call == 1 and not pushed:
            pushed = True
            service.queue.push(safety)

    clock.on_sleep = push_safety
    service.ensure_started()
    service.add_client()
    service.queue.push(_message("박스, 박스. 들어와."))

    deadline = time.monotonic() + 3.0
    while len(log.records) < 2 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await service.aclose()

    assert [record[0] for record in log.records] == [
        "박스, 박스. 들어와.",
        "앞왼쪽 타이어 마모가 80퍼센트야.",
    ]
    types = [payload["type"] for payload in service.payloads]
    interrupted_at = types.index("interrupt")
    safety_caption_at = next(
        index
        for index, payload in enumerate(service.payloads)
        if payload["type"] == "caption"
        and payload["text"] == "앞왼쪽 타이어 마모가 80퍼센트야."
    )
    # interrupt가 먼저 가고 그 뒤에 SAFETY 자막·음성이 나간다.
    assert interrupted_at < safety_caption_at

    # 끊긴 박스 무전은 첫 조각만 나가고 둘째 조각은 보내지 않는다.
    box_audio = [
        payload
        for payload in service.payloads[:interrupted_at]
        if payload["type"] == "audio"
    ]
    assert [payload["seq"] for payload in box_audio] == [0]
    box_done = next(
        payload
        for payload in service.payloads
        if payload["type"] == "done" and payload["text"] == "박스, 박스. 들어와."
    )
    assert box_done["interrupted"] is True

    # SAFETY 무전은 끊긴 뒤에 실제로 재생된다.
    safety_audio = [
        payload
        for payload in service.payloads[interrupted_at + 1 :]
        if payload["type"] == "audio"
    ]
    assert [payload["seq"] for payload in safety_audio] == [0]
    assert log.records[1][1] is True


async def test_without_page_skips_pacing_and_records_unplayed() -> None:
    """무전 페이지가 없으면 재생 대기 없이 큐를 비우고 played=False로 남긴다."""
    clock = FakeClock()
    log = FakeLog()
    service = RecordingService(
        StateStore(),
        cache=TimedCache({"박스, 박스. 들어와.": [_pcm(1.0, 1)]}),
        log=log,
        clock=clock,
        sleep=clock.sleep,
    )

    service.ensure_started()
    service.queue.push(_message("박스, 박스. 들어와."))
    deadline = time.monotonic() + 2.0
    while not log.records and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await service.aclose()

    assert service.queue.pending_count == 0
    assert log.records[0][:2] == ("박스, 박스. 들어와.", False)
    assert clock.sleeps == [], "페이지가 없는데 재생 대기를 했습니다"


async def test_expired_radio_is_recorded_with_reason() -> None:
    """재생 대기 중 만료된 무전도 이유와 함께 기록된다(SCRUM-81)."""
    clock = FakeClock()
    log = FakeLog()
    service = RecordingService(
        StateStore(),
        cache=FakeCache(),
        log=log,
        clock=clock,
        sleep=clock.sleep,
    )
    service.ensure_started()
    service.queue.push(
        _message("곧 만료", kind=RadioKind.COACHING, ttl_ns=1_000_000_000)
    )
    clock.now_ns += 2_000_000_000

    deadline = time.monotonic() + 2.0
    while not log.records and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await service.aclose()

    assert log.records == [("곧 만료", False, None, "expired")]


def test_radio_without_page_discards_queue_and_keeps_status() -> None:
    """무전 페이지가 없어도 큐가 막히지 않고, 상태는 미연결로 보인다."""
    store = StateStore()
    log = FakeLog()
    service = _service(store, log=log)

    async def scenario() -> None:
        service.ensure_started()
        service.queue.push(_message("박스, 박스. 들어와."))
        deadline = time.monotonic() + 2.0
        while not log.records and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await service.aclose()

    asyncio.run(scenario())

    assert service.queue.pending_count == 0
    assert log.records[0][:2] == ("박스, 박스. 들어와.", False)
    assert service.status()["radio_page"] == {
        "state": "disconnected",
        "label": "무전 페이지 미연결",
        "connected": False,
        "clients": 0,
    }


def test_radio_service_without_cache_reports_offline() -> None:
    store = StateStore()
    service = RadioService(store)
    status = service.status()
    assert status["voice"] == {"state": "offline", "label": "음성 오프라인"}

    app = create_app(store, radio=service)
    with TestClient(app) as client:
        state = client.get("/api/state").json()["status"]
        assert state["voice"]["label"] == "음성 오프라인"
        assert state["radio_page"]["connected"] is False
