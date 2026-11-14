"""데모 송신기 세션 UID 판별(SCRUM-93)."""

from __future__ import annotations

import pytest

from f1re.udp.session_uid import (
    DEMO_SESSION_UID_PREFIX,
    demo_session_uid,
    is_demo_session_uid,
)

# 실제 DB에 저장된 실제 게임 세션 UID 표본.
REAL_SESSION_UIDS = (
    0x65153D81F29B6D98,
    0x46C6A53CF8F7D1B8,
    0x0C639F026E854E87,
)


def test_demo_session_uid_matches_sender_formula() -> None:
    """데모 송신기의 기존 식(0x5EED_0000_0000_0000 | seed)과 같은 값을 낸다."""
    for seed in (0, 1, 0x1234_5678, 0xFFFF_FFFF, 0x1_0000_0001):
        assert demo_session_uid(seed) == (
            0x5EED_0000_0000_0000 | (seed & 0xFFFF_FFFF)
        )


def test_demo_session_uid_prefix_is_upper_32_bits() -> None:
    uid = demo_session_uid(0x1234_5678)

    assert uid == 0x5EED_0000_1234_5678
    assert uid >> 32 == DEMO_SESSION_UID_PREFIX == 0x5EED_0000


def test_is_demo_session_uid_true_for_demo_uid() -> None:
    assert is_demo_session_uid(0x5EED_0000_1234_5678) is True
    assert is_demo_session_uid(demo_session_uid(0)) is True


@pytest.mark.parametrize("uid", REAL_SESSION_UIDS)
def test_is_demo_session_uid_false_for_real_session_uids(uid: int) -> None:
    assert is_demo_session_uid(uid) is False


def test_is_demo_session_uid_false_for_none_and_neighbour_prefix() -> None:
    assert is_demo_session_uid(None) is False
    # 위 32비트가 정확히 0x5EED0000일 때만 데모다.
    assert is_demo_session_uid(0x5EED_0001_0000_0000) is False
    assert is_demo_session_uid(0x0) is False
