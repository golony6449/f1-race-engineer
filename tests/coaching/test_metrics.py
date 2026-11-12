"""코너 지표 계산 테스트(작업 15, SCRUM-65).

데모 송신기의 coaching 시나리오 주입값으로 지표가 잡히는지 본다(계획 작업 15
Step 1). 공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import dataclasses
import random
import tomllib
from pathlib import Path

import pytest

from f1re.coaching.metrics import (
    CoachingThresholds,
    corner_duration_ms,
    corner_metrics,
    default_coaching_thresholds,
    load_coaching_thresholds,
    time_at_distance,
)
from f1re.config import ConfigError
from f1re.demo_sender.driving import DriverProfile, Sample as DrivingSample, generate_lap
from f1re.demo_sender.scenarios import (
    SCENARIO_DIR,
    corner_effects,
    events_for_lap,
    load_scenarios,
)
from f1re.laps.traces import LapTrace, Sample
from f1re.tracks.model import Corner, Track, load_outline, load_track
from f1re.tracks.outline import TrackOutline

SUZUKA_ID = 13
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "default.toml"

_TRACK = load_track(SUZUKA_ID)
_OUTLINE = load_outline(SUZUKA_ID)
assert _TRACK is not None and _OUTLINE is not None
TRACK: Track = _TRACK
OUTLINE: TrackOutline = _OUTLINE


# ---------------------------------------------------------------------------
# 도우미


def _corner(number: int) -> Corner:
    return next(corner for corner in TRACK.corners if corner.number == number)


def _coaching_events():
    return load_scenarios(SCENARIO_DIR / "coaching.yaml")


def _effects_for(lap: int):
    numbers = [corner.number for corner in TRACK.corners]
    return corner_effects(events_for_lap(_coaching_events(), lap), numbers)


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


def _trace(
    lap: int,
    *,
    effects: dict | None = None,
    driver: DriverProfile | None = None,
    car_index: int = 0,
    seed: int = 0,
    valid: bool = True,
    invalid_reason: str | None = None,
) -> LapTrace:
    """데모 주행 모델로 랩 하나를 만들어 `LapTrace`로 감싼다."""
    samples = generate_lap(
        TRACK,
        OUTLINE,
        driver or DriverProfile(consistency=1.0),
        lap,
        random.Random(seed),
        corner_effects=effects or {},
    )
    return LapTrace(
        car_index=car_index,
        lap=lap,
        valid=valid,
        invalid_reason=invalid_reason,
        samples=[_to_sample(sample) for sample in samples],
    )


def _scenario_trace(lap: int, **kwargs) -> LapTrace:
    """coaching.yaml의 그 랩 주입을 넣은 랩."""
    return _trace(lap, effects=_effects_for(lap), **kwargs)


# ---------------------------------------------------------------------------
# 시나리오별 지표


def test_late_brake_detected() -> None:
    """3번 코너 늦은 브레이크 12m: 브레이크 시작이 뒤로 밀린다.

    데모 주행 모델은 12m 주입을 브레이크 시작 약 15m 이동으로 만든다. 계획의
    ±2m는 이 생성 모델에서 나오지 않아 ±5m로 확인한다(보고서에 근거 기록).
    """
    baseline = _trace(4)
    scenario = _scenario_trace(1)
    corner = _corner(3)

    base = corner_metrics(baseline, corner).brake_start_m
    late = corner_metrics(scenario, corner).brake_start_m
    assert base is not None and late is not None
    shift = late - base
    assert shift == pytest.approx(12.0, abs=5.0), shift
    assert shift > 0.0

    # 최대 브레이크 압력과 트레일 브레이킹 지표도 값이 채워진다.
    metrics = corner_metrics(scenario, corner)
    assert 0.0 < metrics.brake_peak <= 1.0
    assert 0.0 <= metrics.trail_brake_residual <= 1.0


def test_late_brake_shifts_min_speed_later() -> None:
    """늦은 브레이크는 최저 속도 지점(정점)을 코너 뒤쪽으로 민다."""
    baseline = _trace(4)
    scenario = _scenario_trace(1)
    corner = _corner(3)

    base = corner_metrics(baseline, corner)
    late = corner_metrics(scenario, corner)
    assert late.min_speed_m > base.min_speed_m


def test_early_brake_detected() -> None:
    """5번 코너 이른 브레이크 10m: 브레이크 시작이 앞으로 당겨진다."""
    baseline = _trace(4)
    scenario = _scenario_trace(1)
    corner = _corner(5)

    base = corner_metrics(baseline, corner).brake_start_m
    early = corner_metrics(scenario, corner).brake_start_m
    assert base is not None and early is not None
    assert early < base - 3.0, (early, base)


def test_turn_in_early_detected() -> None:
    """7번 코너 이른 턴인 6m: 조향 시작이 그만큼 앞당겨진다."""
    baseline = _trace(4)
    scenario = _scenario_trace(2)
    corner = _corner(7)

    base = corner_metrics(baseline, corner).turn_in_m
    early = corner_metrics(scenario, corner).turn_in_m
    assert base is not None and early is not None
    assert early == pytest.approx(base - 6.0, abs=2.0), (early, base)


def test_steer_corrections_counted() -> None:
    """3번 코너 조향 수정 3회: 주입한 횟수와 같게 세고, 기준 랩은 0이다."""
    baseline = _trace(4)
    scenario = _scenario_trace(3)
    corner = _corner(3)

    assert corner_metrics(scenario, corner).steer_corrections == 3
    assert corner_metrics(baseline, corner).steer_corrections == 0
    # 수정이 들어간 랩이 최대 조향량도 더 크다.
    assert (
        corner_metrics(scenario, corner).steer_peak
        > corner_metrics(baseline, corner).steer_peak
    )


def test_turn_in_not_moved_by_steer_corrections() -> None:
    """코너 안 조향 수정이 잠깐 문턱값 아래로 내려가도 턴인 지점은 그대로다."""
    baseline = _trace(4)
    scenario = _scenario_trace(3)
    corner = _corner(3)

    base = corner_metrics(baseline, corner).turn_in_m
    corrected = corner_metrics(scenario, corner).turn_in_m
    assert base is not None and corrected is not None
    assert corrected == pytest.approx(base, abs=2.0), (corrected, base)


def test_steer_corrections_ignore_noise() -> None:
    """작은 조향 잡음은 수정 횟수에 더해지지 않는다."""
    scenario = _scenario_trace(3)
    noisy = LapTrace(
        car_index=scenario.car_index,
        lap=scenario.lap,
        valid=True,
        invalid_reason=None,
        samples=[
            dataclasses.replace(
                sample,
                steer=sample.steer + (0.01 if index % 2 else -0.01),
            )
            for index, sample in enumerate(scenario.samples)
        ],
    )
    corner = _corner(3)
    assert corner_metrics(noisy, corner).steer_corrections == 3


def test_late_throttle_detected() -> None:
    """9번 코너 늦은 스로틀 35m: 스로틀을 다시 밟는 지점이 그만큼 늦어진다."""
    baseline = _trace(4)
    scenario = _scenario_trace(2)
    corner = _corner(9)

    base = corner_metrics(baseline, corner).throttle_on_m
    late = corner_metrics(scenario, corner).throttle_on_m
    assert base is not None and late is not None
    assert late - base == pytest.approx(35.0, abs=4.0), (late, base)
    assert (
        corner_metrics(scenario, corner).full_throttle_m
        >= corner_metrics(scenario, corner).throttle_on_m
    )


def test_line_deviation_m() -> None:
    """5번 코너 트랙 이탈 주입 7.5m: 기준 궤적에서 벗어난 최대 거리와 트랙 이탈."""
    baseline = _trace(4)
    scenario = _scenario_trace(3)
    corner = _corner(5)

    from f1re.coaching.reference import trace_outline

    ref_path = trace_outline(baseline)
    assert ref_path is not None
    metrics = corner_metrics(scenario, corner, ref_path)
    assert metrics.max_line_dev_m == pytest.approx(7.5, abs=1.0)
    assert metrics.off_track is True

    # 기준 랩 자신은 라인을 벗어나지 않는다.
    base_metrics = corner_metrics(baseline, corner, ref_path)
    assert base_metrics.max_line_dev_m == pytest.approx(0.0, abs=0.05)
    assert base_metrics.off_track is False


def test_line_deviation_without_reference_is_zero() -> None:
    metrics = corner_metrics(_trace(4), _corner(5), None)
    assert metrics.max_line_dev_m == 0.0
    assert metrics.off_track is False


def test_wheel_slip_is_recorded_for_player_only() -> None:
    """휠 슬립은 Motion Ex가 있는 내 차 샘플에만 값이 있다(설계 6.2)."""
    trace = _trace(4)
    corner = _corner(3)
    assert corner_metrics(trace, corner).exit_wheel_slip is None

    with_slip = LapTrace(
        car_index=0,
        lap=trace.lap,
        valid=True,
        invalid_reason=None,
        samples=[
            dataclasses.replace(sample, wheel_slip=(0.1, 0.2, 0.3, 0.4))
            for sample in trace.samples
        ],
    )
    metrics = corner_metrics(with_slip, corner)
    assert metrics.exit_wheel_slip == pytest.approx(0.4)

    # 다른 차 샘플에는 슬립이 없으므로 같은 비교에 쓸 수 없다.
    other_car = _trace(1, car_index=1)
    assert corner_metrics(other_car, corner).exit_wheel_slip is None


def test_brake_start_ignores_previous_corner_braking() -> None:
    """앞 코너 브레이크가 코너 진입 전에 끝나면 이 코너 브레이크로 보지 않는다."""
    trace = _trace(4)
    # 2번 코너(645m)는 1번 코너 제동이 끝난 뒤 시작한다. 자체 제동이 없다.
    assert corner_metrics(trace, _corner(2)).brake_start_m is None
    # 17번 코너도 16번 코너 제동과 떨어져 있으면 잡지 않는다.
    assert corner_metrics(trace, _corner(17)).brake_start_m is None


def test_corner_time_and_empty_trace() -> None:
    trace = _trace(4)
    corner = _corner(3)
    metrics = corner_metrics(trace, corner)
    expected = int(
        round(
            time_at_distance(trace, corner.exit_m)
            - time_at_distance(trace, corner.entry_m)
        )
    )
    assert metrics.corner_time_ms == expected
    assert metrics.corner_time_ms > 0
    assert corner_duration_ms(trace, corner) == pytest.approx(expected, abs=1.0)

    empty = LapTrace(
        car_index=0, lap=1, valid=False, invalid_reason="outlap", samples=[]
    )
    blank = corner_metrics(empty, corner)
    assert blank.corner == 3
    assert blank.brake_start_m is None
    assert blank.turn_in_m is None
    assert blank.steer_corrections == 0
    assert blank.corner_time_ms == 0
    assert blank.exit_wheel_slip is None


# ---------------------------------------------------------------------------
# 설정 파일


def _config_thresholds() -> dict:
    with CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)["coaching"]["thresholds"]


def test_thresholds_match_config_file() -> None:
    """모든 문턱값이 `config/default.toml`의 값과 같다."""
    raw = _config_thresholds()
    thresholds = default_coaching_thresholds()
    for field in dataclasses.fields(CoachingThresholds):
        assert getattr(thresholds, field.name) == pytest.approx(float(raw[field.name]))
    assert load_coaching_thresholds(CONFIG_PATH) == thresholds


def test_load_thresholds_rejects_missing_section(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[global]\nudp_port = 20777\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="coaching.thresholds"):
        load_coaching_thresholds(path)


def test_load_thresholds_rejects_unknown_key(tmp_path: Path) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    broken = text.replace(
        "[coaching.thresholds]\n\n# 브레이크를 밟기 시작한",
        "[coaching.thresholds]\nunknown_key = 1.0\n\n# 브레이크를 밟기 시작한",
    )
    assert broken != text
    path = tmp_path / "config.toml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown_key"):
        load_coaching_thresholds(path)


def test_load_thresholds_rejects_inverted_full_throttle(tmp_path: Path) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    broken = text.replace("full_throttle = 0.95", "full_throttle = 0.01")
    assert broken != text
    path = tmp_path / "config.toml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ConfigError, match="full_throttle"):
        load_coaching_thresholds(path)
