"""SCRUM-84: 실서버 파이프라인 종단 테스트(실제 UDP).

`f1re serve`와 같은 경로로 엔진을 켜고, 데모 송신기(별도 프로세스)를 빠른
배속으로 보내 실제 UDP → 파싱 → 랩 기록 → 코칭·추월·리프트 앤 코스트 →
무전 큐까지 확인한다. 무전 음성(Qwen)은 쓰지 않도록 가짜 무전 서비스를
`radio_factory`로 넣는다.

확인 항목(작업 지시):
(a) `coaching.yaml` → 코너 번호·동작·수치가 있는 코칭 무전
(b) `overtake.yaml` → 추월 무전
(c) `lift_and_coast.yaml` → 리프트 지시 무전
(d) `/api/report`·`/api/lift-coast`·`/api/track`이 파이프라인의 실제 상태를 읽음
(e) 엔진 OFF→ON 뒤에도 동작, OFF면 파이프라인 객체가 없음

공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from f1re.config import AppConfig
from f1re.demo_sender.scenarios import SCENARIO_DIR
from f1re.engine import Engine
from f1re.radio.queue import RadioMessage, RadioQueue
from f1re.web.app import create_app

SUZUKA_ID = 13
HOST = "127.0.0.1"

# 별도 프로세스에서 데모를 보낸다. 같은 프로세스면 GIL 때문에 송신·수신이
# 번갈아 실행되어 몇 배 느리다(기존 종단 테스트와 같은 이유).
# 윈도우 타이머 해상도 때문에 매 틱 자면 배속이 안 나오므로 여러 틱을 묶어
# 잔다. 묶음이 크면 수신 버퍼가 순간적으로 밀리므로 50틱(약 650패킷)으로 둔다.
_SENDER_CODE = """
import sys
import time

from f1re.demo_sender.scenarios import load_scenarios
from f1re.demo_sender.sender import DemoSender
from f1re.tracks.model import load_outline, load_track


class _BatchedSleeper:
    def __init__(self, speed: float, batch: int = 50) -> None:
        self.interval = batch / 60.0 / speed
        self.batch = batch
        self.calls = 0

    def __call__(self, _seconds: float) -> None:
        self.calls += 1
        if self.calls % self.batch == 0:
            time.sleep(self.interval)


port = int(sys.argv[1])
mode = sys.argv[2]
laps = int(sys.argv[3])
paths = [path for path in sys.argv[4].split(";") if path]
speed = float(sys.argv[5])
scenarios = []
for path in paths:
    scenarios.extend(load_scenarios(path))
