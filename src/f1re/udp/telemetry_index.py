"""다시 보기 텔레메트리 분석용 1회 스캔 시계열 인덱스(SCRUM-134).

`/telemetry` 화면은 35분짜리 녹화(`.f1rec`/`.f1rec.zst`)에서 "언제 타이어
온도가 치솟았는지", "무전이 울린 순간 스로틀이 어땠는지"를 전체 시간축에서
훑고 특정 코너 구간만 확대해 본다. 브라우저가 녹화 원본을 받아 파싱하면
수백 MB를 전송해야 하므로, 서버가 녹화를 **한 번만 순차 스캔**해 다음을
메모리에 만든다(설계 5.1).

1. **내 차 원시 시계열**(`CarSeries`): `CarTelemetry`·`Motion`·`LapData`·
   `CarStatus`·`CarDamage`가 도착할 때마다 메트릭 채널(`array('i')` 시각 +
   `array('f')` 값)에 쌓는다. 숫자 배열만 두어 35분 60Hz 세션이 수십 MB에
   머문다.
2. **10Hz 지도·순위 프레임**(`map_frames`): 100ms 버킷마다 22대 위치·순위·
   피트 상태와 깃발 요약을 압축(`array('f')` 한 줄 + 공유 깃발 딕셔너리)해
   둔다. `TrackMap.car_positions()`로 임의 시각의 좌표를 즉시 계산하므로
   그래프 호버가 전역 `ReplayManager.seek()`를 부르지 않는다.
3. **무전·사건 마커**(`markers`): `StateStore`+`RacePipeline(conn=None)`+
   `AlertRules`+`MemoryRadioSink` 조합(기존 `web_replay.py`와 같다)을 1회
   통과시켜 무전 발화와 SC/VSC/적기 구간, 내 차 피트·페널티·추월·플래시백,
   랩 경계를 모은다. 22대 전체 `lap_complete` 같은 스팸은 넣지 않고, 비교
   차량용으로 차량별 경량 이벤트 맵만 따로 둔다.

**메모리 상한**(설계 3): 최근 연 1건 + 비교 차량 1대만 캐시한다. 비교 차량은
`extract_car_series()`가 선택 시점에 파일을 한 번 더 스캔해 채우고,
`TelemetryIndexStore`가 LRU=1로 유지한다. 무거운 인덱싱은 호출자가
`asyncio.to_thread`로 이벤트 루프 밖에서 실행한다.

**비공개 텔레메트리**: F1 25에서 `your_telemetry == 0`인 드라이버도 `LapData`·
`Motion`은 정상 수신되고 `CarTelemetry`·`CarStatus`·`CarDamage`만 0으로
채워진다. 그래서 원천이 그 셋인 메트릭만 `compare_series: null`,
`compare_restricted: true`로 억제한다(설계 5.1.C).

**타이어 배열 순서**: F1 25는 `0=RL, 1=RR, 2=FL, 3=FR`이고 화면 키는
`_fl=2, _fr=3, _rl=0, _rr=1`이다. `web/app.py`의 `_TYRE_DISPLAY_ORDER`와
같은 매핑을 `WHEEL_INDEX_BY_SUFFIX`에 고정한다.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import math
import sqlite3
import threading
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from f1re.alerts.rules import AlertRules
from f1re.config import AppConfig
from f1re.pipeline import RacePipeline
from f1re.radio.queue import RadioQueue
from f1re.state.events import Event, EventKind
from f1re.state.flags import flag_summary
from f1re.state.store import StateStore
from f1re.udp.parser import UnsupportedPacket, parse_packet
from f1re.udp.recording import iter_recording
from f1re.udp.replay_radio import MemoryRadioLog, MemoryRadioSink
from f1re.udp.spec import PacketId
from f1re.web.map_api import TEAM_COLORS, TrackMap

logger = logging.getLogger(__name__)

# 지도·순위 프레임 버킷(ms). 설계 5.1의 10Hz 타임라인과 같다.
MAP_FRAME_BUCKET_MS = 100

# 위험 경고 판정 주기(ns). `web_replay.ALERT_EVAL_INTERVAL_NS`와 같은 10Hz다.
ALERT_EVAL_INTERVAL_NS = 100_000_000

# 다운샘플 응답의 기본·최대 점 수(설계 5.1.D). API가 이 범위로 클램핑한다.
DEFAULT_MAX_POINTS = 800
MAX_POINTS_LIMIT = 2000

# ERS 저장 최대 에너지(J). F1 규정상 4MJ이며 잔량 %·이번 랩 사용 % 계산에 쓴다.
ERS_STORE_MAX_J = 4_000_000.0

# 4륜 항목의 화면 키 → F1 25 배열 인덱스·한국어 이름.
# `web/app.py`의 `_TYRE_DISPLAY_ORDER`(2=앞왼쪽, 3=앞오른쪽, 0=뒤왼쪽, 1=뒤오른쪽)와
# 반드시 같아야 한다.
WHEEL_INDEX_BY_SUFFIX: tuple[tuple[str, int, str], ...] = (
    ("fl", 2, "앞왼쪽"),
    ("fr", 3, "앞오른쪽"),
    ("rl", 0, "뒤왼쪽"),
    ("rr", 1, "뒤오른쪽"),
)

# 지도 프레임 한 칸의 차량 숫자 폭(자, 거리, x, z, 팀, 순위, 랩, 피트, 결과).
FRAME_CAR_WIDTH = 9

# 한 차의 지도 프레임 숫자 필드 이름(디코딩 순서).
FRAME_CAR_FIELDS = (
    "car",
    "dist_m",
    "x",
    "z",
    "team_id",
    "position",
    "lap",
    "pit_status",
    "result_status",
)


# ---------------------------------------------------------------------------
# 메트릭 카탈로그
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelemetryMetricSpec:
    """메트릭 하나의 정의. 화면 라벨·단위·색 그룹의 서버 단일 출처다."""

    key: str
    label: str
    category_id: str
    unit: str
    unit_group: str
    source_packet: str
    value_range: tuple[float, float] | None
    step: bool
    restricted_when_private: bool
    wheel_group: str | None = None
    wheel_label: str | None = None


@dataclass(frozen=True)
class TelemetryCategorySpec:
    """카테고리 하나와 그 항목들(카테고리 → 항목 2단계 체크박스)."""

    id: str
    label: str
    metrics: tuple[TelemetryMetricSpec, ...]


def _wheel_metrics(
    base_key: str,
    label: str,
    unit: str,
    unit_group: str,
    value_range: tuple[float, float] | None,
    source_packet: str,
) -> tuple[TelemetryMetricSpec, ...]:
    """4륜 항목을 `_fl/_fr/_rl/_rr` 네 메트릭으로 편다.

    `wheel_group`이 같으면 프런트가 한 줄 `[앞좌][앞우][뒤좌][뒤우]` 그룹으로
    묶어 보여 주고, 바퀴별로 따로 켤 수도 있다.
    """
    return tuple(
        TelemetryMetricSpec(
            key=f"{base_key}_{suffix}",
            label=label,
            category_id="tyres_brakes",
            unit=unit,
            unit_group=unit_group,
            source_packet=source_packet,
            value_range=value_range,
            step=False,
            restricted_when_private=source_packet
            in ("CarTelemetry", "CarStatus", "CarDamage"),
            wheel_group=base_key,
            wheel_label=wheel_label,
        )
        for suffix, _index, wheel_label in WHEEL_INDEX_BY_SUFFIX
    )


_DRIVING_METRICS: tuple[TelemetryMetricSpec, ...] = (
    TelemetryMetricSpec(
        "speed", "속도", "driving", "km/h", "speed", "CarTelemetry", (0.0, 360.0), False, True
    ),
    TelemetryMetricSpec(
        "throttle", "스로틀", "driving", "%", "percent", "CarTelemetry", (0.0, 100.0), False, True
    ),
    TelemetryMetricSpec(
        "brake", "브레이크", "driving", "%", "percent", "CarTelemetry", (0.0, 100.0), False, True
    ),
    TelemetryMetricSpec(
        "steer", "조향", "driving", "", "steer", "CarTelemetry", (-1.0, 1.0), False, True
    ),
    TelemetryMetricSpec(
        "gear", "기어", "driving", "단", "gear", "CarTelemetry", (0.0, 8.0), True, True
    ),
    TelemetryMetricSpec(
        "engine_rpm",
        "RPM",
        "driving",
        "RPM",
        "rpm",
        "CarTelemetry",
        (0.0, 15000.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "drs", "DRS", "driving", "0/1", "drs", "CarTelemetry", (0.0, 2.0), True, True
    ),
    TelemetryMetricSpec(
        "clutch", "클러치", "driving", "%", "percent", "CarTelemetry", (0.0, 100.0), False, True
    ),
    TelemetryMetricSpec(
        "g_lat", "횡 G", "driving", "G", "g", "Motion", (-6.0, 6.0), False, False
    ),
    TelemetryMetricSpec(
        "g_lon", "종 G", "driving", "G", "g", "Motion", (-6.0, 6.0), False, False
    ),
    TelemetryMetricSpec(
        "g_vert", "수직 G", "driving", "G", "g", "Motion", (-6.0, 6.0), False, False
    ),
)

_TYRES_BRAKES_METRICS: tuple[TelemetryMetricSpec, ...] = (
    *_wheel_metrics(
        "tyre_surface_temp", "타이어 표면 온도", "°C", "temperature", (0.0, 150.0), "CarTelemetry"
    ),
    *_wheel_metrics(
        "tyre_inner_temp", "타이어 내심 온도", "°C", "temperature", (0.0, 150.0), "CarTelemetry"
    ),
    *_wheel_metrics(
        "tyre_pressure", "타이어 공기압", "psi", "pressure", (0.0, 40.0), "CarTelemetry"
    ),
    *_wheel_metrics(
        "tyre_wear", "타이어 마모", "%", "percent", (0.0, 100.0), "CarDamage"
    ),
    *_wheel_metrics(
        "brake_temp", "브레이크 온도", "°C", "temperature", (0.0, 1200.0), "CarTelemetry"
    ),
)

_ENERGY_FUEL_METRICS: tuple[TelemetryMetricSpec, ...] = (
    TelemetryMetricSpec(
        "ers_pct", "ERS 잔량", "energy_fuel", "%", "percent", "CarStatus", (0.0, 100.0), False, True
    ),
    TelemetryMetricSpec(
        "ers_deployed_pct",
        "ERS 이번 랩 사용",
        "energy_fuel",
        "%",
        "percent",
        "CarStatus",
        (0.0, 100.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "ers_mguk_j",
        "MGU-K 회수",
        "energy_fuel",
        "MJ",
        "energy",
        "CarStatus",
        (0.0, 2.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "ers_mguh_j",
        "MGU-H 회수",
        "energy_fuel",
        "MJ",
        "energy",
        "CarStatus",
        (0.0, 2.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "ers_mode",
        "ERS 모드",
        "energy_fuel",
        "",
        "ers_mode",
        "CarStatus",
        (0.0, 4.0),
        True,
        True,
    ),
    TelemetryMetricSpec(
        "fuel_kg", "연료량", "energy_fuel", "kg", "fuel", "CarStatus", (0.0, 110.0), False, True
    ),
    TelemetryMetricSpec(
        "fuel_remaining_laps",
        "연료 여유 랩",
        "energy_fuel",
        "랩",
        "laps",
        "CarStatus",
        (-10.0, 10.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "engine_temp",
        "엔진 온도",
        "energy_fuel",
        "°C",
        "temperature",
        "CarTelemetry",
        (0.0, 150.0),
        False,
        True,
    ),
)

_LAP_PACE_METRICS: tuple[TelemetryMetricSpec, ...] = (
    TelemetryMetricSpec(
        "current_lap",
        "현재 랩",
        "lap_pace",
        "랩",
        "lap",
        "LapData",
        (0.0, 100.0),
        True,
        False,
    ),
    TelemetryMetricSpec(
        "current_lap_time_s",
        "랩 경과 시간",
        "lap_pace",
        "s",
        "seconds",
        "LapData",
        (0.0, 300.0),
        False,
        False,
    ),
    TelemetryMetricSpec(
        "gap_front_s",
        "앞차 간격",
        "lap_pace",
        "s",
        "seconds",
        "LapData",
        (-1.0, 120.0),
        False,
        False,
    ),
    TelemetryMetricSpec(
        "gap_leader_s",
        "선두 간격",
        "lap_pace",
        "s",
        "seconds",
        "LapData",
        (-1.0, 300.0),
        False,
        False,
    ),
    TelemetryMetricSpec(
        "car_position",
        "순위",
        "lap_pace",
        "위",
        "position",
        "LapData",
        (1.0, 22.0),
        True,
        False,
    ),
    TelemetryMetricSpec(
        "safety_car_delta",
        "SC 델타",
        "lap_pace",
        "s",
        "seconds",
        "LapData",
        (-60.0, 180.0),
        False,
        False,
    ),
)

_DAMAGE_SETUP_METRICS: tuple[TelemetryMetricSpec, ...] = (
    TelemetryMetricSpec(
        "wing_fl_damage",
        "앞날개(왼쪽) 손상",
        "damage_setup",
        "%",
        "percent",
        "CarDamage",
        (0.0, 100.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "wing_fr_damage",
        "앞날개(오른쪽) 손상",
        "damage_setup",
        "%",
        "percent",
        "CarDamage",
        (0.0, 100.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "rear_wing_damage",
        "뒷날개 손상",
        "damage_setup",
        "%",
        "percent",
        "CarDamage",
        (0.0, 100.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "floor_damage",
        "바닥 손상",
        "damage_setup",
        "%",
        "percent",
        "CarDamage",
        (0.0, 100.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "diffuser_damage",
        "디퓨저 손상",
        "damage_setup",
        "%",
        "percent",
        "CarDamage",
        (0.0, 100.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "sidepod_damage",
        "사이드포드 손상",
        "damage_setup",
        "%",
        "percent",
        "CarDamage",
        (0.0, 100.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "gearbox_damage",
        "기어박스 손상",
        "damage_setup",
        "%",
        "percent",
        "CarDamage",
        (0.0, 100.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "engine_damage",
        "엔진 손상",
        "damage_setup",
        "%",
        "percent",
        "CarDamage",
        (0.0, 100.0),
        False,
        True,
    ),
    TelemetryMetricSpec(
        "brake_bias",
        "브레이크 밸런스",
        "damage_setup",
        "%",
        "percent",
        "CarStatus",
        (0.0, 100.0),
        False,
        True,
    ),
)

#: 카테고리 순서와 항목 정의. 화면 체크박스 트리는 이 순서를 그대로 쓴다.
TELEMETRY_CATALOG: tuple[TelemetryCategorySpec, ...] = (
    TelemetryCategorySpec("driving", "주행 입력", _DRIVING_METRICS),
    TelemetryCategorySpec("tyres_brakes", "타이어 · 브레이크", _TYRES_BRAKES_METRICS),
    TelemetryCategorySpec("energy_fuel", "에너지 · 연료", _ENERGY_FUEL_METRICS),
    TelemetryCategorySpec("lap_pace", "랩 · 레이스 간격", _LAP_PACE_METRICS),
    TelemetryCategorySpec("damage_setup", "손상 · 밸런스", _DAMAGE_SETUP_METRICS),
)

_METRIC_SPECS: dict[str, TelemetryMetricSpec] = {
    metric.key: metric for category in TELEMETRY_CATALOG for metric in category.metrics
}

#: 원클릭 프리셋. 그래프 위 버튼이 이 메트릭 묶음을 한 번에 켠다.
TELEMETRY_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "driving",
        "label": "주행 입력",
        "description": "속도·스로틀·브레이크·기어를 한 화면에 겹쳐 본다.",
        "metrics": ["speed", "throttle", "brake", "gear"],
    },
    {
        "id": "tyres_brakes",
        "label": "타이어 · 브레이크",
        "description": "네 바퀴 표면 온도와 브레이크 온도를 함께 본다.",
        "metrics": [
            "tyre_surface_temp_fl",
            "tyre_surface_temp_fr",
            "tyre_surface_temp_rl",
            "tyre_surface_temp_rr",
            "brake_temp_fl",
            "brake_temp_fr",
            "brake_temp_rl",
            "brake_temp_rr",
        ],
    },
    {
        "id": "energy_fuel",
        "label": "에너지 · 연료",
        "description": "ERS 잔량·사용량과 연료 잔량·여유 랩을 본다.",
        "metrics": ["ers_pct", "fuel_kg", "fuel_remaining_laps"],
    },
    {
        "id": "lap_pace",
        "label": "랩 · 간격",
        "description": "앞차·선두 간격과 순위 흐름을 본다.",
        "metrics": ["gap_front_s", "gap_leader_s", "car_position"],
    },
)


def get_metric_spec(key: str) -> TelemetryMetricSpec | None:
    """메트릭 키 하나의 정의. 모르는 키면 `None`."""
    return _METRIC_SPECS.get(key)


def catalog_payload() -> list[dict[str, Any]]:
    """카테고리 → 항목 정의를 화면이 바로 쓰는 JSON 모양으로 낸다."""
    return [
        {
            "id": category.id,
            "label": category.label,
            "metrics": [_metric_payload(metric) for metric in category.metrics],
        }
        for category in TELEMETRY_CATALOG
    ]


def presets_payload() -> list[dict[str, Any]]:
    """프리셋 정의를 복사해 낸다(호출자가 고쳐도 원본이 바뀌지 않게)."""
    return [
        {
            "id": preset["id"],
            "label": preset["label"],
            "description": preset["description"],
            "metrics": list(preset["metrics"]),
        }
        for preset in TELEMETRY_PRESETS
    ]


def _metric_payload(metric: TelemetryMetricSpec) -> dict[str, Any]:
    return {
        "key": metric.key,
        "label": metric.label,
        "category_id": metric.category_id,
        "unit": metric.unit,
        "unit_group": metric.unit_group,
        "source_packet": metric.source_packet,
        "value_range": None if metric.value_range is None else list(metric.value_range),
        "step": metric.step,
        "restricted_when_private": metric.restricted_when_private,
        "wheel_group": metric.wheel_group,
        "wheel_label": metric.wheel_label,
    }


# ---------------------------------------------------------------------------
# 시계열 채널·차 한 대 시리즈
# ---------------------------------------------------------------------------


@dataclass
class MetricChannel:
    """메트릭 하나의 시간(ms)·값 배열. 값이 없으면 `NaN`을 넣는다."""

    times_ms: array = field(default_factory=lambda: array("i"))
    values: array = field(default_factory=lambda: array("f"))

    def append(self, t_ms: int, value: Any) -> None:
        """샘플 하나를 쌓는다. 유한하지 않은 값은 `NaN`으로 둔다."""
        self.times_ms.append(int(t_ms))
        number = _float_or_nan(value)
        self.values.append(number)

    def __len__(self) -> int:
        return len(self.times_ms)


@dataclass
class CarSeries:
    """차 한 대의 메트릭 채널 모음. 내 차와 비교 차량이 같은 구조를 쓴다."""

    car_index: int
    channels: dict[str, MetricChannel] = field(default_factory=dict)

    def append(self, key: str, t_ms: int, value: Any) -> None:
        channel = self.channels.get(key)
        if channel is None:
            channel = MetricChannel()
            self.channels[key] = channel
        channel.append(t_ms, value)

    def channel(self, key: str) -> MetricChannel | None:
        return self.channels.get(key)


# ---------------------------------------------------------------------------
# min/max 보존 다운샘플러
# ---------------------------------------------------------------------------


def downsample_channel(
    channel: MetricChannel,
    spec: TelemetryMetricSpec,
    from_ms: int,
    to_ms: int,
    max_points: int,
) -> tuple[list[list[float]], bool]:
    """`[from_ms, to_ms]`의 채널을 `max_points` 이하로 줄인다.

    유효(NaN 아님) 점이 `max_points` 이하면 원본 그대로(`downsampled=False`)다.
    넘으면 `max_points // 2`개 버킷으로 나눠 각 버킷의 **최솟값과 최댓값**을
    시간 오름차순으로 남긴다. 그래서 35분 전체를 봐도 급브레이킹 한 샘플이
    깎이지 않는다(설계 5.1.D, 리뷰 포커스 2).

    `spec.step`이 참인 이산 지표(기어·DRS·순위·랩·ERS 모드)는 버킷마다
    마지막 유효값 하나만 남겨 계단 모양의 대표 변화점을 유지한다.
    """
    limit = max(1, int(max_points))
    times = channel.times_ms
    left = bisect.bisect_left(times, int(from_ms))
    right = bisect.bisect_right(times, int(to_ms))
    valid: list[tuple[int, float]] = []
    for index in range(left, right):
        value = float(channel.values[index])
        if math.isfinite(value):
            valid.append((index, value))
    if not valid:
        return [], False
    if len(valid) <= limit:
        return [[int(times[index]), value] for index, value in valid], False
    if limit == 1:
        # 점 하나만 허용하면 버킷 대표로 최댓값(스파이크)을 남긴다.
        index, value = max(valid, key=lambda item: item[1])
        return [[int(times[index]), value]], True

    bucket_count = max(1, limit // 2)
    total = len(valid)
    points: list[list[float]] = []
    for bucket in range(bucket_count):
        start = bucket * total // bucket_count
        end = (bucket + 1) * total // bucket_count
        if start >= end:
            continue
        if spec.step:
            index, value = valid[end - 1]
            points.append([int(times[index]), value])
            continue
        low = high = start
        for position in range(start + 1, end):
            value = valid[position][1]
            if value < valid[low][1]:
                low = position
            if value > valid[high][1]:
                high = position
        first, second = (low, high) if low <= high else (high, low)
        index, value = valid[first]
        points.append([int(times[index]), value])
        if second != first:
            index, value = valid[second]
            points.append([int(times[index]), value])
    return points, True


# ---------------------------------------------------------------------------
# 1회 스캔 인덱서
# ---------------------------------------------------------------------------


class TelemetrySessionIndex:
    """녹화 1건의 시계열·지도 프레임·마커 인덱스(설계 5.1).

    `build_telemetry_index()`가 채운다. 화면 API는 `summary_payload()`·
    `query_series()`·`map_frame_at()`·`map_frames_range()`만 쓴다.
    """

    def __init__(
        self,
        *,
        recording_id: int,
        file_path: Path,
        recording: dict[str, Any] | None = None,
    ) -> None:
        self.recording_id = int(recording_id)
        self.file_path = Path(file_path)
        self.recording: dict[str, Any] = dict(recording or {})
        self.duration_ms = 0
        self.track_id: int | None = None
        self.player_index: int | None = None
        self.participants: list[dict[str, Any]] = []
        self.track: dict[str, Any] = {}
        self.markers: dict[str, Any] = {
            "radio": [],
            "events": [],
            "intervals": [],
            "laps": [],
        }
        # 내 차 원시 시계열. `extract_car_series`로 채운 비교 차량은 아래 캐시에 둔다.
        self.series: CarSeries | None = None
        self.compare_cache: tuple[int, CarSeries] | None = None
        self._compare_lock = asyncio.Lock()
        # 차량별 경량 마커(피트·페널티·추월). 비교 차량 토글에 쓴다.
        self._car_events: dict[int, list[dict[str, Any]]] = {}
        self._car_intervals: dict[int, list[dict[str, Any]]] = {}
        # 10Hz 지도 프레임은 dict 대신 압축 배열로 보관한다(수십 MB 상한).
        self._frame_times: array = array("i")
        self._frame_cars: list[array] = []
        self._frame_flags_list: list[dict[str, Any]] = []
        self._frame_flags_dedup: list[dict[str, Any]] = []
        self._frame_lap: array = array("h")
        self._frame_position: array = array("b")

    # --- 요약 -------------------------------------------------------------

    def summary_payload(self) -> dict[str, Any]:
        """`GET /api/replay/telemetry/summary`의 본문(상태 필드는 API가 붙인다)."""
        return {
            "recording": {
                "id": self.recording_id,
                "session_uid": _as_int(self.recording.get("session_uid")),
                "category": self.recording.get("category"),
                "session_type": _as_int(self.recording.get("session_type")),
                "track_id": self.track_id,
                "duration_ms": self.duration_ms,
                "started_at_ns": _as_int(self.recording.get("started_at_ns")),
            },
            "duration_ms": self.duration_ms,
            "player_index": self.player_index,
            "catalog": catalog_payload(),
            "presets": presets_payload(),
            "participants": [dict(entry) for entry in self.participants],
            "track": self.track,
            "markers": self.markers,
        }

    # --- 시계열 -----------------------------------------------------------

    def query_series(
        self,
        metric_keys: list[str],
        from_ms: int,
        to_ms: int,
        max_points: int = DEFAULT_MAX_POINTS,
        compare_car: int | None = None,
    ) -> dict[str, Any]:
        """선택 메트릭들의 구간 다운샘플 시계열을 만든다.

        모르는 메트릭 키는 `ValueError`다(API가 400으로 바꾼다). `from_ms`가
        음수이거나 길이를 넘으면 `[0, duration_ms]`로 클램핑하고, 뒤집힌
        구간은 두 값을 바꿔 받아들인다. `compare_car`가 내 차이거나 범위를
        벗어나면 비교 없음으로 정규화한다.
        """
        specs: list[TelemetryMetricSpec] = []
        for key in metric_keys:
            spec = get_metric_spec(key)
            if spec is None:
                raise ValueError(f"알 수 없는 메트릭입니다: {key}")
            specs.append(spec)

        start = _clamp_ms(from_ms, self.duration_ms)
        end = _clamp_ms(to_ms, self.duration_ms)
        if start > end:
            start, end = end, start
        limit = max(1, min(int(max_points), MAX_POINTS_LIMIT))

        compare = _normalize_compare(compare_car, self.player_index)
        compare_series: CarSeries | None = None
        compare_telemetry: int | None = None
        if compare is not None:
            compare_telemetry = self.participant_telemetry(compare)
            cached = self.compare_cache
            if cached is not None and cached[0] == compare:
                compare_series = cached[1]

        series: dict[str, Any] = {}
        for spec in specs:
            points: list[list[float]] = []
            downsampled = False
            channel = None if self.series is None else self.series.channel(spec.key)
            if channel is not None:
                points, downsampled = downsample_channel(
                    channel, spec, start, end, limit
                )
            restricted = bool(
                compare is not None
                and compare_telemetry == 0
                and spec.restricted_when_private
            )
            compare_points: list[list[float]] | None = None
            compare_downsampled = False
            if compare is not None and not restricted and compare_series is not None:
                compare_channel = compare_series.channel(spec.key)
                if compare_channel is not None:
                    compare_points, compare_downsampled = downsample_channel(
                        compare_channel, spec, start, end, limit
                    )
                else:
                    compare_points = []
            series[spec.key] = {
                **_metric_payload(spec),
                "series": points,
                "downsampled": downsampled,
                "compare_series": compare_points,
                "compare_downsampled": compare_downsampled,
                "compare_restricted": restricted,
                "compare_available": compare_series is not None,
            }

        return {
            "recording_id": self.recording_id,
            "from_ms": start,
            "to_ms": end,
            "max_points": limit,
            "compare_car": compare,
            "compare_your_telemetry": compare_telemetry,
            "series": series,
        }

    def participant_telemetry(self, car_index: int) -> int | None:
        """참가자의 `your_telemetry`(1=공개, 0=비공개). 모르면 `None`."""
        for entry in self.participants:
            if entry.get("car_index") == car_index:
                return _as_int(entry.get("your_telemetry"))
        return None

    def markers_for_car(self, car_index: int) -> dict[str, Any]:
        """차량 한 대의 경량 마커(피트·페널티·추월). 비교 차량 토글용."""
        return {
            "car_index": int(car_index),
            "events": [dict(event) for event in self._car_events.get(car_index, [])],
            "intervals": [
                dict(interval) for interval in self._car_intervals.get(car_index, [])
            ],
        }

    # --- 지도 프레임 ------------------------------------------------------

    @property
    def map_frames(self) -> list[dict[str, Any]]:
        """모든 10Hz 지도 프레임을 디코딩해 낸다(전체 복사는 무겁다)."""
        return [self._decode_frame(index) for index in range(len(self._frame_times))]

    def map_frame_at(self, t_ms: int) -> dict[str, Any]:
        """`t_ms` 이전(같으면 그 시각)의 마지막 지도 프레임 하나."""
        target = _clamp_ms(t_ms, self.duration_ms)
        if not self._frame_times:
            return {
                "t_ms": target,
                "cars": [],
                "flag": "none",
                "flags": _empty_flags(),
                "lap": None,
                "position": None,
            }
        index = bisect.bisect_right(self._frame_times, target) - 1
        if index < 0:
            index = 0
        return self._decode_frame(index)

    def map_frames_range(self, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
        """`[from_ms, to_ms]` 구간의 지도 프레임들(브라우저 청크 프리페치용)."""
        start = _clamp_ms(from_ms, self.duration_ms)
        end = _clamp_ms(to_ms, self.duration_ms)
        if start > end:
            start, end = end, start
        left = bisect.bisect_left(self._frame_times, start)
        right = bisect.bisect_right(self._frame_times, end)
        return [self._decode_frame(index) for index in range(left, right)]

    def _decode_frame(self, index: int) -> dict[str, Any]:
        row = self._frame_cars[index]
        cars: list[dict[str, Any]] = []
        for offset in range(0, len(row), FRAME_CAR_WIDTH):
            values = row[offset : offset + FRAME_CAR_WIDTH]
            car: dict[str, Any] = {}
            for field_name, value in zip(FRAME_CAR_FIELDS, values, strict=True):
                if field_name in ("dist_m", "x", "z"):
                    car[field_name] = _finite_or_none(value)
                else:
                    car[field_name] = _int_or_none(value)
            cars.append(car)
        lap = int(self._frame_lap[index])
        position = int(self._frame_position[index])
        return {
            "t_ms": int(self._frame_times[index]),
            "cars": cars,
            "flag": self._frame_flags_list[index],
            "flags": self._frame_flags_dedup[index],
            "lap": None if lap < 0 else lap,
            "position": None if position < 0 else position,
        }


def build_telemetry_index(
    file_path: Path,
    recording_row: dict[str, Any],
    conn_factory: Callable[[], sqlite3.Connection] | None = None,
    *,
    progress_cb: Callable[[float], None] | None = None,
) -> TelemetrySessionIndex:
    """녹화 파일을 1회 순차 스캔해 `TelemetrySessionIndex`를 만든다.

    `conn_factory`가 있으면 지도 윤곽을 DB에서도 찾는다(없으면 패키지 데이터
    파일만 쓴다). 이 함수는 무거우므로 호출자가 `asyncio.to_thread`로 이벤트
    루프 밖에서 실행해야 한다. 깨진 패킷은 세지 않고 넘어가며, 어느 패킷
    처리에서 예외가 나도 스캔을 멈추지 않는다.
    """
    path = Path(file_path)
    row = dict(recording_row or {})
    recording_id = _as_int(row.get("id"))
    if recording_id is None:
        recording_id = _as_int(row.get("recording_id")) or 0
    index = TelemetrySessionIndex(
        recording_id=recording_id, file_path=path, recording=row
    )

    scanner = _IndexScanner(index=index, row=row, conn_factory=conn_factory)
    scanner.run(path, progress_cb=progress_cb)
    return index


def extract_car_series(file_path: Path, car_index: int) -> CarSeries:
    """비교 차량 한 대의 시계열만 녹화 파일에서 1회 스캔해 뽑는다.

    호출자가 `asyncio.to_thread`로 실행한다. 시각 기준(`first_recv_ns`)은
    `build_telemetry_index`와 같다(첫 레코드의 수신 시각).
    """
    series = CarSeries(car_index=int(car_index))
    first_recv_ns: int | None = None
    for recv_ns, data in iter_recording(file_path):
        if first_recv_ns is None:
            first_recv_ns = int(recv_ns)
        t_ms = (int(recv_ns) - first_recv_ns) // 1_000_000
        try:
            packet = parse_packet(data)
        except UnsupportedPacket:
            continue
        except Exception:  # 파서 버그 하나로 비교 시리즈 전체를 잃지 않는다.
            continue
        _consume_packet_for_series(series, packet, t_ms)
    return series


class _VirtualClock:
    """녹화 패킷의 `recv_ns`를 돌려주는 가상 시계(무전 TTL·경고 판정용)."""

    def __init__(self) -> None:
        self.now_ns = 0

    def __call__(self) -> int:
        return self.now_ns


class _IndexScanner:
    """`build_telemetry_index`의 스캔 상태 묶음(파일을 한 번만 읽는다)."""

    def __init__(
        self,
        *,
        index: TelemetrySessionIndex,
        row: dict[str, Any],
        conn_factory: Callable[[], sqlite3.Connection] | None,
    ) -> None:
        self.index = index
        self.row = row
        self.clock = _VirtualClock()
        self.store = StateStore()
        self.radio_log = MemoryRadioLog(
            session_uid=_as_int(row.get("session_uid"))
        )
        self.queue = RadioQueue(clock=self.clock)
        self.sink = MemoryRadioSink(
            self.queue, self.radio_log, clock_ns=self.clock
        )
        config = AppConfig()
        self.rules = AlertRules(
            clock=self.clock,
            safety_ttl_ns=int(config.radio.safety_ttl_s * 1_000_000_000),
            box_ttl_ns=int(config.radio.box_ttl_s * 1_000_000_000),
        )
        self.pipeline = RacePipeline(
            self.store, self.sink, None, config=config, clock=self.clock
        )
        self.track_map = TrackMap(_open_conn(conn_factory))

        self.player_index: int | None = None
        self.player_series: CarSeries | None = None
        self.session_uid: int | None = _as_int(row.get("session_uid"))
        self.first_recv_ns: int | None = None
        self.last_recv_ns: int = 0
        self.latest_packets: dict[str, dict[str, Any]] = {}
        self.last_alert_ns: int | None = None
        self.last_flags_key: tuple[Any, ...] | None = None
        self.last_flags: dict[str, Any] = _empty_flags()
        self.current_bucket: int | None = None
        # 적기 래치(StateStore 내부 값과 같은 규칙으로 우리 몫을 따로 든다).
        self.red = False
        self.red_lap: int | None = None
        self.last_safety_status: int | None = None
        # 마커 수집.
        self.common_events: list[dict[str, Any]] = []
        self.common_intervals: list[dict[str, Any]] = []
        self.open_intervals: dict[str, dict[str, Any]] = {}
        self.pit_open: dict[int, int] = {}
        self.pit_intervals: dict[int, list[dict[str, Any]]] = {}
        self.car_events: dict[int, list[dict[str, Any]]] = {}
        self.lap_boundaries: list[dict[str, Any]] = []
        self.expected_duration_ms = _as_int(row.get("duration_ms")) or 0
        self.last_progress_t = -1

    # --- 스캔 -------------------------------------------------------------

    def run(
        self, path: Path, *, progress_cb: Callable[[float], None] | None = None
    ) -> None:
        """파일을 끝까지 읽어 인덱스 필드를 채운다."""
        for recv_ns, data in iter_recording(path):
            self._on_record(int(recv_ns), data)
            self._report_progress(progress_cb)
        self._finish(progress_cb)

    def _on_record(self, recv_ns: int, data: bytes) -> None:
        if self.first_recv_ns is None:
            self.first_recv_ns = recv_ns
        t_ms = (recv_ns - self.first_recv_ns) // 1_000_000
        self.last_recv_ns = recv_ns
        try:
            packet = parse_packet(data)
        except UnsupportedPacket:
            return
        except Exception:  # 파서 버그 하나로 인덱스 전체를 잃지 않는다.
            return

        self._note_player(packet)
        header_uid = int(packet.header.session_uid)
        if self.session_uid != header_uid:
            self.session_uid = header_uid
            self.radio_log.set_session_uid(header_uid)
        # 이 패킷이 새 100ms 버킷이면, 직전 버킷의 마지막 상태를 프레임으로 남긴다.
        bucket = t_ms // MAP_FRAME_BUCKET_MS
        if self.current_bucket is None:
            self.current_bucket = bucket
        elif bucket != self.current_bucket:
            self._finalize_frame(self.current_bucket)
            self.current_bucket = bucket

        self.clock.now_ns = recv_ns
        try:
            events = self.store.apply(packet, recv_ns, length=len(data))
        except Exception:
            events = []
        try:
            self.pipeline.on_packet(packet, recv_ns)
        except Exception:
            logger.exception("텔레메트리 인덱스 파이프라인 처리 중 오류")
        self.store.events.clear()

        name = _packet_name(packet)
        if name is not None:
            self.latest_packets[name] = packet.body
        self._update_red(events, name, packet.body)
        self._collect_markers(events, t_ms)
        if self.player_series is not None:
            _consume_packet_for_series(self.player_series, packet, t_ms)
        self._evaluate_radio(recv_ns, events)

    def _note_player(self, packet: Any) -> None:
        """첫 유효 헤더에서 내 차 인덱스를 정하고 시리즈를 만든다."""
        if self.player_series is not None:
            return
        player = int(packet.header.player_car_index)
        if 0 <= player < 22:
            self.player_index = player
            self.player_series = CarSeries(car_index=player)

    def _evaluate_radio(self, recv_ns: int, events: Sequence[Event]) -> None:
        """실서버 무전 서비스처럼 10Hz 가상 시계로 위험 경고를 판정한다."""
        if (
            self.last_alert_ns is not None
            and recv_ns - self.last_alert_ns < ALERT_EVAL_INTERVAL_NS
        ):
            return
        self.last_alert_ns = recv_ns
        try:
            for message in self.rules.evaluate(
                self._light_snapshot(), tuple(events)
            ):
                self.queue.push(message)
        except Exception:  # 판정 버그가 스캔을 멈추지 않게 한다.
            logger.exception("텔레메트리 인덱스 경고 판정 중 오류")
        try:
            self.sink.drain()
        except Exception:
            logger.exception("텔레메트리 인덱스 무전 기록 중 오류")

    # --- 상태 스냅샷·깃발 -------------------------------------------------

    def _light_snapshot(self) -> dict[str, Any]:
        """패킷 본문 참조만 담은 가벼운 스냅샷(깊은 복사 없음).

        `StateStore.snapshot()`은 모든 패킷 본문을 JSON 모양으로 복사하므로
        10Hz 판정마다 부르면 35분 스캔에서 수 GB를 할당한다. 인덱스에는 참조만
        있으면 충분하다(파서가 패킷마다 새 딕셔너리를 만든다).
        """
        return {
            "session_uid": self.session_uid,
            "player_car_index": self.player_index,
            "packets": self.latest_packets,
            "flags": self.last_flags,
        }

    def _refresh_flags(self) -> dict[str, Any]:
        """지금 상태의 깃발 요약. 바뀌지 않았으면 직전 딕셔너리를 재사용한다."""
        flags = flag_summary(
            self.latest_packets, self.player_index, red=self.red
        )
        key = (
            flags["red"],
            flags["blue"],
            flags["yellow"],
            flags["sc"],
            flags["vsc"],
            tuple(
                (zone["start"], zone["end"]) for zone in flags["yellow_zones"]
            ),
        )
        if key == self.last_flags_key:
            return self.last_flags
        self.last_flags_key = key
        self.last_flags = flags
        return flags

    def _update_red(
        self, events: Sequence[Event], name: str | None, body: dict[str, Any]
    ) -> None:
        """`StateStore`의 적기 래치를 마커용으로 똑같이 따라간다."""
        for event in events:
            if event.kind is EventKind.RED_FLAG:
                self.red = True
                self.red_lap = None
            elif event.kind is EventKind.SESSION_RESTART:
                self.red = False
                self.red_lap = None
        if name == "event":
            # SSTA(새 세션)는 적기 래치도 푼다(SCRUM-80).
            if str(body.get("event_string_code", "")).strip() == "SSTA":
                self.red = False
                self.red_lap = None
        elif name == "session":
            status = _as_int(body.get("safety_car_status"))
            if (
                self.red
                and status in (1, 2)
                and status != self.last_safety_status
            ):
                self.red = False
                self.red_lap = None
            self.last_safety_status = status
        elif name == "lap_data" and self.red:
            lap = self._player_lap_number(body)
            if lap is not None and lap > 0:
                if self.red_lap is None:
                    self.red_lap = lap
                elif lap > self.red_lap:
                    self.red = False
                    self.red_lap = None

    def _player_lap_number(self, body: dict[str, Any]) -> int | None:
        entry = _car_entry(body, "lap_data", self.player_index)
        if entry is None:
            return None
        return _as_int(entry.get("current_lap_num"))

    # --- 마커 -------------------------------------------------------------

    def _collect_markers(self, events: Sequence[Event], t_ms: int) -> None:
        """사건을 세션 공통·차량별 경량 맵으로 나눠 모은다.

        22대 전체 `lap_complete`처럼 화면을 덮는 스팸은 넣지 않는다. 나머지
        차량 사건은 `markers_for_car()`용 경량 맵에만 쌓는다.
        """
        for event in events:
            kind = event.kind
            car = event.car_index
            if kind is EventKind.PIT_ENTER:
                if _valid_car(car):
                    self.pit_open[int(car)] = t_ms
            elif kind is EventKind.PIT_EXIT:
                if _valid_car(car):
                    start = self.pit_open.pop(int(car), None)
                    if start is not None:
                        interval = {
                            "kind": "pit",
                            "start_ms": start,
                            "end_ms": t_ms,
                            "car_index": int(car),
                        }
                        self.pit_intervals.setdefault(int(car), []).append(
                            interval
                        )
            elif kind is EventKind.PENALTY:
                if _valid_car(car):
                    self._car_event(
                        int(car),
                        {
                            "kind": "penalty",
                            "t_ms": t_ms,
                            "car_index": int(car),
                            "detail": _event_detail(event),
                        },
                    )
            elif kind is EventKind.OVERTAKE:
                overtaking = _as_int(event.data.get("overtaking_vehicle_idx"))
                overtaken = _as_int(
                    event.data.get("being_overtaken_vehicle_idx")
                )
                if _valid_car(overtaking):
                    self._car_event(
                        int(overtaking),
                        {
                            "kind": "overtake",
                            "t_ms": t_ms,
                            "car_index": int(overtaking),
                            "other_car_index": overtaken,
                            "detail": _event_detail(event),
                        },
                    )
                if _valid_car(overtaken):
                    self._car_event(
                        int(overtaken),
                        {
                            "kind": "overtaken",
                            "t_ms": t_ms,
                            "car_index": int(overtaken),
                            "other_car_index": overtaking,
                            "detail": _event_detail(event),
                        },
                    )
            elif kind is EventKind.FLASHBACK:
                detail = dict(event.data)
                detail.pop("recv_ns", None)
                detail.pop("session_uid", None)
                self.common_events.append(
                    {
                        "kind": "flashback",
                        "t_ms": t_ms,
                        "car_index": None,
                        "detail": detail,
                    }
                )
            elif kind is EventKind.LAP_COMPLETE:
                completed = _as_int(event.lap)
                if not (_valid_car(car) and completed is not None):
                    continue
                if completed < 1:
                    continue
                lap_time_ms = _as_int(event.data.get("lap_time_ms"))
                # 랩 완료는 차량별 경량 맵에만 쌓는다. 화면 기본 마커는 내 차
                # 것만 노출되어 22대 전체 스팸이 되지 않는다.
                self._car_event(
                    int(car),
                    {
                        "kind": "lap_complete",
                        "t_ms": t_ms,
                        "car_index": int(car),
                        "lap": completed,
                        "lap_time_ms": lap_time_ms,
                    },
                )
                if (
                    self.player_index is not None
                    and car == self.player_index
                ):
                    self.lap_boundaries.append(
                        {
                            "lap": completed + 1,
                            "start_ms": t_ms,
                            "completed_lap_time_ms": lap_time_ms,
                        }
                    )

    def _car_event(self, car_index: int, payload: dict[str, Any]) -> None:
        self.car_events.setdefault(car_index, []).append(payload)

    # --- 지도 프레임 ------------------------------------------------------

    def _finalize_frame(self, bucket: int) -> None:
        """직전 100ms 버킷의 마지막 상태를 지도 프레임으로 압축해 남긴다."""
        flags = self._refresh_flags()
        snapshot = {
            "session_uid": self.session_uid,
            "player_car_index": self.player_index,
            "packets": self.latest_packets,
            "flags": flags,
        }
        try:
            cars = self.track_map.car_positions(snapshot)
        except Exception:
            logger.exception("텔레메트리 인덱스 지도 프레임 계산 중 오류")
            cars = []
        lap_entries = _entries(
            self.latest_packets.get("lap_data"), "lap_data"
        )
        row = array("f")
        for car in cars:
            car_index = _as_int(car.get("car"))
            entry = (
                lap_entries[car_index]
                if car_index is not None and car_index < len(lap_entries)
                else None
            )
            row.extend(
                (
                    float(car_index if car_index is not None else -1),
                    _finite_or_nan(car.get("dist_m")),
                    _finite_or_nan(car.get("x")),
                    _finite_or_nan(car.get("z")),
                    _finite_or_nan(car.get("team_id")),
                    _finite_or_nan(car.get("position")),
                    _finite_or_nan(car.get("lap")),
                    _finite_or_nan(_entry_value(entry, "pit_status")),
                    _finite_or_nan(_entry_value(entry, "result_status")),
                )
            )
        self.index._frame_times.append(int(bucket) * MAP_FRAME_BUCKET_MS)
        self.index._frame_cars.append(row)
        self.index._frame_flags_list.append(self.track_map.flag(snapshot))
        self.index._frame_flags_dedup.append(flags)
        player_entry = (
            lap_entries[self.player_index]
            if self.player_index is not None
            and self.player_index < len(lap_entries)
            else None
        )
        lap = _as_int(_entry_value(player_entry, "current_lap_num"))
        position = _as_int(_entry_value(player_entry, "car_position"))
        self.index._frame_lap.append(-1 if lap is None else min(lap, 32767))
        self.index._frame_position.append(
            -1 if position is None else max(-128, min(position, 127))
        )
        self._note_flag_intervals(flags, int(bucket) * MAP_FRAME_BUCKET_MS)

    def _note_flag_intervals(
        self, flags: dict[str, Any], t_ms: int
    ) -> None:
        """세션 공통 SC·VSC·적기 구간을 깃발 전이로 연다/닫는다."""
        for kind, current in (
            ("safety_car", bool(flags.get("sc"))),
            ("vsc", bool(flags.get("vsc"))),
            ("red_flag", bool(flags.get("red"))),
        ):
            band = self.open_intervals.get(kind)
            if current and band is None:
                self.open_intervals[kind] = {
                    "kind": kind,
                    "start_ms": t_ms,
                    "end_ms": None,
                }
            elif not current and band is not None:
                self.open_intervals.pop(kind, None)
                band["end_ms"] = t_ms
                self.common_intervals.append(band)

    # --- 마무리 -----------------------------------------------------------

    def _finish(self, progress_cb: Callable[[float], None] | None) -> None:
        """마지막 버킷·열린 구간·요약 묶음을 정리한다."""
        if self.current_bucket is not None:
            self._finalize_frame(self.current_bucket)
            self.current_bucket = None
        if self.first_recv_ns is None:
            self.index.duration_ms = max(0, self.expected_duration_ms)
        else:
            self.index.duration_ms = max(
                0, (self.last_recv_ns - self.first_recv_ns) // 1_000_000
            )
        end_ms = self.index.duration_ms
        for kind, band in list(self.open_intervals.items()):
            band["end_ms"] = end_ms
            self.common_intervals.append(band)
            self.open_intervals.pop(kind, None)
        for car_index, start in list(self.pit_open.items()):
            interval = {
                "kind": "pit",
                "start_ms": start,
                "end_ms": end_ms,
                "car_index": car_index,
            }
            self.pit_intervals.setdefault(car_index, []).append(interval)
        self.pit_open.clear()

        # 마지막 패킷 뒤 큐에 남은 무전도 마커에 넣는다.
        try:
            self.sink.drain()
        except Exception:
            logger.exception("텔레메트리 인덱스 무전 마무리 중 오류")

        self.index.player_index = self.player_index
        self.index.series = self.player_series
        if (
            self.index.recording.get("session_uid") is None
            and self.session_uid is not None
        ):
            self.index.recording["session_uid"] = self.session_uid
        self.index.track_id = self._track_id()
        self.index.participants = self._participants()
        self.index.track = self._track_payload()
        self.index.markers = self._markers()
        for car_index, events in self.car_events.items():
            events.sort(key=lambda item: int(item["t_ms"]))
        for car_index, intervals in self.pit_intervals.items():
            intervals.sort(key=lambda item: int(item["start_ms"]))
        self.index._car_events = self.car_events
        self.index._car_intervals = self.pit_intervals
        if progress_cb is not None:
            progress_cb(1.0)

    def _track_id(self) -> int | None:
        session = self.latest_packets.get("session")
        value = _as_int(session.get("track_id")) if session else None
        if value is not None and value >= 0:
            return value
        return _as_int(self.row.get("track_id"))

    def _participants(self) -> list[dict[str, Any]]:
        """22대 참가자 목록. 참가자 패킷이 없으면 빈 자리를 채운다."""
        body = self.latest_packets.get("participants") or {}
        raw = body.get("participants")
        entries = raw if isinstance(raw, list) else []
        result: list[dict[str, Any]] = []
        for car_index in range(22):
            entry = entries[car_index] if car_index < len(entries) else None
            entry = entry if isinstance(entry, dict) else {}
            team_id = _as_int(entry.get("team_id"))
            name = entry.get("name")
            result.append(
                {
                    "car_index": car_index,
                    "name": name if isinstance(name, str) and name else f"차량 {car_index + 1}",
                    "team_id": team_id,
                    "color": TEAM_COLORS.get(team_id, TEAM_COLORS[10]),
                    "is_player": car_index == self.player_index,
                    "your_telemetry": _as_int(entry.get("your_telemetry")),
                    "ai_controlled": _as_int(entry.get("ai_controlled")),
                }
            )
        return result

    def _track_payload(self) -> dict[str, Any]:
        snapshot = {
            "session_uid": self.session_uid,
            "player_car_index": self.player_index,
            "packets": self.latest_packets,
            "flags": self._refresh_flags(),
        }
        try:
            return self.track_map.track_payload(snapshot, None)
        except Exception:
            logger.exception("텔레메트리 인덱스 서킷 정보를 만들지 못했습니다")
            return TrackMap(None).track_payload(
                {"packets": {}, "flags": _empty_flags()}, None
            )

    def _markers(self) -> dict[str, Any]:
        """화면이 쓰는 마커 묶음(세션 공통 + 내 차 + 랩 경계 + 무전)."""
        player = self.player_index
        events: list[dict[str, Any]] = []
        if player is not None:
            events.extend(self.car_events.get(player, []))
        events.extend(self.common_events)
        events.sort(key=lambda item: int(item["t_ms"]))

        intervals: list[dict[str, Any]] = list(self.common_intervals)
        if player is not None:
            intervals.extend(self.pit_intervals.get(player, []))
        intervals.sort(key=lambda item: int(item["start_ms"]))

        laps: list[dict[str, Any]] = []
        if self.first_recv_ns is not None:
            laps.append({"lap": 1, "start_ms": 0})
            laps.extend(self.lap_boundaries)
            laps.sort(key=lambda item: int(item["start_ms"]))

        return {
            "radio": self._radio_markers(),
            "events": events,
            "intervals": intervals,
            "laps": laps,
        }

    def _radio_markers(self) -> list[dict[str, Any]]:
        """`MemoryRadioLog`의 모든 발화를 시간순 마커로 바꾼다."""
        if self.first_recv_ns is None:
            return []
        rows = self.radio_log.list_messages(limit=None)
        markers: list[dict[str, Any]] = []
        for row in reversed(rows):  # `list_messages`는 최신순이라 뒤집는다.
            recorded_at = _as_int(row.get("recorded_at_ns"))
            if recorded_at is None:
                continue
            markers.append(
                {
                    "t_ms": max(
                        0, (recorded_at - self.first_recv_ns) // 1_000_000
                    ),
                    "kind": row.get("kind"),
                    "text": row.get("text"),
                    "corner": _as_int(row.get("corner")),
                    "lap": _as_int(row.get("lap")),
                    "priority": _as_int(row.get("priority")),
                    "played": bool(row.get("played")),
                }
            )
        return markers

    def _report_progress(
        self, progress_cb: Callable[[float], None] | None
    ) -> None:
        """DB 녹화 길이를 알 때 1초 가상 시간마다 진행률을 알린다."""
        if progress_cb is None or self.first_recv_ns is None:
            return
        if self.expected_duration_ms <= 0:
            return
        t_ms = (self.last_recv_ns - self.first_recv_ns) // 1_000_000
        if t_ms < self.last_progress_t + 1000:
            return
        self.last_progress_t = t_ms
        progress_cb(min(0.99, max(0.0, t_ms / self.expected_duration_ms)))


# ---------------------------------------------------------------------------
# 패킷 → 채널
# ---------------------------------------------------------------------------


def _consume_packet_for_series(
    series: CarSeries, packet: Any, t_ms: int
) -> None:
    """패킷 하나에서 차 한 대의 메트릭 값을 뽑아 시리즈에 쌓는다."""
    packet_id = packet.header.packet_id
    body = packet.body
    if packet_id == PacketId.CAR_TELEMETRY:
        entry = _car_entry(body, "car_telemetry_data", series.car_index)
        if entry is None:
            return
        series.append("speed", t_ms, entry.get("speed"))
        series.append("throttle", t_ms, entry.get("throttle"))
        series.append("brake", t_ms, entry.get("brake"))
        series.append("steer", t_ms, entry.get("steer"))
        series.append("gear", t_ms, entry.get("gear"))
        series.append("engine_rpm", t_ms, entry.get("engine_rpm"))
        series.append("drs", t_ms, entry.get("drs"))
        series.append("clutch", t_ms, entry.get("clutch"))
        series.append("engine_temp", t_ms, entry.get("engine_temperature"))
        surface = entry.get("tyres_surface_temperature")
        inner = entry.get("tyres_inner_temperature")
        pressure = entry.get("tyres_pressure")
        brakes = entry.get("brakes_temperature")
        for suffix, wheel_index, _label in WHEEL_INDEX_BY_SUFFIX:
            series.append(
                f"tyre_surface_temp_{suffix}",
                t_ms,
                _sequence_value(surface, wheel_index),
            )
            series.append(
                f"tyre_inner_temp_{suffix}",
                t_ms,
                _sequence_value(inner, wheel_index),
            )
            series.append(
                f"tyre_pressure_{suffix}",
                t_ms,
                _sequence_value(pressure, wheel_index),
            )
            series.append(
                f"brake_temp_{suffix}",
                t_ms,
                _sequence_value(brakes, wheel_index),
            )
    elif packet_id == PacketId.MOTION:
        entry = _car_entry(body, "car_motion_data", series.car_index)
        if entry is None:
            return
        series.append("g_lat", t_ms, entry.get("g_force_lateral"))
        series.append("g_lon", t_ms, entry.get("g_force_longitudinal"))
        series.append("g_vert", t_ms, entry.get("g_force_vertical"))
    elif packet_id == PacketId.LAP_DATA:
        entry = _car_entry(body, "lap_data", series.car_index)
        if entry is None:
            return
        series.append("current_lap", t_ms, entry.get("current_lap_num"))
        series.append(
            "current_lap_time_s",
            t_ms,
            _scale(entry.get("current_lap_time_in_ms"), 0.001),
        )
        series.append(
            "gap_front_s", t_ms, _gap_seconds(entry, "delta_to_car_in_front")
        )
        series.append(
            "gap_leader_s", t_ms, _gap_seconds(entry, "delta_to_race_leader")
        )
        series.append("car_position", t_ms, entry.get("car_position"))
        series.append(
            "safety_car_delta", t_ms, entry.get("safety_car_delta")
        )
    elif packet_id == PacketId.CAR_STATUS:
        entry = _car_entry(body, "car_status_data", series.car_index)
        if entry is None:
            return
        energy = _float_or_none(entry.get("ers_store_energy"))
        series.append(
            "ers_pct",
            t_ms,
            None if energy is None else _clamp(energy / ERS_STORE_MAX_J * 100.0, 0.0, 100.0),
        )
        deployed = _float_or_none(entry.get("ers_deployed_this_lap"))
        series.append(
            "ers_deployed_pct",
            t_ms,
            None
            if deployed is None
            else _clamp(deployed / ERS_STORE_MAX_J * 100.0, 0.0, 100.0),
        )
        series.append(
            "ers_mguk_j",
            t_ms,
            _scale(entry.get("ers_harvested_this_lap_mguk"), 1e-6),
        )
        series.append(
            "ers_mguh_j",
            t_ms,
            _scale(entry.get("ers_harvested_this_lap_mguh"), 1e-6),
        )
        series.append("ers_mode", t_ms, entry.get("ers_deploy_mode"))
        series.append("fuel_kg", t_ms, entry.get("fuel_in_tank"))
        series.append(
            "fuel_remaining_laps", t_ms, entry.get("fuel_remaining_laps")
        )
        series.append("brake_bias", t_ms, entry.get("front_brake_bias"))
    elif packet_id == PacketId.CAR_DAMAGE:
        entry = _car_entry(body, "car_damage_data", series.car_index)
        if entry is None:
            return
        wear = entry.get("tyres_wear")
        for suffix, wheel_index, _label in WHEEL_INDEX_BY_SUFFIX:
            series.append(
                f"tyre_wear_{suffix}",
                t_ms,
                _sequence_value(wear, wheel_index),
            )
        series.append("wing_fl_damage", t_ms, entry.get("front_left_wing_damage"))
        series.append(
            "wing_fr_damage", t_ms, entry.get("front_right_wing_damage")
        )
        series.append("rear_wing_damage", t_ms, entry.get("rear_wing_damage"))
        series.append("floor_damage", t_ms, entry.get("floor_damage"))
        series.append("diffuser_damage", t_ms, entry.get("diffuser_damage"))
        series.append("sidepod_damage", t_ms, entry.get("sidepod_damage"))
        series.append("gearbox_damage", t_ms, entry.get("gearbox_damage"))
        series.append("engine_damage", t_ms, entry.get("engine_damage"))


# ---------------------------------------------------------------------------
# LRU=1 인덱스 스토어
# ---------------------------------------------------------------------------


class TelemetryIndexStore:
    """열린 녹화 1건의 인덱스만 캐시하고 백그라운드로 빌드하는 스토어.

    `open_recording()`은 곧바로 `"building"`을 돌려주고 `asyncio.to_thread`로
    인덱싱을 띄운다(이벤트 루프 비차단). 같은 녹화의 동시 요청은 하나의
    태스크로 합쳐지고, 새 녹화가 준비되면 직전 캐시는 버려 LRU=1을 지킨다.
    `protected_recording_ids()`는 `BackupWorker`가 로컬 파일을 정리할 때
    지우면 안 되는 ID(캐시·빌드 중)를 알린다.
    """

    def __init__(
        self,
        *,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self._conn_factory = conn_factory
        self._index: TelemetrySessionIndex | None = None
        self._cached_id: int | None = None
        self._desired_id: int | None = None
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._progress: dict[int, float] = {}
        self._failures: dict[int, str] = {}
        self._state_lock = threading.Lock()
        self._open_lock = asyncio.Lock()

    # --- 열기 -------------------------------------------------------------

    async def open_recording(
        self,
        recording_id: int,
        row: dict[str, Any],
        file_path: Path,
    ) -> str:
        """인덱스 빌드를 시작하고 `"building"`(이미 준비됐으면 `"ready"`)을 낸다."""
        rid = int(recording_id)
        async with self._open_lock:
            with self._state_lock:
                if self._cached_id == rid and self._index is not None:
                    return "ready"
                self._desired_id = rid
                self._failures.pop(rid, None)
                task = self._tasks.get(rid)
                if task is not None and not task.done():
                    return "building"
                self._progress[rid] = 0.0
                self._tasks[rid] = asyncio.create_task(
                    self._build(rid, dict(row), Path(file_path)),
                    name=f"f1re-telemetry-index-{rid}",
                )
                return "building"

    async def _build(
        self, rid: int, row: dict[str, Any], path: Path
    ) -> None:
        """스레드에서 인덱스를 빌드하고, 최신 요청일 때만 캐시를 바꾼다."""

        def progress(value: float) -> None:
            with self._state_lock:
                self._progress[rid] = max(0.0, min(1.0, float(value)))

        try:
            index = await asyncio.to_thread(
                build_telemetry_index,
                path,
                row,
                self._conn_factory,
                progress_cb=progress,
            )
        except Exception as exc:
            logger.exception("텔레메트리 인덱스 빌드에 실패했습니다")
            with self._state_lock:
                self._failures[rid] = f"{type(exc).__name__}: {exc}"
                self._progress.pop(rid, None)
                self._tasks.pop(rid, None)
            return
        with self._state_lock:
            if self._desired_id == rid:
                self._index = index
                self._cached_id = rid
                self._progress[rid] = 1.0
            self._tasks.pop(rid, None)

    # --- 조회 -------------------------------------------------------------

    def status_or_summary(self, recording_id: int) -> dict[str, Any]:
        """`building`(진행률 포함)·`ready`(요약)·`failed`·`idle` 중 하나."""
        rid = int(recording_id)
        with self._state_lock:
            if self._cached_id == rid and self._index is not None:
                payload: dict[str, Any] = {
                    "status": "ready",
                    "recording_id": rid,
                }
                payload.update(self._index.summary_payload())
                return payload
            task = self._tasks.get(rid)
            if task is not None and not task.done():
                return {
                    "status": "building",
                    "recording_id": rid,
                    "progress": self._progress.get(rid, 0.0),
                }
            failure = self._failures.get(rid)
            if failure is not None:
                return {
                    "status": "failed",
                    "recording_id": rid,
                    "message": failure,
                }
        return {"status": "idle", "recording_id": rid}

    def get_ready(self, recording_id: int) -> TelemetrySessionIndex | None:
        """캐시된 인덱스. 아직 준비되지 않았거나 다른 녹화면 `None`."""
        rid = int(recording_id)
        with self._state_lock:
            if self._cached_id == rid:
                return self._index
        return None

    def protected_recording_ids(self) -> set[int]:
        """지금 빌드 중이거나 캐시된 `recording_id`(로컬 정리 보호 목록)."""
        with self._state_lock:
            ids = {
                rid for rid, task in self._tasks.items() if not task.done()
            }
            if self._cached_id is not None:
                ids.add(self._cached_id)
        return ids

    async def wait_ready(
        self, recording_id: int, timeout_s: float = 60.0
    ) -> TelemetrySessionIndex:
        """빌드가 끝날 때까지 기다린다(테스트·스모크 확인용)."""
        rid = int(recording_id)
        with self._state_lock:
            task = self._tasks.get(rid)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
            except TimeoutError:
                raise TimeoutError(
                    f"텔레메트리 인덱스 빌드가 {timeout_s}초 안에 끝나지 않았습니다"
                ) from None
        index = self.get_ready(rid)
        if index is None:
            with self._state_lock:
                failure = self._failures.get(rid, "인덱스가 준비되지 않았습니다")
            raise RuntimeError(failure)
        return index

    # --- 비교 차량 --------------------------------------------------------

    async def ensure_compare_car(
        self, index: TelemetrySessionIndex, car_index: int
    ) -> None:
        """비교 차량 시리즈를 온디맨드로 뽑아 인덱스에 1대만 캐시한다.

        내 차이거나 0~21 범위를 벗어난 값은 조용히 무시한다(잘못된 요청이
        스캔을 일으키지 않게).
        """
        target = int(car_index)
        if not 0 <= target < 22:
            return
        if index.player_index is not None and target == index.player_index:
            return
        async with index._compare_lock:
            cached = index.compare_cache
            if cached is not None and cached[0] == target:
                return
            try:
                series = await asyncio.to_thread(
                    extract_car_series, index.file_path, target
                )
            except Exception:
                logger.exception("비교 차량 시리즈를 추출하지 못했습니다")
                return
            index.compare_cache = (target, series)

    # --- 정리 -------------------------------------------------------------

    async def aclose(self) -> None:
        """진행 중인 빌드 태스크를 정리한다(서버 종료 경로)."""
        with self._state_lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("텔레메트리 인덱스 태스크를 정리하지 못했습니다")


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _open_conn(
    conn_factory: Callable[[], sqlite3.Connection] | None,
) -> sqlite3.Connection | None:
    if conn_factory is None:
        return None
    try:
        return conn_factory()
    except Exception:  # DB가 없거나 잠겨 있어도 패키지 데이터로 지도를 만든다.
        logger.warning("텔레메트리 지도용 DB를 열지 못했습니다", exc_info=True)
        return None


def _packet_name(packet: Any) -> str | None:
    try:
        return PacketId(packet.header.packet_id).name.lower()
    except ValueError:
        return None


def _car_entry(
    body: dict[str, Any], list_key: str, car_index: int | None
) -> dict[str, Any] | None:
    """본문의 차 22대 배열에서 한 차의 항목. 범위를 벗어나면 `None`."""
    if car_index is None or not 0 <= int(car_index) < 22:
        return None
    entries = body.get(list_key)
    if not isinstance(entries, list) or int(car_index) >= len(entries):
        return None
    entry = entries[int(car_index)]
    return entry if isinstance(entry, dict) else None


def _entries(body: Any, list_key: str) -> list[Any]:
    if not isinstance(body, dict):
        return []
    entries = body.get(list_key)
    return entries if isinstance(entries, list) else []


def _entry_value(entry: Any, key: str) -> Any:
    if not isinstance(entry, dict):
        return None
    return entry.get(key)


def _sequence_value(values: Any, index: int) -> Any:
    if not isinstance(values, (list, tuple)) or index >= len(values):
        return None
    return values[index]


def _gap_seconds(entry: dict[str, Any], prefix: str) -> float | None:
    """`m_deltaToCarInFrontMSPart`·`...MinutesPart`를 초로 합친다."""
    minutes = _as_int(entry.get(f"{prefix}_minutes_part"))
    millis = _as_int(entry.get(f"{prefix}_ms_part"))
    if minutes is None or millis is None:
        return None
    return _clamp(minutes * 60.0 + millis / 1000.0, -1.0, 100000.0)


def _event_detail(event: Event) -> dict[str, Any]:
    detail = dict(event.data)
    detail.pop("recv_ns", None)
    detail.pop("session_uid", None)
    return detail


def _valid_car(car_index: int | None) -> bool:
    return car_index is not None and 0 <= int(car_index) < 22


def _normalize_compare(
    compare_car: int | None, player_index: int | None
) -> int | None:
    """비교 차량 인덱스를 0~21·내 차 아님으로 정규화한다."""
    if compare_car is None:
        return None
    value = int(compare_car)
    if not 0 <= value < 22:
        return None
    if player_index is not None and value == player_index:
        return None
    return value


def _clamp_ms(value: int, duration_ms: int) -> int:
    return max(0, min(int(value), max(0, int(duration_ms))))


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _scale(value: Any, factor: float) -> float | None:
    number = _float_or_none(value)
    return None if number is None else number * factor


def _finite_or_nan(value: Any) -> float:
    number = _float_or_none(value)
    return math.nan if number is None else number


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> int | None:
    number = _finite_or_none(value)
    return None if number is None else int(round(number))


def _float_or_nan(value: Any) -> float:
    number = _float_or_none(value)
    return math.nan if number is None else number


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _empty_flags() -> dict[str, Any]:
    return {
        "red": False,
        "blue": False,
        "yellow": False,
        "yellow_zones": [],
        "sc": False,
        "vsc": False,
    }


__all__ = [
    "ALERT_EVAL_INTERVAL_NS",
    "CarSeries",
    "DEFAULT_MAX_POINTS",
    "FRAME_CAR_FIELDS",
    "FRAME_CAR_WIDTH",
    "MAP_FRAME_BUCKET_MS",
    "MAX_POINTS_LIMIT",
    "MetricChannel",
    "TELEMETRY_CATALOG",
    "TELEMETRY_PRESETS",
    "TelemetryCategorySpec",
    "TelemetryIndexStore",
    "TelemetryMetricSpec",
    "TelemetrySessionIndex",
    "WHEEL_INDEX_BY_SUFFIX",
    "build_telemetry_index",
    "catalog_payload",
    "downsample_channel",
    "extract_car_series",
    "get_metric_spec",
    "presets_payload",
]
