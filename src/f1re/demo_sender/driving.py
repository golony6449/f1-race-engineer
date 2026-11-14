"""데모 송신기 주행 모델.

코너별 목표 최저 속도와 가속·제동 한계로 랩 속도 곡선을 만들고, 중심선
(`TrackOutline`)을 따라 60Hz 주행 샘플을 만든다. 물리 시뮬레이터가 아니라
데모·테스트에 쓸 그럴듯한 값을 만드는 것이 목적이다.

속도 곡선은 다음 순서로 만든다.

1. 중심선 점(5m 간격)마다 코너 구간이면 코너 목표 속도를 상한으로 둔다.
2. 뒤로 훑으며 제동 한계(다음 점 속도로부터 감속 가능한 최대 속도)를 적용한다.
3. 앞으로 훑으며 가속 한계를 적용한다.
4. 닫힌 고리가 안정될 때까지 2~3을 반복한다.

목표 속도 표는 공개 온보드·서킷 설명에서 본 대략값을 구현자가 정리한 것이다
(정확한 게임 값이 아니다). 표에 없는 코너는 중심선 곡률로 계산한 값을 쓴다.
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass

from f1re.tracks.model import Corner, Track
from f1re.tracks.outline import TrackOutline

# 공식 서킷 길이가 기준이므로 중심선 점 간격을 이 길이에 맞춰 조금 늘리거나 줄인다.
DEFAULT_RATE_HZ = 60.0

# 최고 속도(km/h). 스즈카는 130R·메인 직선, 모나코는 터널 구간 기준의 대략값.
TOP_SPEED_KMH: dict[int, float] = {
    13: 335.0,  # 스즈카
    5: 300.0,  # 모나코
}
FALLBACK_TOP_SPEED_KMH = 330.0

# 코너별 목표 최저 속도(km/h). 공개 온보드·코너 설명을 참고한 대략값이다.
CORNER_TARGET_SPEEDS_KMH: dict[int, dict[int, float]] = {
    13: {  # 스즈카
        1: 240.0,
        2: 250.0,
        3: 190.0,
        4: 200.0,
        5: 185.0,
        6: 175.0,
        7: 170.0,  # 던롭
        8: 160.0,  # 데그너 1
        9: 150.0,  # 데그너 2
        10: 130.0,
        11: 85.0,  # 헤어핀
        12: 250.0,  # 200R
        13: 185.0,  # 스푼 1
        14: 175.0,  # 스푼 2
        15: 300.0,  # 130R
        16: 110.0,  # 시케인
        17: 115.0,
        18: 175.0,  # 최종 코너
    },
    5: {  # 모나코
        1: 145.0,  # 생트 데보트
        2: 165.0,  # 보 리바주
        3: 130.0,  # 마스네
        4: 145.0,  # 카지노
        5: 125.0,  # 미라보 상
        6: 55.0,  # 헤어핀
        7: 115.0,  # 미라보 하
        8: 120.0,  # 포르티에
        9: 220.0,  # 터널
        10: 105.0,  # 누벨 시케인
        11: 110.0,
        12: 130.0,  # 타박
        13: 145.0,  # 수영장 1단
        14: 145.0,
        15: 140.0,  # 수영장 2단
        16: 140.0,
        17: 125.0,
        18: 78.0,  # 라스카스
        19: 100.0,  # 노게
    },
}

# 곡선 반경에서 목표 속도를 계산할 때 쓰는 한계 횡가속도(m/s²). 표에 없는 코너용.
FALLBACK_LATERAL_ACCEL_MPS2 = 25.0

# 가속·제동 한계. F1 차량의 대략값이다.
ACCEL_MAX_MPS2 = 15.0
ACCEL_ZERO_SPEED_MPS = 140.0  # 이 속도에서 가속 한계가 0이 된다(공기저항).
BRAKE_BASE_MPS2 = 30.0
BRAKE_AERO_MPS2 = 20.0

# 기어별 최고 속도(km/h). 8단 기준.
GEAR_TOP_SPEEDS_KMH: tuple[float, ...] = (85.0, 125.0, 165.0, 205.0, 245.0, 285.0, 320.0, 360.0)
IDLE_RPM = 4000
MAX_RPM = 12000
STEER_GAIN = 25.0
GRAVITY = 9.81

# 코너 목표 속도 곡선은 apex에서 가장 낮고 구간 끝에서 조금 높게 둔다(비율).
CORNER_EDGE_SPEED_FACTOR = 1.05


@dataclass(frozen=True)
class DriverProfile:
    """운전자 성향. `pace`가 1보다 크면 코너를 더 빠르게 지나간다.

    `brake_bias_m`은 브레이크 지점(코너 최저 속도 지점)을 몇 m 뒤로 미는지다.
    양수면 늦게 브레이크를 밟고 apex 이후에 최저 속도가 온다.
    `consistency`는 0~1이며 1이면 코너마다 속도가 항상 같고, 낮을수록 편차가 커진다.
    """

    pace: float = 1.0
    brake_bias_m: float = 0.0
    consistency: float = 0.85

    def __post_init__(self) -> None:
        if not math.isfinite(self.pace) or self.pace <= 0:
            raise ValueError("pace는 0보다 큰 유한한 값이어야 합니다")
        if not math.isfinite(self.brake_bias_m):
            raise ValueError("brake_bias_m은 유한한 값이어야 합니다")
        if not 0.0 <= self.consistency <= 1.0:
            raise ValueError("consistency는 0~1이어야 합니다")


@dataclass(frozen=True)
class CornerEffect:
    """시나리오가 한 랩의 코너 하나에 주입하는 주행 변화.

    `brake_shift_m`은 브레이크 지점(최저 속도 지점)을 미는 거리다(양수=늦게).
    `throttle_delay_m`는 apex 뒤로 스로틀을 늦게 밟는 거리, `wide_m`은 라인
    바깥으로 밀리는 폭, `steer_corrections`는 코너 안 조향 수정 횟수,
    `turn_in_early_m`은 코너 진입 전에 미리 꺾는 거리다.
    """

    brake_shift_m: float = 0.0
    throttle_delay_m: float = 0.0
    wide_m: float = 0.0
    steer_corrections: int = 0
    steer_amplitude: float = 0.0
    turn_in_early_m: float = 0.0
    off_track: bool = False

    def merged(self, other: "CornerEffect") -> "CornerEffect":
        """같은 코너에 걸린 시나리오 두 개를 하나로 합친다."""
        return CornerEffect(
            brake_shift_m=self.brake_shift_m + other.brake_shift_m,
            throttle_delay_m=max(self.throttle_delay_m, other.throttle_delay_m),
            wide_m=max(self.wide_m, other.wide_m),
            steer_corrections=self.steer_corrections + other.steer_corrections,
            steer_amplitude=max(self.steer_amplitude, other.steer_amplitude),
            turn_in_early_m=max(self.turn_in_early_m, other.turn_in_early_m),
            off_track=self.off_track or other.off_track,
        )


@dataclass(frozen=True)
class Sample:
    """한 순간의 차 상태. 거리 단위는 m, 속도는 km/h다."""

    t_ms: int  # 랩 시작 기준
    dist_m: float
    speed_kmh: float
    throttle: float
    brake: float
    steer: float  # -1(왼쪽)~1(오른쪽)
    gear: int
    rpm: int
    x: float
    z: float
    yaw: float  # 라디안, atan2(dz, dx)
    lateral_g: float
    longitudinal_g: float
    line_offset_m: float = 0.0  # 중심선에서 오른쪽이 양수인 이탈 폭


@dataclass(frozen=True)
class SpeedProfile:
    """중심선 점마다의 속도·시각. `dist_m`은 랩 거리, 시각은 랩 시작 기준."""

    dists: tuple[float, ...]
    curvatures: tuple[float, ...]
    speeds: tuple[float, ...]  # m/s
    times: tuple[float, ...]  # s, 마지막 점까지
    segment_lengths: tuple[float, ...]
    lap_time_s: float


def _curvature(points: list[tuple[float, float, float]], half: int = 2, smooth_k: int = 4) -> list[float]:
    """중심선 각 점의 곡률(1/m, 왼쪽 양수). 점 간격은 평균값을 쓴다."""
    count = len(points)
    steps = [
        math.dist(points[index][1:], points[(index + 1) % count][1:])
        for index in range(count)
    ]
    step = max(sum(steps) / count, 1e-6)
    headings: list[float] = []
    for index in range(count):
        previous = points[(index - half) % count]
        following = points[(index + half) % count]
        headings.append(math.atan2(following[2] - previous[2], following[1] - previous[1]))

    raw: list[float] = []
    for index in range(count):
        delta = headings[(index + 1) % count] - headings[index]
        while delta > math.pi:
            delta -= 2 * math.pi
        while delta < -math.pi:
            delta += 2 * math.pi
        raw.append(delta / step)

    smoothed: list[float] = []
    for index in range(count):
        total = 0.0
        for offset in range(-smooth_k, smooth_k + 1):
            total += raw[(index + offset) % count]
        smoothed.append(total / (2 * smooth_k + 1))
    return smoothed


def _corner_target_kmh(track_id: int, corner: Corner, curvature: list[float], dists: tuple[float, ...]) -> float:
    """코너 목표 최저 속도(km/h). 표에 없으면 곡률로 계산한다."""
    table = CORNER_TARGET_SPEEDS_KMH.get(track_id, {})
    if corner.number in table:
        return table[corner.number]
    apex_index = min(
        range(len(dists)),
        key=lambda index: abs(dists[index] - corner.apex_m),
    )
    radius_curvature = abs(curvature[apex_index])
    if radius_curvature < 1e-6:
        return 120.0
    speed_mps = math.sqrt(FALLBACK_LATERAL_ACCEL_MPS2 / radius_curvature)
    return min(TOP_SPEED_KMH.get(track_id, FALLBACK_TOP_SPEED_KMH), speed_mps * 3.6)


def _scaled_distances(points: list[tuple[float, float, float]], track_length_m: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """중심선 점의 (랩 거리, 구간 길이). 구간 길이 합을 공식 랩 길이에 맞춘다."""
    raw_lengths = [
        math.dist(points[index][1:], points[(index + 1) % len(points)][1:])
        for index in range(len(points))
    ]
    total = sum(raw_lengths)
    if total <= 0:
        raise ValueError("중심선 길이가 0입니다")
    scale = track_length_m / total
    lengths = tuple(length * scale for length in raw_lengths)
    dists: list[float] = []
    cursor = 0.0
    for index in range(len(points)):
        dists.append(cursor)
        cursor += lengths[index]
    return tuple(dists), lengths


def _build_profile(
    track: Track,
    outline: TrackOutline,
    driver: DriverProfile,
    rng: random.Random,
    corner_effects: dict[int, CornerEffect] | None = None,
) -> SpeedProfile:
    """코너 목표 속도와 가속·제동 한계로 한 랩의 속도 곡선을 만든다."""
    points = outline.points
    point_count = len(points)
    dists, segment_lengths = _scaled_distances(points, track.length_m)
    curvatures = _curvature(points)
    top_speed_mps = TOP_SPEED_KMH.get(track.track_id, FALLBACK_TOP_SPEED_KMH) / 3.6

    limits = [top_speed_mps] * point_count
    for corner in track.corners:
        effects = corner_effects.get(corner.number) if corner_effects else None
        target_kmh = _corner_target_kmh(track.track_id, corner, curvatures, dists)
        jitter = 1.0 + (1.0 - driver.consistency) * rng.uniform(-0.03, 0.03)
        target_mps = (target_kmh / 3.6) * driver.pace * jitter

        # 늦은 브레이크는 최저 속도 지점을 코너 뒤쪽으로 민다(구간 안으로 자른다).
        shift = driver.brake_bias_m + (effects.brake_shift_m if effects else 0.0)
        apex = _effective_apex(corner, shift)
        half_span = max(apex - corner.entry_m, corner.exit_m - apex, 1.0)

        # 늦게 브레이크를 밟으면 코너 진입 속도가 높아져 제동 시작이 뒤로 밀린다.
        # 진입 쪽 상한만 올리고 apex의 최저 속도는 그대로 둔다.
        entry_boost = 1.0
        if effects is not None and effects.brake_shift_m:
            # 계수는 "10m 늦은 브레이크 → 제동 시작 약 10~12m 뒤"가 되게 맞춘 값이다.
            entry_boost = 1.0 + max(-0.20, min(0.35, effects.brake_shift_m * 0.018))

        for index in range(point_count):
            distance = dists[index]
            if distance < corner.entry_m or distance > corner.exit_m:
                continue
            fraction = abs(distance - apex) / half_span
            ceiling = target_mps * (1.0 + (CORNER_EDGE_SPEED_FACTOR - 1.0) * fraction)
            if distance < apex:
                ceiling *= 1.0 + (entry_boost - 1.0) * fraction
            if ceiling < limits[index]:
                limits[index] = ceiling

    # 닫힌 고리에서 감속·가속 한계를 양방향으로 반복 적용한다.
    speeds = list(limits)
    for _ in range(40):
        for index in range(point_count - 1, -1, -1):
            following = (index + 1) % point_count
            decel = BRAKE_BASE_MPS2 + BRAKE_AERO_MPS2 * (speeds[following] / top_speed_mps)
            allowed = math.sqrt(max(0.0, speeds[following] ** 2 + 2.0 * decel * segment_lengths[index]))
            if speeds[index] > allowed:
                speeds[index] = allowed
        for index in range(point_count):
            following = (index + 1) % point_count
            ratio = min(1.0, speeds[index] / ACCEL_ZERO_SPEED_MPS)
            accel = max(0.1, ACCEL_MAX_MPS2 * (1.0 - ratio**1.5))
            allowed = math.sqrt(max(0.0, speeds[index] ** 2 + 2.0 * accel * segment_lengths[index]))
            if speeds[following] > allowed:
                speeds[following] = allowed

    times = [0.0] * point_count
    for index in range(point_count - 1):
        average = max((speeds[index] + speeds[index + 1]) / 2.0, 1.0)
        times[index + 1] = times[index] + segment_lengths[index] / average
    closing_average = max((speeds[-1] + speeds[0]) / 2.0, 1.0)
    lap_time = times[-1] + segment_lengths[-1] / closing_average

    return SpeedProfile(
        dists=dists,
        curvatures=tuple(curvatures),
        speeds=tuple(speeds),
        times=tuple(times),
        segment_lengths=segment_lengths,
        lap_time_s=lap_time,
    )


def _profile_state(profile: SpeedProfile, time_s: float) -> tuple[float, float, float, float]:
    """랩 시각 `time_s`의 (랩 거리, 속도 m/s, 곡률, 속도 변화율 m/s²)."""
    times = profile.times
    count = len(times)
    if time_s <= 0.0:
        index = 0
        fraction = 0.0
        segment = 0
    elif time_s >= profile.lap_time_s:
        return (
            profile.dists[-1] + profile.segment_lengths[-1] * 0.999,
            profile.speeds[0],
            profile.curvatures[-1],
            0.0,
        )
    else:
        position = bisect.bisect_right(times, time_s) - 1
        position = max(0, min(position, count - 1))
        segment = position
        if position >= count - 1:
            start = times[-1]
            end = profile.lap_time_s
        else:
            start = times[position]
            end = times[position + 1]
        span = max(end - start, 1e-9)
        fraction = min(1.0, max(0.0, (time_s - start) / span))

    following = (segment + 1) % count
    distance = profile.dists[segment] + fraction * profile.segment_lengths[segment]
    distance %= profile.dists[-1] + profile.segment_lengths[-1]
    speed = profile.speeds[segment] + fraction * (profile.speeds[following] - profile.speeds[segment])
    curvature = profile.curvatures[segment] + fraction * (profile.curvatures[following] - profile.curvatures[segment])

    dt = 1.0 / DEFAULT_RATE_HZ
    next_speed = _profile_state_speed(profile, time_s + dt)
    acceleration = (next_speed - speed) / dt
    return distance, max(speed, 1.0), curvature, acceleration


def _profile_state_speed(profile: SpeedProfile, time_s: float) -> float:
    if time_s >= profile.lap_time_s:
        return profile.speeds[0]
    times = profile.times
    position = bisect.bisect_right(times, time_s) - 1
    position = max(0, min(position, len(times) - 1))
    if position >= len(times) - 1:
        start = times[-1]
        end = profile.lap_time_s
        fraction = 0.0 if end <= start else (time_s - start) / (end - start)
        return profile.speeds[-1] + fraction * (profile.speeds[0] - profile.speeds[-1])
    start = times[position]
    end = times[position + 1]
    fraction = 0.0 if end <= start else (time_s - start) / (end - start)
    return profile.speeds[position] + fraction * (profile.speeds[position + 1] - profile.speeds[position])


def gear_and_rpm(speed_kmh: float) -> tuple[int, int]:
    """속도로 기어와 엔진 회전수를 만든다."""
    if speed_kmh <= 1.0:
        return 1, IDLE_RPM
    gear = len(GEAR_TOP_SPEEDS_KMH)
    for index, top in enumerate(GEAR_TOP_SPEEDS_KMH):
        if speed_kmh <= top:
            gear = index + 1
            break
    lower = 0.0 if gear == 1 else GEAR_TOP_SPEEDS_KMH[gear - 2] * 0.55
    upper = GEAR_TOP_SPEEDS_KMH[gear - 1]
    fraction = 0.0 if upper <= lower else min(1.0, max(0.0, (speed_kmh - lower) / (upper - lower)))
    rpm = IDLE_RPM + int(round((MAX_RPM - IDLE_RPM) * (0.35 + 0.65 * fraction)))
    return gear, min(MAX_RPM, max(IDLE_RPM, rpm))


def pedals_for_acceleration(acceleration_mps2: float) -> tuple[float, float]:
    """속도 변화율로 스로틀·브레이크 입력을 만든다."""
    if acceleration_mps2 < -0.8:
        brake = min(1.0, -acceleration_mps2 / 30.0)
        return 0.0, max(0.0, brake)
    if acceleration_mps2 > 0.2:
        throttle = min(1.0, 0.55 + acceleration_mps2 / 12.0)
        return max(0.0, throttle), 0.0
    return 0.35, 0.0


def build_speed_profile(
    track: Track,
    outline: TrackOutline,
    driver: DriverProfile,
    rng: random.Random,
    corner_effects: dict[int, CornerEffect] | None = None,
) -> SpeedProfile:
    """한 랩의 속도 곡선을 만든다. 레이스 필드의 AI 차 주행에도 쓴다."""
    return _build_profile(track, outline, driver, rng, corner_effects)


def profile_state(profile: SpeedProfile, time_s: float) -> tuple[float, float, float, float]:
    """랩 시각 `time_s`의 (랩 거리, 속도 m/s, 곡률, 속도 변화율 m/s²)."""
    return _profile_state(profile, time_s)


def profile_time_at_distance(profile: SpeedProfile, distance_m: float) -> float:
    """랩 거리 `distance_m`의 프로필 시각(s).

    프로필은 랩마다 같은 닫힌 곡선이므로 범위를 벗어난 거리는 랩 길이로 나눈
    나머지를 쓴다. 거리로 위치를 정한 뒤 그 위치의 속도·곡률·경과 시간을
    조회할 때 쓴다. 마지막 구간에서 시각이 랩 타임과 정확히 같아지면
    `_profile_state`가 랩 시작 속도를 돌려주므로 아주 조금 앞에서 멈춘다.
    """
    dists = profile.dists
    length = dists[-1] + profile.segment_lengths[-1]
    distance = distance_m % length if length > 0.0 else 0.0
    index = max(0, min(bisect.bisect_right(dists, distance) - 1, len(dists) - 1))
    if index >= len(dists) - 1:
        start = dists[-1]
        end = length
        span = max(end - start, 1e-9)
        fraction = min(1.0 - 1e-9, max(0.0, (distance - start) / span))
        return profile.times[-1] + fraction * (profile.lap_time_s - profile.times[-1])
    start = dists[index]
    end = dists[index + 1]
    span = max(end - start, 1e-9)
    fraction = min(1.0, max(0.0, (distance - start) / span))
    return profile.times[index] + fraction * (profile.times[index + 1] - profile.times[index])


def profile_state_at_distance(
    profile: SpeedProfile, distance_m: float
) -> tuple[float, float, float, float]:
    """랩 거리 `distance_m`의 (랩 거리, 속도 m/s, 곡률, 속도 변화율 m/s²)."""
    return _profile_state(profile, profile_time_at_distance(profile, distance_m))


def _effective_apex(corner: Corner, shift_m: float) -> float:
    """브레이크 지점 이동을 반영한 코너 최저 속도 지점(코너 안으로 자른다)."""
    span = corner.exit_m - corner.entry_m
    margin = max(1.0, min(span * 0.1, 10.0))
    return min(max(corner.apex_m + shift_m, corner.entry_m + margin), corner.exit_m - margin)


def _turn_sign(outline: TrackOutline, corner: Corner) -> float:
    """코너 진행 방향. +1=왼쪽(곡률 양수), -1=오른쪽. 라인 바깥 방향의 부호다."""
    turn = outline.heading_at(corner.exit_m) - outline.heading_at(corner.entry_m)
    while turn > math.pi:
        turn -= 2.0 * math.pi
    while turn < -math.pi:
        turn += 2.0 * math.pi
    if abs(turn) < 1e-6:
        return 1.0
    return 1.0 if turn > 0.0 else -1.0


def _scenario_windows(
    track: Track,
    outline: TrackOutline,
    driver: DriverProfile,
    corner_effects: dict[int, CornerEffect],
) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[float, float, int, float]],
    list[tuple[float, float]],
    list[tuple[float, float, float, float]],
]:
    """시나리오 효과를 샘플 생성용 구간 목록으로 바꾼다.

    (라인 이탈, 조향 수정, 스로틀 지연, 이른 턴인) 구간을 돌려준다. 이른
    턴인 구간은 (시작 거리, entry, apex, 조향 크기)이고 시작 거리에서 entry까지
    조향을 더하다가 apex까지 서서히 기본 조향으로 돌아간다.
    """
    wide: list[tuple[float, float, float]] = []
    steer: list[tuple[float, float, int, float]] = []
    throttle: list[tuple[float, float]] = []
    turn_in: list[tuple[float, float, float, float]] = []
    for corner in track.corners:
        effect = corner_effects.get(corner.number)
        if effect is None:
            continue
        turn_sign = _turn_sign(outline, corner)
        if effect.wide_m:
            # 바깥쪽은 코너가 도는 쪽(회전 중심)의 반대쪽이다. 라인 오프셋의
            # 양수는 진행 방향의 오른쪽이므로 왼쪽 코너(양수 회전)면 양수다.
            wide.append((corner.entry_m, corner.exit_m, turn_sign * effect.wide_m))
        if effect.steer_corrections:
            steer.append(
                (corner.entry_m, corner.exit_m, effect.steer_corrections, effect.steer_amplitude)
            )
        if effect.throttle_delay_m:
            apex = _effective_apex(corner, driver.brake_bias_m + effect.brake_shift_m)
            end = min(track.length_m, apex + effect.throttle_delay_m)
            throttle.append((apex, end))
        if effect.turn_in_early_m:
            start = max(0.0, corner.entry_m - effect.turn_in_early_m)
            # 코너가 도는 방향으로 미리 꺾는다(기본 조향과 같은 부호).
            turn_in.append((start, corner.entry_m, corner.apex_m, -turn_sign * 0.35))
    return wide, steer, throttle, turn_in


def generate_lap(
    track: Track,
    outline: TrackOutline,
    driver: DriverProfile,
    lap: int,
    rng: random.Random,
    *,
    rate_hz: float = DEFAULT_RATE_HZ,
    corner_effects: dict[int, CornerEffect] | None = None,
) -> list[Sample]:
    """한 랩의 주행 샘플을 만든다. `t_ms`는 랩 시작 기준이다.

    `corner_effects`는 그 랩에만 적용하는 시나리오 주입이다(코너 번호 → 효과).
    같은 `rng` 상태(같은 시드)에서는 항상 같은 결과가 나온다.
    """
    if rate_hz <= 0:
        raise ValueError("rate_hz는 0보다 커야 합니다")
    profile = _build_profile(track, outline, driver, rng, corner_effects)
    wide_windows, steer_windows, throttle_windows, turn_in_windows = _scenario_windows(
        track, outline, driver, corner_effects or {}
    )
    sample_count = max(1, int(math.ceil(profile.lap_time_s * rate_hz)))
    samples: list[Sample] = []

    for index in range(sample_count):
        time_s = index / rate_hz
        distance, speed_mps, curvature, acceleration = _profile_state(profile, time_s)
        speed_kmh = speed_mps * 3.6
        throttle, brake = pedals_for_acceleration(acceleration)
        for start, end in throttle_windows:
            if start <= distance <= end:
                throttle = 0.0  # 스로틀 늦음: apex 뒤로 한동안 밟지 않는다.
                break

        line_offset = 0.0
        for entry, exit_m, amplitude in wide_windows:
            if entry <= distance <= exit_m:
                line_offset += amplitude * math.sin(math.pi * (distance - entry) / (exit_m - entry))

        steer_wobble = 0.0
        for entry, exit_m, count, amplitude in steer_windows:
            if entry <= distance <= exit_m:
                fraction = (distance - entry) / (exit_m - entry)
                steer_wobble += amplitude * math.sin(2.0 * math.pi * count * fraction)
        for start, entry_m, apex_m, amplitude in turn_in_windows:
            if start <= distance <= entry_m:
                span = max(entry_m - start, 1e-9)
                steer_wobble += amplitude * (distance - start) / span
            elif entry_m < distance <= apex_m:
                span = max(apex_m - entry_m, 1e-9)
                steer_wobble += amplitude * (1.0 - (distance - entry_m) / span)

        x, z = outline.position_at(distance)
        yaw = outline.heading_at(distance)
        if line_offset:
            x += line_offset * math.sin(yaw)
            z -= line_offset * math.cos(yaw)
        gear, rpm = gear_and_rpm(speed_kmh)
        steer = max(-1.0, min(1.0, -curvature * STEER_GAIN + steer_wobble))
        if abs(steer) < 0.002:
            steer = 0.0

        samples.append(
            Sample(
                t_ms=int(round(time_s * 1000.0)),
                dist_m=distance,
                speed_kmh=speed_kmh,
                throttle=throttle,
                brake=brake,
                steer=steer,
                gear=gear,
                rpm=rpm,
                x=x,
                z=z,
                yaw=yaw,
                lateral_g=speed_mps**2 * curvature / GRAVITY,
                longitudinal_g=acceleration / GRAVITY,
                line_offset_m=line_offset,
            )
        )
    return samples


def lap_time_ms(samples: list[Sample], rate_hz: float = DEFAULT_RATE_HZ) -> int:
    """샘플 목록으로 랩 시간(ms)을 구한다."""
    if not samples:
        raise ValueError("샘플이 비어 있습니다")
    return int(round(samples[-1].t_ms + 1000.0 / rate_hz))


def sector_split_distances(track: Track) -> tuple[float, float]:
    """1·2섹터 경계 거리. 데모는 랩 길이를 셋으로 나눈 값을 쓴다."""
    return (track.length_m / 3.0, track.length_m * 2.0 / 3.0)


def _time_at_distance(samples: list[Sample], distance_m: float, rate_hz: float) -> int:
    """샘플 사이를 선형 보간해 해당 랩 거리의 시각(ms)을 구한다."""
    previous = samples[0]
    for sample in samples[1:]:
        if sample.dist_m >= distance_m:
            span = sample.dist_m - previous.dist_m
            fraction = 0.0 if span <= 0 else (distance_m - previous.dist_m) / span
            return int(round(previous.t_ms + fraction * (sample.t_ms - previous.t_ms)))
        previous = sample
    return lap_time_ms(samples, rate_hz)


def sector_times_ms(
    samples: list[Sample],
    track: Track,
    rate_hz: float = DEFAULT_RATE_HZ,
) -> tuple[int, int, int]:
    """랩 샘플을 섹터 3개 시간(ms)으로 나눈다."""
    split1, split2 = sector_split_distances(track)
    time1 = _time_at_distance(samples, split1, rate_hz)
    time2 = _time_at_distance(samples, split2, rate_hz)
    total = lap_time_ms(samples, rate_hz)
    return (time1, time2 - time1, total - time2)
