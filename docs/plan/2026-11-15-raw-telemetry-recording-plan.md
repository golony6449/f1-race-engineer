# 세션별 Raw 텔레메트리 자동 녹화 · 백업 · 웹 다시 보기 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 실제 F1 25 세션을 세션 종류(`practice`/`qualifying`/`race`/`time_trial`)별로 자동 녹화해 세션 종료·10분 무수신 시 `.f1rec.zst`로 압축하고 `rclone`으로 GCS에 백업(로컬 최신 5개 유지)하며, 엔진 OFF 상태에서도 웹 대시보드에서 세션 목록 조회·다운로드·리플레이 재생(다시 보기)을 제공한다.

**Architecture:**
- 병렬 워크플로 실행을 위해 파일 소유권과 인터페이스를 13개 독립 작업(`T1`~`T13`)으로 최대 분할한다.
- 60Hz UDP 수신 경로는 비압축 `.f1rec` 순차 쓰기(`.active/`)로 지연 없이 유지하고, 세션 마감 시 웹 서버 `lifespan` 수명 주기의 독립 `BackupWorker`가 `compression.zstd` 압축·`rclone copyto` 업로드·로컬 5개 정리를 수행한다.
- 대시보드 다시 보기는 실시간 `Engine`·`f1re.db`·Qwen TTS와 완전히 분리된 메모리 전용 `ReplayManager`(`StateStore` + `RacePipeline(conn=None)` + `AlertRules` + `MemoryRadioSink`)로 구동한다.

**Tech Stack:** Python 3.14+ (`compression.zstd`, `sqlite3`, `asyncio`), `uv`, `FastAPI`, `rclone` CLI (`subprocess`), 순수 HTML/JS/CSS (`src/f1re/web/static/`), `pytest` / `pytest-asyncio`.

**Spec:** `docs/design/2026-11-15-raw-telemetry-recording-design.md`

---

## Global Constraints

- Python 3.14 이상(`requires-python = ">=3.14"`). 패키지/실행 도구는 `uv`만 사용하며 외부 패키지 추가 없이 표준 `compression.zstd`와 시스템 `rclone` CLI를 사용한다.
- 서버는 리눅스다. Windows 전용 코드·경로·API 금지.
- 주석·문서·커밋 메시지는 한국어. 테스트는 `uv run pytest`.
- `f1re-demo` 데모 세션(`is_demo_session_uid(uid)`)은 자동 녹화·백업하지 않는다(`exclude_demo = true`).
- GCS 업로드 성공(`backup_status == "uploaded"`)이 확인되지 않은 로컬 파일은 절대 삭제하지 않는다.
- 웹 리플레이(`ReplayManager`) 중에는 Qwen TTS API 호출과 `f1re.db`(`sessions`, `reference_sets`, `radio_messages`) 쓰기를 절대 수행하지 않는다.
- 모든 테스트에서 실제 `rclone`·GCS·네트워크를 호출하지 않고 주입 가능한 업로더/다운로더 가짜로 검증한다.

## Review Focus

1. **비정상 종료 후 `.active/*.f1rec` 끝 레코드 손상**: 프로세스가 쓰는 중 끊겨 마지막 레코드가 잘려도 `iter_recording`·`scan_recording`이 완전한 레코드까지만 읽어 `.f1rec.zst`로 안전하게 마감하고, 0바이트·`min_packets` 미만 파일은 폐기한다.
2. **`rclone` 실패 또는 미설치 시 보존**: `rclone` 바이너리 부재, 설정 누락, 업로드 오류 시 녹화·로컬 보존을 막지 않고 `backup_status = "failed"`/`"skipped"`로 기록하며 5개 정리 대상에서 제외한다.
3. **`session_type` 지연 도착 및 데모·초단기 세션 필터**: 첫 패킷 시점에는 `unknown`으로 `.active/`에 쓰기 시작하되 `PacketId.SESSION` 도착 시 `category`/`track_id`를 갱신하고, `is_demo_session_uid(uid)` 또는 `packet_count < min_packets` 세션은 폐기한다.
4. **10분 무수신 타임아웃 후 재개(2부) & 리플레이 중 파일 보호**: 600초 무수신 마감(`end_reason = "inactivity_timeout"`) 후 같은 `sessionUID`가 재개되면 `segment = 2`(`_s2`)로 충돌 없이 새 녹화를 열고, 로컬 5개 정리 시 현재 `ReplayManager`가 재생 중인 `recording_id`는 삭제에서 제외한다.
5. **웹 리플레이 `seek` 시 상태·시계 동기화**: `seek(position_ms)` 시 `StateStore`, `RacePipeline`, `AlertRules` 전이 기억을 모두 초기화하고, 파일 처음부터 `position_ms` 직전까지 지연 없이 고속 재생하며 패킷 `recv_ns`를 가상 시계로 넘겨 무전 TTL·중복 방지·리포트가 깨지지 않게 한다.

