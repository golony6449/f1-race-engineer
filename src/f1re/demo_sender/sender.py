"""F1 25 UDP 데모 송신기 – 타임 트라이얼·그랑프리 레이스.

게임 없이 수신기·코칭을 시험할 수 있도록, F1 25 공식 UDP 규격과 같은 바이트를
127.0.0.1(기본)로 보낸다. 패킷 구조체는 `f1re.udp.formats.f2025`의 정의와
`pack_packet`을 그대로 쓰므로 파서와 같은 규격 이해를 공유한다.

데모는 서킷 데이터(`load_track`·`load_outline`)의 중심선을 따라 만든 주행
샘플로 패킷을 채운다. 패킷별 송신 주기는 `f1re.udp.spec.PACKET_RATES_HZ`를
따르고, 게임 설정과 같은 기본 60Hz(20/30Hz로 낮출 수 있음)를 쓴다.

- `tt` 모드: 내 차 혼자 도는 타임 트라이얼. Time Trial 패킷을 보낸다.
- `gp` 모드: AI 19대 + 내 차. Lap Data·Telemetry·Status·Motion을 20대분
  채우고 Session History·Tyre Sets는 차를 돌아가며 보낸다.
- `--scenario` YAML: 정해진 랩·코너에서 코칭·추월·경고 상황을 주입한다.
"""

from __future__ import annotations

import argparse
import bisect
import math
import random
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from f1re.demo_sender.driving import (
    DEFAULT_RATE_HZ,
    CornerEffect,
    DriverProfile,
    Sample,
    generate_lap,
    lap_time_ms,
    sector_split_distances,
    sector_times_ms,
)
from f1re.demo_sender.field import (
    PLAYER_CAR_INDEX,
    CarProfile,
    CarState,
    RaceField,
    car_ahead,
)
from f1re.demo_sender.scenarios import (
    ScenarioEvent,
    ScenarioError,
    corner_effects,
    events_for_lap,
    load_scenarios,
)
from f1re.tracks.model import Track, load_outline, load_track
from f1re.tracks.outline import TrackOutline
from f1re.udp.formats import f2025
from f1re.udp.packer import pack_packet
from f1re.udp.schema import (
    MatrixField,
    RawField,
    ScalarField,
    StringField,
    StructDef,
    StructField,
)
from f1re.udp.spec import (
    MENU_RATE_HZ,
    PACKET_FORMAT,
    PACKET_RATES_HZ,
    PACKET_SIZES,
    Header,
    PacketId,
)

BASE_RATE_HZ = DEFAULT_RATE_HZ
TRACK_IDS: dict[str, int] = {"suzuka": 13, "monaco": 5}

# 공식 열거형 값(EA F1 25 UDP 문서).
SESSION_TYPE_RACE = 10
SESSION_TYPE_TIME_TRIAL = 13
GAME_MODE_GRAND_PRIX = 1
GAME_MODE_TIME_TRIAL = 2
RULE_SET_RACE = 1
RULE_SET_TIME_TRIAL = 2
WEATHER_CLEAR = 0
SAFETY_CAR_NONE = 0
SAFETY_CAR_FULL = 1
SAFETY_CAR_VIRTUAL = 2
SAFETY_CAR_TYPE_FULL = 0
SAFETY_CAR_TYPE_VIRTUAL = 1
SAFETY_CAR_EVENT_DEPLOYED = 1
SAFETY_CAR_EVENT_RETURNED = 3

MODES = ("tt", "gp")
SECONDARY_CAR_INDEX = 255
DEFAULT_LAPS_TT = 3
DEFAULT_LAPS_GP = 5
COOLDOWN_S = 1.0  # 마지막 랩 완료를 수신기가 볼 수 있게 잠깐 더 보낸다.
AI_DIFFICULTY = 80
GRID_START_POSITION = 20  # 그랑프리 데모에서 내 차는 맨 뒤 그리드에서 출발한다.
MAX_TYRE_AGE_LAPS_IN_SENDER = 255
TYRE_COMPOUND_SOFT = 16
FUEL_CAPACITY_KG = 110.0
FUEL_PER_LAP_KG = 1.8
FUEL_START_KG = 20.0
ERS_STORE_MAX_J = 4_000_000.0
PIT_SPEED_LIMIT_KMH = 80
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 20777
TELEMETRY_RATES_HZ = (20, 30, 60)

# 수신기가 쓰는 종류 가운데 주기 송신하는 패킷. Event는 사건이 생길 때만 보낸다.
# Time Trial 패킷은 타임 트라이얼 모드에서만 보낸다(실제 게임과 같다).
PERIODIC_PACKETS: tuple[PacketId, ...] = (
    PacketId.MOTION,
    PacketId.SESSION,
    PacketId.LAP_DATA,
    PacketId.PARTICIPANTS,
    PacketId.CAR_TELEMETRY,
    PacketId.CAR_STATUS,
    PacketId.CAR_DAMAGE,
    PacketId.SESSION_HISTORY,
    PacketId.TYRE_SETS,
    PacketId.MOTION_EX,
    PacketId.TIME_TRIAL,
)
GP_PERIODIC_PACKETS: tuple[PacketId, ...] = tuple(
    packet_id for packet_id in PERIODIC_PACKETS if packet_id is not PacketId.TIME_TRIAL
)


def _default_value(field_def: Any) -> Any:
    """구조체 필드 하나의 0 기본값."""
    if isinstance(field_def, ScalarField):
        zero: int | float = 0.0 if field_def.fmt in "fd" else 0
        return zero if field_def.count == 1 else [zero] * field_def.count
    if isinstance(field_def, StringField):
        return ""
    if isinstance(field_def, RawField):
        return bytes(field_def.length)
    if isinstance(field_def, MatrixField):
        return [[0] * field_def.cols for _ in range(field_def.rows)]
    if isinstance(field_def, StructField):
        if field_def.count is None:
            return _default_struct_body(field_def.struct)
        return [_default_struct_body(field_def.struct) for _ in range(field_def.count)]
    raise TypeError(f"알 수 없는 필드 정의: {field_def!r}")


def _default_struct_body(struct_def: StructDef) -> dict[str, Any]:
    return {field.key: _default_value(field) for field in struct_def.fields}


def _default_packet_body(packet_id: PacketId) -> dict[str, Any]:
    return _default_struct_body(f2025.PACKETS[packet_id].body)


def _split_ms(total_ms: int) -> tuple[int, int]:
    """ms를 (분, 0~59999ms)로 나눈다. 공식 문서의 분·ms 파트 필드용."""
    total_ms = max(0, total_ms)
    return divmod(total_ms, 60_000)


def _split_delta(total_ms: int) -> tuple[int, int]:
    """간격(ms)을 Lap Data의 (분, ms 파트)로 나눈다. 분은 uint8 상한을 지킨다."""
    minutes, ms_part = divmod(max(0, total_ms), 60_000)
    return min(255, minutes), ms_part


@dataclass(frozen=True)
class LapResult:
    """완료한 랩 하나의 기록."""

    lap: int
    time_ms: int
    sectors_ms: tuple[int, int, int]


@dataclass
class _LapRun:
    """이어서 보낼 랩 하나(또는 마지막 랩 뒤 냉각 구간)."""

    lap: int
    samples: list[Sample]
    cooldown: bool = False
    start_tick: int = 0

    @property
    def ticks(self) -> int:
        return len(self.samples)


@dataclass
class _ScenarioEffects:
    """시나리오가 바꾼 차 상태 값. 랩 시작에 갱신하고 패킷 생성이 읽는다."""

    front_wing_damage: int = 0
    tyre_wear_percent: float | None = None
    tyre_surface_c: int | None = None
    tyre_inner_c: int | None = None
    brake_temp_c: int | None = None
    fuel_margin_laps: float | None = None
    penalty_seconds: int = 0
    total_warnings: int = 0
    tyre_age_override: int | None = None
    safety_car_status: int = SAFETY_CAR_NONE
    safety_car_type: int = SAFETY_CAR_TYPE_FULL
    safety_car_until_lap: int = 0
    drs_until_lap: int = 0
    drs_activation_distance: int = 0

    def drs_active(self, lap: int) -> bool:
        return self.drs_until_lap >= lap > 0


