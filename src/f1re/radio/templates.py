"""무전 문장 틀.

말투는 짧은 반말 문장이다. 무엇이 어땠는지 말하고 어떻게 하라고 이어 준다
(사용자 결정 2026-11-14). 예:
"3번에서 브레이크가 늦었어. 5미터 더 일찍 밟아." / "박스, 박스. 들어와."

감정 단계(SCRUM-91, 사용자 요청 2026-11-14)
------------------------------------------
같은 뜻의 문장을 상황에 따라 다르게 말한다. TTS 모델에는 감정 지시를 넣을 수
없으므로(말투 지시형 모델은 국제 엔드포인트에서 쓸 수 없다), 문장 표현만으로
감정을 낸다.

- ``calm``(보통): 담담하게 알려 준다. 코너 코칭의 기본.
- ``urgent``(급박): 짧게 끊고 반복하며 느낌표를 쓴다. 안전 경고·박스 지시.
- ``hyped``(격앙·흥분): 에너지를 실어 응원한다. 레이스 마지막 3랩, 0.5초
  이내 추월, 그 밖에 흥분할 상황.
- ``warm``(칭찬·밝게): 물결표로 말꼬리를 늘인다. 개선 알림·리프트 좋음.

단계가 없는 틀은 ``calm``으로 대체한다. 각 단계의 표현은 2~3개이고, 한 틀의
모든 표현은 같은 슬롯 집합을 쓴다(검증 테스트). ``render(key)``처럼 단계를
생략하면 ``calm``의 첫 표현을 쓴다(기존 호출 호환).

표현 돌려 쓰기
--------------
``VariantRotator``가 틀·단계별로 마지막 표현 번호를 기억해 다음 번호를 고른다.
무작위가 아니라 결정적이며, 같은 틀·같은 단계가 연달아 나가면 직전 표현을
피한다. 미리 합성 목록은 ``all_phrases()``로 모든 단계·표현을 만든다.
"""

from __future__ import annotations

import math
import string
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from f1re.radio.queue import RadioKind


class TemplateError(Exception):
    """무전 틀을 만들 수 없을 때."""


class UnknownTemplate(TemplateError, KeyError):
    """등록되지 않은 틀 키."""


class MissingSlot(TemplateError, KeyError):
    """틀에 필요한 슬롯 값이 빠졌다."""


class Mood(StrEnum):
    """무전 감정 단계. 값은 evidence·설정에 그대로 쓴다."""

    CALM = "calm"
    URGENT = "urgent"
    HYPED = "hyped"
    WARM = "warm"


# 레이스 마지막 몇 랩부터 격앙된 말투를 쓰는가.
LAST_LAPS = 3
# 추월 무전에서 이 간격(초) 이하면 격앙된 말투를 쓴다.
CLOSE_OVERTAKE_GAP_S = 0.5


def mood_for(
    *,
    urgent: bool = False,
    last_laps: bool = False,
    close_battle: bool = False,
    praise: bool = False,
) -> Mood:
    """상황 깃발로 감정 단계를 고른다. 위에서부터 우선한다.

    안전·박스(urgent) > 마지막 3랩·0.5초 이내 추월(hyped) > 칭찬(warm) > calm.
    """
    if urgent:
        return Mood.URGENT
    if last_laps or close_battle:
        return Mood.HYPED
    if praise:
        return Mood.WARM
    return Mood.CALM


def is_last_laps(remaining_laps: float | None, *, threshold: int = LAST_LAPS) -> bool:
    """남은 랩이 `threshold` 이하인가(레이스 여부는 호출자가 판단한다)."""
    if remaining_laps is None:
        return False
    try:
        value = float(remaining_laps)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value <= threshold


def _slots_in_text(text: str) -> tuple[str, ...]:
    """문장에 들어 있는 슬롯 이름을 처음 나온 순서로 돌려준다."""
    names: list[str] = []
    for _, field_name, _, _ in string.Formatter().parse(text):
        if field_name and field_name not in names:
            names.append(field_name)
    return tuple(names)


def _moods(
    *,
    calm: Sequence[str],
    urgent: Sequence[str] = (),
    hyped: Sequence[str] = (),
    warm: Sequence[str] = (),
) -> dict[str, tuple[str, ...]]:
    """틀 정의용. 값이 있는 단계만 담는다(없으면 calm으로 대체된다)."""
    expressions: dict[str, tuple[str, ...]] = {
        Mood.CALM.value: tuple(calm)
    }
    if urgent:
        expressions[Mood.URGENT.value] = tuple(urgent)
    if hyped:
        expressions[Mood.HYPED.value] = tuple(hyped)
    if warm:
        expressions[Mood.WARM.value] = tuple(warm)
    return expressions


