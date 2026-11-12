# F1 25 UDP 규격 메모

이 폴더에는 EA 공식 UDP 규격 원문(txt·pdf)을 두지 않는다. 공개 레포이고 EA
저작물이기 때문이다. 규격의 출처와 구현 범위, 2026 Season Pack과의 차이만
적어 둔다. 원문은 작업 중 `.ds-tmp/spec/`에만 두었고 커밋하지 않았다.

## 출처

- 게시물: **EA SPORTS™ F1®25: 2026 Season Pack UDP SPECIFICATION**, VERSION 11.0
- URL: <https://forums.ea.com/blog/f1-games-game-info-hub-en/ea-sports%E2%84%A2-f1%C2%AE25-2026-season-pack-udp-specification/12187347>
- 첨부 파일
  - `F1 25 Telemetry Output Structures.txt` — F1 25(`packetFormat` 2025) C 구조체
  - `Data Output from F1 25 v3.pdf` — 패킷·필드 설명
- 받은 날짜: 2026-11-12

## 구현 범위

- 지원 형식은 **`packetFormat` 2025 하나**다. 사용자 결정("2025 먼저, 2026 대비")에
  따라 2026 Season Pack(`packetFormat` 2026)은 파싱하지 않는다. 파서는
  `FORMATS`에 없는 형식이면 `UnsupportedPacket("format")`으로 거부한다.
- 형식별 정의는 `src/f1re/udp/formats/f2025.py`에 있고, `src/f1re/udp/spec.py`의
  `FORMATS = {2025: f2025}`로 등록한다. 2026을 넣을 때는 `formats/`에 모듈을
  추가하고 `FORMATS`에 등록한다.
- 패킷 16종(0~15)의 구조체를 모두 옮겼다. 필드 이름은 원문의 `m_` 접두어를 뺀
  snake_case다.
- `PACKET_SIZES`는 공식 문서의 패킷별 전체 바이트 수(헤더 29바이트 포함)다.
  테스트가 정의 크기 합과 이 값을 매번 대조한다.

## 2025 패킷 크기

| 패킷 | ID | 바이트 |
| --- | ---: | ---: |
| Motion | 0 | 1349 |
| Session | 1 | 753 |
| Lap Data | 2 | 1285 |
| Event | 3 | 45 |
| Participants | 4 | 1284 |
| Car Setups | 5 | 1133 |
| Car Telemetry | 6 | 1352 |
| Car Status | 7 | 1239 |
| Final Classification | 8 | 1042 |
| Lobby Info | 9 | 954 |
| Car Damage | 10 | 1041 |
| Session History | 11 | 1460 |
| Tyre Sets | 12 | 231 |
| Motion Ex | 13 | 273 |
| Time Trial | 14 | 101 |
| Lap Positions | 15 | 1131 |

## 2026 Season Pack과의 차이

원문 두 파일을 비교한 요약이다. 이번 구현에는 반영하지 않았다.

| 항목 | 2025 | 2026 Season Pack |
| --- | --- | --- |
| `packetFormat` | 2025 | 2026 |
| 공통 헤더 | 29바이트 | 같음(29바이트) |
| 차 수 상한 | 22 | 24 (모든 차 배열·관련 패킷 크기 증가) |
| 새 패킷 | — | ID 16 Car Telemetry 2, 269바이트(액티브 에어로 모드·가용 여부·활성화 거리, 오버테이크 모드, 2026 규정 여부, 역주행 여부) |
| Motion | g-force 3개가 float | int16 양자화(÷1000), 1349 → 1325바이트 |
| Session | 753바이트 | 926바이트. 액티브 에어로 존(전체·부분, 각 최대 8), DRS 존(최대 4), 스타트 반응 시간, ABS·TC 어시스트, 레이싱 라인 고대비·색각 보정, 리와인드 안내, `formula` 13 = F1 26, ERS 배포 모드 3 = boost 추가 |
| Event | 45바이트 | 크기 같음. Collision 상세에 `severity` 추가 |
| Participants | driverId·networkId·teamId `uint8` | `uint16`(255 → 65535), 1284 → 1470바이트 |
| Car Setups | 1133바이트 | 1233바이트 |
| Car Telemetry | 1352바이트, 엔진 온도 `uint16` | 1448바이트, 엔진 온도 `uint8` |
| Car Status | 1239바이트 | 1445바이트. 랩당 ERS 회수 한도(`m_ersHarvestLimitPerLap`) 추가 |
| Final Classification | 1042바이트 | 1134바이트 |
| Lobby Info | 954바이트 | 1062바이트. teamId `uint8` → `uint16` |
| Car Damage | 1041바이트 | 1133바이트 |
| Session History | 1460바이트 | 같음 |
| Tyre Sets | 231바이트 | 같음 |
| Motion Ex | 273바이트 | 같음 |
| Time Trial | 101바이트 | 104바이트. teamId `uint8` → `uint16` |
| Lap Positions | 1131바이트 | 1231바이트 |

## Event 패킷 메모

- `m_eventStringCode`는 원문에서 `uint8[4]`지만 4글자 이벤트 코드(`SSTA`,
  `FTLP`, `FLBK`, `BUTN` 등)를 문자열로 다룬다. 파서 body의 `event_string_code`가
  문자열이다.
- `m_eventDetails` union은 이벤트 코드에 따라 해당 구조체로 푼다. 구조체가 없는
  코드(`SSTA`, `SEND`, `DRSE`, `CHQF`, `LGOT`, `RDFL`)는 빈 dict, 모르는 코드는
  원본 12바이트를 `{"raw": bytes}`로 둔다.
- union 크기는 가장 큰 멤버(SpeedTrap)인 12바이트다.