@dataclass(frozen=True)
class _TickContext:
    """한 틱의 주행 샘플과(그랑프리면) 차 20대 상태."""

    run: _LapRun
    sample: Sample
    race_time_s: float
    states: tuple[CarState, ...] = field(default_factory=tuple)

    def state(self, car_index: int) -> CarState | None:
        for state in self.states:
            if state.car_index == car_index:
                return state
        return None


class DemoSender:
    """데모 패킷을 UDP로 보낸다(타임 트라이얼 또는 그랑프리 레이스)."""

    def __init__(
        self,
        track: Track,
        outline: TrackOutline,
        *,
        driver: DriverProfile | None = None,
        laps: int = DEFAULT_LAPS_TT,
        seed: int = 0,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        rate_hz: int = int(BASE_RATE_HZ),
        speed: float = 1.0,
        mode: str = "tt",
        scenarios: Sequence[ScenarioEvent] | None = None,
        sock: socket.socket | None = None,
    ) -> None:
        if laps < 1:
            raise ValueError("laps는 1 이상이어야 합니다")
        if mode not in MODES:
            raise ValueError(f"mode는 {MODES} 중 하나여야 합니다: {mode!r}")
        if rate_hz not in TELEMETRY_RATES_HZ:
            raise ValueError(f"rate_hz는 {TELEMETRY_RATES_HZ} 중 하나여야 합니다")
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("speed는 0보다 큰 유한한 값이어야 합니다")
        if not 0 <= port <= 65535:
            raise ValueError("port는 0~65535여야 합니다")

        self._track = track
        self._laps = laps
        self._host = host
        self._port = int(port)
        self._rate_hz = rate_hz
        self._speed = float(speed)
        self._mode = mode
        self._scenario_events = list(scenarios or [])
        self._validate_scenarios()

        self._session_uid = 0x5EED_0000_0000_0000 | (seed & 0xFFFF_FFFF)
        self._frame_id = 0
        self._completed: list[LapResult] = []
        self._next_lap_end = 0
        self._owns_socket = sock is None
        self._sock = sock if sock is not None else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._lap_effects = self._build_lap_effects()
        self._runs = self._build_runs(
            track, outline, driver or DriverProfile(), laps, seed, self._lap_effects
        )
        self._lap_end_ticks = self._build_lap_end_ticks(self._runs)
        self._end_tick = sum(run.ticks for run in self._runs)
        self._run_start_ms = self._build_run_start_ms(self._runs)
        self._sample_ticks: list[int] = []
        self._sample_runs: list[_LapRun] = []
        self._sample_run_index: list[int] = []
        for run_index, run in enumerate(self._runs):
            for index in range(run.ticks):
                self._sample_ticks.append(run.start_tick + index)
                self._sample_runs.append(run)
                self._sample_run_index.append(run_index)

        self._field = RaceField(track, outline, seed=seed) if mode == "gp" else None
        self._effects = _ScenarioEffects()
        self._triggered: set[int] = set()
        self._current_lap = 0
        self._context_cache: _TickContext | None = None
        self._context_tick = -1
        self._previous_positions: dict[int, int] | None = None
        self._ai_laps_seen: dict[int, int] = {}
        self._session_best_ms: int | None = (
            min(car.lap_time_ms for car in self._field.cars if car.ai_controlled)
            if self._field is not None
            else None
        )
        self._history_cursor = 0
        self._tyre_sets_cursor = 0

        periodic = PERIODIC_PACKETS if mode == "tt" else GP_PERIODIC_PACKETS
        self._periodic_packets = periodic
        self._periods = self._packet_periods(rate_hz)
        self._templates = {
            packet_id: _default_packet_body(packet_id) for packet_id in periodic
        }
        self._session_body = self._build_session_body()
        self._participants_body = self._build_participants_body()
        self._tyre_set_template = _default_struct_body(f2025.TYRE_SET_DATA)
        self._time_trial_template = _default_packet_body(PacketId.TIME_TRIAL)
        self._event_template = _default_packet_body(PacketId.EVENT)

    # ------------------------------------------------------------------
    # 세션 구성

    def _validate_scenarios(self) -> None:
        """시나리오가 가리키는 코너가 트랙에 있는지 미리 확인한다."""
        corners = {corner.number for corner in self._track.corners}
        for event in self._scenario_events:
            if event.corner is not None and event.corner not in corners:
                raise ScenarioError(
                    f"{event.lap}랩 {event.kind}: 트랙에 {event.corner}번 코너가 없습니다"
                )

    def _build_lap_effects(self) -> dict[int, dict[int, CornerEffect]]:
        """랩별 주행 시나리오 효과. 그 랩의 모든 시나리오를 코너 효과로 바꾼다."""
        corner_numbers = [corner.number for corner in self._track.corners]
        effects: dict[int, dict[int, CornerEffect]] = {}
        for lap in range(1, self._laps + 1):
            lap_events = events_for_lap(self._scenario_events, lap)
            if not lap_events:
                continue
            converted = corner_effects(lap_events, corner_numbers)
            if converted:
                effects[lap] = converted
        return effects

    @staticmethod
    def _build_runs(
        track: Track,
        outline: TrackOutline,
        driver: DriverProfile,
        laps: int,
        seed: int,
        lap_effects: Mapping[int, Mapping[int, CornerEffect]] | None = None,
    ) -> list[_LapRun]:
        rng = random.Random(seed)
        runs: list[_LapRun] = []
        for lap in range(1, laps + 1):
            effects = dict(lap_effects[lap]) if lap_effects and lap in lap_effects else None
            samples = generate_lap(
                track, outline, driver, lap, rng, corner_effects=effects
            )
            runs.append(_LapRun(lap=lap, samples=samples))

        # 마지막 랩 완료를 수신기가 관찰할 수 있게 1초 분량을 더 보낸다.
        if runs:
            cooldown_ticks = max(1, int(round(COOLDOWN_S * BASE_RATE_HZ)))
            source = runs[0].samples[:cooldown_ticks]
            runs.append(_LapRun(lap=laps + 1, samples=source, cooldown=True))

        cursor = 0
        for run in runs:
            run.start_tick = cursor
            cursor += run.ticks
        return runs

    @staticmethod
    def _build_lap_end_ticks(runs: list[_LapRun]) -> list[int]:
        ends: list[int] = []
        for run in runs:
            if run.cooldown:
                continue
            ends.append(run.start_tick + run.ticks)
        return ends

    @staticmethod
    def _build_run_start_ms(runs: list[_LapRun]) -> list[float]:
        """각 구간이 시작하는 레이스 시각(ms). 그랑프리 필드의 AI 진행 계산용."""
        starts: list[float] = []
        cursor = 0.0
        for run in runs:
            starts.append(cursor)
            cursor += lap_time_ms(run.samples)
        return starts

    def _packet_periods(self, rate_hz: int) -> dict[PacketId, int]:
        """패킷별 송신 주기를 60Hz 틱 수로 바꾼다."""
        periods: dict[PacketId, int] = {}
        for packet_id in self._periodic_packets:
            rate = PACKET_RATES_HZ[packet_id]
            if rate is None:
                continue
            if rate == MENU_RATE_HZ:
                rate = float(rate_hz)
            periods[packet_id] = max(1, int(round(BASE_RATE_HZ / rate)))
        return periods

    @property
    def lap_results(self) -> list[LapResult]:
        """지금까지 완료한 랩 기록(진행 중이면 완료분까지)."""
        return list(self._completed)

    @property
    def lap_times_ms(self) -> list[int]:
        """보낼 랩들의 예상 랩 타임(ms)."""
        return [lap_time_ms(run.samples) for run in self._runs if not run.cooldown]

    # ------------------------------------------------------------------
    # 패킷 본문

    def _header(self, packet_id: PacketId, time_ms: int) -> Header:
        return Header(
            packet_format=PACKET_FORMAT,
            game_year=25,
            game_major=1,
            game_minor=0,
            packet_version=1,
            packet_id=int(packet_id),
            session_uid=self._session_uid,
            session_time=time_ms / 1000.0,
            frame_id=self._frame_id,
            overall_frame_id=self._frame_id,
            player_car_index=PLAYER_CAR_INDEX,
            secondary_player_car_index=SECONDARY_CAR_INDEX,
        )

    def _build_session_body(self) -> dict[str, Any]:
        body = _default_packet_body(PacketId.SESSION)
        split1, split2 = sector_split_distances(self._track)
        body.update(
            weather=WEATHER_CLEAR,
            track_temperature=32,
            air_temperature=24,
            total_laps=0,  # 타임 트라이얼은 랩 제한이 없다.
            track_length=int(round(self._track.length_m)),
            session_type=SESSION_TYPE_TIME_TRIAL,
            track_id=self._track.track_id,
            formula=0,
            session_time_left=0,
            session_duration=0,
            pit_speed_limit=PIT_SPEED_LIMIT_KMH,
            game_paused=0,
            is_spectating=0,
            spectator_car_index=SECONDARY_CAR_INDEX,
            sli_pro_native_support=0,
            num_marshal_zones=0,
            safety_car_status=0,
            network_game=0,
            num_weather_forecast_samples=0,
            forecast_accuracy=0,
            ai_difficulty=0,
            dynamic_racing_line_type=2,
            game_mode=GAME_MODE_TIME_TRIAL,
            rule_set=RULE_SET_TIME_TRIAL,
            time_of_day=720,
            speed_units_lead_player=0,
            temperature_units_lead_player=0,
            speed_units_secondary_player=0,
            temperature_units_secondary_player=0,
            equal_car_performance=1,
            surface_type=0,
            car_damage=0,
            sector2_lap_distance_start=split1,
            sector3_lap_distance_start=split2,
        )
        if self._mode == "gp":
            body.update(
                total_laps=min(255, self._laps),
                session_type=SESSION_TYPE_RACE,
                game_mode=GAME_MODE_GRAND_PRIX,
                rule_set=RULE_SET_RACE,
                ai_difficulty=AI_DIFFICULTY,
                session_duration=min(65535, sum(self.lap_times_ms) // 1000),
                num_safety_car_periods=0,
                num_virtual_safety_car_periods=0,
            )
        return body

    def _participant_entry(
        self,
        car: CarProfile,
    ) -> dict[str, Any]:
        entry = _default_struct_body(f2025.PARTICIPANT_DATA)
        entry.update(
            ai_controlled=0 if not car.ai_controlled else 1,
            driver_id=255 if not car.ai_controlled else car.car_index,
            network_id=255 if not car.ai_controlled else car.car_index,
            team_id=car.team_id,
            my_team=0,
            race_number=car.car_index + 1,
            nationality=0,
            name=car.name,
            your_telemetry=1 if not car.ai_controlled else 0,
            show_online_names=1,
            tech_level=0,
            platform=0,
            num_colours=2,
        )
        if not car.ai_controlled:
            entry["livery_colours"] = [
                {"red": 192, "green": 192, "blue": 192},
                {"red": 32, "green": 32, "blue": 32},
                {"red": 0, "green": 0, "blue": 0},
                {"red": 0, "green": 0, "blue": 0},
            ]
        else:
            red = (37 * car.car_index) % 256
            green = (91 * car.car_index) % 256
            blue = (167 * car.car_index) % 256
            entry["livery_colours"] = [
                {"red": red, "green": green, "blue": blue},
                {"red": 24, "green": 24, "blue": 24},
                {"red": 0, "green": 0, "blue": 0},
                {"red": 0, "green": 0, "blue": 0},
            ]
        return entry

    def _build_participants_body(self) -> dict[str, Any]:
        body = _default_packet_body(PacketId.PARTICIPANTS)
        participants = list(body["participants"])
        if self._mode == "gp":
            assert self._field is not None
            for car in self._field.cars:
                participants[car.car_index] = self._participant_entry(car)
            body["num_active_cars"] = len(self._field.cars)
        else:
            player = CarProfile(
                car_index=PLAYER_CAR_INDEX,
                name="YOU",
                team_id=0,
                ai_controlled=False,
                compound=TYRE_COMPOUND_SOFT,
                tyre_age_base=0,
                pace=1.0,
                start_offset_m=0.0,
                lap_time_ms=0,
            )
            participants[PLAYER_CAR_INDEX] = self._participant_entry(player)
            body["num_active_cars"] = 1
        body["participants"] = participants
        return body

    def _build_tyre_sets_body(self, car_index: int = PLAYER_CAR_INDEX) -> dict[str, Any]:
        body = _default_packet_body(PacketId.TYRE_SETS)
        sets = list(body["tyre_set_data"])
        first = dict(self._tyre_set_template)
        if self._mode == "gp" and self._field is not None:
            car = self._field.cars[car_index]
            compound = car.compound
            age = min(MAX_TYRE_AGE_LAPS_IN_SENDER, car.tyre_age_base + self._current_lap - 1)
            wear = min(100, int(round(age * 2.2)))
            recommended = SESSION_TYPE_RACE
        else:
            compound = TYRE_COMPOUND_SOFT
            age = 0
            wear = 0
            recommended = SESSION_TYPE_TIME_TRIAL
        first.update(
            actual_tyre_compound=compound,
            visual_tyre_compound=compound,
            wear=wear,
            available=1,
            recommended_session=recommended,
            life_span=20,
            usable_life=20,
            lap_delta_time=0,
            fitted=1,
        )
        sets[0] = first
        body["car_idx"] = car_index
        body["tyre_set_data"] = sets
        body["fitted_idx"] = 0
        return body

    def _car_entries(self, template: list[dict[str, Any]], player: dict[str, Any]) -> list[dict[str, Any]]:
        entries = list(template)
        entries[PLAYER_CAR_INDEX] = player
        return entries

    def _lap_data_entry(
        self,
        run: _LapRun,
        sample: Sample,
        *,
        position: int = 1,
        delta_ahead_ms: int = 0,
        delta_leader_ms: int = 0,
        grid_position: int = 1,
    ) -> dict[str, Any]:
        entry = _default_struct_body(f2025.LAP_DATA)
        last = self._completed[-1] if self._completed else None
        sector1, sector2 = (0, 0)
        sector1_minutes, sector2_minutes = (0, 0)
        if last is not None:
            sector1 = last.sectors_ms[0]
            sector2 = last.sectors_ms[1]
            sector1_minutes, sector1 = _split_ms(sector1)
            sector2_minutes, sector2 = _split_ms(sector2)
        split1, split2 = sector_split_distances(self._track)
        sector = 0 if sample.dist_m < split1 else 1 if sample.dist_m < split2 else 2
        ahead_minutes, ahead_ms = _split_delta(delta_ahead_ms)
        leader_minutes, leader_ms = _split_delta(delta_leader_ms)
        entry.update(
            last_lap_time_in_ms=last.time_ms if last else 0,
            current_lap_time_in_ms=sample.t_ms,
            sector1_time_ms_part=sector1,
            sector1_time_minutes_part=sector1_minutes,
            sector2_time_ms_part=sector2,
            sector2_time_minutes_part=sector2_minutes,
            delta_to_car_in_front_ms_part=ahead_ms,
            delta_to_car_in_front_minutes_part=ahead_minutes,
            delta_to_race_leader_ms_part=leader_ms,
            delta_to_race_leader_minutes_part=leader_minutes,
            lap_distance=sample.dist_m,
            total_distance=(run.lap - 1) * self._track.length_m + sample.dist_m,
            car_position=position,
            current_lap_num=min(run.lap, 255),
            sector=sector,
            grid_position=grid_position,
            driver_status=4,  # On track
            result_status=2,  # Active
            speed_trap_fastest_speed=0.0,
        )
        if self._effects.penalty_seconds:
            entry["penalties"] = min(255, self._effects.penalty_seconds)
            entry["total_warnings"] = min(255, self._effects.total_warnings)
        return entry

    def _car_telemetry_entry(self, run: _LapRun, sample: Sample) -> dict[str, Any]:
        entry = _default_struct_body(f2025.CAR_TELEMETRY_DATA)
        rev = min(100, max(0, int(round((sample.rpm - 4000) / 8000 * 100))))
        brake_temp = self._effects.brake_temp_c
        brake_temps = (
            [brake_temp] * 4
            if brake_temp is not None
            else [420 + int(sample.brake * 180)] * 4
        )
        surface_temp = self._effects.tyre_surface_c
        inner_temp = self._effects.tyre_inner_c
        drs = 1 if self._effects.drs_active(run.lap) else 0
        entry.update(
            speed=min(65535, int(round(sample.speed_kmh))),
            throttle=max(0.0, min(1.0, sample.throttle)),
            steer=max(-1.0, min(1.0, sample.steer)),
            brake=max(0.0, min(1.0, sample.brake)),
            clutch=0,
            gear=max(0, min(8, sample.gear)),
            engine_rpm=max(0, min(65535, sample.rpm)),
            drs=drs,
            rev_lights_percent=rev,
            rev_lights_bit_value=0xFFFF if rev >= 95 else 0,
            brakes_temperature=[min(65535, value) for value in brake_temps],
            tyres_surface_temperature=[surface_temp if surface_temp is not None else 92] * 4,
            tyres_inner_temperature=[inner_temp if inner_temp is not None else 96] * 4,
            engine_temperature=105,
            tyres_pressure=[22.5] * 4,
            surface_type=[0] * 4,
        )
        return entry

    def _car_status_entry(self, run: _LapRun, sample: Sample) -> dict[str, Any]:
        entry = _default_struct_body(f2025.CAR_STATUS_DATA)
        progress = min(float(self._laps), (run.lap - 1) + sample.dist_m / self._track.length_m)
        remaining_laps = max(
            0.0, float(self._laps) - (run.lap - 1) - sample.dist_m / self._track.length_m
        )
        if self._effects.fuel_margin_laps is not None:
            fuel_remaining = max(0.0, remaining_laps - self._effects.fuel_margin_laps)
            fuel = min(FUEL_CAPACITY_KG, fuel_remaining * FUEL_PER_LAP_KG)
        else:
            fuel = max(0.0, FUEL_START_KG - FUEL_PER_LAP_KG * progress)
            fuel_remaining = (fuel / FUEL_PER_LAP_KG) if FUEL_PER_LAP_KG else 0.0
        tyre_age = (
            self._effects.tyre_age_override
            if self._effects.tyre_age_override is not None
            else min(255, max(0, run.lap - 1))
        )
        drs_allowed = 1 if self._effects.drs_active(run.lap) else 0
        entry.update(
            traction_control=0,
            anti_lock_brakes=0,
            fuel_mix=1,
            front_brake_bias=58,
            pit_limiter_status=0,
            fuel_in_tank=fuel,
            fuel_capacity=FUEL_CAPACITY_KG,
            fuel_remaining_laps=fuel_remaining,
            max_rpm=15_000,
            idle_rpm=4_000,
            max_gears=8,
            drs_allowed=drs_allowed,
            drs_activation_distance=(
                self._effects.drs_activation_distance if drs_allowed else 0
            ),
            actual_tyre_compound=TYRE_COMPOUND_SOFT,
            visual_tyre_compound=TYRE_COMPOUND_SOFT,
            tyres_age_laps=tyre_age,
            vehicle_fia_flags=0,
            engine_power_ice=700.0,
            engine_power_mguk=120.0,
            ers_store_energy=ERS_STORE_MAX_J * 0.9,
            ers_deploy_mode=2,
            ers_harvested_this_lap_mguk=500_000.0,
            ers_harvested_this_lap_mguh=500_000.0,
            ers_deployed_this_lap=2_000_000.0,
            network_paused=0,
        )
        return entry

    def _car_damage_entry(self, state: CarState | None = None) -> dict[str, Any]:
        entry = _default_struct_body(f2025.CAR_DAMAGE_DATA)
        if state is None or state.car_index == PLAYER_CAR_INDEX:
            wear = (
                self._effects.tyre_wear_percent
                if self._effects.tyre_wear_percent is not None
                else 0.5
            )
            wing = self._effects.front_wing_damage
        else:
            wear = min(100.0, state.tyre_age_laps * 2.2)
            wing = 0
        entry.update(
            tyres_wear=[wear] * 4,
            tyres_damage=[0] * 4,
            brakes_damage=[0] * 4,
            tyre_blisters=[0] * 4,
            front_left_wing_damage=min(255, wing),
            front_right_wing_damage=min(255, wing),
            rear_wing_damage=0,
            floor_damage=0,
            diffuser_damage=0,
            sidepod_damage=0,
            drs_fault=0,
            ers_fault=0,
            gear_box_damage=0,
            engine_damage=0,
            engine_mguh_wear=0,
            engine_es_wear=0,
            engine_ce_wear=0,
            engine_ice_wear=0,
            engine_mguk_wear=0,
            engine_tc_wear=0,
            engine_blown=0,
            engine_seized=0,
        )
        return entry

    def _motion_entry(self, sample: Sample) -> dict[str, Any]:
        entry = _default_struct_body(f2025.CAR_MOTION_DATA)
        speed_mps = sample.speed_kmh / 3.6
        velocity_x = speed_mps * math.cos(sample.yaw)
        velocity_z = speed_mps * math.sin(sample.yaw)
        forward_x = int(round(math.cos(sample.yaw) * 32767))
        forward_z = int(round(math.sin(sample.yaw) * 32767))
        right_x = int(round(math.sin(sample.yaw) * 32767))
        right_z = int(round(-math.cos(sample.yaw) * 32767))
        entry.update(
            world_position_x=sample.x,
            world_position_y=0.0,
            world_position_z=sample.z,
            world_velocity_x=velocity_x,
            world_velocity_y=0.0,
            world_velocity_z=velocity_z,
            world_forward_dir_x=forward_x,
            world_forward_dir_y=0,
            world_forward_dir_z=forward_z,
            world_right_dir_x=right_x,
            world_right_dir_y=0,
            world_right_dir_z=right_z,
            g_force_lateral=sample.lateral_g,
            g_force_longitudinal=sample.longitudinal_g,
            g_force_vertical=0.0,
            yaw=sample.yaw,
            pitch=-0.01,
            roll=0.0,
        )
        return entry

    def _motion_ex_entry(self, sample: Sample) -> dict[str, Any]:
        entry = _default_struct_body(f2025.MOTION_EX_BODY)
        speed_mps = sample.speed_kmh / 3.6
        entry.update(
            suspension_position=[0.02, 0.02, 0.03, 0.03],
            suspension_velocity=[0.0] * 4,
            suspension_acceleration=[0.0] * 4,
            wheel_speed=[speed_mps] * 4,
            wheel_slip_ratio=[0.0] * 4,
            wheel_slip_angle=[0.0] * 4,
            wheel_lat_force=[0.0] * 4,
            wheel_long_force=[0.0] * 4,
            height_of_cog_above_ground=0.30,
            local_velocity_x=speed_mps,
            local_velocity_y=0.0,
            local_velocity_z=0.0,
            angular_velocity_x=0.0,
            angular_velocity_y=0.0,
            angular_velocity_z=0.0,
            angular_acceleration_x=0.0,
            angular_acceleration_y=0.0,
            angular_acceleration_z=0.0,
            front_wheels_angle=sample.steer * 0.35,
            wheel_vert_force=[2000.0] * 4,
            front_aero_height=0.10,
            rear_aero_height=0.12,
            front_roll_angle=0.0,
            rear_roll_angle=0.0,
            chassis_yaw=sample.yaw,
            chassis_pitch=-0.01,
            wheel_camber=[-0.05] * 4,
            wheel_camber_gain=[0.0] * 4,
        )
        return entry

    def _session_history_body(
        self, car_index: int = PLAYER_CAR_INDEX, state: CarState | None = None
    ) -> dict[str, Any]:
        body = _default_packet_body(PacketId.SESSION_HISTORY)
        history = list(body["lap_history_data"])
        stints = list(body["tyre_stints_history_data"])
        if car_index != PLAYER_CAR_INDEX and state is not None:
            return self._ai_session_history_body(body, history, stints, car_index, state)
        completed = self._completed
        for index, result in enumerate(completed):
            lap_entry = _default_struct_body(f2025.LAP_HISTORY_DATA)
            minutes1, ms1 = _split_ms(result.sectors_ms[0])
            minutes2, ms2 = _split_ms(result.sectors_ms[1])
            minutes3, ms3 = _split_ms(result.sectors_ms[2])
            lap_entry.update(
                lap_time_in_ms=result.time_ms,
                sector1_time_ms_part=ms1,
                sector1_time_minutes_part=minutes1,
                sector2_time_ms_part=ms2,
                sector2_time_minutes_part=minutes2,
                sector3_time_ms_part=ms3,
                sector3_time_minutes_part=minutes3,
                lap_valid_bit_flags=0x0F,
            )
            history[index] = lap_entry

        if completed:
            stint = _default_struct_body(f2025.TYRE_STINT_HISTORY_DATA)
            stint.update(
                end_lap=min(255, completed[-1].lap),
                tyre_actual_compound=TYRE_COMPOUND_SOFT,
                tyre_visual_compound=TYRE_COMPOUND_SOFT,
            )
            stints[0] = stint

        best_lap = min(completed, key=lambda result: result.time_ms) if completed else None
        body.update(
            car_idx=PLAYER_CAR_INDEX,
            num_laps=min(255, len(completed)),
            num_tyre_stints=1 if completed else 0,
            best_lap_time_lap_num=best_lap.lap if best_lap else 0,
            best_sector1_lap_num=best_lap.lap if best_lap else 0,
            best_sector2_lap_num=best_lap.lap if best_lap else 0,
            best_sector3_lap_num=best_lap.lap if best_lap else 0,
            lap_history_data=history,
            tyre_stints_history_data=stints,
        )
        return body

    def _ai_session_history_body(
        self,
        body: dict[str, Any],
        history: list[dict[str, Any]],
        stints: list[dict[str, Any]],
        car_index: int,
        state: CarState,
    ) -> dict[str, Any]:
        """AI 차의 랩 기록. 데모는 차마다 고정 랩 타임을 반복한다."""
        assert self._field is not None
        car = self._field.cars[car_index]
        lap_count = min(100, max(0, state.lap - 1))
        third_ms = car.lap_time_ms // 3
        minutes, ms_part = _split_ms(third_ms)
        for index in range(lap_count):
            lap_entry = _default_struct_body(f2025.LAP_HISTORY_DATA)
            lap_entry.update(
                lap_time_in_ms=car.lap_time_ms,
                sector1_time_ms_part=ms_part,
                sector1_time_minutes_part=minutes,
                sector2_time_ms_part=ms_part,
                sector2_time_minutes_part=minutes,
                sector3_time_ms_part=car.lap_time_ms - 2 * third_ms,
                sector3_time_minutes_part=minutes,
                lap_valid_bit_flags=0x0F,
            )
            history[index] = lap_entry
        if lap_count:
            stint = _default_struct_body(f2025.TYRE_STINT_HISTORY_DATA)
            stint.update(
                end_lap=min(255, lap_count),
                tyre_actual_compound=car.compound,
                tyre_visual_compound=car.compound,
            )
            stints[0] = stint
        body.update(
            car_idx=car_index,
            num_laps=lap_count,
            num_tyre_stints=1 if lap_count else 0,
            best_lap_time_lap_num=1 if lap_count else 0,
            best_sector1_lap_num=1 if lap_count else 0,
            best_sector2_lap_num=1 if lap_count else 0,
            best_sector3_lap_num=1 if lap_count else 0,
            lap_history_data=history,
            tyre_stints_history_data=stints,
        )
        return body

    def _time_trial_body(self) -> dict[str, Any]:
        body = dict(self._time_trial_template)
        best = min(self._completed, key=lambda result: result.time_ms) if self._completed else None
        data = _default_struct_body(f2025.TIME_TRIAL_DATA_SET)
        if best is not None:
            data.update(
                car_idx=PLAYER_CAR_INDEX,
                team_id=0,
                lap_time_in_ms=best.time_ms,
                sector1_time_in_ms=best.sectors_ms[0],
                sector2_time_in_ms=best.sectors_ms[1],
                sector3_time_in_ms=best.sectors_ms[2],
                traction_control=0,
                gearbox_assist=0,
                anti_lock_brakes=0,
                equal_car_performance=1,
                custom_setup=1,
                valid=1,
            )
        body["player_session_best_data_set"] = data
        body["personal_best_data_set"] = data
        body["rival_data_set"] = _default_struct_body(f2025.TIME_TRIAL_DATA_SET)
        return body

    def _pack(self, packet_id: PacketId, body: dict[str, Any], time_ms: int) -> bytes:
        payload = pack_packet(self._header(packet_id, time_ms), body)
        expected = PACKET_SIZES[packet_id]
        if len(payload) != expected:
            raise RuntimeError(
                f"{packet_id.name} 길이가 규격과 다릅니다: {len(payload)} != {expected}"
            )
        return payload

    def _build_packet(self, packet_id: PacketId, tick: int) -> bytes:
        context = self._context(tick)
        run, sample = context.run, context.sample
        time_ms = int(round(tick * 1000.0 / BASE_RATE_HZ))

        if packet_id == PacketId.SESSION:
            if self._mode == "gp":
                body = dict(self._session_body)
                body["safety_car_status"] = self._effects.safety_car_status
            else:
                body = self._session_body
        elif packet_id == PacketId.LAP_DATA:
            if self._mode == "gp":
                body = self._gp_lap_data_body(context)
            else:
                template = self._templates[packet_id]
                body = dict(template)
                body["lap_data"] = self._car_entries(
                    template["lap_data"], self._lap_data_entry(run, sample)
                )
                body["time_trial_pb_car_idx"] = PLAYER_CAR_INDEX
                body["time_trial_rival_car_idx"] = SECONDARY_CAR_INDEX
        elif packet_id == PacketId.PARTICIPANTS:
            body = self._participants_body
        elif packet_id == PacketId.CAR_TELEMETRY:
            if self._mode == "gp":
                body = self._gp_car_telemetry_body(context)
            else:
                template = self._templates[packet_id]
                body = dict(template)
                body["car_telemetry_data"] = self._car_entries(
                    template["car_telemetry_data"], self._car_telemetry_entry(run, sample)
                )
                body["suggested_gear"] = sample.gear
        elif packet_id == PacketId.CAR_STATUS:
            if self._mode == "gp":
                body = self._gp_car_status_body(context)
            else:
                template = self._templates[packet_id]
                body = dict(template)
                body["car_status_data"] = self._car_entries(
                    template["car_status_data"], self._car_status_entry(run, sample)
                )
        elif packet_id == PacketId.CAR_DAMAGE:
            template = self._templates[packet_id]
            body = dict(template)
            entries = list(template["car_damage_data"])
            if self._mode == "gp":
                for state in context.states:
                    entries[state.car_index] = self._car_damage_entry(state)
            else:
                entries[PLAYER_CAR_INDEX] = self._car_damage_entry(None)
            body["car_damage_data"] = entries
        elif packet_id == PacketId.MOTION:
            template = self._templates[packet_id]
            body = dict(template)
            entries = list(template["car_motion_data"])
            if self._mode == "gp":
                for state in context.states:
                    if state.car_index == PLAYER_CAR_INDEX:
                        entries[PLAYER_CAR_INDEX] = self._motion_entry(sample)
                    else:
                        entries[state.car_index] = self._ai_motion_entry(state)
            else:
                entries[PLAYER_CAR_INDEX] = self._motion_entry(sample)
            body["car_motion_data"] = entries
        elif packet_id == PacketId.MOTION_EX:
            body = self._motion_ex_entry(sample)
        elif packet_id == PacketId.SESSION_HISTORY:
            if self._mode == "gp":
                body = self._gp_session_history_body(context)
            else:
                body = self._session_history_body()
        elif packet_id == PacketId.TYRE_SETS:
            if self._mode == "gp":
                body = self._gp_tyre_sets_body(context)
            else:
                body = self._build_tyre_sets_body()
        elif packet_id == PacketId.TIME_TRIAL:
            body = self._time_trial_body()
        else:  # pragma: no cover - PERIODIC_PACKETS와 어긋나면 바로 알린다.
            raise ValueError(f"데모가 보내지 않는 패킷: {packet_id}")

        return self._pack(packet_id, body, time_ms)

    # ------------------------------------------------------------------
    # 그랑프리 차 20대 패킷 본문

    def _ai_lap_data_entry(self, state: CarState) -> dict[str, Any]:
        entry = _default_struct_body(f2025.LAP_DATA)
        ahead_minutes, ahead_ms = _split_delta(state.delta_ahead_ms)
        leader_minutes, leader_ms = _split_delta(state.delta_leader_ms)
        minutes1, ms1 = _split_ms(state.sector1_ms)
        minutes2, ms2 = _split_ms(state.sector2_ms)
        entry.update(
            last_lap_time_in_ms=state.last_lap_ms if state.lap > 1 else 0,
            current_lap_time_in_ms=state.current_lap_ms,
            sector1_time_ms_part=ms1,
            sector1_time_minutes_part=minutes1,
            sector2_time_ms_part=ms2,
            sector2_time_minutes_part=minutes2,
            delta_to_car_in_front_ms_part=ahead_ms,
            delta_to_car_in_front_minutes_part=ahead_minutes,
            delta_to_race_leader_ms_part=leader_ms,
            delta_to_race_leader_minutes_part=leader_minutes,
            lap_distance=state.lap_distance_m,
            total_distance=state.total_distance_m,
            car_position=state.position,
            current_lap_num=min(255, state.lap),
            sector=state.sector,
            grid_position=min(255, state.car_index + 1),
            driver_status=4,
            result_status=2,
        )
        return entry

    def _gp_lap_data_body(self, context: _TickContext) -> dict[str, Any]:
        template = self._templates[PacketId.LAP_DATA]
        body = dict(template)
        entries = list(template["lap_data"])
        for state in context.states:
            if state.car_index == PLAYER_CAR_INDEX:
                entries[state.car_index] = self._lap_data_entry(
                    context.run,
                    context.sample,
                    position=state.position,
                    delta_ahead_ms=state.delta_ahead_ms,
                    delta_leader_ms=state.delta_leader_ms,
                    grid_position=GRID_START_POSITION,
                )
            else:
                entries[state.car_index] = self._ai_lap_data_entry(state)
        body["lap_data"] = entries
        body["time_trial_pb_car_idx"] = SECONDARY_CAR_INDEX
        body["time_trial_rival_car_idx"] = SECONDARY_CAR_INDEX
        return body

    def _ai_car_telemetry_entry(self, state: CarState) -> dict[str, Any]:
        entry = _default_struct_body(f2025.CAR_TELEMETRY_DATA)
        rev = min(100, max(0, int(round((state.rpm - 4000) / 8000 * 100))))
        entry.update(
            speed=min(65535, int(round(state.speed_kmh))),
            throttle=max(0.0, min(1.0, state.throttle)),
            steer=max(-1.0, min(1.0, state.steer)),
            brake=max(0.0, min(1.0, state.brake)),
            clutch=0,
            gear=max(0, min(8, state.gear)),
            engine_rpm=max(0, min(65535, state.rpm)),
            drs=0,
            rev_lights_percent=rev,
            rev_lights_bit_value=0xFFFF if rev >= 95 else 0,
            brakes_temperature=[min(65535, 420 + int(state.brake * 180))] * 4,
            tyres_surface_temperature=[90] * 4,
            tyres_inner_temperature=[94] * 4,
            engine_temperature=105,
            tyres_pressure=[22.5] * 4,
            surface_type=[0] * 4,
        )
        return entry

    def _gp_car_telemetry_body(self, context: _TickContext) -> dict[str, Any]:
        template = self._templates[PacketId.CAR_TELEMETRY]
        body = dict(template)
        entries = list(template["car_telemetry_data"])
        for state in context.states:
            if state.car_index == PLAYER_CAR_INDEX:
                entries[state.car_index] = self._car_telemetry_entry(
                    context.run, context.sample
                )
            else:
                entries[state.car_index] = self._ai_car_telemetry_entry(state)
        body["car_telemetry_data"] = entries
        body["suggested_gear"] = context.sample.gear
        return body

    def _ai_car_status_entry(self, state: CarState) -> dict[str, Any]:
        entry = _default_struct_body(f2025.CAR_STATUS_DATA)
        progress = min(
            float(self._laps), (state.lap - 1) + state.lap_distance_m / self._track.length_m
        )
        fuel = max(0.0, FUEL_START_KG - FUEL_PER_LAP_KG * progress)
        entry.update(
            traction_control=0,
            anti_lock_brakes=0,
            fuel_mix=1,
            front_brake_bias=58,
            pit_limiter_status=0,
            fuel_in_tank=fuel,
            fuel_capacity=FUEL_CAPACITY_KG,
            fuel_remaining_laps=(fuel / FUEL_PER_LAP_KG) if FUEL_PER_LAP_KG else 0.0,
            max_rpm=15_000,
            idle_rpm=4_000,
            max_gears=8,
            drs_allowed=0,
            drs_activation_distance=0,
            actual_tyre_compound=state.compound,
            visual_tyre_compound=state.compound,
            tyres_age_laps=min(255, state.tyre_age_laps),
            vehicle_fia_flags=0,
            engine_power_ice=700.0,
            engine_power_mguk=120.0,
            ers_store_energy=ERS_STORE_MAX_J * 0.9,
            ers_deploy_mode=2,
            ers_harvested_this_lap_mguk=500_000.0,
            ers_harvested_this_lap_mguh=500_000.0,
            ers_deployed_this_lap=2_000_000.0,
            network_paused=0,
        )
        return entry

    def _gp_car_status_body(self, context: _TickContext) -> dict[str, Any]:
        template = self._templates[PacketId.CAR_STATUS]
        body = dict(template)
        entries = list(template["car_status_data"])
        for state in context.states:
            if state.car_index == PLAYER_CAR_INDEX:
                entries[state.car_index] = self._car_status_entry(
                    context.run, context.sample
                )
            else:
                entries[state.car_index] = self._ai_car_status_entry(state)
        body["car_status_data"] = entries
        return body

    def _ai_motion_entry(self, state: CarState) -> dict[str, Any]:
        entry = _default_struct_body(f2025.CAR_MOTION_DATA)
        speed_mps = state.speed_kmh / 3.6
        entry.update(
            world_position_x=state.x,
            world_position_y=0.0,
            world_position_z=state.z,
            world_velocity_x=speed_mps * math.cos(state.yaw),
            world_velocity_y=0.0,
            world_velocity_z=speed_mps * math.sin(state.yaw),
            world_forward_dir_x=int(round(math.cos(state.yaw) * 32767)),
            world_forward_dir_y=0,
            world_forward_dir_z=int(round(math.sin(state.yaw) * 32767)),
            world_right_dir_x=int(round(math.sin(state.yaw) * 32767)),
            world_right_dir_y=0,
            world_right_dir_z=int(round(-math.cos(state.yaw) * 32767)),
            g_force_lateral=state.lateral_g,
            g_force_longitudinal=state.longitudinal_g,
            g_force_vertical=0.0,
            yaw=state.yaw,
            pitch=-0.01,
            roll=0.0,
        )
        return entry

    def _gp_session_history_body(self, context: _TickContext) -> dict[str, Any]:
        assert self._field is not None
        car_count = len(self._field.cars)
        car_index = self._history_cursor % car_count
        self._history_cursor += 1
        state = context.state(car_index)
        if state is None:
            raise RuntimeError(f"차 {car_index}의 상태가 없습니다")
        return self._session_history_body(car_index, state)

    def _gp_tyre_sets_body(self, context: _TickContext) -> dict[str, Any]:
        assert self._field is not None
        car_count = len(self._field.cars)
        car_index = self._tyre_sets_cursor % car_count
        self._tyre_sets_cursor += 1
        return self._build_tyre_sets_body(car_index)

    def _event_packet(self, code: str, details: dict[str, Any] | None, tick: int) -> bytes:
        body = dict(self._event_template)
        body["event_string_code"] = code
        if details is not None:
            body["event_details"] = details
        time_ms = int(round(tick * 1000.0 / BASE_RATE_HZ))
        return self._pack(PacketId.EVENT, body, time_ms)

    # ------------------------------------------------------------------
    # 송신

    def _entry_at(self, tick: int) -> tuple[_LapRun, Sample]:
        index = bisect.bisect_right(self._sample_ticks, tick) - 1
        index = max(0, min(index, len(self._sample_ticks) - 1))
        run = self._sample_runs[index]
        return run, run.samples[index - run.start_tick]

    def _context(self, tick: int) -> _TickContext:
        """한 틱의 샘플·차 상태. 같은 틱에서 여러 패킷을 만들 때 재사용한다."""
        if self._context_cache is not None and self._context_tick == tick:
            return self._context_cache
        index = bisect.bisect_right(self._sample_ticks, tick) - 1
        index = max(0, min(index, len(self._sample_ticks) - 1))
        run = self._sample_runs[index]
        sample = run.samples[index - run.start_tick]
        race_time_s = (
            self._run_start_ms[self._sample_run_index[index]] + sample.t_ms
        ) / 1000.0
        states: tuple[CarState, ...] = ()
        if self._field is not None:
            states = tuple(
                self._field.states_at(
                    race_time_s,
                    player_lap=run.lap,
                    player_lap_distance_m=sample.dist_m,
                    player_speed_kmh=sample.speed_kmh,
                )
            )
        context = _TickContext(
            run=run, sample=sample, race_time_s=race_time_s, states=states
        )
        self._context_cache = context
        self._context_tick = tick
        return context

    def _prepare_tick(self, tick: int) -> None:
        """랩 시작 시나리오 적용과 그랑프리 사건(추월·AI 랩) 감지를 한다."""
        context = self._context(tick)
        lap = self._laps if context.run.cooldown else context.run.lap
        if lap != self._current_lap:
            self._current_lap = lap
            self._start_lap(lap, tick, context)
            # 시나리오가 필드(간격·느린 탈출)를 바꿨을 수 있으니 다시 계산한다.
            self._context_cache = None
            context = self._context(tick)
        if self._field is not None:
            self._detect_ai_laps(context, tick)
            self._detect_overtakes(context, tick)

    def _start_lap(self, lap: int, tick: int, context: _TickContext) -> None:
        effects = self._effects
        if (
            effects.safety_car_status != SAFETY_CAR_NONE
            and lap > effects.safety_car_until_lap
        ):
            self._send(
                self._event_packet(
                    "SCAR",
                    {
                        "safety_car_type": effects.safety_car_type,
                        "event_type": SAFETY_CAR_EVENT_RETURNED,
                    },
                    tick,
                )
            )
            effects.safety_car_status = SAFETY_CAR_NONE
        if effects.drs_until_lap and lap > effects.drs_until_lap:
            effects.drs_until_lap = 0
            effects.drs_activation_distance = 0

        for index, event in enumerate(self._scenario_events):
            if event.lap != lap or index in self._triggered:
                continue
            self._triggered.add(index)
            self._apply_scenario(event, lap, tick, context)

    def _apply_scenario(
        self,
        event: ScenarioEvent,
        lap: int,
        tick: int,
        context: _TickContext,
    ) -> None:
        effects = self._effects
        kind = event.kind
        if kind == "close_to_car_ahead":
            if self._field is not None:
                self._field.set_gap_ahead(float(event.param("seconds")), start_lap=lap)
        elif kind == "drs_available":
            effects.drs_until_lap = lap + max(1, int(event.param("laps"))) - 1
            effects.drs_activation_distance = int(event.param("distance_m"))
        elif kind == "ahead_slow_exit":
            if self._field is not None and event.corner is not None:
                ahead = car_ahead(list(context.states))
                if ahead is not None:
                    self._field.set_slow_exit(
                        ahead.car_index,
                        event.corner,
                        float(event.param("speed_factor")),
                        start_lap=lap,
                    )
        elif kind == "tyre_age_gap":
            effects.tyre_age_override = min(255, int(event.param("laps")))
        elif kind == "tyre_wear_critical":
            effects.tyre_wear_percent = float(event.param("wear"))
        elif kind == "tyre_temp_critical":
            effects.tyre_surface_c = int(event.param("surface_c"))
            effects.tyre_inner_c = int(event.param("inner_c"))
        elif kind == "brake_temp_critical":
            effects.brake_temp_c = int(event.param("c"))
        elif kind == "fuel_short":
            effects.fuel_margin_laps = float(event.param("margin_laps"))
        elif kind == "front_wing_damage":
            effects.front_wing_damage = min(255, int(event.param("percent")))
        elif kind == "penalty":
            effects.penalty_seconds = max(
                effects.penalty_seconds, int(event.param("seconds"))
            )
            effects.total_warnings += 1
            self._send(
                self._event_packet(
                    "PENA",
                    {
                        "penalty_type": int(event.param("type")),
                        "infringement_type": int(event.param("infringement")),
                        "vehicle_idx": PLAYER_CAR_INDEX,
                        "other_vehicle_idx": SECONDARY_CAR_INDEX,
                        "time": min(255, int(event.param("seconds"))),
                        "lap_num": min(255, lap),
                        "places_gained": 0,
                    },
                    tick,
                )
            )
        elif kind in ("safety_car", "vsc"):
            if kind == "safety_car":
                effects.safety_car_status = SAFETY_CAR_FULL
                effects.safety_car_type = SAFETY_CAR_TYPE_FULL
            else:
                effects.safety_car_status = SAFETY_CAR_VIRTUAL
                effects.safety_car_type = SAFETY_CAR_TYPE_VIRTUAL
            effects.safety_car_until_lap = lap + max(1, int(event.param("laps"))) - 1
            self._send(
                self._event_packet(
                    "SCAR",
                    {
                        "safety_car_type": effects.safety_car_type,
                        "event_type": SAFETY_CAR_EVENT_DEPLOYED,
                    },
                    tick,
                )
            )
        # 주행 종류는 _build_lap_effects가 generate_lap에 이미 주입했다.

    def _detect_ai_laps(self, context: _TickContext, tick: int) -> None:
        """AI 차가 랩을 완주해 세션 최고 기록을 세우면 FTLP를 보낸다."""
        for state in context.states:
            if state.car_index == PLAYER_CAR_INDEX:
                continue
            previous = self._ai_laps_seen.get(state.car_index)
            self._ai_laps_seen[state.car_index] = state.lap
            if previous is None or state.lap <= previous:
                continue
            if self._session_best_ms is not None and state.last_lap_ms >= self._session_best_ms:
                continue
            self._session_best_ms = state.last_lap_ms
            self._send(
                self._event_packet(
                    "FTLP",
                    {
                        "vehicle_idx": state.car_index,
                        "lap_time": state.last_lap_ms / 1000.0,
                    },
                    tick,
                )
            )

    def _detect_overtakes(self, context: _TickContext, tick: int) -> None:
        """직전 틱과 순위가 바뀐 차의 OVTK 이벤트를 보낸다."""
        positions = {state.car_index: state.position for state in context.states}
        previous = self._previous_positions
        if previous is not None:
            previous_by_position = {position: car for car, position in previous.items()}
            for car_index, position in positions.items():
                old_position = previous.get(car_index)
                if old_position is None or position >= old_position:
                    continue
                other = previous_by_position.get(old_position - 1)
                if other is None or other == car_index:
                    continue
                me = context.state(car_index)
                them = context.state(other)
                if me is None or them is None or me.lap != them.lap:
                    continue  # 랩이 다르면 랩다운으로 보고 추월 이벤트를 만들지 않는다.
                self._send(
                    self._event_packet(
                        "OVTK",
                        {
                            "overtaking_vehicle_idx": car_index,
                            "being_overtaken_vehicle_idx": other,
                        },
                        tick,
                    )
                )
        self._previous_positions = positions

    def _complete_due_laps(self, tick: int) -> None:
        while (
            self._next_lap_end < len(self._lap_end_ticks)
            and tick >= self._lap_end_ticks[self._next_lap_end]
        ):
            run = self._runs[self._next_lap_end]
            result = LapResult(
                lap=run.lap,
                time_ms=lap_time_ms(run.samples),
                sectors_ms=sector_times_ms(run.samples, self._track),
            )
            if self._mode == "gp":
                is_best = (
                    self._session_best_ms is None or result.time_ms < self._session_best_ms
                )
                if is_best:
                    self._session_best_ms = result.time_ms
            else:
                is_best = not self._completed or result.time_ms < min(
                    lap.time_ms for lap in self._completed
                )
            self._completed.append(result)
            self._next_lap_end += 1
            if is_best:
                self._send(
                    self._event_packet(
                        "FTLP",
                        {
                            "vehicle_idx": PLAYER_CAR_INDEX,
                            "lap_time": result.time_ms / 1000.0,
                        },
                        tick,
                    )
                )

    def _send(self, payload: bytes) -> None:
        try:
            self._sock.sendto(payload, (self._host, self._port))
        except (ConnectionResetError, ConnectionRefusedError):
            # 수신기가 없으면 Windows가 ICMP 응답으로 연결 리셋을 알린다.
            # 게임 PC가 아니어도 데모가 멈추지 않게 무시한다.
            pass

    def run(
        self,
        *,
        duration_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """세션을 송신한다. `duration_s`는 테스트용으로 앞부분만 보낼 때 쓴다."""
        total_ticks = self._end_tick
        if duration_s is not None:
            if duration_s < 0:
                raise ValueError("duration_s는 0 이상이어야 합니다")
            total_ticks = min(total_ticks, int(round(duration_s * BASE_RATE_HZ)))
        started = clock()
        try:
            for tick in range(total_ticks):
                self._frame_id = tick
                if tick == 0:
                    self._send(self._event_packet("SSTA", None, tick))
                self._prepare_tick(tick)
                self._complete_due_laps(tick)
                for packet_id, period in self._periods.items():
                    if tick % period == 0:
                        self._send(self._build_packet(packet_id, tick))
                deadline = started + (tick + 1) / BASE_RATE_HZ / self._speed
                wait = deadline - clock()
                if wait > 0:
                    sleep(wait)
            if duration_s is None:
                self._frame_id = total_ticks
                self._complete_due_laps(total_ticks)
                self._send(self._event_packet("SEND", None, total_ticks))
        finally:
            if self._owns_socket:
                self._sock.close()

    # ------------------------------------------------------------------
    # 실행 정보

    @property
    def mode(self) -> str:
        return self._mode

    def summary_line(self) -> str:
        event = "타임 트라이얼" if self._mode == "tt" else "그랑프리"
        scenarios = (
            f", 시나리오 {len(self._scenario_events)}건" if self._scenario_events else ""
        )
        return (
            f"{self._track.name}, {event} {self._laps}랩, "
            f"{self._host}:{self._port}, {self._rate_hz}Hz, {self._speed:g}배{scenarios}"
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="f1re-demo",
        description="F1 25 UDP 규격 데모 송신기(타임 트라이얼·그랑프리 레이스).",
    )
    parser.add_argument(
        "--track",
        choices=sorted(TRACK_IDS),
        default="suzuka",
        help="서킷 이름(기본 suzuka)",
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="tt",
        help="tt=타임 트라이얼, gp=그랑프리 레이스(AI 19대 + 내 차)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="대상 IP")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="대상 UDP 포트")
    parser.add_argument(
        "--laps",
        type=int,
        default=None,
        help=f"랩 수(기본: tt {DEFAULT_LAPS_TT}, gp {DEFAULT_LAPS_GP})",
    )
    parser.add_argument("--speed", type=float, default=1.0, help="재생 속도 배율")
    parser.add_argument("--seed", type=int, default=0, help="시나리오·주행 시드")
    parser.add_argument(
        "--rate",
        type=int,
        choices=TELEMETRY_RATES_HZ,
        default=60,
        help="텔레메트리 송신 주기(Hz, 기본 60)",
    )
    parser.add_argument(
        "--scenario",
        type=Path,
        action="append",
        default=None,
        help="시나리오 YAML 파일(여러 번 줄 수 있음)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    laps = args.laps if args.laps is not None else (
        DEFAULT_LAPS_TT if args.mode == "tt" else DEFAULT_LAPS_GP
    )
    if laps < 1:
        print("--laps는 1 이상이어야 합니다.", file=sys.stderr)
        return 2

    track_id = TRACK_IDS[args.track]
    track = load_track(track_id)
    outline = load_outline(track_id)
    if track is None or outline is None:
        print(f"서킷 데이터를 불러오지 못했습니다: {args.track}", file=sys.stderr)
        return 2

    scenarios: list[ScenarioEvent] = []
    for path in args.scenario or ():
        try:
            loaded = load_scenarios(path)
        except ScenarioError as exc:
            print(f"시나리오를 읽지 못했습니다: {exc}", file=sys.stderr)
            return 2
        scenarios.extend(loaded)
        print(f"시나리오 {len(loaded)}건을 읽었습니다: {path}")

    try:
        sender = DemoSender(
            track,
            outline,
            laps=laps,
            seed=args.seed,
            host=args.host,
            port=args.port,
            rate_hz=args.rate,
            speed=args.speed,
            mode=args.mode,
            scenarios=scenarios,
        )
    except (ScenarioError, ValueError) as exc:
        print(f"데모 송신기를 만들지 못했습니다: {exc}", file=sys.stderr)
        return 2
    print(f"F1 25 데모 송신기: {sender.summary_line()}")
    print("예상 랩 타임:", ", ".join(f"{ms / 1000:.2f}s" for ms in sender.lap_times_ms))
    try:
        sender.run()
    except KeyboardInterrupt:
        print("송신을 중단했습니다.")
        return 0
    print("송신을 마쳤습니다.")
    for result in sender.lap_results:
        sectors = "/".join(f"{ms / 1000:.2f}" for ms in result.sectors_ms)
        print(f"  랩 {result.lap}: {result.time_ms / 1000:.2f}s ({sectors})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
