# 다시 보기 텔레메트리 분석 화면 설계

**작성일**: 2026-11-15  
**대상**: `f1-race-engineer` (`src/f1re/udp/`, `src/f1re/web/`)  
**상태**: 빌더 검증 반영 완료 (사용자 최종 승인 대기)

---

## 1. 목적과 배경

기존 다시 보기 기능([dashboard.html](../../src/f1re/web/static/dashboard.html), [web_replay.py](../../src/f1re/udp/web_replay.py))은 실시간 대시보드를 그대로 재현하여 **현재 재생 시점의 순간 스냅샷**만 보여 준다. 하지만 주행 복기에서는 "35분 레이스 중 언제 타이어 온도가 치솟았는지", "무전이 울린 순간 스로틀·브레이크·조향이 어땠는지", "앞차와 비교해 어디서 차이가 났는지"를 **전체 시간 흐름 속에서 훑고, 특정 코너 구간만 확대해 서킷 지도 위치와 함께 확인**할 수 있어야 한다.

이를 위해 기존 대시보드(`/`)와 무전 페이지(`/radio`)와 나란히 동작하는 **다시 보기 전용 텔레메트리 분석 화면(`/telemetry`)** 을 추가한다.

---

## 2. 목표와 비목표

### 목표
1. **3단 분석 레이아웃**:
   - **상단**: 녹화 선택 · 비교 차량 선택 · 재생 컨트롤 바(재생/일시정지 · `1x/2x/5x/10x` · 시크바 · 시간)
   - **좌측 상단**: 선택한 메트릭의 **세션 전체 시간(`초/분`) 그래프** + 무전/사건 마커 + 드래그/휠 구간 확대(줌) + 원클릭 프리셋
   - **좌측 하단**: **카테고리 → 항목 2단계 계층 체크박스** (부분 선택 `indeterminate`, 접기/펼치기, 색상·단위·커서 값 표시)
   - **우측**: 기존 서킷 지도([map.js](../../src/f1re/web/static/map.js) `TrackMapView`) 재사용
2. **양방향 연동**:
   - 그래프 호버 시 해당 시점의 차량 위치를 우측 서킷 지도에 미리보기로 표시(재생 루프를 흔들지 않음).
   - 그래프 클릭 시 해당 시점으로 리플레이 시크(`seek`).
   - 재생이 진행되면 그래프 위의 **재생 커서**와 우측 지도가 함께 이동.
3. **다른 차량 겹쳐 보기**: 비교 차량(드라이버 1대, 예: P1 선두, 현재 앞차, 특정 참가자)을 선택하면 내 차는 실선, 비교 차량은 같은 색 점선으로 같은 시간축에 겹쳐 그린다.
4. **엔진 ON/OFF 무관 동작**: 기존 다시 보기 API와 마찬가지로 엔진이 꺼져 있어도 녹화 목록 조회·GCS 복원·시계열 분석·재생이 모두 동작한다.

### 비목표 (이번 범위 제외)
- **거리(`m`) 기준 랩 정렬·겹쳐 보기 모드**: 이번 설계는 세션 전체 시간축(`초/분`)에 집중한다. (향후 확장을 막지 않되 이번 UI/API 범위에서는 제외)
- **3대 이상 동시 비교**: 내 차 + 비교 차량 최대 1대까지만 겹쳐 그린다.
- **실시간 주행 중 라이브 그래프**: 실시간 주행(`/`)에는 붙이지 않고 다시 보기 전용으로 둔다.
- **외부 JS 차트 라이브러리 / 프런트 빌드 도구 도입**: 레포 규칙대로 빌드 없는 순수 HTML/CSS/JS + Canvas 2D만 사용한다.

### 안티골 (하지 말아야 할 것)
- 수십만 패킷(`.f1rec.zst` 160MB+)을 브라우저로 통째로 전송해 메모리를 터뜨리지 않는다.
- 그래프 위에서 마우스를 움직일 때마다 `ReplayManager.seek()`를 호출해 35분 패킷을 재구성하지 않는다.
- 기존 `/` 대시보드의 실시간·다시 보기 동작과 `/radio` 페이지를 깨뜨리지 않는다.

---

