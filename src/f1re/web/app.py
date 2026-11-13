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

- ``GET /api/state``  스냅샷 + 내 차 요약 + 연결 상태 + 깃발 요약(`flags`) +
  음성·무전 페이지 상태
- ``GET /api/thresholds``  색·문턱값 표(`color_table()`)
- ``WS /ws/state``  같은 상태를 10Hz로 민다
- ``GET /``  대시보드 HTML, ``/static/...``  빌드 없는 HTML/JS/CSS
- ``GET /radio``·``WS /ws/radio``  무전 페이지와 음성 전달(`web/radio_api.py`)
- ``GET /api/radio/log``·``POST /api/radio/mute``·``GET /api/report``
  무전 기록·종류별 끄기·세션 리포트(`web/report_api.py`)
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from f1re.alerts.thresholds import color_table, severity
from f1re.engine import (
    ENGINE_OFF,
    ENGINE_ON,
    ENGINE_STATES,
    Engine,
    Runtime,
)
from f1re.state.store import StateStore
from f1re.web.lift_coast_api import create_lift_coast_router
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

# 한 랩 ERS 배포 한도(J)와 MGU-K 최대 출력(W). 화면 막대의 기준값이며
# 값 칸의 `max`로 함께 내려 보낸다. 기준값을 이 한 곳에만 두어 화면이
# 같은 숫자를 따로 들고 있지 않게 한다.
ERS_LAP_DEPLOY_MAX_J = 4_000_000.0
MGUK_MAX_POWER_W = 120_000.0

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
    두어 지도 없이도 같은 모양을 유지한다. `flags`는 깃발 상태 줄(SCRUM-80)이
    쓰는 요약이며 `snapshot["flags"]`와 같은 값이다.
    """
    snapshot = store.snapshot()
    return {
        "snapshot": snapshot,
        "car": player_summary(snapshot),
        "connection": connection_info(snapshot),
        "status": radio.status(),
        "cars": [] if map_state is None else map_state.car_positions(snapshot),
        "flag": "none" if map_state is None else map_state.flag(snapshot),
        "flags": snapshot.get("flags", _empty_flags()),
    }


def _empty_flags() -> dict[str, Any]:
    """스냅샷에 깃발 요약이 없을 때의 기본값(모두 꺼짐)."""
    return {
        "red": False,
        "blue": False,
        "yellow": False,
        "yellow_zones": [],
        "sc": False,
        "vsc": False,
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
    # 게임 값 그대로(J, W)를 내려 주고 MJ·kW 변환은 화면에서만 한다.
    ers = {
        "store_pct": _number_cell(store_pct, unit="%"),
        "mode": _ers_mode_cell(status_entry.get("ers_deploy_mode")),
        "deployed_this_lap": _number_cell(
            status_entry.get("ers_deployed_this_lap"),
            unit="J",
            max_value=ERS_LAP_DEPLOY_MAX_J,
        ),
        "harvested_mguk": _number_cell(
            status_entry.get("ers_harvested_this_lap_mguk"), unit="J"
        ),
        "harvested_mguh": _number_cell(
            status_entry.get("ers_harvested_this_lap_mguh"), unit="J"
        ),
        "mguk_power": _number_cell(
            status_entry.get("engine_power_mguk"),
            unit="W",
            max_value=MGUK_MAX_POWER_W,
        ),
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


class EngineStateRequest(BaseModel):
    """`POST /api/engine` 요청. ``state``는 ``"on"`` 또는 ``"off"``."""

    state: str


# 엔진이 꺼져 있어도 동작해야 하는 HTTP 경로(SCRUM-82).
# 그 밖의 API는 503 `{"engine": "off"}`로 답한다. 화면·정적 파일은 항상 뜬다.
_ENGINE_OFF_ALLOWED_PATHS = {"/", "/radio", "/health", "/api/engine"}


def _engine_off_allowed(path: str) -> bool:
    return path in _ENGINE_OFF_ALLOWED_PATHS or path.startswith("/static/")


def create_app(
    store: StateStore | None = None,
    radio: RadioService | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    """상태 저장소 하나를 읽는 FastAPI 앱을 만든다.

    실서버는 `engine`을 넘긴다. 웹 서버는 항상 뜨고, 엔진이 꺼져 있으면
    화면·정적 파일·health·엔진 API만 동작하며 나머지 API는 503으로 답한다.

    `engine` 없이 `store`/`radio`/`conn`을 넘기면 그 자원에 붙은 고정 엔진(항상
    ON)을 만들어 준다. 테스트·단독 실행이 쓰는 예전 방식이다. `radio`를 생략하면
    음성 캐시 없는 기본 서비스를 만든다.
    """
    if engine is None:
        state_store = store if store is not None else StateStore()
        service = radio if radio is not None else RadioService(state_store)
        engine = Engine(
            runtime=Runtime(
                store=state_store,
                radio=service,
                conn=conn,
                map=TrackMap(conn),
            )
        )
    app = FastAPI(
        title="F1 레이스 엔지니어 대시보드",
        lifespan=_lifespan(engine),
    )
    app.state.engine = engine
    app.state.radio = engine.radio

    @app.middleware("http")
    async def pages_and_engine_guard(request: Request, call_next):
        path = request.url.path
        # OFF면 엔진이 필요한 API를 부르지 않는다. health·화면·엔진 API는 통과.
        if engine.state == ENGINE_OFF and not _engine_off_allowed(path):
            response: Any = JSONResponse(
                status_code=503, content={"engine": ENGINE_OFF}
            )
        else:
            response = await call_next(request)
        # 배포·업데이트 뒤 브라우저가 예전 JS/CSS를 캐시에서 써서 화면이 멈춘 일이
        # 있었다(SCRUM-79). 화면(/, /radio)과 정적 파일은 매번 서버에 다시 확인하게
        # 한다. `no-cache`는 저장 금지가 아니라 재검증 요구라, 안 바뀐 파일은
        # StaticFiles가 304로 가볍게 답한다. API 응답 정책은 그대로 둔다.
        if path in {"/", "/radio", "/static"} or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(radio_router)
    app.include_router(create_map_router(lambda: engine.runtime))
    app.include_router(create_report_router(lambda: engine.runtime))
    app.include_router(create_lift_coast_router(_lift_state_source(engine)))

    def _runtime() -> Runtime:
        runtime = engine.runtime
        if runtime is None:
            raise HTTPException(
                status_code=503, detail={"engine": ENGINE_OFF}
            )
        return runtime

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "dashboard.html",
            media_type="text/html; charset=utf-8",
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        """모니터링용. 엔진 상태와 무관하게 200으로 답한다."""
        return {"status": "ok", "engine": engine.state}

    @app.get("/api/engine")
    def api_engine_get() -> dict[str, str]:
        return {"state": engine.state}

    @app.post("/api/engine")
    async def api_engine_post(body: EngineStateRequest) -> dict[str, str]:
        state = body.state.strip().lower()
        if state not in ENGINE_STATES:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "state는 'on' 또는 'off'여야 합니다",
                    "state": body.state,
                },
            )
        try:
            # 전환은 잠금으로 순서대로 처리된다. 같은 상태면 그대로 성공한다.
            await engine.set_state(state)
        except Exception as exc:  # 포트 사용 중 등. 웹은 계속 뜬다.
            raise HTTPException(
                status_code=500,
                detail={
                    "engine": engine.state,
                    "message": f"{type(exc).__name__}: {exc}",
                },
            ) from exc
        return {"state": engine.state}

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        runtime = _runtime()
        return state_payload(runtime.store, runtime.radio, runtime.map)

    @app.get("/api/thresholds")
    def api_thresholds() -> dict[str, Any]:
        return color_table()

    @app.websocket("/ws/state")
    async def ws_state(websocket: WebSocket) -> None:
        await websocket.accept()
        if engine.state != ENGINE_ON:
            await _send_engine_off(websocket)
            return
        off_event = engine.off_event
        try:
            while engine.state == ENGINE_ON:
                runtime = engine.runtime
                if runtime is None:
                    break
                await websocket.send_json(
                    state_payload(runtime.store, runtime.radio, runtime.map)
                )
                await _wait_or_off(off_event, PUSH_INTERVAL_S)
        except (WebSocketDisconnect, RuntimeError, OSError):
            # 브라우저가 닫혔다. 정리는 Starlette가 한다.
            return
        # 엔진이 꺼졌다. 열려 있던 연결에도 알리고 닫는다(SCRUM-82).
        await _send_engine_off(websocket)

    return app


def _lift_state_source(engine: Engine):
    """`/api/lift-coast`가 읽을 상태를 엔진 런타임의 파이프라인에서 꺼낸다.

    엔진 OFF이거나 세션 전이면 `None`이고 라우터가 빈 기본 상태를 쓴다
    (실서버 배선 SCRUM-84).
    """

    def source():
        runtime = engine.runtime
        if runtime is None:
            return None
        pipeline = getattr(runtime, "pipeline", None)
        return None if pipeline is None else pipeline.lift_state()

    return source


def _lifespan(engine: Engine):
    """고정 엔진이면 무전 루프를 앱 수명에 묶고, 소유 엔진이면 종료 때 정리한다."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        radio = None if engine.managed else engine.radio
        if radio is not None:
            radio.ensure_started()
        try:
            yield
        finally:
            radio = None if engine.managed else engine.radio
            if radio is not None:
                await radio.aclose()
            else:
                # 종료는 사용자가 끈 것이 아니므로 저장 상태를 바꾸지 않는다.
                await engine.aclose()

    return lifespan


async def _wait_or_off(off_event: asyncio.Event, timeout_s: float) -> None:
    """다음 푸시까지 기다리되, 엔진이 꺼지면 바로 깨어난다."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(off_event.wait(), timeout_s)


async def _send_engine_off(websocket: WebSocket) -> None:
    """엔진이 꺼졌다는 메시지를 보내고 WebSocket을 닫는다."""
    with contextlib.suppress(Exception):
        await websocket.send_json({"type": "engine", "state": ENGINE_OFF})
    with contextlib.suppress(Exception):
        await websocket.close()


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
    value: Any,
    *,
    metric: str | None = None,
    unit: str | None = None,
    max_value: float | None = None,
) -> dict[str, Any]:
    """숫자 값 하나를 위험 단계와 함께 포장한다.

    `max_value`는 화면 막대의 기준값이다. 게임 값과 같은 단위로 넣는다.
    """
    number = _finite(value)
    cell = {
        "value": number,
        "unit": unit,
        "metric": metric,
        "severity": severity(metric, number).value if metric else None,
    }
    # 막대 기준값이 있는 칸만 `max`를 더한다(기존 칸 모양은 그대로).
    if max_value is not None:
        cell["max"] = max_value
    return cell


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
