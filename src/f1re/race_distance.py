"""레이스 거리·연료 여유 랩의 기준(SCRUM-95).

F1 25 UDP 사양의 Car Status `m_fuelRemainingLaps`는 "Fuel remaining in terms of
laps (value on MFD)"다. 게임 MFD에 보이는 값은 **남은 레이스 거리 대비 여유
랩(+/-)** 이므로, 이 값을 그대로 `fuel_laps_margin`으로 쓴다. 여기서 남은
레이스 랩을 또 빼면 여유를 두 번 빼게 되어, 실제 주행에서 "연료가 부족하다"는
오경보가 난다(SCRUM-95).

레이스가 아닌 세션(프랙티스·퀄리파잉·타임 트라이얼 등)이나 `total_laps`가
0·없는 세션에는 레이스 거리가 없다. 그런 세션에서는 남은 레이스 랩을 만들지
않고, 연료 부족 경고·리프트 앤 코스트 연료 절약 코칭·마지막 랩 판정도 하지
않는다. 대시보드는 연료 여유 값 자체는 그대로 보여 준다.

계산은 이 모듈 한곳에만 두고, 경고(`alerts`)·파이프라인(`pipeline`)·화면
(`web`)·코칭(`coaching`)·추월(`overtake`)이 모두 같은 함수를 쓴다.
"""

from __future__ import annotations

import math
from typing import Any

from f1re.laps.traces import RACE_SESSION_TYPES


def race_distance_known(session_type: Any, total_laps: Any) -> bool:
    """레이스 세션이고 총 랩 수가 있어 남은 레이스 랩을 셀 수 있는가."""
    total = _integer(total_laps)
    return (
        _integer(session_type) in RACE_SESSION_TYPES
        and total is not None
        and total > 0
    )


def remaining_race_laps(
    session_type: Any,
    total_laps: Any,
    current_lap: Any,
) -> float | None:
    """현재 랩을 포함해 완주까지 남은 랩 수.

    레이스 세션이 아니거나 `total_laps`가 0·없거나 현재 랩이 1 미만이면
    `None`이다. 음수가 되지 않게 0에서 자른다.
    """
    total = _integer(total_laps)
    lap = _integer(current_lap)
    if not race_distance_known(session_type, total) or lap is None or lap <= 0:
        return None
    return float(max(total - lap + 1, 0))


def fuel_laps_margin(fuel_remaining_laps: Any) -> float | None:
    """연료 여유 랩(랩). 게임 MFD 값 `m_fuelRemainingLaps`를 그대로 쓴다.

    값이 없거나 유한하지 않으면 `None`. 음수(연료 부족)도 그대로 돌려준다.
    """
    return _number(fuel_remaining_laps)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


__all__ = [
    "fuel_laps_margin",
    "race_distance_known",
    "remaining_race_laps",
]