---

## 병렬 실행 웨이브 & 파일 소유권

| Wave | Task | 선행 | 배타 수정 파일 (동일 Wave 내 충돌 없음) |
| :--- | :--- | :--- | :--- |
| 1 | T1 | - | `config/default.toml`, `src/f1re/config.py`, `tests/udp/test_recording_config.py` |
| 1 | T2 | - | `src/f1re/udp/session_category.py`, `tests/udp/test_session_category.py` |
| 1 | T3 | - | `src/f1re/udp/recording.py`, `tests/udp/test_recording.py` |
| 1 | T4 | - | `src/f1re/db.py`, `tests/state/test_db.py` |
| 1 | T5 | - | `src/f1re/udp/replay_radio.py`, `tests/udp/test_replay_radio.py` |
| 1 | T6 | - | `src/f1re/web/static/dashboard.html`, `dashboard.js`, `report.js`, `style.css`, `tests/web/test_replay_ui.py` |
| 2 | T7 | T1, T4 | `src/f1re/udp/gcs_transfer.py`, `tests/udp/test_gcs_transfer.py` |
| 2 | T8 | T1, T3, T4 | `src/f1re/udp/backup.py`, `tests/udp/test_backup.py` |
| 2 | T9 | T3, T5 | `src/f1re/udp/web_replay.py`, `tests/udp/test_web_replay.py` |
| 2 | T10 | T1, T2, T3, T4 | `src/f1re/udp/session_recorder.py`, `tests/udp/test_session_recorder.py` |
| 3 | T11 | T7, T8, T10 | `src/f1re/engine.py`, `src/f1re/main.py`, `tests/test_engine.py` |
| 3 | T12 | T2, T4, T7, T8, T9 | `src/f1re/web/replay_api.py`, `tests/web/test_replay_api.py` |
| 4 | T13 | T6, T11, T12 | `src/f1re/web/app.py`, `tests/web/test_replay_app_wiring.py` |

*(참고: 별개 버그 일감 `SCRUM-95`는 Wave 1~4와 무관하게 상시 독립 실행 가능)*

---

## Wave 1: 완전 독립 기반 모듈 (6개 동시 실행)

### 작업 1: `[recording]` 설정 스키마 및 검증 추가

**Files:**
- Modify: `config/default.toml`
- Modify: `src/f1re/config.py`
- Create: `tests/udp/test_recording_config.py`

**Interfaces:**
- Produces (`src/f1re/config.py`):
  - `@dataclass(frozen=True) class RecordingGcsConfig`: `enabled: bool = False`, `rclone_remote: str = "gcs"`, `bucket: str = ""`, `prefix: str = "f1-race-engineer/recordings"`, `rclone_config: str = "~/.config/rclone/rclone.conf"`
  - `@dataclass(frozen=True) class RecordingConfig`: `enabled: bool = True`, `dir: str = "data/recordings"`, `keep_local_count: int = 5`, `keep_cache_count: int = 3`, `inactivity_timeout_s: float = 600.0`, `exclude_demo: bool = True`, `min_packets: int = 100`, `gcs: RecordingGcsConfig = field(default_factory=RecordingGcsConfig)`
  - `AppConfig.recording: RecordingConfig`

