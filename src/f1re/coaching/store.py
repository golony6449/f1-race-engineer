"""조건별 기준 랩 저장·불러오기(작업 19, SCRUM-69).

설계 6.3의 "서킷별 저장"을 담당한다.

- 저장 키는 **서킷 + 조건**이다. 조건은 모드(타임 트라이얼 / 레이스 주말),
  날씨(맑음 / 비), 차(팀 또는 타임 트라이얼 동일 성능)로 나눈다. 키가 다르면
  다른 행이므로 비 기준과 맑음 기준, 예선과 레이스 기준이 섞이지 않는다.
- `load()`는 **같은 조건**의 기준만 돌려준다. 저장된 기준이 있는 서킷은
  다음 방문 세션의 첫 랩부터 그 기준·집중 코너로 코칭할 수 있다(설계 6.4).
- 같은 조건의 기준이 없을 때는 `load_compatible()`이 가장 가까운 조건의
  기준을 돌려주되 `conditions_match=False`로 표시한다. 설계 6.3대로 조건이
  다른 기준밖에 없으면(예: 레이스 초반 연료가 무거운데 예선 랩과 비교)
  **시간 차이는 말하지 않고 동작(브레이크 지점·라인·스로틀 타이밍)만 말한다**.
  `StoredReference.speakable_loss_ms()`가 그 관문이며, 조건이 다르면 `None`이다.
- 가장 빠른 차 기준도 함께 저장한다. 기준 랩 페이로드에는 내 최고 유효 랩과
  세션 최속차의 최고 랩이 들어가고, 불러온 `ReferenceSet`이 코너마다 더 빠른
  쪽을 고른다(설계 6.3). 휠 슬립처럼 내 차에만 있는 값은 최속차 랩에 없으므로
  자연히 내 최고 랩과만 비교된다.

저장 위치는 SQLite `reference_sets` 표(`db.py` 마이그레이션 004)다. 기준 랩과
집중 코너는 각각 `reference.reference_to_dict()`·`focus.focus_to_dict()`로
JSON 직렬화해 한 행에 둔다.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from f1re.coaching.focus import (
    FOCUS_PAYLOAD_VERSION,
    FocusSet,
    focus_from_dict,
    focus_to_dict,
)
from f1re.coaching.reference import (
    ReferenceSet,
    reference_from_dict,
    reference_to_dict,
)

# 모드(설계 6.3). 타임 트라이얼과 레이스 주말(연습·예선·레이스)을 나눈다.
MODE_TT = "tt"
MODE_RACE_WEEKEND = "race_weekend"
MODES = frozenset({MODE_TT, MODE_RACE_WEEKEND})

# 날씨. 게임의 날씨 코드를 맑음(0~2)과 비(3~5) 둘로 묶는다.
WEATHER_DRY = "dry"
WEATHER_WET = "wet"
WEATHERS = frozenset({WEATHER_DRY, WEATHER_WET})

# F1 25 Session 패킷의 session_type 13 = Time Trial.
TIME_TRIAL_SESSION_TYPE = 13
# Session 패킷 weather: 3=light rain, 4=heavy rain, 5=storm.
_WET_WEATHER_FROM = 3

# 타임 트라이얼은 모든 차의 성능이 같아 차 키를 하나로 쓴다(설계 6.3).
TT_CAR_KEY = "tt_equal"
UNKNOWN_TEAM_CAR_KEY = "team:unknown"


def mode_for_session_type(session_type: int | None) -> str:
    """세션 종류를 저장 모드로 나눈다. 13(타임 트라이얼)만 `tt`다."""
    if session_type is not None and int(session_type) == TIME_TRIAL_SESSION_TYPE:
        return MODE_TT
    return MODE_RACE_WEEKEND


def weather_for_code(weather_code: int | None) -> str:
    """게임 날씨 코드를 `dry`/`wet`으로 나눈다. 모르면 맑음으로 본다."""
    if weather_code is not None and int(weather_code) >= _WET_WEATHER_FROM:
        return WEATHER_WET
    return WEATHER_DRY


def car_key_for(team_id: int | None, mode: str) -> str:
    """차 키. 타임 트라이얼은 동일 성능 한 키, 레이스 주말은 팀별 키다."""
    if mode not in MODES:
        raise ValueError(f"알 수 없는 모드입니다: {mode!r}")
    if mode == MODE_TT:
        return TT_CAR_KEY
    if team_id is None:
        return UNKNOWN_TEAM_CAR_KEY
    return f"team:{int(team_id)}"


@dataclass(frozen=True)
class ReferenceKey:
    """기준 랩 저장 키: 서킷 + 조건(모드·날씨·차)."""

    track_id: int
    mode: str
    weather: str
    car_key: str

    def __post_init__(self) -> None:
        if isinstance(self.track_id, bool) or not isinstance(self.track_id, int):
            raise ValueError("track_id는 정수여야 합니다")
        if self.mode not in MODES:
            raise ValueError(f"알 수 없는 모드입니다: {self.mode!r}")
        if self.weather not in WEATHERS:
            raise ValueError(f"알 수 없는 날씨입니다: {self.weather!r}")
        if not isinstance(self.car_key, str) or not self.car_key.strip():
            raise ValueError("car_key가 비어 있습니다")

    @classmethod
    def from_session(
        cls,
        *,
        track_id: int,
        session_type: int | None,
        weather_code: int | None,
        team_id: int | None,
    ) -> ReferenceKey:
        """세션 패킷 값에서 저장 키를 만든다.

        `team_id`는 Participants 패킷의 내 차 팀 ID다. 타임 트라이얼이면 무시하고
        동일 성능 키를 쓴다.
        """
        mode = mode_for_session_type(session_type)
        return cls(
            track_id=int(track_id),
            mode=mode,
            weather=weather_for_code(weather_code),
            car_key=car_key_for(team_id, mode),
        )


@dataclass(frozen=True)
class StoredReference:
    """불러온 기준 랩과 집중 코너, 그리고 그 조건이 요청과 같은지.

    `key`는 요청한 조건, `stored_key`는 실제로 저장돼 있던 조건이다. 둘이 다르면
    (`conditions_match=False`) 시간 차이는 말하지 않고 동작만 말한다(설계 6.3).
    """

    key: ReferenceKey
    stored_key: ReferenceKey
    reference: ReferenceSet
    focus: tuple[int, ...]
    updated_at_ns: int = 0

    @property
    def conditions_match(self) -> bool:
        return self.key == self.stored_key

    @property
    def time_comparable(self) -> bool:
        """시간 손실 수치를 말해도 되는 기준인가."""
        return self.conditions_match

    def speakable_loss_ms(self, loss_ms: int) -> int | None:
        """말해도 되는 시간 손실(ms). 조건이 다른 기준이면 `None`.

        조건이 다르면 무전은 "3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아."처럼 동작과 거리만
        말하고, 시간 손실("0.4초")은 입에 올리지 않는다.
        """
        return int(loss_ms) if self.time_comparable else None


class ReferenceStore:
    """서킷·조건별 기준 랩을 SQLite에 저장하고 불러온다.

    `conn`은 `f1re.db.open_db()`가 연 연결이다(마이그레이션 004가 적용돼 있다).
    `clock`은 테스트에서 저장 시각을 고정할 때 바꾼다.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._conn = conn
        self._clock = clock

    # --- 쓰기 -------------------------------------------------------------

    def save(
        self,
        key: ReferenceKey,
        reference: ReferenceSet,
        focus: FocusSet | Iterable[int] | None = None,
    ) -> None:
        """같은 키의 기준 랩과 집중 코너를 저장한다(있으면 덮어쓴다).

        `focus`는 `FocusSet`이면 집중 코너와 코너별 손실을 함께 저장하고,
        번호 목록이면 손실 없이 목록만 저장한다.
        """
        reference_payload = json.dumps(
            reference_to_dict(reference), ensure_ascii=False, separators=(",", ":")
        )
        focus_payload = json.dumps(
            _focus_dict(focus), ensure_ascii=False, separators=(",", ":")
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO reference_sets (
                    track_id, mode, weather, car_key,
                    reference, focus, updated_at_ns
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_id, mode, weather, car_key) DO UPDATE SET
                    reference = excluded.reference,
                    focus = excluded.focus,
                    updated_at_ns = excluded.updated_at_ns
                """,
                (
                    key.track_id,
                    key.mode,
                    key.weather,
                    key.car_key,
                    reference_payload,
                    focus_payload,
                    int(self._clock()),
                ),
            )

    # --- 읽기 -------------------------------------------------------------

    def load(self, key: ReferenceKey) -> ReferenceSet | None:
        """같은 조건의 기준 랩. 저장된 것이 없거나 내용이 손상됐으면 `None`."""
        row = self._row(key)
        if row is None:
            return None
        return _decode_reference(row[0])

    def load_focus(self, key: ReferenceKey) -> tuple[int, ...]:
        """같은 조건의 집중 코너 번호(손실이 큰 순서). 없으면 빈 튜플."""
        row = self._row(key)
        if row is None:
            return ()
        return _decode_focus(row[1])

    def load_compatible(self, key: ReferenceKey) -> StoredReference | None:
        """같은 조건의 기준을 먼저, 없으면 가장 가까운 조건의 기준을 돌려준다.

        돌려준 `StoredReference.conditions_match`가 False면 조건이 다른 기준이므로
        시간 수치 없이 동작만 말해야 한다. 가까운 조건의 순서는 같은 조건 항목
        수가 많은 순서이며, 같으면 차·날씨 > 차·모드 > 모드·날씨 순으로 고른다.
        그다음이 최근 저장분이다.
        """
        rows = self._conn.execute(
            """
            SELECT track_id, mode, weather, car_key,
                   reference, focus, updated_at_ns
            FROM reference_sets
            WHERE track_id = ?
            """,
            (key.track_id,),
        ).fetchall()
        if not rows:
            return None

        candidates: list[
            tuple[tuple[int, int, int], ReferenceKey, tuple[Any, ...]]
        ] = []
        for row in rows:
            try:
                other = ReferenceKey(
                    track_id=int(row[0]),
                    mode=str(row[1]),
                    weather=str(row[2]),
                    car_key=str(row[3]),
                )
            except ValueError:
                # 손상된 행은 후보에서 뺀다.
                continue
            score = (
                _match_count(key, other),
                -_condition_preference(key, other),
                int(row[6]),
            )
            candidates.append((score, other, row))
        candidates.sort(key=lambda item: item[0], reverse=True)

        for _score, other, row in candidates:
            reference = _decode_reference(row[4])
            if reference is None:
                continue
            return StoredReference(
                key=key,
                stored_key=other,
                reference=reference,
                focus=_decode_focus(row[5]),
                updated_at_ns=int(row[6]),
            )
        return None

    # --- 내부 -------------------------------------------------------------

    def _row(self, key: ReferenceKey) -> tuple[Any, ...] | None:
        return self._conn.execute(
            """
            SELECT reference, focus, updated_at_ns
            FROM reference_sets
            WHERE track_id = ? AND mode = ? AND weather = ? AND car_key = ?
            """,
            (key.track_id, key.mode, key.weather, key.car_key),
        ).fetchone()


# ---------------------------------------------------------------------------
# 조건 거리 계산


def _match_count(requested: ReferenceKey, stored: ReferenceKey) -> int:
    """요청과 저장 조건이 같은 항목 수(0~3)."""
    return (
        int(requested.mode == stored.mode)
        + int(requested.weather == stored.weather)
        + int(requested.car_key == stored.car_key)
    )


def _condition_preference(requested: ReferenceKey, stored: ReferenceKey) -> int:
    """같은 항목 수가 같을 때의 우선순위. 작을수록 먼저 쓴다.

    예선 랩을 레이스에서 쓰는 것(차·날씨 같음)이 가장 유용하고, 그다음이 같은
    차·모드에서 날씨만 다른 기준(맑음 ↔ 비)이다. 다른 팀 차 기준은 가장 뒤다.
    """
    same_car = requested.car_key == stored.car_key
    same_weather = requested.weather == stored.weather
    same_mode = requested.mode == stored.mode
    if same_car and same_weather:
        return 0  # 모드만 다름
    if same_car and same_mode:
        return 1  # 날씨만 다름
    if same_weather and same_mode:
        return 2  # 차만 다름
    if same_car:
        return 3
    if same_weather:
        return 4
    if same_mode:
        return 5
    return 6


# ---------------------------------------------------------------------------
# JSON 변환


def _focus_dict(focus: FocusSet | Iterable[int] | None) -> dict[str, Any]:
    if isinstance(focus, FocusSet):
        return focus_to_dict(focus)
    corners = [] if focus is None else [int(number) for number in focus]
    return {
        "version": FOCUS_PAYLOAD_VERSION,
        "corners": corners,
        "losses": {},
    }


def _decode_reference(payload: Any) -> ReferenceSet | None:
    """JSON 문자열을 `ReferenceSet`으로. 손상됐으면 `None`(서버는 계속 돈다)."""
    if not isinstance(payload, str):
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return reference_from_dict(data)
    except (TypeError, ValueError):
        return None


def _decode_focus(payload: Any) -> tuple[int, ...]:
    if not isinstance(payload, str):
        return ()
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return ()
    if not isinstance(data, dict):
        return ()
    try:
        return focus_from_dict(data)
    except (TypeError, ValueError):
        return ()


__all__ = [
    "MODE_RACE_WEEKEND",
    "MODE_TT",
    "MODES",
    "TIME_TRIAL_SESSION_TYPE",
    "TT_CAR_KEY",
    "UNKNOWN_TEAM_CAR_KEY",
    "WEATHERS",
    "WEATHER_DRY",
    "WEATHER_WET",
    "ReferenceKey",
    "ReferenceStore",
    "StoredReference",
    "car_key_for",
    "mode_for_session_type",
    "weather_for_code",
]
