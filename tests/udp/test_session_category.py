"""세션 범주 매핑과 종료 사유 상수(SCRUM-97)."""

from __future__ import annotations

import pytest

from f1re.udp.session_category import (
    CATEGORIES,
    END_REASONS,
    PRACTICE_SESSION_TYPES,
    QUALIFYING_SESSION_TYPES,
    RACE_SESSION_TYPES,
    SESSION_TYPE_RACE,
    SESSION_TYPE_TIME_TRIAL,
    SPRINT_SHOOTOUT_SESSION_TYPES,
    TIME_TRIAL_SESSION_TYPES,
    category_label,
    end_reason_label,
    is_practice_session,
    is_qualifying_session,
    is_race_session,
    is_sprint_shootout_session,
    is_time_trial_session,
    session_category,
    session_type_label,
)

# F1 25 공식 `m_sessionType` 원값 → 녹화 범주. 0(Unknown)·19 이상·미수신
# (`None`)은 모두 `unknown`이다. F1 23의 10~12=레이스·13=타임 트라이얼 가정과
# 다르다(SCRUM-131).
SESSION_TYPE_CASES: tuple[tuple[int | None, str], ...] = (
    (0, "unknown"),
    (1, "practice"),
    (2, "practice"),
    (3, "practice"),
    (4, "practice"),
    (5, "qualifying"),
    (6, "qualifying"),
    (7, "qualifying"),
    (8, "qualifying"),
    (9, "qualifying"),
    (10, "qualifying"),
    (11, "qualifying"),
    (12, "qualifying"),
    (13, "qualifying"),
    (14, "qualifying"),
    (15, "race"),
    (16, "race"),
    (17, "race"),
    (18, "time_trial"),
    (19, "unknown"),
    (99, "unknown"),
    (-1, "unknown"),
    (None, "unknown"),
)

SESSION_TYPE_LABEL_CASES: tuple[tuple[int | None, str], ...] = (
    (1, "P1"),
    (4, "짧은 연습"),
    (5, "Q1"),
    (9, "원샷 예선"),
    (10, "SS1"),
    (13, "짧은 스프린트 슛아웃"),
    (15, "레이스"),
    (16, "레이스 2"),
    (17, "레이스 3"),
    (18, "타임 트라이얼"),
    (0, "알 수 없음"),
    (None, "알 수 없음"),
)

CATEGORY_LABEL_CASES: tuple[tuple[str, str], ...] = (
    ("practice", "연습"),
    ("qualifying", "예선"),
    ("race", "레이스"),
    ("time_trial", "타임 트라이얼"),
    ("unknown", "미분류"),
)

END_REASON_LABEL_CASES: tuple[tuple[str, str], ...] = (
    ("recording", "녹화 중"),
    ("session_end", "정상 종료"),
    ("session_switch", "세션 전환"),
    ("inactivity_timeout", "10분 만료"),
    ("engine_off", "엔진 OFF"),
    ("recovered", "비정상 복구"),
)


@pytest.mark.parametrize(("session_type", "expected"), SESSION_TYPE_CASES)
def test_session_category_maps_f1_25_session_type(
    session_type: int | None, expected: str
) -> None:
    """F1 25 공식 session_type 값이 설계 4.1의 범주로 매핑된다."""
    assert session_category(session_type) == expected


def test_categories_are_the_recording_directories() -> None:
    """범주 값은 모두 녹화 파일이 놓이는 디렉터리 이름이다."""
    assert CATEGORIES == ("practice", "qualifying", "race", "time_trial", "unknown")
    for _, category in SESSION_TYPE_CASES:
        assert category in CATEGORIES