@dataclass(frozen=True)
class Template:
    """무전 틀 하나.

    `needs_evidence`가 True인 틀은 위치·동작·수치 슬롯을 모두 가져야 한다.
    `moods`는 감정 단계 값 → 표현 목록이다. 모든 표현은 같은 슬롯 집합을 쓴다.
    """

    key: str
    category: RadioKind
    moods: Mapping[str, tuple[str, ...]]
    needs_evidence: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        calm = self.moods.get(Mood.CALM.value)
        if not calm:
            raise TemplateError(f"{self.key}: calm 표현이 없습니다")
        base_slots = set(_slots_in_text(calm[0]))
        for mood, texts in self.moods.items():
            try:
                Mood(mood)
            except ValueError as exc:
                raise TemplateError(
                    f"{self.key}: 모르는 감정 단계 {mood}"
                ) from exc
            if not texts:
                raise TemplateError(f"{self.key}: {mood} 표현이 비었습니다")
            for text in texts:
                slots = set(_slots_in_text(text))
                if slots != base_slots:
                    raise TemplateError(
                        f"{self.key}: {mood} 표현의 슬롯이 다릅니다"
                        f"({sorted(base_slots)} != {sorted(slots)}): {text}"
                    )

    @property
    def text(self) -> str:
        """기존 호출 호환용 calm 첫 표현."""
        return self.moods[Mood.CALM.value][0]

    def texts_for(self, mood: Mood | str) -> tuple[str, ...]:
        """그 단계의 표현 목록. 단계가 없으면 calm으로 대체한다."""
        resolved = _resolve_mood(mood)
        return self.moods.get(resolved.value) or self.moods[Mood.CALM.value]


def _resolve_mood(mood: Mood | str) -> Mood:
    if isinstance(mood, Mood):
        return mood
    try:
        return Mood(mood)
    except ValueError as exc:
        raise TemplateError(f"모르는 감정 단계: {mood}") from exc


