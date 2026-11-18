# 다시 보기 텔레메트리 분석 화면 (`/telemetry`) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 녹화된 F1 25 텔레메트리(`.f1rec`/`.f1rec.zst`)를 1회 스캔으로 인덱싱해 세션 전체 시간축(`초/분`) 그래프·계층 메트릭 체크박스·서킷 지도 미리보기·비교 차량 점선 오버레이·무전/사건 마커를 제공하는 독립 분석 화면(`/telemetry`)을 구현한다.

**Architecture:** 백엔드는 `src/f1re/udp/telemetry_index.py`가 `.f1rec` 파일을 스레드(`asyncio.to_thread`)에서 1회 순차 스캔해 내 차 시계열(`array('f')`), 10Hz 22대 지도 프레임, 범위 제한된 무전/사건 마커를 LRU 캐시(최대 1건)로 보관하고, `replay_api.py`가 `/api/replay/telemetry/{open,summary,series,map_frames}` 엔드포인트로 min/max 보존 다운샘플과 비교 차량 지연 추출을 제공한다. 프런트엔드는 빌드 없는 순수 HTML/CSS/JS + Canvas 2D 차트와 기존 `TrackMapView`(`map.js`)를 조합하며, 청크 프리페치된 10Hz `map_frames`와 브라우저 로컬 타이머로 동작해 대시보드(`/`)의 전역 `ReplayManager`와 완전히 격리된다.

**Tech Stack:** Python 3.14+, FastAPI, `uv`, `pytest`, 표준 `array`/`math`, 기존 `f1re.udp`(`recording`, `formats.f2025`, `web_replay`, `gcs_transfer`), `f1re.state`/`alerts`/`radio`/`tracks`, 순수 HTML/CSS/JS + Canvas 2D.

