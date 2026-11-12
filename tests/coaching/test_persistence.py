"""조건별 기준 랩 저장과 다음 방문 첫 랩 코칭 테스트(작업 19, SCRUM-69).

설계 6.3의 서킷별 저장 규칙을 SQLite에서 확인한다.

- 키(서킷 + 모드·날씨·차)가 다르면 기준이 섞이지 않는다
  (`test_keys_separate_wet_and_dry`).
- 저장된 기준 랩·집중 코너만으로 다음 방문 첫 랩부터 코칭이 나간다
  (`test_saved_reference_enables_lap1_coaching`, 설계 6.4).
- 조건이 다른 기준밖에 없으면 시간 수치 없이 동작만 말한다
  (`test_condition_mismatch_speaks_actions_not_time`, 설계 6.3).

공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import dataclasses
import json
import random
import time
from pathlib import Path

import pytest

from f1re import db
from f1re.coaching.coach import Coach
from f1re.coaching.focus import FocusConfig, FocusSet
from f1re.coaching.reference import (
    SOURCE_FASTEST_CAR,
    SOURCE_SELF,
    ReferenceSet,
    reference_from_dict,
    reference_to_dict,
)
from f1re.coaching.store import (
    MODE_RACE_WEEKEND,
    MODE_TT,
    WEATHER_DRY,
    WEATHER_WET,
    ReferenceKey,
    ReferenceStore,
    car_key_for,
    mode_for_session_type,
    weather_for_code,
)
from f1re.demo_sender.driving import DriverProfile, Sample as DrivingSample, generate_lap
from f1re.demo_sender.scenarios import corner_effects, parse_scenarios
from f1re.laps.traces import LapTrace, Sample
from f1re.radio.queue import RadioKind
from f1re.tracks.model import Track, load_outline, load_track

SUZUKA_ID = 13
FOCUS_CORNER = 9
_BASE_NS = time.time_ns()

_TRACK = load_track(SUZUKA_ID)
_OUTLINE = load_outline(SUZUKA_ID)
assert _TRACK is not None and _OUTLINE is not None
TRACK: Track = _TRACK
OUTLINE = _OUTLINE
CORNER_NUMBERS = [corner.number for corner in TRACK.corners]


# ---------------------------------------------------------------------------
# 도우미


def _to_sample(sample: DrivingSample) -> Sample:
    return Sample(
        dist_m=sample.dist_m,
        t_ms=sample.t_ms,
        speed=sample.speed_kmh,
        throttle=sample.throttle,
        brake=sample.brake,
        steer=sample.steer,
        gear=sample.gear,
        x=sample.x,
        z=sample.z,
    )


def _effects(events: list[dict]) -> dict:
    return corner_effects(parse_scenarios(events), CORNER_NUMBERS)


def _early_brake(corner: int, meters: float = 10.0) -> dict:
    return _effects(
        [
            {
                "lap": 1,
                "corner": corner,
                "kind": "early_brake",
                "params": {"meters": meters},
            }
        ]
    )


def _trace(
    lap: int,
    *,
    effects: dict | None = None,
    pace: float = 1.0,
    car_index: int = 0,
    valid: bool = True,
    invalid_reason: str | None = None,
) -> LapTrace:
    samples = generate_lap(
        TRACK,
        OUTLINE,
        DriverProfile(pace=pace, consistency=1.0),
        lap,
        random.Random(0),
        corner_effects=effects or {},
    )
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=valid,
        invalid_reason=invalid_reason,
        samples=[_to_sample(sample) for sample in samples],
    )


def _focus_for(references: ReferenceSet, lap: LapTrace) -> FocusSet:
    """최근 랩 손실을 반영한 뒤 집중 코너를 하나로 고정한 `FocusSet`."""
    focus = FocusSet(
        TRACK.corners, references, config=FocusConfig(count=5, recent_laps=3)
    )
    focus.update([lap])
    focus.seed([FOCUS_CORNER])
    return focus


def _key(
    *,
    track_id: int = SUZUKA_ID,
    mode: str = MODE_TT,
    weather: str = WEATHER_DRY,
    car_key: str = "tt_equal",
) -> ReferenceKey:
    return ReferenceKey(
        track_id=track_id, mode=mode, weather=weather, car_key=car_key
    )


def _lap_time_ms(reference: ReferenceSet) -> int:
    trace = reference.best_lap(SOURCE_SELF)
    assert trace is not None
    return max(sample.t_ms for sample in trace.samples)


def _state(sample: Sample, lap: int) -> dict:
    return {
        "t_ms": sample.t_ms,
        "speed": sample.speed,
        "throttle": sample.throttle,
        "brake": sample.brake,
        "steer": sample.steer,
        "gear": sample.gear,
        "x": sample.x,
        "z": sample.z,
        "lap_num": lap,
    }


def _feed(coach: Coach, trace: LapTrace) -> list:
    """랩 샘플을 코치에 넣고 나온 무전 목록을 돌려준다."""
    lap = trace.lap
    base_ns = _BASE_NS + lap * 200_000_000_000
    messages: list = []
    for sample in trace.samples:
        now_ns = base_ns + sample.t_ms * 1_000_000
        messages.extend(coach.on_sample(_state(sample, lap), sample.dist_m, now_ns))
    return messages


def _coach(reference: ReferenceSet | None, focus: tuple[int, ...]) -> Coach:
    coach = Coach(
        TRACK,
        references=reference or ReferenceSet(TRACK.corners),
        focus_config=FocusConfig(count=5, recent_laps=3),
    )
    if focus:
        coach.seed_focus(focus)
    return coach


# ---------------------------------------------------------------------------
# 저장된 기준으로 첫 랩부터 코칭


def test_saved_reference_enables_lap1_coaching(tmp_path: Path) -> None:
    """저장된 기준·집중 코너만 넣은 코치가 다음 방문 1랩에서 코칭한다."""
    path = tmp_path / "f1re.db"
    key = _key()
    references = ReferenceSet(TRACK.corners)
    baseline = _trace(99)
    references.update(baseline)
    slow = _trace(1, effects=_early_brake(FOCUS_CORNER))
    focus = _focus_for(references, slow)

    conn = db.open_db(path)
    try:
        ReferenceStore(conn).save(key, references, focus)
    finally:
        conn.close()

    # 새 연결(=다음 방문 세션)에서 DB만 보고 되살린다.
    conn = db.open_db(path)
    try:
        store = ReferenceStore(conn)
        loaded = store.load(key)
        assert loaded is not None
        best = loaded.best_for(FOCUS_CORNER)
        assert best is not None
        assert best[1] == SOURCE_SELF
        assert best[0].samples[-1].t_ms == baseline.samples[-1].t_ms
        focus_seed = store.load_focus(key)
        assert focus_seed == (FOCUS_CORNER,)
    finally:
        conn.close()

    messages = [
        message
        for message in _feed(_coach(loaded, focus_seed), slow)
        if message.corner == FOCUS_CORNER
    ]
    assert len(messages) == 1, "저장된 기준이 있으면 1랩에서도 코칭이 나가야 한다"
    message = messages[0]
    assert message.kind == RadioKind.COACHING
    assert message.lap == 1
    assert message.text.startswith(f"{FOCUS_CORNER}번")

    # 같은 랩이라도 기준이 없으면 코칭이 나가지 않는다(저장된 기준의 효과).
    assert _feed(_coach(None, focus_seed), slow) == []


# ---------------------------------------------------------------------------
# 조건이 다른 기준


def test_condition_mismatch_speaks_actions_not_time(tmp_path: Path) -> None:
    """비 기준밖에 없으면 시간 수치 없이 동작만 말한다(설계 6.3)."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        store = ReferenceStore(conn)
        wet_key = _key(mode=MODE_RACE_WEEKEND, weather=WEATHER_WET, car_key="team:1")
        dry_key = _key(mode=MODE_RACE_WEEKEND, weather=WEATHER_DRY, car_key="team:1")
        references = ReferenceSet(TRACK.corners)
        baseline = _trace(99)
        references.update(baseline)
        slow = _trace(1, effects=_early_brake(FOCUS_CORNER))
        store.save(wet_key, references, _focus_for(references, slow))

        assert store.load(dry_key) is None, "조건이 다르면 load()는 섞지 않는다"

        stored = store.load_compatible(dry_key)
        assert stored is not None
        assert stored.stored_key == wet_key
        assert stored.conditions_match is False
        assert stored.time_comparable is False
        assert stored.speakable_loss_ms(500) is None
        assert stored.focus == (FOCUS_CORNER,)

        exact = store.load_compatible(wet_key)
        assert exact is not None
        assert exact.conditions_match is True
        assert exact.speakable_loss_ms(500) == 500
    finally:
        conn.close()

    messages = [
        message
        for message in _feed(_coach(stored.reference, stored.focus), slow)
        if message.corner == FOCUS_CORNER
    ]
    assert messages, "조건이 달라도 동작 코칭은 나가야 한다"
    text = messages[0].text
    assert "브레이크" in text
    assert "초" not in text
    assert "ms" not in text.lower()
    assert "밀리" not in text
    # 무전 근거에 남은 시간 손실도 말할 수 있는 값이 아니다.
    assert stored.speakable_loss_ms(messages[0].evidence["loss_ms"]) is None


