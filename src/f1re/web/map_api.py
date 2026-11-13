"""서킷 지도 API(설계 6.9-2, 계획 작업 10).

화면은 서버 API를 읽기만 한다. 이 모듈이 지도에 필요한 것을 모두 만든다.

- ``GET /api/track``: 트랙 윤곽(랩 거리 순 점), 코너 번호·정점 위치, 집중 코너
  목록 자리, 코너 지표 자리, 현재 깃발, 팀 색.
- ``TrackMap.car_positions``: ``/api/state``·``/ws/state``에 넣는 모든 차의
  위치. Motion 패킷이 없어도 랩 거리로 윤곽 위를 보간해 그린다.
- ``TrackMap.flag``: `StateStore`의 깃발 요약(SCRUM-80)으로 적기·SC·VSC를
  판정한다. 요약이 없으면 Session 패킷의 `safety_car_status`로 SC·VSC만
  판정한다(예전 스냅샷 호환).

집중 코너(작업 16)와 코너 지표(작업 15)는 아직 없다. API에는 빈 자리만 둔다.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from f1re.tracks.learn import resolve_outline
from f1re.tracks.model import Track, TrackDataError, load_track
from f1re.tracks.outline import TrackOutline

if TYPE_CHECKING:
    import sqlite3

    from f1re.state.store import StateStore

# 공식 팀 ID(0 메르세데스 … 9 자우버, 10 F1 제네릭)별 화면 색.
# 차 점은 팀 색으로 그린다(설계 6.9-2). 화면은 이 표만 쓴다.
TEAM_COLORS: dict[int, str] = {
    0: "#00d2be",  # 메르세데스
    1: "#dc0000",  # 페라리
    2: "#3671c6",  # 레드불
    3: "#005aff",  # 윌리엄스
    4: "#006f62",  # 애스턴 마틴
    5: "#2293d1",  # 알핀
    6: "#6692ff",  # RB
    7: "#b6babd",  # 하스
    8: "#ff8000",  # 맥라렌
    9: "#52e252",  # 자우버
    10: "#8b97a8",  # F1 제네릭
}

_TEAM_COLORS_JSON: dict[str, str] = {
    str(team_id): color for team_id, color in TEAM_COLORS.items()
}

# 학습 윤곽이 없을 때 DB를 다시 확인하는 주기(초). 첫 유효 랩에서 윤곽이
# 저장되면 세션을 다시 시작하지 않아도 지도가 나타나야 한다(설계 6.9-2).
_RECHECK_INTERVAL_S = 5.0


class TrackMap:
    """현재 세션의 서킷 윤곽·코너·차 위치를 만든다. 스레드 안전하다."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._track_id: int | None = None
        self._outline: TrackOutline | None = None
        self._source = "none"
        self._track: Track | None = None
        self._loaded = False
        self._checked_at_s = 0.0

    # ------------------------------------------------------------------
    # API

    def track_payload(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """`GET /api/track` 응답."""
        self._refresh(snapshot)
        with self._lock:
            track_id = self._track_id
            outline = self._outline
            source = self._source
            track = self._track

        corners: list[dict[str, Any]] = []
        if track is not None:
            for corner in track.corners:
                x: float | None = None
                z: float | None = None
                if outline is not None:
                    x, z = outline.position_at(corner.apex_m)
                corners.append(
                    {
                        "number": corner.number,
                        "official": corner.official,
                        "entry_m": corner.entry_m,
                        "apex_m": corner.apex_m,
                        "exit_m": corner.exit_m,
                        "x": x,
                        "z": z,
                    }
                )

        if track is not None:
            name: str | None = track.name
            length_m: float | None = track.length_m
        elif outline is not None:
            name = None
            length_m = outline.lap_length_m
        else:
            name = None
            length_m = None

        return {
            "track_id": track_id,
            "name": name,
            "length_m": length_m,
            "outline_source": source,
            "outline": []
            if outline is None
            else [[dist, x, z] for dist, x, z in outline.points],
            "corners": corners,
            # 집중 코너(작업 16)와 코너 지표(작업 15) 자리.
            "focus_corners": [],
            "corner_metrics": {},
            "flag": self.flag(snapshot),
            "team_colors": dict(_TEAM_COLORS_JSON),
        }

    def car_positions(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """모든 차의 지도 위치. 랩 거리로 윤곽 위를 보간한다.

        Motion 패킷이 없어도 되고, 윤곽이 아직 없으면 `x`·`z`만 `None`이다.
        `is_target`은 내 차 바로 앞(레이스 순위 기준) 한 대다. 순위가 아직
        없으면 총 주행 거리로 가장 가까운 앞차를 고른다.
        """
        self._refresh(snapshot)
        with self._lock:
            outline = self._outline
            track = self._track

        packets = snapshot.get("packets") or {}
        lap_entries = _list((packets.get("lap_data") or {}).get("lap_data"))
        participants = _list((packets.get("participants") or {}).get("participants"))
        player_index = snapshot.get("player_car_index")

        cars: list[dict[str, Any]] = []
        totals: dict[int, float] = {}
        for car_index, entry in enumerate(lap_entries):
            if not isinstance(entry, dict):
                continue
            lap = _as_int(entry.get("current_lap_num"))
            distance = _as_float(entry.get("lap_distance"))
            # 아직 출발하지 않은 빈 슬롯(랩 0)은 지도에 그리지 않는다.
            if lap is None or lap < 1 or distance is None:
                continue
            x: float | None = None
            z: float | None = None
            if outline is not None:
                x, z = outline.position_at(distance)
            team_id: int | None = None
            if car_index < len(participants) and isinstance(
                participants[car_index], dict
            ):
                team_id = _as_int(participants[car_index].get("team_id"))
            cars.append(
                {
                    "car": car_index,
                    "dist_m": distance,
                    "x": x,
                    "z": z,
                    "team_id": team_id,
                    "is_player": car_index == player_index,
                    "is_target": False,
                    "position": _as_int(entry.get("car_position")),
                    "lap": lap,
                }
            )
            total = _as_float(entry.get("total_distance"))
            if total is not None:
                totals[car_index] = total

        lap_length_m: float | None = None
        if track is not None:
            lap_length_m = track.length_m
        elif outline is not None:
            lap_length_m = outline.lap_length_m
        _mark_target(cars, totals, lap_length_m)
        return cars

    def flag(self, snapshot: dict[str, Any]) -> str:
        """현재 세션 깃발. `none`·`safety_car`·`vsc`·`red_flag`.

        적기는 Event RDFL 뒤 `StateStore`가 유지하는 래치라 스냅샷 요약에만
        있다. 적기가 SC·VSC보다 우선한다(트랙 선 색).
        """
        flags = snapshot.get("flags")
        if isinstance(flags, dict):
            if flags.get("red") is True:
                return "red_flag"
            if flags.get("sc") is True:
                return "safety_car"
            if flags.get("vsc") is True:
                return "vsc"
            return "none"
        # 예전 스냅샷(깃발 요약 없음) 호환.
        packets = snapshot.get("packets") or {}
        session = packets.get("session") or {}
        status = _as_int(session.get("safety_car_status"))
        # 공식 열거형: 1 = Full SC, 2 = VSC, 3 = 포메이션 랩(경고 아님).
        return {1: "safety_car", 2: "vsc"}.get(status, "none")

    # ------------------------------------------------------------------
    # 윤곽 해석

    def _refresh(self, snapshot: dict[str, Any]) -> None:
        """트랙 ID가 바뀌면 윤곽을 다시 고른다(학습 윤곽은 없을 때만 재확인)."""
        track_id = _track_id(snapshot)
        now = time.monotonic()
        with self._lock:
            if self._loaded and track_id == self._track_id:
                if self._source != "none":
                    return
                if now - self._checked_at_s < _RECHECK_INTERVAL_S:
                    return

            if track_id is None:
                self._track_id = None
                self._outline = None
                self._source = "none"
                self._track = None
                self._loaded = True
                self._checked_at_s = now
                return

            outline, source = resolve_outline(track_id, self._conn)
            track: Track | None = None
            if source == "data":
                try:
                    track = load_track(track_id)
                except TrackDataError:
                    # 데이터 파일이 깨져도 지도 API가 죽지 않게 한다.
                    track = None
            self._track_id = track_id
            self._outline = outline
            self._source = source
            self._track = track
            self._loaded = True
            self._checked_at_s = now


def create_map_router(runtime: Callable[[], Any | None]) -> APIRouter:
    """지도 API 라우터. `web/app.py`가 앱에 한 줄로 붙인다.

    `runtime`은 엔진이 켜져 있을 때 `Runtime`(저장소·지도)을, 꺼져 있으면
    `None`을 돌려주는 함수다. 라우터는 요청마다 읽으므로 엔진을 껐다 켜도
    같은 앱이 새 런타임을 쓴다(SCRUM-82).
    """
    router = APIRouter()

    @router.get("/api/track")
    def api_track() -> dict[str, Any]:
        current = runtime()
        if current is None:
            raise HTTPException(
                status_code=503, detail={"engine": "off"}
            )
        return current.map.track_payload(current.store.snapshot())

    return router


# ---------------------------------------------------------------------------
# 도우미


def _track_id(snapshot: dict[str, Any]) -> int | None:
    """Session 패킷의 트랙 ID. 아직 세션이 없거나 알 수 없으면 `None`."""
    packets = snapshot.get("packets") or {}
    session = packets.get("session") or {}
    value = session.get("track_id")
    track_id = _as_int(value)
    if track_id is None or track_id < 0:
        # 공식 m_trackId는 int8이고 -1이 "알 수 없음"이다.
        return None
    return track_id


def _mark_target(
    cars: list[dict[str, Any]],
    totals: dict[int, float],
    lap_length_m: float | None,
) -> None:
    """내 차 바로 앞 차 한 대를 추월 대상으로 표시한다(설계 6.9-2)."""
    player = next((car for car in cars if car["is_player"]), None)
    if player is None:
        return

    position = player["position"]
    if position is not None and position >= 2:
        for car in cars:
            if car is not player and car["position"] == position - 1:
                car["is_target"] = True
        return

    # 순위가 아직 없으면(랩 초반 등) 총 주행 거리로 가장 가까운 앞차를 고른다.
    player_total = totals.get(player["car"])
    if player_total is None:
        return
    ranked = sorted(
        (
            (total - player_total, car_index)
            for car_index, total in totals.items()
            if car_index != player["car"] and total > player_total
        )
    )
    if not ranked:
        return
    gap_m, car_index = ranked[0]
    if lap_length_m is not None and gap_m > lap_length_m:
        # 한 랩 이상 앞선 차는 "바로 앞차"가 아니다.
        return
    for car in cars:
        if car["car"] == car_index:
            car["is_target"] = True
            return


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
