"""리프트 앤 코스트 API(SCRUM-71).

``GET /api/lift-coast``는 화면(지도)에 추천 구간과 최근 코스팅 거리를 보여
주기 위한 읽기 전용 API다. 값은 `LiftCoastCoach`가 `LiftCoastState`에 남긴
것을 그대로 돌려준다(화면은 계산하지 않는다, 설계 6.9).

실서버 배선은 뒤 작업 몫이다. 라이브 파이프라인이 `LiftCoastCoach`를 만들 때
`default_state()`를 넘기면 이 라우터와 같은 상태를 쓴다. 지금은 아무도 쓰지
않으면 빈 추천 목록을 돌려준다.

응답 모양::

    {
      "need": {"active": true, "reasons": ["fuel_laps_margin"],
               "fuel_laps_margin": -2.0, "brake_temp": 1050.0,
               "front_tyre_temp": 118.0},
      "recommended": [
        {"corner": 16, "official": true, "label": "16번", "entry_m": 5240.0,
         "brake_m": 5149.0, "lift_m": 5099.0, "straight_m": 340.0,
         "approach_speed_kmh": 335.0, "decel_kmh": 225.0,
         "efficiency": 25.6, "target_m": 50.0}, ...
      ],
      "recent_coasting": [
        {"corner": 16, "lap": 4, "lift_m": 5089.0, "brake_m": 5149.0,
         "distance_m": 60.0, "time_ms": 900, "target_m": 50.0,
         "status": "good"}, ...
      ],
      "fuel": {"last_used_kg": 1.8, "average_used_kg": 1.8,
               "recent": [{"lap": 4, "start_kg": 18.2, "end_kg": 16.4,
                           "used_kg": 1.8}]},
      "instructed": {"lap": 3, "corners": [16, 1, 10],
                     "targets_m": {"16": 50.0, ...}}
    }
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from f1re.coaching.lift_coast import LiftCoastState


_DEFAULT_STATE = LiftCoastState()


def default_state() -> LiftCoastState:
    """이 프로세스가 함께 쓰는 기본 상태. 코치를 만들 때 넘긴다."""
    return _DEFAULT_STATE


def create_lift_coast_router(state: LiftCoastState | None = None) -> APIRouter:
    """리프트 앤 코스트 라우터. `web/app.py`가 앱에 한 줄로 붙인다."""
    target = state or _DEFAULT_STATE
    router = APIRouter()

    @router.get("/api/lift-coast")
    def api_lift_coast() -> dict[str, Any]:
        return target.snapshot()

    return router


router = create_lift_coast_router()


__all__ = ["create_lift_coast_router", "default_state", "router"]
