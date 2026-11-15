"""랩 단위 샘플 기록과 무효 랩 판정.

Car Telemetry 패킷이 올 때마다 차마다 샘플 하나를 만든다(설계 6.2). 샘플의 랩
거리와 랩 시각은 같은 프레임의 Lap Data 값이고, 위치는 Motion(없으면 중심선
보간), 바퀴별 노면은 Car Telemetry, 휠 슬립은 Motion Ex(내 차만), 연료량은
Car Status(내 차만)에서 온다.

Lap Data의 랩 번호가 커지면 이전 랩을 `LapTrace`로 마감해 돌려준다. 다음
경우에는 `valid=False`와 이유를 붙여 기준 랩·코칭에서 빼게 한다(리뷰 포커스
1·4, 설계 6.2).

- ``outlap``: 아웃랩(``driver_status`` 3)
- ``pit``: 피트 랩(피트 레인 진입·인랩, ``pit_status`` 1·2 또는 ``driver_status`` 2)
- ``off_track``: 트랙 이탈 경고(``driver_status`` 5 또는 게임이 무효로 표시한 랩)
- ``flashback``: ``frameIdentifier`` 되감기, ``lapDistance`` 후진, 랩 번호 후진
- ``safety_car``·``vsc``: SC·VSC가 나온 랩
- ``paused``: 일시정지 중에 진행된 랩
- ``race_lap_1``: 레이스 세션 1랩(출발 혼전)
- ``session_restart``: ``sessionUID``가 바뀌어 마감된 미완성 랩

수신 루프가 멈추지 않도록 값이 이상한 패킷은 그 샘플만 건너뛴다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from f1re.tracks.model import Corner
from f1re.tracks.outline import TrackOutline
from f1re.udp.parser import Packet
from f1re.udp.session_category import RACE_SESSION_TYPES, is_race_session
from f1re.udp.spec import PacketId

# 세션 종류(공식 열거형): 15=R, 16=R2, 17=R3(스프린트 레이스 포함). 이 세션의
# 1랩은 출발 혼전이다. 값은 `udp.session_category`가 단일 출처다(SCRUM-131).
# 기존 import(`from f1re.laps.traces import RACE_SESSION_TYPES`) 호환을 위해
# 여기서 다시 노출한다.

# Lap Data의 driver_status(공식 열거형).
DRIVER_IN_LAP = 2
DRIVER_OUT_LAP = 3
DRIVER_OFF_TRACK = 5
DRIVER_IN_PIT = 7

# Lap Data의 pit_status: 1=Pitting(진입), 2=In pit area.
PIT_LANE_STATUSES = frozenset({1, 2})

# Session의 safety_car_status: 1=Full SC, 2=VSC(3=Formation lap은 무효 사유로 보지 않는다).
_SAFETY_CAR_STATUSES = {1: "safety_car", 2: "vsc"}

# 같은 랩 안에서 이만큼(m) 넘게 뒤로 가면 플래시백으로 본다(StateStore와 같은 값).
LAP_DISTANCE_TOLERANCE_M = 1.0

# 접전(traffic) 판정: 앞차 또는 뒤차와의 시간 간격이 이 값(초) 이하이면 접전으로
# 본다(SCRUM-124). 레이스 세션에서만 표시하며, 접전 코너는 코칭·기준 랩·집중
# 코너 계산에서 뺀다.
TRAFFIC_GAP_S = 1.0

# 이유가 여러 개면 이 순서로 하나를 고른다(앞쪽이 더 근본적인 사유).
INVALID_REASON_ORDER: tuple[str, ...] = (
    "flashback",
    "session_restart",
    "off_track",
    "pit",
    "outlap",
    "safety_car",
    "vsc",
    "paused",
    "race_lap_1",
)


@dataclass(frozen=True)
class Sample:
    """랩 거리 기준 주행 샘플 하나.

    `speed`는 km/h, `dist_m`·`t_ms`는 랩 시작(결승선) 기준이다. `wheel_slip`은
    Motion Ex의 바퀴별 슬립 비율(내 차만, 없으면 None)이고 `fuel_kg`는 Car
    Status의 내 차 연료량(없으면 None)이다. `fuel_kg`는 리프트 앤 코스트
    분석(SCRUM-71)이 쓸 수 있게 남겨 둔다.

    `surface`는 Car Telemetry의 바퀴별 노면(`surface_type`, RL·RR·FL·FR 순서)이다.
    F1 25 값은 0 아스팔트, 1 연석, 2 콘크리트, 3 바위, 4 자갈, 5 진흙, 6 모래,
    7 잔디, 8 물, 9 자갈길, 10 금속, 11 요철이다. 옛 기준 랩이나 노면이 없는
    입력은 None이다(SCRUM-93).

    `in_traffic`은 이 샘플 시점에 앞차 또는 뒤차와 `TRAFFIC_GAP_S`(1초) 이내로
    붙어 있었는지다. `position_changed`는 직전 샘플 뒤로 순위(`car_position`)가
    바뀌었는지다. 둘 다 레이스 세션에서만 채우며(SCRUM-124), 접전 코너를 골라
    코칭·기준 랩·집중 코너 계산에서 빼는 데 쓴다.
    """

    dist_m: float
    t_ms: int
    speed: float
    throttle: float
    brake: float
    steer: float
    gear: int
    x: float
    z: float
    wheel_slip: tuple[float, ...] | None = None
    fuel_kg: float | None = None
    surface: tuple[int, ...] | None = None
    in_traffic: bool = False
    position_changed: bool = False


@dataclass
class LapTrace:
    """차 한 대가 달린 랩 하나. `valid=False`면 `invalid_reason`에 이유가 있다."""

    car_index: int
    lap: int
    valid: bool
    invalid_reason: str | None
    samples: list[Sample]


def _delta_to_front_s(entry: Mapping[str, Any]) -> float | None:
    """Lap Data의 앞차와의 시간 간격(초). 없으면 None."""
    minutes = _as_int(entry.get("delta_to_car_in_front_minutes_part"))
    milliseconds = _as_int(entry.get("delta_to_car_in_front_ms_part"))
    if minutes is None and milliseconds is None:
        return None
    return max(0, minutes or 0) * 60.0 + max(0, milliseconds or 0) / 1000.0


def _running_on_track(entry: Mapping[str, Any]) -> bool:
    """차가 실제 주행 중인가(차고·인랩·아웃랩·피트·리타이어 제외)."""
    result_status = _as_int(entry.get("result_status"))
    if result_status is not None and result_status != 2:
        return False
    driver_status = _as_int(entry.get("driver_status"))
    return driver_status not in {
        0,
        DRIVER_IN_LAP,
        DRIVER_OUT_LAP,
        DRIVER_IN_PIT,
    }


def battle_traffic(
    entries: Sequence[Mapping[str, Any]],
    car_index: int,
    *,
    gap_s: float = TRAFFIC_GAP_S,
) -> bool:
    """그 차가 앞차 또는 뒤차와 `gap_s`(기본 1초) 이내로 붙어 있는가.

    `entries`는 Lap Data 패킷의 차량별 항목이다. 앞차 간격은 그 차의
    `delta_to_car_in_front`를 쓰고, 뒤차 간격은 순위가 바로 뒤인 차의
    `delta_to_car_in_front`(그 차 기준 앞차 = 이 차)를 쓴다. 랩이 겹친 차
    (총 주행거리가 더 큰 차)와 차고·피트·리타이어 차는 뒤차로 보지 않는다.
    """
    if not 0 <= car_index < len(entries):
        return False
    entry = entries[car_index]
    if not isinstance(entry, Mapping):
        return False
    position = _as_int(entry.get("car_position"))
    if position is None or position <= 0:
        return False

    if position > 1:
        gap = _delta_to_front_s(entry)
        if gap is not None and gap <= gap_s:
            return True

    behind: Mapping[str, Any] | None = None
    for item in entries:
        if isinstance(item, Mapping) and _as_int(item.get("car_position")) == position + 1:
            behind = item
            break
    if behind is None or not _running_on_track(behind):
        return False
    own_total = _as_float(entry.get("total_distance"))
    behind_total = _as_float(behind.get("total_distance"))
    if own_total is not None and behind_total is not None and behind_total > own_total:
        # 순위는 바로 뒤지만 실제로는 랩이 앞선 차다(랩 다운/업 상황).
        return False
    gap = _delta_to_front_s(behind)
    return gap is not None and gap <= gap_s


def corner_has_traffic(samples: Sequence[Sample], corner: Corner) -> bool:
    """코너 구간(진입~탈출) 안에 접전 샘플이나 순위 변동이 있었는가.

    레이스 세션이 아닐 때의 제외 여부는 호출자가 판단한다(비레이스는 미적용).
    """
    return any(
        (sample.in_traffic or sample.position_changed)
        and corner.entry_m <= sample.dist_m <= corner.exit_m
        for sample in samples
    )


@dataclass
class _LapBuffer:
    """기록 중인 랩 하나의 샘플과 무효 사유."""

    car_index: int
    lap: int
    samples: list[Sample] = field(default_factory=list)
    reasons: set[str] = field(default_factory=set)
    last_distance: float | None = None


class LapTraceRecorder:
    """UDP 패킷을 받아 차마다 랩별 샘플을 쌓고, 랩이 끝나면 `LapTrace`를 낸다.

    `on_packet`은 완료된 랩 목록을 돌려준다. 같은 패킷을 `StateStore`에도
    넣어도 되고(`apply`와 독립적), 데모 송신기 출력을 그대로 넣어도 된다.
    """

    def __init__(self, *, outline: TrackOutline | None = None) -> None:
        self._outline = outline
        self._session_uid: int | None = None
        self._session_type: int | None = None
        self._safety_car_reason: str | None = None
        self._paused = False
        self._player_car_index: int | None = None
        self._max_frame: int | None = None

        self._lap_data: dict[int, dict[str, Any]] = {}
        self._motion: dict[int, dict[str, Any]] = {}
        self._motion_ex: dict[int, tuple[float, ...]] = {}
        self._fuel_kg: dict[int, float] = {}
        self._current: dict[int, _LapBuffer] = {}

        # 접전 판정(SCRUM-124). 내 차의 최신 Lap Data 상태를 샘플에 붙인다.
        # 순위 변동은 다음 텔레메트리 샘플 하나에만 표시하고 지운다.
        self._player_in_traffic = False
        self._player_position: int | None = None
        self._position_changed = False

    def set_outline(self, outline: TrackOutline | None) -> None:
        """중심선 윤곽을 바꾼다. 첫 유효 랩에서 윤곽을 배운 뒤 붙인다(SCRUM-84)."""
        self._outline = outline

    # ------------------------------------------------------------------
    # 패킷 받기

    def on_packet(self, pkt: Packet) -> list[LapTrace]:
        """패킷 하나를 반영하고, 이 패킷으로 완료된 랩들을 돌려준다."""
        completed: list[LapTrace] = []
        header = pkt.header

        if self._session_uid is None:
            self._session_uid = header.session_uid
        elif header.session_uid != self._session_uid:
            completed.extend(self._finish_session())
            self._session_uid = header.session_uid

        if 0 <= header.player_car_index < 22:
            self._player_car_index = header.player_car_index

        self._track_frame(header.overall_frame_id)

        try:
            packet_id = PacketId(header.packet_id)
        except ValueError:
            return completed

        body = pkt.body
        if packet_id is PacketId.LAP_DATA:
            completed.extend(self._handle_lap_data(body))
        elif packet_id is PacketId.CAR_TELEMETRY:
            self._handle_car_telemetry(body)
        elif packet_id is PacketId.MOTION:
            self._handle_motion(body)
        elif packet_id is PacketId.MOTION_EX:
            self._handle_motion_ex(body)
        elif packet_id is PacketId.CAR_STATUS:
            self._handle_car_status(body)
        elif packet_id is PacketId.SESSION:
            self._handle_session(body)
        elif packet_id is PacketId.EVENT:
            # Event FLBK도 플래시백이다(헤더 프레임이 안 되감겨도 놓치지 않는다).
            if str(body.get("event_string_code", "")).strip() == "FLBK":
                self._flag_all("flashback")
        return completed

    # ------------------------------------------------------------------
    # 랩 경계와 무효 사유

    def _handle_lap_data(self, body: dict[str, Any]) -> list[LapTrace]:
        completed: list[LapTrace] = []
        entries = body.get("lap_data")
        if not isinstance(entries, list):
            return completed

        self._update_traffic(entries)

        for car_index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            lap = _as_int(entry.get("current_lap_num"))
            if lap is None or lap <= 0:
                # 아직 달리지 않는 차(데모의 빈 슬롯·AI 대기)는 기록하지 않는다.
                continue
            distance = _as_float(entry.get("lap_distance"))
            if distance is None:
                distance = 0.0

            current = self._current.get(car_index)
            if current is None or lap > current.lap:
                if current is not None:
                    trace = self._finalize(current)
                    if trace is not None:
                        completed.append(trace)
                current = self._start_lap(car_index, lap)
            elif lap < current.lap:
                # 플래시백으로 랩 번호가 되감겼다. 되감기 전 랩은 무효로 마감하고
                # 되감긴 시점의 랩을 새로 기록한다.
                current.reasons.add("flashback")
                trace = self._finalize(current)
                if trace is not None:
                    completed.append(trace)
                current = self._start_lap(car_index, lap)
                current.reasons.add("flashback")
            else:
                last = current.last_distance
                if last is not None and distance + LAP_DISTANCE_TOLERANCE_M < last:
                    current.reasons.add("flashback")

            self._flag_lap_data(current, entry)
            current.last_distance = distance
            self._lap_data[car_index] = entry
        return completed

    def _update_traffic(self, entries: Sequence[Mapping[str, Any]]) -> None:
        """내 차의 접전 여부와 순위 변동을 갱신한다(SCRUM-124).

        비레이스 세션에서는 접전 판정을 하지 않는다. 순위가 바뀌면 다음
        텔레메트리 샘플 하나에 `position_changed`를 붙인다.
        """
        index = self._player_car_index
        if (
            not is_race_session(self._session_type)
            or index is None
            or not 0 <= index < len(entries)
        ):
            self._player_in_traffic = False
            return

        self._player_in_traffic = battle_traffic(entries, index)
        entry = entries[index]
        if not isinstance(entry, Mapping):
            return
        position = _as_int(entry.get("car_position"))
        if position is None:
            return
        if self._player_position is not None and position != self._player_position:
            self._position_changed = True
        self._player_position = position

    def _flag_lap_data(self, current: _LapBuffer, entry: dict[str, Any]) -> None:
        """Lap Data 필드에서 랩 무효 사유를 표시한다."""
        driver_status = _as_int(entry.get("driver_status"))
        if driver_status == DRIVER_OUT_LAP:
            current.reasons.add("outlap")
        elif driver_status == DRIVER_IN_LAP or driver_status == DRIVER_IN_PIT:
            current.reasons.add("pit")
        elif driver_status == DRIVER_OFF_TRACK:
            current.reasons.add("off_track")

        pit_status = _as_int(entry.get("pit_status"))
        if pit_status in PIT_LANE_STATUSES:
            current.reasons.add("pit")

        # 게임이 무효로 표시한 랩(트랙 이탈 경고·커팅 등)도 기준 랩에서 뺀다.
        if _as_int(entry.get("current_lap_invalid")):
            current.reasons.add("off_track")

    def _start_lap(self, car_index: int, lap: int) -> _LapBuffer:
        buffer = _LapBuffer(car_index=car_index, lap=lap)
        # 랩이 시작될 때 이미 진행 중인 상황(SC·VSC·일시정지)은 새 랩에도 적용한다.
        if self._safety_car_reason is not None:
            buffer.reasons.add(self._safety_car_reason)
        if self._paused:
            buffer.reasons.add("paused")
        self._current[car_index] = buffer
        return buffer

    def _track_frame(self, frame: int) -> None:
        """frameIdentifier 되감기를 플래시백으로 본다(Review Focus 1)."""
        if self._max_frame is None:
            self._max_frame = frame
            return
        if frame < self._max_frame:
            self._flag_all("flashback")
            return
        if frame > self._max_frame:
            self._max_frame = frame

    def _flag_all(self, reason: str) -> None:
        for buffer in self._current.values():
            buffer.reasons.add(reason)

    def _handle_session(self, body: dict[str, Any]) -> None:
        session_type = _as_int(body.get("session_type"))
        if session_type is not None:
            self._session_type = session_type

        safety_car_status = _as_int(body.get("safety_car_status"))
        self._safety_car_reason = _SAFETY_CAR_STATUSES.get(safety_car_status)
        if self._safety_car_reason is not None:
            self._flag_all(self._safety_car_reason)

        paused = bool(_as_int(body.get("game_paused")) or 0)
        if paused and not self._paused:
            self._flag_all("paused")
        self._paused = paused

    # ------------------------------------------------------------------
    # 샘플 만들기

    def _handle_car_telemetry(self, body: dict[str, Any]) -> None:
        entries = body.get("car_telemetry_data")
        if not isinstance(entries, list):
            return
        for car_index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            buffer = self._current.get(car_index)
            lap_entry = self._lap_data.get(car_index)
            if buffer is None or lap_entry is None:
                continue
            lap = _as_int(lap_entry.get("current_lap_num"))
            if lap != buffer.lap:
                # Lap Data가 아직 새 랩으로 넘어오지 않았다. 다음 틱에 기록한다.
                continue
            sample = self._make_sample(car_index, entry, lap_entry)
            if sample is not None:
                buffer.samples.append(sample)
                if car_index == self._player_car_index:
                    # 순위 변동 표시는 샘플 하나에만 붙인다.
                    self._position_changed = False

    def _make_sample(
        self,
        car_index: int,
        telemetry: dict[str, Any],
        lap_entry: dict[str, Any],
    ) -> Sample | None:
        dist_m = _as_float(lap_entry.get("lap_distance"))
        t_ms = _as_int(lap_entry.get("current_lap_time_in_ms"))
        speed = _as_float(telemetry.get("speed"))
        throttle = _as_float(telemetry.get("throttle"))
        brake = _as_float(telemetry.get("brake"))
        steer = _as_float(telemetry.get("steer"))
        gear = _as_int(telemetry.get("gear"))
        if None in (dist_m, t_ms, speed, throttle, brake, steer, gear):
            return None
        if not all(math.isfinite(value) for value in (dist_m, speed, throttle, brake, steer)):
            return None

        x, z = self._position(car_index, dist_m)
        is_player = car_index == self._player_car_index
        return Sample(
            dist_m=dist_m,
            t_ms=t_ms,
            speed=speed,
            throttle=throttle,
            brake=brake,
            steer=steer,
            gear=gear,
            x=x,
            z=z,
            wheel_slip=self._motion_ex.get(car_index) if is_player else None,
            fuel_kg=self._fuel_kg.get(car_index) if is_player else None,
            surface=_surface_tuple(telemetry.get("surface_type")),
            in_traffic=self._player_in_traffic if is_player else False,
            position_changed=self._position_changed if is_player else False,
        )

    def _position(self, car_index: int, dist_m: float) -> tuple[float, float]:
        """Motion의 세계 좌표, 없으면 중심선 보간 위치를 쓴다."""
        motion = self._motion.get(car_index)
        if motion is not None:
            x = _as_float(motion.get("world_position_x"))
            z = _as_float(motion.get("world_position_z"))
            if x is not None and z is not None and math.isfinite(x) and math.isfinite(z):
                return x, z
        if self._outline is not None:
            return self._outline.position_at(dist_m)
        return 0.0, 0.0

    def _handle_motion(self, body: dict[str, Any]) -> None:
        entries = body.get("car_motion_data")
        if not isinstance(entries, list):
            return
        for car_index, entry in enumerate(entries):
            if isinstance(entry, dict):
                self._motion[car_index] = entry

    def _handle_motion_ex(self, body: dict[str, Any]) -> None:
        """Motion Ex는 내 차 것만 온다(공식 규격)."""
        if self._player_car_index is None:
            return
        ratios = body.get("wheel_slip_ratio")
        if not isinstance(ratios, (list, tuple)):
            return
        try:
            values = tuple(float(value) for value in ratios)
        except (TypeError, ValueError):
            return
        if values and all(math.isfinite(value) for value in values):
            self._motion_ex[self._player_car_index] = values

    def _handle_car_status(self, body: dict[str, Any]) -> None:
        entries = body.get("car_status_data")
        if not isinstance(entries, list) or self._player_car_index is None:
            return
        if self._player_car_index >= len(entries):
            return
        entry = entries[self._player_car_index]
        if not isinstance(entry, dict):
            return
        fuel = _as_float(entry.get("fuel_in_tank"))
        if fuel is not None and math.isfinite(fuel):
            self._fuel_kg[self._player_car_index] = fuel

    # ------------------------------------------------------------------
    # 마감

    def _finalize(self, buffer: _LapBuffer) -> LapTrace | None:
        """기록 중인 랩을 LapTrace로 마감한다. 샘플이 없으면 버린다."""
        if not buffer.samples:
            return None
        reasons = set(buffer.reasons)
        if self._session_type in RACE_SESSION_TYPES and buffer.lap == 1:
            reasons.add("race_lap_1")
        reason = next(
            (name for name in INVALID_REASON_ORDER if name in reasons), None
        )
        return LapTrace(
            car_index=buffer.car_index,
            lap=buffer.lap,
            valid=not reasons,
            invalid_reason=reason,
            samples=buffer.samples,
        )

    def _finish_session(self) -> list[LapTrace]:
        """세션이 바뀔 때 기록 중인 랩을 무효로 마감하고 상태를 비운다."""
        completed: list[LapTrace] = []
        for buffer in self._current.values():
            buffer.reasons.add("session_restart")
            trace = self._finalize(buffer)
            if trace is not None:
                completed.append(trace)
        self._current.clear()
        self._lap_data.clear()
        self._motion.clear()
        self._motion_ex.clear()
        self._fuel_kg.clear()
        self._session_type = None
        self._safety_car_reason = None
        self._paused = False
        self._player_car_index = None
        self._max_frame = None
        self._player_in_traffic = False
        self._player_position = None
        self._position_changed = False
        return completed


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _surface_tuple(value: Any) -> tuple[int, ...] | None:
    """바퀴별 노면(4개)을 정수 튜플로 만든다. 형식이 다르면 None."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    surfaces: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        surfaces.append(int(item))
    return tuple(surfaces)
