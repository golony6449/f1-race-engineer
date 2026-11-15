"""서킷 데이터 파일을 읽어 `Track`·`Corner`·`DrsZone`·`OvertakingSpot`으로 만든다.

데이터 파일은 `f1re/tracks/data/*.json`에 있고 패키지에 포함된다. 모르는 트랙 ID는
예외 없이 `None`을 돌려준다(리뷰 포커스 5: 코너 데이터 없는 서킷에서도 동작).
코너 이름·DRS 구간·추월 지점은 선택 필드다. 없는 트랙 파일도 그대로 읽는다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import TYPE_CHECKING, Any

from f1re.tracks.outline import TrackOutline

if TYPE_CHECKING:
    from f1re.laps.traces import LapTrace, Sample

_DATA_PACKAGE = "f1re.tracks.data"

# 자동 코너 탐지 기본값. 데모 스즈카 랩(작업 4)으로 맞춘 값이다.
# 조향이 이 값 이상이면 코너로 보고, 잡음은 이동 평균으로 지운다.
DETECT_STEP_M = 2.0
DETECT_STEER_ENTER = 0.04
DETECT_MIN_STEER_PEAK = 0.08
DETECT_SMOOTH_STEER_M = 6.0
DETECT_SMOOTH_SPEED_M = 16.0
# 이 거리 안의 조향 봉우리는 한 코너(시케인·연속 코너)로 합친다.
DETECT_MIN_PEAK_SPACING_M = 30.0
# 두 봉우리 사이 골이 이 비율보다 깊으면 다른 코너로 나눈다.
DETECT_VALLEY_SPLIT_RATIO = 0.5
# 코너 진입은 브레이크 구간까지 포함하고, 탈출은 조금만 여유를 준다.
DETECT_ENTRY_MAX_PAD_M = 80.0
DETECT_EXIT_PAD_M = 40.0
# 조향이 약해도 이만큼 감속하면 코너로 본다(자동 탐지 보조 조건).
DETECT_SPEED_DROP_KMH = 20.0
DETECT_SPEED_WINDOW_M = 150.0


class TrackDataError(ValueError):
    """서킷 데이터 파일이 없거나 형식이 잘못됐을 때."""


#: `OvertakingSpot.difficulty`에 쓸 수 있는 값. 조사 문서의 하/중/상/최상을
#: `low`/`medium`/`high`/`very_high`로 옮겼다. `하~중`처럼 두 단계에 걸친 표현은
#: 더 쉬운 쪽(낮은 난이도)으로 적는다.
OVERTAKING_DIFFICULTIES = ("low", "medium", "high", "very_high")


@dataclass(frozen=True)
class Corner:
    """공식 코너 하나. 거리 단위는 m, 랩 시작(결승선) 기준이다."""

    number: int
    official: bool
    entry_m: float
    apex_m: float
    exit_m: float
    name: str | None = None


@dataclass(frozen=True)
class DrsZone:
    """DRS 한 구간.

    `detection_*`은 DRS 사용권을 얻는 감지 창, `activation_*`은 DRS를 켤 수 있는
    활성 창이다. 활성 창은 랩을 넘을 수 있어 `activation_end_m`이
    `activation_start_m`보다 작으면 결승선을 지나 이어지는 구간을 뜻한다
    (스즈카: 최종 코너 탈출 5,620 m → 결승선 → 1번 진입 510 m).
    """

    detection_start_m: float
    detection_end_m: float
    activation_start_m: float
    activation_end_m: float
    name: str | None = None

    @property
    def activation_wraps(self) -> bool:
        """활성 창이 결승선을 넘는지."""
        return self.activation_end_m < self.activation_start_m


@dataclass(frozen=True)
class OvertakingSpot:
    """추월·방어를 설계할 때 쓰는 지점 하나.

    `attack_corners`는 공격 대상 코너 번호, `prep_corners`는 그 전에 속도를 만드는
    준비 코너 번호다. `entry_window_m`은 이 지점의 공격·방어 무전을 내기 좋은 랩
    거리 창이며, 끝이 시작보다 작으면 결승선을 넘는다. `drs`는 이 지점의 공격이
    DRS 활성 구간과 직접 이어지는지를 뜻한다.
    """

    name: str
    attack_corners: tuple[int, ...]
    difficulty: str
    prep_corners: tuple[int, ...]
    attack_method: str
    defense_line: str
    drs: bool = False
    avoid_reason: str | None = None
    entry_window_m: tuple[float, float] | None = None


@dataclass(frozen=True)
class Track:
    """서킷 하나의 공식 코너 목록과 추월·DRS 정보.

    `drs_zones`·`overtaking_spots`는 선택 데이터다. JSON에 없으면 빈 튜플이며,
    기존처럼 `Track(track_id, name, length_m, corners)`로도 만들 수 있다.
    """

    track_id: int
    name: str
    length_m: float
    corners: list[Corner]
    drs_zones: tuple[DrsZone, ...] = ()
    overtaking_spots: tuple[OvertakingSpot, ...] = ()


def load_track(track_id: int) -> Track | None:
    """트랙 ID로 서킷을 찾는다. 데이터 파일이 없는 ID면 `None`."""
    found = _load_raw(track_id)
    if found is None:
        return None
    raw, filename = found
    return _build_track(raw, filename)


def load_outline(track_id: int) -> TrackOutline | None:
    """트랙 ID로 중심선 윤곽을 찾는다. 데이터 파일이 없는 ID면 `None`."""
    found = _load_raw(track_id)
    if found is None:
        return None
    raw, filename = found
    centerline = raw.get("centerline")
    if not isinstance(centerline, list):
        raise TrackDataError(f"{filename}: centerline이 없습니다")
    try:
        points = [(float(dist), float(x), float(z)) for dist, x, z in centerline]
    except (TypeError, ValueError) as exc:
        raise TrackDataError(f"{filename}: centerline 형식이 잘못됐습니다") from exc
    return TrackOutline(points)


def _load_raw(track_id: int) -> tuple[dict[str, Any], str] | None:
    found: tuple[dict[str, Any], str] | None = None
    for entry in _data_files():
        raw = _read_json(entry)
        if raw.get("track_id") != track_id:
            continue
        if found is not None:
            raise TrackDataError(
                f"트랙 ID {track_id}가 {found[1]}와 {entry.name}에 중복으로 있습니다"
            )
        found = (raw, entry.name)
    return found


def _data_files() -> list[Traversable]:
    try:
        root = resources.files(_DATA_PACKAGE)
        entries = sorted(
            (entry for entry in root.iterdir() if entry.name.endswith(".json")),
            key=lambda entry: entry.name,
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise TrackDataError(f"서킷 데이터 폴더를 찾지 못했습니다: {_DATA_PACKAGE}") from exc
    if not entries:
        raise TrackDataError("서킷 데이터 파일이 없습니다")
    return entries


def _read_json(entry: Traversable) -> dict[str, Any]:
    try:
        raw = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrackDataError(f"{entry.name}: JSON을 읽지 못했습니다") from exc
    if not isinstance(raw, dict):
        raise TrackDataError(f"{entry.name}: 최상위 값이 객체가 아닙니다")
    return raw


def _build_track(raw: dict[str, Any], filename: str) -> Track:
    try:
        track_id = int(raw["track_id"])
        name = str(raw["name"])
        length_m = float(raw["length_m"])
        corners_raw = raw["corners"]
    except (KeyError, TypeError, ValueError) as exc:
        raise TrackDataError(f"{filename}: 필수 값이 없거나 형식이 잘못됐습니다") from exc
    if not name:
        raise TrackDataError(f"{filename}: name이 비어 있습니다")
    if length_m <= 0:
        raise TrackDataError(f"{filename}: length_m은 0보다 커야 합니다")
    if not isinstance(corners_raw, list) or not corners_raw:
        raise TrackDataError(f"{filename}: corners가 비어 있습니다")

    corners: list[Corner] = []
    previous_exit = 0.0
    for item in corners_raw:
        try:
            number = int(item["number"])
            entry_m = float(item["entry_m"])
            apex_m = float(item["apex_m"])
            exit_m = float(item["exit_m"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TrackDataError(f"{filename}: 코너 형식이 잘못됐습니다") from exc
        corner_name = _optional_text(item.get("name"), f"{filename}: {number}번 코너 name")
        corner = Corner(
            number=number,
            official=True,
            entry_m=entry_m,
            apex_m=apex_m,
            exit_m=exit_m,
            name=corner_name,
        )
        if corner.number < 1:
            raise TrackDataError(f"{filename}: 코너 번호는 1 이상이어야 합니다")
        if not (0.0 <= corner.entry_m < corner.apex_m < corner.exit_m <= length_m):
            raise TrackDataError(
                f"{filename}: {corner.number}번 코너 구간이 잘못됐습니다"
            )
        if corner.entry_m < previous_exit:
            raise TrackDataError(f"{filename}: {corner.number}번 코너가 앞 코너와 겹칩니다")
        corners.append(corner)
        previous_exit = corner.exit_m

    corner_numbers = {corner.number for corner in corners}
    drs_zones = _build_drs_zones(raw.get("drs_zones"), length_m, filename)
    overtaking_spots = _build_overtaking_spots(
        raw.get("overtaking_spots"), corner_numbers, length_m, filename
    )

    return Track(
        track_id=track_id,
        name=name,
        length_m=length_m,
        corners=corners,
        drs_zones=drs_zones,
        overtaking_spots=overtaking_spots,
    )


def _optional_text(value: Any, label: str) -> str | None:
    """선택 문자열 필드를 읽는다. `None`이면 `None`, 빈 문자열·다른 타입은 오류."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TrackDataError(f"{label}이 비어 있거나 문자열이 아닙니다")
    return value.strip()


