# 한국어 AI 레이스 엔지니어 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 이 레포 체계에서는 PL이 작업마다 구현 주체(주니어 DeepSeek / 시니어)를 정하고, 검증은 QA(Qwen)가 한다.

**Goal:** F1 25 UDP 텔레메트리를 홈서버에서 받아 코너 코칭·추월 선택지·위험 경고를 한국어 무전(Qwen TTS)으로 들려주고, 차량 상태와 서킷 지도를 브라우저로 보여 준다.

**Architecture:** 게임이 UDP를 홈서버로 바로 보낸다. 서버의 파이썬 프로세스 하나가 수신 → 파싱 → 상태·이벤트 → 랩·코너 기록 → 코칭·추월·경고 판단 → 무전 큐 → TTS(캐시 우선, Qwen API) → WebSocket으로 `/radio` 페이지에 음성 전달, 대시보드에 상태 전달을 한다. 개발과 테스트는 데모 송신기와 녹화 리플레이로 게임 없이 한다.

**Tech Stack:** Python 3.14(서버 3.14.4) · uv · FastAPI + uvicorn(HTTP·WebSocket) · SQLite(`sqlite3`) · 표준 `struct`로 패킷 파싱 · 브라우저 프런트는 빌드 없는 HTML/JS(canvas) · pytest · Qwen TTS API(`qwen3-tts-flash` 계열)

**Spec:** `golony-claude-support/docs/f1-race-engineer/2026-11-12-korean-race-engineer-design.md` (이하 "설계"). 참고: 같은 폴더 `reference-analysis.md`. 구현 레포에는 설계 문서만 `docs/design/`로 복사해 둔다(작업 1). reference-analysis는 공개 레포에 복사하지 않는다.

## Global Constraints

- 코드 레포: `golony6449/f1-race-engineer`(**공개**). 기본 브랜치 `main`, 작업 브랜치는 `feature/<이슈키>` + PR. 공개 레포이므로 상용 앱 분석 문서(reference-analysis)와 비밀값·키 이름은 넣지 않는다.
- Jira: 에픽 SCRUM-48, 계획 라벨 `plan-korean-race-engineer`. 작업 N ↔ 일감 키는 에픽 아래 목록 참고.
- 환경은 uv만 쓴다(`uv init`, `uv add`, `uv run`). `pyproject.toml`과 `uv.lock`을 커밋한다.
- 서버는 리눅스 홈서버(i5-12500T, 32GB, GPU 없음). Windows 전용 코드 금지. 스크립트는 파이썬.
- 게임: F1 25, `packetFormat` 2025, 리틀 엔디언, packed 구조체, 헤더 29바이트. 구조체 정의는 **EA 공식 F1 25 UDP 문서**를 원본으로 한다. 다른 앱의 코드·데이터 파일을 가져다 쓰지 않는다.
- 기본 UDP 포트 20777(설정 가능).
- TTS는 Qwen API만. API 키는 공용 시크릿 모듈의 `get()`으로 받고 출력·로그·커밋하지 않는다. 키 이름은 사용자에게 받는다.
- LLM을 쓰지 않는다. 코칭·추월은 규칙 기반.
- 무전 말투: 짧고 건조한 명사형(예: "3번, 브레이크 늦음. 5미터." / "7번 탈출, 스로틀 늦음." / "앞차 0.6. 다음 직선 DRS." / "박스, 박스.").
- 모든 코칭·추월 무전은 "어디서 + 무엇을 + 얼마나"를 갖춘다. 근거가 약하면 말하지 않는다.
- 내부 단위: 거리 m, 속도 km/h, 온도 °C, 시간 ms, 연료 kg(게임 값 그대로). 표시 변환은 화면에서만.
- 기본값(설정 파일 `config/default.toml`): 집중 코너 4~5개(기본 5), 집중 코너 산정 최근 3랩, 큰 손실 예외 0.3초, 다음 브레이크까지 3초 미만이면 미룸, 추월 안내 켜짐 간격 1.0초, 추월 무전 랩당 1회.
- 무전 우선순위: 안전·위험 경고 > 박스 > 코너 코칭 > 추월 > 개선 알림. 안전 경고는 끌 수 없다.
- 지연 목표: 코너 탈출 → 무전 결정 100ms, 캐시 적중 → 재생 시작 200ms, 캐시 미스 → 첫 소리 1초(실측).

## Review Focus

1. **세션 재시작·플래시백·일시정지**: `sessionUID`가 바뀌거나 `lapDistance`가 뒤로 가거나 `frameIdentifier`가 되감기면, 그 랩은 무효로 표시하고 코칭·기준 랩에 쓰지 않는다. 프로그램은 멈추지 않는다. → 작업 6·13 테스트.
2. **알 수 없는 패킷**: 길이가 규격과 다르거나 `packetFormat`이 2025가 아니거나(2026 DLC 등) `packetId`를 모르면, 버리고 진단 정보(`unsupported_count`, 마지막 형식)만 올린다. 예외로 수신 루프가 죽지 않는다. → 작업 2·6 테스트.
3. **음성 경로 장애**: Qwen API 실패·타임아웃(기본 2초), `/radio` 탭 미연결이면 무전 큐가 막히지 않고, 캐시에 있는 문장만 내보내며, 상태가 "음성 오프라인"/"무전 페이지 미연결"로 보인다. → 작업 11·12 테스트.
4. **코칭하면 안 되는 랩**: 아웃랩, 피트 레인, 무효 랩, SC·VSC 중인 랩, 레이스 1랩은 코칭·기준 랩에서 뺀다. → 작업 13·15·16 테스트.
5. **코너 데이터 없는 서킷**: 스즈카·모나코가 아닌 트랙 ID에서도 동작하고, 코너는 자동 탐지 + "N번째 코너" 표현으로 말한다. 지도 윤곽이 없으면 첫 유효 랩에서 배운다. → 작업 3·10·14 테스트.