- [ ] **Step 1: 실패 테스트 작성 (`tests/udp/test_recording_config.py`)**
  - `test_load_recording_config_defaults`: 기본값(`keep_local_count == 5`, `inactivity_timeout_s == 600.0`, `keep_cache_count == 3`, `min_packets == 100`, `gcs.rclone_remote == "gcs"`) 검증.
  - `test_load_recording_config_rejects_invalid_values`: `keep_local_count < 1`, `keep_cache_count < 0`, `inactivity_timeout_s <= 0`, `min_packets < 1` 시 `ConfigError` 발생 확인.
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/udp/test_recording_config.py -v`
- [ ] **Step 3: 구현**
  - `config/default.toml` 끝에 `[recording]`·`[recording.gcs]` 기본 설정 블록 추가.
  - `src/f1re/config.py`에 `RecordingGcsConfig`, `RecordingConfig`, `AppConfig.recording` 파싱 및 `_validate()` 검증 추가.
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/udp/test_recording_config.py -v`

---

### 작업 2: 세션 범주 매핑 및 종료 사유 상수 모듈 추가

**Files:**
- Create: `src/f1re/udp/session_category.py`
- Create: `tests/udp/test_session_category.py`

**Interfaces:**
- Produces (`src/f1re/udp/session_category.py`):
  - `CATEGORIES = ("practice", "qualifying", "race", "time_trial", "unknown")`
  - `END_REASONS: frozenset[str] = frozenset({"recording", "session_end", "session_switch", "inactivity_timeout", "engine_off", "recovered"})`
  - `session_category(session_type: int | None) -> str`: `1~4 → "practice"`, `5~9 → "qualifying"`, `10~12 → "race"`, `13 → "time_trial"`, 그 외/`None` → `"unknown"`.
  - `category_label(category: str) -> str`: `"연습"` / `"예선"` / `"레이스"` / `"타임 트라이얼"` / `"미분류"`.
  - `end_reason_label(end_reason: str | None) -> str`: `"정상 종료"`(`session_end`), `"세션 전환"`(`session_switch`), `"10분 만료"`(`inactivity_timeout`), `"엔진 OFF"`(`engine_off`), `"비정상 복구"`(`recovered`), `"녹화 중"`(`recording`).

- [ ] **Step 1: 실패 테스트 작성 (`tests/udp/test_session_category.py`)**
  - 모든 F1 25 `session_type` 값(`0..13`, `None`, `99`) 매핑과 `category_label`, `end_reason_label` 테스트.
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/udp/test_session_category.py -v`
- [ ] **Step 3: `src/f1re/udp/session_category.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/udp/test_session_category.py -v`

---

### 작업 3: `.f1rec.zst` 압축·투명 읽기·손상 끝 스캔 및 `FinalizeJob` 구현

**Files:**
- Modify: `src/f1re/udp/recording.py`
- Modify: `tests/udp/test_recording.py`

**Interfaces:**
- Produces (`src/f1re/udp/recording.py`):
  - `ZST_SUFFIX = ".f1rec.zst"`
  - `@dataclass(frozen=True) class FinalizeJob`: `recording_id: int`, `active_path: Path`, `session_uid: int`, `segment: int`, `end_reason: str`, `ended_at_ns: int`, `category: str`, `session_type: int | None`, `track_id: int | None`
  - `iter_recording(path: str | Path) -> Iterator[tuple[int, bytes]]` (`.f1rec.zst` 또는 zstd 매직 바이트 `28 B5 2F FD` 자동 판별, 잘린 끝 레코드에서 안전 종료)
  - `compress_recording(src: str | Path, dst: str | Path) -> int` (`compression.zstd` 스트리밍 압축 → 임시 파일 후 `os.replace`, 압축 파일 바이트 크기 반환)
  - `scan_recording(path: str | Path) -> tuple[int, int]` (`(packet_count, duration_ms)` 반환, 잘린 끝 레코드 무시)

- [ ] **Step 1: 실패 테스트 작성 (`tests/udp/test_recording.py`)**
  - `test_compress_and_iter_zst_roundtrip`: `.f1rec` → `.f1rec.zst` 압축 후 `iter_recording`이 원본 패킷 시퀀스와 동일한 `(recv_ns, payload)` 반환.
  - `test_replay_reads_zst_recording`: 기존 `replay()` 함수가 `.f1rec.zst`에서도 동작.
  - `test_scan_recording_ignores_truncated_tail`: 끝에 5바이트가 잘려 추가된 `.f1rec`에서 완전한 레코드 수와 `duration_ms`만 정확히 계산.
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/udp/test_recording.py -v`
- [ ] **Step 3: `src/f1re/udp/recording.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/udp/test_recording.py -v`

