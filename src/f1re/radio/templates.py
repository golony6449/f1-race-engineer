"""무전 문장 틀.

말투는 실제 F1 팀 무전처럼 짧고 건조한 명사형이다. 예:
"3번, 브레이크 늦음. 5미터." / "7번 탈출, 스로틀 늦음." / "박스, 박스."

- 문장은 전부 이 파일의 틀에서 만든다(`render`).
- 코칭·추월처럼 판단이 필요한 틀은 "어디서 + 무엇을 + 얼마나"를 모두 갖춘다
  (`needs_evidence=True`). 근거 수치를 넣을 슬롯이 없는 틀은 여기에 두지 않는다.
- 리프트 앤 코스트(연료 절약) 틀은 작업 15·16 전에 미리 넣어 둔다.
"""

from __future__ import annotations

import string
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from f1re.radio.queue import RadioKind


class TemplateError(Exception):
    """무전 틀을 만들 수 없을 때."""


class UnknownTemplate(TemplateError, KeyError):
    """등록되지 않은 틀 키."""


class MissingSlot(TemplateError, KeyError):
    """틀에 필요한 슬롯 값이 빠졌다."""


@dataclass(frozen=True)
class Template:
    """무전 틀 하나.

    `needs_evidence`가 True인 틀은 위치·동작·수치 슬롯을 모두 가져야 한다.
    """

    key: str
    category: RadioKind
    text: str
    needs_evidence: bool = False
    note: str = ""