**Spec:** [2026-11-15-replay-telemetry-view-design.md](file:///D:/Programming/f1-race-engineer/docs/design/2026-11-15-replay-telemetry-view-design.md)

## Global Constraints

- **Python / 실행 도구**: Python 3.14 이상(`requires-python = ">=3.14"`), 실행·패키지·테스트는 `uv`(`uv run pytest`)만 사용하며 새 외부 의존성을 추가하지 않는다.
- **서버 환경**: 리눅스 홈서버(`i5-12500T · 32GB`, GPU 없음). Windows 전용 API·경로를 쓰지 않고, 무거운 인덱싱은 이벤트 루프를 막지 않게 `asyncio.to_thread`로 실행하며 메모리에 최근 1건 + 비교 차량 최대 1대만 캐시한다.
- **엔진 & 재생 격리**: 모든 `/api/replay/telemetry/*` API와 `/telemetry` 페이지는 `Engine` ON/OFF 및 전역 `ReplayManager` 재생 상태와 완전히 독립적으로 동작한다.
- **프런트엔드 규약**: 외부 JS 차트 라이브러리·번들러·CDN을 도입하지 않는 순수 HTML/JS/CSS + Canvas 2D만 사용하며, 메트릭 정의·라벨·색·단위 그룹의 단일 출처는 서버 API가 가진다.
- **언어 및 보안**: UI 텍스트·주석·커밋 메시지·문서는 한국어로 작성한다(코드 식별자는 영어). 공개 레포이므로 시크릿·상용 앱 분석 자료를 넣지 않는다.

## Review Focus

1. **비공개 비교 차량(`your_telemetry == 0`)**: `CarTelemetry`·`CarStatus`·`CarDamage` 원천 항목(`restricted_when_private = true`)만 `compare_series: null`, `compare_restricted: true`로 억제되고, `LapData`·`Motion` 원천 항목은 정상 비교 시계열이 내려오는지 (`tests/udp/test_telemetry_index.py`, Task 2).
2. **min/max 다운샘플의 스파이크·시간순서 보존**: 원본 샘플이 `max_points`를 초과할 때 각 버킷의 최솟값·최댓값이 시간 오름차순으로 남고(`brake` 100% 1샘플 스파이크 보존), `step=True` 이산 지표는 버킷 대표 변화점이 유지되는지 (`tests/udp/test_telemetry_index.py`, Task 1).
3. **잘못된/미완료 녹화 및 경계 구간 처리**: 존재하지 않는 `recording_id`(404), 녹화 진행 중(`end_reason == 'recording'`, 409), 미수신 패킷 구간의 `NaN` 필터링(JSON `NaN` 직렬화 오류 방지)이 보장되는지 (`tests/web/test_telemetry_api.py`, Task 3).
4. **전역 `ReplayManager` 비간섭 보장**: `/telemetry`에서 `open` → `summary` → `series` → `map_frames`를 호출한 뒤에도 `ReplayManager.active_recording_id is None`이고 `GET /api/replay/state`의 `replay.active is False`로 남는지 (`tests/web/test_telemetry_api.py`, Task 3).
5. **입력 검증 & 클램핑**: 알 수 없는 `metrics` 키(400), `from_ms > to_ms` 또는 음수 `t_ms`(클램핑/400), `compare_car` 범위 외 값 또는 내 차 지정 시 안전 처리, 그리고 `BackupWorker` 캐시 정리 중 열린 텔레메트리 녹화가 보호되는지 (`tests/udp/test_telemetry_index.py`, `tests/web/test_telemetry_api.py`, Task 2 & Task 3).

---

## File Structure

- **Create**: `src/f1re/udp/telemetry_index.py` — 메트릭 카탈로그(`TELEMETRY_CATALOG`), 프리셋(`TELEMETRY_PRESETS`), 시계열 다운샘플러, 1회 스캔 인덱서, 10Hz 지도 프레임 추출, 비교 차량 지연 추출, LRU=1 `TelemetryIndexStore`.
- **Modify**: `src/f1re/udp/gcs_transfer.py` 또는 `src/f1re/udp/backup.py` — `BackupWorker`가 로컬 캐시 정리 시 보호할 ID 콜백/집합에 `TelemetryIndexStore` 활성 `recording_id` 포함.
- **Modify**: `src/f1re/web/replay_api.py` — `/api/replay/telemetry/open`, `summary`, `series`, `map_frames` 엔드포인트 추가 및 `TelemetryIndexStore` 연결.
- **Modify**: `src/f1re/web/app.py` — `GET /telemetry` 정적 HTML 라우트 추가 및 `TelemetryIndexStore` 생성·보호 ID 연동.
- **Modify**: `src/f1re/web/static/map.js` — `setTrack(track, options)`(리플레이/분석 시 `/api/lift-coast` 폴링 생략 옵션), `is_compare` 비교 차량 점선/라벨 강조, 미리보기 배지 표시 지원.
- **Modify**: `src/f1re/web/static/dashboard.html`, `src/f1re/web/static/dashboard.js`, `src/f1re/web/static/style.css` — 상단/모달 `/telemetry` 링크 및 녹화 목록 행 `"그래프 분석"` 버튼(`/telemetry?id=<id>`).
- **Create**: `src/f1re/web/static/telemetry.html`, `src/f1re/web/static/telemetry.css`, `src/f1re/web/static/telemetry.js` — 3분할 UI, 계층 체크박스, 프리셋 바, 독립 10Hz `map_frames` 재생 루프, Canvas 2D 다중 축 그래프·마커·줌/팬·호버/클릭 연동.
- **Create**: `tests/udp/test_telemetry_index.py`, `tests/web/test_telemetry_api.py` — 백엔드 단위·통합 테스트.

---

### Task 1: 메트릭 카탈로그, 프리셋 및 min/max 다운샘플러 (`telemetry_index.py` 1부)

**Files:**
- Create: `src/f1re/udp/telemetry_index.py`
- Test: `tests/udp/test_telemetry_index.py`

**Interfaces:**
- Produces:
  - `TelemetryMetricSpec(key: str, label: str, category_id: str, unit: str, unit_group: str, source_packet: str, value_range: tuple[float, float] | None, step: bool, restricted_when_private: bool, wheel_group: str | None = None, wheel_label: str | None = None)`
  - `TelemetryCategorySpec(id: str, label: str, metrics: tuple[TelemetryMetricSpec, ...])`
  - `TELEMETRY_CATALOG: tuple[TelemetryCategorySpec, ...]`
  - `TELEMETRY_PRESETS: tuple[dict[str, Any], ...]`
  - `MetricChannel` (`times_ms: array('i')`, `values: array('f')`)
  - `CarSeries` (차량 1대의 `car_index: int`, `channels: dict[str, MetricChannel]`)
  - `catalog_payload() -> list[dict[str, Any]]`
  - `presets_payload() -> list[dict[str, Any]]`
  - `get_metric_spec(key: str) -> TelemetryMetricSpec | None`
  - `downsample_channel(channel: MetricChannel, spec: TelemetryMetricSpec, from_ms: int, to_ms: int, max_points: int) -> tuple[list[list[float]], bool]`

- [ ] **Step 1: Write the failing test**

`tests/udp/test_telemetry_index.py`에 카탈로그·프리셋·min/max 다운샘플러 검증 테스트를 작성한다.

```python
from array import array
import math

from f1re.udp.telemetry_index import (
    TELEMETRY_CATALOG,
    TELEMETRY_PRESETS,
    MetricChannel,
    catalog_payload,
    downsample_channel,
    get_metric_spec,
    presets_payload,
)


def test_catalog_covers_categories_and_private_flags() -> None:
    cats = [c.id for c in TELEMETRY_CATALOG]
    assert cats == ["driving", "tyres_brakes", "energy_fuel", "lap_pace", "damage_setup"]
    assert get_metric_spec("speed").restricted_when_private is True
    assert get_metric_spec("g_lat").restricted_when_private is False
    assert get_metric_spec("gap_front_s").restricted_when_private is False
    assert get_metric_spec("gear").step is True
    payload = catalog_payload()
    assert len(payload) == 5
    tyre_keys = [m["key"] for m in payload[1]["metrics"]]
    assert "tyre_surface_temp_fl" in tyre_keys
    assert "brake_temp_rr" in tyre_keys


def test_presets_reference_valid_metric_keys() -> None:
    presets = presets_payload()
    assert len(presets) >= 4
    for preset in presets:
        assert preset["metrics"]
        for key in preset["metrics"]:
            assert get_metric_spec(key) is not None


def test_downsample_preserves_spikes_and_skips_nans() -> None:
    times = array("i", range(0, 1000, 10))  # 100개 샘플 (0..990ms)
    vals = array("f", [0.0] * 100)
    vals[25] = 100.0  # 짧은 브레이크 스파이크
    vals[50] = math.nan  # 미수신 구간
    ch = MetricChannel(times_ms=times, values=vals)
    spec = get_metric_spec("brake")
    points, downsampled = downsample_channel(ch, spec, 0, 990, max_points=20)
    assert downsampled is True
    assert len(points) <= 20
    assert any(p[0] == 250 and p[1] == 100.0 for p in points)
    assert all(math.isfinite(p[1]) for p in points)
    # 시간 오름차순 유지
    assert [p[0] for p in points] == sorted(p[0] for p in points)


def test_downsample_returns_raw_points_when_within_max_points() -> None:
    ch = MetricChannel(times_ms=array("i", [0, 100, 200]), values=array("f", [1.0, 2.0, 3.0]))
    spec = get_metric_spec("gear")
    points, downsampled = downsample_channel(ch, spec, 0, 200, max_points=10)
    assert downsampled is False
    assert points == [[0, 1.0], [100, 2.0], [200, 3.0]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/udp/test_telemetry_index.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'f1re.udp.telemetry_index'`).

