"""대시보드 웹 앱. FastAPI 앱과 JSON·WebSocket API, 정적 프런트를 묶는다.

설계 6.9의 원칙:

- 화면은 서버 API를 **읽기만** 한다.
- 색은 `/api/thresholds`의 표만 쓴다. 화면에서 문턱값을 다시 계산하지 않는다.
- 값의 위험 단계(`severity`)는 서버가 `f1re.alerts.thresholds.severity`로 판정해
  내려 준다. 음성 경고(작업 12)도 같은 함수를 쓰므로, 화면이 빨간색이 되는
  순간이 곧 음성 경고 조건이다.
- 값이 잠깐 끊겨도 마지막 값을 지우지 않는다. `/api/state`의 `connection.stale`이
  참이면 화면이 칸을 유지한 채 흐리게 표시한다.

API:

- ``GET /api/state``  스냅샷 + 내 차 요약 + 연결 상태 + 음성·무전 페이지 상태
- ``GET /api/thresholds``  색·문턱값 표(`color_table()`)
- ``WS /ws/state``  같은 상태를 10Hz로 민다
- ``GET /``  대시보드 HTML, ``/static/...``  빌드 없는 HTML/JS/CSS
- ``GET /radio``·``WS /ws/radio``  무전 페이지와 음성 전달(`web/radio_api.py`)
- ``GET /api/radio/log``·``POST /api/radio/mute``·``GET /api/report``
  무전 기록·종류별 끄기·세션 리포트(`web/report_api.py`)
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from f1re.alerts.thresholds import color_table, severity
from f1re.state.store import StateStore
from f1re.web.map_api import TrackMap, create_map_router
from f1re.web.radio_api import RadioService, router as radio_router
from f1re.web.report_api import create_report_router

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 상태를 브라우저로 미는 주기(초). 설계 6.9·계획 작업 9의 10Hz.
PUSH_INTERVAL_S = 0.1

# 이 시간 넘게 패킷이 없으면 "데이터 안 옴"으로 본다. 60Hz 수신에서 2초면
# 충분히 긴 공백이라, 값이 잠깐 끊긴 것과 수신이 멈춘 것을 구분한다.
STALE_AFTER_MS = 2000.0

# F1 25 ERS 저장소 최대 에너지(J). 게임 패킷에는 최댓값이 없어 공식 규정값을 쓴다.
ERS_STORE_MAX_J = 4_000_000.0

# 타이어 배열은 공식 패킷 순서(뒤왼쪽, 뒤오른쪽, 앞왼쪽, 앞오른쪽)다.
# 화면에는 앞왼쪽 → 앞오른쪽 → 뒤왼쪽 → 뒤오른쪽 순서로 보여 준다.
_TYRE_DISPLAY_ORDER: tuple[tuple[int, str, str], ...] = (
    (2, "front_left", "앞왼쪽"),
    (3, "front_right", "앞오른쪽"),
    (0, "rear_left", "뒤왼쪽"),
    (1, "rear_right", "뒤오른쪽"),
)

# 공식 열거형의 실제 타이어 컴파운드 값 → 화면 표시 이름.
_TYRE_COMPOUNDS: dict[int, str] = {
    15: "C6",
    16: "C5",
    17: "C4",
    18: "C3",
    19: "C2",
    20: "C1",
    7: "인터미디어트",
    8: "웻",
    9: "드라이",
}

# ERS 배포 모드(공식 열거형) → 화면 표시 이름.
_ERS_DEPLOY_MODES: dict[int, str] = {
    0: "없음",
    1: "중간",
    2: "핫랩",
    3: "추월",
}

# 손상 항목: (표시 키, 화면 이름, Car Damage 패킷 필드, 문턱값 지표 이름).
_DAMAGE_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    (
        "front_left_wing",
        "앞날개 왼쪽",
        "front_left_wing_damage",
        "front_left_wing_damage",
    ),
    (
        "front_right_wing",
        "앞날개 오른쪽",
        "front_right_wing_damage",
        "front_right_wing_damage",
    ),
    ("rear_wing", "뒷날개", "rear_wing_damage", "rear_wing_damage"),
    ("floor", "바닥", "floor_damage", "floor_damage"),
    ("diffuser", "디퓨저", "diffuser_damage", "diffuser_damage"),
    ("sidepod", "사이드포드", "sidepod_damage", "sidepod_damage"),
    ("gearbox", "기어박스", "gear_box_damage", "gearbox_damage"),
    ("engine", "엔진", "engine_damage", "engine_damage"),
    ("engine_mguh_wear", "MGU-H 마모", "engine_mguh_wear", "engine_mguh_wear"),
    (
        "engine_es_wear",
        "에너지 저장장치 마모",
        "engine_es_wear",
        "engine_es_wear",
    ),
    ("engine_ce_wear", "제어 전자장치 마모", "engine_ce_wear", "engine_ce_wear"),
    ("engine_ice_wear", "내연기관 마모", "engine_ice_wear", "engine_ice_wear"),
    ("engine_mguk_wear", "MGU-K 마모", "engine_mguk_wear", "engine_mguk_wear"),
    ("engine_tc_wear", "터보차저 마모", "engine_tc_wear", "engine_tc_wear"),
)


# ---------------------------------------------------------------------------
# 상태 만들기
# ---------------------------------------------------------------------------


def state_payload(
    store: StateStore,
    radio: RadioService,
    map_state: TrackMap | None = None,
) -> dict[str, Any]:
    """화면·API가 함께 쓰는 상태 묶음.

    `cars`·`flag`는 서킷 지도(작업 10)용이다. `map_state`가 없으면 빈 값으로
    두어 지도 없이도 같은 모양을 유지한다.
    """
    snapshot = store.snapshot()
    return {
        "snapshot": snapshot,
        "car": player_summary(snapshot),
        "connection": connection_info(snapshot),
        "status": radio.status(),
        "cars": [] if map_state is None else map_state.car_positions(snapshot),
        "flag": "none" if map_state is None else map_state.flag(snapshot),
    }


def player_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    """스냅샷에서 내 차의 화면용 요약을 만든다.

    값마다 `severity`를 붙인다. 문턱값이 정의되지 않은 값(압력·나이·ERS 등)은
    `severity`가 `None`이다. 패킷이 아직 없어도 칸이 사라지지 않도록 항상
    타이어 4개·브레이크 4개·손상 항목 전부를 `None` 값으로 채워 돌려준다.
    """
    packets = snapshot.get("packets") or {}
    car_index = snapshot.get("player_car_index")
    status_entry = _player_entry(
        packets, "car_status", "car_status_data", car_index
    )
    telemetry = _player_entry(
        packets, "car_telemetry", "car_telemetry_data", car_index
    )
    damage_entry = _player_entry(
        packets, "car_damage", "car_damage_data", car_index
    )
    lap_entry = _player_entry(packets, "lap_data", "lap_data", car_index)
    session = packets.get("session") or {}

    tyres = []
    for index, position, label in _TYRE_DISPLAY_ORDER:
        tyres.append(
            {
                "position": position,
                "label": label,
                "wear": _number_cell(
                    _list_get(damage_entry.get("tyres_wear"), index),
                    metric="tyre_wear",
                    unit="%",
                ),
                "surface_temp": _number_cell(
                    _list_get(telemetry.get("tyres_surface_temperature"), index),
                    metric="tyre_temp",
                    unit="°C",
                ),
                "inner_temp": _number_cell(
                    _list_get(telemetry.get("tyres_inner_temperature"), index),
                    metric="tyre_inner_temp",
                    unit="°C",
                ),
                "pressure": _number_cell(
                    _list_get(telemetry.get("tyres_pressure"), index),
                    unit="psi",
                ),
                "compound": _compound_cell(
                    status_entry.get("actual_tyre_compound")
                ),
                "age_laps": _number_cell(
                    status_entry.get("tyres_age_laps"), unit="랩"
                ),
            }
        )

    brakes = [
        {
            "position": position,
            "label": label,
            "temp": _number_cell(
                _list_get(telemetry.get("brakes_temperature"), index),
                metric="brake_temp",
                unit="°C",
            ),
        }
        for index, position, label in _TYRE_DISPLAY_ORDER
    ]

    damage = [
        {
            "key": key,
            "label": label,
            "value": _number_cell(
                damage_entry.get(field), metric=metric, unit="%"
            ),
        }
        for key, label, field, metric in _DAMAGE_ITEMS
    ]

    fuel_kg = _finite(status_entry.get("fuel_in_tank"))
    fuel_laps = _finite(status_entry.get("fuel_remaining_laps"))
    remaining_race_laps = _remaining_race_laps(session, lap_entry)
    per_lap_kg = (
        fuel_kg / fuel_laps
        if fuel_kg is not None and fuel_laps is not None and fuel_laps > 0
        else None
    )
    laps_margin = (
        fuel_laps - remaining_race_laps
        if fuel_laps is not None and remaining_race_laps is not None
        else None
    )
    fuel = {
        "remaining_kg": _number_cell(fuel_kg, unit="kg"),
        "per_lap_kg": _number_cell(per_lap_kg, unit="kg/랩"),
        "remaining_laps": _number_cell(fuel_laps, unit="랩"),
        "remaining_race_laps": _number_cell(remaining_race_laps, unit="랩"),
        "laps_margin": _number_cell(
            laps_margin, metric="fuel_laps_margin", unit="랩"
        ),
    }

    ers_joules = _finite(status_entry.get("ers_store_energy"))
    store_pct = (
        min(100.0, max(0.0, ers_joules / ERS_STORE_MAX_J * 100.0))
        if ers_joules is not None
        else None
    )
    ers = {
        "store_pct": _number_cell(store_pct, unit="%"),
        "mode": _ers_mode_cell(status_entry.get("ers_deploy_mode")),
    }

    return {
        "car_index": car_index,
        "has_data": bool(status_entry or telemetry or damage_entry or lap_entry),
        "tyres": tyres,
        "brakes": brakes,
        "damage": damage,
        "fuel": fuel,
        "ers": ers,
    }


def connection_info(
    snapshot: dict[str, Any], *, now_ns: int | None = None
) -> dict[str, Any]:
    """수신이 살아 있는지. 마지막 수신 시각으로 나이와 끊김 여부를 낸다."""
    last_recv_ns = (snapshot.get("diagnostics") or {}).get("last_recv_ns")
    if not isinstance(last_recv_ns, (int, float)) or not math.isfinite(
        float(last_recv_ns)
    ):
        return {
            "stale": True,
            "data_age_ms": None,
            "stale_after_ms": STALE_AFTER_MS,
        }
    now = time.time_ns() if now_ns is None else now_ns
    age_ms = max(0.0, (now - int(last_recv_ns)) / 1_000_000.0)
    return {
        "stale": age_ms > STALE_AFTER_MS,
        "data_age_ms": round(age_ms, 1),
        "stale_after_ms": STALE_AFTER_MS,
    }


# ---------------------------------------------------------------------------
# FastAPI 앱
# ---------------------------------------------------------------------------


def create_app(
    store: StateStore,
    radio: RadioService | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> FastAPI:
    """상태 저장소 하나를 읽는 FastAPI 앱을 만든다.

    `radio`를 넘기면 그 무전 서비스를 쓴다(실서버는 음성 캐시·DB 기록을 붙인
    서비스를 넘긴다). 생략하면 음성 캐시 없는 기본 서비스를 만들어, 상태가
    "음성 오프라인"/"무전 페이지 미연결"로 정직하게 보인다.
    `conn`은 학습한 서킷 윤곽(작업 10)을 읽을 SQLite 연결이다. 없으면 패키지
    데이터 파일의 중심선만 쓴다(테스트·단독 실행).
    """
    service = radio if radio is not None else RadioService(store)
    map_state = TrackMap(conn)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        service.ensure_started()
        try:
            yield
        finally:
            await service.aclose()

    app = FastAPI(
        title="F1 레이스 엔지니어 대시보드",
        lifespan=lifespan,
    )
    app.state.radio = service

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(radio_router)
    app.include_router(create_map_router(store, map_state))
    app.include_router(create_report_router(store, service, conn))

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "dashboard.html",
            media_type="text/html; charset=utf-8",
        )

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return state_payload(store, service, map_state)

    @app.get("/api/thresholds")
    def api_thresholds() -> dict[str, Any]:
        return color_table()

    @app.websocket("/ws/state")
    async def ws_state(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(state_payload(store, service, map_state))
                await asyncio.sleep(PUSH_INTERVAL_S)
        except (WebSocketDisconnect, RuntimeError, OSError):
            # 브라우저가 닫혔다. 정리는 Starlette가 한다.
            return

    return app


# ---------------------------------------------------------------------------
# 값 변환
# ---------------------------------------------------------------------------


def _player_entry(
    packets: dict[str, Any],
    packet_name: str,
    list_key: str,
    car_index: Any,
) -> dict[str, Any]:
    """패킷 본문에서 내 차 인덱스의 항목을 꺼낸다. 없으면 빈 dict."""
    if not isinstance(car_index, int) or not 0 <= car_index < 22:
        return {}
    body = packets.get(packet_name) or {}
    entries = body.get(list_key) or []
    if car_index < len(entries) and isinstance(entries[car_index], dict):
        return entries[car_index]
    return {}


def _list_get(values: Any, index: int) -> Any:
    if isinstance(values, (list, tuple)) and 0 <= index < len(values):
        return values[index]
    return None


def _number_cell(
    value: Any, *, metric: str | None = None, unit: str | None = None
) -> dict[str, Any]:
    """숫자 값 하나를 위험 단계와 함께 포장한다."""
    number = _finite(value)
    return {
        "value": number,
        "unit": unit,
        "metric": metric,
        "severity": severity(metric, number).value if metric else None,
    }


def _text_cell(value: str | None, code: int | None) -> dict[str, Any]:
    """문턱값이 없는 문자열 값(컴파운드·모드) 하나."""
    return {
        "value": value,
        "code": code,
        "unit": None,
        "metric": None,
        "severity": None,
    }


def _compound_cell(value: Any) -> dict[str, Any]:
    code = _integer(value)
    if code is None or code == 0:
        return _text_cell(None, None)
    return _text_cell(_TYRE_COMPOUNDS.get(code, f"알 수 없음({code})"), code)


def _ers_mode_cell(value: Any) -> dict[str, Any]:
    code = _integer(value)
    if code is None:
        return _text_cell(None, None)
    return _text_cell(_ERS_DEPLOY_MODES.get(code, f"알 수 없음({code})"), code)


def _remaining_race_laps(
    session: dict[str, Any], lap_entry: dict[str, Any]
) -> float | None:
    """남은 레이스 랩. 현재 랩을 포함해 완주까지 남은 랩 수다.

    타임 트라이얼·프랙티스처럼 `total_laps`가 0이면 알 수 없으므로 `None`.
    """
    total_laps = _integer(session.get("total_laps"))
    current_lap = _integer(lap_entry.get("current_lap_num"))
    if not total_laps or not current_lap or total_laps <= 0 or current_lap <= 0:
        return None
    return float(max(total_laps - current_lap + 1, 0))


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