---

### 작업 4: `session_recordings` SQLite 마이그레이션 006 및 CRUD 구현

**Files:**
- Modify: `src/f1re/db.py`
- Modify: `tests/state/test_db.py`

**Interfaces:**
- Produces (`src/f1re/db.py`):
  - `SCHEMA_VERSION = 6` (`_migration_006_session_recordings`: `session_recordings` 테이블 + `UNIQUE(session_uid, segment)` + `idx_session_recordings_started`, `idx_session_recordings_backup`)
  - `create_recording(conn, *, session_uid: int, segment: int, started_at_ns: int, category: str = "unknown", session_type: int | None = None, track_id: int | None = None) -> int`
  - `next_recording_segment(conn, session_uid: int) -> int`
  - `update_recording_session_meta(conn, recording_id: int, *, category: str | None = None, session_type: int | None = None, track_id: int | None = None) -> None`
  - `finalize_recording(conn, recording_id: int, *, end_reason: str, ended_at_ns: int, packet_count: int, duration_ms: int, local_path: str | None, local_exists: bool, file_size_bytes: int | None, backup_status: str) -> None`
  - `mark_recording_backup(conn, recording_id: int, *, backup_status: str, gcs_object: str | None = None, backed_up_at_ns: int | None = None, backup_error: str | None = None) -> None`
  - `list_recordings(conn, *, category: str | None = None, limit: int = 50) -> list[dict[str, Any]]` (최신순 `started_at_ns DESC, id DESC`)
  - `get_recording(conn, recording_id: int) -> dict[str, Any] | None`
  - `find_local_prune_targets(conn, *, keep_count: int, exclude_ids: Iterable[int] = ()) -> list[dict[str, Any]]` (`local_exists = 1 AND backup_status = 'uploaded'` 대상만 `exclude_ids` 제외 후 최신 `keep_count`개 이후 반환)
  - `mark_recording_local_deleted(conn, recording_id: int) -> None`
  - `list_pending_backup_recordings(conn) -> list[dict[str, Any]]` (`local_exists = 1 AND backup_status IN ('pending', 'failed')`)
  - `delete_recording_row(conn, recording_id: int) -> None`