- [ ] **Step 3: Implement catalog, presets, `MetricChannel`, and `downsample_channel` in `src/f1re/udp/telemetry_index.py`**

설계서 5.1.A 표의 5개 카테고리(`driving`, `tyres_brakes`, `energy_fuel`, `lap_pace`, `damage_setup`)와 4륜 항목(`_fl/_fr/_rl/_rr`, `wheel_group` 필드 포함해 프런트에서 한 줄 4버튼 그룹 가능) 전체를 정의한다.
`TELEMETRY_PRESETS`는 4종(`주행 입력`, `타이어 · 브레이크`, `에너지 · 연료`, `랩 · 간격`)을 포함한다.
`downsample_channel()`은 `bisect_left`/`bisect_right`로 `[from_ms, to_ms]` 슬라이스 범위를 찾고 NaN을 건너뛴 뒤, 유효 점 개수가 `max_points` 이하이면 원본 점을 그대로(`downsampled=False`), 초과하면 `max_points // 2`개 버킷으로 분할해 각 버킷의 `(min_idx, max_idx)`(같은 인덱스면 1점, 다르면 인덱스 오름차순으로 최대 2점; `spec.step`이 `True`면 각 버킷 마지막 유효값)을 반환한다.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/udp/test_telemetry_index.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/f1re/udp/telemetry_index.py tests/udp/test_telemetry_index.py
git commit -m "feat(telemetry): 메트릭 카탈로그·프리셋 및 min/max 다운샘플러 추가"
```

---

### Task 2: 1회 스캔 인덱서·비교 차량 온디맨드 추출·10Hz 지도 프레임·LRU 스토어 (`telemetry_index.py` 2부)

**Files:**
- Modify: `src/f1re/udp/telemetry_index.py`
- Test: `tests/udp/test_telemetry_index.py`
- Reference fixture: `tests/` 내 기존 `.f1rec` 픽스처 (`recording-6-suzuka-race-snippet.f1rec` 또는 `tests/udp/`의 합성 패킷 빌더)

**Interfaces:**
- Consumes: Task 1의 `TELEMETRY_CATALOG`, `CarSeries`, `MetricChannel`, `get_metric_spec`, `downsample_channel`; 기존 `f1re.udp.recording.iter_recording`, `f1re.udp.parser.parse_packet`, `f1re.udp.web_replay`, `f1re.state.store.StateStore`, `f1re.pipeline.RacePipeline`, `f1re.alerts.rules.AlertRules`, `f1re.web.map_api.TrackMap`, `f1re.tracks.load_track`.
- Produces:
  - `TelemetrySessionIndex`:
    - `recording_id: int`, `duration_ms: int`, `track_id: int | None`, `player_index: int | None`
    - `participants: list[dict[str, Any]]` (22대 목록: `car_index`, `name`, `team_id`, `color`, `is_player`, `your_telemetry`)
    - `track: dict[str, Any]` (`TrackMap.track_payload(...)` 포맷)
    - `markers: dict[str, Any]` (`radio`, `events`, `intervals`, `laps`)
    - `map_frames: list[dict[str, Any]]` (100ms 단위 `{"t_ms": int, "cars": list, "flag": str, "flags": dict, "lap": int | None, "position": int | None}`)
    - `summary_payload() -> dict[str, Any]`
    - `query_series(metric_keys: list[str], from_ms: int, to_ms: int, max_points: int, compare_car: int | None) -> dict[str, Any]`
    - `map_frame_at(t_ms: int) -> dict[str, Any]`
    - `map_frames_range(from_ms: int, to_ms: int) -> list[dict[str, Any]]`
  - `build_telemetry_index(file_path: Path, recording_row: dict[str, Any], conn_factory: Callable[[], sqlite3.Connection] | None = None, *, progress_cb: Callable[[float], None] | None = None) -> TelemetrySessionIndex`
  - `extract_car_series(file_path: Path, car_index: int) -> CarSeries` (비교 차량 온디맨드 추출, 최대 1대 캐시)
  - `TelemetryIndexStore`:
    - `async def open_recording(self, recording_id: int, row: dict[str, Any], file_path: Path) -> str` (`"building" | "ready"`)
    - `def status_or_summary(self, recording_id: int) -> dict[str, Any]`
    - `def get_ready(self, recording_id: int) -> TelemetrySessionIndex | None`
    - `async def ensure_compare_car(self, index: TelemetrySessionIndex, car_index: int) -> None`
    - `def protected_recording_ids(self) -> set[int]` (현재 빌드 중이거나 캐시된 `recording_id`)

- [ ] **Step 1: Write the failing test**

`tests/udp/test_telemetry_index.py`에 다음 테스트를 추가한다(기존 `packer.py` + `write_recording` 또는 기존 회귀 `.f1rec` 픽스처 사용):
- `test_build_telemetry_index_extracts_series_map_frames_and_markers`: 참가자, 내 차 60Hz `CarTelemetry`/`Motion`/`LapData`/`CarStatus`/`CarDamage`, 10Hz `map_frames`, 세션 공통 `SC`/`VSC`/`RDFL` 구간(`intervals`) 및 내 차 `events`(`pit`/`penalty`/`overtake`/`flashback`/`lap_complete`)만 포함되고 관계없는 다른 차 20대의 일반 `lap_complete` 등이 스팸으로 들어가지 않는지 검증.
- `test_compare_car_respects_your_telemetry_privacy`: 비교 차량(`your_telemetry=0`)을 선택해 `query_series(["speed", "gap_front_s", "g_lat"], ...)`를 부르면 `speed`는 `compare_series is None` + `compare_restricted is True`, 반면 `gap_front_s`와 `g_lat`은 `compare_series` 배열과 `compare_restricted is False`로 내려오는지 검증.
- `test_telemetry_index_store_lru_eviction_and_protected_ids`: 서로 다른 2개 녹화를 순차 `open_recording` 하면 1건만 LRU 캐시에 남고 `protected_recording_ids()`가 최신 ID만 반환하는지 검증.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/udp/test_telemetry_index.py -v`
Expected: FAIL (`ImportError: cannot import name 'build_telemetry_index'`).