---

## 파일 구조

```
f1-race-engineer/
  pyproject.toml, uv.lock, README.md
  config/default.toml            # 기본값·문턱값 (Global Constraints의 기본값 + 위험 문턱값)
  docs/design/                   # 설계·참고 분석 복사본, docs/spec/ (EA 공식 UDP 문서 사본·메모)
  src/f1re/
    udp/spec.py                  # 패킷 구조체 정의(필드 이름·struct 형식·크기), 헤더
    udp/parser.py                # bytes → Packet
    udp/packer.py                # Packet → bytes (데모 송신기·테스트용, spec.py 공유)
    udp/receiver.py              # asyncio UDP 수신
    udp/recording.py             # 녹화 파일 쓰기·읽기, 리플레이
    state/store.py               # StateStore: 최신 상태 + 진단
    state/events.py              # Event 종류, EventQueue
    tracks/model.py              # Track, Corner, 코너 데이터 로드, 자동 탐지
    tracks/outline.py            # 트랙 윤곽(랩 거리 순 점), 보간
    tracks/data/suzuka.json, monaco.json
    laps/traces.py               # 차별 랩 샘플 기록, 랩 유효성
    coaching/metrics.py          # 코너 지표
    coaching/reference.py        # 기준 랩 선택, 코너 시간 손실
    coaching/focus.py            # 집중 코너 선택·갱신
    coaching/coach.py            # 탈출 감지 → 무전 결정
    overtake/advisor.py          # 추월 선택지
    alerts/thresholds.py         # 위험 기준 단일 출처(색·음성 공통)
    alerts/rules.py              # 위험 경고 → 무전
    radio/templates.py           # 무전 문장 틀
    radio/queue.py               # 우선순위 큐, 유효 시간, 끊기
    radio/log.py                 # 무전 기록
    tts/qwen.py                  # Qwen TTS 클라이언트(일반·스트리밍)
    tts/cache.py                 # 음성 캐시, 미리 만들기
    tts/radiofx.py               # 무전 효과
    db.py                        # SQLite 스키마·접근
    web/app.py                   # FastAPI 앱, API, WebSocket
    web/static/                  # dashboard.html/js, radio.html/js, map.js, carstate.js
    main.py                      # `f1re serve` 진입점
  tools/demo_sender/
    sender.py                    # `f1re-demo` 진입점
    driving.py                   # 속도 곡선·주행 샘플 생성
    field.py                     # AI 차 19대
    scenarios.py                 # 시나리오 파일 해석·주입
    scenarios/*.yaml
  tools/voice_samples.py         # 보이스 샘플 생성
  tests/...                      # 모듈별 대응
```

## 핵심 인터페이스 (작업 간 공유 이름)

```python
# udp/spec.py
HEADER_SIZE = 29
PACKET_FORMAT = 2025
class PacketId(IntEnum): MOTION=0; SESSION=1; LAP_DATA=2; EVENT=3; PARTICIPANTS=4; CAR_SETUPS=5; CAR_TELEMETRY=6; CAR_STATUS=7; FINAL_CLASSIFICATION=8; LOBBY_INFO=9; CAR_DAMAGE=10; SESSION_HISTORY=11; TYRE_SETS=12; MOTION_EX=13; TIME_TRIAL=14; LAP_POSITIONS=15
PACKET_SIZES: dict[PacketId, int]          # 공식 문서 값 그대로
@dataclass(frozen=True) class Header: packet_format:int; game_year:int; game_major:int; game_minor:int; packet_version:int; packet_id:int; session_uid:int; session_time:float; frame_id:int; overall_frame_id:int; player_car_index:int; secondary_player_car_index:int

# udp/parser.py
@dataclass(frozen=True) class Packet: header: Header; body: dict   # 필드 이름은 공식 문서의 m_ 접두어를 뺀 snake_case
class UnsupportedPacket(Exception): reason: str
def parse_packet(data: bytes) -> Packet          # 실패 시 UnsupportedPacket

# udp/packer.py
def pack_packet(header: Header, body: dict) -> bytes

# state/store.py
class StateStore:
    def apply(self, pkt: Packet, recv_ns: int) -> list[Event]
    def snapshot(self) -> dict                    # JSON 직렬화 가능, 화면·API용
    diagnostics: Diagnostics                      # packets_per_sec, last_packet_id, last_format, last_len, last_recv_ns, session_uid, unsupported_count

# state/events.py
class EventKind(StrEnum): SESSION_START, SESSION_END, LAP_COMPLETE, PIT_ENTER, PIT_EXIT, OVERTAKE, PENALTY, SAFETY_CAR, VSC, RED_FLAG, WEATHER_CHANGE, FLASHBACK, SESSION_RESTART
@dataclass(frozen=True) class Event: kind: EventKind; car_index: int|None; lap: int|None; data: dict

# tracks/model.py
@dataclass(frozen=True) class Corner: number: int; official: bool; entry_m: float; apex_m: float; exit_m: float
@dataclass(frozen=True) class Track: track_id: int; name: str; length_m: float; corners: list[Corner]
def load_track(track_id: int) -> Track|None
def detect_corners(trace: LapTrace, track_length_m: float) -> list[Corner]   # official=False

# laps/traces.py
@dataclass class LapTrace: car_index: int; lap: int; valid: bool; invalid_reason: str|None; samples: list[Sample]
@dataclass(frozen=True) class Sample: dist_m: float; t_ms: int; speed: float; throttle: float; brake: float; steer: float; gear: int; x: float; z: float; wheel_slip: tuple[float,...]|None

# coaching/metrics.py
@dataclass(frozen=True) class CornerMetrics: corner: int; brake_start_m: float|None; brake_peak: float; trail_brake_residual: float; turn_in_m: float|None; steer_peak: float; min_speed: float; min_speed_m: float; steer_corrections: int; throttle_on_m: float|None; full_throttle_m: float|None; exit_speed: float; max_line_dev_m: float; off_track: bool; corner_time_ms: int
def corner_metrics(trace: LapTrace, corner: Corner, ref_path: TrackOutline|None) -> CornerMetrics

# radio/queue.py
class Priority(IntEnum): SAFETY=0; BOX=1; COACHING=2; OVERTAKE=3; IMPROVEMENT=4
@dataclass(frozen=True) class RadioMessage: kind: str; priority: Priority; text: str; expires_ns: int; evidence: dict
```

