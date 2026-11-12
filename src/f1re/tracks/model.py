"""서킷 데이터 파일을 읽어 `Track`·`Corner`로 만든다.

데이터 파일은 `f1re/tracks/data/*.json`에 있고 패키지에 포함된다. 모르는 트랙 ID는
예외 없이 `None`을 돌려준다(리뷰 포커스 5: 코너 데이터 없는 서킷에서도 동작).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

from f1re.tracks.outline import TrackOutline

_DATA_PACKAGE = "f1re.tracks.data"


class TrackDataError(ValueError):
    """서킷 데이터 파일이 없거나 형식이 잘못됐을 때."""


@dataclass(frozen=True)
class Corner:
    """공식 코너 하나. 거리 단위는 m, 랩 시작(결승선) 기준이다."""

    number: int
    official: bool
    entry_m: float
    apex_m: float
    exit_m: float


@dataclass(frozen=True)
class Track:
    """서킷 하나의 공식 코너 목록."""

    track_id: int
    name: str
    length_m: float
    corners: list[Corner]


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
            corner = Corner(
                number=int(item["number"]),
                official=True,
                entry_m=float(item["entry_m"]),
                apex_m=float(item["apex_m"]),
                exit_m=float(item["exit_m"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TrackDataError(f"{filename}: 코너 형식이 잘못됐습니다") from exc
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

    return Track(track_id=track_id, name=name, length_m=length_m, corners=corners)
