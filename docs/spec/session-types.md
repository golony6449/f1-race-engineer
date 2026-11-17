# F1 25 세션 종류(`m_sessionType`) 표

F1 25 UDP Session 패킷의 `m_sessionType` 공식 값 표다. 코드의 단일 출처는
`src/f1re/udp/session_category.py`이고, 이 문서는 규격 값과 녹화 범주 매핑을
설명한다.

## 출처

- EA SPORTS™ F1®25: 2026 Season Pack UDP SPECIFICATION, VERSION 11.0
- URL: <https://forums.ea.com/blog/f1-games-game-info-hub-en/ea-sports%E2%84%A2-f1%C2%AE25-2026-season-pack-udp-specification/12187347>
- 첨부 파일의 Session 패킷 구조체(`m_sessionType`)와 필드 설명
- 확인 날짜: 2026-11-15

저작물 원문 파일은 이 레포에 넣지 않는다(공개 레포). 규격 원문은 출처 URL에서
직접 확인한다.

## 값 표

| 값 | 세션 종류 | 녹화 범주 |
| ---: | --- | --- |
| 0 | Unknown | `unknown` |
| 1 | Practice 1 (P1) | `practice` |
| 2 | Practice 2 (P2) | `practice` |
| 3 | Practice 3 (P3) | `practice` |
| 4 | Short Practice | `practice` |
| 5 | Qualifying 1 (Q1) | `qualifying` |
| 6 | Qualifying 2 (Q2) | `qualifying` |
| 7 | Qualifying 3 (Q3) | `qualifying` |
| 8 | Short Qualifying | `qualifying` |
| 9 | One-Shot Qualifying | `qualifying` |
| 10 | Sprint Shootout 1 (SS1) | `qualifying` |
| 11 | Sprint Shootout 2 (SS2) | `qualifying` |
| 12 | Sprint Shootout 3 (SS3) | `qualifying` |
| 13 | Short Sprint Shootout | `qualifying` |
| 14 | One-Shot Sprint Shootout | `qualifying` |
| 15 | Race (스프린트 레이스 포함) | `race` |
| 16 | Race 2 | `race` |
| 17 | Race 3 | `race` |
| 18 | Time Trial | `time_trial` |

그 밖의 값과 패킷 미수신(`None`)은 `unknown`이다.

## 묶음 상수

`src/f1re/udp/session_category.py`가 다음 묶음을 정의하고, 판정 함수
(`is_practice_session`, `is_qualifying_session`, `is_sprint_shootout_session`,
`is_race_session`, `is_time_trial_session`, `session_category`)가 이 값만 쓴다.

| 이름 | 값 |
| --- | --- |
| `PRACTICE_SESSION_TYPES` | `{1, 2, 3, 4}` |
| `QUALIFYING_SESSION_TYPES` | `{5, 6, 7, 8, 9}` |
| `SPRINT_SHOOTOUT_SESSION_TYPES` | `{10, 11, 12, 13, 14}` |
| `RACE_SESSION_TYPES` | `{15, 16, 17}` |
| `TIME_TRIAL_SESSION_TYPES` | `{18}` |
| `SESSION_TYPE_RACE` | `15` |
| `SESSION_TYPE_TIME_TRIAL` | `18` |
| `TIME_TRIAL_SESSION_TYPE` | `18` |

## 결정 메모

- **F1 23 값과 다르다.** F1 23까지는 10~12가 레이스, 13이 타임 트라이얼이었다.
  F1 25에서는 10~14가 스프린트 슛아웃, 15~17이 레이스, 18이 타임 트라이얼이다.
  이전 값을 하드코딩한 판정은 실전 레이스를 레이스로 인식하지 못한다
  (SCRUM-131에서 수정).
- **스프린트 슛아웃은 예선 범주로 묶는다.** 녹화 폴더 레이아웃은 설계 4.1의
  5개 범주(`practice`, `qualifying`, `race`, `time_trial`, `unknown`)를
  유지한다. 스프린트 슛아웃은 예선과 같은 주말 세션 성격이므로
  `session_category()`가 `qualifying`으로 매핑한다. 세션 종류 자체를 구분해야
  하는 곳은 `SPRINT_SHOOTOUT_SESSION_TYPES`/`is_sprint_shootout_session()`을
  쓴다.
- **스프린트 레이스는 레이스다.** 15~17에 포함되므로 레이스 거리·연료·리프트
  앤 코스트·추월 안내가 모두 켜진다.
- **DB 재분류.** 마이그레이션 007이 `session_recordings.session_type`이 있는
  기존 행의 `category`를 이 표대로 다시 계산한다. 파일 경로는 바꾸지 않는다.
