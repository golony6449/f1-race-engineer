# 세션별 Raw 텔레메트리 자동 녹화 · 백업 · 웹 다시 보기 설계

- 작성: 2026-11-15
- 관련 문서: [기본 설계](2026-11-12-korean-race-engineer-design.md), [홈서버 배포](../../deploy/README.md)
- 상태: 승인 완료 (2026-11-15)

---

## 1. 목적과 배경

실제 F1 25 주행 세션의 원본 UDP 패킷(`.f1rec`)을 **세션 종류별로 자동 녹화·보관**하여 다음 세 가지 목적을 달성한다.

1. **버그·코칭 리플레이 재현**: 주행 중 놓친 코칭 판단이나 파서·트랙 이탈 오판정 같은 이슈를 당시 패킷 그대로 재현한다.
2. **주행 분석 및 로컬 테스트 데이터 확보**: 홈서버가 저장·백업한 녹화 파일을 개발 PC(단말기)로 바로 내려받아 `f1re serve --replay`나 `pytest` 픽스처로 쓴다.
3. **대시보드에서 다시 보기**: 게임을 끄고 난 뒤(엔진 OFF 상태 포함)에도 웹 대시보드에서 과거 세션을 골라 차량 상태·서킷 지도·무전 판단을 재생하며 복기한다.

### 왜 파일 저장 + `rclone` GCS 백업인가
`f1re.db`는 홈서버에서 Litestream으로 Google Cloud Storage(GCS)에 실시간 백업되고 있지만, 60Hz 수신 시 raw UDP 패킷은 초당 약 380~400 KB/s(10분 ≈ 230~240 MB, 45분 레이스 ≈ 1 GB 이상)에 달한다.
- raw 패킷을 `f1re.db`에 직접 넣으면 코칭·기준 랩·무전 DB가 수 GB로 팽창하고, 삭제 후에도 GCS WAL 이력·`VACUUM` 부담이 남는다.
- 반면 `.f1rec` 파일로 기록하고 세션 종료 시 `zstd` 압축(`.f1rec.zst`) 후 `rclone`으로 GCS에 백업하며 `f1re.db`에는 메타데이터만 남기면, 기존 `--replay` 파이프라인과 로컬 테스트 활용이 심리스하게 이어진다.

---

## 2. 목표 · 비목표 · 반목표

### 목표
1. `Engine` ON 동안 들어오는 실제 게임 세션을 `sessionUID`·`session_type`별로 자동 분리 녹화한다.
2. 세션이 끝나면 백그라운드에서 `.f1rec.zst`(`compression.zstd`)로 압축하고 `rclone`으로 GCS에 자동 백업한다.
3. 백업이 완료된 녹화 중 **전체 최신 5개**만 로컬에 유지하고 오래된 로컬 파일은 삭제한다. 메타데이터와 GCS 위치는 `f1re.db` 세션 목록에 계속 남긴다.
4. 대시보드에서 전체 세션 목록(로컬 + GCS 아카이브) 조회, **웹 리플레이 플레이어**(재생·일시정지·배속·위치 이동), 로컬 테스트용 **파일 다운로드**를 제공한다(로컬에 없으면 GCS에서 자동 복원).

### 비목표
- `f1re-demo` 데모 세션(`is_demo_session_uid`)의 자동 녹화·백업.
- 웹 리플레이 재생 중 Qwen TTS API 재호출(과금 방지) 및 `f1re.db`의 `sessions`·`reference_sets`·`radio_messages` 덮어쓰기.
- 다중 사용자 동시 웹 리플레이(홈서버 1인 사용 기준으로 단일 활성 리플레이 세션만 지원).
- 기존 CLI `f1re serve --replay` 동작 제거(단 `.f1rec.zst` 입력은 함께 지원).

### 반목표 (기술적으로 돌아가도 실패인 것)
- **60Hz 수신 지연·드롭**: 압축이나 `rclone` 업로드 때문에 UDP 수신 루프가 막히는 것.
- **백업 전 삭제**: GCS 업로드가 성공하지 않았는데 로컬 파일이 삭제되어 주행 기록이 사라지는 것.
- **실주행 DB 오염**: 과거 세션을 대시보드에서 다시 보다가 기준 랩(`reference_sets`)이나 실세션 무전 기록이 바뀌는 것.

---

## 3. 고려한 접근 방식과 선택

