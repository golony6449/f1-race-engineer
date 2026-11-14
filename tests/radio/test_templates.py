"""무전 문장 틀: 슬롯, 말투 규칙, 리프트 앤 코스트, 미리 만들기 문장."""

from __future__ import annotations

import pytest

from f1re.radio.queue import RadioKind
from f1re.radio.templates import (
    AMOUNT_SLOTS,
    PRELOAD_KEYS,
    TEMPLATES,
    WHERE_SLOTS,
    MissingSlot,
    UnknownTemplate,
    all_templates,
    corner_phrases,
    prefetch_phrases,
    render,
    template_slots,
    templates_by_category,
)

SAMPLE_VALUES: dict[str, object] = {
    "corner": 3,
    "corner_label": "3번째 코너",
    "corners": "3·5",
    "meters": 5,
    "count": 2,
    "laps": 2,
    "gap": 0.6,
    "wait_laps": 2,
    "kmh": 7,
    "tyre": "왼쪽 앞",
    "tyres": "4개",
    "percent": 82,
    "celsius": 110,
    "part": "앞날개",
    "seconds": 5,
    "next_corner": 7,
    "next_label": "7번",
}


def _sample_for(key: str) -> dict[str, object]:
    return {name: SAMPLE_VALUES[name] for name in template_slots(key)}


def test_template_keys_are_unique_and_categories_are_radio_kinds() -> None:
    keys = [template.key for template in all_templates()]

    assert len(keys) == len(set(keys))
    assert all(template.category in set(RadioKind) for template in all_templates())
    assert len(TEMPLATES) == len(keys)


def test_all_templates_render_short_korean_text() -> None:
    for template in all_templates():
        text = render(template.key, **_sample_for(template.key))

        assert text.strip()
        assert text != template.key
        # 무전은 짧아야 한다. 한 문장이 길어지면 말이 늦게 끝난다.
        assert len(text) <= 80, (template.key, text)


def test_templates_have_where_what_howmuch() -> None:
    checked = 0
    for template in all_templates():
        if not template.needs_evidence:
            continue
        checked += 1
        slots = set(template_slots(template.key))

        assert slots & WHERE_SLOTS, f"{template.key}: 위치 슬롯이 없습니다"
        assert slots & AMOUNT_SLOTS, f"{template.key}: 수치 슬롯이 없습니다"
        assert template.category in {RadioKind.COACHING, RadioKind.OVERTAKE}
    assert checked >= 10


def test_lift_and_coast_templates_present() -> None:
    assert (
        render("coaching.fuel_lift", laps=2, corners="3·7", meters=40)
        == "연료가 2랩 모자라. 3·7에서 40미터 일찍 발 떼."
    )
    assert render("improvement.fuel_lift_good", corner=7) == "7번 리프트 좋아. 그대로 해."
    assert (
        render("coaching.fuel_lift_short", corner=7, meters=10)
        == "7번 리프트가 짧았어. 10미터 더 일찍 떼."
    )
    assert render("improvement.fuel_lift_stop") == "연료 회복됐어. 이제 리프트 안 해도 돼."


def test_coaching_examples_match_radio_wording() -> None:
    assert (
        render("coaching.brake_late", corner=3, meters=5)
        == "3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아."
    )
    assert (
        render("coaching.late_throttle", corner=7, meters=10)
        == "7번 탈출에서 가속이 늦었어. 10미터 더 일찍 밟아."
    )
    assert render("overtake.drs", gap="0.6") == "앞차랑 0.6초야. 다음 직선에서 DRS 써."
    assert render("box.box_box") == "박스, 박스. 들어와."


def test_bundled_warning_templates_match_radio_wording() -> None:
    """바퀴별 경고를 묶을 때의 문장(SCRUM-81)."""
    assert (
        render("safety.tyre_wear_bundle", tyres="4개", percent=88)
        == "4개 타이어 마모가 88퍼센트야."
    )
    assert (
        render("safety.tyre_wear_bundle", tyres="앞 두 개", percent=88)
        == "앞 두 개 타이어 마모가 88퍼센트야."
    )
    assert (
        render(
            "safety.tyre_wear_bundle", tyres="뒤왼쪽·앞오른쪽", percent=88
        )
        == "뒤왼쪽·앞오른쪽 타이어 마모가 88퍼센트야."
    )
    assert (
        render("safety.tyre_temp_bundle", tyres="4개", celsius=121)
        == "4개 타이어 온도가 121도야. 관리해."
    )
    assert (
        render("safety.brake_temp_bundle", celsius=1050, tyres="네 바퀴")
        == "브레이크 온도가 1050도까지 올라갔어. 네 바퀴."
    )
    assert (
        render("safety.front_wing_both", percent=60)
        == "앞날개 양쪽 손상이 60퍼센트야."
    )


def test_render_unknown_key_raises() -> None:
    with pytest.raises(UnknownTemplate):
        render("coaching.nope", corner=1)


def test_render_missing_slot_raises() -> None:
    with pytest.raises(MissingSlot) as caught:
        render("coaching.brake_late", corner=3)

    assert "meters" in str(caught.value)


def test_render_ignores_extra_slots() -> None:
    assert (
        render("safety.safety_car", unrelated="값")
        == "세이프티 카 나왔어. 속도 줄여."
    )


def test_corner_phrases_cover_every_corner_number() -> None:
    phrases = corner_phrases([1, 2, 18], meters=5)

    assert phrases == [
        "1번에서 브레이크가 늦었어. 5미터 더 일찍 밟아.",
        "2번에서 브레이크가 늦었어. 5미터 더 일찍 밟아.",
        "18번에서 브레이크가 늦었어. 5미터 더 일찍 밟아.",
    ]
    assert len(set(phrases)) == 3


def test_prefetch_phrases_include_corners_and_fixed_warnings() -> None:
    phrases = prefetch_phrases([1, 2, 3])

    assert "2번에서 브레이크가 늦었어. 5미터 더 일찍 밟아." in phrases
    for key in PRELOAD_KEYS:
        assert render(key, **_sample_for(key)) in phrases
    assert len(phrases) == len(set(phrases)) == 3 + len(PRELOAD_KEYS)


def test_templates_by_category_splits_kinds() -> None:
    coaching = templates_by_category(RadioKind.COACHING)

    assert coaching
    assert all(template.category is RadioKind.COACHING for template in coaching)
    assert {template.key for template in coaching} >= {
        "coaching.brake_late",
        "coaching.fuel_lift",
    }
