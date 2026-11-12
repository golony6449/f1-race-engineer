"""서킷 윤곽 학습(작업 10, SCRUM-60).

데이터 파일이 없는 서킷은 내 첫 유효 랩의 위치 샘플로 윤곽을 만들고, 서킷 ID로
SQLite에 저장해 다음 랩·다음 방문에서 다시 쓴다(설계 6.9-2, 리뷰 포커스 5).
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from f1re import db
from f1re.demo_sender.driving import DriverProfile, generate_lap
from f1re.laps.traces import LapTrace, Sample
from f1re.tracks.learn import (
    LEARN_STEP_M,
    learn_outline,
    remember_outline,
    resolve_outline,
)
from f1re.tracks.model import load_outline, load_track
from f1re.tracks.outline import TrackOutline

# 한 변 100m인 정사각형. 마지막 점에서 첫 점으로 닫는 변까지 400m.
SQUARE = TrackOutline(
    [
        (0.0, 0.0, 0.0),
        (100.0, 100.0, 0.0),
        (200.0, 100.0, 100.0),
        (300.0, 0.0, 100.0),
    ]
)


def _square_trace(step_m: float = 1.0) -> LapTrace:
    """정사각형 중심선을 따라 1m 간격으로 달린 유효 랩."""
    samples: list[Sample] = []
    distance = step_m
    while distance < SQUARE.lap_length_m:
        x, z = SQUARE.position_at(distance)
        samples.append(
            Sample(
                dist_m=distance,
                t_ms=int(distance * 100),
                speed=200.0,
                throttle=1.0,
                brake=0.0,
                steer=0.1,
                gear=6,
                x=x,
                z=z,
            )
        )
        distance += step_m
    return LapTrace(
        car_index=0, lap=2, valid=True, invalid_reason=None, samples=samples
    )


def _demo_trace(lap: int = 2) -> LapTrace:
    """데모 스즈카 랩(작업 4)을 실제 랩 기록 모양으로 바꾼다."""
    track = load_track(13)
    outline = load_outline(13)
    assert track is not None and outline is not None
    samples = generate_lap(track, outline, DriverProfile(), lap, random.Random(0))
    return LapTrace(
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


def test_learn_outline_from_valid_lap() -> None:
    """유효 랩 위치로 랩 거리 순 윤곽을 만들고, 점은 실제 주행선 위에 있다."""
    trace = _square_trace()
    outline = learn_outline(trace)

    distances = [point[0] for point in outline.points]
    assert distances == sorted(distances)
    assert len(set(distances)) == len(distances)
    assert outline.points[0][0] == pytest.approx(trace.samples[0].dist_m)
    assert outline.points[-1][0] == pytest.approx(trace.samples[-1].dist_m)
    # 학습한 점은 원래 주행선(정사각형) 위에 있다.
    for dist, x, z in outline.points:
        expected_x, expected_z = SQUARE.position_at(dist)
        assert x == pytest.approx(expected_x, abs=1e-6)
        assert z == pytest.approx(expected_z, abs=1e-6)
    # 점 간격은 LEARN_STEP_M 이하이고, 닫는 변까지 포함한 길이는 랩 길이와 같다.
    gaps = [second - first for first, second in zip(distances, distances[1:])]
    assert all(gap <= LEARN_STEP_M + 1e-6 for gap in gaps)
    assert outline.lap_length_m == pytest.approx(SQUARE.lap_length_m, rel=0.01)


def test_learn_outline_from_demo_lap_matches_track_centerline() -> None:
    """데모 스즈카 랩으로 배운 윤곽이 공식 중심선과 크게 다르지 않다."""
    trace = _demo_trace()
    learned = learn_outline(trace)
    official = load_outline(13)
    assert official is not None

    assert learned.lap_length_m == pytest.approx(official.lap_length_m, rel=0.01)
    for dist, x, z in learned.points:
        expected_x, expected_z = official.position_at(dist)
        assert math.dist((x, z), (expected_x, expected_z)) < 2.0


def test_learn_outline_rejects_invalid_lap() -> None:
    """무효 랩(아웃랩·피트·플래시백 등)은 윤곽을 배우지 않는다(리뷰 포커스 4)."""
    trace = _square_trace()
    trace.valid = False
    trace.invalid_reason = "outlap"
    with pytest.raises(ValueError):
        learn_outline(trace)


@pytest.mark.parametrize(
    "samples",
    [
        [],
        [
            Sample(
                dist_m=0.0,
                t_ms=0,
                speed=100.0,
                throttle=1.0,
                brake=0.0,
                steer=0.0,
                gear=3,
                x=0.0,
                z=0.0,
            )
        ],
        # 위치가 유한하지 않은 샘플만 있는 랩.
        [
            Sample(
                dist_m=float(index),
                t_ms=index,
                speed=100.0,
                throttle=1.0,
                brake=0.0,
                steer=0.0,
                gear=3,
                x=math.nan,
                z=0.0,
            )
            for index in range(10)
        ],
    ],
)
def test_learn_outline_rejects_too_few_positions(samples: list[Sample]) -> None:
    trace = LapTrace(
        car_index=0, lap=2, valid=True, invalid_reason=None, samples=samples
    )
    with pytest.raises(ValueError):
        learn_outline(trace)


def test_unknown_track_learns_outline_on_first_valid_lap(tmp_path: Path) -> None:
    """데이터 파일이 없는 서킷도 첫 유효 랩에서 윤곽을 배워 저장하고 다시 쓴다."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        unknown_track_id = 99
        assert db.load_track_outline(conn, unknown_track_id) is None
        assert resolve_outline(unknown_track_id, conn) == (None, "none")

        trace = _square_trace()
        learned = remember_outline(conn, unknown_track_id, trace, learned_at_ns=123)
        assert learned is not None

        stored = db.load_track_outline(conn, unknown_track_id)
        assert stored is not None
        for got, want in zip(stored.points, learned.points, strict=True):
            assert got == pytest.approx(want)

        resolved, source = resolve_outline(unknown_track_id, conn)
        assert source == "learned"
        assert resolved is not None
        for got, want in zip(resolved.points, learned.points, strict=True):
            assert got == pytest.approx(want)

        # 두 번째 유효 랩은 저장된 윤곽을 다시 배우지 않고 그대로 쓴다.
        again = remember_outline(conn, unknown_track_id, trace)
        assert again is not None
        for got, want in zip(again.points, learned.points, strict=True):
            assert got == pytest.approx(want)
    finally:
        conn.close()


def test_remember_outline_ignores_unlearnable_lap(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        trace = _square_trace()
        trace.valid = False
        trace.invalid_reason = "pit"
        assert remember_outline(conn, 98, trace) is None
        assert db.load_track_outline(conn, 98) is None
    finally:
        conn.close()


def test_resolve_outline_prefers_packaged_track_data(tmp_path: Path) -> None:
    """스즈카·모나코는 데이터 파일의 중심선을 먼저 쓴다."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        resolved, source = resolve_outline(13, conn)
        official = load_outline(13)
        assert source == "data"
        assert resolved is not None and official is not None
        assert resolved.points == official.points

        assert resolve_outline(None, conn) == (None, "none")
    finally:
        conn.close()