_TEMPLATES: tuple[Template, ...] = (
    # --- 안전·위험 경고 (끌 수 없음) -------------------------------------
    Template(
        "safety.tyre_wear",
        RadioKind.SAFETY,
        "타이어 {tyre} 마모 {percent}퍼센트.",
    ),
    Template(
        "safety.tyre_temp",
        RadioKind.SAFETY,
        "타이어 {tyre} 온도 {celsius}도.",
    ),
    Template(
        "safety.brake_temp",
        RadioKind.SAFETY,
        "브레이크 {tyre} 온도 {celsius}도.",
    ),
    Template(
        "safety.fuel_short",
        RadioKind.SAFETY,
        "연료 마이너스 {laps}랩.",
    ),
    Template(
        "safety.damage",
        RadioKind.SAFETY,
        "{part} 손상 {percent}퍼센트.",
    ),
    Template(
        "safety.penalty",
        RadioKind.SAFETY,
        "페널티 {seconds}초.",
    ),
    Template("safety.safety_car", RadioKind.SAFETY, "세이프티 카."),
    Template("safety.vsc", RadioKind.SAFETY, "버추얼 세이프티 카."),
    Template("safety.red_flag", RadioKind.SAFETY, "적기. 적기."),
    # --- 박스 지시 --------------------------------------------------------
    Template("box.box_box", RadioKind.BOX, "박스, 박스."),
    Template("box.this_lap", RadioKind.BOX, "박스, 박스. 이번 랩."),
    Template("box.next_lap", RadioKind.BOX, "박스. 다음 랩."),
    # --- 코너 코칭 --------------------------------------------------------
    Template(
        "coaching.brake_late",
        RadioKind.COACHING,
        "{corner}번, 브레이크 늦음. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.brake_early",
        RadioKind.COACHING,
        "{corner}번, 브레이크 일찍. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.turn_in_early",
        RadioKind.COACHING,
        "{corner}번, 턴인 일찍. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.turn_in_late",
        RadioKind.COACHING,
        "{corner}번, 턴인 늦음. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.steer_corrections",
        RadioKind.COACHING,
        "{corner}번, 조향 수정 {count}회.",
        needs_evidence=True,
    ),
    Template(
        "coaching.late_throttle",
        RadioKind.COACHING,
        "{corner}번 탈출, 스로틀 늦음. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.line_wide",
        RadioKind.COACHING,
        "{corner}번, 라인 밖. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.off_track",
        RadioKind.COACHING,
        "{corner}번, 트랙 이탈. 라인 {meters}미터 밖.",
        needs_evidence=True,
    ),
    Template(
        "coaching.min_speed_low",
        RadioKind.COACHING,
        "{corner}번, 정점 속도 {kmh}킬로 낮음.",
        needs_evidence=True,
    ),
    # --- 코너 데이터가 없는 서킷: 공식 번호가 아니라 "N번째 코너"로 말한다 ---
    Template(
        "coaching.brake_late_ordinal",
        RadioKind.COACHING,
        "{corner_label}, 브레이크 늦음. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.brake_early_ordinal",
        RadioKind.COACHING,
        "{corner_label}, 브레이크 일찍. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.turn_in_early_ordinal",
        RadioKind.COACHING,
        "{corner_label}, 턴인 일찍. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.turn_in_late_ordinal",
        RadioKind.COACHING,
        "{corner_label}, 턴인 늦음. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.steer_corrections_ordinal",
        RadioKind.COACHING,
        "{corner_label}, 조향 수정 {count}회.",
        needs_evidence=True,
    ),
    Template(
        "coaching.late_throttle_ordinal",
        RadioKind.COACHING,
        "{corner_label} 탈출, 스로틀 늦음. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.line_wide_ordinal",
        RadioKind.COACHING,
        "{corner_label}, 라인 밖. {meters}미터.",
        needs_evidence=True,
    ),
    Template(
        "coaching.off_track_ordinal",
        RadioKind.COACHING,
        "{corner_label}, 트랙 이탈. 라인 {meters}미터 밖.",
        needs_evidence=True,
    ),
    Template(
        "coaching.min_speed_low_ordinal",
        RadioKind.COACHING,
        "{corner_label}, 정점 속도 {kmh}킬로 낮음.",
        needs_evidence=True,
    ),
    # 리프트 앤 코스트: 연료 부족 때 코너 몇 곳에서 액셀을 짧게 뗀다.
    Template(
        "coaching.fuel_lift",
        RadioKind.COACHING,
        "연료 마이너스 {laps}랩. {corners} 리프트. {meters}미터.",
        needs_evidence=True,
        note="리프트 앤 코스트 지시",
    ),
    Template(
        "coaching.fuel_lift_short",
        RadioKind.COACHING,
        "{corner}번 리프트 짧음. {meters}미터 더.",
        needs_evidence=True,
        note="리프트 앤 코스트: 리프트가 짧았다",
    ),
    # --- 추월 선택지 ------------------------------------------------------
    Template(
        "overtake.drs",
        RadioKind.OVERTAKE,
        "앞차 {gap}. 다음 직선 DRS.",
        needs_evidence=True,
    ),
    Template(
        "overtake.drs_ers",
        RadioKind.OVERTAKE,
        "앞차 {gap}. 다음 직선 DRS. ERS 전부.",
        needs_evidence=True,
    ),
    Template(
        "overtake.slow_exit",
        RadioKind.OVERTAKE,
        "앞차 {gap}. {corner}번 탈출 느림. {corner}번 붙고 직선에서.",
        needs_evidence=True,
    ),
    # 코너 데이터가 없는 서킷: 공식 번호 대신 "N번째 코너"로 말한다.
    Template(
        "overtake.slow_exit_ordinal",
        RadioKind.OVERTAKE,
        "앞차 {gap}. {corner_label} 탈출 느림. {corner_label} 붙고 직선에서.",
        needs_evidence=True,
    ),
    Template(
        "overtake.tyre_wait",
        RadioKind.OVERTAKE,
        "앞차 {gap}. 타이어 {laps}랩 새거. {wait_laps}랩 기다림.",
        needs_evidence=True,
    ),
    # --- 개선 알림 --------------------------------------------------------
    Template(
        "improvement.corner_improved",
        RadioKind.IMPROVEMENT,
        "{corner}번 좋아짐. 다음 {next_corner}번.",
    ),
    Template(
        "improvement.corner_improved_short",
        RadioKind.IMPROVEMENT,
        "{corner}번 좋아짐.",
    ),
    Template(
        "improvement.corner_improved_ordinal",
        RadioKind.IMPROVEMENT,
        "{corner_label} 좋아짐. 다음 {next_label}.",
    ),
    Template(
        "improvement.corner_improved_short_ordinal",
        RadioKind.IMPROVEMENT,
        "{corner_label} 좋아짐.",
    ),
    Template(
        "improvement.fuel_lift_good",
        RadioKind.IMPROVEMENT,
        "{corner}번 리프트 좋음.",
        note="리프트 앤 코스트: 리프트가 좋았다",
    ),
    Template(
        "improvement.fuel_lift_stop",
        RadioKind.IMPROVEMENT,
        "연료 회복. 리프트 그만.",
        note="리프트 앤 코스트: 그만해도 된다",
    ),
)

TEMPLATES: dict[str, Template] = {template.key: template for template in _TEMPLATES}

# `needs_evidence` 틀이 갖춰야 하는 슬롯 묶음(위치 / 수치).
WHERE_SLOTS = frozenset({"corner", "corner_label", "corners", "gap"})
AMOUNT_SLOTS = frozenset(
    {"meters", "count", "laps", "gap", "wait_laps", "kmh"}
)