## 3. 제약 사항

- **플랫폼**: 리눅스 홈서버(`i5-12500T · 32GB RAM`), Python 3.14+.
- **정적 프런트 원칙**: 번들러/외부 CDN 없이 순수 HTML/JS/CSS로 동작해야 하며, 메트릭 정의·라벨·색/위험 기준의 단일 출처는 서버 API가 가진다.
- **메모리·응답성**: 녹화 1건 인덱싱은 이벤트 루프를 막지 않도록 스레드(`asyncio.to_thread`)에서 실행하고, 서버 메모리에 **최근 열린 녹화 1건**(열린 내 차 + 필요 시 비교 차량 1대)만 캐시해 32GB 서버에서 수십 MB 수준으로 제한한다.
- **공개 레포·보안 규약**: 시크릿 값, 상용 앱 분석 자료 금지. 모든 UI·문서·주석은 한국어로 작성한다.

---

## 4. 구현 접근 방식 비교 및 선택

### 검토한 후보
1. **방안 A (선택): 서버 1회 스캔 시계열 인덱스 + 구간 동적 다운샘플 API + 순수 Canvas 2D 차트**
   - 녹화를 열면 서버가 `.f1rec(.zst)`를 1회 순차 스캔해 내 차 60Hz 메트릭 컬럼 + 10Hz 차량 22대 지도/순위 스냅샷 + 무전/사건 타임라인을 메모리에 인덱스한다. 비교 차량을 고른 경우에만 해당 차량 1대의 컬럼을 추가 추출한다.
   - 브라우저는 보고 있는 시간 구간 `[from_ms, to_ms]`와 선택된 `metrics[]`, 캔버스 픽셀 수(`max_points`)만 요청해, 전체 보기에서는 가볍게 요약(min/max 보존)되고 10~20초 구간으로 줌인하면 60Hz 원시 정밀도로 그려진다.
2. **방안 B (미선택): 고정 1~2Hz 요약 일괄 응답 + CDN 차트 라이브러리**
   - 구현은 단순하지만 ① 줌인 시 브레이크/스로틀/G-포스의 짧은 순간이 뭉개지고, ② 외부 CDN 의존성이 오프라인·무빌드 정적 프런트 원칙과 어긋난다.
3. **방안 C (드롭): 브라우저에서 `.f1rec.zst` 직접 파싱**
   - 160MB+ 전송과 F1 25 바이너리 파서 중복은 구조상 부적합하다.

**선택**: **방안 A**. 전체 세션 조망(35분)과 코너 순간 확대(10초)를 한 화면에서 지연 없이 오가고, 호버 미리보기를 전역 리플레이 엔진(`ReplayManager`) 상태와 무관하게 즉시 처리할 수 있는 구조다.

---

## 5. 상세 아키텍처

```text
브라우저 (/telemetry)                         서버 (FastAPI + replay_api.py)
───────────────────────────────────           ─────────────────────────────────────────
GET /telemetry[?id=<id>&t=<ms>]               telemetry.html + telemetry.js + map.js
  │
  ├─ GET /api/recordings                      session_recordings 목록
  ├─ POST /api/replay/telemetry/open          RecordingFetcher(로컬/GCS) → TelemetryIndex 캐시 빌드
  ├─ GET /api/replay/telemetry/summary        메트릭 카탈로그·참가자[22]·무전/사건 마커·트랙 윤곽
  ├─ GET /api/replay/telemetry/series         [from_ms..to_ms] 구간 min/max 다운샘플 (+ compare_car)
  └─ GET /api/replay/telemetry/map_frames     단일 시각(t_ms) 또는 구간([from_ms..to_ms]) 10Hz 지도 프레임
                                              → 프런트엔드 로컬 재생 타이머·호버가 독립 구동 (ReplayManager 비간섭)
```

### 5.1 백엔드: 시계열 인덱스 (`src/f1re/udp/telemetry_index.py` 신규)

#### A. 메트릭 카탈로그 (`TELEMETRY_CATALOG`)
서버가 카테고리 순서·항목 키·한국어 이름·단위·그룹·값 범위·차량 비교 가능 여부(`restricted_when_private`)를 단일 정의로 관리한다. (`CarTelemetry`·`CarStatus`·`CarDamage` 원천 항목만 `restricted_when_private = true`, `LapData`·`Motion` 원천 항목은 `false`.)