def test_fallback_picks_most_similar_condition(tmp_path: Path) -> None:
    """여러 조건이 있으면 같은 항목이 많은 기준을 고른다(차·날씨 > 모드)."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        store = ReferenceStore(conn)
        qualy_dry = _key(
            mode=MODE_RACE_WEEKEND, weather=WEATHER_DRY, car_key="team:1"
        )
        race_wet = _key(
            mode=MODE_RACE_WEEKEND, weather=WEATHER_WET, car_key="team:2"
        )
        store.save(qualy_dry, _single_lap_reference(99))
        store.save(race_wet, _single_lap_reference(99, pace=0.98))

        requested = _key(
            mode=MODE_TT, weather=WEATHER_DRY, car_key="team:1"
        )
        stored = store.load_compatible(requested)
        assert stored is not None
        # 모드만 다른 예선 기준(차·날씨 같음)을 먼저 쓴다.
        assert stored.stored_key == qualy_dry
        assert stored.conditions_match is False
    finally:
        conn.close()


def test_no_reference_for_other_track(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        store = ReferenceStore(conn)
        store.save(_key(), _single_lap_reference(99))
        other_track = _key(track_id=5)
        assert store.load(other_track) is None
        assert store.load_compatible(other_track) is None
        assert store.load_focus(other_track) == ()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 키 분리


def test_keys_separate_wet_and_dry(tmp_path: Path) -> None:
    """맑음·비 기준이 같은 서킷에 각각 남고 서로 덮어쓰지 않는다."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        store = ReferenceStore(conn)
        dry_key = _key(weather=WEATHER_DRY)
        wet_key = _key(weather=WEATHER_WET)
        store.save(dry_key, _single_lap_reference(99))
        store.save(wet_key, _single_lap_reference(99, pace=0.97))

        dry = store.load(dry_key)
        wet = store.load(wet_key)
        assert dry is not None and wet is not None
        assert dry is not wet
        assert _lap_time_ms(wet) > _lap_time_ms(dry)

        # 저장 순서를 바꿔도 다른 조건의 행은 그대로다.
        store.save(dry_key, dry)
        reloaded_wet = store.load(wet_key)
        assert reloaded_wet is not None
        assert _lap_time_ms(reloaded_wet) == _lap_time_ms(wet)
    finally:
        conn.close()