---

## 마일스톤 1: 보이스 샘플

### 작업 1: 레포 뼈대 + Qwen TTS 클라이언트 + 무전 효과 + 보이스 샘플

**Files:** Create `pyproject.toml`, `README.md`, `config/default.toml`, `docs/design/*`, `src/f1re/tts/qwen.py`, `src/f1re/tts/radiofx.py`, `tools/voice_samples.py`, `docs/qwen-tts-notes.md`; Test `tests/tts/test_radiofx.py`, `tests/tts/test_qwen.py`

**Interfaces:**
- Produces: `QwenTTS.synthesize(text: str, voice: str) -> bytes`(WAV PCM 16bit mono), `QwenTTS.stream(text: str, voice: str) -> AsyncIterator[bytes]`(PCM 조각), `apply_radio_fx(pcm: bytes, sample_rate: int) -> bytes`

- [ ] **Step 1:** `uv init --package f1re`, `uv add fastapi uvicorn httpx pyyaml` / `uv add --dev pytest pytest-asyncio`. 설계 문서만 `docs/design/`에 복사(reference-analysis 제외). 커밋.
- [ ] **Step 2: 실패 테스트** `test_radiofx_keeps_length_and_limits_band`: 1kHz·5kHz 사인 합성 입력 → 출력 길이 동일(±시작·끝 클릭음 길이), 5kHz 성분 에너지가 입력 대비 −12dB 이하, 1kHz는 −3dB 이내. `test_qwen_synthesize_sends_text_and_voice`: httpx 모의 전송으로 요청 본문에 text·voice·model이 들어가고, 키는 헤더에만 있고 로그에 안 찍힘.
- [ ] **Step 3:** `apply_radio_fx` 구현: 대역 통과(약 300Hz~3.4kHz), 약한 노이즈, 시작·끝 클릭음. numpy 없이 표준 라이브러리로(필요하면 `uv add numpy`도 허용, 이유를 PR에 적음).
- [ ] **Step 4:** `QwenTTS` 구현. Model Studio 국제 엔드포인트, 모델 `qwen3-tts-flash`(일반) / 실시간 모델(스트리밍). 타임아웃 2초. 키는 시크릿 모듈 `get(<사용자가 준 키 이름>)`.
- [ ] **Step 5:** 테스트 통과 확인 `uv run pytest tests/tts -v` → PASS.
- [ ] **Step 6:** `tools/voice_samples.py`: 한국어 보이스 목록(문서에서 확인)마다 아래 5문장을 만들어 `samples/<voice>/<n>.wav`(무전 효과 적용·미적용 둘 다)로 저장. 문장: "3번, 브레이크 늦음. 5미터." / "7번 탈출, 스로틀 늦음." / "앞차 0.6. 다음 직선 DRS." / "박스, 박스." / "3번 좋아짐. 다음 7번." 문장별 첫 소리까지 지연(일반·스트리밍)을 측정해 표로 출력.
- [ ] **Step 7:** `docs/qwen-tts-notes.md`에 확인 결과 정리: 한국어 지원 모델·보이스 목록, 실시간 모델 단가, 한글 글자 수 계산 방식, 측정 지연. 샘플 파일 위치를 사용자에게 알리고 보이스 선택을 받는다(PL이 팀장 경유 보고). 선택한 보이스를 `config/default.toml`의 `tts.voice`에 기록.
- [ ] **Step 8:** 커밋·PR.

## 마일스톤 2: 수신·파싱·녹화 + 데모 송신기

### 작업 2: 패킷 규격·파서·패커

**Files:** Create `docs/spec/`(공식 문서 사본·출처 메모), `src/f1re/udp/spec.py`, `parser.py`, `packer.py`; Test `tests/udp/test_spec.py`, `test_parser.py`

**Interfaces:** Produces `PacketId`, `PACKET_SIZES`, `Header`, `Packet`, `UnsupportedPacket`, `parse_packet`, `pack_packet` (위 핵심 인터페이스).