| 카테고리 ID | 카테고리명 | 주요 항목 (`metrics` 키) | 단위 | 원천 패킷 |
|---|---|---|---|---|
| `driving` | 주행 입력 | `speed` (속도), `throttle` (스로틀), `brake` (브레이크), `steer` (조향), `gear` (기어), `engine_rpm` (RPM), `drs` (DRS), `clutch` (클러치), `g_lat`/`g_lon`/`g_vert` (횡/종/수직 G) | `km/h`, `%`, `-1..1`, `단`, `RPM`, `0/1`, `G` | `CarTelemetry`, `Motion` |
| `tyres_brakes` | 타이어 · 브레이크 | 4륜(`fl/fr/rl/rr`)별 `tyre_surface_temp` (표면 온도), `tyre_inner_temp` (내심 온도), `tyre_pressure` (공기압), `tyre_wear` (마모), `brake_temp` (브레이크 온도) *(4륜 일괄/개별 선택 지원)* | `°C`, `psi`, `%` | `CarTelemetry`, `CarDamage` |
| `energy_fuel` | 에너지 · 연료 | `ers_pct` (ERS 잔량 %), `ers_deployed_pct` (이번 랩 사용 %), `ers_mguk_j` / `ers_mguh_j` (회수), `ers_mode` (배포 모드), `fuel_kg` (연료량), `fuel_remaining_laps` (여유 랩), `engine_temp` (엔진 온도) | `%`, `MJ`, `kg`, `랩`, `°C` | `CarStatus`, `CarTelemetry` |
| `lap_pace` | 랩 · 레이스 간격 | `current_lap` (랩 번호), `current_lap_time_s` (랩 경과 시간), `gap_front_s` (앞차 간격), `gap_leader_s` (선두 간격), `car_position` (순위), `safety_car_delta` (SC 델타) | `랩`, `s`, `위` | `LapData` |
| `damage_setup` | 손상 · 밸런스 | `wing_fl_damage`, `wing_fr_damage`, `rear_wing_damage`, `floor_damage`, `diffuser_damage`, `sidepod_damage`, `gearbox_damage`, `engine_damage`, `brake_bias` | `%` | `CarDamage`, `CarStatus` |

- 계층 구조: **카테고리 → 항목** (4륜 항목은 `"타이어 표면 온도 (앞좌/앞우/뒤좌/뒤우)"`처럼 항목 아래 4바퀴를 묶어 체크하거나 낱개로 켤 수 있는 서브그룹 또는 `[FL, FR, RL, RR]` 4개 항목으로 배치 — 카테고리 체크박스는 하위 전체 선택/`indeterminate`/전체 해제를 지원).

#### B. 1회 스캔 인덱싱 (`build_telemetry_index(file_path, recording_meta, conn_factory)`)
녹화 파일을 `iter_recording()`으로 처음부터 끝까지 1회 순차 읽기 하면서 다음 4가지를 동시에 수집한다.

1. **기본 메타 & 참가자**: `first_recv_ns`, `duration_ms`, `track_id`, `player_index`, `participants[22]` (`name`, `team_id`, `ai_controlled`, `your_telemetry`).
2. **내 차 원시 시계열 (`CarSeries`)**:
   - `CarTelemetry`(60Hz), `Motion`(60Hz), `LapData`, `CarStatus`, `CarDamage`가 도착할 때마다 각 패킷 스트림 또는 공통 샘플 타임라인에 `t_ms = (recv_ns - first_recv_ns) // 1_000_000`과 값을 `array('d')` / `numpy.float32` 배열로 저장한다(NaN으로 미수신 구분).
   - 메모리: 35분 60Hz(약 126,000샘플) × ~38개 `float32` 채널 ≈ **약 19MB**.
