"""스즈카 추월·DRS 데이터와 코너별 마크다운 KB 로드."""

from __future__ import annotations

from importlib import resources
from typing import Any, Callable

import pytest

from f1re.tracks.kb import _parse_sections, load_corner_kb, load_track_kb
from f1re.tracks.model import (
    OVERTAKING_DIFFICULTIES,
    Corner,
    DrsZone,
    OvertakingSpot,
    Track,
    TrackDataError,
    _build_track,
    load_track,
)

SUZUKA_ID = 13
MONACO_ID = 5

EXPECTED_CORNER_NAMES = {
    1: "1번 코너",
    2: "2번 코너",
    3: "S커브 1",
    4: "S커브 2",
    5: "S커브 3",
    6: "역뱅크",
    7: "던롭 커브",
    8: "데그너 1",
    9: "데그너 2",
    10: "무명 우코너 (110R)",
    11: "헤어핀",
    12: "200R",
    13: "스푼 커브 1",
    14: "스푼 커브 2",
    15: "130R",
    16: "카시오 시케인 진입",
    17: "카시오 시케인 탈출",
    18: "최종 코너",
}


# ---------------------------------------------------------------------------
# 스즈카 데이터


def test_suzuka_corner_names() -> None:
    track = load_track(SUZUKA_ID)

    assert track is not None
    assert {corner.number: corner.name for corner in track.corners} == (
        EXPECTED_CORNER_NAMES
    )


def test_suzuka_drs_zone() -> None:
    track = load_track(SUZUKA_ID)

    assert track is not None
    assert len(track.drs_zones) == 1
    zone = track.drs_zones[0]
    assert isinstance(zone, DrsZone)
    assert zone.name == "메인 스트레이트"
    assert (zone.detection_start_m, zone.detection_end_m) == (4900.0, 5240.0)
    assert (zone.activation_start_m, zone.activation_end_m) == (5620.0, 510.0)
    # 활성 구간은 최종 코너 탈출에서 결승선을 넘어 1번 코너 앞까지다.
    assert zone.activation_wraps


def test_suzuka_overtaking_spots() -> None:
    track = load_track(SUZUKA_ID)

    assert track is not None
    spots = {spot.name: spot for spot in track.overtaking_spots}
    assert set(spots) == {"카시오 시케인", "1번 코너", "헤어핀", "스푼 커브", "130R"}

    corner_numbers = {corner.number for corner in track.corners}
    for spot in spots.values():
        assert isinstance(spot, OvertakingSpot)
        assert spot.difficulty in OVERTAKING_DIFFICULTIES
        assert spot.attack_corners
        assert spot.prep_corners
        assert set(spot.attack_corners) <= corner_numbers
        assert set(spot.prep_corners) <= corner_numbers
        assert spot.attack_method.strip()
        assert spot.defense_line.strip()

    casio = spots["카시오 시케인"]
    assert casio.attack_corners == (16, 17)
    assert casio.prep_corners == (14, 15)
    assert casio.difficulty == "low"
    assert casio.drs is False
    assert casio.entry_window_m == (5090.0, 5140.0)

    first_corner = spots["1번 코너"]
    assert first_corner.attack_corners == (1, 2)
    assert first_corner.prep_corners == (17, 18)
    assert first_corner.drs is True
    # 결승선을 넘는 무전 창이다.
    assert first_corner.entry_window_m == (5800.0, 430.0)

    hairpin = spots["헤어핀"]
    assert hairpin.attack_corners == (11,)
    assert hairpin.prep_corners == (9,)

    spoon = spots["스푼 커브"]
    assert spoon.attack_corners == (13, 14)
    assert spoon.difficulty == "high"

    fast_corner = spots["130R"]
    assert fast_corner.difficulty == "very_high"
    assert fast_corner.avoid_reason is not None
    assert "고위험" in fast_corner.avoid_reason


# ---------------------------------------------------------------------------
# 하위 호환


def test_monaco_still_loads_without_optional_fields() -> None:
    track = load_track(MONACO_ID)

    assert track is not None
    assert len(track.corners) == 19
    assert all(corner.name is None for corner in track.corners)
    assert track.drs_zones == ()
    assert track.overtaking_spots == ()