def _required_text(value: Any, label: str) -> str:
    """필수 문자열 필드를 읽는다. 빈 문자열·다른 타입은 오류."""
    if not isinstance(value, str) or not value.strip():
        raise TrackDataError(f"{label}이 비어 있거나 문자열이 아닙니다")
    return value.strip()


def _number(value: Any, label: str) -> float:
    """숫자 필드를 읽는다. `true`/`false`와 숫자 문자열은 받지 않는다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrackDataError(f"{label}이 숫자가 아닙니다")
    number = float(value)
    if not math.isfinite(number):
        raise TrackDataError(f"{label}이 유한한 숫자가 아닙니다")
    return number


def _check_distance_range(
    start_m: float, end_m: float, length_m: float, label: str, *, wrap: bool
) -> None:
    """랩 거리 구간이 0~`length_m` 안에 있는지 확인한다.

    `wrap=True`면 끝이 시작보다 작은(결승선을 넘는) 구간을 허용한다.
    """
    for value in (start_m, end_m):
        if not math.isfinite(value) or not (0.0 <= value <= length_m):
            raise TrackDataError(
                f"{label} 거리 {value}가 0~{length_m:.1f}m 범위를 벗어났습니다"
            )
    if start_m == end_m:
        raise TrackDataError(f"{label}의 시작과 끝이 같습니다")
    if not wrap and start_m > end_m:
        raise TrackDataError(f"{label}의 시작이 끝보다 큽니다")


def _build_drs_zones(
    raw: Any, length_m: float, filename: str
) -> tuple[DrsZone, ...]:
    """`drs_zones` 선택 필드를 읽는다. 없으면 빈 튜플."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TrackDataError(f"{filename}: drs_zones가 목록이 아닙니다")

    zones: list[DrsZone] = []
    for index, item in enumerate(raw, start=1):
        label = f"{filename}: {index}번째 DRS 구간"
        if not isinstance(item, dict):
            raise TrackDataError(f"{label}이 객체가 아닙니다")
        try:
            detection_start_raw = item["detection_start_m"]
            detection_end_raw = item["detection_end_m"]
            activation_start_raw = item["activation_start_m"]
            activation_end_raw = item["activation_end_m"]
        except (KeyError, TypeError) as exc:
            raise TrackDataError(f"{label}에 필요한 거리 값이 없습니다") from exc
        detection_start_m = _number(detection_start_raw, f"{label} detection_start_m")
        detection_end_m = _number(detection_end_raw, f"{label} detection_end_m")
        activation_start_m = _number(
            activation_start_raw, f"{label} activation_start_m"
        )
        activation_end_m = _number(activation_end_raw, f"{label} activation_end_m")
        _check_distance_range(
            detection_start_m, detection_end_m, length_m, f"{label} 감지 구간", wrap=False
        )
        _check_distance_range(
            activation_start_m,
            activation_end_m,
            length_m,
            f"{label} 활성 구간",
            wrap=True,
        )
        zones.append(
            DrsZone(
                detection_start_m=detection_start_m,
                detection_end_m=detection_end_m,
                activation_start_m=activation_start_m,
                activation_end_m=activation_end_m,
                name=_optional_text(item.get("name"), f"{label} name"),
            )
        )
    return tuple(zones)


