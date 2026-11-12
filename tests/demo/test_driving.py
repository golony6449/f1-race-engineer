"""주행 모델 테스트: 코너 목표 속도·랩 타임·재현성."""

from __future__ import annotations

import random

import pytest

from f1re.demo_sender.driving import (
    CORNER_TARGET_SPEEDS_KMH,
    DriverProfile,
    Sample,
    generate_lap,
    lap_time_ms,
    sector_times_ms,
)
from f1re.tracks.model import Track, load_outline, load_track
from f1re.tracks.outline import TrackOutline

SUZUKA_ID = 13
MONACO_ID = 5


def _track_and_outline(track_id: int) -> tuple[Track, TrackOutline]:
    track = load_track(track_id)
    outline = load_outline(track_id)
    assert track is not None and outline is not None
    return track, outline


def _lap(track_id: int, seed: int = 0, driver: DriverProfile | None = None) -> tuple[Track, list[Sample]]:
    track, outline = _track_and_outline(track_id)
    samples = generate_lap(track, outline, driver or DriverProfile(), 1, random.Random(seed))
    return track, samples


@pytest.mark.parametrize("track_id", [SUZUKA_ID, MONACO_ID])
def test_lap_speed_minimum_inside_each_corner(track_id: int) -> None:
    """코너마다 최저 속도 지점이 entry~exit 안에 있고 목표 속도를 넘지 않는다."""
    track, samples = _lap(track_id)
    targets = CORNER_TARGET_SPEEDS_KMH[track_id]
    for corner in track.corners:
        inside = [s for s in samples if corner.entry_m <= s.dist_m <= corner.exit_m]
        assert inside, f"{track.name} {corner.number}번 코너 샘플이 없습니다"
        slowest = min(inside, key=lambda sample: sample.speed_kmh)
        assert corner.entry_m <= slowest.dist_m <= corner.exit_m
        # 샘플 간격 때문에 apex 근처 값이 ±1km/h 정도 튈 수 있다.
        assert slowest.speed_kmh <= targets[corner.number] + 2.0, (
            f"{track.name} {corner.number}번: {slowest.speed_kmh:.1f} > "
            f"{targets[corner.number]:.1f}"
        )


def test_lap_time_reasonable() -> None:
    """타임 트라이얼 랩 타임이 실제 서킷의 대략 범위에 들어온다."""
    suzuka_ms = lap_time_ms(_lap(SUZUKA_ID)[1])
    monaco_ms = lap_time_ms(_lap(MONACO_ID)[1])

    assert 85_000 <= suzuka_ms <= 100_000, suzuka_ms
    assert 68_000 <= monaco_ms <= 80_000, monaco_ms


def test_seed_reproducible() -> None:
    """같은 시드는 같은 주행을, 다른 시드는 다른 주행을 만든다."""
    driver = DriverProfile(consistency=0.4)
    track, outline = _track_and_outline(SUZUKA_ID)

    first = generate_lap(track, outline, driver, 1, random.Random(7))
    same = generate_lap(track, outline, driver, 1, random.Random(7))
    other = generate_lap(track, outline, driver, 1, random.Random(8))

    assert first == same
    assert [sample.speed_kmh for sample in first] != [sample.speed_kmh for sample in other]


def test_sector_times_sum_to_lap_time() -> None:
    track, samples = _lap(MONACO_ID)
    sectors = sector_times_ms(samples, track)
    total = lap_time_ms(samples)

    assert all(sector > 0 for sector in sectors)
    assert abs(sum(sectors) - total) <= 20


def test_samples_are_plausible() -> None:
    track, samples = _lap(SUZUKA_ID)

    assert samples
    assert samples[0].t_ms == 0
    distances = [sample.dist_m for sample in samples]
    assert distances == sorted(distances)
    assert all(0.0 <= sample.dist_m <= track.length_m + 1.0 for sample in samples)
    for sample in samples:
        assert 0.0 < sample.speed_kmh <= 340.0
        assert 0.0 <= sample.throttle <= 1.0
        assert 0.0 <= sample.brake <= 1.0
        assert -1.0 <= sample.steer <= 1.0
        assert 1 <= sample.gear <= 8
        assert 0 <= sample.rpm <= 15000
        assert sample.x == sample.x and sample.z == sample.z  # NaN 아님


def test_faster_pace_makes_lap_shorter() -> None:
    track, outline = _track_and_outline(MONACO_ID)
    baseline = generate_lap(track, outline, DriverProfile(), 1, random.Random(3))
    faster = generate_lap(
        track, outline, DriverProfile(pace=1.05), 1, random.Random(3)
    )
    assert lap_time_ms(faster) < lap_time_ms(baseline)


def test_driver_profile_validation() -> None:
    with pytest.raises(ValueError):
        DriverProfile(pace=0.0)
    with pytest.raises(ValueError):
        DriverProfile(consistency=1.5)