| 접근 | 요약 | 장점 | 단점 / 판단 |
|---|---|---|---|
| **A. 독립 메모리 `ReplaySession` + 종료 시 `.f1rec.zst` & `rclone` 백업 (선택)** | 실시간 `Engine`과 분리된 메모리 전용 리플레이 러너를 웹 앱에 두고, 녹화는 `.f1rec` 순차 쓰기 후 종료 시 비동기 압축·업로드 | 엔진 OFF 상태에서도 웹 다시 보기 가능. 실주행 DB·Qwen TTS와 완벽히 격리. 비정상 종료 시에도 녹화분 복구 가능 | 리플레이용 WS/제어 API와 상단 재생 UI가 추가됨 |
| **B. 기존 `Engine` 입력 소스를 UDP ↔ 파일로 전환** | `Engine`이 UDP 대신 파일을 읽어 기존 `/ws/state`를 그대로 사용 | 웹소켓 경로 하나만 유지 | 엔진 ON/OFF 수명 주기와 충돌(다시 보려고 엔진을 켜야 함, UDP 수신과 공존 불가). DB·TTS 차단 분기가 파이프라인 전역에 퍼짐 |
| **C. 별도 SQLite(`raw.db`)에 압축 청크 저장 + Litestream** | raw 데이터를 SQLite 테이블에 넣어 Litestream에 일임 | 추가 백업 도구 없이 Litestream 설정만 추가 | 단말기에서 `.f1rec` 테스트 데이터로 바로 꺼내 쓰기 번거롭고, 로컬 5개 삭제 후 DB/GCS 정리 관리가 무거움 |

**선택: 접근 A.** 단말기 테스트 활용성과 실시간 엔지니어 격리, 엔진 OFF 상태 복기 요구에 가장 잘 맞는다.

---

## 4. 상세 설계

### 4.1 세션 종류 분류 및 파일 레이아웃

`PacketId.SESSION`의 `session_type` 값을 다음 범주로 매핑한다.

| `session_type` (공식 F1 25) | 범주 (`category`) | 비고 |
|---|---|---|
| `1, 2, 3, 4` | `practice` | P1, P2, P3, Short Practice |
| `5, 6, 7, 8, 9` | `qualifying` | Q1, Q2, Q3, Short Q, One-Shot Q |
| `10, 11, 12` | `race` | Race, Race 2, Race 3 |
| `13` | `time_trial` | Time Trial |
| 그 외 / 미수신 | `unknown` | `SESSION` 패킷 수신 전 종료 등 |

- **디렉터리 구조** (기본 `data/recordings/`, 설정으로 변경 가능):
  - 녹화 중 임시 경로: `data/recordings/.active/<session_uid>_s<segment>.f1rec`
  - 마감·압축 후 확정 경로: `data/recordings/<category>/<YYYYMMDD-HHMMSS>_t<track_id>_<session_uid>_s<segment>.f1rec.zst`
  - GCS 온디맨드 복원 캐시: `data/recordings/.cache/<recording_id>.f1rec.zst`
- **`session_type` 지연 도착 처리**: 첫 패킷(`SESSION_START`) 시점에는 아직 `PacketId.SESSION`(2Hz)이 도착하지 않았을 수 있다. 따라서 `.active/<session_uid>.f1rec`로 즉시 녹화를 시작하고, `StateStore`/패킷에서 `session_type`과 `track_id`를 얻어 메타데이터를 갱신한 뒤 세션이 마감될 때 최종 `<category>/...` 경로로 확정한다.
- **데모·노이즈 필터**:
  - `is_demo_session_uid(uid)`이고 `exclude_demo = true`이면 녹화 파일을 쓰지 않거나 마감 시 폐기한다.
  - 패킷 수가 최소 기준(`min_packets`, 예: 메뉴만 잠깐 열었다 닫은 세션 걸러내기) 미만이거나 `session_type`이 끝까지 불명확한 초단기 세션은 백업하지 않고 정리할 수 있게 한다(수치 기본값은 구현 단계에서 결정).

### 4.2 녹화 · 압축 · `rclone` 백업 · 로컬 5개 유지

