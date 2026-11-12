"""서킷 데이터 로드: 공식 번호·길이·구간과 중심선 곡률 정합."""

from __future__ import annotations

import math
from importlib import resources

import pytest

from f1re.tracks.model import load_outline, load_track
from f1re.tracks.outline import TrackOutline

# (트랙 ID, 이름, 공식 길이 m, 코너 수)
TRACKS = [
    (13, "스즈카 서킷", 5807.0, 18),
    (5, "모나코 서킷", 3337.0, 19),
]

# 코너 구간 안에서 heading이 이만큼은 돌아야 한다(가장 완만한 모나코 9번도 15도).
MIN_CORNER_TURN_DEG = 10.0
# 곧게 편 직선 구간에서 heading 변화 허용치.
MAX_STRAIGHT_TURN_DEG = 0.5

# (트랙 ID, 긴 직선 구간 [(시작 m, 끝 m)]). 끝이 시작보다 작으면 랩을 넘어간다.
STRAIGHTS = {
    13: [(5640.0, 490.0), (3990.0, 4790.0)],
    5: [(3080.0, 180.0)],
}


def _heading_delta_deg(outline: TrackOutline, start_m: float, end_m: float) -> float:
    delta = outline.heading_at(end_m) - outline.heading_at(start_m)
    wrapped = math.atan2(math.sin(delta), math.cos(delta))
    return math.degrees(wrapped)


@pytest.mark.parametrize(
    ("track_id", "name", "official_length_m", "corner_count"), TRACKS
)
def test_load_suzuka_and_monaco(
    track_id: int, name: str, official_length_m: float, corner_count: int
) -> None:
    track = load_track(track_id)

    assert track is not None
    assert track.track_id == track_id
    assert track.name == name
    # 공식 서킷 길이 ±1%
    assert track.length_m == pytest.approx(official_length_m, rel=0.01)
    assert len(track.corners) == corner_count
    assert [corner.number for corner in track.corners] == list(
        range(1, corner_count + 1)
    )
    assert all(corner.official for corner in track.corners)

    previous_exit = 0.0
    for corner in track.corners:
        assert 0.0 <= corner.entry_m < corner.apex_m < corner.exit_m <= track.length_m
        # 코너 구간은 서로 겹치지 않고 번호 순서대로다.
        assert corner.entry_m >= previous_exit
        previous_exit = corner.exit_m


@pytest.mark.parametrize("track_id", [0, -1, 999, 2025])
def test_load_unknown_track_returns_none(track_id: int) -> None:
    assert load_track(track_id) is None
    assert load_outline(track_id) is None


def test_data_files_are_packaged() -> None:
    names = {entry.name for entry in resources.files("f1re.tracks.data").iterdir()}
    assert {"suzuka.json", "monaco.json"} <= names


@pytest.mark.parametrize(("track_id", "_name", "official_length_m", "_corners"), TRACKS)
def test_centerline_spacing_closure_and_length(
    track_id: int, _name: str, official_length_m: float, _corners: int
) -> None:
    track = load_track(track_id)
    outline = load_outline(track_id)

    assert track is not None
    assert outline is not None
    assert outline.points[0][0] == 0.0

    gaps = [
        second[0] - first[0]
        for first, second in zip(outline.points, outline.points[1:], strict=False)
    ]
    # 점 간격은 10m 이하다.
    assert all(0.0 < gap <= 10.0 for gap in gaps)
    # 기록한 거리는 실제 점 사이 직선 거리와 같다(구간 안을 선형 보간하므로).
    for first, second in zip(outline.points, outline.points[1:], strict=False):
        assert math.dist(first[1:], second[1:]) == pytest.approx(
            second[0] - first[0], abs=0.02
        )
    # 마지막 점에서 첫 점으로 닫는 거리도 점 간격 수준이다.
    closing = math.dist(outline.points[-1][1:], outline.points[0][1:])
    assert 0.0 < closing <= 10.0
    # 닫는 변까지 포함한 총 길이는 공식 길이 ±1%다.
    assert outline.lap_length_m == pytest.approx(official_length_m, rel=0.01)
    assert outline.lap_length_m == pytest.approx(track.length_m, rel=0.01)


@pytest.mark.parametrize(("track_id", "_name", "_length", "_corners"), TRACKS)
def test_corner_intervals_have_curvature_and_straights_do_not(
    track_id: int, _name: str, _length: float, _corners: int
) -> None:
    track = load_track(track_id)
    outline = load_outline(track_id)
    assert track is not None
    assert outline is not None

    for corner in track.corners:
        turn = abs(_heading_delta_deg(outline, corner.entry_m, corner.exit_m))
        assert turn >= MIN_CORNER_TURN_DEG, (
            f"{track.name} {corner.number}번 코너 방향 변화가 {turn:.1f}도뿐입니다"
        )

    for start_m, end_m in STRAIGHTS[track_id]:
        turn = abs(_heading_delta_deg(outline, start_m, end_m))
        assert turn <= MAX_STRAIGHT_TURN_DEG, (
            f"{track.name} 직선 {start_m}~{end_m}m 방향 변화가 {turn:.1f}도입니다"
        )
