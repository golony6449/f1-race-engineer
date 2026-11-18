"""서킷 데이터 로드: 공식 번호·길이·구간과 중심선 곡률 정합."""

from __future__ import annotations

import dataclasses
import math
from importlib import resources

import pytest

from f1re.tracks.model import TrackDataError, load_outline, load_track
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


def test_lap_distance_offset_defaults_and_suzuka_calibration() -> None:
    """스즈카만 실제 게임 lap_distance 보정값(130m)을 갖고, 모나코는 0이다."""
    suzuka = load_track(13)
    monaco = load_track(5)

    assert suzuka is not None
    assert monaco is not None
    assert suzuka.lap_distance_offset_m == pytest.approx(130.0)
    assert monaco.lap_distance_offset_m == 0.0


def test_in_lap_distance_frame_shifts_corner_drs_and_spot_labels() -> None:
    """게임 프레임 사본은 코너·DRS·추월 창을 130m 옮기고 오프셋을 지운다."""
    suzuka = load_track(13)
    assert suzuka is not None

    shifted = suzuka.in_lap_distance_frame()

    assert shifted is not suzuka
    assert shifted.lap_distance_offset_m == 0.0
    first = shifted.corners[0]
    assert first.entry_m == pytest.approx(640.0)
    assert first.apex_m == pytest.approx(710.19)
    assert first.exit_m == pytest.approx(760.0)
    assert shifted.corners[-1].exit_m == pytest.approx(5750.0)
    zone = shifted.drs_zones[0]
    assert zone.activation_start_m == pytest.approx(5750.0)
    assert zone.activation_end_m == pytest.approx(640.0)
    assert zone.activation_wraps
    spots = {spot.name: spot for spot in shifted.overtaking_spots}
    # 중심선 5,800m → 게임 5,930m는 랩 길이(5,807m)를 감아 123m가 된다.
    window = spots["1번 코너"].entry_window_m
    assert window is not None
    assert window[0] == pytest.approx(123.0)
    assert window[1] == pytest.approx(560.0)
    kasio = spots["카시오 시케인"].entry_window_m
    assert kasio is not None
    assert kasio[0] == pytest.approx(5220.0)
    assert kasio[1] == pytest.approx(5270.0)
    # 원본 데이터는 중심선 프레임 그대로다.
    assert suzuka.corners[0].entry_m == 510.0
    assert suzuka.drs_zones[0].activation_start_m == 5620.0
    assert suzuka.lap_distance_offset_m == pytest.approx(130.0)
    # 이미 게임 프레임인 사본은 다시 옮겨도 그대로다.
    assert shifted.in_lap_distance_frame() is shifted


def test_corner_at_applies_lap_distance_offset() -> None:
    """corner_at(lap_distance=True)은 게임 랩 거리를 중심선 거리로 바꿔 판정한다."""
    suzuka = load_track(13)
    assert suzuka is not None

    corner = suzuka.corner_at(640.0, lap_distance=True)
    assert corner is not None and corner.number == 1
    assert suzuka.corner_at(639.9, lap_distance=True) is None
    # 2026-11-15 실녹화(recording-6) 구간: 게임 3,013m는 헤어핀(11번) 탈출
    # 직후(~2,883m)다. 보정하지 않으면 200R(12번) 구간으로 잘못 본다.
    assert suzuka.track_distance(3013.0) == pytest.approx(2883.0)
    assert suzuka.corner_at(3013.0, lap_distance=True) is None
    assert suzuka.corner_at(3013.0) is not None
    assert suzuka.corner_at(3013.0).number == 12


def test_in_lap_distance_frame_rejects_corner_crossing_lap_end() -> None:
    """코너가 랩 끝을 넘는 트랙은 게임 프레임으로 옮길 수 없다."""
    suzuka = load_track(13)
    assert suzuka is not None
    corners = [
        dataclasses.replace(corner, exit_m=corner.exit_m + 100.0)
        if corner.number == 18
        else corner
        for corner in suzuka.corners
    ]
    too_wide = dataclasses.replace(suzuka, corners=corners)

    with pytest.raises(TrackDataError):
        too_wide.in_lap_distance_frame()


def test_monaco_stays_in_centerline_frame() -> None:
    monaco = load_track(5)
    assert monaco is not None

    assert monaco.in_lap_distance_frame() is monaco
    assert monaco.track_distance(123.0) == pytest.approx(123.0)


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