def test_condition_key_helpers() -> None:
    """세션 값이 저장 키로 바뀌는 규칙(모드·날씨·차)."""
    assert mode_for_session_type(13) == MODE_TT
    assert mode_for_session_type(10) == MODE_RACE_WEEKEND
    assert mode_for_session_type(None) == MODE_RACE_WEEKEND
    assert weather_for_code(0) == WEATHER_DRY
    assert weather_for_code(2) == WEATHER_DRY
    assert weather_for_code(3) == WEATHER_WET
    assert weather_for_code(5) == WEATHER_WET
    assert car_key_for(3, MODE_TT) == "tt_equal"
    assert car_key_for(3, MODE_RACE_WEEKEND) == "team:3"
    assert car_key_for(None, MODE_RACE_WEEKEND) == "team:unknown"

    key = ReferenceKey.from_session(
        track_id=SUZUKA_ID, session_type=13, weather_code=4, team_id=3
    )
    assert key == _key(mode=MODE_TT, weather=WEATHER_WET, car_key="tt_equal")

    with pytest.raises(ValueError):
        ReferenceKey(SUZUKA_ID, "qualifying", WEATHER_DRY, "tt_equal")
    with pytest.raises(ValueError):
        ReferenceKey(SUZUKA_ID, MODE_TT, "snow", "tt_equal")