3. **10Hz 지도·순위 타임라인 (`map_frames`)**:
   - `100ms` 버킷마다 22대 `(lap_distance, current_lap_num, car_position, pit_status, result_status)`와 `flags` 요약(`yellow_zones`, `safety_car_status`, `red_flag`)을 기록한다(약 21,000프레임 × 22대 압축 구조 ≈ **수 MB**).
   - `TrackMap`으로 임의 `t_ms`의 좌표를 즉시 계산할 수 있어, 그래프 호버 시 `ReplayManager.seek()` 없이 지도 미리보기를 응답한다.
4. **무전 & 사건 마커 타임라인 (`markers`)**:
   - `StateStore` + `RacePipeline(conn=None)` + `AlertRules` + `MemoryRadioSink`를 1회 통과시켜 전체 세션의:
     - **무전 마커**: `MemoryRadioLog`에 쌓인 모든 발화(`t_ms`, `kind`, `mood`, `text`, `corner`)
     - **사건 마커** *(범위 제한 — 22대 전체 사건 제외)*:
       - 세션 공통 사건: `safety_car`, `vsc`, `red_flag` (발생~해제를 하나의 구간 `start_ms..end_ms` 밴드로 묶어 기록)
       - 차량 단위 사건: `flashback`, `pit_enter`~`pit_exit`(구간), `penalty`, `overtake`(`car_index` 추월 또는 `being_overtaken_vehicle_idx` 피추월 모두), `lap_complete` — **내 차(`player_index`) 관여 사건**을 기본 수집하고, 비교 차량 선택 시 쓸 수 있도록 `car_index`·`other_car_index`를 함께 저장해 API/프런트에서 내 차 및 선택 비교 차량 사건만 표시한다.

#### C. 비교 차량 (`compare_car_index`) 지연 추출 및 비공개 처리
- 처음 오픈 시에는 내 차만 추출해 열기 속도를 빠르게 유지한다.
- 사용자가 비교 차량(`compare_car` != 내 차)을 선택하면, 해당 차량 인덱스 1대의 `CarSeries`만 녹화 파일에서 1회 스캔해 `index.compare_cache`에 캐시한다(최대 1대만 유지해 메모리 상한 고정).
- **비공개 텔레메트리 처리 (`your_telemetry == 0`)**: F1 25 규격상 비공개 드라이버도 `LapData`(간격·순위·랩 타임)와 `Motion`(위치·G-포스)은 정상 수신되고 `CarTelemetry`·`CarStatus`·`CarDamage`만 0/제한된다. 따라서 `restricted_when_private=True` 메트릭만 `compare_series: null`, `compare_restricted: true`로 억제해 잘못된 `0` 그래프를 방지하고, `LapData`/`Motion` 메트릭은 정상 비교하며 상단에 안내 배지를 띄운다.
- 비교 차량의 사건 마커가 필요한 경우, 요청 시 해당 차량의 `pit`·`lap_complete`·`overtake`·`penalty` 이벤트를 함께 반환(또는 1회 스캔 시 차량 인덱스별 경량 이벤트 맵 보관)하고 프런트에서 토글한다.

#### D. 구간 다운샘플링 (`/api/replay/telemetry/series`)
- 입력: `recording_id`, `metrics` (쉼표 구분), `from_ms` (기본 0), `to_ms` (기본 `duration_ms`), `max_points` (기본 800, 상한 2000), `compare_car` (선택 `int`).
- 처리:
  - `[from_ms, to_ms]` 범위의 원본 샘플 수가 `max_points` 이하이면 **원본 60Hz 점 그대로** 내려준다(`downsampled: false`).
  - 초과하면 구간을 `max_points // 2`개 버킷으로 나누어 각 버킷의 **최솟값(min)과 최댓값(max) 포인트(시간 순서 유지)** 를 뽑는다(`downsampled: true`). 따라서 35분 전체를 봐도 급브레이킹·기어 변속 스파이크가 깎이지 않는다.
  - 이산/단계 값(`gear`, `drs`, `car_position`, `current_lap`, `ers_mode`)은 계단형 렌더링 힌트(`step: true`)와 함께 버킷 마지막 유효값/최빈 변화점을 유지한다.

#### E. API 명세 (`src/f1re/web/replay_api.py` 확장)
모든 경로는 기존 `/api/replay/*` 아래에 두며, `Engine` ON/OFF 및 전역 `ReplayManager` 재생 상태와 완전히 독립적으로 동작한다.

