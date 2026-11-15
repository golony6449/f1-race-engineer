"""칭찬 무전(SCRUM-125): 잘한 점을 한 랩에 한 번만 짧게 알린다.

실전 레이스 후기를 반영해 "지적"뿐 아니라 "잘한 점"도 무전으로 말한다. 무전
종류는 기존 개선 알림(`RadioKind.IMPROVEMENT`)을 그대로 쓰므로 우선순위
(코칭 다음)와 대시보드 끄기 규칙이 이미 정해져 있다.

규칙
----
- **한 랩 최대 1번.** 랩이 끝날 때 한 번만 판단하고, 이미 칭찬한 랩은 다시
  칭찬하지 않는다.
- **우선순위**: 개인 최고 랩 > 섹터 최고 > 순위 상승 > 클린 랩. 조건이 여러 개
  맞으면 앞의 것 하나만 말한다.
- **퀄리파잉·스프린트 슛아웃**(`is_qualifying_session` /
  `is_sprint_shootout_session`)에서는 `*_short` 틀의 짧은 문장만 말한다.
- **무효 랩·피트 인/아웃 랩에서는 칭찬하지 않는다.** `LapTrace.valid=False`는
  아웃랩·인랩·피트·트랙 이탈·플래시백·SC/VSC·레이스 1랩을 모두 포함한다.
  무효 랩은 개인 최고·섹터 최고 기준도 갱신하지 않는다.
- **클린 랩은 직전 랩이 무효였을 때(복귀)만** 칭찬한다. 모든 정상 랩마다
  "깔끔한 랩"을 말하면 매 랩 소음이 되기 때문이다(설계 2장 "도움 안 되는 말"
  금지). 피트 후 첫 정상 랩이나 트랙 이탈 후 복귀 랩이 여기에 해당한다.
- 섹터 경계는 Session 패킷의 `sector2_lap_distance_start`·`sector3_...`를 쓰고,
  없으면 랩 길이를 3등분한다. 섹터 기록은 랩 거리 기준으로 시간을 보간해
  계산한다. "섹터 최고"는 개인 최고만 본다(설계가 "개인 최고 섹터 또는 세션
  최고"를 허용하므로 개인 최고 쪽을 구현).

호출 순서(한 랩)
----------------
1. 파이프라인이 Session 패킷에서 `configure(...)`로 랩 길이·섹터 경계를 준다.
2. 완료 랩마다 `on_lap_completed(trace, context, now_ns)`를 부른다. 내 차 랩이
   아니면 아무것도 하지 않는다. 무효 랩도 넘겨야 클린 랩 판정(직전 랩 무효)이
   맞는다.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from f1re.laps.traces import LapTrace, Sample
from f1re.radio.queue import RadioKind, RadioMessage, RadioQueue
from f1re.radio.templates import VariantRotator, is_last_laps, mood_for
from f1re.race_distance import remaining_race_laps
from f1re.udp.session_category import (
    is_qualifying_session,
    is_sprint_shootout_session,
)

# 칭찬 무전 유효 시간(초). 기존 개선 알림과 같은 값이다.
PRAISE_TTL_S = 10.0

# 칭찬 종류. 우선순위가 앞선 것부터다.
PRAISE_PERSONAL_BEST = "personal_best"
PRAISE_SECTOR_BEST = "sector_best"
PRAISE_POSITION_GAINED = "position_gained"
PRAISE_CLEAN_LAP = "clean_lap"

# 칭찬 종류 → (일반 틀, 퀄리파잉 짧은 틀).
_PRAISE_TEMPLATES: dict[str, tuple[str, str]] = {
    PRAISE_PERSONAL_BEST: (
        "praise.personal_best",
        "praise.personal_best_short",
    ),
    PRAISE_SECTOR_BEST: (
        "praise.sector_best",
        "praise.sector_best_short",
    ),
    PRAISE_POSITION_GAINED: (
        "praise.position_gained",
        "praise.position_gained_short",
    ),
    PRAISE_CLEAN_LAP: (
        "praise.clean_lap",
        "praise.clean_lap_short",
    ),
}


@dataclass(frozen=True)
class PraiseContext:
    """랩 하나를 칭찬할 때 필요한 세션 정보.

    `position`은 랩이 끝난 시점의 내 순위(1부터)다. 없으면 순위 상승 칭찬을
    하지 않는다. `total_laps`는 마지막 3랩 격앙 표현 판정에 쓴다.
    """

    session_type: int | None = None
    total_laps: int | None = None
    position: int | None = None


@dataclass(frozen=True)
class _Candidate:
    """이번 랩에 칭찬할 수 있는 후보 하나."""

    kind: str
    slots: dict[str, Any]
    evidence: dict[str, Any]


class PraiseTracker:
    """완료 랩을 받아 칭찬 무전을 만들고 `RadioQueue`에 넣는다.

    한 세션 동안 하나를 만들어 계속 쓴다. 세션이 바뀌면 새로 만들거나
    `reset()`을 부른다. `queue`를 주면 만든 무전을 바로 넣는다(끈 종류는 큐가
    거른다).
    """

    def __init__(
        self,
        *,
        queue: RadioQueue | None = None,
        player_car_index: int = 0,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._queue = queue
        self._player_car_index = player_car_index
        self._clock = clock
        self._variants = VariantRotator()
        self._length_m = 0.0
        self._sector2_m: float | None = None
        self._sector3_m: float | None = None
        self._reset_memory()

    # --- 설정 -------------------------------------------------------------

    def configure(
        self,
        *,
        track_length_m: float,
        sector2_m: float | None = None,
        sector3_m: float | None = None,
    ) -> None:
        """랩 길이와 섹터 경계를 정한다(Session 패킷).

        경계가 없거나 순서가 이상하면 랩 길이를 3등분한 값을 쓴다. 랩 길이도
        없으면(0 이하) 섹터 칭찬을 하지 않는다.
        """
        length = _number(track_length_m) or 0.0
        self._length_m = max(0.0, float(length))
        second = _number(sector2_m)
        third = _number(sector3_m)
        if (
            second is not None
            and third is not None
            and 0 < second < third
            and (self._length_m <= 0 or third < self._length_m)
        ):
            self._sector2_m = float(second)
            self._sector3_m = float(third)
        elif self._length_m > 0:
            self._sector2_m = self._length_m / 3.0
            self._sector3_m = self._length_m * 2.0 / 3.0
        else:
            self._sector2_m = None
            self._sector3_m = None

    def reset(self) -> None:
        """세션 시작 등에서 배운 기록을 모두 지운다."""
        self._reset_memory()
        self._variants.reset()

    def _reset_memory(self) -> None:
        self._best_lap_ms: float | None = None
        self._best_sectors: dict[int, float] = {}
        self._last_position: int | None = None
        # None은 "아직 판단할 이전 랩이 없다"는 뜻이다. 첫 랩을 클린 랩으로
        # 칭찬하지 않게 False와 구분한다.
        self._previous_lap_valid: bool | None = None
        self._last_praised_lap: int | None = None

    # --- 랩 완료 ----------------------------------------------------------

    def on_lap_completed(
        self,
        trace: LapTrace,
        context: PraiseContext | None = None,
        now_ns: int | None = None,
    ) -> list[RadioMessage]:
        """완료 랩 하나를 반영하고, 칭찬할 것이 있으면 무전 하나를 돌려준다."""
        if trace.car_index != self._player_car_index:
            return []
        now = int(now_ns) if now_ns is not None else int(self._clock())
        ctx = context or PraiseContext()
        lap = trace.lap

        if not trace.valid or len(trace.samples) < 2:
            # 무효·피트·아웃랩 등: 칭찬하지 않고 기준도 갱신하지 않는다.
            # 순위 기준도 지워, 피트 후 첫 랩에서 엉뚱한 상승을 칭찬하지 않는다.
            self._previous_lap_valid = False
            self._last_position = None
            return []

        lap_ms = _lap_time_ms(trace)
        if lap_ms <= 0:
            return []

        candidates: list[_Candidate] = []

        # 1) 개인 최고 랩. 첫 유효 랩은 기준만 세우고 칭찬하지 않는다.
        previous_best = self._best_lap_ms
        if previous_best is not None and lap_ms < previous_best:
            candidates.append(
                _Candidate(
                    PRAISE_PERSONAL_BEST,
                    {"lap_s": _seconds_text(lap_ms)},
                    {"lap_time_ms": int(round(lap_ms))},
                )
            )
        if previous_best is None or lap_ms < previous_best:
            self._best_lap_ms = lap_ms

        # 2) 섹터 최고. 여러 섹터가 좋아지면 개선 폭이 가장 큰 섹터 하나를 쓴다.
        sector = self._sector_candidate(trace, lap_ms)
        if sector is not None:
            candidates.append(sector)

        # 3) 순위 상승. 직전 유효 랩에서보다 순위가 높아졌는가.
        position = _position(ctx.position)
        previous_position = self._last_position
        if (
            position is not None
            and previous_position is not None
            and position < previous_position
        ):
            candidates.append(
                _Candidate(
                    PRAISE_POSITION_GAINED,
                    {"position": position},
                    {
                        "position": position,
                        "previous_position": previous_position,
                    },
                )
            )
        self._last_position = position

        # 4) 클린 랩: 직전 랩이 무효였을 때(복귀)만.
        if self._previous_lap_valid is False:
            candidates.append(_Candidate(PRAISE_CLEAN_LAP, {}, {}))
        self._previous_lap_valid = True

        if not candidates or self._last_praised_lap == lap:
            return []

        candidate = candidates[0]
        message = self._build_message(candidate, ctx, lap, now)
        if message is None:
            return []
        if self._queue is not None and not self._queue.push(message):
            return []
        self._last_praised_lap = lap
        return [message]

    # --- 후보 계산 --------------------------------------------------------

    def _sector_candidate(
        self, trace: LapTrace, lap_ms: float
    ) -> _Candidate | None:
        if self._sector2_m is None or self._sector3_m is None:
            return None
        times = _sector_times(trace, self._sector2_m, self._sector3_m, lap_ms)
        if times is None:
            return None
        best: tuple[int, float, float] | None = None
        for number, sector_ms in times.items():
            previous = self._best_sectors.get(number)
            if previous is None:
                self._best_sectors[number] = sector_ms
                continue
            if sector_ms >= previous:
                continue
            self._best_sectors[number] = sector_ms
            improvement = previous - sector_ms
            if best is None or improvement > best[1]:
                best = (number, improvement, sector_ms)
        if best is None:
            return None
        number, improvement, sector_ms = best
        return _Candidate(
            PRAISE_SECTOR_BEST,
            {"sector": number, "sector_s": _seconds_text(sector_ms)},
            {
                "sector": number,
                "sector_time_ms": int(round(sector_ms)),
                "improvement_ms": int(round(improvement)),
            },
        )

    # --- 무전 만들기 -------------------------------------------------------

    def _build_message(
        self,
        candidate: _Candidate,
        context: PraiseContext,
        lap: int,
        now_ns: int,
    ) -> RadioMessage | None:
        qualifying = is_qualifying_session(context.session_type) or (
            is_sprint_shootout_session(context.session_type)
        )
        long_key, short_key = _PRAISE_TEMPLATES[candidate.kind]
        key = short_key if qualifying else long_key
        remaining = remaining_race_laps(context.session_type, context.total_laps, lap)
        mood = mood_for(last_laps=is_last_laps(remaining), praise=True)
        text = self._variants.render(key, mood=mood, **candidate.slots)
        evidence: dict[str, Any] = {
            "praise": candidate.kind,
            "mood": mood.value,
            "qualifying": qualifying,
            "session_type": context.session_type,
            **candidate.evidence,
        }
        return RadioMessage.create(
            RadioKind.IMPROVEMENT.value,
            text,
            now_ns=now_ns,
            ttl_ns=int(PRAISE_TTL_S * 10**9),
            evidence=evidence,
            lap=lap,
        )


# ---------------------------------------------------------------------------
# 값 도우미


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _position(value: Any) -> int | None:
    """차 순위(1~22). 0·음수·모르는 값은 None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 22 else None


