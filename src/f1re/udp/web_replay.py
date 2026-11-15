"""웹 대시보드 "다시 보기" 리플레이 런타임(설계 4.6, 계획 작업 9).

실주행 `Engine`·`f1re.db`·Qwen TTS와 완전히 분리된 **메모리 전용** 재생기를
만든다. 녹화 파일(`.f1rec`/`.f1rec.zst`)을 `iter_recording()`으로 읽어
`StateStore`에 반영하고, 같은 패킷으로 `RacePipeline(conn=None)`과
`AlertRules`를 돌린다. 그래서 리플레이 중에도

- 코너 코칭·추월·리프트 앤 코스트(`RacePipeline`),
- 안전 경고·박스 지시(`AlertRules`),
- 대시보드의 차량 상태·서킷 지도·무전 기록·세션 리포트

가 실시간과 같은 모양으로 재현되지만, 기준 랩(`reference_sets`) 읽기/쓰기와
`radio_messages`·`sessions` 쓰기, Qwen TTS 호출은 일어나지 않는다.
`RacePipeline`에는 `conn=None`을 주고 무전은 `MemoryRadioSink`가 받아
`MemoryRadioLog`에만 쌓는다.

**가상 시계**: 패킷의 `recv_ns`를 그대로 시계로 쓴다(`_VirtualClock`).
`RadioQueue`·`RacePipeline`·`AlertRules`·`MemoryRadioSink`가 모두 이 시계를
보므로, 배속·시크로 시간을 건너뛰어도 무전 TTL·중복 방지·리포트 계산이
실시간과 같은 규칙을 지킨다(계획 Review Focus 5).

**시크**: 텔레메트리 상태는 이전 패킷 누적에 의존하므로, `seek(position_ms)`는
`StateStore`·`RacePipeline`·`AlertRules` 전이 기억을 모두 새로 만든 뒤 파일
처음부터 목표 지점 **직전**까지 `sleep` 없이 재생해 상태를 다시 만든다.
재구성 중에 나온 무전도 같은 가상 시각으로 `MemoryRadioLog`에 다시 쌓이므로,
무전 기록·리포트가 그 시점까지의 실제 재생과 같아진다.

**10Hz 상태 푸시**는 `state_payload()`가 맡는다. `web/app.py`의 기존
`state_payload()`와 같은 구조에 `replay` 메타를 더해 돌려주므로, 화면은
실시간과 같은 컴포넌트를 그대로 쓴다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from f1re.alerts.rules import AlertRules
from f1re.config import AppConfig
from f1re.pipeline import RacePipeline
from f1re.radio.queue import RadioQueue
from f1re.state.store import StateStore
from f1re.udp.parser import UnsupportedPacket, parse_packet
from f1re.udp.recording import iter_recording, scan_recording
from f1re.udp.replay_radio import MemoryRadioLog, MemoryRadioSink
from f1re.web.map_api import TrackMap

logger = logging.getLogger(__name__)

# 위험 경고 판정 주기(가상 시계). 실서버 무전 서비스의 10Hz와 같게 둔다.
ALERT_EVAL_INTERVAL_NS = 100_000_000

# 재생 대기 중 일시정지·정지·배속 변경을 확인하는 주기(초).
WAIT_SLICE_S = 0.05

# 이보다 짧은 남은 대기는 0으로 본다. 부동소수 오차(1e-17초)를 계속 자며
# 영원히 기다리는 것을 막는다.
WAIT_EPSILON_S = 1e-6

# 시크 재구성 뒤 `RacePipeline`의 완료 랩 처리 태스크가 따라잡을 시간(이벤트 루프 양보 횟수).
SEEK_SETTLE_YIELDS = 100

# `radio_log_payload()`가 기본으로 돌려줄 최근 무전 수(기존 무전 기록 화면과 같다).
DEFAULT_RADIO_LIMIT = 50

# 리플레이 상태 메타의 기본값(리플레이가 없을 때).
_INACTIVE_REPLAY: dict[str, Any] = {
    "active": False,
    "recording_id": None,
    "session_uid": None,
    "paused": False,
    "speed": 1.0,
    "position_ms": 0,
    "duration_ms": 0,
    "category": None,
    "track_id": None,
}


class _VirtualClock:
    """리플레이 패킷의 `recv_ns`를 돌려주는 가상 시계."""

    def __init__(self) -> None:
        self.now_ns = 0

    def __call__(self) -> int:
        return self.now_ns


@dataclass
class _ReplayRuntime:
    """리플레이 한 번(또는 시크 한 번)에 사는 메모리 전용 런타임 묶음.

    시크는 이 객체를 통째로 새로 만들어 전이 기억(`StateStore`·`RacePipeline`·
    `AlertRules`)을 모두 버린다. 재생 파일의 반복자도 여기 있어, 시크 재구성에
    쓴 위치에서 라이브 재생이 그대로 이어진다.
    """

    clock: _VirtualClock
    store: StateStore
    track_map: TrackMap
    # 지도 윤곽을 DB에서 읽을 때만 쓰는 읽기 전용 연결. 없으면 `None`.
    conn: sqlite3.Connection | None
    radio_log: MemoryRadioLog
    queue: RadioQueue
    sink: MemoryRadioSink
    pipeline: RacePipeline
    rules: AlertRules
    records: Iterator[tuple[int, bytes]]
    # 시크 재구성 중 목표 지점에 닿아 아직 적용하지 않은 레코드(라이브 루프가 먼저 쓴다).
    pending_record: tuple[int, bytes] | None = None
    first_recv_ns: int | None = None
    last_recv_ns: int | None = None
    # 재생 속도 계산 기준점(실제 시계, 마지막으로 적용한 패킷의 가상 시각).
    anchor_clock: float = 0.0
    anchor_ns: int = 0
    last_alert_ns: int | None = None
    position_ms: int = 0

    def next_record(self) -> tuple[int, bytes] | None:
        """다음 녹화 레코드. 파일 끝이면 `None`."""
        if self.pending_record is not None:
            record, self.pending_record = self.pending_record, None
            return record
        try:
            return next(self.records)
        except StopIteration:
            return None


class ReplayManager:
    """메모리 전용 리플레이 세션 하나를 재생·제어·조회한다(설계 4.6).

    `start()`가 녹화 파일을 받아 전용 런타임을 만들고 재생 태스크를 띄운다.
    이후 `pause`·`resume`·`seek`·`set_speed`·`stop`이 상태를 바꾸며,
    `state_payload()`·`track_payload()`·`report_payload()`·`radio_log_payload()`가
    대시보드 API에 넘길 묶음을 만든다. 리플레이는 한 번에 하나만 지원한다.

    `map_factory`는 지도 윤곽을 DB에서 읽을 때 쓴다(예: `lambda: TrackMap(conn)`).
    `conn_factory`를 주면 리플레이마다 새 연결을 열어 `TrackMap(conn)`에 넘기고,
    재생을 끝낼 때 그 연결을 닫는다(호출자가 넘긴 연결은 소유하지 않는다).
    둘 다 없으면 데이터 파일 윤곽만 쓰는 `TrackMap(None)`을 만든다. 리플레이
    쪽은 DB를 **읽기만** 하며, 어떤 테이블도 쓰지 않는다.
    """

    def __init__(
        self,
        *,
        config: AppConfig | None = None,
        map_factory: Callable[[], TrackMap] | None = None,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config or AppConfig()
        self._map_factory = map_factory
        self._conn_factory = conn_factory
        self._clock = clock
        self._sleep = sleep

        self._runtime: _ReplayRuntime | None = None
        self._task: asyncio.Task[None] | None = None
        self._resume = asyncio.Event()
        self._paused = False
        self._speed = 1.0
        self._finished = False

        self._recording_id: int | None = None
        self._meta: dict[str, Any] = {}
        self._file_path: Path | None = None
        self._duration_ms = 0
        self._position_ms = 0

    # ------------------------------------------------------------------
    # 조회

    @property
    def active_recording_id(self) -> int | None:
        """지금 재생 중인 `session_recordings.id`.

        로컬 5개 정리(`BackupWorker`)가 이 녹화 파일을 지우지 않게 하는 표식이다
        (계획 Review Focus 4). 리플레이가 없으면 `None`.
        """
        return self._recording_id

    @property
    def active(self) -> bool:
        """리플레이 런타임이 떠 있는가(일시정지·재생 끝 포함)."""
        return self._runtime is not None

    # ------------------------------------------------------------------
    # 수명 주기

    async def start(
        self,
        recording_meta: Mapping[str, Any],
        file_path: str | Path,
        *,
        speed: float = 1.0,
    ) -> dict[str, Any]:
        """녹화 재생을 시작한다. 이미 재생 중이면 이전 리플레이를 먼저 멈춘다.

        `recording_meta`는 `session_recordings` 행(딕셔너리)이다. `id`,
        `session_uid`, `category`, `track_id`, `duration_ms`를 상태 메타·리포트에
        쓴다. 파일이 없으면 `FileNotFoundError`를 낸다.
        """
        resolved_speed = _valid_speed(speed)
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"녹화 파일이 없습니다: {path}")

        await self._teardown()
        self._meta = dict(recording_meta or {})
        self._recording_id = _recording_id_from_meta(self._meta)
        self._file_path = path
        self._duration_ms = _duration_from_meta(self._meta, path)
        self._speed = resolved_speed
        self._paused = False
        self._finished = False
        self._position_ms = 0

        runtime = self._build_runtime()
        self._runtime = runtime
        try:
            await runtime.pipeline.start()
        except BaseException:
            self._runtime = None
            await self._close_runtime(runtime)
            raise
        self._task = asyncio.create_task(self._play_loop(), name="f1re-web-replay")
        return self.state_payload()

    async def stop(self) -> None:
        """리플레이를 끝내고 런타임·파일·DB 연결을 정리한다(여러 번 안전)."""
        await self._teardown()
        self._recording_id = None
        self._meta = {}
        self._file_path = None
        self._duration_ms = 0
        self._position_ms = 0
        self._paused = False
        self._finished = False
        self._speed = 1.0

    async def aclose(self) -> None:
        """서버 종료 경로. `stop()`과 같다."""
        await self.stop()

    # ------------------------------------------------------------------
    # 제어

    async def pause(self) -> dict[str, Any]:
        """재생을 멈춘다. 리플레이가 없으면 아무것도 하지 않는다."""
        if self._runtime is not None:
            self._paused = True
        return self.state_payload()

    async def resume(self) -> dict[str, Any]:
        """멈춘 재생을 이어 간다. 재생이 끝난 뒤면 처음부터 다시 시작한다."""
        if self._runtime is None:
            return self.state_payload()
        if self._finished:
            await self.seek(0)
        self._paused = False
        runtime = self._runtime
        if runtime is not None:
            runtime.anchor_clock = self._clock()
            runtime.anchor_ns = (
                runtime.last_recv_ns
                if runtime.last_recv_ns is not None
                else runtime.anchor_ns
            )
        self._resume.set()
        # 예기치 못한 오류로 재생 태스크가 끝났으면 이어서 다시 띄운다.
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._play_loop(), name="f1re-web-replay"
            )
        return self.state_payload()

    async def set_speed(self, speed: float) -> dict[str, Any]:
        """재생 배속을 바꾼다. 다음 패킷의 대기 시간부터 새 배속을 쓴다."""
        self._speed = _valid_speed(speed)
        runtime = self._runtime
        if runtime is not None:
            runtime.anchor_clock = self._clock()
            runtime.anchor_ns = (
                runtime.last_recv_ns
                if runtime.last_recv_ns is not None
                else runtime.anchor_ns
            )
        return self.state_payload()

    async def seek(self, position_ms: int) -> dict[str, Any]:
        """목표 위치로 이동한다.

        상태는 이전 패킷 누적에 의존하므로, 전이 기억을 모두 새로 만든 뒤 파일
        처음부터 `position_ms` 직전 패킷까지 지연 없이 재생해 다시 만든다.
        재구성 중에 나온 무전도 가상 시각 그대로 `MemoryRadioLog`에 쌓여,
        무전 기록·리포트가 그 시점까지의 실제 재생과 같아진다.
        """
        runtime = self._runtime
        if runtime is None:
            return self.state_payload()
        target = max(0, min(int(position_ms), int(self._duration_ms)))

        await self._cancel_task()
        await self._close_runtime(runtime)
        self._runtime = None

        rebuilt = self._build_runtime()
        self._runtime = rebuilt
        try:
            await rebuilt.pipeline.start()
            self._rebuild_to(rebuilt, target)
            self._position_ms = target
            self._finished = False
            await self._settle()
            # 재구성 뒤 파이프라인 태스크가 낸 마지막 무전까지 기록한다.
            self._drain_radio(rebuilt)
        except BaseException:
            if self._runtime is rebuilt:
                self._runtime = None
            await self._close_runtime(rebuilt)
            raise
        if self._runtime is not rebuilt:
            return self.state_payload()

        # 재생 기준점을 시크 목표 위치로 둔다. 다음 패킷까지 남은 간격을
        # 목표 위치에서부터 계산해, 긴 공백 한가운데로 시크해도 위치가 맞는다.
        rebuilt.anchor_clock = self._clock()
        rebuilt.anchor_ns = (
            rebuilt.first_recv_ns + target * 1_000_000
            if rebuilt.first_recv_ns is not None
            else 0
        )
        self._task = asyncio.create_task(
            self._play_loop(), name="f1re-web-replay"
        )
        return self.state_payload()

    # ------------------------------------------------------------------
    # 대시보드 API용 묶음

    def state_payload(self) -> dict[str, Any]:
        """`/api/replay/state`·`/ws/replay`가 보내는 상태.

        기존 `state_payload()`와 같은 구조(`snapshot`, `car`, `connection`,
        `cars`, `flag`, `flags`, `status`)에 `replay` 메타를 더한다. `connection`은
        실제 시각이 아니라 리플레이 가상 시계로 계산해, 과거 녹화를 재생해도
        "데이터 안 옴"으로 흐려지지 않게 한다.
        """
        from f1re.web.app import connection_info, state_payload

        runtime = self._runtime
        if runtime is None:
            return self._inactive_state()
        payload = state_payload(runtime.store, runtime.sink, runtime.track_map)
        snapshot = payload["snapshot"]
        payload["connection"] = connection_info(
            snapshot, now_ns=runtime.clock.now_ns
        )
        payload["replay"] = self._replay_meta(snapshot)
        return payload

    def track_payload(self) -> dict[str, Any]:
        """`/api/replay/track` 응답. 기존 `map.js`가 그대로 쓰는 모양이다."""
        runtime = self._runtime
        if runtime is None:
            return TrackMap(None).track_payload(StateStore().snapshot(), None)
        snapshot = runtime.store.snapshot()
        return runtime.track_map.track_payload(
            snapshot, runtime.pipeline.track_state()
        )

    def report_payload(self) -> dict[str, Any]:
        """`/api/replay/report` 응답. 기존 `report.js`가 그대로 쓰는 모양이다."""
        from f1re.web import report_api

        runtime = self._runtime
        if runtime is None:
            return _empty_report(report_api)
        rows = runtime.radio_log.report_source()
        entries = report_api._loss_entries(rows)
        corner_losses = report_api._corner_losses(entries)
        focus_changes = report_api._focus_changes(rows)
        improvements = report_api._message_improvements(rows)
        return {
            "available": True,
            "reason": None,
            "session_uid": self._session_uid(runtime),
            "data_source": report_api.DATA_SOURCE_RADIO_EVIDENCE,
            "message_count": len(rows),
            "corner_losses": corner_losses,
            "focus_changes": focus_changes,
            "message_improvements": improvements,
            "pipeline": report_api._pipeline_section(
                runtime.pipeline.report_state()
            ),
            "summary": report_api._summary(
                rows, corner_losses, focus_changes, improvements
            ),
        }

    def radio_log_payload(
        self, *, limit: int | None = DEFAULT_RADIO_LIMIT
    ) -> dict[str, Any]:
        """`/api/replay/radio/log` 응답. 메모리 무전 기록을 화면 모양으로 바꾼다."""
        from f1re.web import report_api

        runtime = self._runtime
        if runtime is None:
            rows: list[dict[str, Any]] = []
            muted: list[str] = []
            session_uid = None
        else:
            rows = runtime.radio_log.list_messages(limit=limit)
            muted = sorted(runtime.queue.muted_kinds)
            session_uid = self._session_uid(runtime)
        return {
            "available": True,
            "reason": None,
            "session_uid": session_uid,
            "messages": [report_api._message_payload(row) for row in rows],
            "muted_kinds": muted,
            "kinds": report_api._kind_states(muted),
        }

    # ------------------------------------------------------------------
    # 재생 루프

    async def _play_loop(self) -> None:
        """녹화를 녹화 당시 간격(÷배속)대로 흘려보낸다."""
        try:
            while True:
                if not await self._wait_if_paused():
                    return
                runtime = self._runtime
                if runtime is None:
                    return
                record = runtime.next_record()
                if record is None:
                    self._finish(runtime)
                    return
                recv_ns, data = record
                wait = self._wait_seconds(runtime, recv_ns)
                while wait > 0:
                    await self._sleep(min(wait, WAIT_SLICE_S))
                    if self._runtime is not runtime:
                        return
                    if self._paused:
                        if not await self._wait_if_paused():
                            return
                        # 멈춰 있던 실제 시간은 재생 위치에 반영하지 않는다.
                        runtime.anchor_clock = self._clock()
                        runtime.anchor_ns = (
                            runtime.last_recv_ns
                            if runtime.last_recv_ns is not None
                            else recv_ns
                        )
                    wait = self._wait_seconds(runtime, recv_ns)
                if self._runtime is not runtime:
                    return
                self._apply(runtime, recv_ns, data)
                runtime.anchor_clock = self._clock()
                runtime.anchor_ns = recv_ns
                # 대기 없이 이어지는 패킷에서도 일시정지·정지를 확인할 수 있게 양보한다.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:  # 재생 버그 하나로 관리자 전체가 멈추지 않게 한다.
            logger.exception("웹 리플레이 재생 중 오류")
            self._paused = True

    async def _wait_if_paused(self) -> bool:
        """일시정지가 풀릴 때까지 기다린다. 런타임이 사라졌으면 False."""
        while self._paused:
            if self._runtime is None:
                return False
            await self._resume.wait()
            self._resume.clear()
        return self._runtime is not None

    def _wait_seconds(self, runtime: _ReplayRuntime, recv_ns: int) -> float:
        """다음 패킷까지 실제로 기다릴 시간(초). 배속·기준점 반영."""
        if runtime.last_recv_ns is None:
            return 0.0
        gap_s = (recv_ns - runtime.anchor_ns) / 1_000_000_000.0
        target_elapsed = gap_s / self._speed
        wait = target_elapsed - (self._clock() - runtime.anchor_clock)
        # 부동소수 오차로 남은 1e-17초를 계속 자면 재생이 멈춘 것처럼 보인다.
        return wait if wait > WAIT_EPSILON_S else 0.0

    # ------------------------------------------------------------------
    # 패킷 적용

    def _apply(self, runtime: _ReplayRuntime, recv_ns: int, data: bytes) -> None:
        """패킷 하나를 상태·파이프라인·경고 규칙에 반영하고 무전을 적재한다."""
        runtime.clock.now_ns = int(recv_ns)
        try:
            packet = parse_packet(data)
        except UnsupportedPacket as exc:
            runtime.store.record_unsupported(exc, recv_ns)
            return
        except Exception as exc:  # 파서 버그도 재생을 멈추지 않게 한다.
            runtime.store.record_error(exc, recv_ns)
            return

        try:
            runtime.store.apply(packet, recv_ns, length=len(data))
            runtime.pipeline.on_packet(packet, recv_ns)
        except Exception:  # 저장소·파이프라인 오류도 다음 패킷부터 계속한다.
            logger.exception("리플레이 패킷을 처리하지 못했습니다")

        # 세션 전이 기억을 새로 만들 때 쓸 세션 UID는 첫 패킷 헤더가 정한다.
        if runtime.first_recv_ns is None:
            runtime.first_recv_ns = int(recv_ns)
            runtime.radio_log.set_session_uid(int(packet.header.session_uid))
        runtime.last_recv_ns = int(recv_ns)
        runtime.position_ms = max(0, (int(recv_ns) - runtime.first_recv_ns) // 1_000_000)
        self._position_ms = runtime.position_ms

        # 사건 큐는 스냅샷과 별개다. 리플레이는 사건을 따로 쓰지 않으므로 비워
        # 장시간 세션에서 큐가 무한히 자라지 않게 한다.
        runtime.store.events.clear()

        if (
            runtime.last_alert_ns is None
            or int(recv_ns) - runtime.last_alert_ns >= ALERT_EVAL_INTERVAL_NS
        ):
            self._evaluate_rules(runtime)
        self._drain_radio(runtime)

    def _evaluate_rules(self, runtime: _ReplayRuntime) -> None:
        """실서버 무전 서비스처럼 10Hz 가상 시계로 위험 경고를 판정한다."""
        runtime.last_alert_ns = runtime.clock.now_ns
        try:
            for message in runtime.rules.evaluate(runtime.store.snapshot()):
                runtime.queue.push(message)
        except Exception:  # 판정 버그가 재생을 멈추지 않게 한다.
            logger.exception("리플레이 위험 경고 판정 중 오류")

    def _drain_radio(self, runtime: _ReplayRuntime) -> None:
        """큐에 쌓인 무전을 메모리 기록에 적재한다(Qwen TTS·DB 없음)."""
        try:
            runtime.sink.drain()
        except Exception:  # 기록 버그가 재생을 멈추지 않게 한다.
            logger.exception("리플레이 무전을 기록하지 못했습니다")

    def _finish(self, runtime: _ReplayRuntime) -> None:
        """파일 끝. 마지막 상태를 유지한 채 재생 끝으로 표시한다."""
        self._position_ms = max(self._duration_ms, runtime.position_ms)
        self._finished = True
        self._paused = True
        self._drain_radio(runtime)

    # ------------------------------------------------------------------
    # 시크 재구성

    def _rebuild_to(self, runtime: _ReplayRuntime, position_ms: int) -> None:
        """파일 처음부터 `position_ms` 직전 패킷까지 지연 없이 재생한다."""
        target_offset_ns = max(0, int(position_ms)) * 1_000_000
        while True:
            record = runtime.next_record()
            if record is None:
                return
            recv_ns, data = record
            if runtime.first_recv_ns is None:
                # 첫 패킷의 수신 시각이 위치 0의 기준이다. 위치 0으로 시크하면
                # 이 패킷도 아직 재생 전이므로 라이브 루프에 넘긴다.
                runtime.first_recv_ns = int(recv_ns)
                runtime.anchor_ns = int(recv_ns)
                if target_offset_ns <= 0:
                    runtime.pending_record = record
                    return
            if int(recv_ns) - runtime.first_recv_ns >= target_offset_ns:
                runtime.pending_record = record
                return
            self._apply(runtime, recv_ns, data)

    async def _settle(self) -> None:
        """재구성으로 큐에 쌓인 완료 랩을 `RacePipeline` 태스크가 처리하게 한다."""
        for _ in range(SEEK_SETTLE_YIELDS):
            await asyncio.sleep(0)

    # ------------------------------------------------------------------
    # 런타임 수명

    def _build_runtime(self) -> _ReplayRuntime:
        """메모리 전용 런타임을 새로 만든다(DB 쓰기·TTS 없음)."""
        if self._file_path is None:
            raise RuntimeError("녹화 파일 경로가 없습니다")
        clock = _VirtualClock()
        store = StateStore()
        radio_log = MemoryRadioLog(session_uid=_as_int(self._meta.get("session_uid")))
        queue = RadioQueue(clock=clock)
        sink = MemoryRadioSink(queue, radio_log, clock_ns=clock, sleep=self._sleep)
        conn = self._open_conn()
        if self._map_factory is not None:
            track_map = self._map_factory()
        else:
            # 윤곽은 읽기만 한다. DB가 없거나 못 열면 데이터 파일 윤곽만 쓴다.
            track_map = TrackMap(conn)
        pipeline = RacePipeline(store, sink, None, config=self._config, clock=clock)
        radio_config = self._config.radio
        rules = AlertRules(
            clock=clock,
            safety_ttl_ns=_seconds_to_ns(radio_config.safety_ttl_s),
            box_ttl_ns=_seconds_to_ns(radio_config.box_ttl_s),
        )
        return _ReplayRuntime(
            clock=clock,
            store=store,
            track_map=track_map,
            conn=conn,
            radio_log=radio_log,
            queue=queue,
            sink=sink,
            pipeline=pipeline,
            rules=rules,
            records=iter_recording(self._file_path),
            anchor_clock=self._clock(),
        )

    def _open_conn(self) -> sqlite3.Connection | None:
        """지도 윤곽 조회용 읽기 전용 연결. 실패해도 재생은 계속한다."""
        if self._conn_factory is None:
            return None
        try:
            return self._conn_factory()
        except Exception:  # DB 파일이 없거나 잠겨 있어도 지도 없이 재생한다.
            logger.warning("리플레이 지도용 DB를 열지 못했습니다", exc_info=True)
            return None

    async def _teardown(self) -> None:
        """재생 태스크·런타임을 정리한다."""
        await self._cancel_task()
        runtime = self._runtime
        self._runtime = None
        if runtime is not None:
            await self._close_runtime(runtime)
        self._resume.set()

    async def _cancel_task(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # 재생 루프는 이미 기록했다. 정리는 계속한다.
            logger.exception("웹 리플레이 태스크를 정리하지 못했습니다")

    async def _close_runtime(self, runtime: _ReplayRuntime) -> None:
        close_records = getattr(runtime.records, "close", None)
        if callable(close_records):
            close_records()
        try:
            # 파이프라인이 닫히며 내는 마지막 무전까지 기록한 뒤 싱크를 닫는다.
            await runtime.pipeline.aclose()
        except Exception:
            logger.exception("리플레이 파이프라인을 닫지 못했습니다")
        try:
            await runtime.sink.aclose()
        except Exception:
            logger.exception("리플레이 무전 기록기를 닫지 못했습니다")
        close_map = getattr(runtime.track_map, "aclose", None)
        if callable(close_map):
            with contextlib.suppress(Exception):
                await close_map()
        if runtime.conn is not None:
            with contextlib.suppress(sqlite3.Error):
                runtime.conn.close()

    # ------------------------------------------------------------------
    # 메타

    def _session_uid(self, runtime: _ReplayRuntime | None) -> int | None:
        if runtime is not None:
            uid = runtime.store.snapshot().get("session_uid")
            if isinstance(uid, int) and not isinstance(uid, bool):
                return uid
            resolved = runtime.radio_log.session_uid
            if resolved is not None:
                return resolved
        return _as_int(self._meta.get("session_uid"))

    def _replay_meta(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        uid = _as_int(self._meta.get("session_uid"))
        if uid is None:
            uid = snapshot.get("session_uid")
        track_id = _as_int(self._meta.get("track_id"))
        if track_id is None:
            session = (snapshot.get("packets") or {}).get("session") or {}
            track_id = _as_int(session.get("track_id"))
        return {
            "active": self._runtime is not None,
            "recording_id": self._recording_id,
            "session_uid": uid,
            "paused": self._paused,
            "speed": self._speed,
            "position_ms": int(self._position_ms),
            "duration_ms": int(self._duration_ms),
            "category": self._meta.get("category"),
            "track_id": track_id,
        }

    def _inactive_state(self) -> dict[str, Any]:
        """리플레이가 없을 때의 상태(기존 구조 + 비활성 메타)."""
        from f1re.web.app import state_payload

        sink = MemoryRadioSink(RadioQueue(), MemoryRadioLog())
        payload = state_payload(StateStore(), sink)
        payload["replay"] = dict(_INACTIVE_REPLAY)
        return payload


# ---------------------------------------------------------------------------
# 도우미


def _valid_speed(value: Any) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        raise ValueError("speed는 0보다 큰 유한한 값이어야 합니다") from None
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("speed는 0보다 큰 유한한 값이어야 합니다")
    return speed


def _recording_id_from_meta(meta: Mapping[str, Any]) -> int | None:
    """`session_recordings` 행의 식별자. `id`(DB 열) 또는 `recording_id`."""
    for key in ("id", "recording_id"):
        value = _as_int(meta.get(key))
        if value is not None:
            return value
    return None


def _duration_from_meta(meta: Mapping[str, Any], path: Path) -> int:
    """녹화 길이(ms). DB 메타에 없으면 파일을 스캔해 센다."""
    value = _as_int(meta.get("duration_ms"))
    if value is not None and value >= 0:
        return value
    _, duration_ms = scan_recording(path)
    return duration_ms


def _empty_report(report_api: Any) -> dict[str, Any]:
    """리플레이가 없을 때 `/api/replay/report`가 돌려줄 빈 리포트."""
    return {
        "available": False,
        "reason": "리플레이 중이 아닙니다",
        "session_uid": None,
        "data_source": report_api.DATA_SOURCE_RADIO_EVIDENCE,
        "message_count": 0,
        "corner_losses": [],
        "focus_changes": [],
        "message_improvements": [],
        "pipeline": report_api._pipeline_section(None),
        "summary": report_api._summary([], [], [], []),
    }


def _seconds_to_ns(seconds: float) -> int:
    return int(round(float(seconds) * 1_000_000_000))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


__all__ = [
    "ALERT_EVAL_INTERVAL_NS",
    "DEFAULT_RADIO_LIMIT",
    "ReplayManager",
    "SEEK_SETTLE_YIELDS",
    "WAIT_SLICE_S",
]
