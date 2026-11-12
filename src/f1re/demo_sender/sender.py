"""F1 25 UDP 데모 송신기 – 타임 트라이얼.

게임 없이 수신기·코칭을 시험할 수 있도록, F1 25 공식 UDP 규격과 같은 바이트를
127.0.0.1(기본)로 보낸다. 패킷 구조체는 `f1re.udp.formats.f2025`의 정의와
`pack_packet`을 그대로 쓰므로 파서와 같은 규격 이해를 공유한다.

데모는 서킷 데이터(`load_track`·`load_outline`)의 중심선을 따라 만든 주행
샘플로 패킷을 채운다. 패킷별 송신 주기는 `f1re.udp.spec.PACKET_RATES_HZ`를
따르고, 게임 설정과 같은 기본 60Hz(20/30Hz로 낮출 수 있음)를 쓴다.
"""

from __future__ import annotations

import argparse
import bisect
import math
import random
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from f1re.demo_sender.driving import (
    DEFAULT_RATE_HZ,
    DriverProfile,
    Sample,
    generate_lap,
    lap_time_ms,
    sector_split_distances,
    sector_times_ms,
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
SESSION_TYPE_TIME_TRIAL = 13
GAME_MODE_TIME_TRIAL = 2
RULE_SET_TIME_TRIAL = 2
WEATHER_CLEAR = 0

PLAYER_CAR_INDEX = 0
SECONDARY_CAR_INDEX = 255
DEFAULT_LAPS_TT = 3
COOLDOWN_S = 1.0  # 마지막 랩 완료를 수신기가 볼 수 있게 잠깐 더 보낸다.
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


class DemoSender:
    """타임 트라이얼 데모 패킷을 UDP로 보낸다."""

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
        sock: socket.socket | None = None,
    ) -> None:
        if laps < 1:
            raise ValueError("laps는 1 이상이어야 합니다")
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

        self._session_uid = 0x5EED_0000_0000_0000 | (seed & 0xFFFF_FFFF)
        self._frame_id = 0
        self._completed: list[LapResult] = []
        self._next_lap_end = 0
        self._owns_socket = sock is None
        self._sock = sock if sock is not None else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._runs = self._build_runs(track, outline, driver or DriverProfile(), laps, seed)
        self._lap_end_ticks = self._build_lap_end_ticks(self._runs)
        self._end_tick = sum(run.ticks for run in self._runs)
        self._sample_ticks: list[int] = []
        self._sample_runs: list[_LapRun] = []
        for run in self._runs:
            for index in range(run.ticks):
                self._sample_ticks.append(run.start_tick + index)
                self._sample_runs.append(run)

        self._periods = self._packet_periods(rate_hz)
        self._templates = {
            packet_id: _default_packet_body(packet_id) for packet_id in PERIODIC_PACKETS
        }
        self._session_body = self._build_session_body()
        self._participants_body = self._build_participants_body()
        self._tyre_sets_body = self._build_tyre_sets_body()
        self._time_trial_template = _default_packet_body(PacketId.TIME_TRIAL)
        self._event_template = _default_packet_body(PacketId.EVENT)

    # ------------------------------------------------------------------
    # 세션 구성

    @staticmethod
    def _build_runs(
        track: Track,
        outline: TrackOutline,
        driver: DriverProfile,
        laps: int,
        seed: int,
    ) -> list[_LapRun]:
        rng = random.Random(seed)
        runs: list[_LapRun] = []
        for lap in range(1, laps + 1):
            samples = generate_lap(track, outline, driver, lap, rng)
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
    def _packet_periods(rate_hz: int) -> dict[PacketId, int]:
        """패킷별 송신 주기를 60Hz 틱 수로 바꾼다."""
        periods: dict[PacketId, int] = {}
        for packet_id in PERIODIC_PACKETS:
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
        return body

    def _build_participants_body(self) -> dict[str, Any]:
        body = _default_packet_body(PacketId.PARTICIPANTS)
        participants = list(body["participants"])
        player = _default_struct_body(f2025.PARTICIPANT_DATA)
        player.update(
            ai_controlled=0,
            driver_id=255,  # 공식 문서: 플레이어 차는 255.
            network_id=255,
            team_id=0,
            my_team=0,
            race_number=1,
            nationality=0,
            name="YOU",
            your_telemetry=1,
            show_online_names=1,
            tech_level=0,
            platform=0,
            num_colours=2,
        )
        player["livery_colours"] = [
            {"red": 192, "green": 192, "blue": 192},
            {"red": 32, "green": 32, "blue": 32},
            {"red": 0, "green": 0, "blue": 0},
            {"red": 0, "green": 0, "blue": 0},
        ]
        participants[PLAYER_CAR_INDEX] = player
        body["num_active_cars"] = 1
        body["participants"] = participants
        return body

    def _build_tyre_sets_body(self) -> dict[str, Any]:
        body = _default_packet_body(PacketId.TYRE_SETS)
        sets = list(body["tyre_set_data"])
        first = _default_struct_body(f2025.TYRE_SET_DATA)
        first.update(
            actual_tyre_compound=TYRE_COMPOUND_SOFT,
            visual_tyre_compound=TYRE_COMPOUND_SOFT,
            wear=0,
            available=1,
            recommended_session=SESSION_TYPE_TIME_TRIAL,
            life_span=20,
            usable_life=20,
            lap_delta_time=0,
            fitted=1,
        )
        sets[0] = first
        body["car_idx"] = PLAYER_CAR_INDEX
        body["tyre_set_data"] = sets
        body["fitted_idx"] = 0
        return body

    def _car_entries(self, template: list[dict[str, Any]], player: dict[str, Any]) -> list[dict[str, Any]]:
        entries = list(template)
        entries[PLAYER_CAR_INDEX] = player
        return entries

    def _lap_data_entry(self, run: _LapRun, sample: Sample) -> dict[str, Any]:
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
        entry.update(
            last_lap_time_in_ms=last.time_ms if last else 0,
            current_lap_time_in_ms=sample.t_ms,
            sector1_time_ms_part=sector1,
            sector1_time_minutes_part=sector1_minutes,
            sector2_time_ms_part=sector2,
            sector2_time_minutes_part=sector2_minutes,
            lap_distance=sample.dist_m,
            total_distance=(run.lap - 1) * self._track.length_m + sample.dist_m,
            car_position=1,
            current_lap_num=min(run.lap, 255),
            sector=sector,
            grid_position=1,
            driver_status=4,  # On track
            result_status=2,  # Active
            speed_trap_fastest_speed=0.0,
        )
        return entry

    def _car_telemetry_entry(self, run: _LapRun, sample: Sample) -> dict[str, Any]:
        entry = _default_struct_body(f2025.CAR_TELEMETRY_DATA)
        rev = min(100, max(0, int(round((sample.rpm - 4000) / 8000 * 100))))
        entry.update(
            speed=min(65535, int(round(sample.speed_kmh))),
            throttle=max(0.0, min(1.0, sample.throttle)),
            steer=max(-1.0, min(1.0, sample.steer)),
            brake=max(0.0, min(1.0, sample.brake)),
            clutch=0,
            gear=max(0, min(8, sample.gear)),
            engine_rpm=max(0, min(65535, sample.rpm)),
            drs=0,
            rev_lights_percent=rev,
            rev_lights_bit_value=0xFFFF if rev >= 95 else 0,
            brakes_temperature=[420 + int(sample.brake * 180)] * 4,
            tyres_surface_temperature=[92] * 4,
            tyres_inner_temperature=[96] * 4,
            engine_temperature=105,
            tyres_pressure=[22.5] * 4,
            surface_type=[0] * 4,
        )
        return entry

    def _car_status_entry(self, run: _LapRun, sample: Sample) -> dict[str, Any]:
        entry = _default_struct_body(f2025.CAR_STATUS_DATA)
        progress = min(float(self._laps), (run.lap - 1) + sample.dist_m / self._track.length_m)
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
            actual_tyre_compound=TYRE_COMPOUND_SOFT,
            visual_tyre_compound=TYRE_COMPOUND_SOFT,
            tyres_age_laps=min(255, max(0, run.lap - 1)),
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

    def _session_history_body(self) -> dict[str, Any]:
        body = _default_packet_body(PacketId.SESSION_HISTORY)
        history = list(body["lap_history_data"])
        stints = list(body["tyre_stints_history_data"])
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
        run, sample = self._entry_at(tick)
        time_ms = int(round(tick * 1000.0 / BASE_RATE_HZ))

        if packet_id == PacketId.SESSION:
            body = self._session_body
        elif packet_id == PacketId.LAP_DATA:
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
            template = self._templates[packet_id]
            body = dict(template)
            body["car_telemetry_data"] = self._car_entries(
                template["car_telemetry_data"], self._car_telemetry_entry(run, sample)
            )
            body["suggested_gear"] = sample.gear
        elif packet_id == PacketId.CAR_STATUS:
            template = self._templates[packet_id]
            body = dict(template)
            body["car_status_data"] = self._car_entries(
                template["car_status_data"], self._car_status_entry(run, sample)
            )
        elif packet_id == PacketId.CAR_DAMAGE:
            template = self._templates[packet_id]
            body = dict(template)
            player = _default_struct_body(f2025.CAR_DAMAGE_DATA)
            player.update(
                tyres_wear=[0.5] * 4,
                tyres_damage=[0] * 4,
                brakes_damage=[0] * 4,
                tyre_blisters=[0] * 4,
                front_left_wing_damage=0,
                front_right_wing_damage=0,
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
            body["car_damage_data"] = self._car_entries(
                template["car_damage_data"], player
            )
        elif packet_id == PacketId.MOTION:
            template = self._templates[packet_id]
            body = dict(template)
            body["car_motion_data"] = self._car_entries(
                template["car_motion_data"], self._motion_entry(sample)
            )
        elif packet_id == PacketId.MOTION_EX:
            body = self._motion_ex_entry(sample)
        elif packet_id == PacketId.SESSION_HISTORY:
            body = self._session_history_body()
        elif packet_id == PacketId.TYRE_SETS:
            body = self._tyre_sets_body
        elif packet_id == PacketId.TIME_TRIAL:
            body = self._time_trial_body()
        else:  # pragma: no cover - PERIODIC_PACKETS와 어긋나면 바로 알린다.
            raise ValueError(f"데모가 보내지 않는 패킷: {packet_id}")

        return self._pack(packet_id, body, time_ms)

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

    def summary_line(self) -> str:
        return (
            f"{self._track.name}, 타임 트라이얼 {self._laps}랩, "
            f"{self._host}:{self._port}, {self._rate_hz}Hz, {self._speed:g}배"
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="f1re-demo",
        description="F1 25 UDP 규격 데모 송신기(타임 트라이얼).",
    )
    parser.add_argument(
        "--track",
        choices=sorted(TRACK_IDS),
        default="suzuka",
        help="서킷 이름(기본 suzuka)",
    )
    parser.add_argument(
        "--mode",
        choices=("tt", "gp"),
        default="tt",
        help="tt=타임 트라이얼(이번 구현), gp=그랑프리(작업 5)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="대상 IP")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="대상 UDP 포트")
    parser.add_argument("--laps", type=int, default=DEFAULT_LAPS_TT, help="랩 수")
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
        default=None,
        help="시나리오 파일(작업 5에서 구현)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.mode != "tt":
        print("그랑프리 모드는 아직 구현되지 않았습니다(작업 5).", file=sys.stderr)
        return 2
    if args.scenario is not None:
        print("시나리오 주입은 아직 구현되지 않았습니다(작업 5).", file=sys.stderr)
        return 2
    if args.laps < 1:
        print("--laps는 1 이상이어야 합니다.", file=sys.stderr)
        return 2

    track_id = TRACK_IDS[args.track]
    track = load_track(track_id)
    outline = load_outline(track_id)
    if track is None or outline is None:
        print(f"서킷 데이터를 불러오지 못했습니다: {args.track}", file=sys.stderr)
        return 2

    sender = DemoSender(
        track,
        outline,
        laps=args.laps,
        seed=args.seed,
        host=args.host,
        port=args.port,
        rate_hz=args.rate,
        speed=args.speed,
    )
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
