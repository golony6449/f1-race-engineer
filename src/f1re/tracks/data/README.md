# 서킷 데이터

`f1re`가 쓰는 스즈카·모나코 코너 데이터와 중심선이다. 작업 3(SCRUM-52)에서 처음 만들었다.

## 파일

| 파일 | 트랙 ID | 이름 | 공식 길이 | 코너 |
| --- | --- | --- | --- | --- |
| `suzuka.json` | 13 | 스즈카 서킷 | 5,807 m | 18 |
| `monaco.json` | 5 | 모나코 서킷 | 3,337 m | 19 |

## 형식

```json
{
  "track_id": 13,
  "name": "스즈카 서킷",
  "length_m": 5807.0,
  "corners": [
    {"number": 1, "entry_m": 510.0, "apex_m": 580.2, "exit_m": 630.0}
  ],
  "centerline": [[0.0, 575.801, -34.336], [5.0, 571.4, -33.9]]
}
```

- `length_m`: 공식 서킷 길이(m). 중심선을 이 길이에 맞춰 축척했다. 5 m 간격으로 다시
  샘플링하면서 폴리라인 길이는 1% 안에서 조금 짧아진다.
- `lap_distance_offset_m`(선택): 실제 게임 `lap_distance`와 중심선 `dist_m`의 차이.
  게임 랩 거리에서 이 값을 빼면 중심선 거리다. 스즈카는 130.0 m(SCRUM-57 실측,
  p5~p95 127~137 m), 없으면 0이다. 데모 송신기는 중심선 좌표로 주행하므로 이
  보정을 쓰지 않고, 실제 게임 세션의 지도 차 위치만 보정한다.
- `corners`: 공식 코너 번호 순서(1번부터 연속). `entry_m`~`exit_m`는 랩 시작(결승선)
  기준 랩 거리(m), `apex_m`는 그 구간에서 곡률이 가장 큰 지점이다. 데이터 파일에는 공식
  번호만 넣고, 로더(`f1re.tracks.model.load_track`)가 `official=True`로 채운다.
- `centerline`: `[dist_m, x, z]` 점. `dist_m`은 결승선 기준 랩 거리, `x`·`z`는 m 단위
  로컬 평면 좌표다. 점 간격은 10 m 이하이고 마지막 점에서 첫 점으로 닫힌다.
- 좌표계는 게임 좌표와 맞추지 않았다. 데모 송신기(작업 4)가 같은 좌표를 쓰고, 실제 게임
  좌표와의 정렬은 작업 7에서 확인한다.

### 선택 필드: 코너 이름·DRS 구간·추월 지점

스즈카처럼 추월·방어 데이터가 있는 트랙은 아래 선택 필드를 더 쓴다. 모나코처럼 이
필드가 없는 파일은 수정 없이 그대로 로드되며, `Track.drs_zones`와
`Track.overtaking_spots`는 빈 튜플, `Corner.name`은 `None`이 된다.

```json
{
  "corners": [
    {"number": 16, "name": "카시오 시케인 진입", "entry_m": 5240.0,
     "apex_m": 5266.28, "exit_m": 5298.0}
  ],
  "drs_zones": [
    {
      "name": "메인 스트레이트",
      "detection_start_m": 4900.0,
      "detection_end_m": 5240.0,
      "activation_start_m": 5620.0,
      "activation_end_m": 510.0
    }
  ],
  "overtaking_spots": [
    {
      "name": "카시오 시케인",
      "attack_corners": [16, 17],
      "difficulty": "low",
      "prep_corners": [14, 15],
      "attack_method": "백 스트레이트 슬립스트림 뒤 16번 인코스 늦은 브레이킹",
      "defense_line": "브레이킹 존 전 우측 인코스 커버, 17번 각지게",
      "drs": false,
      "entry_window_m": [5090.0, 5140.0]
    }
  ]
}
```

- `corners[].name`(선택): 코너 통용 이름. 없으면 `None`. 빈 문자열·다른 타입은 오류다.
- `drs_zones[]`(선택): DRS 구간 목록.
  - `detection_start_m`~`detection_end_m`: DRS 사용권을 얻는 감지 창.
  - `activation_start_m`~`activation_end_m`: DRS를 켤 수 있는 활성 창. 끝이 시작보다
    작으면 결승선을 넘는 구간이다(스즈카 활성: 5,620 m → 결승선 0 m → 510 m).
  - `name`(선택): 구간 이름. 비어 있으면 오류다.
  - 스즈카의 감지·활성 거리는 `docs/research/suzuka-overtaking.md`의 추정값이다. 실제
    F1 25 텔레메트리 `lap_distance`와 게임 HUD로 보정해야 한다.
- `overtaking_spots[]`(선택): 주요 추월·주의 지점 목록.
  - `attack_corners`: 공격 대상 코너 번호 목록. 번호 하나만 있을 때는
    `attack_corner`(정수)로 써도 된다. 두 키를 같이 쓰면 오류다.
  - `name`: 지점 이름. `difficulty`: `low`·`medium`·`high`·`very_high` 중 하나.
    조사 문서의 하/중/상/최상을 이 값으로 옮기며, `하~중`처럼 걸친 표현은 더 쉬운
    쪽(`low`)으로 적는다.
  - `prep_corners`: 준비 코너 번호 목록.
  - `attack_method`·`defense_line`: 공격 방법·방어 라인 요약.
  - `drs`: 이 지점 공격이 DRS 활성 구간과 직접 이어지는지(`true`/`false`).
  - `avoid_reason`(선택): 공격을 권하지 않는 이유.
  - `entry_window_m`(선택): 공격·방어 무전을 내기 좋은 랩 거리 `[시작, 끝]`. 끝이
    시작보다 작으면 결승선을 넘는다.
