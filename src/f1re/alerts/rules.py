"""위험 경고 무전. 화면에서 빨간색이 되는 순간에만 말한다(설계 6.6).

대시보드와 **같은 문턱값**을 쓴다. 값의 위험 단계는 `alerts.thresholds.severity()`
하나로 판정하므로, `/api/thresholds`의 DANGER 색과 이 경고의 발화 조건은
언제나 같다.

원칙:

- DANGER로 **바뀌는 순간** 무전 한 줄을 만든다. 계속 DANGER인 동안에는 다시
  말하지 않고, 해제(OK/WARN)된 뒤 다시 DANGER가 되면 또 한 번 말한다.
- 같은 지표의 바퀴별 DANGER(타이어 마모·표면/내부 온도, 브레이크 온도)와
  앞날개 좌우 손상은 **한 평가 주기 안에서 한 무전으로 묶는다**(SCRUM-81).
  값은 위험한 것 중 가장 나쁜 값이고, 근거에는 바퀴별 값을 모두 남긴다.
  이미 DANGER였던 바퀴는 다시 말하지 않으므로, 새로 위험해진 바퀴만 문장에
  들어간다.
- 안전 경고·박스 지시의 유효 시간은 앞 무전이 재생되는 동안에도 지나지 않게
  설정 `[radio]`의 넉넉한 값을 쓴다(`rules_from_config()`).
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
from f1re.config import load_config
from f1re.radio.queue import RadioKind, RadioMessage
from f1re.radio.templates import render
from f1re.state.events import Event, EventKind

# 무전 유효 시간 기본값. 안전 경고·박스 지시는 앞 무전이 재생되는 동안
# 기다려도 만료되지 않도록 넉넉히 둔다(SCRUM-81). 실제 값은 설정 `[radio]`가
# 정하고 `rules_from_config()`가 읽는다. 코칭·추월은 시점이 중요하므로 각
# 모듈이 짧은 유효 시간을 직접 정한다.
SAFETY_TTL_NS = 30_000_000_000
BOX_TTL_NS = 30_000_000_000

# 패킷의 타이어 배열 순서(뒤왼쪽, 뒤오른쪽, 앞왼쪽, 앞오른쪽)와 같은 이름.
_TYRE_LABELS: tuple[str, ...] = ("뒤왼쪽", "뒤오른쪽", "앞왼쪽", "앞오른쪽")

# 바퀴별 위험 지표: (지표, 읽을 패킷, 필드, 근거에 넣을 온도 종류).
_TYRE_METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("tyre_wear", "damage", "tyres_wear", ""),
    ("tyre_temp", "telemetry", "tyres_surface_temperature", "surface"),
    ("tyre_inner_temp", "telemetry", "tyres_inner_temperature", "inner"),
    ("brake_temp", "telemetry", "brakes_temperature", ""),
)

# 지표별 (한 바퀴 틀, 여러 바퀴 묶음 틀, 값 슬롯). dict 순서가 무전 순서다.
_TYRE_TEMPLATES: dict[str, tuple[str, str, str]] = {
    "tyre_wear": ("safety.tyre_wear", "safety.tyre_wear_bundle", "percent"),
    "tyre_temp": ("safety.tyre_temp", "safety.tyre_temp_bundle", "celsius"),
    "tyre_inner_temp": ("safety.tyre_temp", "safety.tyre_temp_bundle", "celsius"),
    "brake_temp": ("safety.brake_temp", "safety.brake_temp_bundle", "celsius"),
}

# 앞날개는 좌우가 함께 위험해질 때 한 무전으로 묶는다(SCRUM-81).
_WING_KEYS = frozenset({"front_left_wing", "front_right_wing"})

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


@dataclass(frozen=True)
class _TyreReading:
    """바퀴 하나의 위험 지표 값. DANGER가 아니어도 근거에 넣는다."""

    metric: str
    position: int
    label: str
    value: float
    kind: str = ""

    @property
    def key(self) -> str:
        """전이 기억 키. 지표와 바퀴 이름으로 바퀴 하나를 가린다."""
        return f"{self.metric}:{self.label}"


@dataclass(frozen=True)
class _WingState:
    """앞날개 한쪽이 DANGER인 상태."""

    key: str
    part: str
    label: str
    side: str
    metric: str
    value: float


@dataclass
class _Memory:
    """세션 안에서의 전이 기억. 세션이 바뀌면 비운다."""

    danger_keys: set[str] = field(default_factory=set)
    box_active: bool = False
    safety_car_status: int | None = None
    penalties: int | None = None
    penalty_pending: int | None = None
    red_flag_periods: int | None = None


def rules_from_config(*, clock: Callable[[], int] = time.time_ns) -> AlertRules:
    """설정 `[radio]`의 유효 시간으로 위험 경고 규칙을 만든다.

    안전 경고·박스 지시의 TTL은 앞 무전 재생 때문에 사라지면 안 되므로
    설정 파일에서 넉넉한 값을 읽는다(SCRUM-81).
    """
    config = load_config()
    return AlertRules(
        clock=clock,
        safety_ttl_ns=_seconds_to_ns(config.radio.safety_ttl_s),
        box_ttl_ns=_seconds_to_ns(config.radio.box_ttl_s),
    )


class AlertRules:
    """위험 전이를 무전으로 바꾸는 규칙. 세션마다 전이 기억을 초기화한다."""

    def __init__(
        self,
        *,
        clock: Callable[[], int] = time.time_ns,
        safety_ttl_ns: int = SAFETY_TTL_NS,
        box_ttl_ns: int = BOX_TTL_NS,
    ) -> None:
        self._clock = clock
        self._safety_ttl_ns = int(safety_ttl_ns)
        self._box_ttl_ns = int(box_ttl_ns)
        self._session_uid: int | None = None
        self._memory = _Memory()

    @property
    def safety_ttl_ns(self) -> int:
        """안전 경고 유효 시간(ns)."""
        return self._safety_ttl_ns

    @property
    def box_ttl_ns(self) -> int:
        """박스 지시 유효 시간(ns)."""
        return self._box_ttl_ns

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
                        self._safety_ttl_ns,
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
        readings, wheels, wings, candidates = self._danger_scan(snapshot, packets)
        # 같은 지표의 바퀴들은 한 무전으로 묶는다(SCRUM-81).
        messages.extend(self._wheel_messages(readings, wheels, now_ns, lap))
        new_keys = [c for c in candidates if c.key not in self._memory.danger_keys]
        for candidate in new_keys:
            messages.append(
                self._make(
                    candidate.template,
                    RadioKind.SAFETY,
                    candidate.slots,
                    now_ns,
                    self._safety_ttl_ns,
                    evidence=candidate.evidence,
                    lap=lap,
                )
            )
        self._memory.danger_keys = (
            {reading.key for reading in wheels}
            | {wing.key for wing in wings}
            | {candidate.key for candidate in candidates}
        )

        tyre_danger = any(reading.metric == "tyre_wear" for reading in wheels)
        self._pit_messages(snapshot, packets, tyre_danger, now_ns, messages)

        session = packets.get("session") or {}
        self._safety_car_messages(session, lap, now_ns, messages)
        self._penalty_messages(snapshot, packets, lap, now_ns, messages)

    def _danger_scan(
        self, snapshot: dict[str, Any], packets: dict[str, Any]
    ) -> tuple[
        dict[str, list[_TyreReading]],
        list[_TyreReading],
        list[_WingState],
        list[_Candidate],
    ]:
        """지금 DANGER인 것 전부. 화면 색과 같은 `severity()`로 판정한다.

        돌려주는 것은 (지표별 타이어 값 전체, DANGER인 바퀴, DANGER인 앞날개,
        나머지 경고 후보)다. 바퀴는 `_wheel_messages()`가 지표별로 묶는다.
        """
        car_index = _player_index(snapshot)
        if car_index is None:
            return {}, [], [], []
        damage = _player_entry(packets, "car_damage", "car_damage_data", car_index)
        telemetry = _player_entry(
            packets, "car_telemetry", "car_telemetry_data", car_index
        )
        status = _player_entry(packets, "car_status", "car_status_data", car_index)

        readings = _tyre_readings(damage, telemetry)
        wheels = [
            reading
            for group in readings.values()
            for reading in group
            if severity(reading.metric, reading.value) is Severity.DANGER
        ]
        wings = _wing_states(damage)

        candidates: list[_Candidate] = []
        fuel = self._fuel_candidate(packets, car_index, status)
        if fuel is not None:
            candidates.append(fuel)
        candidates.extend(self._wing_candidates(wings))

        # 날개·부품 손상(앞날개 좌우는 위에서 묶었으므로 건너뛴다).
        for key, label, field_name, metric in _DAMAGE_ITEMS:
            if key in _WING_KEYS:
                continue
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
        return readings, wheels, wings, candidates

    def _fuel_candidate(
        self,
        packets: dict[str, Any],
        car_index: int,
        status: dict[str, Any],
    ) -> _Candidate | None:
        """연료 부족: 남은 연료 랩 - 남은 레이스 랩 < 0."""
        fuel_laps = _finite(status.get("fuel_remaining_laps"))
        remaining_race_laps = _remaining_race_laps(packets, car_index)
        if fuel_laps is None or remaining_race_laps is None:
            return None
        margin = fuel_laps - remaining_race_laps
        if severity("fuel_laps_margin", margin) is not Severity.DANGER:
            return None
        return _Candidate(
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

    def _wing_candidates(self, wings: Sequence[_WingState]) -> list[_Candidate]:
        """앞날개 좌우가 함께 새로 위험해지면 한 문장으로 묶는다(SCRUM-81)."""
        new = [wing for wing in wings if wing.key not in self._memory.danger_keys]
        if not new:
            return []
        sides = [
            {"side": wing.side, "part": wing.part, "value": wing.value}
            for wing in wings
        ]
        if len(new) == 2:
            worst = max(wing.value for wing in new)
            return [
                _Candidate(
                    key="damage:front_wing_both",
                    template="safety.front_wing_both",
                    slots={"percent": _fmt(worst)},
                    evidence=_evidence(
                        "front_wing_damage",
                        worst,
                        sides=sides,
                        new_sides=[wing.side for wing in new],
                    ),
                )
            ]
        return [
            _Candidate(
                key=wing.key,
                template="safety.damage",
                slots={"part": wing.label, "percent": _fmt(wing.value)},
                evidence=_evidence(
                    wing.metric, wing.value, part=wing.part, sides=sides
                ),
            )
            for wing in new
        ]

    def _wheel_messages(
        self,
        readings: dict[str, list[_TyreReading]],
        wheels: Sequence[_TyreReading],
        now_ns: int,
        lap: int | None,
    ) -> list[RadioMessage]:
        """바퀴별 DANGER를 지표별로 묶어 무전을 만든다.

        이번 평가에서 **새로** 위험해진 바퀴만 말한다(전이 1회 규칙). 같은
        지표에서 둘 이상이 새로 위험해지면 한 문장으로 묶고, 값은 그중 가장
        나쁜 값을 쓴다. 근거에는 네 바퀴 값을 모두 남긴다.
        """
        messages: list[RadioMessage] = []
        by_metric: dict[str, list[_TyreReading]] = {}
        for reading in wheels:
            by_metric.setdefault(reading.metric, []).append(reading)
        for metric, (single, bundle, value_slot) in _TYRE_TEMPLATES.items():
            current = by_metric.get(metric, [])
            new = [
                reading
                for reading in current
                if reading.key not in self._memory.danger_keys
            ]
            if not new:
                continue
            group = readings.get(metric, [])
            values = _wheel_values(group)
            kind = group[0].kind if group else ""
            extra = {"kind": kind} if kind else {}
            if len(new) == 1:
                reading = new[0]
                template = single
                slots = {
                    "tyre": reading.label,
                    value_slot: _fmt(reading.value),
                }
                evidence = _evidence(
                    metric,
                    reading.value,
                    tyre=reading.label,
                    position=reading.position,
                    values=values,
                    new_tyres=[reading.label],
                    **extra,
                )
            else:
                worst = max(reading.value for reading in new)
                pair_word = "바퀴" if metric == "brake_temp" else "개"
                phrase = _wheel_phrase(new, pair_word=pair_word)
                template = bundle
                slots = {"tyres": phrase, value_slot: _fmt(worst)}
                evidence = _evidence(
                    metric,
                    worst,
                    tyres=phrase,
                    values=values,
                    new_tyres=[reading.label for reading in new],
                    **extra,
                )
            messages.append(
                self._make(
                    template,
                    RadioKind.SAFETY,
                    slots,
                    now_ns,
                    self._safety_ttl_ns,
                    evidence=evidence,
                    lap=lap,
                )
            )
        return messages

    def _pit_messages(
        self,
        snapshot: dict[str, Any],
        packets: dict[str, Any],
        tyre_danger: bool,
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
        tyre_danger = total_laps > 0 and tyre_danger
        box_needed = bool(tyre_danger or unserved > 0)
        if box_needed and not self._memory.box_active:
            reason = "tyre_wear" if tyre_danger else "pit_penalty"
            messages.append(
                self._make(
                    "box.box_box",
                    RadioKind.BOX,
                    {},
                    now_ns,
                    self._box_ttl_ns,
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
                        self._safety_ttl_ns,
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
                        self._safety_ttl_ns,
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
                        self._safety_ttl_ns,
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
            self._safety_ttl_ns,
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


def _tyre_readings(
    damage: dict[str, Any], telemetry: dict[str, Any]
) -> dict[str, list[_TyreReading]]:
    """지표별 네 바퀴 값. 패킷 순서(뒤왼쪽, 뒤오른쪽, 앞왼쪽, 앞오른쪽)다."""
    readings: dict[str, list[_TyreReading]] = {}
    for metric, packet, field_name, kind in _TYRE_METRICS:
        entry = damage if packet == "damage" else telemetry
        values = entry.get(field_name)
        readings[metric] = [
            _TyreReading(metric, position, label, value, kind)
            for position, label in enumerate(_TYRE_LABELS)
            if (value := _finite(_index(values, position))) is not None
        ]
    return readings


def _wing_states(damage: dict[str, Any]) -> list[_WingState]:
    """지금 DANGER인 앞날개 좌우 상태."""
    states: list[_WingState] = []
    for key, label, field_name, metric in _DAMAGE_ITEMS:
        if key not in _WING_KEYS:
            continue
        value = _finite(damage.get(field_name))
        if value is None or severity(metric, value) is not Severity.DANGER:
            continue
        side = "왼쪽" if key.endswith("left_wing") else "오른쪽"
        states.append(
            _WingState(
                key=f"damage:{key}",
                part=key,
                label=label,
                side=side,
                metric=metric,
                value=value,
            )
        )
    return states


def _wheel_values(readings: Sequence[_TyreReading]) -> list[dict[str, Any]]:
    """근거에 남길 바퀴별 값. 문장에 넣지 않은 바퀴도 모두 담는다."""
    return [
        {
            "tyre": reading.label,
            "position": reading.position,
            "value": reading.value,
        }
        for reading in readings
    ]


def _wheel_phrase(states: Sequence[_TyreReading], *, pair_word: str) -> str:
    """묶인 바퀴를 부르는 말. 네 바퀴·앞 두 개·뒤왼쪽·앞오른쪽 식이다.

    `pair_word`는 "개"(타이어) 또는 "바퀴"(브레이크)다.
    """
    positions = {state.position for state in states}
    if len(states) == 4:
        return "4개" if pair_word == "개" else "네 바퀴"
    if positions == {0, 1}:
        return f"뒤 두 {pair_word}"
    if positions == {2, 3}:
        return f"앞 두 {pair_word}"
    return "·".join(state.label for state in states)


def _seconds_to_ns(seconds: float) -> int:
    return int(round(seconds * 1_000_000_000))


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