def _build_overtaking_spots(
    raw: Any, corner_numbers: set[int], length_m: float, filename: str
) -> tuple[OvertakingSpot, ...]:
    """`overtaking_spots` 선택 필드를 읽는다. 없으면 빈 튜플."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TrackDataError(f"{filename}: overtaking_spots가 목록이 아닙니다")

    spots: list[OvertakingSpot] = []
    for index, item in enumerate(raw, start=1):
        label = f"{filename}: {index}번째 추월 지점"
        if not isinstance(item, dict):
            raise TrackDataError(f"{label}이 객체가 아닙니다")
        name = _required_text(item.get("name"), f"{label} name")
        difficulty = _required_text(item.get("difficulty"), f"{label} difficulty")
        if difficulty not in OVERTAKING_DIFFICULTIES:
            allowed = ", ".join(OVERTAKING_DIFFICULTIES)
            raise TrackDataError(
                f"{label} difficulty '{difficulty}'는 허용 값({allowed})이 아닙니다"
            )
        attack_corners = _corner_numbers(
            item,
            label,
            "attack_corners",
            corner_numbers,
            single_key="attack_corner",
        )
        prep_corners = _corner_numbers(item, label, "prep_corners", corner_numbers)
        drs = item.get("drs")
        if not isinstance(drs, bool):
            raise TrackDataError(f"{label} drs는 true/false여야 합니다")
        spots.append(
            OvertakingSpot(
                name=name,
                attack_corners=attack_corners,
                difficulty=difficulty,
                prep_corners=prep_corners,
                attack_method=_required_text(
                    item.get("attack_method"), f"{label} attack_method"
                ),
                defense_line=_required_text(
                    item.get("defense_line"), f"{label} defense_line"
                ),
                drs=drs,
                avoid_reason=_optional_text(
                    item.get("avoid_reason"), f"{label} avoid_reason"
                ),
                entry_window_m=_optional_window(
                    item.get("entry_window_m"), length_m, f"{label} entry_window_m"
                ),
            )
        )
    return tuple(spots)


def _corner_numbers(
    item: dict[str, Any],
    label: str,
    key: str,
    corner_numbers: set[int],
    *,
    single_key: str | None = None,
) -> tuple[int, ...]:
    """코너 번호 목록을 읽고 이 트랙의 코너인지 확인한다.

    `single_key`는 `attack_corner`처럼 번호 하나를 받는 다른 이름이다. 두 키가
    같이 있으면 어느 쪽을 쓸지 모호하므로 오류로 본다.
    """
    if single_key is not None and key in item and single_key in item:
        raise TrackDataError(f"{label}에 {key}와 {single_key}가 함께 있습니다")
    if key in item:
        raw = item[key]
        if not isinstance(raw, list):
            raise TrackDataError(f"{label} {key}가 목록이 아닙니다")
    elif single_key is not None and single_key in item:
        raw = [item[single_key]]
    else:
        raise TrackDataError(f"{label}에 {key}가 없습니다")

    numbers: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TrackDataError(f"{label} {key}에 정수가 아닌 값이 있습니다")
        if value not in corner_numbers:
            raise TrackDataError(f"{label} {key}의 {value}번 코너가 트랙에 없습니다")
        if value in numbers:
            raise TrackDataError(f"{label} {key}에 {value}번 코너가 중복됩니다")
        numbers.append(value)
    if not numbers:
        raise TrackDataError(f"{label} {key}가 비어 있습니다")
    return tuple(numbers)


def _optional_window(
    value: Any, length_m: float, label: str
) -> tuple[float, float] | None:
    """선택 거리 창 `[시작_m, 끝_m]`을 읽는다. 끝이 작으면 결승선을 넘는다."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise TrackDataError(f"{label}은 [시작_m, 끝_m] 두 값이어야 합니다")
    start_m = _number(value[0], f"{label} 시작")
    end_m = _number(value[1], f"{label} 끝")
    _check_distance_range(start_m, end_m, length_m, label, wrap=True)
    return (start_m, end_m)