- [ ] **Step 1: 실패 테스트 작성 (`tests/state/test_db.py`)**
  - `test_migration_006_session_recordings_schema`: 컬럼 및 `(session_uid, segment)` 유니크 제약 검증.
  - `test_recording_segments_and_prune_targets`: `segment=1, 2` 생성, 7개 `uploaded` 중 `exclude_ids` 빼고 최신 5개 제외한 2개만 반환되며 `failed`·`pending` 행은 절대 반환되지 않음을 검증.
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/state/test_db.py -k "006 or recording" -v`
- [ ] **Step 3: `src/f1re/db.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/state/test_db.py -v`

---

### 작업 5: 리플레이용 메모리 무전 로그(`MemoryRadioLog`·`MemoryRadioSink`) 구현

**Files:**
- Create: `src/f1re/udp/replay_radio.py`
- Create: `tests/udp/test_replay_radio.py`

**Interfaces:**
- Consumes: `RadioQueue`, `RadioMessage` (`src/f1re/radio/queue.py`, `messages.py`)
- Produces (`src/f1re/udp/replay_radio.py`):
  - `class MemoryRadioLog`: `append(message: RadioMessage, *, created_at_ns: int) -> None`, `list_messages() -> list[dict[str, Any]]`, `report_source() -> list[dict[str, Any]]` (SQLite `radio_messages` 스키마와 호환되는 딕셔너리 반환), `clear() -> None`
  - `class MemoryRadioSink`: `RadioQueue`에서 메시지를 꺼내 Qwen TTS·DB 없이 즉시 `MemoryRadioLog`에 적재(가상 시계 `clock_ns: Callable[[], int]` 사용), `start()`, `aclose()`

- [ ] **Step 1: 실패 테스트 작성 (`tests/udp/test_replay_radio.py`)**
  - `test_memory_radio_sink_records_without_tts_or_db`: `RadioQueue`에 넣은 메시지가 `MemoryRadioLog.list_messages()`·`report_source()`에 기록되고 외부 호출·DB 파일이 전혀 생기지 않음을 검증.
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/udp/test_replay_radio.py -v`
- [ ] **Step 3: `src/f1re/udp/replay_radio.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/udp/test_replay_radio.py -v`

---

### 작업 6: 대시보드 다시 보기 UI(세션 목록 모달·리플레이 컨트롤 바) 구현

**Files:**
- Modify: `src/f1re/web/static/dashboard.html`
- Modify: `src/f1re/web/static/dashboard.js`
- Modify: `src/f1re/web/static/report.js`
- Modify: `src/f1re/web/static/style.css`
- Create: `tests/web/test_replay_ui.py`

**API Contract (백엔드 T12/T13과 동결된 인터페이스):**
- `GET /api/recordings?category=&limit=50` → `{"recordings": [{id, session_uid, segment, end_reason, end_reason_label, category, category_label, session_type, track_id, track_name, started_at_ns, duration_ms, packet_count, file_size_bytes, local_exists, backup_status, downloadable}]}`
- `GET /api/recordings/{id}/download` → `.f1rec.zst` 파일 다운로드
- `POST /api/replay/start` (`{"recording_id": int, "speed": float}`)
- `POST /api/replay/control` (`{"action": "pause"|"resume"|"seek"|"speed"|"stop", "position_ms"?: int, "speed"?: float}`)
- `WS /ws/replay` (실시간 `/ws/state`와 동일한 스키마 + `"replay": {"recording_id", "playing", "paused", "speed", "position_ms", "duration_ms"}`)
- `GET /api/replay/report`, `GET /api/replay/radio/log`, `GET /api/replay/track`

- [ ] **Step 1: 실패 테스트 작성 (`tests/web/test_replay_ui.py`)**
  - `TestClient(create_app(StateStore()))`로 `/`, `/static/dashboard.js`, `/static/report.js`, `/static/style.css`를 조회해:
    - 엔진 OFF 화면 및 상단 상태줄에 "다시 보기" 버튼(`#btn-open-recordings`) 존재 확인.
    - 세션 목록 모달(`#recordings-modal`, 범주 필터, `2부` 배지·종료 사유 배지·다운로드 링크 렌더링 함수) 존재 확인.
    - 리플레이 컨트롤 바(`#replay-bar`, 재생/일시정지, 배속 `1x/2x/5x/10x`, 시크 바, "실시간으로 돌아가기") 및 `report.js`의 `/api/replay/report` 분기 존재 확인.
    - `alert(` / `confirm(` / `prompt(` 미사용 확인.
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/web/test_replay_ui.py -v`
- [ ] **Step 3: `dashboard.html`, `dashboard.js`, `report.js`, `style.css` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/web/test_replay_ui.py tests/web/test_dashboard.py -v`

---

## Wave 2: 핵심 기능 계층 (4개 동시 실행)

### 작업 7: `RcloneTransport` 및 `RecordingFetcher` 온디맨드 GCS 복원·캐시 계층

