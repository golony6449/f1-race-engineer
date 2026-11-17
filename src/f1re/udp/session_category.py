"""F1 25 세션 종류(`m_sessionType`) 판정과 녹화 범주 매핑의 단일 출처.

설계 4.1의 `data/recordings/<category>/...` 파일 레이아웃과 대시보드의 범주·
종료 사유 배지가 이 모듈의 값만 쓴다. 아직 `PacketId.SESSION`이 도착하지
않았거나 모르는 값이면 오류를 내지 않고 `unknown`/`미분류`로 떨어뜨려
녹화를 계속할 수 있게 한다.

F1 25 공식 `m_sessionType` 값은 다음과 같다(EA 공식 UDP 명세, SCRUM-131).
규격 표는 `docs/spec/session-types.md`에 있다.

- 1~4: 연습(1=P1, 2=P2, 3=P3, 4=짧은 연습)
- 5~9: 예선(5=Q1, 6=Q2, 7=Q3, 8=짧은 예선, 9=원샷 예선)
- 10~14: 스프린트 슛아웃(10=SS1, 11=SS2, 12=SS3, 13=짧은 슛아웃,
  14=원샷 슛아웃)
- 15~17: 레이스(15=레이스, 16=레이스 2, 17=레이스 3, 스프린트 레이스 포함)
- 18: 타임 트라이얼

F1 23까지의 10~12=레이스·13=타임 트라이얼 가정은 F1 25에서 틀리다. 세션
종류를 판정하는 모든 모듈은 이 모듈의 상수·함수만 쓴다.
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

# --- F1 25 공식 `m_sessionType` 묶음(EA F1 25 UDP 명세) ---------------------

PRACTICE_SESSION_TYPES: frozenset[int] = frozenset({1, 2, 3, 4})
QUALIFYING_SESSION_TYPES: frozenset[int] = frozenset({5, 6, 7, 8, 9})
SPRINT_SHOOTOUT_SESSION_TYPES: frozenset[int] = frozenset({10, 11, 12, 13, 14})
RACE_SESSION_TYPES: frozenset[int] = frozenset({15, 16, 17})
TIME_TRIAL_SESSION_TYPES: frozenset[int] = frozenset({18})

# 자주 쓰는 대표값.
SESSION_TYPE_RACE = 15
SESSION_TYPE_TIME_TRIAL = 18
# `coaching.store` 등 기존 이름 호환.
TIME_TRIAL_SESSION_TYPE = SESSION_TYPE_TIME_TRIAL

# 화면에 보여 줄 범주 이름. `unknown`은 "미분류".
_CATEGORY_LABELS: dict[str, str] = {
    "practice": "연습",
    "qualifying": "예선",
    "race": "레이스",
    "time_trial": "타임 트라이얼",
    "unknown": "미분류",
}

# 화면에 보여 줄 세션 종류 이름(공식 표 기준).
_SESSION_TYPE_LABELS: dict[int, str] = {
    1: "P1",
    2: "P2",
    3: "P3",
    4: "짧은 연습",
    5: "Q1",
    6: "Q2",
    7: "Q3",
    8: "짧은 예선",
    9: "원샷 예선",
    10: "SS1",
    11: "SS2",
    12: "SS3",
    13: "짧은 스프린트 슛아웃",
    14: "원샷 스프린트 슛아웃",
    15: "레이스",
    16: "레이스 2",
    17: "레이스 3",
    18: "타임 트라이얼",
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
_FALLBACK_SESSION_TYPE_LABEL = "알 수 없음"
_FALLBACK_END_REASON_LABEL = "알 수 없음"


def _session_type_value(session_type: int | None) -> int | None:
    """`m_sessionType` 원값을 정수로 맞춘다. 모르는 형식이면 `None`."""
    if session_type is None or isinstance(session_type, bool):
        return None
    try:
        return int(session_type)
    except (TypeError, ValueError):
        return None


def is_practice_session(session_type: int | None) -> bool:
    """연습 세션(1~4)인가."""
    return _session_type_value(session_type) in PRACTICE_SESSION_TYPES


def is_qualifying_session(session_type: int | None) -> bool:
    """예선 세션(5~9)인가.

    스프린트 슛아웃(10~14)은 별도 묶음이라 여기에 포함하지 않는다. 녹화
    범주로는 둘 다 `qualifying`이므로 범주 판정에는 `session_category()`를 쓴다.
    """
    return _session_type_value(session_type) in QUALIFYING_SESSION_TYPES


def is_sprint_shootout_session(session_type: int | None) -> bool:
    """스프린트 슛아웃(10~14)인가."""
    return _session_type_value(session_type) in SPRINT_SHOOTOUT_SESSION_TYPES


def is_race_session(session_type: int | None) -> bool:
    """레이스(15~17, 스프린트 레이스 포함)인가."""
    return _session_type_value(session_type) in RACE_SESSION_TYPES


def is_time_trial_session(session_type: int | None) -> bool:
    """타임 트라이얼(18)인가."""
    return _session_type_value(session_type) in TIME_TRIAL_SESSION_TYPES


def session_category(session_type: int | None) -> str:
    """F1 25 `m_sessionType` 원값을 녹화 범주로 바꾼다.

    공식 값은 1~4 연습, 5~9 예선, 10~14 스프린트 슛아웃, 15~17 레이스,
    18 타임 트라이얼이다. 스프린트 슛아웃은 녹화 폴더 레이아웃의 5개 범주를
    늘리지 않기 위해 예선(`qualifying`)으로 묶는다. 0(Unknown)·19 이상·
    `None`(SESSION 패킷 미수신)은 `unknown`이다.
    """
    value = _session_type_value(session_type)
    if value is None:
        return _FALLBACK_CATEGORY
    if value in PRACTICE_SESSION_TYPES:
        return "practice"
    # 스프린트 슛아웃은 예선과 같은 주말 세션 묶음으로 본다(SCRUM-131).
    if value in QUALIFYING_SESSION_TYPES or value in SPRINT_SHOOTOUT_SESSION_TYPES:
        return "qualifying"
    if value in RACE_SESSION_TYPES:
        return "race"
    if value in TIME_TRIAL_SESSION_TYPES:
        return "time_trial"
    return _FALLBACK_CATEGORY


def category_label(category: str) -> str:
    """화면에 보여 줄 범주 이름. 모르는 값은 `미분류`."""
    return _CATEGORY_LABELS.get(category, _CATEGORY_LABELS[_FALLBACK_CATEGORY])


def session_type_label(session_type: int | None) -> str:
    """화면에 보여 줄 세션 종류 이름(예: 15=레이스, 18=타임 트라이얼).

    모르는 값은 `알 수 없음`이다.
    """
    value = _session_type_value(session_type)
    if value is None:
        return _FALLBACK_SESSION_TYPE_LABEL
    return _SESSION_TYPE_LABELS.get(value, _FALLBACK_SESSION_TYPE_LABEL)


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
    "PRACTICE_SESSION_TYPES",
    "QUALIFYING_SESSION_TYPES",
    "RACE_SESSION_TYPES",
    "SESSION_TYPE_RACE",
    "SESSION_TYPE_TIME_TRIAL",
    "SPRINT_SHOOTOUT_SESSION_TYPES",
    "TIME_TRIAL_SESSION_TYPES",
    "TIME_TRIAL_SESSION_TYPE",
    "category_label",
    "end_reason_label",
    "is_practice_session",
    "is_qualifying_session",
    "is_race_session",
    "is_sprint_shootout_session",
    "is_time_trial_session",
    "session_category",
    "session_type_label",
]
