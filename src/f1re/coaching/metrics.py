"""코너 지표 계산(작업 15, SCRUM-65).

설계 6.2의 코너 지표 표를 랩 샘플(`LapTrace`)에서 계산한다. 지표는 코너 하나의
구간(진입~탈출) 안에서 다음처럼 뽑는다.

- 진입: 브레이크 시작 지점, 최대 브레이크 압력, 트레일 브레이킹(턴인 때 남은 압력)
- 턴인: 조향 시작 지점, 최대 조향량
- 정점: 최저 속도와 그 위치, 조향 수정 횟수
- 탈출: 스로틀 다시 밟은 지점, 풀스로틀 지점, 탈출 속도, 휠 슬립(내 차만)
- 라인: 기준 궤적에서 벗어난 최대 거리(진입·정점·탈출 지점), 트랙 이탈
- 결과: 코너 구간 시간

문턱값은 `config/default.toml`의 `[coaching.thresholds]`에만 둔다(설계 6.2).
값은 데모 송신기 스즈카·모나코 랩에 맞춰 정했고, 실제 녹화로 다시 맞춘다.

몇 가지 판정 규칙(구현자가 정한 값, 보고서에 근거를 남긴다):

- 브레이크 시작은 코너 진입 앞 `brake_search_back_m`까지 거슬러 올라가 찾되,
  코너 진입 직전에 끝난 브레이크 구간은 앞 코너의 것으로 보고 버린다
  (`brake_release_margin_m`). 앞 코너와 간격이 아주 좁은 시케인에서 앞 코너
  브레이크를 이 코너 것으로 잘못 잡지 않기 위해서다.
- 턴인은 코너 진입 앞 `steer_search_back_m` 안에서 조향이 문턱값 아래로
  내려왔다가 처음 위로 올라오는 지점이다. 이른 턴인(코너 진입 전에 미리 꺾는
  것)을 잡으면서, 앞 코너 조향이 아직 남아 있는 구간은 건너뛴다. 조향이 창
  안에서 계속 문턱값 위면(연속 코너) 코너 진입을 턴인으로 본다.
- 조향 수정 횟수는 이동 평균으로 잡음을 지운 조향이 방향을 바꾼 횟수를 세고,
  왕복 두 번(한 번 흔들었다가 돌아옴)을 수정 한 번으로 센다. 데모 시나리오의
  `steer_corrections.count`와 같은 단위다. `steer_correction_deadband`보다 작은
  변화와 `steer_correction_min_gap_m`보다 가까운 전환은 잡음으로 버린다.
- 스로틀 다시 밟은 지점은 최저 속도 뒤에서 스로틀이 문턱값을 아래에서 위로
  지난 마지막 지점이다(잠깐 튄 값에 속지 않는다).
- 기준 궤적 이탈 거리는 같은 랩 거리 근처(`line_dev_search_m`)의 기준 궤적
  선분까지 수직 거리다. 진입·정점·탈출 세 지점 중 가장 큰 값을 쓴다.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

from f1re.config import ConfigError, default_config_path
from f1re.laps.traces import LapTrace, Sample
from f1re.tracks.model import Corner
from f1re.tracks.outline import TrackOutline


@dataclass(frozen=True)
class CoachingThresholds:
    """코너 지표 판정 문턱값. 값은 설정 파일에서 읽는다."""

    brake_on: float
    brake_search_back_m: float
    brake_release_margin_m: float
    steer_on: float
    steer_search_back_m: float
    steer_smooth_m: float
    steer_correction_deadband: float
    steer_correction_min_gap_m: float
    throttle_on: float
    full_throttle: float
    off_track_dev_m: float
    line_dev_search_m: float


@dataclass(frozen=True)
class CornerMetrics:
    """코너 하나의 지표. 거리 단위 m, 속도 km/h, 시간 ms다.

    `exit_wheel_slip`은 탈출 구간(최저 속도 지점~탈출)의 바퀴별 슬립 비율 최대값
    이며 Motion Ex가 있는 내 차 샘플에만 값이 있다. 휠 슬립은 내 차에만 있으므로
    가장 빠른 차 기준과 비교할 때는 쓰지 않는다(설계 6.3).
    """

    corner: int
    brake_start_m: float | None
    brake_peak: float
    trail_brake_residual: float
    turn_in_m: float | None
    steer_peak: float
    min_speed: float
    min_speed_m: float
    steer_corrections: int
    throttle_on_m: float | None
    full_throttle_m: float | None
    exit_speed: float
    max_line_dev_m: float
    off_track: bool
    corner_time_ms: int
    exit_wheel_slip: float | None = None


_THRESHOLD_FIELDS = tuple(field.name for field in fields(CoachingThresholds))

# 페달·조향 문턱값은 0~1 범위다.
_UNIT_INTERVAL_FIELDS = (
    "brake_on",
    "steer_on",
    "throttle_on",
    "full_throttle",
)
_NON_NEGATIVE_FIELDS = (
    "brake_search_back_m",
    "brake_release_margin_m",
    "steer_search_back_m",
    "steer_correction_deadband",
    "steer_correction_min_gap_m",
)
_POSITIVE_FIELDS = (
    "steer_smooth_m",
    "off_track_dev_m",
    "line_dev_search_m",
)


def load_coaching_thresholds(path: str | Path | None = None) -> CoachingThresholds:
    """TOML 설정의 `[coaching.thresholds]`를 읽는다. 없으면 레포 기본 설정."""
    config_path = Path(path) if path is not None else default_config_path()
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"설정 파일을 읽지 못했습니다: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"설정 파일 형식이 잘못됐습니다: {config_path}") from exc

    coaching = raw.get("coaching")
    if not isinstance(coaching, dict) or not isinstance(
        coaching.get("thresholds"), dict
    ):
        raise ConfigError("[coaching.thresholds] 절이 없습니다")
    section = coaching["thresholds"]

    unknown = set(section) - set(_THRESHOLD_FIELDS)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ConfigError(f"[coaching.thresholds]에 모르는 값이 있습니다: {names}")

    values: dict[str, float] = {}
    for name in _THRESHOLD_FIELDS:
        if name not in section:
            raise ConfigError(f"coaching.thresholds.{name} 값이 없습니다")
        values[name] = _number(section[name], f"coaching.thresholds.{name}")

    thresholds = CoachingThresholds(**values)
    _validate_thresholds(thresholds)
    return thresholds


@lru_cache(maxsize=1)
def default_coaching_thresholds() -> CoachingThresholds:
    """레포 기본 설정의 코너 문턱값. 프로세스에서 한 번만 읽는다."""
    return load_coaching_thresholds()


def _validate_thresholds(thresholds: CoachingThresholds) -> None:
    for name in _UNIT_INTERVAL_FIELDS:
        value = getattr(thresholds, name)
        if not 0.0 <= value <= 1.0:
            raise ConfigError(f"coaching.thresholds.{name}은 0~1이어야 합니다")
    if thresholds.full_throttle <= thresholds.throttle_on:
        raise ConfigError(
            "coaching.thresholds.full_throttle은 throttle_on보다 커야 합니다"
        )
    for name in _NON_NEGATIVE_FIELDS:
        if getattr(thresholds, name) < 0.0:
            raise ConfigError(f"coaching.thresholds.{name}은 0 이상이어야 합니다")
    for name in _POSITIVE_FIELDS:
        if getattr(thresholds, name) <= 0.0:
            raise ConfigError(f"coaching.thresholds.{name}은 0보다 커야 합니다")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name}는 숫자여야 합니다")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{name}는 유한한 수여야 합니다")
    return number


# ---------------------------------------------------------------------------
# 공개 계산 함수


def corner_metrics(
    trace: LapTrace,
    corner: Corner,
    ref_path: TrackOutline | None = None,
    *,
    thresholds: CoachingThresholds | None = None,
) -> CornerMetrics:
    """랩 하나에서 코너 `corner`의 지표를 계산한다.

    `ref_path`는 기준 랩의 궤적(`(dist_m, x, z)` 점)이다. 없으면 라인 지표는
    0이고 트랙 이탈은 아니라고 본다.
    """
    limits = thresholds or default_coaching_thresholds()
    samples = _clean_samples(trace)
    if not samples:
        return CornerMetrics(
            corner=corner.number,
            brake_start_m=None,
            brake_peak=0.0,
            trail_brake_residual=0.0,
            turn_in_m=None,
            steer_peak=0.0,
            min_speed=0.0,
            min_speed_m=0.0,
            steer_corrections=0,
            throttle_on_m=None,
            full_throttle_m=None,
            exit_speed=0.0,
            max_line_dev_m=0.0,
            off_track=False,
            corner_time_ms=0,
        )

    brake_start_m, brake_peak = _brake_metrics(samples, corner, limits)
    turn_in_m = _turn_in(samples, corner, limits)
    trail_brake_residual = (
        0.0
        if turn_in_m is None
        else max(0.0, min(1.0, _value_at(samples, turn_in_m, lambda s: s.brake)))
    )

    steer_start = corner.entry_m if turn_in_m is None else min(turn_in_m, corner.entry_m)
    steer_peak = _max_abs_steer(samples, steer_start, corner.exit_m)
    min_speed, min_speed_m = _min_speed(samples, corner)
    steer_corrections = _steer_corrections(
        samples, steer_start, corner.exit_m, limits
    )
    throttle_on_m = _throttle_crossing(
        samples, min_speed_m, corner.exit_m, limits.throttle_on
    )
    full_throttle_m = _throttle_crossing(
        samples, min_speed_m, corner.exit_m, limits.full_throttle
    )
    exit_speed = _value_at(samples, corner.exit_m, lambda s: s.speed)
    max_line_dev_m = _line_deviation(samples, corner, ref_path, limits)
    exit_wheel_slip = _exit_wheel_slip(samples, min_speed_m, corner.exit_m)
    corner_time_ms = int(
        round(
            time_at_distance(trace, corner.exit_m)
            - time_at_distance(trace, corner.entry_m)
        )
    )

    return CornerMetrics(
        corner=corner.number,
        brake_start_m=brake_start_m,
        brake_peak=brake_peak,
        trail_brake_residual=trail_brake_residual,
        turn_in_m=turn_in_m,
        steer_peak=steer_peak,
        min_speed=min_speed,
        min_speed_m=min_speed_m,
        steer_corrections=steer_corrections,
        throttle_on_m=throttle_on_m,
        full_throttle_m=full_throttle_m,
        exit_speed=exit_speed,
        max_line_dev_m=max_line_dev_m,
        off_track=max_line_dev_m >= limits.off_track_dev_m,
        corner_time_ms=corner_time_ms,
        exit_wheel_slip=exit_wheel_slip,
    )


def time_at_distance(trace: LapTrace, dist_m: float) -> float:
    """랩 거리 `dist_m`의 랩 시각(ms)을 샘플 사이 선형 보간으로 구한다.

    샘플 범위를 벗어나면 처음·마지막 샘플 시각으로 자른다.
    """
    samples = _clean_samples(trace)
    if not samples:
        return 0.0
    return _time_at(samples, dist_m)


def corner_duration_ms(trace: LapTrace, corner: Corner) -> float:
    """코너 진입~탈출 구간 시간(ms). 랩 거리 기준으로 보간한다."""
    return time_at_distance(trace, corner.exit_m) - time_at_distance(
        trace, corner.entry_m
    )


# ---------------------------------------------------------------------------
# 내부 계산


def _clean_samples(trace: LapTrace) -> list[Sample]:
    """유한한 값만 남기고 랩 거리 순으로 정렬한다."""
    cleaned: list[Sample] = []
    for sample in trace.samples:
        if not all(
            math.isfinite(value)
            for value in (
                sample.dist_m,
                sample.speed,
                sample.throttle,
                sample.brake,
                sample.steer,
                sample.x,
                sample.z,
            )
        ):
            continue
        cleaned.append(sample)
    cleaned.sort(key=lambda sample: sample.dist_m)
    return cleaned


def _window(
    samples: Sequence[Sample], start_m: float, end_m: float
) -> list[Sample]:
    return [sample for sample in samples if start_m <= sample.dist_m <= end_m]


def _value_at(
    samples: Sequence[Sample], dist_m: float, getter: Callable[[Sample], float]
) -> float:
    """`dist_m` 지점의 값을 앞뒤 샘플 선형 보간으로 구한다(범위 밖은 자름)."""
    if not samples:
        return 0.0
    if dist_m <= samples[0].dist_m:
        return float(getter(samples[0]))
    if dist_m >= samples[-1].dist_m:
        return float(getter(samples[-1]))
    previous = samples[0]
    for current in samples[1:]:
        if current.dist_m >= dist_m:
            span = current.dist_m - previous.dist_m
            if span <= 0.0:
                return float(getter(current))
            fraction = (dist_m - previous.dist_m) / span
            return float(getter(previous)) + fraction * (
                float(getter(current)) - float(getter(previous))
            )
        previous = current
    return float(getter(samples[-1]))


def _time_at(samples: Sequence[Sample], dist_m: float) -> float:
    return _value_at(samples, dist_m, lambda sample: float(sample.t_ms))


def _position_at(samples: Sequence[Sample], dist_m: float) -> tuple[float, float]:
    x = _value_at(samples, dist_m, lambda sample: sample.x)
    z = _value_at(samples, dist_m, lambda sample: sample.z)
    return x, z


def _brake_metrics(
    samples: Sequence[Sample],
    corner: Corner,
    thresholds: CoachingThresholds,
) -> tuple[float | None, float]:
    """브레이크 시작 지점과 최대 브레이크 압력."""
    start_m = max(0.0, corner.entry_m - thresholds.brake_search_back_m)
    window = _window(samples, start_m, corner.apex_m)

    # 브레이크가 문턱값 이상인 구간(러닝)을 찾는다.
    runs: list[tuple[float, float, float]] = []  # (시작, 끝, 최대 압력)
    run_start: float | None = None
    run_peak = 0.0
    previous_dist = window[0].dist_m if window else start_m
    for sample in window:
        if sample.brake >= thresholds.brake_on:
            if run_start is None:
                run_start = sample.dist_m
                run_peak = sample.brake
            else:
                run_peak = max(run_peak, sample.brake)
        elif run_start is not None:
            runs.append((run_start, previous_dist, run_peak))
            run_start = None
            run_peak = 0.0
        previous_dist = sample.dist_m
    if run_start is not None:
        runs.append((run_start, window[-1].dist_m, run_peak))

    # 코너 진입에 닿지 않고 끝난 구간은 앞 코너의 브레이크다.
    candidates = [
        run
        for run in runs
        if run[1] >= corner.entry_m - thresholds.brake_release_margin_m
    ]
    if not candidates:
        peak = max(
            (
                sample.brake
                for sample in _window(samples, corner.entry_m, corner.exit_m)
            ),
            default=0.0,
        )
        return None, peak

    chosen_start, _chosen_end, chosen_peak = candidates[-1]
    peak = max(
        (
            sample.brake
            for sample in _window(samples, chosen_start, corner.exit_m)
        ),
        default=chosen_peak,
    )
    return chosen_start, peak


def _turn_in(
    samples: Sequence[Sample],
    corner: Corner,
    thresholds: CoachingThresholds,
) -> float | None:
    """조향이 문턱값 아래로 내려왔다가 처음 위로 올라오는 지점(턴인)."""
    start_m = max(0.0, corner.entry_m - thresholds.steer_search_back_m)
    window = _window(samples, start_m, corner.apex_m)
    if not window:
        return None

    saw_below = False
    for sample in window:
        if abs(sample.steer) < thresholds.steer_on:
            saw_below = True
        elif saw_below:
            return sample.dist_m
    # 조향이 창 안에서 계속 문턱값 위면(연속 코너) 코너 진입을 턴인으로 본다.
    if abs(_value_at(samples, corner.entry_m, lambda s: s.steer)) >= thresholds.steer_on:
        return corner.entry_m
    return None


def _max_abs_steer(
    samples: Sequence[Sample], start_m: float, end_m: float
) -> float:
    return max(
        (abs(sample.steer) for sample in _window(samples, start_m, end_m)),
        default=0.0,
    )


def _min_speed(samples: Sequence[Sample], corner: Corner) -> tuple[float, float]:
    inside = _window(samples, corner.entry_m, corner.exit_m)
    if not inside:
        # 부분 랩(아웃랩 등)은 코너 정점에 가장 가까운 샘플로 대신한다.
        inside = [min(samples, key=lambda sample: abs(sample.dist_m - corner.apex_m))]
    lowest = min(inside, key=lambda sample: sample.speed)
    return lowest.speed, lowest.dist_m


def _steer_corrections(
    samples: Sequence[Sample],
    start_m: float,
    end_m: float,
    thresholds: CoachingThresholds,
) -> int:
    """조향 수정 횟수. 이동 평균으로 잡음을 지운 뒤 방향 전환을 센다.

    방향 전환 두 번(한 번 흔들었다가 되돌아옴)을 수정 한 번으로 센다.
    """
    inside = _window(samples, start_m, end_m)
    if len(inside) < 10:
        return 0
    smoothed = _moving_average(
        inside, [sample.steer for sample in inside], thresholds.steer_smooth_m
    )
    direction = 0
    last_value = smoothed[0]
    reversals = 0
    last_reversal_m = -math.inf
    for sample, value in zip(inside[1:], smoothed[1:]):
        delta = value - last_value
        if abs(delta) < thresholds.steer_correction_deadband:
            # 잡음은 흘려보내고 마지막으로 의미 있던 값부터 다시 잰다.
            continue
        sign = 1 if delta > 0.0 else -1
        if direction == 0:
            direction = sign
        elif sign != direction:
            if sample.dist_m - last_reversal_m >= thresholds.steer_correction_min_gap_m:
                reversals += 1
                last_reversal_m = sample.dist_m
            direction = sign
        last_value = value
    return reversals // 2


def _moving_average(
    samples: Sequence[Sample], values: list[float], window_m: float
) -> list[float]:
    """랩 거리 기준 창(±window_m/2)의 이동 평균. 양 끝은 창을 줄인다."""
    count = len(values)
    if count == 0:
        return []
    span = samples[-1].dist_m - samples[0].dist_m
    step = span / max(1, count - 1)
    half = max(1, int(round(window_m / 2.0 / max(step, 1e-6))))
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    smoothed: list[float] = []
    for index in range(count):
        start = max(0, index - half)
        end = min(count, index + half + 1)
        smoothed.append((prefix[end] - prefix[start]) / (end - start))
    return smoothed


def _throttle_crossing(
    samples: Sequence[Sample],
    min_speed_m: float,
    end_m: float,
    threshold: float,
) -> float | None:
    """최저 속도 뒤에서 스로틀이 문턱값을 아래→위로 지난 마지막 지점."""
    start_index = 0
    for index, sample in enumerate(samples):
        if sample.dist_m >= min_speed_m:
            start_index = index
            break
    crossing: float | None = None
    previous = samples[max(0, start_index - 1)]
    for sample in samples[start_index:]:
        if sample.dist_m > end_m:
            break
        if previous.throttle < threshold <= sample.throttle:
            crossing = sample.dist_m
        previous = sample
    return crossing


def _exit_wheel_slip(
    samples: Sequence[Sample], start_m: float, end_m: float
) -> float | None:
    """탈출 구간의 바퀴 슬립 최대값. Motion Ex가 없는 차는 None."""
    values: list[float] = []
    for sample in _window(samples, start_m, end_m):
        if sample.wheel_slip:
            values.append(max(sample.wheel_slip))
    return max(values) if values else None


def _line_deviation(
    samples: Sequence[Sample],
    corner: Corner,
    ref_path: TrackOutline | None,
    thresholds: CoachingThresholds,
) -> float:
    """진입·정점·탈출 지점에서 기준 궤적까지의 수직 거리 중 최대값."""
    if ref_path is None or len(ref_path.points) < 2:
        return 0.0
    if not any(sample.x or sample.z for sample in samples):
        # 위치 값이 아예 없으면(Motion 없이 윤곽도 없던 랩) 라인을 판단하지 않는다.
        return 0.0
    if not any(x or z for _dist, x, z in ref_path.points):
        return 0.0

    deviations: list[float] = []
    for dist_m in (corner.entry_m, corner.apex_m, corner.exit_m):
        point = _position_at(samples, dist_m)
        deviations.append(
            _distance_to_path(
                ref_path, point, dist_m, thresholds.line_dev_search_m
            )
        )
    return max(deviations)


def _distance_to_path(
    ref_path: TrackOutline,
    point: tuple[float, float],
    dist_hint: float,
    search_m: float,
) -> float:
    """랩 거리 `dist_hint` 근처 기준 궤적 선분까지의 최소 수직 거리."""
    px, pz = point
    points = ref_path.points
    best = math.inf
    for first, second in zip(points, points[1:]):
        if second[0] < dist_hint - search_m or first[0] > dist_hint + search_m:
            continue
        best = min(best, _point_segment_distance(px, pz, first, second))
    if math.isfinite(best):
        return best
    # 창 안에 선분이 없으면(랩 양 끝) 가장 가까운 점과의 거리를 쓴다.
    return min(
        math.hypot(px - x, pz - z) for _dist, x, z in points
    )


def _point_segment_distance(
    px: float,
    pz: float,
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> float:
    ax, az = first[1], first[2]
    bx, bz = second[1], second[2]
    dx = bx - ax
    dz = bz - az
    length_squared = dx * dx + dz * dz
    if length_squared <= 0.0:
        return math.hypot(px - ax, pz - az)
    fraction = ((px - ax) * dx + (pz - az) * dz) / length_squared
    fraction = max(0.0, min(1.0, fraction))
    nearest_x = ax + fraction * dx
    nearest_z = az + fraction * dz
    return math.hypot(px - nearest_x, pz - nearest_z)