- [ ] **Step 1:** EA 공식 F1 25 UDP 문서를 구해 `docs/spec/`에 두고 출처 URL·버전을 메모. 못 구하면 멈추고 PL에 보고(추측으로 규격을 쓰지 않는다).
- [ ] **Step 2: 실패 테스트**
  - `test_struct_sizes_match_spec`: 패킷별 struct 크기 합 == `PACKET_SIZES[id]`(공식 문서 값), 헤더 == 29.
  - `test_roundtrip_all_packet_types`: 패킷 종류마다 대표값 body → `pack_packet` → `parse_packet` → 같은 body.
  - `test_reject_wrong_length`, `test_reject_unknown_format` (packetFormat 2026 → `UnsupportedPacket("format")`), `test_reject_unknown_packet_id`.
- [ ] **Step 3:** `spec.py`에 패킷 16종(0~15) 정의. 필드 이름은 `m_` 뺀 snake_case, 차 배열은 22대. 파서가 실제로 쓰는 것은 0·1·2·3·4·6·7·10·11·12·13·14이고 나머지(5·8·9·15)는 크기 검증만 해도 된다.
- [ ] **Step 4:** `parse_packet`, `pack_packet` 구현(같은 정의 공유).
- [ ] **Step 5:** `uv run pytest tests/udp -v` → PASS. 커밋·PR.

### 작업 3: 서킷 데이터 (스즈카·모나코)

**Files:** Create `src/f1re/tracks/model.py`, `tracks/outline.py`, `tracks/data/suzuka.json`, `tracks/data/monaco.json`; Test `tests/tracks/test_model.py`, `test_outline.py`

**Interfaces:**
- Produces: `Corner`, `Track`, `load_track`(`detect_corners`는 작업 14에서 같은 파일에 추가한다), `TrackOutline(points: list[tuple[float,float,float]])`(dist_m, x, z) · `TrackOutline.position_at(dist_m: float) -> tuple[float,float]` · `TrackOutline.heading_at(dist_m) -> float`
- JSON 형식: `{track_id, name, length_m, corners:[{number, entry_m, apex_m, exit_m}], centerline:[[dist_m,x,z],...]}`

- [ ] **Step 1: 실패 테스트** `test_load_suzuka_and_monaco`: 트랙 ID는 공식 문서 값(문서 확인; 참고로 모나코 5, 스즈카 13로 알려져 있음), 코너 번호 1부터 연속, `entry_m < apex_m < exit_m`, 코너 구간이 겹치지 않음, 길이는 공식 서킷 길이 ±1%(스즈카 약 5,807m, 모나코 약 3,337m). `test_load_unknown_track_returns_none`. `test_outline_interpolates_and_wraps`: 마지막 점 이후 거리는 처음으로 감김, 두 점 사이 선형 보간.
- [ ] **Step 2:** 중심선·코너 위치를 공개 자료(공식 서킷 지도, 위키백과 코너 목록, OSM 등)로 직접 만든다. 출처를 JSON 옆 `README`에 적는다. 게임 좌표계와 맞출 필요는 없고(데모 송신기가 같은 좌표를 쓴다), 실제 게임 좌표와의 정렬은 작업 7에서 확인.
- [ ] **Step 3:** 구현 → 테스트 PASS → 커밋·PR.

### 작업 4: 데모 송신기 – 타임 트라이얼

**Files:** Create `tools/demo_sender/sender.py`, `driving.py`; Test `tests/demo/test_driving.py`, `test_sender.py`

**Interfaces:**
- Consumes: `pack_packet`, `load_track`, `TrackOutline`
- Produces: CLI `uv run f1re-demo --track suzuka|monaco --mode tt|gp --host 127.0.0.1 --port 20777 --laps N --speed 1.0 --seed 0 [--scenario file.yaml]`; `generate_lap(track, driver: DriverProfile, lap: int, rng) -> list[Sample]`; `DriverProfile(pace: float, brake_bias_m: float, consistency: float)`

- [ ] **Step 1: 실패 테스트** `test_lap_speed_minimum_inside_each_corner`(코너마다 최저 속도 위치가 entry~exit 안), `test_lap_time_reasonable`(스즈카 TT 랩 85~100초, 모나코 68~80초), `test_sender_emits_parsable_packets_at_rates`(1초 분량 송신을 소켓으로 받아 모두 `parse_packet` 성공, Car Telemetry·Motion·Lap Data는 60Hz, Session은 2Hz 등 공식 주기), `test_seed_reproducible`.
- [ ] **Step 2:** `driving.py`: 코너마다 목표 최저 속도·브레이크 지점 → 속도 곡선(최대 감속·가속 한계) → 시간 적분으로 샘플 생성, 위치는 중심선 보간, 스티어는 곡률에 비례, 기어는 속도 구간으로.
- [ ] **Step 3:** `sender.py`: TT 모드에서 Session(타임 트라이얼 세션 종류), Lap Data, Car Telemetry, Car Status, Car Damage, Motion, Motion Ex, Participants, Time Trial, Event(랩 완료 등) 송신. 재생 속도 배율 지원.
- [ ] **Step 4:** 테스트 PASS → 커밋·PR.

### 작업 5: 데모 송신기 – 그랑프리 레이스 + 시나리오 주입

**Files:** Create `tools/demo_sender/field.py`, `scenarios.py`, `scenarios/coaching.yaml`, `overtake.yaml`, `alerts.yaml`; Test `tests/demo/test_scenarios.py`, `test_field.py`

**Interfaces:**
- Consumes: 작업 4
- Produces: 시나리오 YAML 형식 `[{lap, corner?, kind, params}]`. `kind` ∈ `late_brake, early_brake, early_turn_in, steer_corrections, late_throttle, off_track, close_to_car_ahead, drs_available, ahead_slow_exit, tyre_age_gap, tyre_wear_critical, tyre_temp_critical, fuel_short, front_wing_damage, penalty, safety_car, vsc`