1. `POST /api/replay/telemetry/open` (`{"recording_id": int}`)
   - 로컬/GCS에서 녹화 확보(`RecordingFetcher`), 백그라운드 인덱스 빌드 시작(또는 캐시 히트). 반환: `{"status": "building" | "ready", "recording_id": int}`.
2. `GET /api/replay/telemetry/summary?recording_id=<id>`
   - 준비 중이면 `{"status": "building", "progress": 0.0..1.0}`, 완료 시:
     - `recording` (이름, 트랙, 범주, 길이 `duration_ms`)
     - `catalog` (카테고리 → 항목 목록, `restricted_when_private` 플래그 포함)
     - `presets` (원클릭 프리셋 정의)
     - `participants` (22대 목록: `car_index`, `name`, `team_id`, `color`, `is_player`, `your_telemetry`)
     - `track` (기존 `/api/replay/track`과 동일 포맷 — `outline`, `corners`, `team_colors`)
     - `markers` (`radio[]`, `events[]`, `intervals[]`(SC/VSC/피트 구간), `laps[]` 랩 시작/완료 시간 경계)
3. `GET /api/replay/telemetry/series?recording_id=...&metrics=...&from_ms=...&to_ms=...&max_points=800&compare_car=...`
   - 선택된 메트릭별 `{series: [[t_ms, v], ...], compare_series: [[t_ms, v], ...] | null, unit, step, compare_restricted}` 반환.
4. `GET /api/replay/telemetry/map_frames?recording_id=...&t_ms=...` (또는 `&from_ms=...&to_ms=...`)
   - 단건 `t_ms` 요청 시 해당 시각 10Hz 스냅샷 1건 반환(호버/즉시 점프용).
   - 구간 `[from_ms, to_ms]` 요청 시 해당 구간의 10Hz 프레임 배열(`cars[]` 지도 좌표, `flags`, `flag`, `lap`, `position`)을 반환(브라우저 재생 버퍼 청크 프리페치용, 예: 30~60초 단위).

> **참고**: `ReplayManager.active_recording_id`뿐 아니라 `TelemetryIndexStore`가 캐시 중인 `recording_id`도 `BackupWorker`의 로컬 정리 보호 대상에 포함하거나, `open` 직후 삭제되지 않게 확인한다.

---

### 5.2 프런트엔드: `/telemetry` 화면

- **파일 구성** (`src/f1re/web/static/`):
  - `telemetry.html`, `telemetry.js`, `telemetry.css` (기존 `style.css` 테마 토큰 공유 + 3분할 그리드 전용 스타일), `map.js` 재사용.
  - `src/f1re/web/app.py`에 `GET /telemetry` 라우트 추가.
  - `dashboard.html` 상단 상태줄·엔진 OFF 안내·다시 보기 목록 모달에 `텔레메트리 분석` 링크/버튼 추가 (목록의 각 녹화 행에도 `"그래프 분석"` 버튼을 두어 해당 `recording_id`를 쿼리 파라미터 `/telemetry?id=<id>`로 바로 열 수 있음).
- **초기 진입 및 URL 쿼리 동작**:
  - `/telemetry?id=<id>`로 진입 시 해당 녹화를 즉시 `open`하고 `0:00` 정지 상태에서 요약·시리즈·첫 청크 지도 프레임을 로드한다. `?t=<ms>`가 함께 오면 해당 시각으로 커서를 초기화한다.
  - `?id` 없이 `/telemetry`만 열면 `recordings` 중 가장 최근 녹화(또는 대시보드에서 재생 중인 녹화가 있으면 안내와 함께 선택 드롭다운)를 열어 준다.
  - `/telemetry` 내부의 재생(`▶/⏸`, `1x/2x/5x/10x`, 타임라인 시크)은 브라우저 로컬 `requestAnimationFrame`/타이머와 청크 프리페치된 10Hz `map_frames`로만 동작하므로 대시보드(`/`)의 `ReplayManager` 재생을 멈추거나 덮어쓰지 않는다.

#### 화면 배치 (텍스트 목업)

