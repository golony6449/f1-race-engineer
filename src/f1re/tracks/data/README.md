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
- `corners`: 공식 코너 번호 순서(1번부터 연속). `entry_m`~`exit_m`는 랩 시작(결승선)
  기준 랩 거리(m), `apex_m`는 그 구간에서 곡률이 가장 큰 지점이다. 데이터 파일에는 공식
  번호만 넣고, 로더(`f1re.tracks.model.load_track`)가 `official=True`로 채운다.
- `centerline`: `[dist_m, x, z]` 점. `dist_m`은 결승선 기준 랩 거리, `x`·`z`는 m 단위
  로컬 평면 좌표다. 점 간격은 10 m 이하이고 마지막 점에서 첫 점으로 닫힌다.
- 좌표계는 게임 좌표와 맞추지 않았다. 데모 송신기(작업 4)가 같은 좌표를 쓰고, 실제 게임
  좌표와의 정렬은 작업 7에서 확인한다.

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
    근사했다. 작업 7에서 실제 게임 `lapDistance`와 맞출 때 보정한다.
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
