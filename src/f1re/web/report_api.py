"""무전 기록·종류별 끄기·세션 뒤 코너 리포트(설계 6.7·6.9-3·4, 작업 20).

이 모듈이 대시보드에 붙는 세 API를 맡는다. 화면은 서버 API를 읽기만 하고,
무전 끄기 버튼만 POST로 상태를 바꾼다.

- ``GET /api/radio/log?session=``: 최근 무전 목록. 시각, 랩, 코너, 종류, 문장,
  근거 수치, 실제 재생 여부·지연, 재생되지 못한 이유(`dropped_reason`),
  종류별 끄기 상태를 내려 준다.
- ``POST /api/radio/mute``: ``{kind, muted}``로 무전 종류를 끄거나 켠다.
  안전 경고는 `RadioQueue.set_muted`가 거부하므로 409로 답한다.
- ``GET /api/report?session=``: 코너별 손실 순위, 집중 코너 변화, 무전이 나간
  코너와 그 뒤 개선 여부.

리포트의 데이터 출처는 무전 기록의 근거 수치(``evidence.loss_ms``)다. 이
마일스톤에는 완료 랩의 샘플을 DB에 저장하지 않으므로, 코치가 무전을 낸 코너의
손실만 알 수 있다. 응답의 ``data_source``에 그 사실을 밝히고, DB가 없을 때도
``available``·``reason``으로 화면이 깨지지 않게 한다.

세션 선택 규칙: ``session`` 쿼리가 있으면 그 세션, 없으면 지금 수신 중인 세션,
그것도 없으면 DB에 마지막으로 기록된 세션이다. 세션이 하나도 없으면
``session_uid``는 ``None``이고 빈 목록을 돌려준다.

끄기 상태는 서버 프로세스가 사는 동안 유지된다(대시보드가 다시 접속해도 같은
`RadioQueue`를 읽는다). 안전 경고는 설계 6.7(마스터 판단)에 따라 끌 수 없다.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from f1re.radio.log import RadioLog
from f1re.radio.queue import MUTABLE_KINDS, RadioKind

if TYPE_CHECKING:
    from f1re.state.store import StateStore
    from f1re.web.radio_api import RadioService

# 무전 기록 화면의 기본·최대 줄 수. 화면은 최근 것부터 보여 준다.
DEFAULT_LOG_LIMIT = 50
MAX_LOG_LIMIT = 200

# 리포트가 한 세션에서 읽을 최대 기록 수. `RadioLog.recent()`의 상한과 같다.
REPORT_MESSAGE_LIMIT = 1000

# 손실이 이만큼(ms) 줄면 "개선"으로 본다. 집중 코너를 빼는 문턱값
# (`FocusSet.min_loss_ms`, 기본 50ms)과 같은 값을 써, 무전이 사라진 이유와
# 리포트의 "개선" 판정이 어긋나지 않게 한다.
IMPROVEMENT_MIN_MS = 50

DATA_SOURCE_RADIO_EVIDENCE = "radio_evidence"
_DB_UNAVAILABLE = "무전 기록 DB가 연결되지 않았습니다"

# 설계 6.7의 무전 종류별 화면 이름. 화면은 이 이름만 쓴다.
_KIND_LABELS: dict[str, str] = {
    RadioKind.SAFETY.value: "안전 경고",
    RadioKind.BOX.value: "박스 지시",
    RadioKind.COACHING.value: "코너 코칭",
    RadioKind.OVERTAKE.value: "추월",
    RadioKind.IMPROVEMENT.value: "개선 알림",
}

# 화면에 보여 줄 순서. 안전 경고를 맨 앞에 두어 끌 수 없음을 눈에 띄게 한다.
_KIND_ORDER: tuple[RadioKind, ...] = (
    RadioKind.SAFETY,
    RadioKind.BOX,
    RadioKind.COACHING,
    RadioKind.OVERTAKE,
    RadioKind.IMPROVEMENT,
)


class MuteRequest(BaseModel):
    """무전 끄기 요청. `muted=False`면 다시 켠다."""

    kind: str
    muted: bool


@dataclass(frozen=True)
class _LossEntry:
    """근거 수치에 코너 손실(ms)이 있는 무전 한 줄."""

    row: dict[str, Any]
    corner: int
    loss_ms: int
    evidence: dict[str, Any]


# ---------------------------------------------------------------------------
# 라우터


def create_report_router(runtime: Callable[[], Any | None]) -> APIRouter:
    """무전 기록·끄기·세션 리포트 라우터. `web/app.py`가 앱에 한 줄로 붙인다.

    `runtime`은 엔진이 켜져 있을 때 `Runtime`(저장소·무전 서비스·DB)을, 꺼져
    있으면 `None`을 돌려준다. 요청마다 읽으므로 엔진을 껐다 켜도 같은 앱이
    새 런타임을 쓴다(SCRUM-82).
    """
    router = APIRouter()

    def _current() -> Any:
        current = runtime()
        if current is None:
            raise HTTPException(status_code=503, detail={"engine": "off"})
        return current

    @router.get("/api/radio/log")
    def api_radio_log(
        session: int | None = Query(default=None, description="세션 UID"),
        limit: int = Query(default=DEFAULT_LOG_LIMIT, ge=1, le=MAX_LOG_LIMIT),
    ) -> dict[str, Any]:
        current = _current()
        return radio_log_payload(
            current.store,
            current.radio,
            current.conn,
            session_uid=session,
            limit=limit,
        )

    @router.post("/api/radio/mute")
    def api_radio_mute(body: MuteRequest) -> dict[str, Any]:
        radio = _current().radio
        kind = body.kind.strip().lower()
        try:
            changed = radio.queue.set_muted(kind, body.muted)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not changed:
            # `MUTABLE_KINDS`에서 빠진 것은 안전 경고뿐이다(설계 6.7).
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "안전 경고는 끌 수 없습니다",
                    "kind": kind,
                    "muted": False,
                    "mutable": False,
                },
            )
        return {
            "kind": kind,
            "muted": radio.queue.is_muted(kind),
            "muted_kinds": sorted(radio.queue.muted_kinds),
        }

    @router.get("/api/report")
    def api_report(
        session: int | None = Query(default=None, description="세션 UID"),
    ) -> dict[str, Any]:
        current = _current()
        return report_payload(
            current.store, current.conn, session_uid=session
        )

    return router


# ---------------------------------------------------------------------------
# 무전 기록


def radio_log_payload(
    store: StateStore,
    radio: RadioService,
    conn: sqlite3.Connection | None,
    *,
    session_uid: int | None = None,
    limit: int = DEFAULT_LOG_LIMIT,
) -> dict[str, Any]:
    """`GET /api/radio/log` 응답."""
    resolved = _resolve_session(store, conn, session_uid)
    rows = _query_rows(conn, resolved, limit)
    muted = sorted(radio.queue.muted_kinds)
    return {
        "available": rows is not None,
        "reason": None if rows is not None else _DB_UNAVAILABLE,
        "session_uid": resolved,
        "messages": [] if rows is None else [_message_payload(row) for row in rows],
        "muted_kinds": muted,
        "kinds": _kind_states(muted),
    }


def _message_payload(row: dict[str, Any]) -> dict[str, Any]:
    """무전 한 줄을 화면이 바로 쓰는 모양으로 바꾼다."""
    recorded_at_ns = int(row["recorded_at_ns"])
    evidence = row["evidence"] if isinstance(row["evidence"], dict) else {}
    return {
        "id": int(row["id"]),
        "session_uid": row["session_uid"],
        "recorded_at_ns": recorded_at_ns,
        "recorded_at_iso": _iso_time(recorded_at_ns),
        "lap": row["lap"],
        "corner": row["corner"],
        "kind": row["kind"],
        "kind_label": _KIND_LABELS.get(row["kind"], row["kind"]),
        "priority": int(row["priority"]),
        "text": row["text"],
        "evidence": evidence,
        "played": bool(row["played"]),
        "latency_ms": row["latency_ms"],
        # 유효 시간 만료 등으로 재생되지 못한 이유(SCRUM-81).
        "dropped_reason": row.get("dropped_reason"),
    }


def _kind_states(muted: list[str]) -> list[dict[str, Any]]:
    """종류별 끄기 버튼 상태. 안전 경고는 `mutable=False`다."""
    muted_set = set(muted)
    return [
        {
            "kind": kind.value,
            "label": _KIND_LABELS[kind.value],
            "mutable": kind in MUTABLE_KINDS,
            "muted": kind.value in muted_set,
        }
        for kind in _KIND_ORDER
    ]


# ---------------------------------------------------------------------------
# 세션 리포트


def report_payload(
    store: StateStore,
    conn: sqlite3.Connection | None,
    *,
    session_uid: int | None = None,
) -> dict[str, Any]:
    """`GET /api/report` 응답.

    무전 기록에서 코너 손실 근거가 있는 줄만 모아 순위를 만들고, 개선 알림과
    코칭 무전의 다음 근거를 비교해 "무전이 나간 코너의 그 뒤 개선 여부"를 낸다.
    """
    resolved = _resolve_session(store, conn, session_uid)
    rows = _query_rows(conn, resolved, REPORT_MESSAGE_LIMIT)
    if rows is None:
        return {
            "available": False,
            "reason": _DB_UNAVAILABLE,
            "session_uid": resolved,
            "data_source": DATA_SOURCE_RADIO_EVIDENCE,
            "message_count": 0,
            "corner_losses": [],
            "focus_changes": [],
            "message_improvements": [],
            "summary": _summary([], [], [], []),
        }

    entries = _loss_entries(rows)
    corner_losses = _corner_losses(entries)
    focus_changes = _focus_changes(rows)
    message_improvements = _message_improvements(rows)
    return {
        "available": True,
        "reason": None,
        "session_uid": resolved,
        "data_source": DATA_SOURCE_RADIO_EVIDENCE,
        "message_count": len(rows),
        "corner_losses": corner_losses,
        "focus_changes": focus_changes,
        "message_improvements": message_improvements,
        "summary": _summary(
            rows, corner_losses, focus_changes, message_improvements
        ),
    }


def _loss_entries(rows: list[dict[str, Any]]) -> list[_LossEntry]:
    """근거 수치에 `loss_ms`가 있는 줄만 골라 코너·손실과 묶는다.

    `rows`는 `RadioLog.recent()`가 돌려준 새것부터 순서다.
    """
    entries: list[_LossEntry] = []
    for row in rows:
        evidence = row["evidence"] if isinstance(row["evidence"], dict) else {}
        loss_ms = _as_int(evidence.get("loss_ms"))
        corner = _corner_of(row, evidence)
        if loss_ms is None or corner is None:
            continue
        entries.append(
            _LossEntry(row=row, corner=corner, loss_ms=loss_ms, evidence=evidence)
        )
    return entries


def _corner_losses(entries: list[_LossEntry]) -> list[dict[str, Any]]:
    """코너별 최근 손실. 큰 순서로 정렬한다.

    `latest_loss_ms`는 그 코너에 대해 마지막으로 기록된 근거 수치다. 이
    마일스톤에는 같은 코너를 매 랩 측정해 저장하지 않으므로, 코치가 무전을 낸
    랩의 값만 있다. `messages`는 그 코너 근거가 남은 무전 수다.
    """
    latest: dict[int, _LossEntry] = {}
    history: dict[int, list[int]] = defaultdict(list)
    counts: Counter[int] = Counter()
    for entry in entries:  # 새것부터
        history[entry.corner].append(entry.loss_ms)
        counts[entry.corner] += 1
        latest.setdefault(entry.corner, entry)

    result: list[dict[str, Any]] = []
    for corner, entry in latest.items():
        values = history[corner]
        result.append(
            {
                "corner": corner,
                "latest_loss_ms": entry.loss_ms,
                "previous_loss_ms": values[1] if len(values) > 1 else None,
                "best_loss_ms": min(values),
                "trend": _trend(values),
                "messages": counts[corner],
                "last_lap": entry.row["lap"],
                "latest_cause": entry.evidence.get("cause"),
                "latest_text": entry.row["text"],
                "last_heard_at_ns": int(entry.row["recorded_at_ns"]),
            }
        )
    result.sort(key=lambda item: (-item["latest_loss_ms"], item["corner"]))
    return result


def _trend(values: list[int]) -> str:
    """최근 두 손실을 비교한 방향. 근거가 하나뿐이면 `unknown`."""
    if len(values) < 2:
        return "unknown"
    delta = values[0] - values[1]
    if delta <= -IMPROVEMENT_MIN_MS:
        return "improved"
    if delta >= IMPROVEMENT_MIN_MS:
        return "worsened"
    return "steady"


def _focus_changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """집중 코너 변화(개선 알림). 오래된 것부터 보여 준다.

    `FocusSet`이 내는 ADDED·DROPPED 변화는 무전을 내지 않으므로 기록에 없다.
    무전이 나간 "집중 코너에서 빠짐(개선)"만 리포트에 실린다.
    """
    changes: list[dict[str, Any]] = []
    for row in rows:
        if row["kind"] != RadioKind.IMPROVEMENT.value:
            continue
        evidence = row["evidence"] if isinstance(row["evidence"], dict) else {}
        corner = _corner_of(row, evidence)
        if corner is None:
            continue
        changes.append(
            {
                "message_id": int(row["id"]),
                "lap": row["lap"],
                "corner": corner,
                "loss_ms": _as_int(evidence.get("loss_ms")),
                "next_corner": _as_int(evidence.get("next_corner")),
                "text": row["text"],
                "recorded_at_ns": int(row["recorded_at_ns"]),
            }
        )
    changes.sort(key=lambda item: (item["recorded_at_ns"], item["message_id"]))
    return changes


def _message_improvements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """코칭 무전이 나간 코너가 그 뒤 근거에서 좋아졌는지 판정한다.

    같은 코너의 다음 근거(랩이 같거나 뒤, 기록 시각이 뒤)와 비교한다. 다음
    근거가 없으면 `status="unknown"`, `improved=None`이다. 개선 알림이 나온
    경우는 손실이 문턱 아래로 내려간 것이므로 개선으로 센다.
    """
    entries = _loss_entries(rows)
    entries.sort(key=_entry_order)
    by_corner: dict[int, list[_LossEntry]] = defaultdict(list)
    for entry in entries:
        by_corner[entry.corner].append(entry)

    results: list[dict[str, Any]] = []
    for message in entries:
        if message.row["kind"] != RadioKind.COACHING.value:
            continue
        following = next(
            (
                entry
                for entry in by_corner[message.corner]
                if _entry_order(entry) > _entry_order(message)
                and _lap_not_earlier(entry, message)
            ),
            None,
        )
        item: dict[str, Any] = {
            "message_id": int(message.row["id"]),
            "lap": message.row["lap"],
            "corner": message.corner,
            "text": message.row["text"],
            "loss_ms": message.loss_ms,
            "cause": message.evidence.get("cause"),
            "recorded_at_ns": int(message.row["recorded_at_ns"]),
            "next_message_id": None,
            "next_lap": None,
            "next_loss_ms": None,
            "delta_ms": None,
            "status": "unknown",
            "improved": None,
        }
        if following is not None:
            delta = message.loss_ms - following.loss_ms
            if (
                delta >= IMPROVEMENT_MIN_MS
                or following.row["kind"] == RadioKind.IMPROVEMENT.value
            ):
                status = "improved"
            elif delta <= -IMPROVEMENT_MIN_MS:
                status = "worsened"
            else:
                status = "steady"
            item.update(
                {
                    "next_message_id": int(following.row["id"]),
                    "next_lap": following.row["lap"],
                    "next_loss_ms": following.loss_ms,
                    "delta_ms": delta,
                    "status": status,
                    "improved": status == "improved",
                }
            )
        results.append(item)
    return results


def _entry_order(entry: _LossEntry) -> tuple[int, int]:
    return (int(entry.row["recorded_at_ns"]), int(entry.row["id"]))


def _lap_not_earlier(entry: _LossEntry, message: _LossEntry) -> bool:
    """다음 근거의 랩이 코칭 무전의 랩보다 앞서지 않는지."""
    entry_lap = entry.row["lap"]
    message_lap = message.row["lap"]
    if not isinstance(entry_lap, int) or not isinstance(message_lap, int):
        return True
    return entry_lap >= message_lap


def _summary(
    rows: list[dict[str, Any]],
    corner_losses: list[dict[str, Any]],
    focus_changes: list[dict[str, Any]],
    message_improvements: list[dict[str, Any]],
) -> dict[str, Any]:
    played = [row for row in rows if row["played"]]
    latencies = [
        int(row["latency_ms"])
        for row in played
        if isinstance(row["latency_ms"], int)
    ]
    statuses = Counter(item["status"] for item in message_improvements)
    return {
        "messages": len(rows),
        "played": len(played),
        "latency_avg_ms": int(round(sum(latencies) / len(latencies)))
        if latencies
        else None,
        "corners_with_loss": len(corner_losses),
        "focus_changes": len(focus_changes),
        "coaching_messages": len(message_improvements),
        "improved": statuses.get("improved", 0),
        "worsened": statuses.get("worsened", 0),
        "steady": statuses.get("steady", 0),
        "unknown": statuses.get("unknown", 0),
    }


# ---------------------------------------------------------------------------
# 세션·행 조회


def _resolve_session(
    store: StateStore,
    conn: sqlite3.Connection | None,
    session_uid: int | None,
) -> int | None:
    """쿼리 세션 → 현재 세션 → DB의 마지막 세션 순서로 고른다."""
    if session_uid is not None:
        return int(session_uid)
    current = store.snapshot().get("session_uid")
    if isinstance(current, int) and not isinstance(current, bool):
        return current
    return _latest_session_uid(conn)


def _latest_session_uid(conn: sqlite3.Connection | None) -> int | None:
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT session_uid FROM radio_messages
            WHERE session_uid IS NOT NULL
            ORDER BY recorded_at_ns DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    value = row["session_uid"] if hasattr(row, "keys") else row[0]
    return _as_int(value)


def _query_rows(
    conn: sqlite3.Connection | None,
    session_uid: int | None,
    limit: int,
) -> list[dict[str, Any]] | None:
    """세션의 무전 기록. DB가 없거나 읽지 못하면 `None`."""
    if conn is None:
        return None
    try:
        log = RadioLog(conn)
        if session_uid is None:
            return log.recent(limit=limit, all_sessions=True)
        return log.recent(limit=limit, session_uid=session_uid)
    except (sqlite3.Error, TypeError, KeyError):
        # DB 파일이 없거나 스키마가 다르면 API가 500으로 죽지 않게 한다.
        return None


# ---------------------------------------------------------------------------
# 값 도우미


def _corner_of(row: dict[str, Any], evidence: dict[str, Any]) -> int | None:
    """무전의 코너 번호. 열이 비어 있으면 근거 수치에서 찾는다."""
    corner = _as_int(row.get("corner"))
    if corner is not None:
        return corner
    return _as_int(evidence.get("corner"))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(round(value))
    return None


def _iso_time(recorded_at_ns: int) -> str | None:
    """화면·로그 확인용 UTC 시각. 값이 이상하면 `None`."""
    if recorded_at_ns <= 0:
        return None
    try:
        return datetime.fromtimestamp(
            recorded_at_ns / 1_000_000_000, tz=UTC
        ).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


__all__ = [
    "DEFAULT_LOG_LIMIT",
    "IMPROVEMENT_MIN_MS",
    "MAX_LOG_LIMIT",
    "MuteRequest",
    "REPORT_MESSAGE_LIMIT",
    "create_report_router",
    "radio_log_payload",
    "report_payload",
]
