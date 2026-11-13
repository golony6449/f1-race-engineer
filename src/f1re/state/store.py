"""UDP 패킷을 최신 상태·사건·진단으로 바꿔 쌓는 저장소.

`StateStore.apply`는 패킷 종류별 최신 body를 그대로 보관하고(차 22대 배열
포함), Event 패킷과 상태 변화에서 사건을 만든다. 사건은 반환값과
`StateStore.events` 큐 양쪽에 들어간다.

Review Focus 1(세션 재시작·플래시백·일시정지)에 따라 다음을 감지하면
진행 중인 랩을 무효로 표시한다. 수신 루프는 멈추지 않는다.

- `sessionUID` 변경 → `SESSION_RESTART`(세션 종료 뒤 새 세션이면
  `SESSION_START`)와 상태 초기화
- `frameIdentifier` 되감기 또는 Event FLBK → `FLASHBACK`
- 같은 랩에서 `lapDistance`가 뒤로 감 → `FLASHBACK`
- Session의 `game_paused`가 1이 됨 → 진행 중 랩 무효(사건은 만들지 않음)

진단에는 초당 패킷 수, 마지막 패킷 ID·형식·길이, 마지막 수신 시각, 세션 UID,
미지원·오류 횟수를 둔다.
"""

from __future__ import annotations

import dataclasses
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

from f1re.state.events import Event, EventKind, EventQueue
from f1re.state.flags import flag_summary
from f1re.udp.parser import Packet, UnsupportedPacket
from f1re.udp.spec import FORMATS, PacketId

# 초당 패킷 수를 세는 창(1초).
_DIAGNOSTIC_WINDOW_NS = 1_000_000_000

# 같은 랩 안에서 이만큼(1m) 넘게 뒤로 가면 플래시백으로 본다.
_LAP_DISTANCE_TOLERANCE_M = 1.0

# FTLP의 랩 타임(float 초)과 LapData의 랩 타임(ms)을 맞출 때 허용 오차.
_LAP_TIME_TOLERANCE_MS = 2


@dataclass
class Diagnostics:
    """수신 진단. 대시보드의 "데이터 안 옴" 원인 표시에 쓴다."""

    packets_per_sec: float = 0.0
    last_packet_id: int | None = None
    last_format: int | None = None
    last_len: int | None = None
    last_recv_ns: int | None = None
    session_uid: int | None = None
    session_started_ns: int | None = None
    unsupported_count: int = 0
    error_count: int = 0
    total_packets: int = 0
    last_unsupported_reason: str | None = None
    last_unsupported_format: int | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class LapStatus:
    """차 한 대의 진행 중인 랩 상태."""

    lap: int
    valid: bool = True
    invalid_reason: str | None = None