- [ ] **Step 1: 실패 테스트** `test_late_brake_shifts_brake_point`(지정 랩·코너의 브레이크 시작이 params.m만큼 뒤, 정점 라인 바깥 이동), `test_field_has_20_cars_and_positions_consistent`(Lap Data 순위·간격이 서로 맞음), `test_close_to_car_ahead_gap_under_1s`, `test_safety_car_event_and_session_flag`, `test_front_wing_damage_value_in_car_damage`.
- [ ] **Step 2:** `field.py`: AI 19대 + 내 차, DriverProfile 분포(seed), 앞뒤 간격·추월 단순 모델, Session History·Tyre Sets·Car Status(타이어 나이) 송신.
- [ ] **Step 3:** `scenarios.py`: 시나리오를 주행 생성·상태에 주입.
- [ ] **Step 4:** 테스트 PASS → 커밋·PR.

### 작업 6: 수신기 + 녹화·리플레이 + 상태 저장소 기본 + 진단

**Files:** Create `src/f1re/udp/receiver.py`, `udp/recording.py`, `state/store.py`, `state/events.py`, `src/f1re/db.py`(`open_db(path) -> sqlite3.Connection`, 버전 번호 기반 스키마 마이그레이션, 세션 테이블), `src/f1re/main.py`; Test `tests/udp/test_receiver.py`, `test_recording.py`, `tests/state/test_store.py`

**Interfaces:**
- Consumes: `parse_packet`, `UnsupportedPacket`
- Produces: `StateStore`, `Diagnostics`, `Event`, `EventKind`, `EventQueue.drain() -> list[Event]`; 녹화 형식 `.f1rec` = 반복 `[u64 recv_ns][u16 len][bytes]`(리틀 엔디언), `write_recording(path)`, `iter_recording(path) -> Iterator[tuple[int, bytes]]`, `replay(path, sink, speed: float)`; CLI `uv run f1re serve [--record dir] [--replay file]`

- [ ] **Step 1: 실패 테스트**
  - `test_receiver_survives_garbage_and_unknown_format`: 쓰레기 바이트·2026 형식 패킷을 섞어 보내도 루프가 살아 있고 `unsupported_count` 증가.
  - `test_recording_roundtrip_and_replay_order`.
  - `test_session_restart_emits_event_and_resets`: `session_uid` 변경 → `SESSION_RESTART`, 상태 초기화.
  - `test_flashback_detected`: Event 패킷 FLBK 또는 `frame_id` 되감기 → `FLASHBACK`.
  - `test_diagnostics_packets_per_sec`.
- [ ] **Step 2:** 구현. 수신은 asyncio `DatagramProtocol`. 상태는 패킷 종류별 최신 body + 진단. Event 패킷 코드(SSTA, SEND, FTLP, PENA, SCAR, OVTK, FLBK, RTMT 등, 공식 문서 기준)를 `EventKind`로 매핑.
- [ ] **Step 3:** 데모 송신기 → 수신기 통합 테스트 `test_demo_tt_end_to_end`(스즈카 2랩, 랩 완료 이벤트 2회).
- [ ] **Step 4:** 테스트 PASS → 커밋·PR.

### 작업 7: 실제 게임 녹화로 대조 (사용자 참여)

**Files:** Create `docs/spec/real-game-check.md`, `tests/fixtures/real/`(짧은 녹화 조각)

- [ ] **Step 1:** 사용자에게 요청: 게임 텔레메트리 IP를 녹화 중인 서버(또는 PC)로 지정하고 타임 트라이얼 3랩(스즈카 또는 모나코), 스토리 모드 세션 1분을 녹화. (PL → 팀장 → 사용자)
- [ ] **Step 2:** 녹화를 리플레이해 모든 패킷 파싱 성공·크기 일치 확인. 실패 항목은 작업 2 수정으로 되돌린다.
- [ ] **Step 3:** 확인 기록: 스토리 모드에서 UDP가 나오는지(설계 4장), 실제 게임 좌표와 `tracks/data` 중심선 정렬(필요하면 좌표 변환 파라미터를 트랙 JSON에 추가), 패킷별 실제 주기.
- [ ] **Step 4:** 짧은 녹화 조각을 픽스처로 커밋(수 MB 이하), 회귀 테스트 `test_real_fixture_parses` 추가. 커밋·PR.

## 마일스톤 3: 대시보드

### 작업 8: 위험 기준 단일 출처

**Files:** Create `src/f1re/alerts/thresholds.py`, `config/default.toml`의 `[thresholds]`; Test `tests/alerts/test_thresholds.py`

**Interfaces:** Produces `Severity(StrEnum): OK, WARN, DANGER, NO_DATA`; `severity(metric: str, value: float|None, ctx: dict) -> Severity`; `color_table() -> dict`(화면이 그대로 쓰는 색·문턱값 표, JSON)

- [ ] **Step 1: 실패 테스트** 지표별 경계값 테스트: `front_wing_damage`(경고 25%, 위험 50%), `tyre_wear`(문턱값은 설정 파일 값), `fuel_laps_margin`(남은 연료 랩 − 남은 레이스 랩 < 0 → DANGER), `tyre_temp`, `brake_temp`, `engine_temp`, `None → NO_DATA`. `test_color_table_matches_severity`(표의 문턱값과 `severity` 결과가 같음).
- [ ] **Step 2:** 구현. 날개 손상 25/50은 참고 앱의 값을 출발점으로 쓰고, 나머지 문턱값은 구현자가 정해 설정 파일에 둔다(PR에 근거 기록).
- [ ] **Step 3:** PASS → 커밋·PR.