# 공식 코너 틀 → 같은 뜻의 "N번째 코너" 틀. 코너 데이터가 없는 서킷에 쓴다.
ORDINAL_KEYS: dict[str, str] = {
    "coaching.brake_late": "coaching.brake_late_ordinal",
    "coaching.brake_early": "coaching.brake_early_ordinal",
    "coaching.turn_in_early": "coaching.turn_in_early_ordinal",
    "coaching.turn_in_late": "coaching.turn_in_late_ordinal",
    "coaching.steer_corrections": "coaching.steer_corrections_ordinal",
    "coaching.late_throttle": "coaching.late_throttle_ordinal",
    "coaching.line_wide": "coaching.line_wide_ordinal",
    "coaching.off_track": "coaching.off_track_ordinal",
    "coaching.min_speed_low": "coaching.min_speed_low_ordinal",
    "overtake.slow_exit": "overtake.slow_exit_ordinal",
    "improvement.corner_improved": "improvement.corner_improved_ordinal",
    "improvement.corner_improved_short": "improvement.corner_improved_short_ordinal",
}

# 세션 시작 때 미리 만들어 둘 고정 문장(코너 번호와 무관한 경고·개선).
PRELOAD_KEYS: tuple[str, ...] = (
    "safety.safety_car",
    "safety.vsc",
    "safety.red_flag",
    "box.box_box",
    "improvement.fuel_lift_stop",
)


def all_templates() -> tuple[Template, ...]:
    return _TEMPLATES


def templates_by_category(category: RadioKind | str) -> tuple[Template, ...]:
    resolved = RadioKind(category)
    return tuple(t for t in _TEMPLATES if t.category == resolved)


def get_template(key: str) -> Template:
    try:
        return TEMPLATES[key]
    except KeyError as exc:
        raise UnknownTemplate(f"모르는 무전 틀: {key}") from exc


def template_slots(key: str) -> tuple[str, ...]:
    """틀 문장에 들어 있는 슬롯 이름을 처음 나온 순서로 돌려준다."""
    template = get_template(key)
    names: list[str] = []
    for _, field_name, _, _ in string.Formatter().parse(template.text):
        if field_name and field_name not in names:
            names.append(field_name)
    return tuple(names)


def render(key: str, **slots: object) -> str:
    """틀 키와 슬롯 값으로 무전 문장을 만든다.

    필요한 슬롯이 빠지면 `MissingSlot`, 모르는 키면 `UnknownTemplate`을 올린다.
    남는 슬롯 값은 무시한다(뒤 작업이 근거 수치를 함께 넘겨도 된다).
    """
    template = get_template(key)
    missing = [name for name in template_slots(key) if name not in slots]
    if missing:
        raise MissingSlot(
            f"{key} 틀에 필요한 값이 없습니다: {', '.join(missing)}"
        )
    return template.text.format(**slots)


def corner_label(number: int, *, official: bool = True) -> str:
    """무전에 넣는 코너 이름. 공식 번호면 "3번", 자동 탐지면 "3번째 코너"."""
    return f"{int(number)}번" if official else f"{int(number)}번째 코너"


def render_corner(
    key: str, corner: int, *, official: bool = True, **slots: object
) -> str:
    """코너 번호가 들어가는 틀을 공식/비공식에 맞춰 만든다.

    `official=False`(데이터 파일이 없는 서킷의 자동 탐지 코너)면 같은 뜻의
    "N번째 코너" 틀을 골라 쓴다. `slots`에는 틀에 필요한 나머지 값
    (meters·count·kmh 등)을 넘긴다.
    """
    if official:
        return render(key, corner=int(corner), **slots)
    try:
        ordinal = ORDINAL_KEYS[key]
    except KeyError as exc:
        raise UnknownTemplate(f"{key} 틀에는 비공식 코너 문장이 없습니다") from exc
    return render(
        ordinal, corner_label=corner_label(corner, official=False), **slots
    )


def corner_phrases(
    corners: Iterable[int], *, meters: int = 5
) -> list[str]:
    """서킷 코너 번호가 들어가는 대표 코칭 문장을 코너마다 만든다.

    세션 시작 때 `VoiceCache.prefetch()`에 넘겨, 가장 자주 나갈 코너 무전을
    미리 합성해 둔다(설계 6.8 "미리 만들기").
    """
    return [
        render("coaching.brake_late", corner=int(number), meters=meters)
        for number in corners
    ]


def prefetch_phrases(corners: Sequence[int], *, meters: int = 5) -> list[str]:
    """세션 시작 때 미리 만들 문장 전체: 코너 번호 문장 + 고정 경고·개선."""
    phrases = corner_phrases(corners, meters=meters)
    phrases.extend(render(key) for key in PRELOAD_KEYS)
    return list(dict.fromkeys(phrases))