track = load_track(13)
outline = load_outline(13)
sender = DemoSender(
    track,
    outline,
    laps=laps,
    seed=0,
    host="127.0.0.1",
    port=port,
    rate_hz=20,
    speed=speed,
    mode=mode,
    scenarios=scenarios,
)
sender.run(sleep=_BatchedSleeper(speed))
"""


class _CaptureQueue(RadioQueue):
    """푸시된 무전을 모두 보관하는 큐(재생 루프가 없어 비우지 않는다)."""

    def __init__(self) -> None:
        super().__init__()
        self.pushed: list[RadioMessage] = []

    def push(self, msg: RadioMessage) -> bool:
        self.pushed.append(msg)
        return super().push(msg)


class _FakeRadio:
    """음성(TTS) 없이 무전 큐만 제공하는 무전 서비스 대역."""

    def __init__(self) -> None:
        self.queue = _CaptureQueue()

    def ensure_started(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {
            "voice": {"state": "offline", "label": "음성 오프라인"},
            "radio_page": {
                "state": "disconnected",
                "label": "무전 페이지 미연결",
                "connected": False,
                "clients": 0,
            },
        }


def _radio_factory(_store: Any, _conn: Any) -> _FakeRadio:
    return _FakeRadio()


def _engine(tmp_path: Path, *, db_name: str = "f1re.db") -> Engine:
    return Engine(
        config=AppConfig(),
        host=HOST,
        port=0,
        db_path=tmp_path / db_name,
        state_path=tmp_path / "engine-state.json",
        radio_factory=_radio_factory,
        persist_state=False,
    )


def _scenario_paths(*names: str) -> str:
    return ";".join(str(SCENARIO_DIR / name) for name in names)


async def _send_demo(
    engine: Engine,
    scenario_paths: str,
    *,
    mode: str,
    laps: int,
    speed: float = 30.0,
    timeout: float = 240.0,
) -> None:
    """데모 송신기를 별도 프로세스로 돌려 엔진의 UDP 포트로 보낸다."""
    port = engine.udp_port
    assert port is not None, "엔진이 UDP 소켓을 열지 않았습니다"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _SENDER_CODE,
        str(port),
        mode,
        str(laps),
        scenario_paths,
        str(speed),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode(errors="replace")[-2000:]


async def _wait_for(
    predicate: Any, *, timeout: float = 30.0, interval: float = 0.1
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def _messages(radio: Any, kind: str) -> list[RadioMessage]:
    return [msg for msg in radio.queue.pushed if msg.kind == kind]


def _corner_coaching(messages: list[RadioMessage]) -> list[RadioMessage]:
    """코너 번호·동작·수치가 있는 코칭 무전만 고른다(리프트 지시 제외)."""
    return [
        msg
        for msg in messages
        if msg.kind == "coaching"
        and re.search(r"\d+번", msg.text)
        and re.search(r"(브레이크|턴인|정점 속도|조향|스로틀|트랙 이탈|라인)", msg.text)
        and re.search(r"\d+\s*(미터|킬로|회)", msg.text)
    ]


# ---------------------------------------------------------------------------
# (a)(d) 코칭 + 지도·리포트·리프트 API


async def test_coaching_radio_and_report_map_lift_state(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    await engine.start()
    try:
        pipeline = engine.pipeline
        assert pipeline is not None
        radio = engine.runtime.radio

        # 타임 트라이얼 5랩: 2랩부터 코칭 문장이 나오고, 4랩이면 집중 코너가 정해진다.
        await _send_demo(
            engine, _scenario_paths("coaching.yaml"), mode="tt", laps=5
        )
        ready = await _wait_for(
            lambda: bool(pipeline.focus_corners)
            and bool(pipeline.report_state()["corner_losses"]),
            timeout=60.0,
        )
        assert ready, "집중 코너·코너 손실이 계산되지 않았습니다"

        coaching = _corner_coaching(_messages(radio, "coaching"))
        assert coaching, [msg.text for msg in radio.queue.pushed]
        text = coaching[0].text
        # "7번, 턴인 늦음. 6미터."처럼 코너·동작·수치를 갖춘다.
        assert re.search(r"\d+번", text)
        assert re.search(r"(미터|킬로|회)", text)

        client = TestClient(create_app(engine=engine))

        track = client.get("/api/track").json()
        assert track["track_id"] == SUZUKA_ID
        assert len(track["corners"]) == 18
        assert track["focus_corners"], track
        assert track["corner_metrics"], track
        metric = next(iter(track["corner_metrics"].values()))
        assert {"loss_ms", "player", "reference"} <= set(metric)

        report = client.get("/api/report").json()
        assert report["pipeline"]["track_id"] == SUZUKA_ID
        assert report["pipeline"]["focus_corners"]
        assert report["pipeline"]["corner_losses"]
        assert report["pipeline"]["reference"]["self"]["lap"] >= 1

        lift = client.get("/api/lift-coast").json()
        assert lift["recommended"], lift
        assert len(lift["recommended"]) <= 3
    finally:
        await engine.stop()


# ---------------------------------------------------------------------------
# (b)(c) 추월·리프트 앤 코스트


async def test_overtake_and_lift_coast_radio(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    await engine.start()
    try:
        pipeline = engine.pipeline
        assert pipeline is not None
        radio = engine.runtime.radio

        # 그랑프리 4랩. 레이스 2랩부터 추월 안내가 켜지고, 연료·브레이크 위험으로
        # 리프트 앤 코스트 지시가 나간다.
        await _send_demo(
            engine,
            _scenario_paths("overtake.yaml", "lift_and_coast.yaml"),
            mode="gp",
            laps=4,
        )
        ready = await _wait_for(
            lambda: bool(pipeline.lift_state() and pipeline.lift_state().snapshot()["recommended"]),
            timeout=60.0,
        )
        assert ready, "리프트 앤 코스트 추천 구간이 계산되지 않았습니다"

        overtake = [
            msg
            for msg in _messages(radio, "overtake")
            if "앞차" in msg.text and ("DRS" in msg.text or "기다림" in msg.text)
        ]
        assert overtake, [msg.text for msg in radio.queue.pushed]
        assert re.search(r"앞차 \d+\.\d", overtake[0].text)

        lift = [
            msg
            for msg in _messages(radio, "coaching")
            if "리프트" in msg.text
        ]
        assert lift, [msg.text for msg in radio.queue.pushed]
        assert re.search(r"\d+\s*미터", lift[0].text)

        state = pipeline.lift_state().snapshot()
        assert state["need"]["active"] is True
        assert state["need"]["reasons"]
        assert state["recent_coasting"], state
    finally:
        await engine.stop()


# ---------------------------------------------------------------------------
# (e) 엔진 OFF→ON


async def test_engine_off_on_rewires_pipeline(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    await engine.start()
    first = engine.pipeline
    assert first is not None
    first_radio = engine.runtime.radio
    try:
        await _send_demo(
            engine, _scenario_paths("coaching.yaml"), mode="tt", laps=3
        )
        assert await _wait_for(
            lambda: bool(_corner_coaching(_messages(first_radio, "coaching"))),
            timeout=60.0,
        ), "첫 번째 ON에서 코칭 무전이 나오지 않았습니다"
    finally:
        await engine.stop()

    # OFF면 파이프라인 객체가 없다.
    assert engine.pipeline is None
    assert engine.runtime is None

    # 다시 ON하면 새 파이프라인이 만들어지고 같은 DB의 저장 기준으로 코칭한다.
    await engine.start()
    second = engine.pipeline
    assert second is not None and second is not first
    second_radio = engine.runtime.radio
    try:
        await _send_demo(
            engine, _scenario_paths("coaching.yaml"), mode="tt", laps=3
        )
        assert await _wait_for(
            lambda: bool(_corner_coaching(_messages(second_radio, "coaching"))),
            timeout=60.0,
        ), "다시 켠 엔진에서 코칭 무전이 나오지 않았습니다"
    finally:
        await engine.stop()
    assert engine.pipeline is None