- 모든 코너 번호 참조는 `corners`에 있는 번호여야 한다. 없는 번호, 빈 문자열, 잘못된
  타입·난이도, 0~`length_m` 밖의 거리는 `TrackDataError`를 낸다.
- 추월을 권하지 않는 유지 구간(스즈카 3~10·12·18번)은 `overtaking_spots`에 넣지 않고
  아래 KB에 설명한다. 소비자는 `overtaking_spots`에 없는 구간을 기본적으로 공격
  대상으로 보지 않는다.

## 코너별 KB

LLM 프롬프트와 규칙 엔진이 쓸 코너별 공략·방어 설명은 `src/f1re/tracks/kb/<슬러그>.md`에
둔다. 트랙 ID와 파일 이름은 `kb/__init__.py`의 `_KB_FILES`에서 잇는다(현재 13 →
`suzuka.md`).

- 문서는 `## 코너 <번호>: <이름>` 형식의 2단계 제목으로 섹션을 나눈다. 섹션은 다음
  `## ` 제목이나 문서 끝에서 끝난다.
- 로더: `load_track_kb(track_id)` → `TrackKB | None`,
  `load_corner_kb(track_id, corner_number)` → `str | None`,
  `TrackKB.get_corner_section()`, `get_corner_sections()`, `render()`.
- KB가 없는 트랙은 `None`을 돌려주며 오류가 아니다.
- KB 마크다운은 패키지 데이터로 포함된다(`f1re.tracks.kb`).

## 트랙 ID·길이·코너 번호 출처

- 트랙 ID: EA F1 25 공식 UDP 문서의 트랙 ID 표(스즈카 13, 모나코 5). PL이 전달한 값이다.
- 길이·코너 수:
  - Suzuka: Wikipedia "Suzuka Circuit" infobox — 5.807 km, 18 turns.
    <https://en.wikipedia.org/wiki/Suzuka_Circuit>
  - Monaco: Wikipedia "Circuit de Monaco" — 3.337 km, 19 turns.
    <https://en.wikipedia.org/wiki/Circuit_de_Monaco>
- 코너 번호:
  - Monaco: F1 공식 트랙 맵(1~19번 표기)
    <https://media.formula1.com/image/upload/f_auto/q_auto/v1677244984/content/dam/fom-website/2018-redesign-assets/Circuit%20maps%2016x9/Monaco_Circuit.png>
    과 F1 "How every Monaco corner got its name"
    <https://www.formula1.com/en/latest/article/explained-how-every-monaco-corner-got-its-name.5ClYqWmfpeUpsRJA6mDNaX>
  - Suzuka: F1 공식 트랙 맵(1~18번 표기)
    <https://media.formula1.com/image/upload/f_auto/q_auto/v1677250050/content/dam/fom-website/2018-redesign-assets/Circuit%20maps%2016x9/Japan_Circuit.png>,
    racetrackdesigner "Suzuka: layout, corners and history"
    (<https://racetrackdesigner.com/tracks/suzuka.html>), Goodyear Dunlop EWC
    프레스팩(8 left / 10 right, Degner 8–9, 130R 15, 시케인 16–17). Wikipedia의
    "Turn 7 — Dunlop curve", "Turn 15 — 130R" 표기와도 맞는다.
- 코너 이름과 대략 위치는 Wikipedia의 코너 설명과 F1 공식 트랙 맵을 참고했다.

## 중심선 출처와 가공

- OpenStreetMap 서킷 관계의 도로 형상을 썼다.
  - 스즈카: relation 284570 "鈴鹿サーキット"
    <https://www.openstreetmap.org/relation/284570>
  - 모나코: relation 148194 "Circuit de Monaco"
    <https://www.openstreetmap.org/relation/148194>
- © OpenStreetMap contributors, ODbL 1.0 (<https://www.openstreetmap.org/copyright>).
  데이터는 2026-11-12에 내려받았다.
- `tools/make_tracks.py`가 관계 멤버 도로를 끝점이 맞닿는 순서로 이어 붙이고, 로컬 미터
  좌표로 투영하고, 작은 단차를 스무딩하고, 주요 직선(스즈카 메인·서측, 모나코 스타트)을
  곧게 편 뒤 공식 길이에 맞춰 축척하고 5 m 간격으로 다시 샘플링한다.
- 랩 거리 0(결승선) 기준:
  - 모나코: OSM 관계의 `start-finish` 노드.
  - 스즈카: OSM에 결승선 노드가 없어 피트 빌딩(way 184422099) 중심을 결승선 위치로
    근사했다. 실제 게임 `lapDistance`와는 130 m 차이가 나서(SCRUM-57 실측)
    `lap_distance_offset_m`에 적었다.
- 코너 `entry_m`/`exit_m`는 공식 번호 순서대로 정한 랩 거리 구간이고, `apex_m`는 그 안에서
  곡률이 가장 큰 지점이다. 구간이 실제 코너 곡률을 덮는지는
  `tests/tracks/test_model.py::test_corner_intervals_have_curvature_and_straights_do_not`가
  확인한다.
- 다른 앱의 데이터 파일은 쓰지 않았다.

## 다시 만들기

```bash
uv run python tools/make_tracks.py
```

OSM API에서 XML을 내려받는다. 이미 받아 둔 파일이 있으면 경로를 준다.

```bash
uv run python tools/make_tracks.py --suzuka-osm suzuka.osm --monaco-osm monaco.osm
```