### 작업 9: 웹 서버 + 차량 상태 화면

**Files:** Create `src/f1re/web/app.py`, `web/static/dashboard.html`, `dashboard.js`, `carstate.js`, `style.css`; Modify `main.py`; Test `tests/web/test_api.py`

**Interfaces:**
- Consumes: `StateStore.snapshot()`, `color_table()`
- Produces: `GET /api/state`(스냅샷 + 내 차 요약), `GET /api/thresholds`, `WS /ws/state`(10Hz 상태 푸시), `GET /`(대시보드)

- [ ] **Step 1: 실패 테스트** `test_state_endpoint_shape`(타이어 4개 마모·표면/내부 온도·압력·컴파운드·나이, 브레이크 온도 4개, 손상 항목, 연료 남은 양·랩당·남은 랩, ERS %, 모드, 각 값의 `severity`), `test_ws_pushes_updates`.
- [ ] **Step 2:** 화면: 태블릿 가로 우선, 휴대폰 세로 대응. 값이 끊기면 마지막 값을 흐리게 유지(칸 삭제 금지). 색은 `/api/thresholds` 표만 사용(화면에서 재계산 금지). 상단에 수신 진단(6.1)·음성 상태·무전 페이지 연결 여부.
- [ ] **Step 3:** 데모 송신기 alerts 시나리오로 수동 확인(스크린샷 PR 첨부). PASS → 커밋·PR.

### 작업 10: 서킷 지도 화면

**Files:** Create `src/f1re/tracks/learn.py`, `web/static/map.js`; Modify `web/app.py`, `db.py`(윤곽 저장); Test `tests/tracks/test_learn.py`, `tests/web/test_map_api.py`

**Interfaces:**
- Produces: `learn_outline(trace: LapTrace) -> TrackOutline`; `GET /api/track`(윤곽 점, 코너 번호·위치, 집중 코너 목록), `WS /ws/state`에 차 위치 `[{car, dist_m, x, z, team_id, is_player, is_target}]` 추가

- [ ] **Step 1: 실패 테스트** `test_learn_outline_from_valid_lap`(유효 랩 위치로 윤곽 생성, 점이 랩 거리 순), `test_unknown_track_learns_outline_on_first_valid_lap`, `test_car_positions_from_lap_distance_without_motion`(Motion 없이 랩 거리만으로 위치 계산).
- [ ] **Step 2:** 지도: 팀 색 점, 내 차 강조, 앞차 표시, 집중 코너 번호, 코너 클릭 시 최근 지표(나/기준), SC·VSC·적기 때 선 색 변경.
- [ ] **Step 3:** PASS → 수동 확인 스크린샷 → 커밋·PR.

## 마일스톤 4: 무전과 위험 경고

### 작업 11: 무전 큐·기록 + TTS 캐시·스트리밍

**Files:** Create `src/f1re/radio/queue.py`, `radio/log.py`, `radio/templates.py`, `tts/cache.py`; Modify `db.py`; Test `tests/radio/test_queue.py`, `tests/tts/test_cache.py`

**Interfaces:**
- Produces: `RadioQueue.push(msg: RadioMessage)`, `RadioQueue.next(now_ns) -> RadioMessage|None`, `RadioQueue.interrupt_if_higher(msg) -> bool`, `RadioQueue.set_muted(kind: str, muted: bool)`(SAFETY는 무시); `VoiceCache.get_or_synthesize(text) -> AudioSource`(캐시 적중이면 파일, 미스면 스트리밍), `VoiceCache.prefetch(texts: list[str])`; `RadioLog.record(msg, played: bool, latency_ms: int|None)`; `templates.render(key: str, **slots) -> str`

- [ ] **Step 1: 실패 테스트** `test_priority_order`, `test_expired_messages_dropped`, `test_higher_priority_interrupts`, `test_safety_cannot_be_muted`, `test_cache_hit_no_api_call`, `test_api_failure_falls_back_to_cache_only_and_flags_offline`(Review Focus 3), `test_prefetch_track_phrases`(스즈카 코너 번호 문장 전부 캐시), `test_templates_have_where_what_howmuch`(코칭·추월 틀은 코너/위치·동작·수치 슬롯을 모두 가짐).
- [ ] **Step 2:** 구현. 캐시 키 = sha256(text + voice + fx 버전). 캐시 파일은 무전 효과 적용본. 문장 틀 목록은 설계 말투 예시로 채우고 PR 설명에 전체 목록을 실어 사용자 확인을 받는다.
- [ ] **Step 3:** PASS → 커밋·PR.

### 작업 12: `/radio` 페이지 + 위험 경고 연결

**Files:** Create `web/static/radio.html`, `radio.js`, `src/f1re/alerts/rules.py`; Modify `web/app.py`; Test `tests/alerts/test_rules.py`, `tests/web/test_radio_ws.py`

**Interfaces:**
- Consumes: `severity`, `RadioQueue`, `VoiceCache`
- Produces: `WS /ws/radio`(서버 → 음성 조각·자막·상태), `GET /radio`; `AlertRules.evaluate(snapshot, events) -> list[RadioMessage]`

