"""서킷 데이터: 공식 코너 구간, 중심선 윤곽, 추월·DRS 정보, 코너별 KB."""

from f1re.tracks.kb import TrackKB, load_corner_kb, load_track_kb
from f1re.tracks.model import (
    OVERTAKING_DIFFICULTIES,
    Corner,
    DrsZone,
    OvertakingSpot,
    Track,
    TrackDataError,
    detect_corners,
    load_outline,
    load_track,
)
from f1re.tracks.outline import TrackOutline

__all__ = [
    "OVERTAKING_DIFFICULTIES",
    "Corner",
    "DrsZone",
    "OvertakingSpot",
    "Track",
    "TrackDataError",
    "TrackKB",
    "TrackOutline",
    "detect_corners",
    "load_corner_kb",
    "load_outline",
    "load_track",
    "load_track_kb",
]