1. **수신 중 (60Hz 경로) & 무수신 타임아웃**:
   - `Engine.start()`가 `SessionRecordingManager`를 생성해 UDP 수신 경로(`on_datagram` 또는 `DatagramReceiver`)와 연결한다.
   - `sessionUID`가 바뀌면 이전 `RecordingWriter`를 닫아 마감 큐에 넘기고 새 `sessionUID`의 `.active/<uid>_s1.f1rec`를 연다.
   - `SESSION_END` 이벤트(`end_reason = 'session_end'`), `sessionUID` 변경(`'session_switch'`), `Engine.stop()`/`aclose()`(`'engine_off'`), 또는 **UDP 패킷이 `inactivity_timeout_s`(기본 600초 = 10분) 동안 전혀 오지 않을 때(`'inactivity_timeout'`)** 현재 활성 파일을 닫아 `BackupWorker` 큐에 넘긴다. (무수신 10분 동안은 패킷이 들어오지 않으므로 파일 용량도 늘어나지 않는다.)
   - 10분 타임아웃으로 1부(`segment = 1`)가 마감된 뒤 30분 후 재개처럼 같은 `sessionUID` 패킷이 다시 들어오면 `segment`를 증가시켜(`_s2`, 동일 세션 2부) 새 파일로 안전하게 이어 받는다. (참고: 일시정지 중에도 게임이 `game_paused = 1` 패킷을 계속 송신하는 경우라면 패킷이 끊기지 않았으므로 1부 그대로 이어진다.)
   - 녹화 중에는 비압축 `.f1rec` 순차 쓰기만 수행하므로 60Hz 수신에 압축 CPU 지연이 없으며, 비정상 종료 시에도 `iter_recording()`이 마지막 완전 레코드까지 복구한다.
2. **종료 후 비동기 처리 (`BackupWorker` — 웹 서버 레벨 독립 워커)**:
   - 45분 레이스 압축·업로드가 `Engine.stop()`(HTTP 요청)이나 `f1re.service` `TimeoutStopSec=10`을 막지 않도록, `BackupWorker`는 `Engine` 내부가 아닌 **웹 서버(`lifespan`) 레벨의 독립 워커(전용 SQLite 연결)**로 둔다. `Engine.stop()`/`aclose()`는 활성 파일만 닫아 큐에 넣고 즉시 반환하며, 엔진을 끈 뒤에도 백그라운드에서 마저 압축·업로드가 완료된다.
   - 내부 스레드풀(`asyncio.to_thread`)에서 순차 처리하며 단계는 다음과 같다:
     1. `.active/<uid>_s<segment>.f1rec` → `<category>/<파일명>.f1rec.zst`로 `compression.zstd` 스트리밍 압축 후 원본 `.f1rec` 삭제.
     2. DB `session_recordings`에 `backup_status = 'pending'`, `local_exists = 1`, `file_size_bytes` 기록.
     3. `gcs_enabled = true`이고 `rclone`이 사용 가능하면 `rclone copyto <로컬파일> <remote>:<bucket>/<prefix>/<category>/<파일명>` 실행. (`rclone sync`가 아닌 `copyto`를 써서 로컬 삭제가 GCS 파일 삭제로 이어지지 않게 한다.)
     4. 업로드 성공 시 `backup_status = 'uploaded'`, `gcs_object`, `backed_up_at_ns` 갱신. 실패 시 `backup_status = 'failed'`, `backup_error`만 남기고 **로컬 파일은 절대 삭제하지 않는다**.
     5. **로컬 5개 유지 정리**: `local_exists = 1 AND backup_status = 'uploaded'`인 행을 `started_at_ns DESC`로 정렬해 상위 `keep_local_count`(기본 `5`)개를 제외한 나머지 로컬 파일을 삭제하고 `local_exists = 0`으로 갱신한다(단, 현재 웹 리플레이가 사용 중인 파일은 제외).
3. **비정상 종료 복구 및 실패 재시도**:
   - 웹 서버 기동 시(`.active/*.f1rec`)에 남은 미마감 파일이 있으면 손상된 끝부분을 제외한 완전 레코드를 `.f1rec.zst`로 정리해 백업 큐에 넣는다.
   - `pending`·`failed` 상태 녹화는 서버 기동 시 및 주기적으로 자동 재시도하며(필요 시 수동 재시도 API 제공), 성공 후 로컬 5개 정리 규칙을 적용한다.
4. **CLI `--record`와의 공존**:
   - 사용자가 개발/디버깅용으로 `f1re serve --record DIR`를 명시한 경우 기존 수동 녹화도 함께 동작하거나 우선하도록 유지한다.
5. **`.f1rec.zst` 공용 읽기 지원**:
   - `src/f1re/udp/recording.py`의 `iter_recording()`·`replay()`가 `.f1rec`와 `.f1rec.zst`를 확장자(또는 zstd 매직 바이트)로 자동 판별해 읽도록 확장해 `f1re serve --replay`와 테스트가 바로 쓸 수 있게 한다.

### 4.3 설정 (`config/default.toml`)