- [ ] **Step 3: Implement `build_telemetry_index`, `extract_car_series`, `TelemetrySessionIndex`, and `TelemetryIndexStore` in `src/f1re/udp/telemetry_index.py`**

구현 핵심:
- `iter_recording(file_path)` 1회 순회 중 `t_ms = (recv_ns - first_recv_ns) // 1_000_000`:
  - `StateStore` + `TrackMap` + `MemoryRadioSink` + `RacePipeline(conn=None)`(기존 `web_replay.py`의 경량 조합 재사용)로 무전 발화와 100ms 버킷(`t_ms // 100`)마다 `TrackMap.car_positions(snapshot)`·`flags`·`flag`·`lap`·`position` 수집.
  - 이벤트 마커는 `EventLog` 또는 패킷 직접 해석으로 `SCAR`(SC/VSC 시작~해제 구간), `RDFL`, `FLBK`, `PENA`, `OVTK`, `LapData.pit_status` 진입~이탈 구간, 랩 경계 `laps[]`를 수집하되 `player_index` 관련 사건 + 각 차량 경량 맵(최대 22대 pit/penalty/overtake)으로 정리해 기본은 세션 공통 + 내 차 + 선택 비교 차량만 노출.
  - F1 25 타이어 배열 순서(`0=RL, 1=RR, 2=FL, 3=FR`)와 화면 키(`_fl=2, _fr=3, _rl=0, _rr=1`) 매핑을 기존 `web/app.py` `_TYRE_DISPLAY_ORDER`와 동일하게 맞춘다.