def test_f1_25_session_type_sets_are_the_single_source() -> None:
    """공식 F1 25 묶음 상수와 대표값이 규격 표와 같다."""
    assert PRACTICE_SESSION_TYPES == frozenset({1, 2, 3, 4})
    assert QUALIFYING_SESSION_TYPES == frozenset({5, 6, 7, 8, 9})
    assert SPRINT_SHOOTOUT_SESSION_TYPES == frozenset({10, 11, 12, 13, 14})
    assert RACE_SESSION_TYPES == frozenset({15, 16, 17})
    assert TIME_TRIAL_SESSION_TYPES == frozenset({18})
    assert SESSION_TYPE_RACE == 15
    assert SESSION_TYPE_TIME_TRIAL == 18


@pytest.mark.parametrize(
    ("session_type", "practice"),
    [(1, True), (4, True), (5, False), (15, False), (18, False), (None, False)],
)
def test_is_practice_session(session_type: int | None, practice: bool) -> None:
    assert is_practice_session(session_type) is practice


@pytest.mark.parametrize(
    ("session_type", "qualifying"),
    [
        (5, True),
        (9, True),
        # 스프린트 슛아웃은 별도 묶음이라 예선 판정에는 포함하지 않는다.
        (10, False),
        (14, False),
        (15, False),
        (None, False),
    ],
)
def test_is_qualifying_session(session_type: int | None, qualifying: bool) -> None:
    assert is_qualifying_session(session_type) is qualifying


@pytest.mark.parametrize(
    ("session_type", "shootout"),
    [(10, True), (14, True), (9, False), (15, False), (None, False)],
)
def test_is_sprint_shootout_session(
    session_type: int | None, shootout: bool
) -> None:
    assert is_sprint_shootout_session(session_type) is shootout


@pytest.mark.parametrize(
    ("session_type", "race"),
    [
        (15, True),
        (16, True),
        (17, True),
        # F1 23의 10~12는 이제 스프린트 슛아웃이다.
        (10, False),
        (12, False),
        (13, False),
        (14, False),
        (18, False),
        (None, False),
    ],
)
def test_is_race_session(session_type: int | None, race: bool) -> None:
    assert is_race_session(session_type) is race


@pytest.mark.parametrize(
    ("session_type", "time_trial"),
    [(18, True), (13, False), (17, False), (None, False)],
)
def test_is_time_trial_session(
    session_type: int | None, time_trial: bool
) -> None:
    assert is_time_trial_session(session_type) is time_trial


def test_session_type_accepts_float_from_state_snapshot() -> None:
    """상태 스냅샷은 JSON 숫자를 float로 줄 수 있다. 같은 값으로 판정한다."""
    assert session_category(15.0) == "race"
    assert is_race_session(15.0) is True
    assert is_time_trial_session(18.0) is True


@pytest.mark.parametrize(("session_type", "expected"), SESSION_TYPE_LABEL_CASES)
def test_session_type_label_returns_official_name(
    session_type: int | None, expected: str
) -> None:
    assert session_type_label(session_type) == expected


@pytest.mark.parametrize(("category", "expected"), CATEGORY_LABEL_CASES)
def test_category_label_returns_korean_name(category: str, expected: str) -> None:
    assert category_label(category) == expected


def test_category_label_falls_back_to_unknown_label() -> None:
    """DB나 설정에 모르는 범주가 있어도 화면은 미분류로 보여 준다."""
    assert category_label("endurance") == "미분류"


def test_end_reasons_are_the_db_end_reason_values() -> None:
    """설계 4.4의 `session_recordings.end_reason` 값 전체를 담는다."""
    assert END_REASONS == frozenset(
        reason for reason, _ in END_REASON_LABEL_CASES
    )
    assert isinstance(END_REASONS, frozenset)
    assert "recording" in END_REASONS


@pytest.mark.parametrize(("end_reason", "expected"), END_REASON_LABEL_CASES)
def test_end_reason_label_returns_korean_name(
    end_reason: str, expected: str
) -> None:
    assert end_reason_label(end_reason) == expected


def test_end_reason_label_falls_back_for_none_and_unknown() -> None:
    """아직 마감 정보가 없거나(`None`) 모르는 값이면 알 수 없음으로 보여 준다."""
    assert end_reason_label(None) == "알 수 없음"
    assert end_reason_label("mystery") == "알 수 없음"