def test_constructor_backward_compatibility() -> None:
    corner = Corner(1, True, 10.0, 20.0, 30.0)
    track = Track(track_id=99, name="테스트 서킷", length_m=100.0, corners=[corner])

    assert corner.name is None
    assert track.drs_zones == ()
    assert track.overtaking_spots == ()


# ---------------------------------------------------------------------------
# 스키마 검증


def _sample_raw() -> dict[str, Any]:
    """선택 필드 검증에 쓰는 작은 정상 트랙."""
    return {
        "track_id": 99,
        "name": "테스트 서킷",
        "length_m": 1000.0,
        "corners": [
            {
                "number": 1,
                "name": "테스트 1코너",
                "entry_m": 100.0,
                "apex_m": 150.0,
                "exit_m": 200.0,
            },
            {"number": 2, "entry_m": 300.0, "apex_m": 350.0, "exit_m": 400.0},
            {"number": 3, "entry_m": 500.0, "apex_m": 550.0, "exit_m": 600.0},
        ],
        "drs_zones": [
            {
                "name": "메인 스트레이트",
                "detection_start_m": 600.0,
                "detection_end_m": 800.0,
                "activation_start_m": 900.0,
                "activation_end_m": 100.0,
            }
        ],
        "overtaking_spots": [
            {
                "name": "1번 코너",
                "attack_corners": [1],
                "difficulty": "medium",
                "prep_corners": [3],
                "attack_method": "인코스 늦은 브레이킹",
                "defense_line": "인코스 선점",
                "drs": True,
                "entry_window_m": [850.0, 50.0],
            }
        ],
    }