```text
+------------------------------------------------------------------------------+
| ← 대시보드 | 녹화: [스즈카 레이스 #6 ▼] | 비교: [없음 / P1 홍길동 ▼]         |
| [▶/⏸] [1x 2x 5x 10x] [==========●==================] 12:34 / 35:10 [줌 초기화] |
+---------------------------------------------+--------------------------------+
| [주행 입력] [타이어·브레이크] [ERS·연료] [간격] | 서킷 지도 (TrackMapView)      |
| ─────────────────────────────────────────── |                                |
| 🔊무전  🚩SC/황기   ↺플래시백  ┃(재생) ┆(호버) |      ╭──── T1               |
| 100% ┼──/\────/\───\───────/───\───────     |     /   ● 내차   ○ 앞차        |
|  50% ┼─/  \──/  \───\─────/─────\──────     |    ╰─────────╮                |
|   0% ┼──────■────■───\___/───────\_____     |               ╰──╯             |
|      12:00   12:10   12:20   12:30          | [코너 정보 / 호버 시점 차량 상태] |
+---------------------------------------------+--------------------------------+
| 메트릭 선택 (체크 시 즉시 그래프 반영)          |                                |
| ▼ ☑ 주행 입력 (3/11)                         |                                |
|     ☑ 속도 (km/h) ■  ☑ 스로틀 (%) ■  ☑ 브레이크 (%) ■  ☐ 조향  ☐ 기어 ...      |
| ▶ ☐ 타이어 · 브레이크 (0/20)                  |                                |
| ▶ ☐ 에너지 · 연료 (0/8)                      |                                |
| ▶ ☐ 랩 · 레이스 간격 (0/6)                    |                                |
| ▶ ☐ 손상 · 밸런스 (0/9)                      |                                |
+---------------------------------------------+--------------------------------+
```

#### 주요 UI 동작
1. **계층 체크박스 (좌측 하단)**:
   - 카테고리 헤더에 체크박스(`checked` / `indeterminate` / `unchecked`) + 접기/펼치기 화살표 + 선택 개수 `(선택/전체)`.
   - 타이어·브레이크처럼 바퀴 4개가 있는 항목은 **한 줄 그룹**(`표면 온도 [전좌][전우][후좌][후우]`) 또는 하위 펼침으로 배치해 세로 공간을 과도하게 먹지 않으면서도 바퀴별 개별 선택이 가능하도록 한다.
   - 마지막 선택 항목·프리셋은 `localStorage`에 저장해 새로고침해도 유지한다.
2. **Canvas 2D 그래프 (좌측 상단)**:
   - **축 스케일 처리**: 단위가 다른 메트릭(예: `속도 0~350 km/h` + `스로틀 0~100 %` + `조향 -1~1`)을 함께 켰을 때 찌그러지지 않도록, **각 단위 그룹별 독립 정규화(0~100% 높이 매핑)** 를 기본으로 하고(같은 단위는 같은 축 공유, 예: 스로틀·브레이크는 같은 0~100% 축 / 4륜 온도는 같은 °C 축), 좌·우 Y축 눈금 및 마우스 호버 툴팁에는 **원래 단위의 실제 값**(내 차 + 비교 차량 + 차이 `Δ`)을 정확히 표시한다.
   - **무전 & 사건 마커**:
     - 그래프 상단 레인에 종류별 아이콘/색 막대(무전 🔊, SC/VSC/적기 🚩, 피트 🔧, 플래시백 ↺, 페널티 ⚠️) 및 랩 경계 수직선(`L1`, `L2`…) 표시.
     - 필터 토글(`무전 마커 표시`, `사건 마커 표시`)로 복잡할 때 켜고 끌 수 있으며, 마커에 마우스를 올리면 상세 내용(무전 문구 전체, 페널티/추월/플래시백 사유)을 툴팁으로 보여 준다.
   - **구간 확대 (줌 & 팬)**:
     - 그래프 위에서 마우스 **드래그**로 `[t1, t2]` 구간을 선택하면 즉시 확대되고, **마우스 휠**로 커서 중심 줌인/아웃, **Shift+드래그**(또는 확대 상태에서 좌우 스크롤/버튼)로 이동, **더블클릭 또는 `[줌 초기화]`** 로 전체 세션 보기로 복귀한다.
     - 확대하면 서버 `/series`가 새 `[from_ms, to_ms]`로 다시 호출되어 해상도가 자동으로 높아진다(60Hz까지).
   - **커서 연동**:
     - **마우스 호버**: 그래프 위 수직 점선 + 툴팁 수치 표시 + 우측 지도에 해당 시점 차량 위치 미리보기(`map_frames` 단건/캐시 조회, `requestAnimationFrame` / 50ms 디바운스로 과요청 방지). 마우스가 그래프를 벗어나면 지도는 다시 현재 로컬 재생 커서 위치로 돌아온다.
     - **그래프 클릭** (드래그가 아닌 클릭): 로컬 재생 커서를 해당 시점 `t_ms`로 **즉시(0ms) 이동**하고 지도·하단 값 표시를 함께 갱신한다.
     - **로컬 재생 동기화**: 프런트 재생 루프(`requestAnimationFrame`)가 `1x/2x/5x/10x` 배율로 커서 `position_ms`를 전진시키며 그래프 커서와 우측 지도(`map_frames` 버퍼)를 함께 갱신한다. (확대 뷰에서 커서가 화면 오른쪽 밖으로 나가면 자동 따라가기 토글 옵션 지원)