```toml
[recording]
enabled = true
dir = "data/recordings"
keep_local_count = 5
keep_cache_count = 3
inactivity_timeout_s = 600
exclude_demo = true
min_packets = 100

[recording.gcs]
enabled = true
rclone_remote = "gcs"
bucket = "f1-race-engineer-backup"
prefix = "recordings"
rclone_config = "~/.config/rclone/rclone.conf"
```

- 홈서버에 `rclone` 또는 설정이 없으면 경고를 남기고 `backup_status = 'skipped'`(또는 `'failed'`)로 처리하며, 녹화 자체와 로컬 파일 보존은 중단하지 않는다.

### 4.4 SQLite 메타데이터 (`src/f1re/db.py` 마이그레이션 006)

`session_recordings` 테이블을 추가한다(기존 `sessions` 테이블과 `session_uid`로 연결되되 독립 조회 가능, `UNIQUE(session_uid, segment)` 제약으로 타임아웃 후 재개 세그먼트 충돌을 막는다).

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | 녹화 식별자 |
| `session_uid` | `INTEGER NOT NULL` | F1 25 세션 UID |
| `segment` | `INTEGER NOT NULL DEFAULT 1` | 동일 세션 재개 시 부 번호 (`1` = 1부, `2` = 2부…) |
| `end_reason` | `TEXT NOT NULL DEFAULT 'recording'` | `recording` / `session_end` / `session_switch` / `inactivity_timeout` / `engine_off` / `recovered` |
| `category` | `TEXT NOT NULL` | `practice` / `qualifying` / `race` / `time_trial` / `unknown` |
| `session_type` | `INTEGER` | 공식 `m_sessionType` 원값 |
| `track_id` | `INTEGER` | 서킷 ID |
| `started_at_ns` | `INTEGER NOT NULL` | 첫 패킷 수신 시각 |
| `ended_at_ns` | `INTEGER` | 마감 시각 |
| `packet_count` | `INTEGER NOT NULL DEFAULT 0` | 기록된 데이터그램 수 |
| `duration_ms` | `INTEGER NOT NULL DEFAULT 0` | 첫 패킷~마지막 패킷 길이(ms) |
| `local_path` | `TEXT` | 레포 기준 로컬 `.f1rec.zst` 경로 |
| `local_exists` | `INTEGER NOT NULL DEFAULT 0` | 로컬 보유 여부 (`0`/`1`) |
| `file_size_bytes` | `INTEGER` | 압축 파일 크기 |
| `gcs_object` | `TEXT` | GCS 내 상대 객체 경로 (`<category>/<파일명>`) |
| `backup_status` | `TEXT NOT NULL DEFAULT 'recording'` | `recording` / `pending` / `uploaded` / `failed` / `skipped` |
| `backed_up_at_ns` | `INTEGER` | 업로드 완료 시각 |
| `backup_error` | `TEXT` | 마지막 실패 사유 요약 |

### 4.5 온디맨드 복원 및 다운로드 API

**엔진 OFF 상태에서도 동작해야 한다.** `web/app.py`의 엔진 OFF 차단 대상에서 `/api/recordings*`와 `/api/replay*`, `/ws/replay`를 제외한다.

- `GET /api/recordings?category=&limit=`:
  - `session_recordings`에서 최신순으로 목록 반환 (`id`, `session_uid`, `segment`, `end_reason`, `category`, `session_type`, `track_id`, `started_at_ns`, `duration_ms`, `packet_count`, `file_size_bytes`, `local_exists`, `backup_status`, `downloadable` 여부).
- `GET /api/recordings/{recording_id}/download`:
  - 로컬 파일(`local_path`)이 있으면 바로 파일 응답(`Content-Disposition: attachment`).
  - 로컬에 없고 `gcs_object`가 있으면 복원 서비스(`RecordingFetcher`)가 `.cache/<recording_id>.f1rec.zst`로 `rclone copyto` 다운로드 후 전송. (복원 캐시는 LRU `keep_cache_count` 상한을 유지해 디스크 팽창 방지.)
  - 같은 `recording_id`에 대한 동시 다운로드/재생 요청은 잠금으로 중복 `rclone` 호출을 합친다.

### 4.6 웹 대시보드 "다시 보기" (`ReplaySession`)

