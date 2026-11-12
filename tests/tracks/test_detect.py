"""코너 자동 탐지(작업 14, SCRUM-64).

공식 데이터 파일이 없는 서킷에서 기준 랩의 감속·조향 구간으로 코너를 찾는다.
데모 스즈카 랩에서 데이터 파일 코너 수와 크게 다르지 않고, 각 정점이 실제
코너 구간 안에 있어야 한다(리뷰 포커스 5).
"""

from __future__ import annotations

import random

import pytest

from f1re.demo_sender.driving import DriverProfile, generate_lap
from f1re.laps.traces import LapTrace, Sample
from f1re.tracks.model import Track, detect_corners, load_outline, load_track

SUZUKA_ID = 13


def _trace_from_demo(lap: int = 2) -> tuple[Track, LapTrace]:
    track = load_track(SUZUKA_ID)
    outline = load_outline(SUZUKA_ID)
    assert track is not None and outline is not None
    samples = generate_lap(track, outline, DriverProfile(), lap, random.Random(0))
    trace = LapTrace(
        car_index=0,
        lap=lap,
        valid=True,
        invalid_reason=None,
        samples=[
            Sample(
                dist_m=sample.dist_m,
                t_ms=sample.t_ms,
                speed=sample.speed_kmh,
                throttle=sample.throttle,
                brake=sample.brake,
                steer=sample.steer,
                gear=sample.gear,
                x=sample.x,
                z=sample.z,
            )
            for sample in samples
        ],
    )
    return track, trace


def _synthetic_trace(
    *,
    speed_kmh: list[float],
    steer: list[float],
    step_m: float = 5.0,
    lap_length_m: float = 1000.0,
) -> LapTrace:
    assert len(speed_kmh) == len(steer)
    samples = [
        Sample(
            dist_m=index * step_m,
            t_ms=index * 100,
            speed=speed,
            throttle=0.5,
            brake=0.0,
            steer=steer_value,
            gear=5,
            x=float(index),
            z=0.0,
        )
        for index, (speed, steer_value) in enumerate(zip(speed_kmh, steer))
    ]
    assert samples[-1].dist_m < lap_length_m
    return LapTrace(
        car_index=0, lap=1, valid=True, invalid_reason=None, samples=samples
    )


@pytest.mark.parametrize("lap", [1, 2, 3])
def test_detect_corners_on_demo_suzuka(lap: int) -> None:
    """데모 스즈카 랩에서 데이터 파일 코너 수 ±20%로 찾고, 정점은 실제 구간 안에 있다."""
    track, trace = _trace_from_demo(lap)
    corners = detect_corners(trace, track.length_m)

    official_count = len(track.corners)
    assert official_count * 0.8 <= len(corners) <= official_count * 1.2
    assert [corner.number for corner in corners] == list(
        range(1, len(corners) + 1)
    )
    assert all(corner.official is False for corner in corners)

    previous_exit = 0.0
    for corner in corners:
        assert corner.entry_m < corner.apex_m < corner.exit_m
        assert corner.exit_m <= track.length_m
        assert corner.entry_m >= previous_exit
        previous_exit = corner.exit_m
        # 탐지한 정점은 공식 코너 구간 안이어야 한다("N번째 코너"가 실제 코너여야 함).
        assert any(
            official.entry_m <= corner.apex_m <= official.exit_m
            for official in track.corners
        ), f"{corner.number}번째 코너 정점 {corner.apex_m:.1f}m가 공식 구간 밖"


def test_detect_corners_is_deterministic() -> None:
    track, trace = _trace_from_demo(2)
    first = detect_corners(trace, track.length_m)
    second = detect_corners(trace, track.length_m)
    assert first == second


def test_detect_corners_on_synthetic_unknown_track() -> None:
    """코너 데이터가 없는 서킷도 감속·조향 구간으로 코너를 찾는다."""
    # 1,000m 랩에 두 코너(250m, 650m 부근)가 있는 단순 프로필.
    speeds: list[float] = []
    steer_values: list[float] = []
    for distance in range(0, 1000, 5):
        speed = 300.0
        steer = 0.0
        for center in (250.0, 650.0):
            offset = abs(distance - center)
            if offset < 60.0:
                speed = min(speed, 80.0 + offset * 2.0)
            if offset < 40.0:
                steer = max(steer, 0.5 * (1.0 - offset / 40.0))
        speeds.append(speed)
        steer_values.append(steer)
    trace = _synthetic_trace(speed_kmh=speeds, steer=steer_values)

    corners = detect_corners(trace, 1000.0)

    assert len(corners) == 2
    assert all(corner.official is False for corner in corners)
    assert [corner.number for corner in corners] == [1, 2]
    assert 190.0 <= corners[0].apex_m <= 310.0
    assert 590.0 <= corners[1].apex_m <= 710.0
    assert corners[0].exit_m <= corners[1].entry_m


def test_detect_corners_falls_back_to_speed_drops_without_steering() -> None:
    """조향 값이 비어 있으면 감속 구간으로 코너를 찾는다."""
    speeds: list[float] = []
    for distance in range(0, 1000, 5):
        if abs(distance - 300.0) < 40.0:
            speeds.append(90.0)
        elif abs(distance - 700.0) < 40.0:
            speeds.append(100.0)
        else:
            speeds.append(300.0)
    trace = _synthetic_trace(speed_kmh=speeds, steer=[0.0] * len(speeds))

    corners = detect_corners(trace, 1000.0)

    assert len(corners) == 2
    assert corners[0].apex_m == pytest.approx(300.0, abs=45.0)
    assert corners[1].apex_m == pytest.approx(700.0, abs=45.0)


def test_detect_corners_empty_trace_returns_empty() -> None:
    trace = LapTrace(car_index=0, lap=1, valid=False, invalid_reason="outlap", samples=[])
    assert detect_corners(trace, 5807.0) == []
