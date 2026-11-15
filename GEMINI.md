# GEMINI.md

이 문서는 Gemini(Antigravity) 에이전트가 `f1-race-engineer` 프로젝트에서 일관되고 안전하게 작업할 수 있도록 핵심 규칙, 명령어, 아키텍처와 주의사항을 정리한 가이드입니다.

---

## 1. 프로젝트 개요

`f1-race-engineer`(`f1re`)는 **F1 25 UDP 텔레메트리(`packetFormat` 2025)** 기반의 한국어 AI 레이스 엔지니어입니다.

- **핵심 기능**: 코너 공략 코칭(기준 랩 대비 시간 손실이 큰 집중 코너 탈출 직후 짧은 무전), 추월 선택지 안내, 위험 경고(타이어·브레이크·연료·손상·페널티·깃발), Qwen TTS 무전 음성 합성 및 웹 대시보드(`/`)·무전 페이지(`/radio`) 제공.
- **운영 환경**: 게임 PC가 UDP 20777로 패킷을 보내고, 홈서버(리눅스, `i5-12500T · 32GB`, GPU 없음)가 수신·분석·TTS·웹 서빙(`0.0.0.0:8093`, nginx 뒤에서는 `127.0.0.1:8093`)을 모두 맡습니다.
- **구동 도메인**: `https://race-engineer.golony.dev/` (대시보드 `/`, 무전 `/radio`, nginx 리버스 프록시 → `127.0.0.1:8093`, systemd 사용자 서비스 `f1re.service`).
- **설계·계획 문서**:
  - 설계: `docs/design/2026-11-12-korean-race-engineer-design.md`
  - 기본 계획: `docs/plan/2026-11-12-korean-race-engineer-plan.md` (작업 N ↔ Jira SCRUM 일감, 에픽 `SCRUM-48`)
  - 추가 계획: `docs/plan/` 하위 문서들
- **서킷 지식**: 레포 `kb/` 폴더(목록 `kb/README.md`, 서킷별 `kb/circuits/<서킷>.md`, 예: `kb/circuits/suzuka.md`). 코너·직선·DRS·추월 지점 등 무전용 레이아웃 정보이며, 추월·코칭 무전 기능을 만들거나 고칠 때 먼저 읽는다.

---

## 2. 핵심 원칙 및 개발 규칙

### 대화·문서·코드 규약
- 사용자에게 보이는 모든 글(진행 안내, 보고, 질문 등), 코드 주석, 문서, 커밋 메시지는 **한국어**로 작성한다(코드 식별자는 영어).
- 브랜치는 `feature/SCRUM-<번호>`, PR 제목 앞에는 `[SCRUM-<번호>]`를 붙인다.

### 실행 환경 및 플랫폼 제약
- **패키지·실행 도구는 `uv`만 사용한다** (`uv sync`, `uv add`, `uv run`). 의존성 변경 시 `pyproject.toml`과 `uv.lock`을 함께 커밋한다.
- **Python 3.14 이상**(`requires-python = ">=3.14"`).
- **서버는 리눅스다.** Windows 전용 코드·경로·API 사용을 금지하며, 유틸리티/운영 스크립트는 파이썬(`tools/`)으로 작성한다.
- `config/default.toml`, `assets/`, `data/` 등의 상대 경로는 `f1re.config.repo_root()` 기준이므로 서버는 레포 루트에서 실행한다.

### 보안·공개 레포 규칙
- 이 레포(`golony6449/f1-race-engineer`)는 **공개 레포**다. API 키·비밀값, 상용 앱 분석 자료는 절대 코드·테스트·문서·로그에 넣지 않는다.
- Qwen TTS API 키는 `src/f1re/secrets.py`의 `load_qwen_key()`가 공용 도우미(`~/.claude/skills/qwen-key/get-qwen-key.py`)를 격리 실행(`python -I`)해 파이프로만 읽는다. 홈 폴더의 시크릿 저장 파일을 직접 열거나 키 값을 출력·기록하지 않는다.
- 저작권 효과음(`assets/`), 런타임 DB·상태·음성 캐시(`data/`), 녹화 파일(`recordings/`, `*.f1rec`)은 `.gitignore` 대상이며 커밋하지 않는다.