3. **우측 서킷 지도 (`TrackMapView` 확장)**:
   - 기존 `TrackMapView`를 그대로 쓰되, 외부(`telemetry.js`)에서 스냅샷 프레임(`cars`, `flags`, `flag`)을 직접 주입해 그릴 수 있는 메서드(또는 호버 미리보기 배지 `"미리보기 12:34.2"`)를 가볍게 추가한다.
   - 비교 차량이 선택되어 있으면 지도 위에서 비교 차량도 테두리/라벨로 강조해 내 차와의 위치 차이를 바로 볼 수 있다.

---

## 6. 기존 코드 변경·추가 범위

| 구분 | 파일 | 역할 |
|---|---|---|
| 신규 | `src/f1re/udp/telemetry_index.py` | 1회 스캔 인덱서(`build_telemetry_index`), 메트릭 카탈로그, 비교 차량 온디맨드 추출, min/max 다운샘플러, 10Hz 지도 스냅샷 구간/단건 조회 |
| 수정 | `src/f1re/web/replay_api.py` (필요 시 `web_replay.py` 최소 연동) | `/api/replay/telemetry/open`, `summary`, `series`, `map_frames` 엔드포인트 및 인덱스 캐시 정리 보호 연동 |
| 수정 | `src/f1re/web/app.py` | `GET /telemetry` 정적 HTML 서빙 |
| 수정 | `src/f1re/web/static/dashboard.html` · `dashboard.js` | 상단/모달에 `/telemetry` 진입 링크 및 녹화 목록 `"그래프 분석"` 버튼 (`?id=<id>&t=<ms>`) |
| 수정 | `src/f1re/web/static/map.js` | 외부 호버 프레임 미리보기 주입 · 비교 차량 강조 지원(기존 `/` 동작 유지) |
| 신규 | `src/f1re/web/static/telemetry.html` · `telemetry.js` · `telemetry.css` | 3단 레이아웃, Canvas 2D 차트(줌/호버/커서/마커/비교 점선), 프리셋, 계층 체크박스, 재생 컨트롤 |
| 신규 테스트 | `tests/udp/test_telemetry_index.py`, `tests/web/test_telemetry_api.py` | 인덱스 빌드·다운샘플 정합성·비교 차량·비공개 처리·API·페이지 응답 검증 (기존 회귀 픽스처 `recording-6-suzuka-race-snippet.f1rec` 등 활용) |

---

## 7. 빌더에게 위임하는 세부 결정

- Canvas 다중 Y축 눈금 표시 최대 개수와 색상 팔레트(색맹/다크 테마 대비).
- 4륜 항목의 체크박스 UI 배치 세부(한 줄 내 4버튼 vs 접히는 서브 행).
- `building` 상태 폴링 간격(예: 300ms) 및 인덱스 내 `array('f')` vs `numpy.float32` 저장 구현 세부.
- 줌 상태의 자동 따라가기(Auto-follow) 기본값.