_TEMPLATES: tuple[Template, ...] = (
    # --- 안전·위험 경고 (끌 수 없음) -------------------------------------
    Template(
        "safety.tyre_wear",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "{tyre} 타이어 마모가 {percent}퍼센트야.",
                "{tyre} 타이어가 {percent}퍼센트나 닳았어.",
            ),
            urgent=(
                "{tyre} 타이어 마모 {percent}퍼센트! 관리해야 돼!",
                "{tyre} 타이어 {percent}퍼센트야! 닳았어, 조심해!",
            ),
        ),
    ),
    # 같은 지표의 바퀴들이 한 평가 주기에 함께 위험해지면 한 무전으로 묶는다
    # (SCRUM-81). 값은 위험한 바퀴 중 가장 나쁜 값이다.
    Template(
        "safety.tyre_wear_bundle",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "{tyres} 타이어 마모가 {percent}퍼센트야.",
                "{tyres} 타이어가 {percent}퍼센트나 닳았어.",
            ),
            urgent=(
                "{tyres} 타이어 마모 {percent}퍼센트! 관리해!",
                "{tyres} 타이어 {percent}퍼센트야! 조심해!",
            ),
        ),
        note="바퀴 여러 개가 함께 위험할 때(값은 가장 나쁜 값)",
    ),
    Template(
        "safety.tyre_temp",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "{tyre} 타이어 온도가 {celsius}도야. 관리해.",
                "{tyre} 타이어가 {celsius}도까지 올라갔어.",
            ),
            urgent=(
                "{tyre} 타이어 {celsius}도! 뜨거워, 관리해!",
                "{tyre} 타이어 {celsius}도야! 식혀!",
            ),
        ),
    ),
    Template(
        "safety.tyre_temp_bundle",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "{tyres} 타이어 온도가 {celsius}도야. 관리해.",
                "{tyres} 타이어가 {celsius}도까지 올라갔어.",
            ),
            urgent=(
                "{tyres} 타이어 {celsius}도! 뜨거워, 관리해!",
                "{tyres} 타이어 {celsius}도야! 식혀!",
            ),
        ),
        note="바퀴 여러 개가 함께 위험할 때(값은 가장 나쁜 값)",
    ),
    Template(
        "safety.brake_temp",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "{tyre} 브레이크 온도가 {celsius}도까지 올라갔어.",
                "{tyre} 브레이크가 {celsius}도야.",
            ),
            urgent=(
                "{tyre} 브레이크 {celsius}도야! 너무 뜨거워, 식혀!",
                "{tyre} 브레이크 {celsius}도! 뜨거워, 식혀!",
            ),
        ),
    ),
    Template(
        "safety.brake_temp_bundle",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "브레이크 온도가 {celsius}도까지 올라갔어. {tyres}.",
                "브레이크가 {celsius}도야. {tyres}.",
            ),
            urgent=(
                "브레이크 {celsius}도! 너무 뜨거워, 식혀! {tyres}!",
                "브레이크 {celsius}도야! 식혀! {tyres}!",
            ),
        ),
        note="바퀴 여러 개가 함께 위험할 때(값은 가장 나쁜 값)",
    ),
    Template(
        "safety.fuel_short",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "연료가 {laps}랩 모자라.",
                "{laps}랩 치 연료가 없어.",
            ),
            urgent=(
                "연료가 {laps}랩 모자라! 아껴야 돼!",
                "{laps}랩 치 연료가 없어! 아껴!",
            ),
        ),
    ),
    Template(
        "safety.damage",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "{part} 손상이 {percent}퍼센트야.",
                "{part} {percent}퍼센트 손상이야.",
            ),
            urgent=(
                "{part} 손상 {percent}퍼센트! 조심해!",
                "{part} {percent}퍼센트야! 손상됐어!",
            ),
        ),
    ),
    # 앞날개는 좌우가 함께 위험해질 때 한 무전으로 묶는다(SCRUM-81).
    Template(
        "safety.front_wing_both",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "앞날개 양쪽 손상이 {percent}퍼센트야.",
                "앞날개 양쪽 다 {percent}퍼센트 손상이야.",
            ),
            urgent=(
                "앞날개 양쪽 손상 {percent}퍼센트! 조심해!",
                "앞날개 양쪽 다 {percent}퍼센트야! 손상됐어!",
            ),
        ),
        note="앞날개 좌우가 함께 위험할 때(값은 가장 나쁜 값)",
    ),
    Template(
        "safety.penalty",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "{seconds}초 페널티 받았어.",
                "{seconds}초 페널티야.",
            ),
            urgent=(
                "{seconds}초 페널티! 받았어!",
                "페널티 {seconds}초! 받았어!",
            ),
        ),
    ),
    Template(
        "safety.safety_car",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "세이프티 카 나왔어. 속도 줄여.",
                "세이프티 카야. 속도 줄여.",
            ),
            urgent=(
                "세이프티 카! 세이프티 카 나왔어! 속도 줄여, 당장!",
                "세이프티 카야! 속도 줄여! 당장!",
            ),
        ),
    ),
    Template(
        "safety.vsc",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "버추얼 세이프티 카야. 델타 지켜.",
                "버추얼 세이프티 카야. 속도 줄여.",
            ),
            urgent=(
                "버추얼 세이프티 카! 델타 지켜! 당장!",
                "VSC! VSC! 델타 지켜!",
            ),
        ),
    ),
    Template(
        "safety.red_flag",
        RadioKind.SAFETY,
        _moods(
            calm=(
                "적기야, 적기. 속도 줄이고 피트로 와.",
                "적기야. 속도 줄이고 피트로 와.",
            ),
            urgent=(
                "적기! 적기! 속도 줄이고 피트로 와!",
                "레드 플래그! 적기야! 피트로 와, 당장!",
            ),
        ),
    ),
    # --- 박스 지시 --------------------------------------------------------
    Template(
        "box.box_box",
        RadioKind.BOX,
        _moods(
            calm=(
                "박스, 박스. 들어와.",
                "박스야. 들어와.",
            ),
            urgent=(
                "박스, 박스! 들어와!",
                "박스! 박스! 지금 들어와!",
            ),
        ),
    ),
    Template(
        "box.this_lap",
        RadioKind.BOX,
        _moods(
            calm=(
                "박스, 박스. 이번 랩에 들어와.",
                "이번 랩에 박스야. 들어와.",
            ),
            urgent=(
                "박스, 박스! 이번 랩에 들어와!",
                "박스! 이번 랩이야! 들어와!",
            ),
        ),
    ),
    Template(
        "box.next_lap",
        RadioKind.BOX,
        _moods(
            calm=(
                "다음 랩에 박스야.",
                "박스는 다음 랩이야.",
            ),
            urgent=(
                "박스! 다음 랩이야! 준비해!",
                "다음 랩 박스! 준비해!",
            ),
        ),
    ),
    # --- 코너 코칭 --------------------------------------------------------
    Template(
        "coaching.brake_late",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번에서 브레이크가 늦었어. {meters}미터 더 일찍 밟아.",
                "{corner}번 브레이크 {meters}미터 늦었어. 더 일찍 밟자.",
            ),
            hyped=(
                "{corner}번 브레이크 늦었어! {meters}미터 당겨! 밀어붙여!",
                "{corner}번 늦었어! {meters}미터 더 일찍! 간다!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.brake_early",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번에서 브레이크를 너무 일찍 밟았어. {meters}미터 더 참아도 돼.",
                "{corner}번 브레이크가 일렀어. {meters}미터 더 참아.",
            ),
            hyped=(
                "{corner}번 일찍 밟았어! {meters}미터 참아! 더 갈 수 있어!",
                "{corner}번 브레이크 일러! {meters}미터 버텨! 밀어!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.turn_in_early",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번에서 턴인이 빨랐어. {meters}미터 더 기다렸다 꺾어.",
                "{corner}번 턴인이 빨랐어. {meters}미터 더 기다려.",
            ),
            hyped=(
                "{corner}번 너무 일찍 꺾었어! {meters}미터 기다려! 붙어!",
                "{corner}번 턴인 일러! {meters}미터 참아! 간다!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.turn_in_late",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번에서 턴인이 늦었어. {meters}미터 더 일찍 꺾어.",
                "{corner}번 턴인 늦었어. {meters}미터 당겨서 꺾어.",
            ),
            hyped=(
                "{corner}번 늦게 꺾었어! {meters}미터 당겨! 바로!",
                "{corner}번 턴인 늦어! {meters}미터 일찍! 밀어!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.steer_corrections",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번에서 핸들을 {count}번 고쳐 잡았어. 한 번에 부드럽게 꺾어.",
                "{corner}번에서 핸들을 {count}번이나 고쳤어. 부드럽게 한 번에.",
            ),
            hyped=(
                "{corner}번 핸들 {count}번! 부드럽게! 한 번에!",
                "{corner}번에서 {count}번 고쳤어! 매끄럽게 가!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.late_throttle",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번 탈출에서 가속이 늦었어. {meters}미터 더 일찍 밟아.",
                "{corner}번 나오면서 가속이 늦었어. {meters}미터 더 일찍 밟아.",
            ),
            hyped=(
                "{corner}번 탈출 늦었어! {meters}미터 일찍 밟아! 밀어!",
                "{corner}번 가속 늦어! {meters}미터 당겨! 간다!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.line_wide",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번에서 라인이 {meters}미터 바깥으로 밀렸어.",
                "{corner}번 라인이 {meters}미터 벗어났어.",
            ),
            hyped=(
                "{corner}번 라인 {meters}미터 밀렸어! 안으로 붙어!",
                "{corner}번 {meters}미터 밖이야! 당겨!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.off_track",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번에서 트랙 벗어났어. 라인이 {meters}미터 밖이었어.",
                "{corner}번 트랙 밖이었어. 라인이 {meters}미터 나갔어.",
            ),
            hyped=(
                "{corner}번 트랙 밖! {meters}미터야! 안으로 붙어!",
                "{corner}번 나갔어! {meters}미터! 라인 당겨!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.min_speed_low",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번 코너 속도가 기준보다 {kmh}킬로 느려. 더 빠르게 돌 수 있어.",
                "{corner}번 코너 속도가 {kmh}킬로 느려. 더 밀어붙일 수 있어.",
            ),
            hyped=(
                "{corner}번 {kmh}킬로 느려! 더 밀어! 갈 수 있어!",
                "{corner}번 속도 {kmh}킬로 부족! 밀어붙여!",
            ),
        ),
        needs_evidence=True,
    ),
    # --- 코너 데이터가 없는 서킷: 공식 번호가 아니라 "N번째 코너"로 말한다 ---
    Template(
        "coaching.brake_late_ordinal",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner_label}에서 브레이크가 늦었어. {meters}미터 더 일찍 밟아.",
                "{corner_label} 브레이크 {meters}미터 늦었어. 더 일찍 밟자.",
            ),
            hyped=(
                "{corner_label} 브레이크 늦었어! {meters}미터 당겨! 밀어붙여!",
                "{corner_label} 늦었어! {meters}미터 더 일찍! 간다!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.brake_early_ordinal",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner_label}에서 브레이크를 너무 일찍 밟았어. {meters}미터 더 참아도 돼.",
                "{corner_label} 브레이크가 일렀어. {meters}미터 더 참아.",
            ),
            hyped=(
                "{corner_label} 일찍 밟았어! {meters}미터 참아! 더 갈 수 있어!",
                "{corner_label} 브레이크 일러! {meters}미터 버텨! 밀어!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.turn_in_early_ordinal",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner_label}에서 턴인이 빨랐어. {meters}미터 더 기다렸다 꺾어.",
                "{corner_label} 턴인이 빨랐어. {meters}미터 더 기다려.",
            ),
            hyped=(
                "{corner_label} 너무 일찍 꺾었어! {meters}미터 기다려! 붙어!",
                "{corner_label} 턴인 일러! {meters}미터 참아! 간다!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.turn_in_late_ordinal",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner_label}에서 턴인이 늦었어. {meters}미터 더 일찍 꺾어.",
                "{corner_label} 턴인 늦었어. {meters}미터 당겨서 꺾어.",
            ),
            hyped=(
                "{corner_label} 늦게 꺾었어! {meters}미터 당겨! 바로!",
                "{corner_label} 턴인 늦어! {meters}미터 일찍! 밀어!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.steer_corrections_ordinal",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner_label}에서 핸들을 {count}번 고쳐 잡았어. 한 번에 부드럽게 꺾어.",
                "{corner_label}에서 핸들을 {count}번이나 고쳤어. 부드럽게 한 번에.",
            ),
            hyped=(
                "{corner_label} 핸들 {count}번! 부드럽게! 한 번에!",
                "{corner_label}에서 {count}번 고쳤어! 매끄럽게 가!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.late_throttle_ordinal",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner_label} 탈출에서 가속이 늦었어. {meters}미터 더 일찍 밟아.",
                "{corner_label} 나오면서 가속이 늦었어. {meters}미터 더 일찍 밟아.",
            ),
            hyped=(
                "{corner_label} 탈출 늦었어! {meters}미터 일찍 밟아! 밀어!",
                "{corner_label} 가속 늦어! {meters}미터 당겨! 간다!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.line_wide_ordinal",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner_label}에서 라인이 {meters}미터 바깥으로 밀렸어.",
                "{corner_label} 라인이 {meters}미터 벗어났어.",
            ),
            hyped=(
                "{corner_label} 라인 {meters}미터 밀렸어! 안으로 붙어!",
                "{corner_label} {meters}미터 밖이야! 당겨!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.off_track_ordinal",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner_label}에서 트랙 벗어났어. 라인이 {meters}미터 밖이었어.",
                "{corner_label} 트랙 밖이었어. 라인이 {meters}미터 나갔어.",
            ),
            hyped=(
                "{corner_label} 트랙 밖! {meters}미터야! 안으로 붙어!",
                "{corner_label} 나갔어! {meters}미터! 라인 당겨!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "coaching.min_speed_low_ordinal",
        RadioKind.COACHING,
        # `corner_label`이 이미 "3번째 코너"이므로 "코너"를 두 번 말하지 않는다.
        _moods(
            calm=(
                "{corner_label} 속도가 기준보다 {kmh}킬로 느려. 더 빠르게 돌 수 있어.",
                "{corner_label} 속도가 {kmh}킬로 느려. 더 밀어붙일 수 있어.",
            ),
            hyped=(
                "{corner_label} {kmh}킬로 느려! 더 밀어! 갈 수 있어!",
                "{corner_label} 속도 {kmh}킬로 부족! 밀어붙여!",
            ),
        ),
        needs_evidence=True,
    ),
    # 리프트 앤 코스트: 연료 부족 때 코너 몇 곳에서 액셀을 짧게 뗀다.
    Template(
        "coaching.fuel_lift",
        RadioKind.COACHING,
        _moods(
            calm=(
                "연료가 {laps}랩 모자라. {corners}에서 {meters}미터 일찍 발 떼.",
                "연료가 {laps}랩 부족해. {corners}에서 {meters}미터 일찍 떼.",
            ),
            hyped=(
                "연료 {laps}랩 모자라! {corners}에서 {meters}미터 일찍 떼! 가자!",
                "{laps}랩 부족해! {corners}, {meters}미터 리프트! 지금부터!",
            ),
        ),
        needs_evidence=True,
        note="리프트 앤 코스트 지시",
    ),
    Template(
        "coaching.fuel_lift_short",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner}번 리프트가 짧았어. {meters}미터 더 일찍 떼.",
                "{corner}번 리프트가 모자랐어. {meters}미터 더 일찍 떼.",
            ),
            hyped=(
                "{corner}번 리프트 짧아! {meters}미터 더 일찍! 당겨!",
                "{corner}번 부족해! {meters}미터 더! 지금!",
            ),
        ),
        needs_evidence=True,
        note="리프트 앤 코스트: 리프트가 짧았다",
    ),
    # 브레이크·타이어 온도 때문에 리프트 앤 코스트를 지시할 때. 연료 부족이
    # 아니므로 연료 문장을 쓰지 않는다(SCRUM-71).
    Template(
        "coaching.brake_temp_lift",
        RadioKind.COACHING,
        _moods(
            calm=(
                "브레이크가 {celsius}도야. {corners}에서 {meters}미터 일찍 발 떼서 식혀.",
                "브레이크가 {celsius}도야. {corners}에서 {meters}미터 일찍 떼서 식혀.",
            ),
            hyped=(
                "브레이크 {celsius}도! {corners}에서 {meters}미터 일찍 떼! 식혀!",
                "{celsius}도야! {corners}, {meters}미터 리프트! 뜨거워!",
            ),
        ),
        needs_evidence=True,
        note="리프트 앤 코스트: 브레이크 냉각",
    ),
    Template(
        "coaching.tyre_temp_lift",
        RadioKind.COACHING,
        _moods(
            calm=(
                "앞 타이어가 {celsius}도야. {corners}에서 {meters}미터 일찍 발 떼서 식혀.",
                "앞 타이어가 {celsius}도야. {corners}에서 {meters}미터 일찍 떼서 식혀.",
            ),
            hyped=(
                "앞 타이어 {celsius}도! {corners}에서 {meters}미터 일찍 떼! 식혀!",
                "{celsius}도야! {corners}, {meters}미터 리프트! 뜨거워!",
            ),
        ),
        needs_evidence=True,
        note="리프트 앤 코스트: 앞 타이어 냉각",
    ),
    # --- 추월 선택지 ------------------------------------------------------
    Template(
        "overtake.drs",
        RadioKind.OVERTAKE,
        _moods(
            calm=(
                "앞차랑 {gap}초야. 다음 직선에서 DRS 써.",
                "앞차랑 {gap}초야. 직선에서 DRS 써.",
            ),
            hyped=(
                "앞차랑 {gap}초밖에 안 돼! 다음 직선에서 DRS! 가자!",
                "붙었어! {gap}초야! DRS 써! 넘어!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "overtake.drs_ers",
        RadioKind.OVERTAKE,
        _moods(
            calm=(
                "앞차랑 {gap}초야. 다음 직선에서 DRS랑 ERS 다 써.",
                "앞차랑 {gap}초야. 직선에서 DRS랑 ERS 다 써.",
            ),
            hyped=(
                "앞차랑 {gap}초밖에 안 돼! 다음 직선에서 DRS랑 ERS 전부 다 써! 가자!!",
                "붙었어! {gap}초야! DRS랑 ERS 다! 넘어!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "overtake.slow_exit",
        RadioKind.OVERTAKE,
        _moods(
            calm=(
                "앞차랑 {gap}초야. 앞차가 {corner}번 탈출이 느려. "
                "{corner}번에서 바짝 붙어서 직선에서 넘어.",
                "앞차랑 {gap}초야. 앞차 {corner}번 탈출이 느려. "
                "{corner}번에서 붙어서 직선에서 넘어.",
            ),
            hyped=(
                "앞차랑 {gap}초야! 앞차 {corner}번 탈출 느려! "
                "{corner}번에서 붙어! 직선에서 넘어!",
                "붙었어! {gap}초! {corner}번 탈출 노려! 바짝 붙어서 넘어!",
            ),
        ),
        needs_evidence=True,
    ),
    # 코너 데이터가 없는 서킷: 공식 번호 대신 "N번째 코너"로 말한다.
    Template(
        "overtake.slow_exit_ordinal",
        RadioKind.OVERTAKE,
        _moods(
            calm=(
                "앞차랑 {gap}초야. 앞차가 {corner_label} 탈출이 느려. "
                "{corner_label}에서 바짝 붙어서 직선에서 넘어.",
                "앞차랑 {gap}초야. 앞차 {corner_label} 탈출이 느려. "
                "{corner_label}에서 붙어서 직선에서 넘어.",
            ),
            hyped=(
                "앞차랑 {gap}초야! 앞차 {corner_label} 탈출 느려! "
                "{corner_label}에서 붙어! 직선에서 넘어!",
                "붙었어! {gap}초! {corner_label} 탈출 노려! 바짝 붙어서 넘어!",
            ),
        ),
        needs_evidence=True,
    ),
    Template(
        "overtake.tyre_wait",
        RadioKind.OVERTAKE,
        _moods(
            calm=(
                "앞차랑 {gap}초야. 우리 타이어가 {laps}랩 더 새거야. "
                "{wait_laps}랩만 기다렸다가 공격해.",
                "앞차랑 {gap}초야. 우리 타이어가 {laps}랩 더 새거야. "
                "{wait_laps}랩 기다렸다 공격해.",
            ),
            hyped=(
                "앞차랑 {gap}초야! 타이어가 {laps}랩 더 새거야! "
                "{wait_laps}랩만 참고 공격하자!",
                "{gap}초! 타이어 {laps}랩 우위야! {wait_laps}랩 뒤에 간다!",
            ),
        ),
        needs_evidence=True,
    ),
    # --- 개선 알림 --------------------------------------------------------
    Template(
        "improvement.corner_improved",
        RadioKind.IMPROVEMENT,
        _moods(
            calm=(
                "{corner}번 좋아졌어. 다음은 {next_corner}번 보자.",
                "{corner}번 좋아. 다음은 {next_corner}번이야.",
            ),
            hyped=(
                "{corner}번 좋아졌어! 그대로야! 다음은 {next_corner}번! 가자!",
                "좋아! {corner}번 완벽해! 이제 {next_corner}번 간다!",
            ),
            warm=(
                "오~ {corner}번 좋아졌어~ 그렇지~ 다음은 {next_corner}번 보자.",
                "좋아~ {corner}번 훨씬 나아졌어~ 다음은 {next_corner}번이야~",
            ),
        ),
    ),
    Template(
        "improvement.corner_improved_short",
        RadioKind.IMPROVEMENT,
        _moods(
            calm=(
                "{corner}번 좋아졌어.",
                "{corner}번 좋아.",
            ),
            hyped=(
                "{corner}번 좋아졌어! 이대로!",
                "좋아! {corner}번 완벽해!",
            ),
            warm=(
                "오~ {corner}번 좋아졌어~",
                "좋아~ {corner}번 딱 좋아~",
            ),
        ),
    ),
    Template(
        "improvement.corner_improved_ordinal",
        RadioKind.IMPROVEMENT,
        _moods(
            calm=(
                "{corner_label} 좋아졌어. 다음은 {next_label} 보자.",
                "{corner_label} 좋아. 다음은 {next_label}이야.",
            ),
            hyped=(
                "{corner_label} 좋아졌어! 그대로야! 다음은 {next_label}! 가자!",
                "좋아! {corner_label} 완벽해! 이제 {next_label} 간다!",
            ),
            warm=(
                "오~ {corner_label} 좋아졌어~ 그렇지~ 다음은 {next_label} 보자.",
                "좋아~ {corner_label} 훨씬 나아졌어~ 다음은 {next_label}이야~",
            ),
        ),
    ),
    Template(
        "improvement.corner_improved_short_ordinal",
        RadioKind.IMPROVEMENT,
        _moods(
            calm=(
                "{corner_label} 좋아졌어.",
                "{corner_label} 좋아.",
            ),
            hyped=(
                "{corner_label} 좋아졌어! 이대로!",
                "좋아! {corner_label} 완벽해!",
            ),
            warm=(
                "오~ {corner_label} 좋아졌어~",
                "좋아~ {corner_label} 딱 좋아~",
            ),
        ),
    ),
    Template(
        "improvement.fuel_lift_good",
        RadioKind.IMPROVEMENT,
        _moods(
            calm=(
                "{corner}번 리프트 좋아. 그대로 해.",
                "{corner}번 리프트 좋았어. 계속 해.",
            ),
            hyped=(
                "{corner}번 리프트 좋아! 그대로! 계속!",
                "완벽해! {corner}번 리프트! 이대로!",
            ),
            warm=(
                "오~ {corner}번 리프트 좋아~ 그대로 해~",
                "좋아~ {corner}번 리프트 좋아~ 이대로 가~",
            ),
        ),
        note="리프트 앤 코스트: 리프트가 좋았다",
    ),
    # 자동 탐지 코너(official=False)는 "3번째 코너"로 말한다(리뷰 포커스 5).
    Template(
        "coaching.fuel_lift_short_ordinal",
        RadioKind.COACHING,
        _moods(
            calm=(
                "{corner_label} 리프트가 짧았어. {meters}미터 더 일찍 떼.",
                "{corner_label} 리프트가 모자랐어. {meters}미터 더 일찍 떼.",
            ),
            hyped=(
                "{corner_label} 리프트 짧아! {meters}미터 더 일찍! 당겨!",
                "{corner_label} 부족해! {meters}미터 더! 지금!",
            ),
        ),
        needs_evidence=True,
        note="리프트 앤 코스트: 리프트가 짧았다(비공식 코너)",
    ),
    Template(
        "improvement.fuel_lift_good_ordinal",
        RadioKind.IMPROVEMENT,
        _moods(
            calm=(
                "{corner_label} 리프트 좋아. 그대로 해.",
                "{corner_label} 리프트 좋았어. 계속 해.",
            ),
            hyped=(
                "{corner_label} 리프트 좋아! 그대로! 계속!",
                "완벽해! {corner_label} 리프트! 이대로!",
            ),
            warm=(
                "오~ {corner_label} 리프트 좋아~ 그대로 해~",
                "좋아~ {corner_label} 리프트 좋아~ 이대로 가~",
            ),
        ),
        note="리프트 앤 코스트: 리프트가 좋았다(비공식 코너)",
    ),
    Template(
        "improvement.fuel_lift_stop",
        RadioKind.IMPROVEMENT,
        _moods(
            calm=(
                "연료 회복됐어. 이제 리프트 안 해도 돼.",
                "연료 괜찮아. 리프트 그만해도 돼.",
            ),
            hyped=(
                "연료 회복! 리프트 그만해도 돼! 좋아!",
                "연료 괜찮아! 리프트 끝! 이제 밀어!",
            ),
            warm=(
                "연료 회복됐어~ 이제 리프트 안 해도 돼~",
                "좋아~ 연료 괜찮아~ 리프트 그만해~",
            ),
        ),
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
    "coaching.fuel_lift_short": "coaching.fuel_lift_short_ordinal",
    "improvement.fuel_lift_good": "improvement.fuel_lift_good_ordinal",
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
    """틀 문장에 들어 있는 슬롯 이름을 처음 나온 순서로 돌려준다(calm 첫 표현)."""
    return _slots_in_text(get_template(key).text)


def template_slots_for(key: str, mood: Mood | str, variant: int = 0) -> tuple[str, ...]:
    """그 단계·표현의 슬롯 이름. 모든 표현이 같은 집합인지 검사할 때 쓴다."""
    texts = get_template(key).texts_for(mood)
    _check_variant(key, variant, len(texts))
    return _slots_in_text(texts[variant])


def _check_variant(key: str, variant: int, count: int) -> None:
    if not isinstance(variant, int) or isinstance(variant, bool):
        raise TemplateError(f"{key}: 표현 번호는 정수여야 합니다: {variant!r}")
    if not 0 <= variant < count:
        raise TemplateError(
            f"{key}: 표현 번호 {variant}가 범위를 벗어났습니다(0~{count - 1})"
        )


def render(
    key: str,
    *,
    mood: Mood | str | None = None,
    variant: int = 0,
    **slots: object,
) -> str:
    """틀 키와 슬롯 값으로 무전 문장을 만든다.

    `mood`를 생략하면 calm 첫 표현을 쓴다(기존 호출 호환). `variant`는 그
    단계의 표현 번호다. `VariantRotator`가 연달아 같은 표현이 나가지 않게
    번호를 골라 넘긴다.

    필요한 슬롯이 빠지면 `MissingSlot`, 모르는 키면 `UnknownTemplate`을 올린다.
    남는 슬롯 값은 무시한다(뒤 작업이 근거 수치를 함께 넘겨도 된다).
    """
    template = get_template(key)
    if mood is None:
        texts = template.moods[Mood.CALM.value]
    else:
        texts = template.texts_for(mood)
    _check_variant(key, variant, len(texts))
    text = texts[variant]
    missing = [name for name in _slots_in_text(text) if name not in slots]
    if missing:
        raise MissingSlot(
            f"{key} 틀에 필요한 값이 없습니다: {', '.join(missing)}"
        )
    return text.format(**slots)


def all_phrases(key: str, **slots: object) -> list[str]:
    """그 틀의 모든 단계·표현. 미리 합성(캐시) 목록에 쓴다(SCRUM-91)."""
    template = get_template(key)
    phrases: list[str] = []
    for mood, texts in template.moods.items():
        for variant in range(len(texts)):
            phrases.append(render(key, mood=mood, variant=variant, **slots))
    return list(dict.fromkeys(phrases))


class VariantRotator:
    """같은 틀·같은 단계에서 직전 표현을 피해 다음 표현을 고른다.

    무작위가 아니라 결정적이다. 틀·단계별로 마지막 표현 번호를 기억하고
    다음 번호를 쓴다. 한 세션(코치·규칙 객체)마다 하나씩 만들어 쓰면 세션이
    바뀔 때 자연히 초기화된다.
    """

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._last.clear()

    def next_variant(self, key: str, mood: Mood | str = Mood.CALM) -> int:
        template = get_template(key)
        resolved = _resolve_mood(mood)
        count = len(template.texts_for(resolved))
        state_key = (key, resolved.value)
        with self._lock:
            last = self._last.get(state_key)
            variant = 0 if last is None else (last + 1) % count
            self._last[state_key] = variant
            return variant

    def render(self, key: str, *, mood: Mood | str = Mood.CALM, **slots: object) -> str:
        variant = self.next_variant(key, mood)
        return render(key, mood=mood, variant=variant, **slots)

    def render_corner(
        self,
        key: str,
        corner: int,
        *,
        official: bool = True,
        mood: Mood | str = Mood.CALM,
        **slots: object,
    ) -> str:
        resolved = _resolve_corner_key(key, official)
        variant = self.next_variant(resolved, mood)
        return render_corner(
            key, corner, official=official, mood=mood, variant=variant, **slots
        )


def _resolve_corner_key(key: str, official: bool) -> str:
    if official:
        return key
    try:
        return ORDINAL_KEYS[key]
    except KeyError as exc:
        raise UnknownTemplate(f"{key} 틀에는 비공식 코너 문장이 없습니다") from exc


def corner_label(number: int, *, official: bool = True) -> str:
    """무전에 넣는 코너 이름. 공식 번호면 "3번", 자동 탐지면 "3번째 코너"."""
    return f"{int(number)}번" if official else f"{int(number)}번째 코너"


def render_corner(
    key: str,
    corner: int,
    *,
    official: bool = True,
    mood: Mood | str | None = None,
    variant: int = 0,
    **slots: object,
) -> str:
    """코너 번호가 들어가는 틀을 공식/비공식에 맞춰 만든다.

    `official=False`(데이터 파일이 없는 서킷의 자동 탐지 코너)면 같은 뜻의
    "N번째 코너" 틀을 골라 쓴다. `slots`에는 틀에 필요한 나머지 값
    (meters·count·kmh 등)을 넘긴다.
    """
    resolved = _resolve_corner_key(key, official)
    if official:
        return render(
            resolved, mood=mood, variant=variant, corner=int(corner), **slots
        )
    return render(
        resolved,
        mood=mood,
        variant=variant,
        corner_label=corner_label(corner, official=False),
        **slots,
    )


def corner_phrases(
    corners: Iterable[int], *, meters: int = 5
) -> list[str]:
    """서킷 코너 번호가 들어가는 대표 코칭 문장을 모든 단계·표현으로 만든다.

    세션 시작 때 `VoiceCache.prefetch()`에 넘겨, 가장 자주 나갈 코너 무전을
    미리 합성해 둔다(설계 6.8 "미리 만들기"). 감정 단계가 바뀌어도 캐시에
    있는 문장으로 바로 말할 수 있게 모든 단계·표현을 포함한다.
    """
    phrases: list[str] = []
    for number in corners:
        phrases.extend(
            all_phrases("coaching.brake_late", corner=int(number), meters=meters)
        )
    return list(dict.fromkeys(phrases))


def prefetch_phrases(corners: Sequence[int], *, meters: int = 5) -> list[str]:
    """세션 시작 때 미리 만들 문장 전체: 코너 번호 문장 + 고정 경고·개선.

    모든 감정 단계·표현을 포함한다(SCRUM-91).
    """
    phrases = corner_phrases(corners, meters=meters)
    for key in PRELOAD_KEYS:
        phrases.extend(all_phrases(key))
    return list(dict.fromkeys(phrases))
