"""세션 범주 매핑과 종료 사유 상수(SCRUM-97)."""

from __future__ import annotations

import pytest

from f1re.udp.session_category import (
    CATEGORIES,
    END_REASONS,
    category_label,
    end_reason_label,
    session_category,
)

# 설계 4.1의 `m_sessionType` 원값 → 녹화 범주. 0(Unknown)과 14 이상, 미수신
# (`None`)은 모두 `unknown`이다.
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
    (10, "race"),
    (11, "race"),
    (12, "race"),
    (13, "time_trial"),
    (14, "unknown"),
    (99, "unknown"),
    (-1, "unknown"),
    (None, "unknown"),
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