- [ ] **Step 1: 실패 테스트** `test_alert_fires_once_on_transition_to_danger`(DANGER로 바뀔 때 1회, 계속 DANGER면 반복 안 함, 해제 후 재진입 시 다시), `test_safety_car_and_penalty_messages`, `test_box_message`, `test_radio_ws_delivers_audio_and_caption`, `test_radio_disconnected_status_visible`(Review Focus 3).
- [ ] **Step 2:** `/radio`: "무전 연결" 버튼 1회 클릭 후 재생, 자동 재연결, 연결 상태 크게 표시, 마지막 무전 자막, 백그라운드 탭 유지 안내 문구.
- [ ] **Step 3:** 데모 alerts 시나리오로 PC 브라우저에서 소리 확인, 지연 측정값을 PR에 기록. PASS → 커밋·PR.

### 작업 13: 홈서버 배포

**Files:** Create `deploy/f1re.service`(systemd 사용자 서비스), `deploy/README.md`

- [ ] **Step 1:** 서버 체크아웃 `/home/golony/dev/repo/f1-race-engineer/f1-race-engineer`, `uv sync`, `uv run f1re serve --port <웹 포트>` 실행 방법, UDP 20777 수신, 방화벽, 시크릿 전달 방식을 문서화.
- [ ] **Step 2:** **크리티컬(배포)**: 서비스 등록·시작은 사용자 승인 뒤 서버 세션에 요청(팀장 경유). 게임 텔레메트리 IP를 서버로 지정하는 법을 README에.
- [ ] **Step 3:** 데모 송신기를 PC에서 서버로 쏴서 태블릿 대시보드·PC `/radio` 확인. 커밋·PR.

## 마일스톤 5: 코너 코칭 (타임 트라이얼)

### 작업 14: 랩 샘플 기록 + 랩 유효성 + 코너 자동 탐지

**Files:** Create `src/f1re/laps/traces.py`; Modify `tracks/model.py`(`detect_corners`); Test `tests/laps/test_traces.py`, `tests/tracks/test_detect.py`

**Interfaces:** Produces `LapTraceRecorder.on_packet(pkt) -> list[LapTrace]`(완료된 랩 반환), `LapTrace`, `Sample`(위 정의)

- [ ] **Step 1: 실패 테스트** `test_records_all_cars_by_lap_distance`, `test_invalid_lap_reasons`(아웃랩, 피트, 트랙 이탈 경고, 플래시백, SC/VSC, 레이스 1랩 → `valid=False`와 이유; Review Focus 1·4), `test_detect_corners_on_demo_suzuka`(자동 탐지 코너 수가 데이터 파일 코너 수의 ±20%, 각 탐지 정점이 실제 코너 구간 안).
- [ ] **Step 2:** 구현. 샘플은 Car Telemetry 시각 기준, 랩 거리는 같은 프레임의 Lap Data, 위치는 Motion(없으면 윤곽 보간), 휠 슬립은 Motion Ex(내 차만).
- [ ] **Step 3:** PASS → 커밋·PR.

### 작업 15: 코너 지표 + 기준 랩 + 코너 시간 손실

**Files:** Create `src/f1re/coaching/metrics.py`, `coaching/reference.py`, `config/default.toml`의 `[coaching.thresholds]`; Test `tests/coaching/test_metrics.py`, `test_reference.py`

**Interfaces:**
- Produces: `corner_metrics(...) -> CornerMetrics`; `ReferenceSet.update(trace: LapTrace)`, `ReferenceSet.best_for(corner: int) -> tuple[LapTrace, str]`(기준 랩과 출처 "self"|"fastest_car"), `corner_time_loss_ms(trace, ref, corner) -> int`(랩 거리 기준 보간 시간 차)

- [ ] **Step 1: 실패 테스트** 데모 coaching 시나리오 기반: `test_late_brake_detected`(브레이크 시작 차이 ≈ 주입값 ±2m), `test_early_brake_detected`, `test_turn_in_early_detected`, `test_steer_corrections_counted`(주입 횟수와 같음, 잡음 무시), `test_late_throttle_detected`, `test_line_deviation_m`, `test_time_loss_positive_when_slower`, `test_invalid_laps_never_become_reference`(Review Focus 4).
- [ ] **Step 2:** 구현. 문턱값(브레이크 시작 = 압력 0.1 이상, 조향 시작·수정 판정, 잡음 필터)은 설정 파일. 기준 궤적 이탈 거리는 기준 랩 위치와의 수직 거리.
- [ ] **Step 3:** PASS → 커밋·PR.

### 작업 16: 집중 코너 + 코너 탈출 직후 무전

**Files:** Create `src/f1re/coaching/focus.py`, `coaching/coach.py`; Modify `radio/templates.py`; Test `tests/coaching/test_focus.py`, `test_coach.py`

**Interfaces:**
- Consumes: `CornerMetrics`, `ReferenceSet`, `RadioQueue`, `Track`
- Produces: `FocusSet.update(completed_laps: list[LapTrace]) -> list[FocusChange]`; `Coach.on_sample(car_state, dist_m, now_ns) -> list[RadioMessage]`(내 차만); `pick_cause(m: CornerMetrics, ref: CornerMetrics) -> Cause`(`Cause` = 원인 키 + 수치)

- [ ] **Step 1: 실패 테스트**
  - `test_focus_top5_from_last_3_laps`, `test_focus_replaced_when_improved_and_announces`("3번 좋아짐. 다음 7번." 형식)
  - `test_message_only_after_focus_corner_exit`, `test_big_loss_non_focus_corner_speaks`(0.3초 이상 또는 트랙 이탈)
  - `test_defer_when_next_brake_within_3s`(모나코 시케인 구간: 다음 긴 직선으로 미룸, 그 랩 안에 못 하면 버림)
  - `test_single_cause_message_has_where_what_howmuch`, `test_no_message_when_weak_evidence`, `test_not_twice_same_lap`
  - `test_ordinal_wording_for_unofficial_corners`(Review Focus 5: "3번째 코너")
  - `test_decision_latency_under_100ms`(코너 탈출 샘플 → 무전 결정)
