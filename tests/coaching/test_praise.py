"""칭찬 무전 테스트(SCRUM-125).

한 랩 최대 1번, 우선순위(개인 최고 랩 > 섹터 최고 > 순위 상승 > 클린 랩),
퀄리파잉 짧은 문장, 무효·피트 랩 제외를 확인한다. 공용 conftest를 만들지
않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import pytest

from f1re.coaching.praise import (
    PRAISE_CLEAN_LAP,
    PRAISE_PERSONAL_BEST,
    PRAISE_POSITION_GAINED,
    PRAISE_SECTOR_BEST,
    PraiseContext,
    PraiseTracker,
)
from f1re.laps.traces import LapTrace, Sample
from f1re.radio.queue import RadioKind, RadioQueue
from f1re.radio.templates import TEMPLATES

TRACK_LENGTH_M = 5807.0
SECTOR2_M = TRACK_LENGTH_M / 3.0
SECTOR3_M = TRACK_LENGTH_M * 2.0 / 3.0
NOW_NS = 10_000_000_000


# ---------------------------------------------------------------------------
# 도우미


def _sample(dist_m: float, t_ms: int) -> Sample:
    return Sample(
        dist_m=dist_m,
        t_ms=t_ms,
        speed=200.0,
        throttle=1.0,
        brake=0.0,
        steer=0.0,
        gear=7,
        x=0.0,
        z=0.0,
    )


def _trace(
    *,
    lap: int,
    lap_ms: int = 90_000,
    sector1_ms: int | None = None,
    sector2_ms: int | None = None,
    sector3_ms: int | None = None,
    valid: bool = True,
    car_index: int = 0,
) -> LapTrace:
    """랩 길이를 세 구간으로 나눈 직선 주행 랩. 섹터 시간을 직접 정할 수 있다."""
    first = lap_ms // 3 if sector1_ms is None else sector1_ms
    second = lap_ms // 3 if sector2_ms is None else sector2_ms
    third = lap_ms - first - second if sector3_ms is None else sector3_ms
    t2 = first
    t3 = first + second
    total = first + second + third
    samples = [
        _sample(0.0, 0),
        _sample(SECTOR2_M, t2),
        _sample(SECTOR3_M, t3),
        _sample(TRACK_LENGTH_M, total),
    ]
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=valid,
        invalid_reason=None if valid else "pit",
        samples=samples,
    )


def _tracker(**kwargs) -> PraiseTracker:
    tracker = PraiseTracker(**kwargs)
    tracker.configure(track_length_m=TRACK_LENGTH_M)
    return tracker


def _choices(key: str, mood: str, **slots: object) -> list[str]:
    return [text.format(**slots) for text in TEMPLATES[key].texts_for(mood)]


def _ctx(
    *,
    session_type: int | None = 18,
    total_laps: int | None = None,
    position: int | None = None,
) -> PraiseContext:
    return PraiseContext(
        session_type=session_type,
        total_laps=total_laps,
        position=position,
    )


# ---------------------------------------------------------------------------
# 개인 최고 랩


def test_personal_best_after_a_baseline_lap() -> None:
    tracker = _tracker()

    # 첫 유효 랩은 기준만 세우고 칭찬하지 않는다.
    assert tracker.on_lap_completed(_trace(lap=1, lap_ms=91_000), _ctx()) == []

    messages = tracker.on_lap_completed(_trace(lap=2, lap_ms=90_000), _ctx())

    assert len(messages) == 1
    message = messages[0]
    assert message.kind == RadioKind.IMPROVEMENT
    assert message.evidence["praise"] == PRAISE_PERSONAL_BEST
    assert message.evidence["lap_time_ms"] == 90_000
    assert message.evidence["mood"] == "warm"
    assert message.text in _choices("praise.personal_best", "warm", lap_s="90.0")


def test_equal_or_slower_lap_is_not_personal_best() -> None:
    tracker = _tracker()
    tracker.on_lap_completed(_trace(lap=1, lap_ms=90_000), _ctx())

    # 같은 시간(동률)도 최고 기록 갱신이 아니다.
    assert tracker.on_lap_completed(_trace(lap=2, lap_ms=90_000), _ctx()) == []
    # 섹터도 그대로라 아무 칭찬이 없다.
    assert tracker.on_lap_completed(_trace(lap=3, lap_ms=90_000), _ctx()) == []


def test_only_one_praise_per_lap() -> None:
    tracker = _tracker()
    tracker.on_lap_completed(_trace(lap=1, lap_ms=91_000), _ctx())
    first = tracker.on_lap_completed(_trace(lap=2, lap_ms=90_000), _ctx())
    assert len(first) == 1

    # 같은 랩을 다시 처리해도(중복 완료) 두 번째 칭찬은 나가지 않는다.
    duplicate = tracker.on_lap_completed(
        _trace(lap=2, lap_ms=89_000), _ctx(position=3)
    )

    assert duplicate == []


# ---------------------------------------------------------------------------
# 우선순위


def test_personal_best_wins_over_sector_and_position() -> None:
    tracker = _tracker()
    tracker.on_lap_completed(
        _trace(lap=1, lap_ms=91_000), _ctx(position=10)
    )

    messages = tracker.on_lap_completed(
        _trace(lap=2, lap_ms=89_000), _ctx(position=8)
    )

    assert len(messages) == 1
    assert messages[0].evidence["praise"] == PRAISE_PERSONAL_BEST


def test_sector_best_wins_over_position_gain() -> None:
    tracker = _tracker()
    tracker.on_lap_completed(
        _trace(lap=1, lap_ms=90_000), _ctx(position=10)
    )

    # 전체 랩은 느려져(92초) 개인 최고가 아니지만 2섹터는 28초로 줄었다.
    messages = tracker.on_lap_completed(
        _trace(
            lap=2,
            lap_ms=92_000,
            sector1_ms=33_000,
            sector2_ms=28_000,
            sector3_ms=31_000,
        ),
        _ctx(position=8),
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.evidence["praise"] == PRAISE_SECTOR_BEST
    assert message.evidence["sector"] == 2
    assert message.evidence["sector_time_ms"] == 28_000
    assert message.text in _choices(
        "praise.sector_best", "warm", sector=2, sector_s="28.0"
    )


def test_position_gain_when_no_best_lap_or_sector() -> None:
    tracker = _tracker()
    tracker.on_lap_completed(
        _trace(lap=1, lap_ms=90_000), _ctx(position=10)
    )

    messages = tracker.on_lap_completed(
        _trace(lap=2, lap_ms=90_000), _ctx(position=8)
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.evidence["praise"] == PRAISE_POSITION_GAINED
    assert message.evidence["position"] == 8
    assert message.evidence["previous_position"] == 10
    assert message.text in _choices(
        "praise.position_gained", "warm", position=8
    )


def test_clean_lap_after_invalid_lap() -> None:
    """직전 랩이 무효였을 때만 클린 랩을 칭찬한다(복귀)."""
    tracker = _tracker()
    assert tracker.on_lap_completed(
        _trace(lap=1, lap_ms=95_000, valid=False), _ctx()
    ) == []

    messages = tracker.on_lap_completed(
        _trace(lap=2, lap_ms=90_000), _ctx()
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.evidence["praise"] == PRAISE_CLEAN_LAP
    assert message.text in _choices("praise.clean_lap", "warm")


def test_clean_lap_is_not_repeated_every_lap() -> None:
    tracker = _tracker()
    tracker.on_lap_completed(_trace(lap=1, lap_ms=90_000), _ctx())

    # 연속 정상 랩은 클린 랩 칭찬을 하지 않는다(매 랩 소음 방지).
    assert tracker.on_lap_completed(_trace(lap=2, lap_ms=90_000), _ctx()) == []
    assert tracker.on_lap_completed(_trace(lap=3, lap_ms=90_000), _ctx()) == []


# ---------------------------------------------------------------------------
# 무효 랩·피트 랩


@pytest.mark.parametrize("valid", [False])
def test_invalid_lap_is_not_praised_and_does_not_set_best(valid: bool) -> None:
    tracker = _tracker()
    assert tracker.on_lap_completed(
        _trace(lap=1, lap_ms=80_000, valid=valid), _ctx()
    ) == []

    # 무효 랩(80초)은 개인 최고 기준이 되지 않는다. 첫 유효 랩은 기준을
    # 세우고, 직전 랩이 무효였으므로 클린 랩 칭찬이 나간다.
    messages = tracker.on_lap_completed(_trace(lap=2, lap_ms=90_000), _ctx())
    assert len(messages) == 1
    assert messages[0].evidence["praise"] == PRAISE_CLEAN_LAP
    # 그다음 유효 랩이 기준(90초)보다 빠르면 그때 개인 최고다.
    messages = tracker.on_lap_completed(_trace(lap=3, lap_ms=89_000), _ctx())
    assert len(messages) == 1
    assert messages[0].evidence["praise"] == PRAISE_PERSONAL_BEST
    assert messages[0].evidence["lap_time_ms"] == 89_000


def test_sector_best_not_set_by_invalid_lap() -> None:
    tracker = _tracker()
    tracker.on_lap_completed(
        _trace(lap=1, lap_ms=60_000, valid=False), _ctx()
    )
    # 첫 유효 랩이 섹터 기준을 세운다(섹터 칭찬 없음). 직전 무효 랩 때문에
    # 클린 랩 칭찬만 나간다.
    messages = tracker.on_lap_completed(_trace(lap=2, lap_ms=90_000), _ctx())
    assert [message.evidence["praise"] for message in messages] == [
        PRAISE_CLEAN_LAP
    ]
    # 3랩째는 전체가 느려지지만 1섹터만 30초 → 25초로 줄었다. 무효 랩(60초,
    # 섹터 20초)이 기준이었다면 이 칭찬이 나지 않았을 것이다.
    messages = tracker.on_lap_completed(
        _trace(
            lap=3,
            lap_ms=95_000,
            sector1_ms=25_000,
            sector2_ms=35_000,
            sector3_ms=35_000,
        ),
        _ctx(),
    )
    assert len(messages) == 1
    assert messages[0].evidence["praise"] == PRAISE_SECTOR_BEST
    assert messages[0].evidence["sector"] == 1
    assert messages[0].evidence["sector_time_ms"] == 25_000


# ---------------------------------------------------------------------------
# 퀄리파잉 짧은 문장


@pytest.mark.parametrize("session_type", [5, 6, 7, 10, 11, 12])
def test_qualifying_and_shootout_use_short_sentences(session_type: int) -> None:
    tracker = _tracker()
    track_lap = _trace(lap=1, lap_ms=91_000)
    tracker.on_lap_completed(track_lap, _ctx(session_type=session_type))

    messages = tracker.on_lap_completed(
        _trace(lap=2, lap_ms=90_000), _ctx(session_type=session_type)
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.evidence["qualifying"] is True
    assert message.text in _choices("praise.personal_best_short", "warm")


def test_practice_uses_full_sentences() -> None:
    tracker = _tracker()
    tracker.on_lap_completed(_trace(lap=1, lap_ms=91_000), _ctx(session_type=1))

    messages = tracker.on_lap_completed(
        _trace(lap=2, lap_ms=90_000), _ctx(session_type=1)
    )

    assert messages[0].evidence["qualifying"] is False
    assert messages[0].text in _choices(
        "praise.personal_best", "warm", lap_s="90.0"
    )


# ---------------------------------------------------------------------------
# 감정 단계·무전 계층


def test_last_three_laps_uses_hyped_wording() -> None:
    tracker = _tracker()
    # 20랩 레이스의 17랩(남은 4랩)이 기준, 18랩(남은 3랩)에 개인 최고.
    tracker.on_lap_completed(
        _trace(lap=17, lap_ms=91_000), _ctx(session_type=15, total_laps=20)
    )

    messages = tracker.on_lap_completed(
        _trace(lap=18, lap_ms=90_000), _ctx(session_type=15, total_laps=20)
    )

    assert len(messages) == 1
    assert messages[0].evidence["mood"] == "hyped"
    assert messages[0].text in _choices(
        "praise.personal_best", "hyped", lap_s="90.0"
    )


def test_queue_receives_praise_and_muting_suppresses_it() -> None:
    queue = RadioQueue(clock=lambda: NOW_NS)
    tracker = _tracker(queue=queue)
    tracker.on_lap_completed(_trace(lap=1, lap_ms=91_000), _ctx(), NOW_NS)

    messages = tracker.on_lap_completed(
        _trace(lap=2, lap_ms=90_000), _ctx(), NOW_NS
    )

    assert len(messages) == 1
    assert queue.pending_count == 1
    assert queue.pending()[0].kind == RadioKind.IMPROVEMENT

    muted = RadioQueue(clock=lambda: NOW_NS)
    assert muted.set_muted(RadioKind.IMPROVEMENT, True)
    muted_tracker = _tracker(queue=muted)
    muted_tracker.on_lap_completed(
        _trace(lap=1, lap_ms=91_000), _ctx(), NOW_NS
    )
    assert (
        muted_tracker.on_lap_completed(
            _trace(lap=2, lap_ms=90_000), _ctx(), NOW_NS
        )
        == []
    )
    assert muted.pending_count == 0


def test_custom_sector_boundaries_from_session_packet() -> None:
    """Session 패킷의 섹터 경계를 쓰면 그 지점에서 시간을 보간한다."""
    tracker = PraiseTracker()
    tracker.configure(
        track_length_m=TRACK_LENGTH_M, sector2_m=1000.0, sector3_m=4000.0
    )
    first = LapTrace(
        car_index=0,
        lap=1,
        valid=True,
        invalid_reason=None,
        samples=[
            _sample(0.0, 0),
            _sample(1000.0, 20_000),
            _sample(4000.0, 60_000),
            _sample(TRACK_LENGTH_M, 90_000),
        ],
    )
    tracker.on_lap_completed(first, _ctx())

    # 2섹터만 40초 → 35초로 줄고 전체 랩은 85초로 빨라진다(개인 최고가 우선).
    second = LapTrace(
        car_index=0,
        lap=2,
        valid=True,
        invalid_reason=None,
        samples=[
            _sample(0.0, 0),
            _sample(1000.0, 20_000),
            _sample(4000.0, 55_000),
            _sample(TRACK_LENGTH_M, 85_000),
        ],
    )
    messages = tracker.on_lap_completed(second, _ctx())

    assert messages[0].evidence["praise"] == PRAISE_PERSONAL_BEST
    # 다음 랩은 전체는 느려지고 2섹터만 갱신된다.
    third = LapTrace(
        car_index=0,
        lap=3,
        valid=True,
        invalid_reason=None,
        samples=[
            _sample(0.0, 0),
            _sample(1000.0, 23_000),
            _sample(4000.0, 56_000),
            _sample(TRACK_LENGTH_M, 88_000),
        ],
    )
    messages = tracker.on_lap_completed(third, _ctx())

    assert messages[0].evidence["praise"] == PRAISE_SECTOR_BEST
    assert messages[0].evidence["sector"] == 2
    assert messages[0].evidence["sector_time_ms"] == 33_000
