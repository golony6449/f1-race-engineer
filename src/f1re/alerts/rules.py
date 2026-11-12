"""위험 경고 무전. 화면에서 빨간색이 되는 순간에만 말한다(설계 6.6).

대시보드와 **같은 문턱값**을 쓴다. 값의 위험 단계는 `alerts.thresholds.severity()`
하나로 판정하므로, `/api/thresholds`의 DANGER 색과 이 경고의 발화 조건은
언제나 같다.

원칙:

- DANGER로 **바뀌는 순간** 무전 한 줄을 만든다. 계속 DANGER인 동안에는 다시
  말하지 않고, 해제(OK/WARN)된 뒤 다시 DANGER가 되면 또 한 번 말한다.
- 경고 대상: 타이어 마모·표면/내부 온도, 브레이크 온도, 연료 부족(남은 연료
  랩 < 남은 레이스 랩), 날개·부품 큰 손상, 페널티, SC·VSC·적기, 박스 지시.
- 박스 지시는 (1) 레이스에서 타이어 마모가 DANGER가 되었거나 (2) 서브해야 할
  드라이브스루·스톱고 페널티가 생겼을 때 한 번 나간다. 타임 트라이얼에는
  박스가 없으므로 타이어 경고만 한다.
- 문장은 `radio.templates`의 틀에서만 만든다. 말투는 짧고 건조한 명사형이다.
- 무전 종류는 `RadioKind`(safety/box)이고, 안전 경고는 큐에서 끌 수 없다.

`evaluate(snapshot, events)`는 `StateStore.snapshot()`과 그 사이에 생긴 사건을
받는다. 상태 패킷만으로 알 수 있는 것(타이어·연료·손상·SC·페널티 등)은
스냅샷에서 읽고, 적기처럼 상태 필드가 없는 것은 사건에서 읽는다. 사건 목록은
생략할 수 있다(무전 서비스는 스냅샷만 주기적으로 본다).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from f1re.alerts.thresholds import Severity, metric_threshold, severity
from f1re.radio.queue import RadioKind, RadioMessage
from f1re.radio.templates import render
from f1re.state.events import Event, EventKind

# 무전 유효 시간. 안전 경고는 재생까지 조금 기다릴 수 있게, 박스는 더 짧게 둔다.
SAFETY_TTL_NS = 10_000_000_000
BOX_TTL_NS = 5_000_000_000

# 패킷의 타이어 배열 순서(뒤왼쪽, 뒤오른쪽, 앞왼쪽, 앞오른쪽)와 같은 이름.
_TYRE_LABELS: tuple[str, ...] = ("뒤왼쪽", "뒤오른쪽", "앞왼쪽", "앞오른쪽")

# 손상 항목: (상태 키, 화면·무전 이름, Car Damage 패킷 필드, 문턱값 지표).
_DAMAGE_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    (
        "front_left_wing",
        "앞날개 왼쪽",
        "front_left_wing_damage",
        "front_left_wing_damage",
    ),
    (
        "front_right_wing",
        "앞날개 오른쪽",
        "front_right_wing_damage",
        "front_right_wing_damage",
    ),
    ("rear_wing", "뒷날개", "rear_wing_damage", "rear_wing_damage"),
    ("floor", "바닥", "floor_damage", "floor_damage"),
    ("diffuser", "디퓨저", "diffuser_damage", "diffuser_damage"),
    ("sidepod", "사이드포드", "sidepod_damage", "sidepod_damage"),
    ("gearbox", "기어박스", "gear_box_damage", "gearbox_damage"),
    ("engine", "엔진", "engine_damage", "engine_damage"),
    # 파워유닛 마모도 화면에서 같은 색 기준으로 빨개진다. 빨간색이 되는 순간
    # 말한다는 원칙(6.6)에 따라 경고 대상에 넣는다.
    ("engine_mguh_wear", "MGU-H", "engine_mguh_wear", "engine_mguh_wear"),
    ("engine_es_wear", "에너지 저장장치", "engine_es_wear", "engine_es_wear"),
    ("engine_ce_wear", "제어 전자장치", "engine_ce_wear", "engine_ce_wear"),
    ("engine_ice_wear", "내연기관", "engine_ice_wear", "engine_ice_wear"),
    ("engine_mguk_wear", "MGU-K", "engine_mguk_wear", "engine_mguk_wear"),
    ("engine_tc_wear", "터보차저", "engine_tc_wear", "engine_tc_wear"),
)


@dataclass(frozen=True)
class _Candidate:
    """DANGER 판정을 통과한 경고 후보 하나."""

    key: str
    template: str
    slots: dict[str, str]
    evidence: dict[str, Any]


@dataclass
class _Memory:
    """세션 안에서의 전이 기억. 세션이 바뀌면 비운다."""

    danger_keys: set[str] = field(default_factory=set)
    box_active: bool = False
    safety_car_status: int | None = None
    penalties: int | None = None
    penalty_pending: int | None = None
    red_flag_periods: int | None = None


class AlertRules:
    """위험 전이를 무전으로 바꾸는 규칙. 세션마다 전이 기억을 초기화한다."""

    def __init__(self, *, clock: Callable[[], int] = time.time_ns) -> None:
        self._clock = clock
        self._session_uid: int | None = None
        self._memory = _Memory()

    def reset(self) -> None:
        """세션 전이 기억을 비운다(세션 재시작·테스트용)."""
        self._memory = _Memory()

    def evaluate(
        self,
        snapshot: dict[str, Any],
        events: Iterable[Event] = (),
    ) -> list[RadioMessage]:
        """스냅샷·사건에서 지금 나가야 할 무전을 만든다."""
        now_ns = self._clock()
        self._sync_session(snapshot)
        messages: list[RadioMessage] = []
        self._event_messages(tuple(events), snapshot, now_ns, messages)
        self._snapshot_messages(snapshot, now_ns, messages)
        return messages

    # ------------------------------------------------------------------
    # 세션·사건

    def _sync_session(self, snapshot: dict[str, Any]) -> None:
        uid = snapshot.get("session_uid")
        if uid != self._session_uid:
            self._session_uid = uid if isinstance(uid, int) else None
            self.reset()

    def _event_messages(
        self,
        events: Sequence[Event],
        snapshot: dict[str, Any],
        now_ns: int,
        messages: list[RadioMessage],
    ) -> None:
        player = _player_index(snapshot)
        for event in events:
            if event.kind is EventKind.RED_FLAG:
                messages.append(
                    self._make(
                        "safety.red_flag",
                        RadioKind.SAFETY,
                        {},
                        now_ns,
                        SAFETY_TTL_NS,
                        evidence={"event": EventKind.RED_FLAG.value},
                        lap=_current_lap(snapshot),
                    )
                )
            elif event.kind is EventKind.PENALTY:
                if event.car_index is not None and event.car_index != player:
                    continue
                seconds = _as_int(event.data.get("time"))
                if seconds is None or seconds <= 0:
                    continue
                self._memory.penalties = max(
                    self._memory.penalties or 0, seconds
                )
                self._memory.penalty_pending = seconds
                messages.append(
                    self._penalty_message(seconds, snapshot, now_ns)
                )

    # ------------------------------------------------------------------
    # 스냅샷

    def _snapshot_messages(
        self,
        snapshot: dict[str, Any],
        now_ns: int,
        messages: list[RadioMessage],
    ) -> None:
        packets = snapshot.get("packets") or {}
        lap = _current_lap(snapshot)
        candidates = self._danger_candidates(snapshot, packets)
        new_keys = [c for c in candidates if c.key not in self._memory.danger_keys]
        for candidate in new_keys:
            messages.append(
                self._make(
                    candidate.template,
                    RadioKind.SAFETY,
                    candidate.slots,
                    now_ns,
                    SAFETY_TTL_NS,
                    evidence=candidate.evidence,
                    lap=lap,
                )
            )
        self._memory.danger_keys = {candidate.key for candidate in candidates}

        self._pit_messages(snapshot, packets, candidates, now_ns, messages)

        session = packets.get("session") or {}
        self._safety_car_messages(session, lap, now_ns, messages)
        self._penalty_messages(snapshot, packets, lap, now_ns, messages)

    def _danger_candidates(
        self, snapshot: dict[str, Any], packets: dict[str, Any]
    ) -> list[_Candidate]:
        """지금 DANGER인 지표 전부. 화면 색과 같은 `severity()`로 판정한다."""
        car_index = _player_index(snapshot)
        if car_index is None:
            return []
        damage = _player_entry(packets, "car_damage", "car_damage_data", car_index)
        telemetry = _player_entry(
            packets, "car_telemetry", "car_telemetry_data", car_index
        )
        status = _player_entry(packets, "car_status", "car_status_data", car_index)
        candidates: list[_Candidate] = []

        # 타이어 마모
        wear = damage.get("tyres_wear")
        for index, label in enumerate(_TYRE_LABELS):
            value = _finite(_index(wear, index))
            if value is None or severity("tyre_wear", value) is not Severity.DANGER:
                continue
            candidates.append(
                _Candidate(
                    key=f"tyre_wear:{label}",
                    template="safety.tyre_wear",
                    slots={"tyre": label, "percent": _fmt(value)},
                    evidence=_evidence(
                        "tyre_wear", value, tyre=label, position=index
                    ),
                )
            )

        # 타이어 표면·내부 온도
        for metric, field_name, tag in (
            ("tyre_temp", "tyres_surface_temperature", "surface"),
            ("tyre_inner_temp", "tyres_inner_temperature", "inner"),
        ):
            values = telemetry.get(field_name)
            for index, label in enumerate(_TYRE_LABELS):
                value = _finite(_index(values, index))
                if (
                    value is None
                    or severity(metric, value) is not Severity.DANGER
                ):
                    continue
                candidates.append(
                    _Candidate(
                        key=f"{metric}:{label}",
                        template="safety.tyre_temp",
                        slots={"tyre": label, "celsius": _fmt(value)},
                        evidence=_evidence(
                            metric, value, tyre=label, kind=tag, position=index
                        ),
                    )
                )

        # 브레이크 온도
        brakes = telemetry.get("brakes_temperature")
        for index, label in enumerate(_TYRE_LABELS):
            value = _finite(_index(brakes, index))
            if value is None or severity("brake_temp", value) is not Severity.DANGER:
                continue
            candidates.append(
                _Candidate(
                    key=f"brake_temp:{label}",
                    template="safety.brake_temp",
                    slots={"tyre": label, "celsius": _fmt(value)},
                    evidence=_evidence("brake_temp", value, tyre=label, position=index),
                )
            )

        # 연료 부족: 남은 연료 랩 - 남은 레이스 랩 < 0
        fuel_laps = _finite(status.get("fuel_remaining_laps"))
        remaining_race_laps = _remaining_race_laps(packets, car_index)
        if fuel_laps is not None and remaining_race_laps is not None:
            margin = fuel_laps - remaining_race_laps
            if severity("fuel_laps_margin", margin) is Severity.DANGER:
                candidates.append(
                    _Candidate(
                        key="fuel_laps_margin",
                        template="safety.fuel_short",
                        slots={"laps": _fmt(abs(margin))},
                        evidence=_evidence(
                            "fuel_laps_margin",
                            margin,
                            fuel_remaining_laps=fuel_laps,
                            remaining_race_laps=remaining_race_laps,
                        ),
                    )
                )

        # 날개·부품 손상
        for key, label, field_name, metric in _DAMAGE_ITEMS:
            value = _finite(damage.get(field_name))
            if value is None or severity(metric, value) is not Severity.DANGER:
                continue
            candidates.append(
                _Candidate(
                    key=f"damage:{key}",
                    template="safety.damage",
                    slots={"part": label, "percent": _fmt(value)},
                    evidence=_evidence(metric, value, part=key),
                )
            )
        return candidates

    def _pit_messages(
        self,
        snapshot: dict[str, Any],
        packets: dict[str, Any],
        candidates: Sequence[_Candidate],
        now_ns: int,
        messages: list[RadioMessage],
    ) -> None:
        """박스 지시. 타이어 마모 위험(레이스)이나 서브할 페널티가 생겼을 때 1회."""
        session = packets.get("session") or {}
        total_laps = _as_int(session.get("total_laps")) or 0
        lap_entry = _player_entry(
            packets,
            "lap_data",
            "lap_data",
            _player_index(snapshot),
        )
        unserved = sum(
            value
            for value in (
                _as_int(lap_entry.get("num_unserved_drive_through_pens")),
                _as_int(lap_entry.get("num_unserved_stop_go_pens")),
            )
            if value is not None
        )
        tyre_danger = (
            total_laps > 0
            and any(candidate.key.startswith("tyre_wear:") for candidate in candidates)
        )
        box_needed = bool(tyre_danger or unserved > 0)
        if box_needed and not self._memory.box_active:
            reason = "tyre_wear" if tyre_danger else "pit_penalty"
            messages.append(
                self._make(
                    "box.box_box",
                    RadioKind.BOX,
                    {},
                    now_ns,
                    BOX_TTL_NS,
                    evidence={"reason": reason, "unserved_penalties": unserved},
                    lap=_current_lap(snapshot),
                )
            )
        self._memory.box_active = box_needed

    def _safety_car_messages(
        self,
        session: dict[str, Any],
        lap: int | None,
        now_ns: int,
        messages: list[RadioMessage],
    ) -> None:
        """SC·VSC·적기. Session 패킷의 상태 값이 바뀔 때만 말한다."""
        status = _as_int(session.get("safety_car_status"))
        if status is not None and status != self._memory.safety_car_status:
            if status == 1:
                messages.append(
                    self._make(
                        "safety.safety_car",
                        RadioKind.SAFETY,
                        {},
                        now_ns,
                        SAFETY_TTL_NS,
                        evidence={"safety_car_status": status},
                        lap=lap,
                    )
                )
            elif status == 2:
                messages.append(
                    self._make(
                        "safety.vsc",
                        RadioKind.SAFETY,
                        {},
                        now_ns,
                        SAFETY_TTL_NS,
                        evidence={"safety_car_status": status},
                        lap=lap,
                    )
                )
            self._memory.safety_car_status = status

        red_flags = _as_int(session.get("num_red_flag_periods"))
        if red_flags is not None:
            previous = self._memory.red_flag_periods
            if previous is not None and red_flags > previous:
                messages.append(
                    self._make(
                        "safety.red_flag",
                        RadioKind.SAFETY,
                        {},
                        now_ns,
                        SAFETY_TTL_NS,
                        evidence={
                            "num_red_flag_periods": red_flags,
                            "previous": previous,
                        },
                        lap=lap,
                    )
                )
            self._memory.red_flag_periods = red_flags

    def _penalty_messages(
        self,
        snapshot: dict[str, Any],
        packets: dict[str, Any],
        lap: int | None,
        now_ns: int,
        messages: list[RadioMessage],
    ) -> None:
        """Lap Data의 누적 페널티 초가 늘면 한 번 말한다."""
        lap_entry = _player_entry(
            packets, "lap_data", "lap_data", _player_index(snapshot)
        )
        penalties = _as_int(lap_entry.get("penalties"))
        if penalties is None:
            return
        # 사건으로 먼저 안 값이 아직 Lap Data에 반영되지 않았을 수 있다.
        # 스냅샷이 따라올 때까지는 같은 페널티를 다시 말하지 않는다.
        pending = self._memory.penalty_pending
        if pending is not None:
            if penalties < pending:
                return
            self._memory.penalty_pending = None
        previous = self._memory.penalties
        if previous is not None and penalties > previous:
            messages.append(self._penalty_message(penalties, snapshot, now_ns))
        self._memory.penalties = penalties

    # ------------------------------------------------------------------
    # 문장 만들기

    def _penalty_message(
        self, seconds: int, snapshot: dict[str, Any], now_ns: int
    ) -> RadioMessage:
        return self._make(
            "safety.penalty",
            RadioKind.SAFETY,
            {"seconds": str(seconds)},
            now_ns,
            SAFETY_TTL_NS,
            evidence={"penalty_seconds": seconds},
            lap=_current_lap(snapshot),
        )

    def _make(
        self,
        template: str,
        kind: RadioKind,
        slots: dict[str, str],
        now_ns: int,
        ttl_ns: int,
        *,
        evidence: dict[str, Any],
        lap: int | None,
    ) -> RadioMessage:
        return RadioMessage.create(
            kind=kind.value,
            text=render(template, **slots),
            now_ns=now_ns,
            ttl_ns=ttl_ns,
            evidence=evidence,
            lap=lap,
        )


# ---------------------------------------------------------------------------
# 스냅샷 읽기 도우미


def _player_index(snapshot: dict[str, Any]) -> int | None:
    value = snapshot.get("player_car_index")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < 22 else None


def _player_entry(
    packets: dict[str, Any],
    packet_name: str,
    list_key: str,
    car_index: int | None,
) -> dict[str, Any]:
    if car_index is None:
        return {}
    body = packets.get(packet_name) or {}
    entries = body.get(list_key) or []
    if car_index < len(entries) and isinstance(entries[car_index], dict):
        return entries[car_index]
    return {}


def _current_lap(snapshot: dict[str, Any]) -> int | None:
    packets = snapshot.get("packets") or {}
    lap_entry = _player_entry(
        packets, "lap_data", "lap_data", _player_index(snapshot)
    )
    return _as_int(lap_entry.get("current_lap_num"))


def _remaining_race_laps(
    packets: dict[str, Any], car_index: int | None
) -> float | None:
    """화면과 같은 계산: total_laps - current_lap_num + 1."""
    session = packets.get("session") or {}
    total_laps = _as_int(session.get("total_laps"))
    lap_entry = _player_entry(packets, "lap_data", "lap_data", car_index)
    current_lap = _as_int(lap_entry.get("current_lap_num"))
    if not total_laps or not current_lap or total_laps <= 0 or current_lap <= 0:
        return None
    return float(max(total_laps - current_lap + 1, 0))


def _evidence(metric: str, value: float, **extra: Any) -> dict[str, Any]:
    """근거 수치. 문턱값은 화면과 같은 단일 출처에서 읽는다."""
    threshold = metric_threshold(metric)
    payload: dict[str, Any] = {
        "metric": metric,
        "value": value,
        "warn": threshold.warn,
        "danger": threshold.danger,
        "unit": threshold.unit,
    }
    payload.update(extra)
    return payload


def _index(values: Any, index: int) -> Any:
    if isinstance(values, (list, tuple)) and 0 <= index < len(values):
        return values[index]
    return None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _fmt(value: float) -> str:
    """무전에 넣을 숫자. 정수면 소수점을 붙이지 않는다."""
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"
