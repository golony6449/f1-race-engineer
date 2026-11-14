# 코너 트랙 이탈 오판정 고치기 계획 (SCRUM-93)

일감: https://golony.atlassian.net/browse/SCRUM-93

## 배경

2026-11-14 스즈카 세션(실제 게임)에서 매 코너 "N번에서 트랙 벗어났어. 라인이 M미터 밖이었어"가
나왔다. 지금 트랙 이탈 판정은 `max_line_dev_m >= off_track_dev_m(5 m)`
(`src/f1re/coaching/metrics.py`의 `corner_metrics`)이다. 바퀴·노면은 보지 않는다.

원인(PL이 실제 데이터로 확인):

- 저장소 트랙 데이터(`src/f1re/tracks/data/suzuka.json`)의 좌표는 게임 좌표와 맞추지 않았다
  (`src/f1re/tracks/data/README.md`). 데모 송신기는 이 좌표를 쓴다.
- DB에 저장된 기준 랩("self")이 데모 송신기 세션에서 저장된 것이라 저장소 좌표계다. 실제 게임 랩의
  Motion 세계 좌표와 비교하니 같은 랩 거리에서 수백 m 차이가 났다.
- 그래서 기준 궤적 이탈 거리가 110~1157 m로 나와 모든 코너가 트랙 이탈이 됐다.

사용자 요구: **바퀴 4개가 모두 트랙 밖으로 나간 경우만 트랙 이탈로 본다.** 연석을 밟거나 바퀴
하나만 나간 것은 이탈이 아니다.

## 실제 표본

같은 순간(세션 5099945309337276856, 스즈카 track_id 13) 실제 게임 패킷의 차 위치와 저장소 윤곽
(`suzuka.json` centerline) 같은 랩 거리 위치:

| 차 | lap_distance | Motion world (x, z) | 윤곽 위치 (x, z) | 차이 |
|---|---|---|---|---|
| 0 | 4781 | (-251.5, 29.5) | (-259.2, -40.6) | 71 m |
| 5 | 4456 | (-557.5, -78.8) | (-554.1, 94.9) | 174 m |
| 19(내 차) | 4276 | (-720.6, -153.8) | (-717.5, 170.0) | 324 m |

그때 코너 지표(내 차 1랩, 1번 코너, 기준 "self"):
`player.max_line_dev_m = 929.5`, `off_track = true`, `reference.max_line_dev_m = 0.0`.
같은 세션 코너별 이탈 거리: 930, 1156, 1048, 937, 736, 584, 302, 287, 375, 283, 497, 514, 788, 838, 110, 223, 263, 309(1~18번).

Car Telemetry 패킷의 바퀴별 노면(`surface_type`, 4개, 순서 RL·RR·FL·FR)은 트랙 위에서
`[0, 0, 0, 0]`이었다. F1 25 노면 값: 0 아스팔트, 1 연석(rumble strip), 2 콘크리트, 3 바위,
4 자갈, 5 진흙, 6 모래, 7 잔디, 8 물, 9 자갈길(cobblestone), 10 금속, 11 요철(ridged).

## Global Constraints

- 주석·문서·커밋 메시지는 한국어. 테스트는 `uv run pytest`.
- 의존성 파일(`pyproject.toml`, `uv.lock`)은 바꾸지 않는다. 공용 `conftest.py`는 만들지 않는다.
- 저장된 기준 랩 JSON(`reference_sets`)에는 노면 값이 없다. 옛 데이터를 읽을 때 깨지지 않아야 한다.

### 작업 1: 샘플에 바퀴별 노면 기록

**Files:**
- Modify: `src/f1re/laps/traces.py`
- Modify: `src/f1re/coaching/reference.py`
- Test: `tests/laps/test_traces.py`
- Test: `tests/coaching/test_reference.py`

1. `Sample`에 `surface: tuple[int, ...] | None = None` 필드를 끝에 추가한다(기존 위치 인자 호출이 깨지지 않게).
2. `_make_sample`에서 Car Telemetry `surface_type`이 길이 4의 정수 목록이면 `tuple`로 넣는다. 아니면 `None`.
   모든 차에 넣는다(Car Telemetry는 모든 차 값이 있다).
3. `reference.py`의 `_sample_to_dict`/`_sample_from_dict`가 `surface`를 저장·복원한다. 키가 없거나 형식이
   틀리면 `None`(옛 데이터 호환). `REFERENCE_PAYLOAD_VERSION`은 올리지 않는다(필드 추가만 하고 옛 값을 읽을 수 있으므로).
4. 테스트: `surface_type=[0, 1, 7, 7]` 패킷 → 샘플 `surface == (0, 1, 7, 7)`. 저장·복원 왕복, `surface` 없는 옛 dict 복원.

### 작업 2: 트랙 이탈은 4바퀴 모두 밖일 때만, 비정상 라인 거리는 버리기

**Files:**
- Modify: `src/f1re/coaching/metrics.py`
- Modify: `config/default.toml`
- Modify: `src/f1re/coaching/coach.py`
- Modify: `src/f1re/radio/templates.py`
- Test: `tests/coaching/test_metrics.py`
- Test: `tests/coaching/test_coach.py`

