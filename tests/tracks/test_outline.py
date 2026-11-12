"""중심선 보간과 랩 감김."""

from __future__ import annotations

import math

import pytest

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


def test_lap_length_includes_closing_edge() -> None:
    assert SQUARE.lap_length_m == pytest.approx(400.0)


def test_interpolates_between_points() -> None:
    assert SQUARE.position_at(0.0) == pytest.approx((0.0, 0.0))
    assert SQUARE.position_at(50.0) == pytest.approx((50.0, 0.0))
    assert SQUARE.position_at(150.0) == pytest.approx((100.0, 50.0))
    assert SQUARE.position_at(250.0) == pytest.approx((50.0, 100.0))
    assert SQUARE.position_at(350.0) == pytest.approx((0.0, 50.0))


def test_distance_wraps_around_lap() -> None:
    assert SQUARE.position_at(400.0) == pytest.approx((0.0, 0.0))
    assert SQUARE.position_at(450.0) == pytest.approx((50.0, 0.0))
    assert SQUARE.position_at(800.0) == pytest.approx((0.0, 0.0))
    assert SQUARE.position_at(-50.0) == pytest.approx((0.0, 50.0))


def test_heading_at_segments_and_closing_edge() -> None:
    assert SQUARE.heading_at(50.0) == pytest.approx(0.0)
    assert SQUARE.heading_at(150.0) == pytest.approx(math.pi / 2)
    assert SQUARE.heading_at(250.0) == pytest.approx(math.pi)
    assert SQUARE.heading_at(350.0) == pytest.approx(-math.pi / 2)
    # 마지막 점을 지나면 첫 점으로 닫는 변을 따라간다.
    assert SQUARE.heading_at(399.0) == pytest.approx(-math.pi / 2)
    assert SQUARE.heading_at(401.0) == pytest.approx(0.0)


@pytest.mark.parametrize(
    "points",
    [
        [],
        [(0.0, 0.0, 0.0)],
        [(0.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [(0.0, 0.0, 0.0), (10.0, 1.0, 0.0), (5.0, 2.0, 0.0)],
        [(0.0, 0.0, 0.0), (10.0, math.nan, 0.0)],
    ],
)
def test_rejects_bad_points(points: list[tuple[float, float, float]]) -> None:
    with pytest.raises(ValueError):
        TrackOutline(points)


def test_rejects_point_without_three_values() -> None:
    with pytest.raises(ValueError):
        TrackOutline([(0.0, 0.0), (10.0, 0.0, 0.0)])  # type: ignore[list-item]


def test_rejects_non_finite_distance_query() -> None:
    with pytest.raises(ValueError):
        SQUARE.position_at(math.inf)
    with pytest.raises(ValueError):
        SQUARE.heading_at(math.nan)