1. **메모리 전용 리플레이 런타임 (`RacePipeline` + `AlertRules`)**:
   - `ReplaySession`은 전용 `StateStore`, `TrackMap(conn)`(서킷 윤곽 읽기 전용 조회, 쓰기 안 함), **메모리 전용 `RacePipeline`**(`conn=None`으로 두어 `reference_sets`를 읽거나 쓰지 않고, 무전 큐는 Qwen TTS가 아닌 `MemoryRadioSink`에 연결), 그리고 **메모리 전용 `AlertRules`**로 구성한다.
   - `AlertRules`도 함께 평가하되 리플레이 패킷의 `recv_ns`를 가상 시계로 넘겨, 코너 코칭·추월·리프트 앤 코스트뿐 아니라 안전 경고(타이어·브레이크·연료·손상·페널티·깃발)와 박스 지시까지 실시간과 동일하게 재현한다.
   - 리플레이 중 실서버의 `f1re.db`·기준 랩·실시간 `Engine` 상태가 전혀 영향을 받지 않고 Qwen API 호출도 발생하지 않는다.
   - 생성된 무전 문장과 근거 수치는 리플레이 전용 `RadioLog` 버퍼에 쌓아, 대시보드의 무전 기록·세션 리포트 영역에서 실시간과 동일하게 확인·재현한다.
2. **제어·상태 API 및 WebSocket**:
   - `POST /api/replay/start`: `{ "recording_id": ..., "speed": 1.0 }`
     - 로컬 또는 GCS 복원 캐시에서 `.f1rec.zst` 확보 후 리플레이 태스크 시작.
   - `POST /api/replay/control`: `{ "action": "pause" | "resume" | "seek" | "speed" | "stop", "speed"?: float, "position_ms"?: int }`
     - **Seek 처리**: 텔레메트리 상태는 이전 패킷 누적에 의존하므로, 목표 `position_ms`로 이동할 때는 `StateStore`·`RacePipeline`·`AlertRules` 전이 메모리를 모두 초기화한 뒤 파일 처음부터 `position_ms` 직전까지 지연 없이 고속 재생(`sleep` 없음, `recv_ns` 가상 시계 유지)으로 상태를 재구성한다.
   - `GET /api/replay/state` / `WS /ws/replay` (10Hz 푸시):
     - 기존 `state_payload()`와 같은 구조(`snapshot`, `car`, `connection`, `cars`, `flag`, `flags`, `status`)에 `replay` 메타(`active`, `recording_id`, `session_uid`, `paused`, `speed`, `position_ms`, `duration_ms`, `category`, `track_id`)를 함께 내려준다.
   - `GET /api/replay/track`, `GET /api/replay/report`, `GET /api/replay/radio/log`:
     - 리플레이 중인 세션의 서킷 정보, 코너 리포트, 무전 로그를 반환해 기존 `map.js`·`report.js`와 동일한 화면 구성을 유지한다.
3. **프런트엔드 UI (`dashboard.html`, `dashboard.js`, `map.js`, `report.js`)**:
   - 상단 상태줄 또는 화면 모서리(엔진 OFF 화면 포함)에 **"다시 보기"** 버튼을 둔다.
   - 세션을 선택하면(모달/드로어 목록: 날짜·서킷·종류 배지·부 표시(`2부` 등, `segment > 1` 또는 분할 세션 시)·종료 사유 배지(`정상 종료` / `10분 만료` / `세션 전환` / `엔진 종료` / `복구`)·길이·로컬/클라우드 표시 + 테스트용 **다운로드 링크**) 서버가 복원·시작하고 대시보드가 리플레이 모드로 전환된다.
   - 리플레이 모드에서는 상단에 **재생 컨트롤 바**(재생/일시정지, 배속 `1x`/`2x`/`5x`/`10x`, 타임라인 시크 바, 현재 시각/전체 길이, "실시간으로 돌아가기" 버튼)가 나타나며, 나머지 차량 상태·서킷 지도·무전 기록·리포트 UI는 기존 컴포넌트를 그대로 사용한다.

---

## 5. 구현자에게 맡기는 부분

- `min_packets` 기본값(예: 100~300 패킷), GCS 복원 시 진행 상태 표시 세부 UI.
- `seek` 응답성 개선을 위해 리플레이 시작 시 패킷 메타/길이 스캔 방식 세부.
- 기존 `sessions` 표의 `recorded_file`과 `session_recordings.local_path` 간 연동 세부.
- 테스트 전략: 실제 `rclone`·GCS 호출 없이 주입 가능한 업로더/다운로더 인터페이스로 압축·백업 실패 보존·5개 정리·온디맨드 복원·웹 리플레이 시크/배속/무오염을 단위·통합 테스트로 검증한다.