# ---------------------------------------------------------------------------
# 직렬화


def test_roundtrip_keeps_fastest_car_reference_and_player_values() -> None:
    """내 최고 랩과 세션 최속차 최고 랩, 내 차 전용 값이 함께 살아남는다."""
    references = ReferenceSet(TRACK.corners)
    me = _trace(1)
    me.samples = [
        dataclasses.replace(sample, wheel_slip=(0.1, 0.2, 0.3, 0.4), fuel_kg=55.5)
        for sample in me.samples
    ]
    # 전체 랩은 더 빠르지만 5번 코너만 이른 브레이크로 느린 최속차.
    fastest_car = _trace(1, pace=1.02, car_index=1, effects=_early_brake(5))
    assert fastest_car.samples[-1].t_ms < me.samples[-1].t_ms
    references.update(me)
    references.update(fastest_car)

    # JSON을 실제로 거쳐 저장 형식이 직렬화 가능한지도 확인한다.
    payload = json.loads(json.dumps(reference_to_dict(references)))
    loaded = reference_from_dict(payload)

    corner5 = loaded.best_for(5)
    corner9 = loaded.best_for(9)
    assert corner5 is not None and corner5[1] == SOURCE_SELF
    assert corner5[0].car_index == 0
    assert corner9 is not None and corner9[1] == SOURCE_FASTEST_CAR
    assert corner9[0].car_index == 1

    best = loaded.best_lap(SOURCE_SELF)
    assert best is not None
    assert best.samples[0].wheel_slip == pytest.approx((0.1, 0.2, 0.3, 0.4))
    assert best.samples[0].fuel_kg == pytest.approx(55.5)
    fastest = loaded.best_lap(SOURCE_FASTEST_CAR)
    assert fastest is not None
    assert fastest.samples[0].wheel_slip is None


def test_store_returns_none_for_corrupt_payload(tmp_path: Path) -> None:
    """DB 행이 손상돼도 예외 없이 기준 없음으로 본다."""
    conn = db.open_db(tmp_path / "f1re.db")
    try:
        store = ReferenceStore(conn)
        key = _key()
        store.save(key, _single_lap_reference(99), [FOCUS_CORNER])
        conn.execute("UPDATE reference_sets SET reference = ?", ("{깨진",))
        conn.execute("UPDATE reference_sets SET focus = ?", ("[]",))
        conn.commit()

        assert store.load(key) is None
        assert store.load_compatible(key) is None
        assert store.load_focus(key) == ()
    finally:
        conn.close()


def _single_lap_reference(lap: int, *, pace: float = 1.0) -> ReferenceSet:
    references = ReferenceSet(TRACK.corners)
    references.update(_trace(lap, pace=pace))
    return references
