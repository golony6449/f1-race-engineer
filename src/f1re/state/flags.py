"""깃발 요약(SCRUM-80).

화면·API가 그대로 쓸 수 있는 깃발 상태를 패킷 본문에서 만든다. 화면은 서버
API를 읽기만 하므로(설계 6.9), 어떤 패킷 값이 어떤 깃발인지 판정은 여기서만
한다.

- 옐로: Session 패킷 `marshal_zones`의 `zone_flag`가 3인 구역. 구역의 끝은
  다음 구역의 시작(마지막 구역은 랩 끝 1.0)이다. `num_marshal_zones`만큼만
  읽는다.
- 블루: 내 차(Header `player_car_index`) Car Status `vehicle_fia_flags`가 2.
- SC·VSC: Session 패킷 `safety_car_status`가 1·2(3은 포메이션 랩).
- 레드: Event `RDFL`을 받은 뒤 `StateStore`가 유지하는 래치 값.
  `safety_car_status`는 3(포메이션 랩)일 때 켜지지 않는다.
"""

from __future__ import annotations

import math
from typing import Any

# 옐로 구역을 표시하는 Session 패킷의 zone_flag 값.
ZONE_FLAG_YELLOW = 3

# 블루 깃발을 표시하는 Car Status 패킷의 vehicle_fia_flags 값.
FIA_FLAG_BLUE = 2


def flag_summary(
    packets: dict[str, Any],
    player_car_index: Any,
    *,
    red: bool = False,
) -> dict[str, Any]:
    """패킷 본문에서 화면용 깃발 요약을 만든다.

    반환 모양은 `/api/state`·`/ws/state`의 `flags`와 같다.

    ``{"red": bool, "blue": bool, "yellow": bool,
    "yellow_zones": [{"start": float, "end": float}], "sc": bool, "vsc": bool}``

    `start`·`end`는 랩 거리 비율(0~1)이다. 옐로 구역이 없으면 빈 목록이다.
    """
    session = packets.get("session")
    if not isinstance(session, dict):
        session = {}

    safety_car_status = _as_int(session.get("safety_car_status"))
    yellow_zones = _yellow_zones(session)
    blue = (
        _player_flag(packets, player_car_index, "car_status", "car_status_data")
        == FIA_FLAG_BLUE
    )
    return {
        "red": bool(red),
        "blue": blue,
        "yellow": bool(yellow_zones),
        "yellow_zones": yellow_zones,
        "sc": safety_car_status == 1,
        "vsc": safety_car_status == 2,
    }


def _yellow_zones(session: dict[str, Any]) -> list[dict[str, float]]:
    """Session 패킷의 마셜 구역에서 옐로 구간만 뽑는다."""
    count = _as_int(session.get("num_marshal_zones"))
    zones = session.get("marshal_zones")
    if count is None or count <= 0 or not isinstance(zones, list):
        return []

    starts: list[tuple[float, int]] = []
    for entry in zones[: min(count, len(zones))]:
        if not isinstance(entry, dict):
            continue
        start = _as_finite(entry.get("zone_start"))
        flag = _as_int(entry.get("zone_flag"))
        if start is None or flag is None or not 0.0 <= start <= 1.0:
            continue
        starts.append((start, flag))
    # 게임은 랩 순서로 보내지만, 순서가 어긋나도 구간이 뒤집히지 않게 정렬한다.
    starts.sort(key=lambda item: item[0])

    yellow: list[dict[str, float]] = []
    for index, (start, flag) in enumerate(starts):
        if flag != ZONE_FLAG_YELLOW:
            continue
        end = starts[index + 1][0] if index + 1 < len(starts) else 1.0
        if end > start:
            yellow.append({"start": start, "end": end})
    return yellow


def _player_flag(
    packets: dict[str, Any],
    player_car_index: Any,
    packet_name: str,
    list_key: str,
) -> int | None:
    """패킷 본문에서 내 차 항목의 `vehicle_fia_flags`를 꺼낸다."""
    if not isinstance(player_car_index, int) or not 0 <= player_car_index < 22:
        return None
    body = packets.get(packet_name)
    if not isinstance(body, dict):
        return None
    entries = body.get(list_key)
    if not isinstance(entries, list) or player_car_index >= len(entries):
        return None
    entry = entries[player_car_index]
    if not isinstance(entry, dict):
        return None
    return _as_int(entry.get("vehicle_fia_flags"))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def _as_finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