- [ ] **Step 2:** 구현. 원인 고르기 규칙은 손실 기여가 큰 순(브레이크 > 턴인 > 정점 속도 > 스로틀 > 라인)으로 시작하고, 데모·실제 녹화로 다듬는다. 세션 시작 때 그 서킷 코칭 문장 `VoiceCache.prefetch`.
- [ ] **Step 3:** 데모 스즈카·모나코 coaching 시나리오 리플레이 → 나간 무전 목록을 PR에 첨부(설계 8장 "도움 안 되는 말" 점검). PASS → 커밋·PR.

## 마일스톤 6: 레이스 확장

### 작업 17: 가장 빠른 차 기준 + 레이스 세션 규칙

**Files:** Modify `coaching/reference.py`, `coaching/focus.py`, `coaching/coach.py`; Test `tests/coaching/test_race_rules.py`

- [ ] **Step 1: 실패 테스트** `test_fastest_car_reference_used_per_corner`(코너마다 더 빠른 쪽), `test_fastest_car_comparison_ignores_player_only_fields`(휠 슬립 비교 안 함), `test_race_lap1_excluded_and_start_after_2_laps`, `test_time_trial_has_no_fastest_car`.
- [ ] **Step 2:** 구현 → PASS → 커밋·PR.

### 작업 18: 추월 선택지

**Files:** Create `src/f1re/overtake/advisor.py`; Modify `radio/templates.py`; Test `tests/overtake/test_advisor.py`

**Interfaces:** Produces `OvertakeAdvisor.evaluate(snapshot, traces_by_car, now_ns) -> list[RadioMessage]`

- [ ] **Step 1: 실패 테스트** (데모 overtake 시나리오) `test_activates_within_1s_and_deactivates`, `test_drs_and_ers_option`, `test_ahead_slow_exit_option_names_corner`, `test_tyre_age_wait_option`, `test_max_two_options_and_once_per_lap`, `test_no_overtake_in_time_trial`.
- [ ] **Step 2:** 구현. 재료: 앞차 간격·변화, DRS 허용·활성 거리, ERS 잔량·모드, 타이어 컴파운드·나이, 코너별 탈출 속도 비교. 선택지는 점수 순 상위 2개.
- [ ] **Step 3:** PASS → 커밋·PR.

## 마일스톤 7: 서킷별 저장 + 리포트

### 작업 19: 조건별 기준 랩 저장 + 다음 방문 첫 랩 코칭

**Files:** Modify `db.py`, `coaching/reference.py`, `coaching/focus.py`; Test `tests/coaching/test_persistence.py`

**Interfaces:** 키 `(track_id, mode: "tt"|"race_weekend", weather: "dry"|"wet", car_key: str)`; `ReferenceStore.load(key) -> ReferenceSet|None`, `ReferenceStore.save(key, ref, focus)`

- [ ] **Step 1: 실패 테스트** `test_saved_reference_enables_lap1_coaching`, `test_condition_mismatch_speaks_actions_not_time`(예: 비 vs 맑음 기준이면 시간 수치 없이 동작만), `test_keys_separate_wet_and_dry`.
- [ ] **Step 2:** 구현 → PASS → 커밋·PR.

### 작업 20: 무전 기록 화면 + 종류별 끄기 + 세션 뒤 코너 리포트

**Files:** Create `web/static/report.js`; Modify `web/app.py`, `dashboard.html`; Test `tests/web/test_report_api.py`

**Interfaces:** `GET /api/radio/log?session=`, `POST /api/radio/mute {kind, muted}`, `GET /api/report?session=`(코너별 손실 순위, 집중 코너 변화, 무전이 나간 코너와 다음 랩 개선 여부)

- [ ] **Step 1: 실패 테스트** `test_mute_kind_persists_and_safety_rejected`, `test_report_marks_improvement_after_message`, `test_log_includes_evidence_and_latency`.
- [ ] **Step 2:** 구현 → 수동 확인 → PASS → 커밋·PR.

---

## 선후관계 요약 (Jira 차단 링크용)

| 작업 | 먼저 끝나야 하는 작업 |
| --- | --- |
| 1 레포 뼈대·TTS·보이스 샘플 | — |
| 2 패킷 규격·파서 | 1 |
| 3 서킷 데이터 | 1 |
| 4 데모 송신기 TT | 2, 3 |
| 5 데모 송신기 GP·시나리오 | 4 |
| 6 수신기·녹화·상태 | 2, 4 |
| 7 실제 게임 대조 | 6 |
| 8 위험 기준 | 6 |
| 9 웹·차량 상태 화면 | 8 |
| 10 서킷 지도 화면 | 9, 3 |
| 11 무전 큐·TTS 캐시 | 1, 6 |
| 12 /radio·위험 경고 | 9, 11, 5 |
| 13 홈서버 배포 | 12 |
| 14 랩 기록·코너 탐지 | 6, 3 |
| 15 코너 지표·기준 랩 | 14, 5 |
| 16 집중 코너·코칭 무전 | 15, 11 |
| 17 가장 빠른 차·레이스 규칙 | 16 |
| 18 추월 선택지 | 17 |
| 19 조건별 저장 | 16 |
| 20 무전 기록·리포트 | 16, 12 |