class StateStore:
    """최신 상태·사건 큐·진단을 함께 가진 저장소. 스레드 안전하다."""

    def __init__(self) -> None:
        self.events = EventQueue()
        self.diagnostics = Diagnostics()

        self._lock = threading.RLock()
        self._packets: dict[str, dict[str, Any]] = {}
        self._session_uid: int | None = None
        self._session_marked = False
        self._session_ended = False
        self._player_car_index: int | None = None

        self._laps: dict[int, LapStatus] = {}
        self._invalid_laps: dict[tuple[int, int], str] = {}
        self._emitted_laps: set[tuple[int, int]] = set()
        self._last_lap_num: dict[int, int] = {}
        self._last_lap_distance: dict[int, float] = {}
        self._last_lap_time: dict[int, int] = {}
        self._last_pit_status: dict[int, int] = {}

        self._last_weather: int | None = None
        self._last_paused: bool | None = None
        self._paused = False
        self._last_safety_car_status: int | None = None

        # 적기(RDFL)는 해제 Event가 없어 래치로 유지한다(SCRUM-80).
        # `_red_flag_lap`은 RDFL 뒤 처음 본 내 차 랩 번호다. 그 랩을 넘어서면
        # 세션이 재개된 것으로 보고 끈다.
        self._red_flag = False
        self._red_flag_lap: int | None = None

        self._max_frame: int | None = None
        self._flashback_pending = False
        self._recv_times: deque[int] = deque()

    # ------------------------------------------------------------------
    # 패킷 적용

    def apply(
        self,
        pkt: Packet,
        recv_ns: int,
        *,
        length: int | None = None,
    ) -> list[Event]:
        """패킷 하나를 상태에 반영하고 생긴 사건을 돌려준다.

        `length`는 원본 데이터그램 길이다. 생략하면 규격 크기로 채운다.
        같은 사건은 `StateStore.events` 큐에도 들어간다.
        """
        with self._lock:
            self._note_received(recv_ns)
            events: list[Event] = []

            uid = pkt.header.session_uid
            if self._session_uid is None:
                self._begin_session(uid, recv_ns)
                events.append(self._session_start_event(recv_ns, "first_packet"))
            elif uid != self._session_uid:
                previous_uid = self._session_uid
                was_ended = self._session_ended
                self._reset_session(uid, recv_ns)
                if was_ended:
                    events.append(self._session_start_event(recv_ns, "after_session_end"))
                else:
                    events.append(
                        self._event(
                            EventKind.SESSION_RESTART,
                            recv_ns,
                            data={"previous_session_uid": previous_uid},
                        )
                    )

            index = pkt.header.player_car_index
            self._player_car_index = index if 0 <= index < 22 else None

            diagnostics = self.diagnostics
            diagnostics.last_packet_id = int(pkt.header.packet_id)
            diagnostics.last_format = pkt.header.packet_format
            diagnostics.last_len = (
                length if length is not None else self._format_length(pkt)
            )
            diagnostics.last_recv_ns = recv_ns
            diagnostics.session_uid = uid

            events.extend(self._track_frame(pkt.header.overall_frame_id, recv_ns))

            name = self._packet_name(pkt)
            # `parse_packet`이 매번 새로 만든 body라 보관만 하면 된다.
            # 복사하면 초당 수천 패킷에서 비용이 크다. `snapshot`이 새 구조를 만든다.
            self._packets[name] = pkt.body
            if name == "event":
                events.extend(self._handle_event(pkt.body, recv_ns))
            elif name == "lap_data":
                events.extend(self._handle_lap_data(pkt.body, recv_ns))
            elif name == "session":
                events.extend(self._handle_session(pkt.body, recv_ns))

            for event in events:
                self.events.push(event)
            return events

    def record_unsupported(self, exc: UnsupportedPacket, recv_ns: int) -> None:
        """파서가 거부한 패킷을 진단에만 올린다(상태·사건에는 반영하지 않는다)."""
        with self._lock:
            self._note_received(recv_ns)
            diagnostics = self.diagnostics
            diagnostics.unsupported_count += 1
            diagnostics.last_unsupported_reason = exc.reason
            if exc.packet_format is not None:
                diagnostics.last_unsupported_format = exc.packet_format
            diagnostics.last_recv_ns = recv_ns
            if exc.length is not None:
                diagnostics.last_len = exc.length
            if exc.packet_format is not None:
                diagnostics.last_format = exc.packet_format
            if exc.packet_id is not None:
                diagnostics.last_packet_id = exc.packet_id

    def record_error(self, exc: BaseException, recv_ns: int) -> None:
        """예상하지 못한 예외를 진단에 올린다. 수신 루프는 계속 돈다."""
        with self._lock:
            self._note_received(recv_ns)
            diagnostics = self.diagnostics
            diagnostics.error_count += 1
            diagnostics.last_error = f"{type(exc).__name__}: {exc}"
            diagnostics.last_recv_ns = recv_ns

    # ------------------------------------------------------------------
    # 스냅샷

    def snapshot(self) -> dict[str, Any]:
        """화면·API에 바로 넘길 수 있는 JSON 직렬화 가능한 상태."""
        with self._lock:
            return {
                "session_uid": self._session_uid,
                "player_car_index": self._player_car_index,
                "game_paused": self._paused,
                "packets": _jsonable(self._packets),
                "flags": flag_summary(
                    self._packets,
                    self._player_car_index,
                    red=self._red_flag,
                ),
                "lap_validity": [
                    {
                        "car_index": car_index,
                        "lap": status.lap,
                        "valid": status.valid,
                        "invalid_reason": status.invalid_reason,
                    }
                    for car_index, status in sorted(self._laps.items())
                ],
                "diagnostics": self.diagnostics.to_dict(),
            }

    # ------------------------------------------------------------------
    # 세션

    def _begin_session(self, uid: int, recv_ns: int) -> None:
        self._session_uid = uid
        self._session_marked = True
        self._session_ended = False
        self.diagnostics.session_uid = uid
        self.diagnostics.session_started_ns = recv_ns

    def _reset_session(self, uid: int, recv_ns: int) -> None:
        """세션에 묶인 상태를 비운다. 진단 누적값은 유지한다."""
        self._packets.clear()
        self._laps.clear()
        self._invalid_laps.clear()
        self._emitted_laps.clear()
        self._last_lap_num.clear()
        self._last_lap_distance.clear()
        self._last_lap_time.clear()
        self._last_pit_status.clear()
        self._last_weather = None
        self._last_paused = None
        self._paused = False
        self._last_safety_car_status = None
        self._clear_red_flag()
        self._max_frame = None
        self._flashback_pending = False
        self._player_car_index = None
        self._begin_session(uid, recv_ns)

    def _session_start_event(self, recv_ns: int, source: str) -> Event:
        return self._event(EventKind.SESSION_START, recv_ns, data={"source": source})

    # ------------------------------------------------------------------
    # 프레임·플래시백

    def _track_frame(self, frame: int, recv_ns: int) -> list[Event]:
        if self._max_frame is None:
            self._max_frame = frame
            return []
        if frame < self._max_frame:
            return self._flag_flashback("frame_rewind", recv_ns, source="frame_id")
        if frame > self._max_frame:
            self._max_frame = frame
            # 되감긴 프레임이 최댓값을 다시 넘어서면 플래시백 상황이 끝난 것이다.
            self._flashback_pending = False
        return []

    def _flag_flashback(
        self, reason: str, recv_ns: int, *, source: str
    ) -> list[Event]:
        """진행 중인 모든 랩을 무효로 만들고, 한 번만 FLASHBACK 사건을 낸다."""
        self._invalidate_current_laps(reason)
        if self._flashback_pending:
            return []
        self._flashback_pending = True
        return [
            self._event(
                EventKind.FLASHBACK,
                recv_ns,
                data={"reason": reason, "source": source},
            )
        ]

    def _invalidate_current_laps(self, reason: str) -> None:
        for car_index, status in self._laps.items():
            self._invalid_laps.setdefault((car_index, status.lap), reason)
            if status.valid:
                status.valid = False
                status.invalid_reason = reason

    # ------------------------------------------------------------------
    # 패킷 종류별 처리

    def _handle_session(self, body: dict[str, Any], recv_ns: int) -> list[Event]:
        events: list[Event] = []
        weather = body.get("weather")
        if isinstance(weather, int):
            if self._last_weather is not None and weather != self._last_weather:
                events.append(
                    self._event(
                        EventKind.WEATHER_CHANGE,
                        recv_ns,
                        data={"previous": self._last_weather, "current": weather},
                    )
                )
            self._last_weather = weather

        # 적기 뒤 SC·VSC가 나오면 세션이 재개 절차에 들어간 것으로 본다
        # (SCRUM-80). 같은 상태가 이어지는 동안에는 끄지 않는다.
        safety_car_status = _as_int(body.get("safety_car_status"))
        if (
            self._red_flag
            and safety_car_status in (1, 2)
            and safety_car_status != self._last_safety_car_status
        ):
            self._clear_red_flag()
        self._last_safety_car_status = safety_car_status

        paused = bool(body.get("game_paused", 0))
        if paused and not self._last_paused:
            # 일시정지 중에 진행된 랩은 코칭·기준 랩에 쓰지 않는다.
            self._invalidate_current_laps("paused")
        self._last_paused = paused
        self._paused = paused
        return events

    def _handle_lap_data(self, body: dict[str, Any], recv_ns: int) -> list[Event]:
        entries = body.get("lap_data") or []
        events: list[Event] = []
        for car_index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            lap = int(entry.get("current_lap_num", 0))
            if lap <= 0:
                continue
            if car_index == self._player_car_index and self._red_flag:
                # RDFL 직후 처음 본 랩을 기준으로 잡는다. 그 랩을 넘어서면
                # 랩 진행이 재개된 것으로 보고 적기를 끈다(SCRUM-80).
                if self._red_flag_lap is None:
                    self._red_flag_lap = lap
                elif lap > self._red_flag_lap:
                    self._clear_red_flag()
            distance = float(entry.get("lap_distance", 0.0))
            last_lap_time_ms = int(entry.get("last_lap_time_in_ms", 0))
            previous_lap = self._last_lap_num.get(car_index)
            previous_distance = self._last_lap_distance.get(car_index)

            if previous_lap is not None and lap > previous_lap:
                events.extend(
                    self._complete_lap(
                        car_index,
                        previous_lap,
                        last_lap_time_ms,
                        recv_ns,
                    )
                )
                self._start_lap(car_index, lap)
            elif previous_lap is not None and lap < previous_lap:
                events.extend(
                    self._flag_flashback(
                        "lap_number_rewind", recv_ns, source="lap_data"
                    )
                )
                status = LapStatus(lap=lap, valid=False, invalid_reason="flashback")
                self._laps[car_index] = status
                self._invalid_laps.setdefault((car_index, lap), "flashback")
            else:
                status = self._laps.get(car_index)
                if status is None or status.lap != lap:
                    self._start_lap(car_index, lap)
                if (
                    previous_lap == lap
                    and previous_distance is not None
                    and distance + _LAP_DISTANCE_TOLERANCE_M < previous_distance
                ):
                    events.extend(
                        self._flag_flashback(
                            "lap_distance_rewind", recv_ns, source="lap_data"
                        )
                    )
                    self._invalid_laps.setdefault((car_index, lap), "lap_distance_rewind")

            events.extend(
                self._pit_events(
                    car_index,
                    lap,
                    int(entry.get("pit_status", 0)),
                    recv_ns,
                )
            )
            self._last_lap_num[car_index] = lap
            self._last_lap_distance[car_index] = distance
            self._last_lap_time[car_index] = last_lap_time_ms
        return events

    def _start_lap(self, car_index: int, lap: int) -> None:
        status = LapStatus(lap=lap)
        if self._paused:
            status.valid = False
            status.invalid_reason = "paused"
            self._invalid_laps.setdefault((car_index, lap), "paused")
        self._laps[car_index] = status

    def _complete_lap(
        self,
        car_index: int,
        completed: int,
        last_lap_time_ms: int,
        recv_ns: int,
    ) -> list[Event]:
        # 랩 번호가 여러 칸 건너뛰어도 직전에 보던 랩 하나만 완료로 센다.
        status = self._laps.get(car_index)
        if status is not None and status.lap == completed and not status.valid:
            self._invalid_laps.setdefault(
                (car_index, completed), status.invalid_reason or "invalid"
            )
        if completed < 1 or (car_index, completed) in self._emitted_laps:
            return []
        self._emitted_laps.add((car_index, completed))
        return [
            self._event(
                EventKind.LAP_COMPLETE,
                recv_ns,
                car_index=car_index,
                lap=completed,
                data={
                    "lap_time_ms": last_lap_time_ms,
                    "valid": (car_index, completed) not in self._invalid_laps,
                    "invalid_reason": self._invalid_laps.get((car_index, completed)),
                    "source": "lap_data",
                },
            )
        ]

    def _pit_events(
        self, car_index: int, lap: int, pit_status: int, recv_ns: int
    ) -> list[Event]:
        previous = self._last_pit_status.get(car_index)
        events: list[Event] = []
        if previous is not None and pit_status != previous:
            if pit_status in (1, 2) and previous == 0:
                events.append(
                    self._event(
                        EventKind.PIT_ENTER,
                        recv_ns,
                        car_index=car_index,
                        lap=lap,
                        data={"pit_status": pit_status},
                    )
                )
            elif pit_status == 0 and previous in (1, 2):
                events.append(
                    self._event(
                        EventKind.PIT_EXIT,
                        recv_ns,
                        car_index=car_index,
                        lap=lap,
                        data={"pit_status": pit_status},
                    )
                )
        self._last_pit_status[car_index] = pit_status
        return events

    def _handle_event(self, body: dict[str, Any], recv_ns: int) -> list[Event]:
        code = str(body.get("event_string_code", "")).strip()
        details = body.get("event_details")
        if not isinstance(details, dict):
            details = {}
        match code:
            case "SSTA":
                # 새 세션 알림이면 적기 래치도 푼다(SCRUM-80).
                self._clear_red_flag()
                if self._session_marked:
                    return []
                self._session_marked = True
                return [self._session_start_event(recv_ns, "SSTA")]
            case "SEND":
                self._session_ended = True
                return [self._event(EventKind.SESSION_END, recv_ns)]
            case "FTLP":
                return self._fastest_lap_event(details, recv_ns)
            case "FLBK":
                return self._flag_flashback(
                    "flashback_event", recv_ns, source="FLBK"
                )
            case "SCAR":
                return self._safety_car_event(details, recv_ns)
            case "OVTK":
                return self._overtake_event(details, recv_ns)
            case "PENA":
                return self._penalty_event(details, recv_ns)
            case "RDFL":
                # 적기는 해제 Event가 없다. 랩 진행·SC/VSC·새 세션에서 끈다.
                self._red_flag = True
                self._red_flag_lap = None
                return [self._event(EventKind.RED_FLAG, recv_ns)]
            case _:
                # RTMT(리타이어), SPTP(속도 트랩) 등은 아직 사건 종류가 없다.
                return []

    def _fastest_lap_event(
        self, details: dict[str, Any], recv_ns: int
    ) -> list[Event]:
        car_index = details.get("vehicle_idx")
        if not isinstance(car_index, int) or not 0 <= car_index < 22:
            return []
        current_lap = self._last_lap_num.get(car_index)
        if current_lap is None:
            return []
        try:
            lap_time_ms = int(round(float(details.get("lap_time", 0.0)) * 1000.0))
        except (TypeError, ValueError):
            return []

        recorded = self._last_lap_time.get(car_index)
        if (
            recorded
            and lap_time_ms
            and abs(recorded - lap_time_ms) <= _LAP_TIME_TOLERANCE_MS
        ):
            # LapData가 이미 다음 랩으로 넘어갔다면 완료 랩은 그 전 랩이다.
            lap = current_lap - 1 if current_lap > 1 else current_lap
        else:
            lap = current_lap
        if lap < 1 or (car_index, lap) in self._emitted_laps:
            return []
        self._emitted_laps.add((car_index, lap))
        return [
            self._event(
                EventKind.LAP_COMPLETE,
                recv_ns,
                car_index=car_index,
                lap=lap,
                data={
                    "lap_time_ms": lap_time_ms,
                    "valid": (car_index, lap) not in self._invalid_laps,
                    "invalid_reason": self._invalid_laps.get((car_index, lap)),
                    "source": "fastest_lap",
                },
            )
        ]

    def _safety_car_event(
        self, details: dict[str, Any], recv_ns: int
    ) -> list[Event]:
        safety_car_type = int(details.get("safety_car_type", 0))
        event_type = int(details.get("event_type", 0))
        kind = EventKind.VSC if safety_car_type == 2 else EventKind.SAFETY_CAR
        return [
            self._event(
                kind,
                recv_ns,
                data={
                    "safety_car_type": safety_car_type,
                    "event_type": event_type,
                },
            )
        ]

    def _overtake_event(self, details: dict[str, Any], recv_ns: int) -> list[Event]:
        overtaking = details.get("overtaking_vehicle_idx")
        overtaken = details.get("being_overtaken_vehicle_idx")
        return [
            self._event(
                EventKind.OVERTAKE,
                recv_ns,
                car_index=overtaking if isinstance(overtaking, int) else None,
                data={
                    "overtaking_vehicle_idx": overtaking,
                    "being_overtaken_vehicle_idx": overtaken,
                },
            )
        ]

    def _penalty_event(self, details: dict[str, Any], recv_ns: int) -> list[Event]:
        car_index = details.get("vehicle_idx")
        return [
            self._event(
                EventKind.PENALTY,
                recv_ns,
                car_index=car_index if isinstance(car_index, int) else None,
                data={
                    "penalty_type": details.get("penalty_type"),
                    "infringement_type": details.get("infringement_type"),
                    "time": details.get("time"),
                    "lap": details.get("lap_num"),
                    "places_gained": details.get("places_gained"),
                },
            )
        ]

    # ------------------------------------------------------------------
    # 도우미

    def _clear_red_flag(self) -> None:
        self._red_flag = False
        self._red_flag_lap = None

    def _note_received(self, recv_ns: int) -> None:
        self._recv_times.append(recv_ns)
        cutoff = recv_ns - _DIAGNOSTIC_WINDOW_NS
        while self._recv_times and self._recv_times[0] < cutoff:
            self._recv_times.popleft()
        diagnostics = self.diagnostics
        diagnostics.packets_per_sec = float(len(self._recv_times))
        diagnostics.total_packets += 1

    def _event(
        self,
        kind: EventKind,
        recv_ns: int,
        *,
        car_index: int | None = None,
        lap: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> Event:
        payload: dict[str, Any] = {
            "session_uid": self._session_uid,
            "recv_ns": recv_ns,
        }
        if data:
            payload.update(data)
        return Event(kind=kind, car_index=car_index, lap=lap, data=payload)

    @staticmethod
    def _packet_name(pkt: Packet) -> str:
        return PacketId(pkt.header.packet_id).name.lower()

    @staticmethod
    def _format_length(pkt: Packet) -> int | None:
        module = FORMATS.get(pkt.header.packet_format)
        if module is None:
            return None
        try:
            return int(module.PACKET_SIZES[PacketId(pkt.header.packet_id)])
        except (KeyError, ValueError):
            return None


def _as_int(value: Any) -> int | None:
    """JSON·패킷 값 하나를 정수로. bool은 정수로 본다(파서가 0/1로 준다)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def _jsonable(value: Any) -> Any:
    """bytes를 hex 문자열로, 유한하지 않은 float은 None으로 바꾼다."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