**Depends on:** Task 1 (`RecordingConfig`), Task 4 (`db.get_recording`)

**Files:**
- Create: `src/f1re/udp/gcs_transfer.py`
- Create: `tests/udp/test_gcs_transfer.py`

**Interfaces:**
- Produces (`src/f1re/udp/gcs_transfer.py`):
  - `class BackupSkippedError(RuntimeError)`, `class BackupError(RuntimeError)`
  - `class RcloneTransport`:
    - `is_available() -> bool` (`shutil.which("rclone")` 및 config 확인)
    - `async def upload(local_path: Path, remote_object: str) -> None` (`rclone copyto`)
    - `async def download(remote_object: str, local_path: Path) -> None` (`rclone copyto`)
  - `class RecordingFetcher`:
    - `__init__(conn_factory: Callable[[], sqlite3.Connection], config: RecordingConfig, *, transport: RcloneTransport | None = None)`
    - `async def ensure_local_file(recording_id: int) -> Path` (로컬 존재 시 즉시 반환, 없으면 `recording_id`별 `asyncio.Lock`으로 `.cache/<id>.f1rec.zst` 1회 복원 + `keep_cache_count` LRU 정리)

- [ ] **Step 1: 실패 테스트 작성 (`tests/udp/test_gcs_transfer.py`)**
  - `test_fetcher_returns_existing_local_path_without_download`
  - `test_fetcher_deduplicates_concurrent_downloads_and_evicts_lru_cache`
  - `test_rclone_transport_skips_when_binary_or_bucket_missing` (가짜 `which`/config로 안전 예외 확인, 비밀 출력 없음)
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/udp/test_gcs_transfer.py -v`
- [ ] **Step 3: `src/f1re/udp/gcs_transfer.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/udp/test_gcs_transfer.py -v`

---

### 작업 8: `BackupWorker` 비동기 압축·업로드·로컬 5개 보존·복구 워커 구현

**Depends on:** Task 1 (`RecordingConfig`), Task 3 (`compress_recording`, `scan_recording`, `FinalizeJob`), Task 4 (`db.*`)

**Files:**
- Create: `src/f1re/udp/backup.py`
- Create: `tests/udp/test_backup.py`

**Interfaces:**
- Produces (`src/f1re/udp/backup.py`):
  - `class UploadTransport(Protocol)`: `is_available() -> bool`, `async def upload(local_path: Path, remote_object: str) -> None`
  - `class BackupWorker`:
    - `__init__(db_path: str | Path | None, config: RecordingConfig, *, transport: UploadTransport | None = None, active_replay_id: Callable[[], int | None] | None = None)`
    - `async def start() -> None` (`.active/*.f1rec` 복구 + `pending`/`failed` 재시도 등록 + 워커 태스크 시작)
    - `async def aclose() -> None`
    - `def enqueue(job: FinalizeJob) -> None` (비블로킹 큐 삽입)
    - `async def retry(recording_id: int) -> dict[str, Any]`

- [ ] **Step 1: 실패 테스트 작성 (`tests/udp/test_backup.py`)**
  - `test_backup_worker_compresses_uploads_and_keeps_latest_5`
  - `test_backup_worker_keeps_local_file_on_upload_failure`
  - `test_backup_worker_protects_active_replay_recording`
  - `test_backup_worker_recovers_active_f1rec_on_startup` (잘린 끝 포함 `.active/12345_s1.f1rec` 복구 `end_reason == "recovered"`)
  - `test_backup_worker_discards_under_min_packets`
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/udp/test_backup.py -v`
- [ ] **Step 3: `src/f1re/udp/backup.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/udp/test_backup.py -v`

---

### 작업 9: 메모리 전용 `ReplayManager`(`RacePipeline` + `AlertRules` + `Seek`) 구현

**Depends on:** Task 3 (`iter_recording`, `scan_recording`), Task 5 (`MemoryRadioLog`, `MemoryRadioSink`)

**Files:**
- Create: `src/f1re/udp/web_replay.py`
- Create: `tests/udp/test_web_replay.py`

**Interfaces:**
- Consumes: `StateStore`, `RacePipeline(conn=None)`, `AlertRules`, `TrackMap`, `state_payload` (`src/f1re/web/app.py`)
- Produces (`src/f1re/udp/web_replay.py`):
  - `class ReplayManager`:
    - `active_recording_id: int | None`
    - `async def start(recording_meta: Mapping[str, Any], file_path: Path, *, speed: float = 1.0) -> dict[str, Any]`
    - `async def pause() -> dict[str, Any]`, `async def resume() -> dict[str, Any]`, `async def set_speed(speed: float) -> dict[str, Any]`
    - `async def seek(position_ms: int) -> dict[str, Any]`
    - `async def stop() -> None`, `async def aclose() -> None`
    - `def state_payload() -> dict[str, Any]`, `def track_payload() -> dict[str, Any]`, `def report_payload() -> dict[str, Any]`, `def radio_log_payload() -> dict[str, Any]`

- [ ] **Step 1: 실패 테스트 작성 (`tests/udp/test_web_replay.py`)**
  - `test_replay_runs_pipeline_and_alerts_without_db_or_tts`: `.f1rec.zst` 재생 시 위험 경고·코칭이 `radio_log_payload()`에 기록되지만 `reference_sets`/`radio_messages` DB 표는 쓰이지 않음.
  - `test_replay_pause_resume_speed_and_stop`
  - `test_replay_seek_resets_state_and_rebuilds_to_position`: `seek()` 후 `StateStore`, `RacePipeline`, `AlertRules`가 해당 시점 상태로 정확히 재구성됨.
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/udp/test_web_replay.py -v`
- [ ] **Step 3: `src/f1re/udp/web_replay.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/udp/test_web_replay.py -v`

---

### 작업 10: 세션별 자동 녹화기 `SessionRecordingManager` 구현

**Depends on:** Task 1 (`RecordingConfig`), Task 2 (`session_category`, `END_REASONS`), Task 3 (`RecordingWriter`, `FinalizeJob`), Task 4 (`db.*`)

**Files:**
- Create: `src/f1re/udp/session_recorder.py`
- Create: `tests/udp/test_session_recorder.py`

**Interfaces:**
- Produces (`src/f1re/udp/session_recorder.py`):
  - `class FinalizeQueue(Protocol)`: `def enqueue(job: FinalizeJob) -> None`
  - `class SessionRecordingManager`:
    - `__init__(conn: sqlite3.Connection, config: RecordingConfig, backup_worker: FinalizeQueue | None, *, clock_ns: Callable[[], int] = time.time_ns)`
    - `def on_packet_bytes(recv_ns: int, data: bytes, packet: Packet | None) -> None`
    - `def on_events(events: Iterable[Event]) -> None`
    - `def check_inactivity(now_ns: int | None = None) -> bool`
    - `def close_active(end_reason: str) -> None`

- [ ] **Step 1: 실패 테스트 작성 (`tests/udp/test_session_recorder.py`)**
  - `test_splits_files_by_session_uid_and_tracks_category`: `session_switch` 마감 및 새 파일/DB 생성.
  - `test_ignores_demo_session_uid`: 데모 UID 무시.
  - `test_inactivity_timeout_closes_and_resumes_as_segment_2`: 600초 경과 시 `inactivity_timeout` 마감 후 같은 UID 재개 시 `segment == 2`("2부").
  - `test_session_end_event_closes_with_session_end_reason`: `EventKind.SESSION_END` → `end_reason == "session_end"`.
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/udp/test_session_recorder.py -v`
- [ ] **Step 3: `src/f1re/udp/session_recorder.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/udp/test_session_recorder.py -v`

---

## Wave 3: 엔진 연동 & 백엔드 웹 API (2개 동시 실행)

### 작업 11: `Engine`·CLI 자동 녹화 연동

**Depends on:** Task 7 (`RcloneTransport`), Task 8 (`BackupWorker`), Task 10 (`SessionRecordingManager`)

**Files:**
- Modify: `src/f1re/engine.py`
- Modify: `src/f1re/main.py`
- Modify: `tests/test_engine.py`

**Interfaces:**
- `Engine.__init__(..., backup_worker: BackupWorker | None = None)`
- `Engine` 시작 시 `SessionRecordingManager` 생성(기존 `--record` 수동 녹화 병행 가능), 60Hz 수신에서 `on_packet_bytes`·`on_events` 전달, 1초 루프에서 `check_inactivity()` 호출, `stop()`/`aclose()`에서 `close_active("engine_off")` 호출 후 즉시 반환.

- [ ] **Step 1: 실패 테스트 작성 (`tests/test_engine.py`)**
  - `test_engine_auto_records_and_submits_to_backup_worker_on_stop`
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/test_engine.py -v`
- [ ] **Step 3: `src/f1re/engine.py`, `src/f1re/main.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/test_engine.py -v`

---

### 작업 12: 녹화 목록·다운로드·웹 리플레이 API 라우터(`replay_api.py`) 구현

**Depends on:** Task 2 (`category_label`, `end_reason_label`), Task 4 (`db.list_recordings`, `db.get_recording`), Task 7 (`RecordingFetcher`), Task 8 (`BackupWorker`), Task 9 (`ReplayManager`)

**Files:**
- Create: `src/f1re/web/replay_api.py`
- Create: `tests/web/test_replay_api.py`

**Interfaces:**
- Produces (`src/f1re/web/replay_api.py`):
  - `create_replay_router(*, conn_factory: Callable[[], sqlite3.Connection], backup_worker: BackupWorker, fetcher: RecordingFetcher, replay_manager: ReplayManager) -> APIRouter`
  - Endpoints: `GET /api/recordings`, `GET /api/recordings/{recording_id}/download`, `POST /api/recordings/{recording_id}/retry-backup`, `POST /api/replay/start`, `POST /api/replay/control`, `GET /api/replay/state`, `WS /ws/replay`, `GET /api/replay/track`, `GET /api/replay/report`, `GET /api/replay/radio/log`.

- [ ] **Step 1: 실패 테스트 작성 (`tests/web/test_replay_api.py`)**
  - `test_recordings_list_includes_segment_and_end_reason`
  - `test_download_restores_from_gcs_when_local_deleted`
  - `test_replay_start_control_seek_and_websocket`
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/web/test_replay_api.py -v`
- [ ] **Step 3: `src/f1re/web/replay_api.py` 구현**
- [ ] **Step 4: 통과 확인 & 커밋**: `uv run pytest tests/web/test_replay_api.py -v`

---

## Wave 4: 앱 최종 배선 & 통합 검증

### 작업 13: `web/app.py` 최종 배선(`lifespan` `BackupWorker` + `replay_api` 마운트 + OFF 예외)

**Depends on:** Task 6 (Frontend UI), Task 11 (`Engine`), Task 12 (`create_replay_router`)

**Files:**
- Modify: `src/f1re/web/app.py`
- Create: `tests/web/test_replay_app_wiring.py`

- [ ] **Step 1: 실패 테스트 작성 (`tests/web/test_replay_app_wiring.py`)**
  - `test_recordings_and_replay_work_when_engine_is_off`: `Engine` OFF 상태에서도 `/api/recordings*`, `/api/replay*`, `/ws/replay`가 503이 아닌 정상 응답 반환.
  - `test_lifespan_wires_backup_worker_and_protects_active_replay`: `lifespan`이 `BackupWorker`를 기동·종료하고 `ReplayManager.active_recording_id` 콜백 및 `Engine`과 연결됨 검증.
- [ ] **Step 2: 실패 확인**: `uv run pytest tests/web/test_replay_app_wiring.py -v`
- [ ] **Step 3: `src/f1re/web/app.py` 구현**
- [ ] **Step 4: 전체 테스트 실행 & 커밋**: `uv run pytest`