- `TelemetryIndexStore`는 `recording_id`별 `asyncio.Lock` 및 백그라운드 `asyncio.Task`(`asyncio.to_thread(build_telemetry_index, ...)`)로 동시 `open` 요청을 병합하고, 새 녹화가 완료되면 직전 캐시 1건만 유지(LRU=1). 비교 차량 추출도 `compare_cache: tuple[int, CarSeries] | None`로 1대만 캐시하며 `index._compare_lock`으로 중복 스캔을 막는다.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/udp/test_telemetry_index.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/f1re/udp/telemetry_index.py tests/udp/test_telemetry_index.py
git commit -m "feat(telemetry): 1회 스캔 시계열 인덱서 및 비교 차량·10Hz 지도 프레임 스토어 구현"
```

---

### Task 3: `/api/replay/telemetry/*` API, `/telemetry` 라우트, `BackupWorker` 정리 보호 연동

**Files:**
- Modify: `src/f1re/web/replay_api.py`
- Modify: `src/f1re/web/app.py`
- Modify: `src/f1re/udp/backup.py` (로컬 정리 시 추가 보호 ID 조회 지원)
- Test: `tests/web/test_telemetry_api.py`

**Interfaces:**
- Consumes: Task 2의 `TelemetryIndexStore`, `TelemetrySessionIndex`; 기존 `replay_api._load_recording`, `_is_recording_in_progress`, `_ensure_local_file`, `create_replay_router`.
- Produces:
  - `POST /api/replay/telemetry/open` (`{"recording_id": int}`) -> `{"status": "building" | "ready", "recording_id": int}` (미존재 404, 진행 중 409)
  - `GET /api/replay/telemetry/summary?recording_id=<id>` -> `building` 시 `{"status": "building", "progress": float}`, 완료 시 `{"status": "ready", ...}`
  - `GET /api/replay/telemetry/series?recording_id=...&metrics=...&from_ms=...&to_ms=...&max_points=800&compare_car=...` -> `{"recording_id", "from_ms", "to_ms", "compare_car", "compare_your_telemetry", "series": {...}}`
  - `GET /api/replay/telemetry/map_frames?recording_id=...&t_ms=...` 또는 `&from_ms=...&to_ms=...` -> `{"frames": [...]}` (단건 `t_ms`일 때도 1건짜리 `frame` 및 `frames` 모두 혹은 통일된 `{"frame": ..., "frames": [...]}` 반환)
  - `GET /telemetry` -> `STATIC_DIR / "telemetry.html"` `FileResponse`

- [ ] **Step 1: Write the failing test**

`tests/web/test_telemetry_api.py`를 작성한다 (`FastAPI` + `TestClient`, 임시 SQLite DB + 합성 `.f1rec` 파일 + 스텁 `RecordingFetcher` 사용):
- `test_telemetry_open_summary_series_and_map_frames_end_to_end`: `open` → `summary`(`status == "ready"`, `catalog`, `presets`, `participants`, `track`, `markers`) → `series`(`speed`, `brake`, `compare_car`) → `map_frames`(단건 `t_ms` 및 구간 `[from_ms, to_ms]`) 응답 검증 + **그 직후 `GET /api/replay/state`가 여전히 `replay.active is False`인지(전역 `ReplayManager` 비간섭) 검증**.
- `test_telemetry_open_errors_404_and_409`: 없는 `recording_id`는 `404`, `end_reason == "recording"` 진행 중 녹화는 `409` 반환.
- `test_telemetry_series_rejects_unknown_metric_400`: 잘못된 `metrics=not_a_metric`은 `400` 반환.
- `test_get_telemetry_html_returns_200_even_when_engine_off`: 엔진 `off` 상태에서도 `GET /telemetry`가 `200 text/html` 반환.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/web/test_telemetry_api.py -v`
Expected: FAIL (404 Not Found on `/api/replay/telemetry/open` and `/telemetry`).

- [ ] **Step 3: Implement endpoints in `replay_api.py`, route in `app.py`, and retention protection in `backup.py`**

- `src/f1re/udp/backup.py`에서 `active_recording_id` 외에 선택적 `extra_protected_ids: Callable[[], Iterable[int]] | None = None` 인자를 받아 로컬 파일 정리 보호 목록에 합산하고, `app.py`가 `TelemetryIndexStore.protected_recording_ids`를 연결한다.
- `create_replay_router(..., telemetry_store: TelemetryIndexStore | None = None)`에 기본 인스턴스를 생성하거나 주입받아 `open`, `summary`, `series`, `map_frames` 4개 엔드포인트를 구현한다(`compare_car == player_index`인 경우는 `compare_car = None`으로 정규화, `max_points`는 `10..2000` 클램핑, `from_ms`/`to_ms` 클램핑).
- `src/f1re/web/app.py`에 `@app.get("/telemetry")`를 추가한다.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/web/test_telemetry_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/f1re/web/replay_api.py src/f1re/web/app.py src/f1re/udp/backup.py tests/web/test_telemetry_api.py
git commit -m "feat(web): /api/replay/telemetry/* API 및 /telemetry 라우트 추가"
```

---

### Task 4: `TrackMapView` 미리보기/비교차량 확장 및 대시보드 진입 UI

**Files:**
- Modify: `src/f1re/web/static/map.js`
- Modify: `src/f1re/web/static/dashboard.html`
- Modify: `src/f1re/web/static/dashboard.js`
- Modify: `src/f1re/web/static/style.css`

**Interfaces:**
- Consumes: 기존 `TrackMapView` (`src/f1re/web/static/map.js`).
- Produces:
  - `TrackMapView.setTrack(track, { skipLiftCoast = false })`: `/telemetry`처럼 라이브 엔진의 `/api/lift-coast` 폴링이 불필요할 때 백그라운드 503 요청을 안 날리도록 옵션 지원(기본값 `false`로 기존 `/` 유지).
  - `TrackMapView.setCompareCar(carIndex: number | null)`: 비교 차량 인덱스를 지정해 지도에서 청록/골드 점선 고리로 강조 표시.
  - `TrackMapView.render(payload)` 확장: `car.is_compare` 또는 `car.car === this.compareCarIndex`일 때 점선 고리+라벨(`비교`) 표시, 옵션 `previewLabel`이 있으면 지도 상단에 `"미리보기 12:34.2"` 배지 표시.
  - `dashboard.html` 상단·엔진 OFF 안내·녹화 목록 모달에 `텔레메트리 분석` 링크(`/telemetry`), `dashboard.js` `recordingItem()`에 `"그래프 분석"` 링크/버튼(`/telemetry?id=<id>`) 추가.

- [ ] **Step 1: Verify existing dashboard & replay tests before editing**

Run: `uv run pytest tests/web/ -v`
Expected: PASS (기존 웹 테스트 전체 통과 확인).

- [ ] **Step 2: Extend `TrackMapView` in `src/f1re/web/static/map.js`**

기존 `_drawCars` 및 `setTrack` 동작을 보존하면서 `skipLiftCoast` 옵션, `setCompareCar(carIndex)`, `setPreviewLabel(text | null)`(또는 `render(payload)` 내 `payload.preview_label`)을 추가한다.

- [ ] **Step 3: Add `/telemetry` entry points to `dashboard.html`, `dashboard.js`, `style.css`**

- `dashboard.html`: 상단 우측 컨트롤 영역(`header` 또는 상태줄)과 엔진 OFF 안내 카드에 `<a href="/telemetry">` `"텔레메트리 분석"` 버튼 추가.
- `dashboard.js`: `recordingItem()`의 `actions` 영역에 `downloadable`과 동일한 기준(또는 `recording.end_reason !== "recording" && downloadable`)으로 `"그래프 분석"` `<a class="recording-analyze" href="/telemetry?id=...">` 버튼 추가.
- `style.css`: `.recording-analyze` 및 상단 링크 스타일 추가.

- [ ] **Step 4: Run web tests & check static files serve cleanly**

Run: `uv run pytest tests/web/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/f1re/web/static/map.js src/f1re/web/static/dashboard.html src/f1re/web/static/dashboard.js src/f1re/web/static/style.css
git commit -m "feat(ui): TrackMapView 비교·미리보기 확장 및 대시보드 /telemetry 진입 추가"
```

---

### Task 5: `/telemetry` 3단 레이아웃·계층 체크박스·프리셋·독립 재생 컨트롤 (`telemetry.html`, `telemetry.css`, `telemetry.js` 1부)

**Files:**
- Create: `src/f1re/web/static/telemetry.html`
- Create: `src/f1re/web/static/telemetry.css`
- Create: `src/f1re/web/static/telemetry.js`

**Interfaces:**
- Consumes: Task 3 APIs (`/api/recordings`, `/api/replay/telemetry/open`, `/summary`, `/map_frames`) & Task 4 `TrackMapView`.
- Produces:
  - 3단 CSS Grid 레이아웃 (`style.css` 다크 테마 토큰 재사용):
    - 상단 바: `← 대시보드`, 녹화 드롭다운, 비교 차량 드롭다운(비공개 드라이버에 `(비공개)` 표기 및 상단 안내 배지 자리), 재생 컨트롤(`▶/⏸`, `1x/2x/5x/10x`, 타임라인 시크바, `mm:ss / mm:ss`, `[줌 초기화]`, `[자동 따라가기]`, 마커 토글 `무전`·`사건`).
    - 좌측 상단: 프리셋 버튼 바 + Canvas 2D 차트 래퍼 + 툴팁 오버레이 + 로딩/인덱싱 진행률 오버레이.
    - 좌측 하단: 2단계 계층 체크박스(`카테고리 → 항목`, 4륜 한 줄 그룹 `[전좌][전우][후좌][후우]`, `checked`/`indeterminate`, 접기/펼치기, `(선택/전체)`, 색상 스와치, 커서 시점 현재 값 표시, `localStorage` 상태 저장).
    - 우측: `TrackMapView` 컨테이너 + 호버 미리보기 배지 + 코너/시점 요약.
  - `telemetry.js` 내 `ReplayClock` & `MapFrameBuffer`:
    - URL `?id=<id>&t=<ms>` 파싱 → `open` → `summary` 300ms 폴링 → `0:00`(또는 `?t`) 정지 상태로 초기화.
    - 10Hz `map_frames`를 30초 청크(`30_000ms`) 단위로 프리페치하고 다음 청크를 선행 로드(prefetch)해 `requestAnimationFrame` 로컬 재생 루프가 끊김 없이 지도를 갱신(단, 호버 미리보기 중일 때는 호버 프레임을 우선 그림).

- [ ] **Step 1: Add minimal route/HTML content check in `tests/web/test_telemetry_api.py`**

`GET /telemetry` 응답 본문에 핵심 DOM ID(`#telemetry-chart`, `#metric-tree`, `#telemetry-map`, `#recording-select`, `#compare-select`)와 `telemetry.js`, `map.js` 스크립트 참조가 포함되는지 검증하는 테스트를 추가하고 실패를 확인한다.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/web/test_telemetry_api.py -k telemetry_html -v`
Expected: FAIL (`telemetry.html` 미존재 또는 필수 ID 누락).

- [ ] **Step 3: Create `telemetry.html`, `telemetry.css`, and `telemetry.js` (Part 1: layout, catalog tree, presets, open/summary polling, local 10Hz playback & chunk prefetch)**

`telemetry.js`는 차트 렌더러를 별도 내부 클래스/객체(`TelemetryChart` 또는 모듈 내 함수 묶음)로 깔끔히 분리해 Task 6에서 그래프/마커/줌/호버 상호작용을 바로 연결할 수 있게 구성한다.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/web/test_telemetry_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/f1re/web/static/telemetry.html src/f1re/web/static/telemetry.css src/f1re/web/static/telemetry.js tests/web/test_telemetry_api.py
git commit -m "feat(ui): /telemetry 3단 레이아웃·계층 메트릭 트리·독립 10Hz 재생 구현"
```

---

### Task 6: Canvas 2D 다중 축 그래프·무전/사건 마커·줌/팬·양방향 호버/클릭 완성 (`telemetry.js` 2부 + 전체 검증)

**Files:**
- Modify: `src/f1re/web/static/telemetry.js`
- Modify: `src/f1re/web/static/telemetry.css`

**Interfaces:**
- Consumes: Task 3 `/api/replay/telemetry/series`, `/api/replay/telemetry/map_frames?t_ms=...`; Task 5의 `ReplayClock`, `MapFrameBuffer`, 메트릭 선택 상태, `TrackMapView`.
- Produces:
  - 다중 단위 정규화 Canvas 2D 그래프:
    - 선택된 메트릭의 `unit_group`별로 0~100% 높이 정규화(같은 단위는 Y축 공유, 좌·우 최대 2개 대표 Y축 눈금 표시 + 색 구분).
    - 내 차는 실선, 비교 차량은 같은 색 점선(`setLineDash([5, 4])`), `step: true` 항목은 계단형 선.
    - 비교 차량의 `compare_restricted === true` 메트릭은 범례/배지로 `"비공개 텔레메트리"` 안내.
  - 상단 마커 레인 & 랩 경계:
    - 랩 시작 수직선(`L1`, `L2`…), SC/VSC/적기·피트 반투명 구간 밴드, 무전(🔊)·사건(🚩/↺/⚠️) 마커. `무전 마커 표시` / `사건 마커 표시` 토글 연동 및 마 Marker 호버 상세 툴팁.
  - 줌/팬 & 60Hz 재조회:
    - 마우스 드래그 구간 선택 → `[t1, t2]` 확대, 마우스 휠 → 커서 중심 줌인/아웃, `Shift+드래그` 팬 이동, 더블클릭 또는 `[줌 초기화]` → 전체 복귀. 뷰포트 변경 시 120ms 디바운스로 `/api/replay/telemetry/series` 재요청(확대 구간 60Hz 수신).
    - 확대 상태에서 재생 시 커서가 오른쪽 끝을 넘으면 자동 따라가기(`Auto-follow`) 옵션에 따라 뷰포트 스크롤.
  - 양방향 연동:
    - 그래프 호버 → 수직 점선 + 호버 툴팁(내 차 값 · 비교 값 · 차이 `Δ`) + 하단 체크박스 옆 현재 값 갱신 + `MapFrameBuffer` 또는 `/map_frames?t_ms=`(50ms 디바운스)로 우측 `TrackMapView` 미리보기. 그래프에서 마우스 이탈 시 현재 재생 커서 위치 복원.
    - 그래프 단순 클릭(드래그 거리 < 5px) → `ReplayClock.seek(t_ms)`로 **즉시(0ms)** 커서 및 지도 이동.

- [ ] **Step 1: Implement Canvas 2D multi-axis renderer, markers, zoom/pan, and hover/click sync in `telemetry.js`**

설계서 5.2의 모든 상호작용을 구현한다. (DPR 대응, 빈 메트릭 선택 안내, 비공개 배지, 마우스 이탈 복귀 포함)

- [ ] **Step 2: Run full backend pytest suite**

Run: `uv run pytest`
Expected: PASS (모든 기존 테스트 및 신규 텔레메트리 테스트 통과).

- [ ] **Step 3: Smoke check with `f1re` app / fixture recording via Python script or TestClient (no live server alteration)**

`uv run python -c ...` 또는 `pytest`로 실제 회귀 녹화 픽스처(`tests/` 내 `.f1rec` 파일이 있다면 포함)에 대해 `TelemetryIndexStore` + `replay_api` 전체 플로우 에러 0건 확인.

- [ ] **Step 4: Commit**

```bash
git add src/f1re/web/static/telemetry.js src/f1re/web/static/telemetry.css
git commit -m "feat(ui): Canvas 2D 다중 축 그래프·마커·줌·양방향 지도 미리보기 구현"
```

---

## Self-Review Check
- **Spec coverage**: 3단 레이아웃(Task 5), 시간축·독립 재생 커서·청크 10Hz `map_frames`(Task 2, 3, 5), 5개 카테고리·프리셋·4륜 그룹 계층 체크박스(Task 1, 5), 비교 차량 점선 + 비공개 `your_telemetry == 0` 부분 억제(Task 1, 2, 6), 무전·범위 제한 사건·구간 마커(Task 2, 6), 줌/팬 60Hz 재조회(Task 1, 3, 6), 호버 미리보기·클릭 시크(Task 4, 6), `Engine` OFF·`ReplayManager` 비간섭·`BackupWorker` 보호(Task 2, 3) 모두 1:1 매핑됨.
- **No placeholders / consistent names**: `TelemetryMetricSpec`, `TELEMETRY_CATALOG`, `TELEMETRY_PRESETS`, `TelemetrySessionIndex`, `TelemetryIndexStore`, `open`/`summary`/`series`/`map_frames` 이름이 모든 Task에서 동일함.