1. `metrics.py`에 `ON_TRACK_SURFACES = frozenset({0, 1})`(아스팔트, 연석)를 둔다. 이유를 주석으로 적는다.
2. `corner_metrics`의 `off_track`: 코너 구간(`corner.entry_m` ~ `corner.exit_m`) 안 샘플 중
   `surface`가 있고 **4개 모두 `ON_TRACK_SURFACES`에 없는** 샘플이 하나라도 있으면 `True`.
   `surface`가 없는 샘플만 있으면(데모·옛 기준 랩) `False`. 라인 거리(`max_line_dev_m`)로는 더 이상 정하지 않는다.
   - 연석 1개 + 잔디 3개(`(1, 7, 7, 7)`)는 이탈이 아니다. `(7, 7, 7, 7)`, `(4, 7, 6, 7)`은 이탈이다.
3. 라인 거리 상한: `[coaching.thresholds]`에 `line_dev_max_m = 50.0`을 추가한다(`CoachingThresholds` 필드,
   양수 검사 목록에 넣음). `_line_deviation`에서 진입·정점·탈출 세 지점 중 하나라도 거리가 이 값을 넘으면
   좌표계가 다른 기준 랩(데모 좌표 등)으로 보고 `0.0`을 돌려준다(라인 판단 안 함). 주석에 실제 표본(110~1157 m)을 근거로 적는다.
4. 쓰지 않게 된 `off_track_dev_m` 문턱값은 설정·필드에서 지운다. 지우면 깨지는 테스트는 새 규칙에 맞게 고친다.
5. `coach.py`의 트랙 이탈 원인(`CAUSE_OFF_TRACK`)과 템플릿 `coaching.off_track`·`coaching.off_track_ordinal`:
   더 이상 미터를 말하지 않는다. 예: calm "{corner}번에서 네 바퀴 다 나갔어.", "{corner}번 트랙 밖으로 나갔어.",
   hyped "{corner}번 나갔어! 트랙 안에 붙여!" 등. ordinal 판은 `{corner_label}`을 쓴다. 표현 개수·감정 단계 구조는
   기존 템플릿 규칙(`templates.py`의 검사 코드)을 따른다. `Cause`의 `slot`/`value`는 템플릿 검사가 요구하는 대로
   맞춘다(필요하면 바깥으로 나간 바퀴 수 4를 `value`로 둔다). `detail`에 `line_dev_m`은 그대로 둬도 된다.
6. 테스트:
   - 실제 표본처럼 기준 궤적과 수백 m 떨어진 위치 + 노면 `[0,0,0,0]` → `off_track False`, `max_line_dev_m == 0.0`, 코치가 `line_wide`·`off_track` 원인을 내지 않는다.
   - 코너 구간에 `(7,7,7,7)` 샘플 → `off_track True`, 원인 `off_track`.
   - `(1,7,7,7)`만 있음 → `off_track False`.
   - 구간 밖(진입 앞)의 `(7,7,7,7)` → `off_track False`.
   - 라인 거리 10 m(상한 안) → 지금처럼 `line_wide` 판정이 된다.

### 작업 3: 데모 세션 기준 랩 분리

**Files:**
- Create: `src/f1re/udp/session_uid.py`
- Modify: `src/f1re/demo_sender/sender.py`
- Modify: `src/f1re/pipeline.py`
- Test: `tests/udp/test_session_uid.py`
- Test: `tests/test_pipeline_integration.py`

1. `src/f1re/udp/session_uid.py`: `DEMO_SESSION_UID_PREFIX = 0x5EED_0000`(위 32비트),
   `demo_session_uid(seed: int) -> int`(지금 데모 송신기 식과 같은 값), `is_demo_session_uid(uid: int | None) -> bool`
   (`uid >> 32 == DEMO_SESSION_UID_PREFIX`, `None`이면 False).
2. 데모 송신기는 `demo_session_uid(seed)`로 UID를 만든다(값은 지금과 같아야 한다).
3. 파이프라인: 지금 세션 UID가 데모면
   - 저장된 기준을 불러오지 않는다(`_build_coaches`에서 `load_compatible`을 부르지 않고 빈 `ReferenceSet`으로 시작).
   - 기준 랩을 저장하지 않는다(`_save_reference`가 아무것도 하지 않음).
   - 학습 윤곽을 저장하지 않는다(`remember_outline`을 부르지 않음).
   - 세션 안의 코칭(그 세션 랩끼리 비교)은 지금처럼 한다.
4. 테스트: `is_demo_session_uid(0x5EED_0000_1234_5678)` True, 실제 표본 UID 3개(`0x65153d81f29b6d98`, `0x46c6a53cf8f7d1b8`, `0x0c639f026e854e87`) False, `None` False.
   데모 UID 세션은 DB에 기준이 있어도 불러오지 않고, 랩을 끝내도 `reference_sets`에 저장하지 않는다. 실제 UID 세션은 지금처럼 불러오고 저장한다.

## Review Focus

1. 트랙 이탈이 "4바퀴 모두 아스팔트·연석이 아님"일 때만 참인가. 바퀴 하나라도 아스팔트·연석이면 이탈이 아닌가.
2. `surface`가 없는 옛 기준 랩·데모 샘플에서 오류 없이 `off_track False`가 되는가. 옛 `reference_sets` JSON을 읽을 수 있는가.
3. 상한(50 m)을 넘는 라인 거리가 `line_wide` 원인을 만들지 않는가.
4. 트랙 이탈 무전에 더 이상 미터 수치가 나오지 않는가. 템플릿 검사 테스트가 통과하는가.
5. 데모 세션(UID 위 32비트 0x5EED0000)은 기준 랩·윤곽을 불러오지도 저장하지도 않는가. 실제 세션 동작은 그대로인가.
