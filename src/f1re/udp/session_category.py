"""F1 25 세션 종류를 녹화 범주로 매핑하고 종료 사유 이름을 준다(SCRUM-97).

설계 4.1의 `data/recordings/<category>/...` 파일 레이아웃과 대시보드의 범주·
종료 사유 배지가 이 모듈의 값만 쓴다. 아직 `PacketId.SESSION`이 도착하지
않았거나 모르는 값이면 오류를 내지 않고 `unknown`/`미분류`로 떨어뜨려
녹화를 계속할 수 있게 한다.
"""

from __future__ import annotations

# 녹화 파일이 놓이는 범주 디렉터리(설계 4.1). 순서는 화면 필터 순서와 같다.
CATEGORIES: tuple[str, ...] = (
    "practice",
    "qualifying",
    "race",
    "time_trial",
    "unknown",
)

# `session_recordings.end_reason`에 들어가는 값(설계 4.4).
END_REASONS: frozenset[str] = frozenset(
    {
        "recording",
        "session_end",
        "session_switch",
        "inactivity_timeout",
        "engine_off",
        "recovered",
    }
)

# 화면에 보여 줄 범주 이름. `unknown`은 "미분류".
_CATEGORY_LABELS: dict[str, str] = {
    "practice": "연습",
    "qualifying": "예선",
    "race": "레이스",
    "time_trial": "타임 트라이얼",
    "unknown": "미분류",
}

# 화면에 보여 줄 종료 사유 이름.
_END_REASON_LABELS: dict[str, str] = {
    "recording": "녹화 중",
    "session_end": "정상 종료",
    "session_switch": "세션 전환",
    "inactivity_timeout": "10분 만료",
    "engine_off": "엔진 OFF",
    "recovered": "비정상 복구",
}

_FALLBACK_CATEGORY = "unknown"
_FALLBACK_END_REASON_LABEL = "알 수 없음"


def session_category(session_type: int | None) -> str:
    """F1 25 `m_sessionType` 원값을 녹화 범주로 바꾼다.

    공식 값은 1~4 연습, 5~9 예선, 10~12 레이스, 13 타임 트라이얼이다.
    0(Unknown)·14 이상·`None`(SESSION 패킷 미수신)은 `unknown`이다.
    """
    if session_type is None:
        return _FALLBACK_CATEGORY
    if 1 <= session_type <= 4:
        return "practice"
    if 5 <= session_type <= 9:
        return "qualifying"
    if 10 <= session_type <= 12:
        return "race"
    if session_type == 13:
        return "time_trial"
    return _FALLBACK_CATEGORY


def category_label(category: str) -> str:
    """화면에 보여 줄 범주 이름. 모르는 값은 `미분류`."""
    return _CATEGORY_LABELS.get(category, _CATEGORY_LABELS[_FALLBACK_CATEGORY])


def end_reason_label(end_reason: str | None) -> str:
    """화면에 보여 줄 종료 사유 이름.

    아직 마감되지 않았거나(`None`) 모르는 값이면 `알 수 없음`이다.
    """
    if end_reason is None:
        return _FALLBACK_END_REASON_LABEL
    return _END_REASON_LABELS.get(end_reason, _FALLBACK_END_REASON_LABEL)


__all__ = [
    "CATEGORIES",
    "END_REASONS",
    "category_label",
    "end_reason_label",
    "session_category",
]
