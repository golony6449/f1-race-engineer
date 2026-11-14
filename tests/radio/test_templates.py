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
    Mood,
    TemplateError,
    UnknownTemplate,
    VariantRotator,
    all_phrases,
    all_templates,
    corner_phrases,
    is_last_laps,
    mood_for,
    prefetch_phrases,
    render,
    template_slots,
    template_slots_for,
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

    expected: list[str] = []
    for number in (1, 2, 18):
        expected.extend(
            all_phrases("coaching.brake_late", corner=number, meters=5)
        )
    # 모든 감정 단계·표현을 코너마다 담는다(SCRUM-91).
    assert phrases == list(dict.fromkeys(expected))
    assert render("coaching.brake_late", corner=1, meters=5) in phrases
    assert (
        render(
            "coaching.brake_late",
            mood=Mood.HYPED,
            variant=0,
            corner=1,
            meters=5,
        )
        in phrases
    )


def test_prefetch_phrases_include_corners_and_fixed_warnings() -> None:
    phrases = prefetch_phrases([1, 2, 3])

    expected: list[str] = []
    for number in (1, 2, 3):
        expected.extend(
            all_phrases("coaching.brake_late", corner=number, meters=5)
        )
    for key in PRELOAD_KEYS:
        expected.extend(all_phrases(key, **_sample_for(key)))
    assert phrases == list(dict.fromkeys(expected))
    assert len(phrases) == len(set(phrases))


def test_templates_by_category_splits_kinds() -> None:
    coaching = templates_by_category(RadioKind.COACHING)

    assert coaching
    assert all(template.category is RadioKind.COACHING for template in coaching)
    assert {template.key for template in coaching} >= {
        "coaching.brake_late",
        "coaching.fuel_lift",
    }


# ---------------------------------------------------------------------------
# 감정 단계와 표현 돌려 쓰기 (SCRUM-91)


def test_every_mood_expression_uses_the_same_slots() -> None:
    """한 틀의 모든 단계·표현은 같은 슬롯 집합을 쓴다."""
    for template in all_templates():
        base = set(template_slots(template.key))
        for mood, texts in template.moods.items():
            for variant in range(len(texts)):
                slots = set(
                    template_slots_for(template.key, mood, variant)
                )
                assert slots == base, (template.key, mood, variant)
            # 각 단계는 같은 뜻의 표현 2~3개를 둔다.
            assert 2 <= len(texts) <= 3, (template.key, mood)


def test_all_mood_expressions_render_short_text() -> None:
    """어떤 단계·표현이 나가도 무전은 짧아야 한다."""
    for template in all_templates():
        for mood, texts in template.moods.items():
            for variant in range(len(texts)):
                text = render(
                    template.key,
                    mood=mood,
                    variant=variant,
                    **_sample_for(template.key),
                )
                assert text.strip()
                assert len(text) <= 80, (template.key, mood, variant, text)


def test_legacy_render_keeps_calm_first_expression() -> None:
    """단계를 생략한 기존 호출은 calm 첫 표현 그대로다."""
    assert render("box.box_box") == "박스, 박스. 들어와."
    assert render("coaching.brake_late", corner=3, meters=5) == (
        "3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아."
    )
    # 단계를 줘도 표현 번호를 생략하면 그 단계의 첫 표현이다.
    assert (
        render("box.box_box", mood=Mood.URGENT)
        == "박스, 박스! 들어와!"
    )


def test_mood_selection_rules() -> None:
    """안전→urgent, 마지막 3랩·0.5초 이내 추월→hyped, 개선→warm."""
    assert mood_for(urgent=True) is Mood.URGENT
    assert mood_for(urgent=True, last_laps=True) is Mood.URGENT
    assert mood_for(last_laps=True) is Mood.HYPED
    assert mood_for(last_laps=True, praise=True) is Mood.HYPED
    assert mood_for(close_battle=True) is Mood.HYPED
    assert mood_for(praise=True) is Mood.WARM
    assert mood_for() is Mood.CALM

    assert is_last_laps(3) is True
    assert is_last_laps(2.9) is True
    assert is_last_laps(4) is False
    assert is_last_laps(None) is False


def test_rotator_avoids_previous_expression() -> None:
    """같은 틀·같은 단계가 연달아 나가면 직전 표현을 피한다(결정적)."""
    rotator = VariantRotator()
    first = rotator.render("box.box_box", mood=Mood.URGENT)
    second = rotator.render("box.box_box", mood=Mood.URGENT)
    third = rotator.render("box.box_box", mood=Mood.URGENT)

    choices = set(all_phrases("box.box_box"))
    assert first in choices and second in choices and third in choices
    assert first != second
    assert third != second
    assert third == first  # 표현이 둘이라 세 번째는 첫 표현으로 돌아온다.

    # 다른 단계는 따로 센다.
    calm_first = rotator.render("box.box_box", mood=Mood.CALM)
    assert calm_first == "박스, 박스. 들어와."


def test_render_rejects_unknown_mood_and_variant() -> None:
    with pytest.raises(TemplateError):
        render("box.box_box", mood="angry")
    with pytest.raises(TemplateError):
        render("box.box_box", mood=Mood.URGENT, variant=5)