def _spot(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["overtaking_spots"][0]


def _set_spot(**values: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(raw: dict[str, Any]) -> None:
        _spot(raw).update(values)

    return mutate


def _delete_spot(key: str) -> Callable[[dict[str, Any]], None]:
    def mutate(raw: dict[str, Any]) -> None:
        del _spot(raw)[key]

    return mutate


def _set_zone(**values: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(raw: dict[str, Any]) -> None:
        raw["drs_zones"][0].update(values)

    return mutate


def _delete_zone(key: str) -> Callable[[dict[str, Any]], None]:
    def mutate(raw: dict[str, Any]) -> None:
        del raw["drs_zones"][0][key]

    return mutate


INVALID_RAW = [
    pytest.param(_set_spot(attack_corners=[99]), id="없는 공격 코너"),
    pytest.param(_set_spot(prep_corners=[99]), id="없는 준비 코너"),
    pytest.param(_set_spot(difficulty="extreme"), id="허용되지 않은 난이도"),
    pytest.param(_set_spot(name=""), id="빈 추월 지점 이름"),
    pytest.param(_set_spot(attack_method="  "), id="빈 공격 방법"),
    pytest.param(_set_spot(defense_line=""), id="빈 방어 라인"),
    pytest.param(_set_spot(drs="yes"), id="drs 타입 오류"),
    pytest.param(_delete_spot("attack_corners"), id="공격 코너 없음"),
    pytest.param(_set_spot(attack_corner=1), id="공격 코너 키 중복"),
    pytest.param(_set_spot(attack_corners=[]), id="빈 공격 코너 목록"),
    pytest.param(_set_spot(attack_corners=[1, 1]), id="공격 코너 중복"),
    pytest.param(_set_spot(attack_corners=1), id="공격 코너 목록 타입 오류"),
    pytest.param(_set_spot(attack_corners=[1.0]), id="공격 코너 정수 아님"),
    pytest.param(_set_spot(entry_window_m=[850.0]), id="거리 창 길이 오류"),
    pytest.param(_set_spot(entry_window_m=[850.0, 1200.0]), id="거리 창 범위 오류"),
    pytest.param(_set_spot(entry_window_m=["850", "50"]), id="거리 창 문자열"),
    pytest.param(_set_spot(avoid_reason=""), id="빈 회피 이유"),
    pytest.param(
        _set_zone(detection_start_m=900.0, detection_end_m=800.0),
        id="감지 구간 역순",
    ),
    pytest.param(_set_zone(activation_end_m=1200.0), id="활성 구간 범위 오류"),
    pytest.param(_set_zone(detection_start_m="600"), id="DRS 거리 문자열"),
    pytest.param(_set_zone(activation_start_m=True), id="DRS 거리 불리언"),
    pytest.param(_delete_zone("detection_start_m"), id="DRS 필수 값 없음"),
    pytest.param(_set_zone(name=""), id="빈 DRS 이름"),
    pytest.param(lambda raw: raw.update(drs_zones={}), id="DRS 목록 타입 오류"),
    pytest.param(
        lambda raw: raw.update(overtaking_spots={}), id="추월 지점 목록 타입 오류"
    ),
    pytest.param(
        lambda raw: raw["corners"][0].update(name=""), id="빈 코너 이름"
    ),
    pytest.param(
        lambda raw: raw["corners"][0].update(name=123), id="코너 이름 타입 오류"
    ),
]


@pytest.mark.parametrize("mutate", INVALID_RAW)
def test_invalid_schema_raises_track_data_error(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    raw = _sample_raw()
    mutate(raw)

    with pytest.raises(TrackDataError):
        _build_track(raw, "sample.json")


def test_missing_optional_fields_load_as_empty() -> None:
    raw = _sample_raw()
    del raw["drs_zones"]
    del raw["overtaking_spots"]

    track = _build_track(raw, "sample.json")

    assert track.drs_zones == ()
    assert track.overtaking_spots == ()
    assert [corner.name for corner in track.corners] == ["테스트 1코너", None, None]


def test_attack_corner_singular_is_accepted() -> None:
    raw = _sample_raw()
    del _spot(raw)["attack_corners"]
    _spot(raw)["attack_corner"] = 2

    track = _build_track(raw, "sample.json")

    assert track.overtaking_spots[0].attack_corners == (2,)


# ---------------------------------------------------------------------------
# 코너별 KB


def test_suzuka_kb_has_every_corner_section() -> None:
    kb = load_track_kb(SUZUKA_ID)

    assert kb is not None
    assert kb.track_id == SUZUKA_ID
    assert set(kb.sections) == set(range(1, 19))
    assert "메인 스트레이트" in kb.sections[1]
    assert "스푼" in kb.sections[14]
    assert "카시오" in kb.sections[16]


def test_load_corner_kb() -> None:
    section = load_corner_kb(SUZUKA_ID, 16)

    assert section is not None
    assert section.startswith("## 코너 16:")
    assert "카시오" in section
    assert load_corner_kb(SUZUKA_ID, 99) is None


def test_track_without_kb_returns_none() -> None:
    assert load_track_kb(MONACO_ID) is None
    assert load_track_kb(999) is None
    assert load_corner_kb(MONACO_ID, 1) is None


def test_kb_render_prep_and_attack_sections() -> None:
    kb = load_track_kb(SUZUKA_ID)

    assert kb is not None
    sections = kb.get_corner_sections([14, 15, 16])
    assert len(sections) == 3
    assert sections[0].startswith("## 코너 14:")
    assert sections[1].startswith("## 코너 15:")
    assert sections[2].startswith("## 코너 16:")
    assert kb.render([16]) == kb.get_corner_section(16)

    full = kb.render()
    assert full.startswith("## 코너 1:")
    assert full.index("## 코너 1:") < full.index("## 코너 18:")


def test_overtaking_spot_corners_have_kb_sections() -> None:
    track = load_track(SUZUKA_ID)
    kb = load_track_kb(SUZUKA_ID)

    assert track is not None
    assert kb is not None
    for spot in track.overtaking_spots:
        for number in (*spot.attack_corners, *spot.prep_corners):
            assert kb.get_corner_section(number) is not None


def test_kb_file_is_packaged() -> None:
    assert resources.files("f1re.tracks.kb").joinpath("suzuka.md").is_file()


def test_non_corner_heading_ends_section() -> None:
    text = "## 코너 1: 첫 코너\n본문\n## 요약\n요약 내용\n## 코너 2: 둘째 코너\n본문 2\n"

    sections = _parse_sections(text, "test.md")

    assert sections[1] == "## 코너 1: 첫 코너\n본문"
    assert sections[2] == "## 코너 2: 둘째 코너\n본문 2"


def test_kb_without_corner_sections_raises() -> None:
    with pytest.raises(TrackDataError):
        _parse_sections("# KB\n\n코너 섹션이 없는 문서\n", "test.md")


def test_duplicate_corner_sections_raise() -> None:
    with pytest.raises(TrackDataError):
        _parse_sections("## 코너 1: a\n본문\n## 코너 1: b\n본문\n", "test.md")
