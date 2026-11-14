"""실서버 파이프라인(SCRUM-84): 수신 패킷 → 랩 기록 → 코칭·추월·리프트 앤 코스트.

`f1re serve`가 엔진을 켤 때 이 객체를 만들어 UDP 수신기에 `on_packet`으로
붙인다. 지금까지 단위 테스트만 있던 모듈을 실제 서버 경로에 연결한다.

- **랩 기록**: 패킷마다 `LapTraceRecorder.on_packet`으로 차 22대의 랩 샘플을
  쌓는다(작업 14). 완료 랩은 짧은 큐에 넣고 이벤트 루프의 별도 태스크가
  처리한다. 무거운 계산(기준 랩 갱신·코너 지표·DB 저장)을 수신 핸들러에서
  바로 하지 않아 60Hz 수신이 밀리지 않는다(코칭 결정 100ms 목표).
- **코칭·집중 코너·개선 알림**: 완료 랩마다 `Coach.on_lap_completed`(작업 16)
  로 기준 랩(`ReferenceSet`)·집중 코너(`FocusSet`)를 갱신하고, Car Telemetry
  샘플마다 `Coach.on_sample`로 코너 탈출 직후 무전을 만든다.
- **리프트 앤 코스트**: 같은 샘플로 `LiftCoastCoach.on_sample`(SCRUM-71).
- **추월**: 10Hz 태스크가 `OvertakeAdvisor.evaluate`(작업 18)를 부른다.
- **무전**: 세 모듈 모두 `RadioQueue`를 직접 받아 `push()`한다(설계 6.7의
  우선순위·끄기 규칙은 큐가 맡는다).
- **세션**: Session 패킷에서 트랙·세션 종류·날씨를 읽어 `load_track`(없으면
  자동 탐지 경로), `ReferenceStore.load_compatible`(트랙·모드·날씨·차)로
  저장된 기준·집중 코너를 불러오고, 코칭 문장을 `VoiceCache.prefetch`로 미리
  만든다. 랩 완료·세션 끝·엔진 OFF에 `ReferenceStore.save`로 저장한다.
- **세션 재시작·플래시백**(리뷰 포커스 1): `sessionUID`가 바뀌면 코치·기준·
  집중 코너·기록기를 모두 새로 만든다. 프레임 되감기·랩 거리 후진·FLBK는 그
  랩을 무효로 표시해 코칭·기준 랩에 쓰지 않는다. 프로그램은 멈추지 않는다.
- **코너 데이터 없는 서킷**(리뷰 포커스 5): 첫 유효 랩에서 `detect_corners`로
  코너를 자동 탐지해 `official=False` 트랙을 만들고, 학습 윤곽을 DB에 저장한다.

`RacePipeline.track_state()`·`report_state()`·`lift_state()`가 웹 API
(`/api/track`·`/api/report`·`/api/lift-coast`)에 이번 세션의 실제 상태를 준다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from f1re.coaching.coach import Coach, CoachConfig, metric_values
from f1re.coaching.focus import FocusConfig, corner_loss_ms, lap_time_points
from f1re.coaching.lift_coast import LiftCoastCoach
from f1re.coaching.metrics import (
    CoachingThresholds,
    CornerMetrics,
    corner_metrics,
    default_coaching_thresholds,
)
from f1re.coaching.reference import (
    SOURCE_FASTEST_CAR,
    SOURCE_SELF,
    ReferenceSet,
    trace_outline,
)
from f1re.coaching.store import (
    MODE_RACE_WEEKEND,
    ReferenceKey,
    ReferenceStore,
    mode_for_session_type,
)
from f1re.config import AppConfig
from f1re.laps.traces import LAP_DISTANCE_TOLERANCE_M, LapTrace, LapTraceRecorder
from f1re.overtake.advisor import OvertakeAdvisor, OvertakeConfig
from f1re.radio.queue import RadioQueue
from f1re.tracks.learn import remember_outline, resolve_outline
from f1re.tracks.model import (
    Corner,
    Track,
    TrackDataError,
    detect_corners,
    load_track,
)
from f1re.tracks.outline import TrackOutline
from f1re.udp.parser import Packet
from f1re.udp.spec import PacketId

logger = logging.getLogger(__name__)

# 추월 판단 주기(초, 설계 6.5·6.9). 코칭 결정 100ms 목표와 같은 수준이다.
OVERTAKE_INTERVAL_S = 0.1

# 완료 랩 처리 큐 크기. 수신이 처리보다 빠르면 가장 오래된 항목을 버려
# 수신 루프를 막지 않는다(설계 6.8 성능).
LAP_QUEUE_MAX = 64

# 트랙을 아직 모를 때(코너 자동 탐지 전) 보관할 완료 랩 수.
PENDING_TRACE_MAX = 64

# 서킷 데이터가 없을 때 자동 탐지 코너를 만들 최소 샘플 수.
MIN_DETECT_SAMPLES = 10

# 기준 랩 캐시(궤적·지표·랩 시각) 최대 항목 수. 넘으면 비운다.
REFERENCE_CACHE_MAX = 256

# 완료 랩 처리 큐에 넣는 항목 종류.
_ITEM_LAPS = "laps"
_ITEM_SAVE = "save"


class RacePipeline:
    """엔진이 켜져 있는 동안 수신 패킷을 코칭·추월·무전으로 잇는다.

    `on_packet`은 UDP 수신 스레드가 아니라 이벤트 루프에서 불린다(asyncio
    datagram 콜백). 그래서 여기서 만든 무전은 곧바로 `RadioQueue`에 들어간다.
    웹 API는 다른 스레드(요청 처리 스레드풀)에서 `track_state`·`report_state`를
    읽으므로 그 두 메서드는 잠금으로 복사본을 만든다.
    """

    def __init__(
        self,
        store: Any,
        radio: Any,
        conn: Any | None = None,
        *,
        config: AppConfig | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._store = store
        self._radio = radio
        self._conn = conn
        self._config = config or AppConfig()
        self._clock = clock
        queue = getattr(radio, "queue", None)
        self._radio_queue: RadioQueue = queue if queue is not None else RadioQueue()

        # 세션·트랙 상태(이벤트 루프 스레드만 쓴다).
        self._session_uid: int | None = None
        self._generation = 0
        self._track_id: int | None = None
        self._track: Track | None = None
        self._auto_track: Track | None = None
        self._track_length: float | None = None
        self._session_type: int | None = None
        self._weather: int | None = None
        self._total_laps: int | None = None
        self._team_id: int | None = None
        self._player_index: int | None = None
        self._outline: TrackOutline | None = None

        self._lap_recorder = LapTraceRecorder()
        self._coach: Coach | None = None
        self._lift_coach: LiftCoastCoach | None = None
        self._advisor: OvertakeAdvisor | None = None
        self._advisor_active = False
        self._reference_store = (
            ReferenceStore(conn) if conn is not None else None
        )
        self._key: ReferenceKey | None = None
        self._conditions_match = True
        self._thresholds: CoachingThresholds = default_coaching_thresholds()

        # 최신 패킷 본문(추월 스냅샷·내 차 상태 만들기). 복사하지 않고 참조만 둔다.
        self._latest_session: dict[str, Any] | None = None
        self._latest_lap_body: dict[str, Any] | None = None
        self._latest_status_body: dict[str, Any] | None = None
        self._player_lap: dict[str, Any] | None = None
        self._player_status: dict[str, Any] | None = None
        self._player_motion: dict[str, Any] | None = None

        # 내 차 랩 무효 표시(플래시백·일시정지·게임 표시).
        self._player_lap_number: int | None = None
        self._player_last_distance: float | None = None
        self._invalid_reasons: set[str] = set()
        self._max_frame: int | None = None

        self._pending_traces: list[LapTrace] = []
        self._traces_by_car: dict[int, list[LapTrace]] = {}
        # 웹 API가 읽는 복사본. `_state_lock`으로 보호한다.
        self._state_lock = threading.Lock()
        self._focus_corners: list[int] = []
        self._focus_losses: dict[str, int] = {}
        self._corner_metrics: dict[str, dict[str, Any]] = {}
        self._reference_info: dict[str, Any] = {}

        # 기준 랩 계산 캐시(랩 객체 id 기준).
        self._ref_path_cache: dict[int, tuple[LapTrace, TrackOutline | None]] = {}
        self._ref_points_cache: dict[int, tuple[LapTrace, tuple[tuple[float, float], ...]]] = {}
        self._ref_metrics_cache: dict[
            int, tuple[LapTrace, dict[int, CornerMetrics]]
        ] = {}

        self._lap_queue: asyncio.Queue[tuple[str, Any]] | None = None
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------
    # 수명 주기

    async def start(self) -> None:
        """완료 랩 처리·추월 판단 태스크를 띄운다(엔진 ON)."""
        if self._tasks:
            return
        self._lap_queue = asyncio.Queue(maxsize=LAP_QUEUE_MAX)
        self._tasks = [
            asyncio.create_task(self._lap_loop(), name="f1re-pipeline-laps"),
            asyncio.create_task(
                self._overtake_loop(), name="f1re-pipeline-overtake"
            ),
        ]

    async def aclose(self) -> None:
        """대기 중인 랩을 마저 처리하고 기준 랩을 저장한 뒤 정리한다."""
        queue, self._lap_queue = self._lap_queue, None
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if queue is not None:
            # 종료 직전에 들어온 완료 랩도 버리지 않고 처리한다.
            while True:
                try:
                    kind, payload = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if kind == _ITEM_LAPS:
                    generation, batch = payload
                    if generation == self._generation:
                        await self._process_laps(batch)
        self._save_reference()
        self._clear_session_state()
        self._advisor = None

    # ------------------------------------------------------------------
    # 수신 패킷

    def on_packet(self, pkt: Packet, recv_ns: int) -> None:
        """UDP 데이터그램 하나를 파이프라인에 반영한다. 예외를 밖으로 내지 않는다."""
        try:
            self._handle_packet(pkt, int(recv_ns))
        except Exception:  # 파이프라인 버그가 수신을 멈추지 않게 한다.
            logger.exception("레이스 파이프라인 처리 중 오류")

    def _handle_packet(self, pkt: Packet, recv_ns: int) -> None:
        header = pkt.header
        if header.session_uid != self._session_uid:
            if self._session_uid is None:
                self._session_uid = header.session_uid
            else:
                self._restart_session(header.session_uid)

        player = header.player_car_index
        if 0 <= player < 22:
            self._player_index = player

        frame = header.overall_frame_id
        if self._max_frame is not None and frame < self._max_frame:
            self._mark_invalid("flashback")
        elif self._max_frame is None or frame > self._max_frame:
            self._max_frame = frame

        completed = self._lap_recorder.on_packet(pkt)
        if completed:
            # 세대(generation)를 함께 넣어 세션이 바뀐 뒤 도착한 옛 랩을 거른다.
            self._enqueue(_ITEM_LAPS, (self._generation, completed))

        try:
            packet_id = PacketId(header.packet_id)
        except ValueError:
            return
        body = pkt.body
        if packet_id is PacketId.SESSION:
            self._on_session(body)
        elif packet_id is PacketId.LAP_DATA:
            self._on_lap_data(body)
        elif packet_id is PacketId.CAR_TELEMETRY:
            self._on_car_telemetry(body, recv_ns)
        elif packet_id is PacketId.CAR_STATUS:
            self._on_car_status(body)
        elif packet_id is PacketId.MOTION:
            self._on_motion(body)
        elif packet_id is PacketId.PARTICIPANTS:
            self._on_participants(body)
        elif packet_id is PacketId.EVENT:
            self._on_event(body)

    # ------------------------------------------------------------------
    # 세션·트랙

    def _restart_session(self, uid: int) -> None:
        """sessionUID가 바뀌었다. 이전 세션 기준을 저장하고 상태를 모두 버린다."""
        self._save_reference()
        self._generation += 1
        self._session_uid = uid
        self._lap_recorder = LapTraceRecorder()
        self._clear_session_state()
        self._max_frame = None

    def _clear_session_state(self) -> None:
        """세션에 묶인 코치·기준·캐시를 비운다(세션 재시작·엔진 OFF)."""
        self._coach = None
        self._lift_coach = None
        self._advisor = None
        self._advisor_active = False
        self._key = None
        self._conditions_match = True
        self._track = None
        self._auto_track = None
        self._track_id = None
        self._track_length = None
        self._session_type = None
        self._weather = None
        self._total_laps = None
        self._team_id = None
        self._player_index = None
        self._outline = None
        self._pending_traces.clear()
        self._traces_by_car.clear()
        self._latest_session = None
        self._latest_lap_body = None
        self._latest_status_body = None
        self._player_lap = None
        self._player_status = None
        self._player_motion = None
        self._player_lap_number = None
        self._player_last_distance = None
        self._invalid_reasons.clear()
        self._ref_path_cache.clear()
        self._ref_points_cache.clear()
        self._ref_metrics_cache.clear()
        with self._state_lock:
            self._focus_corners = []
            self._focus_losses = {}
            self._corner_metrics = {}
            self._reference_info = {}

    def _on_session(self, body: dict[str, Any]) -> None:
        self._latest_session = body
        track_id = _int(body.get("track_id"))
        if (
            track_id is not None
            and track_id >= 0
            and track_id != self._track_id
        ):
            self._track_id = track_id
            self._track = None
            self._auto_track = None
            self._coach = None
            self._lift_coach = None
            self._advisor = None
            self._outline, _source = resolve_outline(track_id, self._conn)
            self._lap_recorder.set_outline(self._outline)

        session_type = _int(body.get("session_type"))
        if session_type is not None and session_type != self._session_type:
            self._session_type = session_type
            # 세션 종류가 바뀌면 코치를 새로 만든다(세션 UID 변경이 없는 경우 대비).
            self._coach = None
            self._lift_coach = None
            self._advisor = None

        weather = _int(body.get("weather"))
        if weather is not None:
            self._weather = weather
        total_laps = _int(body.get("total_laps"))
        if total_laps is not None:
            self._total_laps = total_laps
        length = _float(body.get("track_length"))
        if length is not None and length > 0:
            self._track_length = length

        if _int(body.get("game_paused")):
            self._mark_invalid("paused")

        self._maybe_configure()

    def _on_participants(self, body: dict[str, Any]) -> None:
        index = self._player_index
        entries = body.get("participants")
        if index is None or not isinstance(entries, list) or index >= len(entries):
            return
        entry = entries[index]
        if not isinstance(entry, dict):
            return
        team_id = _int(entry.get("team_id"))
        if team_id is None or team_id == self._team_id:
            return
        self._team_id = team_id
        self._maybe_configure()

    def _maybe_configure(self) -> None:
        """세션 정보가 모이면 트랙·기준·코치·추월 판단을 만든다."""
        if self._coach is not None:
            return
        if (
            self._session_uid is None
            or self._player_index is None
            or self._track_id is None
            or self._session_type is None
        ):
            return
        # 레이스 주말 기준 키에는 내 차 팀이 필요하다. 참가자 패킷을 기다린다.
        if (
            mode_for_session_type(self._session_type) == MODE_RACE_WEEKEND
            and self._team_id is None
        ):
            return

        track = self._resolve_track()
        if track is None:
            return
        try:
            self._build_coaches(track)
        except Exception:  # 기준 랩 파일 손상 등. 수신은 계속한다.
            logger.exception("코칭·추월 파이프라인을 만들지 못했습니다")
            self._coach = None
            self._lift_coach = None
            self._advisor = None

    def _resolve_track(self) -> Track | None:
        """데이터 파일 트랙, 없으면 자동 탐지로 만든 트랙."""
        if self._track is not None:
            return self._track
        track: Track | None = None
        if self._track_id is not None:
            try:
                track = load_track(self._track_id)
            except TrackDataError:
                logger.warning("서킷 데이터를 읽지 못했습니다: %s", self._track_id)
                track = None
        if track is None:
            track = self._auto_track
        self._track = track
        return track

    def _build_coaches(self, track: Track) -> None:
        """기준 랩을 불러와 코치·추월 판단을 만들고, 밀린 랩을 반영한다."""
        player = int(self._player_index or 0)
        references: ReferenceSet | None = None
        focus_seed: tuple[int, ...] = ()
        if self._reference_store is not None and self._track_id is not None:
            key = ReferenceKey.from_session(
                track_id=self._track_id,
                session_type=self._session_type,
                weather_code=self._weather,
                team_id=self._team_id,
            )
            self._key = key
            try:
                stored = self._reference_store.load_compatible(key)
            except Exception:
                logger.exception("저장된 기준 랩을 읽지 못했습니다")
                stored = None
            if stored is not None:
                self._conditions_match = stored.conditions_match
                references = _adopt_reference(
                    stored.reference, track, player, self._session_type
                )
                focus_seed = stored.focus
        if references is None:
            self._conditions_match = True
            references = ReferenceSet(
                track.corners,
                player_car_index=player,
                session_type=self._session_type,
            )

        coach = Coach(
            track,
            references=references,
            queue=self._radio_queue,
            config=CoachConfig.from_app_config(self._config),
            focus_config=FocusConfig.from_app_config(self._config),
            thresholds=self._thresholds,
            player_car_index=player,
            session_type=self._session_type,
        )
        if focus_seed:
            coach.seed_focus(focus_seed)
        lift = LiftCoastCoach(
            track,
            queue=self._radio_queue,
            coach_config=CoachConfig.from_app_config(self._config),
            thresholds=self._thresholds,
            player_car_index=player,
            session_type=self._session_type,
        )
        self._coach = coach
        self._lift_coach = lift
        self._advisor = OvertakeAdvisor(
            OvertakeConfig.from_app_config(self._config),
            queue=self._radio_queue,
            track=track,
        )
        self._advisor_active = False

        # 트랙을 알기 전에 끝난 랩을 반영한다(코너 자동 탐지 직후 등).
        for trace in self._pending_traces:
            if not trace.valid or len(trace.samples) < 2:
                continue
            coach.references.update(trace)
            if trace.car_index == player:
                coach.focus.update([trace])
                lift.on_lap_completed(trace, self._clock())
        self._pending_traces.clear()
        coach.references.prune()
        self._refresh_caches()

        # 그 서킷 코칭 문장을 미리 만들어 둔다(캐시가 있을 때만, 네트워크).
        cache = getattr(self._radio, "cache", None)
        if cache is not None:
            task = asyncio.create_task(
                self._prefetch(coach, cache), name="f1re-pipeline-prefetch"
            )
            self._tasks.append(task)

    async def _prefetch(self, coach: Coach, cache: Any) -> None:
        try:
            voice = getattr(getattr(self._config, "tts", None), "voice", None)
            result = await coach.prefetch(cache, voice)
            logger.info("코칭 문장 프리페치 완료: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:  # 음성 API 실패가 세션 시작을 막지 않는다.
            logger.warning("코칭 문장 프리페치를 마치지 못했습니다", exc_info=True)

    # ------------------------------------------------------------------
    # 패킷별 상태

    def _on_lap_data(self, body: dict[str, Any]) -> None:
        self._latest_lap_body = body
        index = self._player_index
        entries = body.get("lap_data")
        if index is None or not isinstance(entries, list) or index >= len(entries):
            return
        entry = entries[index]
        if not isinstance(entry, dict):
            return
        self._player_lap = entry

        lap = _int(entry.get("current_lap_num"))
        if lap is not None and lap > 0:
            if self._player_lap_number is None or lap > self._player_lap_number:
                self._player_lap_number = lap
                self._invalid_reasons.clear()
                self._player_last_distance = None
            elif lap < self._player_lap_number:
                self._player_lap_number = lap
                self._mark_invalid("flashback")

        distance = _float(entry.get("lap_distance"))
        if distance is not None:
            previous = self._player_last_distance
            if (
                previous is not None
                and distance + LAP_DISTANCE_TOLERANCE_M < previous
            ):
                self._mark_invalid("flashback")
            self._player_last_distance = distance

        if _int(entry.get("current_lap_invalid")):
            self._mark_invalid("invalid_lap")

    def _on_car_status(self, body: dict[str, Any]) -> None:
        self._latest_status_body = body
        index = self._player_index
        entries = body.get("car_status_data")
        if index is None or not isinstance(entries, list) or index >= len(entries):
            return
        entry = entries[index]
        if isinstance(entry, dict):
            self._player_status = entry

    def _on_motion(self, body: dict[str, Any]) -> None:
        index = self._player_index
        entries = body.get("car_motion_data")
        if index is None or not isinstance(entries, list) or index >= len(entries):
            return
        entry = entries[index]
        if isinstance(entry, dict):
            self._player_motion = entry

    def _on_event(self, body: dict[str, Any]) -> None:
        code = str(body.get("event_string_code", "")).strip()
        if code == "FLBK":
            self._mark_invalid("flashback")
        elif code == "SEND":
            # 세션 끝. 기준 랩·집중 코너를 저장한다(설계 6.3).
            self._enqueue(_ITEM_SAVE, None)

    def _on_car_telemetry(self, body: dict[str, Any], recv_ns: int) -> None:
        coach = self._coach
        lift = self._lift_coach
        if coach is None and lift is None:
            return
        index = self._player_index
        entries = body.get("car_telemetry_data")
        if index is None or not isinstance(entries, list) or index >= len(entries):
            return
        telemetry = entries[index]
        if not isinstance(telemetry, dict):
            return

        car_state = self._car_state(telemetry)
        if car_state is None:
            return
        distance = _float(car_state.get("lap_distance"))
        if distance is None:
            return
        now = int(recv_ns)
        if coach is not None:
            coach.on_sample(car_state, distance, now)
        if lift is not None:
            lift.on_sample(car_state, distance, now)

    def _car_state(self, telemetry: Mapping[str, Any]) -> dict[str, Any] | None:
        """Car Telemetry·Lap Data·Status·Motion을 코치 입력 하나로 묶는다."""
        lap = self._player_lap or {}
        status = self._player_status or {}
        motion = self._player_motion or {}

        speed = _float(telemetry.get("speed"))
        throttle = _float(telemetry.get("throttle"))
        brake = _float(telemetry.get("brake"))
        steer = _float(telemetry.get("steer"))
        gear = _int(telemetry.get("gear"))
        if None in (speed, throttle, brake, steer, gear):
            return None

        lap_number = _int(lap.get("current_lap_num"))
        remaining_race_laps: float | None = None
        if self._total_laps and lap_number:
            remaining_race_laps = float(max(self._total_laps - lap_number + 1, 0))
        fuel_remaining = _float(status.get("fuel_remaining_laps"))
        fuel_margin = (
            fuel_remaining - remaining_race_laps
            if fuel_remaining is not None and remaining_race_laps is not None
            else None
        )
        safety_car_status = (
            _int(self._latest_session.get("safety_car_status"))
            if self._latest_session
            else None
        )
        invalid = bool(self._invalid_reasons)

        return {
            "is_player": True,
            "car_index": self._player_index,
            "session_type": self._session_type,
            "lap_num": lap_number,
            "lap_distance": _float(lap.get("lap_distance")),
            "t_ms": _int(lap.get("current_lap_time_in_ms")),
            "speed": speed,
            "throttle": throttle,
            "brake": brake,
            "steer": steer,
            "gear": gear,
            "x": _float(motion.get("world_position_x")) or 0.0,
            "z": _float(motion.get("world_position_z")) or 0.0,
            "lap_valid": not invalid,
            "invalid_reason": next(iter(self._invalid_reasons), None),
            "in_pit": _int(lap.get("pit_status")) in (1, 2),
            "driver_status": _int(lap.get("driver_status")),
            "pit_status": _int(lap.get("pit_status")),
            "safety_car": safety_car_status == 1,
            "vsc": safety_car_status == 2,
            "fuel_kg": _float(status.get("fuel_in_tank")),
            "fuel_remaining_laps": fuel_remaining,
            "remaining_race_laps": remaining_race_laps,
            "total_laps": self._total_laps,
            # 감정 단계(SCRUM-91): 마지막 3랩이면 코칭·리프트가 격앙 표현을 쓴다.
            "remaining_laps": remaining_race_laps,
            "fuel_laps_margin": fuel_margin,
            "brake_temp": _max_number(telemetry.get("brakes_temperature")),
            "brake_temps": _number_list(telemetry.get("brakes_temperature")),
            "tyre_temp": _max_front_tyre_temp(telemetry),
            "tyre_temps": _number_list(telemetry.get("tyres_surface_temperature")),
        }

    def _mark_invalid(self, reason: str) -> None:
        self._invalid_reasons.add(reason)

    # ------------------------------------------------------------------
    # 완료 랩 처리(별도 태스크)

    def _enqueue(self, kind: str, payload: Any) -> None:
        queue = self._lap_queue
        if queue is None:
            return
        try:
            queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            # 수신이 우선이다. 가장 오래된 항목을 버리고 새 항목을 넣는다.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait((kind, payload))
            except asyncio.QueueFull:  # pragma: no cover - 경합 시에만.
                logger.warning("완료 랩 큐가 가득 차 항목을 버렸습니다")

    async def _lap_loop(self) -> None:
        queue = self._lap_queue
        if queue is None:
            return
        while True:
            kind, payload = await queue.get()
            try:
                if kind == _ITEM_LAPS:
                    generation, batch = payload
                    if generation != self._generation:
                        continue  # 세션이 바뀌기 전에 끝난 랩이다.
                    await self._process_laps(batch)
                elif kind == _ITEM_SAVE:
                    self._save_reference()
            except asyncio.CancelledError:
                raise
            except Exception:  # 랩 하나의 오류로 코칭 전체가 멈추지 않게 한다.
                logger.exception("완료 랩 처리 중 오류")

    async def _process_laps(self, batch: Sequence[LapTrace]) -> None:
        for trace in batch:
            # 세션이 바뀌며 마감된 랩은 새 세션 기준에 넣지 않는다.
            if trace.invalid_reason == "session_restart":
                continue
            self._remember_trace(trace)
            if self._coach is None:
                self._maybe_detect_track(trace)
            coach = self._coach
            if coach is None:
                if trace.valid and len(trace.samples) >= 2:
                    self._pending_traces.append(trace)
                    if len(self._pending_traces) > PENDING_TRACE_MAX:
                        self._pending_traces.pop(0)
                continue

            now = int(self._clock())
            if trace.car_index == self._player_index:
                coach.on_lap_completed(trace, now)
                if self._lift_coach is not None:
                    self._lift_coach.on_lap_completed(trace, now)
                if trace.valid:
                    await self._update_corner_metrics(trace)
                    self._save_reference()
                coach.references.prune()
                self._refresh_caches()
            else:
                # 다른 차 랩은 최속차 기준으로만 쓴다. 집중 코너·미뤄 둔 무전은
                # 내 차 랩에만 걸리므로 무거운 집중 코너 재계산을 하지 않는다.
                coach.add_reference_lap(trace)
                coach.references.prune()
                self._refresh_reference_info()

    def _remember_trace(self, trace: LapTrace) -> None:
        """추월 판단이 쓸 차별 최신 유효 랩만 남긴다(메모리 절약)."""
        if not trace.valid or len(trace.samples) < 2:
            return
        self._traces_by_car[trace.car_index] = [trace]
        if len(self._traces_by_car) > 22:
            # 알 수 없는 차 인덱스가 섞여도 무한히 늘지 않게 한다.
            for car_index in list(self._traces_by_car)[:-22]:
                self._traces_by_car.pop(car_index, None)

    def _maybe_detect_track(self, trace: LapTrace) -> None:
        """코너 데이터가 없는 서킷이면 첫 유효 랩에서 코너를 자동 탐지한다."""
        if self._track is not None or self._auto_track is not None:
            return
        if trace.car_index != self._player_index:
            return
        if not trace.valid or len(trace.samples) < MIN_DETECT_SAMPLES:
            return
        length = self._track_length
        if length is None or not math.isfinite(length) or length <= 0:
            length = max(
                (sample.dist_m for sample in trace.samples), default=0.0
            ) + 10.0
        corners = detect_corners(trace, length)
        if not corners:
            return
        self._auto_track = Track(
            track_id=-1 if self._track_id is None else self._track_id,
            name=f"트랙 {self._track_id if self._track_id is not None else '?'}",
            length_m=length,
            corners=corners,
        )
        if self._conn is not None and self._track_id is not None:
            outline = remember_outline(self._conn, self._track_id, trace)
            if outline is not None:
                self._outline = outline
                self._lap_recorder.set_outline(outline)
        self._maybe_configure()

    async def _update_corner_metrics(self, trace: LapTrace) -> None:
        """완료 랩의 코너 지표를 기준 랩과 비교해 지도·리포트용으로 남긴다."""
        coach = self._coach
        track = self._track
        if coach is None or track is None:
            return
        points = lap_time_points(trace)
        result: dict[str, dict[str, Any]] = {}
        for corner in track.corners:
            reference = coach.references.best_for(corner.number)
            if reference is None:
                continue
            ref_trace, source = reference
            ref_path = self._reference_path(ref_trace)
            try:
                player_metrics = corner_metrics(
                    trace, corner, ref_path, thresholds=self._thresholds
                )
                ref_metrics = self._reference_metrics(
                    ref_trace, corner, ref_path
                )
            except Exception:  # 지표 하나가 이상해도 나머지를 계속 계산한다.
                logger.exception("코너 %s 지표를 계산하지 못했습니다", corner.number)
                continue
            result[str(corner.number)] = {
                "corner": corner.number,
                "official": corner.official,
                "lap": trace.lap,
                "loss_ms": corner_loss_ms(
                    points, self._reference_points(ref_trace), corner
                ),
                "reference_source": source,
                "conditions_match": self._conditions_match,
                "player": metric_values(player_metrics),
                "reference": metric_values(ref_metrics),
            }
            # 코너 하나마다 수신 루프에 양보한다(60Hz 수신을 막지 않게).
            await asyncio.sleep(0)

        with self._state_lock:
            self._corner_metrics = result

    def _reference_path(self, trace: LapTrace) -> TrackOutline | None:
        key = id(trace)
        cached = self._ref_path_cache.get(key)
        if cached is not None and cached[0] is trace:
            return cached[1]
        if len(self._ref_path_cache) > REFERENCE_CACHE_MAX:
            self._ref_path_cache.clear()
        path = trace_outline(trace)
        self._ref_path_cache[key] = (trace, path)
        return path

    def _reference_points(
        self, trace: LapTrace
    ) -> tuple[tuple[float, float], ...]:
        key = id(trace)
        cached = self._ref_points_cache.get(key)
        if cached is not None and cached[0] is trace:
            return cached[1]
        if len(self._ref_points_cache) > REFERENCE_CACHE_MAX:
            self._ref_points_cache.clear()
        points = lap_time_points(trace)
        self._ref_points_cache[key] = (trace, points)
        return points

    def _reference_metrics(
        self, trace: LapTrace, corner: Corner, ref_path: TrackOutline | None
    ) -> CornerMetrics:
        key = id(trace)
        cached = self._ref_metrics_cache.get(key)
        if cached is not None and cached[0] is trace:
            by_corner = cached[1]
            metrics = by_corner.get(corner.number)
            if metrics is not None:
                return metrics
        else:
            by_corner = {}
            if len(self._ref_metrics_cache) > REFERENCE_CACHE_MAX:
                self._ref_metrics_cache.clear()
            self._ref_metrics_cache[key] = (trace, by_corner)
        metrics = corner_metrics(trace, corner, ref_path, thresholds=self._thresholds)
        by_corner[corner.number] = metrics
        return metrics

    def _save_reference(self) -> None:
        """같은 조건(트랙·모드·날씨·차) 키로 기준 랩·집중 코너를 저장한다."""
        coach = self._coach
        if (
            self._reference_store is None
            or self._key is None
            or coach is None
        ):
            return
        if coach.references.best_lap(SOURCE_SELF) is None:
            return
        try:
            self._reference_store.save(
                self._key, coach.references, coach.focus
            )
        except Exception:  # DB 오류 하나로 세션이 멈추지 않게 한다.
            logger.exception("기준 랩을 저장하지 못했습니다")

    def _refresh_caches(self) -> None:
        """집중 코너·기준 랩 요약을 웹 API용 복사본으로 갱신한다."""
        self._refresh_focus()

    def _refresh_focus(self) -> None:
        coach = self._coach
        if coach is None:
            return
        corners = list(coach.focus.corners)
        try:
            losses = {
                str(int(number)): int(loss)
                for number, loss in coach.focus.losses().items()
            }
        except Exception:
            logger.exception("집중 코너 손실을 계산하지 못했습니다")
            losses = {}
        with self._state_lock:
            self._focus_corners = corners
            self._focus_losses = losses
        self._refresh_reference_info()

    def _refresh_reference_info(self) -> None:
        """기준 랩 요약(내 최고·세션 최속차 최고)만 갱신한다."""
        coach = self._coach
        if coach is None:
            return
        info: dict[str, Any] = {}
        for source in (SOURCE_SELF, SOURCE_FASTEST_CAR):
            lap = coach.references.best_lap(source)
            if lap is None:
                continue
            info[source] = {
                "lap": lap.lap,
                "lap_time_ms": int(
                    max(sample.t_ms for sample in lap.samples)
                ),
            }
        info["conditions_match"] = self._conditions_match
        with self._state_lock:
            self._reference_info = info

    # ------------------------------------------------------------------
    # 추월 판단(10Hz 태스크)

    async def _overtake_loop(self) -> None:
        while True:
            await asyncio.sleep(OVERTAKE_INTERVAL_S)
            try:
                self._evaluate_overtake()
            except asyncio.CancelledError:
                raise
            except Exception:  # 판단 버그가 파이프라인을 멈추지 않게 한다.
                logger.exception("추월 판단 중 오류")

    def _evaluate_overtake(self) -> None:
        advisor = self._advisor
        if (
            advisor is None
            or self._session_uid is None
            or self._player_index is None
            or self._latest_lap_body is None
        ):
            return
        snapshot = {
            "session_uid": self._session_uid,
            "player_car_index": self._player_index,
            "packets": {
                "session": self._latest_session or {},
                "lap_data": self._latest_lap_body,
                "car_status": self._latest_status_body or {},
            },
        }
        traces = {
            car_index: list(items)
            for car_index, items in self._traces_by_car.items()
        }
        advisor.evaluate(snapshot, traces, int(self._clock()))
        self._advisor_active = advisor.active

    # ------------------------------------------------------------------
    # 웹 API용 상태

    @property
    def session_uid(self) -> int | None:
        return self._session_uid

    @property
    def track(self) -> Track | None:
        return self._track

    @property
    def coach(self) -> Coach | None:
        return self._coach

    @property
    def lift_coach(self) -> LiftCoastCoach | None:
        return self._lift_coach

    @property
    def focus_corners(self) -> tuple[int, ...]:
        with self._state_lock:
            return tuple(self._focus_corners)

    def track_state(self) -> dict[str, Any] | None:
        """`/api/track`에 넣을 이번 세션의 코너·집중 코너·코너 지표."""
        track = self._track
        if track is None:
            return None
        with self._state_lock:
            focus_corners = list(self._focus_corners)
            focus_losses = dict(self._focus_losses)
            corner_metrics = dict(self._corner_metrics)
            reference = dict(self._reference_info)
        return {
            "track_id": track.track_id,
            "name": track.name,
            "length_m": track.length_m,
            "corners": [
                {
                    "number": corner.number,
                    "official": corner.official,
                    "entry_m": corner.entry_m,
                    "apex_m": corner.apex_m,
                    "exit_m": corner.exit_m,
                }
                for corner in track.corners
            ],
            "focus_corners": focus_corners,
            "focus_losses": focus_losses,
            "corner_metrics": corner_metrics,
            "reference": reference,
        }

    def report_state(self) -> dict[str, Any]:
        """`/api/report`에 넣을 이번 세션의 실제 코너 손실·기준 랩."""
        track = self._track
        with self._state_lock:
            focus_corners = list(self._focus_corners)
            focus_losses = dict(self._focus_losses)
            corner_metrics = dict(self._corner_metrics)
            reference = dict(self._reference_info)
        losses = sorted(
            (
                {
                    "corner": item["corner"],
                    "lap": item["lap"],
                    "loss_ms": item["loss_ms"],
                    "reference": item.get("reference_source"),
                    "conditions_match": item.get("conditions_match", True),
                }
                for item in corner_metrics.values()
            ),
            key=lambda item: (-int(item["loss_ms"]), int(item["corner"])),
        )
        return {
            "track_id": None if track is None else track.track_id,
            "track_name": None if track is None else track.name,
            "focus_corners": focus_corners,
            "focus_losses": focus_losses,
            "corner_losses": losses,
            "reference": reference,
            "overtake_active": self._advisor_active,
        }

    def lift_state(self) -> Any | None:
        """`/api/lift-coast`가 읽을 `LiftCoastState`. 코치가 없으면 None."""
        lift = self._lift_coach
        return None if lift is None else lift.state


# ---------------------------------------------------------------------------
# 도우미


def _adopt_reference(
    stored: ReferenceSet,
    track: Track,
    player_car_index: int,
    session_type: int | None,
) -> ReferenceSet:
    """저장된 기준 랩을 이번 세션의 코너·차 인덱스로 다시 만든다.

    저장된 랩의 `car_index`는 지난 세션 값이라, 내 차 인덱스가 달라졌으면
    내 최고 랩은 이번 인덱스로 옮긴다. 코너 정의는 이번 트랙 것을 쓴다.
    """
    reference = ReferenceSet(
        track.corners,
        player_car_index=player_car_index,
        session_type=session_type,
    )
    for source in (SOURCE_SELF, SOURCE_FASTEST_CAR):
        trace = stored.best_lap(source)
        if trace is None:
            continue
        if source == SOURCE_SELF and trace.car_index != player_car_index:
            trace = dataclasses.replace(trace, car_index=player_car_index)
        elif (
            source == SOURCE_FASTEST_CAR
            and trace.car_index == player_car_index
        ):
            # 지난 세션 최속차가 이번 내 차 인덱스와 겹치면 다른 칸으로 옮긴다.
            trace = dataclasses.replace(
                trace, car_index=(player_car_index + 1) % 22
            )
        reference.update(trace)
    return reference


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _number_list(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)):
        return None
    numbers: list[float] = []
    for item in value:
        number = _float(item)
        if number is None:
            return None
        numbers.append(number)
    return numbers


def _max_number(value: Any) -> float | None:
    numbers = _number_list(value)
    return None if not numbers else max(numbers)


def _max_front_tyre_temp(telemetry: Mapping[str, Any]) -> float | None:
    """앞 타이어(패킷 인덱스 2·3) 표면 온도 최대값. 없으면 전체 최대값."""
    numbers = _number_list(telemetry.get("tyres_surface_temperature"))
    if not numbers:
        return None
    if len(numbers) >= 4:
        return max(numbers[2], numbers[3])
    return max(numbers)


__all__ = ["LAP_QUEUE_MAX", "OVERTAKE_INTERVAL_S", "RacePipeline"]
