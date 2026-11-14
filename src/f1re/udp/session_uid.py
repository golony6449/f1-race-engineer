"""F1 25 세션 UID에서 데모 송신기 세션인지 가른다(SCRUM-93).

데모 송신기는 실제 게임과 같은 UDP 규격으로 패킷을 보내지만 좌표는 저장소
서킷 데이터(게임 좌표와 다름)를 쓴다. 그래서 데모 세션이 저장한 기준 랩을
실제 게임 세션이 물려받으면 라인·트랙 이탈 판정이 수백 m 어긋난다(2026-11-14
스즈카 세션). 반대로 데모 세션이 실제 기준을 덮어써도 안 된다.

데모 UID는 위 32비트가 `0x5EED0000`이다. 아래 32비트는 시드다.
"""

from __future__ import annotations

# 데모 송신기 세션 UID의 위 32비트 표식.
DEMO_SESSION_UID_PREFIX = 0x5EED_0000
# 아래 32비트 시드 마스크.
_SEED_MASK = 0xFFFF_FFFF


def demo_session_uid(seed: int) -> int:
    """데모 송신기가 쓰는 세션 UID. 지금 송신기 식과 같은 값이다."""
    return (DEMO_SESSION_UID_PREFIX << 32) | (seed & _SEED_MASK)


def is_demo_session_uid(uid: int | None) -> bool:
    """세션 UID가 데모 송신기 것인가. UID를 모르면(`None`) False."""
    if uid is None:
        return False
    return uid >> 32 == DEMO_SESSION_UID_PREFIX


__all__ = [
    "DEMO_SESSION_UID_PREFIX",
    "demo_session_uid",
    "is_demo_session_uid",
]