# ---------------------------------------------------------------------------
# 데이터 파일이 없는 서킷의 코너 자동 탐지


def detect_corners(trace: LapTrace, track_length_m: float) -> list[Corner]:
    """기준 랩에서 감속·조향 구간을 찾아 코너를 만든다(공식 번호 아님).

    데이터 파일이 없는 서킷에서 쓰며, 모든 코너는 `official=False`이고 랩 순서대로
    1부터 번호를 붙인다(설계 6.2: "코너 3" 대신 "3번째 코너"로 말한다).
    조향이 뚜렷한 구간을 코너로 보고, 진입은 브레이크 구간까지 조금 넓힌다.
    조향 없이 감속만 있는 랩(조향 값이 비어 있는 텔레메트리)은 감속 구간으로
    대신 찾는다.
    """
    if track_length_m <= 0:
        return []
    samples = sorted(
        (
            sample
            for sample in trace.samples
            if math.isfinite(sample.dist_m)
            and math.isfinite(sample.speed)
            and math.isfinite(sample.steer)
        ),
        key=lambda sample: sample.dist_m,
    )
    if len(samples) < 10:
        return []

    grid = _resample_lap(samples)
    if len(grid) < 10:
        return []
    speed = _moving_average([sample.speed for sample in grid], DETECT_SMOOTH_SPEED_M)
    steer = _moving_average(
        [abs(sample.steer) for sample in grid], DETECT_SMOOTH_STEER_M
    )
    distance_of = [sample.dist_m for sample in grid]

    # 조향 봉우리를 코너 후보로 삼는다. 가까운 봉우리나 얕은 골로 이어진 봉우리는
    # 한 코너(시케인·연속 코너)로 묶는다.
    peaks = _steer_peaks(steer)
    clusters = _cluster_peaks(peaks, steer, distance_of)
    candidates: list[tuple[int, int, int]] = []
    for cluster in clusters:
        core_start = _core_extent(steer, cluster[0], -1)
        core_end = _core_extent(steer, cluster[-1], +1)
        if _span(distance_of, core_start, core_end) <= 0.0:
            continue
        apex_index = max(cluster, key=lambda index: steer[index])
        candidates.append((core_start, core_end, apex_index))

    # 조향으로 못 찾았으면 감속 구간을 코너로 본다(조향 값이 비어 있는 경우).
    if not candidates:
        for index in _local_min_speed_indices(speed):
            start = max(0, index - 1)
            end = min(len(grid) - 1, index + 1)
            candidates.append((start, end, index))

    if not candidates:
        return []

    corners: list[Corner] = []
    for order, (start, end, apex_index) in enumerate(candidates):
        if order > 0:
            # 앞 코너와의 경계는 두 정점 사이 조향이 가장 낮은 지점으로 나눈다.
            entry_m = distance_of[
                _valley_index(steer, candidates[order - 1][2], apex_index)
            ]
        else:
            # 첫 코너는 브레이크 구간까지 앞으로 넓힌다.
            entry_m = max(0.0, distance_of[start] - DETECT_ENTRY_MAX_PAD_M)
        if order + 1 < len(candidates):
            exit_m = distance_of[
                _valley_index(steer, apex_index, candidates[order + 1][2])
            ]
        else:
            exit_m = min(track_length_m, distance_of[end] + DETECT_EXIT_PAD_M)

        apex_m = distance_of[apex_index]
        # 진입·정점·탈출 순서를 반드시 지킨다.
        entry_m = min(entry_m, apex_m - 1.0)
        exit_m = max(exit_m, apex_m + 1.0)
        if entry_m < 0.0 or exit_m > track_length_m or exit_m <= entry_m:
            continue
        corners.append(
            Corner(
                number=order + 1,
                official=False,
                entry_m=entry_m,
                apex_m=apex_m,
                exit_m=exit_m,
            )
        )
    return corners