### Antigravity(agy) 도구 사용 규칙
- `run_command`에서 복합 셸 연결(`;`, `&&`, `||`, `|`, 줄바꿈)을 쓰지 않는다. `git`, `gh`, `uv` 명령은 반드시 **단일 명령**으로 나누어 실행한다.
- 파일 읽기·쓰기·수정은 셸 명령 대신 `read_file`, `write_file`, `edit_file`을 쓰고, Windows에서는 `Path` 인자에 **백슬래시(`\`) 절대 경로**를 사용한다.

### 홈서버 운영 및 Orca 세션 위임
- 서비스 구동 도메인은 `race-engineer.golony.dev`이며, 실제 서비스는 리눅스 홈서버(`/home/golony/dev/repo/f1-race-engineer` 기준, `f1re.service`)에서 돌아간다.
- 홈서버에서 직접 확인·실행해야 하는 작업(서버 상태·로그·DB·포트 점검, 또는 사용자가 지시한 `git pull`·`uv sync`·`systemctl --user` 재시작 등)은 **Orca의 `homeserver` 환경 세션에 위임**한다(상세 절차: 사용자 공통 kb의 `kb/homeserver.md`. 레포 `kb/` 폴더가 아니다).
  - 목록 조회: `orca terminal list --environment homeserver --json` (`worktreePath`·`title`로 `f1-race-engineer` 세션 핸들 확인)
  - 작업 요청: `orca terminal send --environment homeserver --terminal <핸들> --text "<요청>" --enter --wait-submit 5 --json`
  - 대기·확인: `orca terminal wait --environment homeserver --terminal <핸들> --for tui-idle --timeout-ms 240000` 후 `orca terminal read --environment homeserver --terminal <핸들> --json`
- **위임 시 주의사항**:
  - 설치·설정 변경·`git pull`·서비스 시작/재시작 등 서버 상태를 바꾸는 작업은 **사용자가 요청한 것만** 위임한다.
  - 요청문에는 값·토큰·비밀을 출력하지 말라고 명시하고, 시크릿 관련 파일·모듈 이름 대신 "키 이름" 등으로 돌려 써서 `secrets-guard` 차단을 피한다.
  - 홈서버 세션에서 `systemctl --user`를 다룰 때는 `XDG_RUNTIME_DIR` 지정이 필요할 수 있다.

---

## 3. 자주 쓰는 명령어

```sh
# 의존성 및 .venv 설치
uv sync

# 무전 시작 효과음 받기 (assets/radio-intro.wav, 최초 1회)
uv run python tools/fetch_assets.py

# 실시간 서버 실행 (UDP 20777 + 웹 8093)
uv run f1re serve

# 엔진 시작 상태 지정 (미지정 시 data/engine-state.json 유지, 없으면 off)
uv run f1re serve --engine on
uv run f1re serve --engine off

# 녹화 파일(.f1rec) 리플레이 (웹 서버 미실행)
uv run f1re serve --replay <파일.f1rec> --speed 5

# 게임 없이 데모 패킷 송신 (스즈카 / 모나코, tt / race 등)
uv run f1re-demo --track suzuka --mode tt --host 127.0.0.1 --port 20777 --laps 1 --speed 5
uv run f1re-demo --track suzuka --mode tt --scenario src/f1re/demo_sender/scenarios/alerts.yaml

# 전체 테스트 실행 (pyproject.toml: asyncio_mode="auto", testpaths=["tests"])
uv run pytest
```

- 홈서버 배포(`deploy/f1re.service`, `deploy/README.md`)의 최초 서비스 등록·방화벽 설정은 서버 사용자가 직접 실행하며, 이후 홈서버 상의 확인·유지보수는 사용자의 지시에 따라 Orca `homeserver` 세션에 위임한다.

---

## 4. 아키텍처 및 주요 모듈

### 전체 데이터 흐름

```
F1 25 (UDP 20777) / f1re-demo
   │
   ▼
[udp/receiver.py] ──▶ [udp/parser.py + formats/f2025.py] ──(선택)──▶ [udp/recording.py (.f1rec)]
   │
   ├──▶ [state/store.py (StateStore)] + [state/flags.py] + [state/events.py]
   │         │
   │         └── 1초 주기 DB 반영 ──▶ [db.py (data/f1re.db)]
   │
   └──▶ [pipeline.py (RacePipeline.on_packet)]
             ├── [laps/traces.py (LapTraceRecorder)] ──(완료 랩 비동기 큐)──▶ [coaching/ (Coach, FocusSet, ReferenceSet, ReferenceStore)]
             ├── [coaching/lift_coast.py (LiftCoastCoach)]
             ├── [overtake/advisor.py (OvertakeAdvisor, 10Hz)]
             └── [alerts/rules.py + thresholds.py]
                       │
                       ▼
                 [radio/queue.py (RadioQueue)] ──▶ [tts/ (pipeline, qwen, cache, radiofx, stretch)]
                                                         │
                                                         ▼
[web/app.py (FastAPI 8093)] ◀────────────────── [web/radio_api.py (/radio, WS /ws/radio)]
   ├── GET / · /static/*        (대시보드, 빌드 없는 HTML/JS/CSS)
   ├── GET /api/state · WS /ws/state (10Hz 상태 푸시)
   ├── GET /api/thresholds      (색·문턱값 단일 출처)
   ├── GET /api/engine · POST /api/engine · GET /health
   └── /api/track · /api/report · /api/lift-coast · /api/radio/log · /api/radio/mute
```

### 핵심 모듈별 역할

1. **엔진 ON/OFF 수명 주기 (`src/f1re/engine.py`, `src/f1re/main.py`)**
   - 웹 서버(`uvicorn`)는 항상 떠 있고, `Engine`이 게임 중에만 필요한 `Runtime`(`StateStore`, SQLite 연결, `RadioService`, `TrackMap`, `RacePipeline`, UDP 소켓, DB 반영 태스크)을 켜고 끈다.
   - 마지막 상태는 `data/engine-state.json`에 저장하며 파일이 없으면 `off`로 시작한다. 서버 종료(`aclose`)는 저장 상태를 바꾸지 않는다.
   - `numpy` 등 무거운 모듈은 `Engine.start()` 내부에서 지연 import한다. OFF 상태에서는 `/`, `/radio`, `/static/*`, `/health`, `/api/engine` 외 API·WebSocket이 `503 {"engine": "off"}`를 반환한다.

2. **UDP 수신·파싱·녹화 (`src/f1re/udp/`)**
   - `formats/f2025.py`가 공식 F1 25 패킷(`packetFormat` 2025, 29바이트 헤더) 구조를 정의하고, `parser.py`·`packer.py`가 파싱·직렬화를 담당한다.
   - `session_uid.py`의 `is_demo_session_uid()`로 데모 송신기 세션과 실제 게임 세션을 구분해 기준 랩·서킷 윤곽이 섞이지 않게 분리한다.

3. **실서버 파이프라인·코칭·추월 (`src/f1re/pipeline.py`, `laps/`, `coaching/`, `overtake/`, `tracks/`)**
   - `RacePipeline`이 60Hz 수신 경로를 막지 않기 위해 완료 랩 처리를 별도 비동기 큐(`LAP_QUEUE_MAX = 64`)와 워커 태스크로 분리한다.
   - 트랙 데이터(`tracks/data/suzuka.json`, `monaco.json`)가 없는 서킷은 첫 유효 랩에서 `detect_corners()`·`TrackOutline`으로 자동 학습해 DB(`track_outlines`)에 저장한다.
   - 플래시백·랩 거리 후진·네 바퀴 모두 트랙 이탈 등은 무효 랩으로 처리해 기준 랩(`reference_sets`)에 넣지 않는다.

4. **위험 기준·무전·TTS (`src/f1re/alerts/`, `radio/`, `tts/`)**
   - 위험 문턱값과 대시보드 색은 `config/default.toml`의 `[thresholds]`가 단일 출처다. 화면이 빨간색(`DANGER`)이 되는 조건과 음성 경고 조건이 동일하다.
   - `RadioQueue` 우선순위: `안전 경고 > 박스 지시 > 코너 코칭 > 추월 > 개선 알림`. 동시 발화를 막고 TTL 만료 및 종류별 음소거를 처리한다.
   - `tts/qwen.py`는 `config/default.toml`의 `[tts]` 설정(`qwen3-tts-flash`, `qwen3-tts-flash-realtime`, `voice = "Katerina"`, `language_type = "Korean"`)을 사용하며, `tts/cache.py`·`tts/radiofx.py`·`tts/stretch.py`로 캐시·무전 필터·효과음·속도(`WSOLA`)를 처리한다.

5. **SQLite 저장소 (`src/f1re/db.py`)**
   - 표준 `sqlite3`만 사용하며 기본 경로는 `data/f1re.db`다.
   - 스키마는 `MIGRATIONS` 순차 마이그레이션(`SCHEMA_VERSION = 5`)으로 관리하며 `sessions`, `radio_messages`, `track_outlines`, `reference_sets` 등을 저장한다.

6. **웹 프런트 (`src/f1re/web/static/`)**
   - 번들러 없는 순수 HTML/JS/CSS로 구성된다(`dashboard.html`, `radio.html`, `dashboard.js`, `carstate.js`, `map.js`, `report.js`, `radio.js`, `style.css`).
   - 프런트엔드는 서버 API를 읽기만 하며 문턱값·위험 단계를 직접 계산하지 않는다.

7. **데모 송신기 & 테스트 (`src/f1re/demo_sender/`, `tests/`, `tools/`)**
   - `demo_sender/`는 `alerts.yaml`, `coaching.yaml`, `flags.yaml`, `lift_and_coast.yaml`, `overtake.yaml` 시나리오로 실제 패킷과 동일한 UDP를 생성한다.
   - `tools/`에는 효과음 다운로드(`fetch_assets.py`), 서킷 데이터 생성(`make_tracks.py`), 보이스 샘플 생성(`voice_samples.py`) 스크립트가 있다.