def _lap_time_ms(trace: LapTrace) -> float:
    """랩 전체 시간(ms). 랩 시작 기준 마지막 샘플 시각."""
    return float(max(sample.t_ms for sample in trace.samples))


def _seconds_text(milliseconds: float) -> str:
    """무전에 넣는 초 표기("92.4"). 소수 한 자리."""
    return f"{milliseconds / 1000.0:.1f}"


def _sector_times(
    trace: LapTrace,
    sector2_m: float,
    sector3_m: float,
    lap_ms: float,
) -> dict[int, float] | None:
    """랩 거리로 보간한 섹터 시간(ms). 계산할 수 없으면 None."""
    samples = sorted(
        (
            sample
            for sample in trace.samples
            if math.isfinite(sample.dist_m) and math.isfinite(float(sample.t_ms))
        ),
        key=lambda sample: sample.dist_m,
    )
    if len(samples) < 2:
        return None
    # 같은 거리의 샘플이 있으면 마지막 것을 쓴다(랩 경계 중복).
    unique: list[Sample] = []
    for sample in samples:
        if unique and sample.dist_m <= unique[-1].dist_m:
            unique[-1] = sample
        else:
            unique.append(sample)
    t2 = _time_at(unique, sector2_m)
    t3 = _time_at(unique, sector3_m)
    if t2 is None or t3 is None:
        return None
    times = {
        1: t2,
        2: t3 - t2,
        3: lap_ms - t3,
    }
    if any(value <= 0 for value in times.values()):
        return None
    return times


def _time_at(samples: Sequence[Sample], distance: float) -> float | None:
    """랩 거리 `distance`에서의 랩 시각(ms). 구간을 선형 보간한다."""
    first = samples[0]
    if distance <= first.dist_m:
        return float(first.t_ms)
    previous = first
    for sample in samples[1:]:
        if sample.dist_m >= distance:
            span = sample.dist_m - previous.dist_m
            if span <= 0:
                return float(sample.t_ms)
            ratio = (distance - previous.dist_m) / span
            return float(previous.t_ms) + (
                float(sample.t_ms) - float(previous.t_ms)
            ) * ratio
        previous = sample
    return None


__all__ = [
    "PRAISE_CLEAN_LAP",
    "PRAISE_PERSONAL_BEST",
    "PRAISE_POSITION_GAINED",
    "PRAISE_SECTOR_BEST",
    "PRAISE_TTL_S",
    "PraiseContext",
    "PraiseTracker",
]