def _valley_index(steer: list[float], left: int, right: int) -> int:
    """두 봉우리 사이에서 조향이 가장 낮은 점(코너 경계)."""
    if right <= left:
        return left
    return min(range(left, right + 1), key=lambda index: steer[index])


def _resample_lap(samples: list[Sample]) -> list[_GridPoint]:
    """랩 전체를 `DETECT_STEP_M` 간격의 점으로 다시 샘플링한다(선형 보간)."""
    first = samples[0]
    last = samples[-1]
    if last.dist_m <= first.dist_m:
        return []
    grid: list[_GridPoint] = []
    count = int((last.dist_m - first.dist_m) / DETECT_STEP_M) + 1
    index = 0
    for step in range(count):
        distance = first.dist_m + step * DETECT_STEP_M
        while index + 1 < len(samples) and samples[index + 1].dist_m <= distance:
            index += 1
        current = samples[index]
        following = samples[min(index + 1, len(samples) - 1)]
        span = following.dist_m - current.dist_m
        fraction = 0.0 if span <= 0.0 else (distance - current.dist_m) / span
        grid.append(
            _GridPoint(
                dist_m=distance,
                speed=current.speed + fraction * (following.speed - current.speed),
                steer=current.steer + fraction * (following.steer - current.steer),
            )
        )
    return grid


@dataclass(frozen=True)
class _GridPoint:
    dist_m: float
    speed: float
    steer: float


def _moving_average(values: list[float], window_m: float) -> list[float]:
    """거리 기준 창(±window_m)의 이동 평균."""
    half = max(1, int(round(window_m / DETECT_STEP_M)))
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    count = len(values)
    smoothed: list[float] = []
    for index in range(count):
        start = max(0, index - half)
        end = min(count, index + half + 1)
        smoothed.append((prefix[end] - prefix[start]) / (end - start))
    return smoothed


def _steer_peaks(steer: list[float]) -> list[int]:
    """조향 크기의 국소 봉우리. `DETECT_MIN_STEER_PEAK` 이상만 남긴다."""
    count = len(steer)
    if count < 3:
        return []
    half = max(1, int(round(DETECT_MIN_PEAK_SPACING_M / 2.0 / DETECT_STEP_M)))
    peaks: list[int] = []
    for index in range(1, count - 1):
        value = steer[index]
        if value < DETECT_MIN_STEER_PEAK:
            continue
        if value < steer[index - 1] or value < steer[index + 1]:
            continue
        # 같은 봉우리 근처의 작은 요동은 버리고 가장 높은 점만 남긴다.
        start = max(0, index - half)
        end = min(count, index + half + 1)
        if value < max(steer[start:end]):
            continue
        if peaks and index - peaks[-1] <= 2:
            if value > steer[peaks[-1]]:
                peaks[-1] = index
            continue
        peaks.append(index)
    return peaks


def _cluster_peaks(
    peaks: list[int], steer: list[float], distance_of: list[float]
) -> list[list[int]]:
    """봉우리들을 코너 하나로 묶는다. 가까운 봉우리·얕은 골은 같은 코너다."""
    if not peaks:
        return []
    clusters: list[list[int]] = [[peaks[0]]]
    for index in peaks[1:]:
        previous = clusters[-1][-1]
        valley = min(steer[previous : index + 1])
        shallow = valley >= DETECT_VALLEY_SPLIT_RATIO * min(
            steer[previous], steer[index]
        )
        close = distance_of[index] - distance_of[previous] < DETECT_MIN_PEAK_SPACING_M
        if shallow or close:
            clusters[-1].append(index)
        else:
            clusters.append([index])
    return clusters


def _core_extent(steer: list[float], index: int, direction: int) -> int:
    """봉우리에서 `DETECT_STEER_ENTER` 아래로 떨어질 때까지 조향 구간을 넓힌다."""
    position = index
    while 0 <= position + direction < len(steer):
        if steer[position + direction] < DETECT_STEER_ENTER:
            break
        position += direction
    return position


def _span(distance_of: list[float], start: int, end: int) -> float:
    return distance_of[end] - distance_of[start]


def _local_min_speed_indices(speed: list[float]) -> list[int]:
    """감속 구간(국소 최저 속도)을 찾는다. 조향 값이 없을 때 쓰는 보조 탐지."""
    half = max(1, int(round(DETECT_SPEED_WINDOW_M / DETECT_STEP_M)))
    count = len(speed)
    minima: list[int] = []
    index = half
    while index < count - half:
        window = range(max(0, index - half), min(count, index + half + 1))
        lowest = min(window, key=lambda position: speed[position])
        drop = max(speed[position] for position in window) - speed[lowest]
        if lowest == index and drop >= DETECT_SPEED_DROP_KMH:
            minima.append(index)
            index += half
        else:
            index += 1
    return minima
