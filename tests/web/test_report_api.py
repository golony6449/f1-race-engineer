"""무전 기록·종류별 끄기·세션 뒤 코너 리포트 API(작업 20, SCRUM-70).

공용 conftest를 만들지 않는 규칙에 따라 무전·DB 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from f1re import db
from f1re.radio.log import RadioLog
from f1re.radio.queue import RadioKind, RadioMessage
from f1re.state.store import StateStore
from f1re.web.app import create_app
from f1re.web.radio_api import RadioService

SESSION_UID = 7


# ---------------------------------------------------------------------------
# 도우미


def _message(
    text: str,
    *,
    kind: RadioKind = RadioKind.COACHING,
    created_ns: int | None = None,
    evidence: dict[str, Any] | None = None,
    lap: int | None = 5,
    corner: int | None = 3,
) -> RadioMessage:
    now_ns = time.time_ns() if created_ns is None else created_ns
    return RadioMessage.create(
        kind=kind.value,
        text=text,
        now_ns=now_ns,
        ttl_ns=10**15,
        evidence=evidence if evidence is not None else {"loss_ms": 100},
        lap=lap,
        corner=corner,
    )


def _record(
    conn: sqlite3.Connection,
    message: RadioMessage,
    *,
    session_uid: int = SESSION_UID,
    played: bool = True,
    latency_ms: int | None = 40,
) -> int:
    return RadioLog(conn, session_uid=session_uid).record(
        message, played=played, latency_ms=latency_ms
    )


def _open(tmp_path: Path) -> sqlite3.Connection:
    return db.open_db(tmp_path / "f1re.db")


def _client(
    conn: sqlite3.Connection | None,
    *,
    radio: RadioService | None = None,
) -> TestClient:
    store = StateStore()
    service = radio if radio is not None else RadioService(store)
    return TestClient(create_app(store, radio=service, conn=conn))


# ---------------------------------------------------------------------------
# 무전 기록


def test_log_includes_evidence_and_latency(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        coaching = _message(
            "3번, 브레이크 늦음. 5미터.",
            created_ns=1_000,
            evidence={"loss_ms": 120, "cause": "brake_late"},
        )
        box = _message(
            "박스, 박스.",
            kind=RadioKind.BOX,
            created_ns=2_000,
            evidence={"reason": "test"},
            lap=6,
            corner=None,
        )
        _record(conn, coaching, played=True, latency_ms=87)
        _record(conn, box, played=False, latency_ms=None)

        with _client(conn) as client:
            response = client.get(f"/api/radio/log?session={SESSION_UID}")
            assert response.status_code == 200
            data = response.json()

            # 세션을 생략하면 DB에 마지막으로 기록된 세션을 쓴다.
            latest = client.get("/api/radio/log").json()
            assert latest["session_uid"] == SESSION_UID
            assert [message["id"] for message in latest["messages"]] == [
                message["id"] for message in data["messages"]
            ]

        assert data["available"] is True
        assert data["reason"] is None
        assert data["session_uid"] == SESSION_UID
        assert [message["text"] for message in data["messages"]] == [
            "박스, 박스.",
            "3번, 브레이크 늦음. 5미터.",
        ]

        newest, oldest = data["messages"]
        assert newest["kind"] == "box"
        assert newest["kind_label"] == "박스 지시"
        assert newest["played"] is False
        assert newest["latency_ms"] is None
        assert newest["recorded_at_ns"] == 2_000

        assert oldest["kind"] == "coaching"
        assert oldest["kind_label"] == "코너 코칭"
        assert oldest["lap"] == 5
        assert oldest["corner"] == 3
        assert oldest["evidence"] == {"loss_ms": 120, "cause": "brake_late"}
        assert oldest["played"] is True
        assert oldest["latency_ms"] == 87
        assert oldest["recorded_at_iso"] is not None

        assert data["muted_kinds"] == []
        kinds = {item["kind"]: item for item in data["kinds"]}
        assert kinds["safety"] == {
            "kind": "safety",
            "label": "안전 경고",
            "mutable": False,
            "muted": False,
        }
        assert kinds["coaching"]["mutable"] is True
        assert kinds["improvement"]["label"] == "개선 알림"
    finally:
        conn.close()


def test_mute_kind_persists_and_safety_rejected(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    store = StateStore()
    radio = RadioService(store)
    try:
        with _client(conn, radio=radio) as client:
            response = client.post(
                "/api/radio/mute", json={"kind": "coaching", "muted": True}
            )
            assert response.status_code == 200
            assert response.json() == {
                "kind": "coaching",
                "muted": True,
                "muted_kinds": ["coaching"],
            }
            assert radio.queue.is_muted("coaching") is True
            # 끈 종류는 큐가 걸러 낸다.
            assert radio.queue.push(_message("코칭 무전")) is False

            # 다른 요청(화면 새로 고침)에서도 끄기 상태가 유지된다.
            log = client.get(f"/api/radio/log?session={SESSION_UID}").json()
            assert log["muted_kinds"] == ["coaching"]
            kinds = {item["kind"]: item for item in log["kinds"]}
            assert kinds["coaching"]["muted"] is True

            before = sorted(radio.queue.muted_kinds)
            response = client.post(
                "/api/radio/mute", json={"kind": "safety", "muted": True}
            )
            assert response.status_code == 409
            detail = response.json()["detail"]
            assert detail["muted"] is False
            assert detail["mutable"] is False
            assert "안전 경고" in detail["message"]
            assert radio.queue.is_muted("safety") is False
            assert sorted(radio.queue.muted_kinds) == before

            response = client.post(
                "/api/radio/mute", json={"kind": "coaching", "muted": False}
            )
            assert response.status_code == 200
            assert response.json()["muted"] is False
            assert radio.queue.is_muted("coaching") is False
            assert radio.queue.push(_message("코칭 무전")) is True

            response = client.post(
                "/api/radio/mute", json={"kind": "unknown", "muted": True}
            )
            assert response.status_code == 400
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 세션 리포트


def test_report_marks_improvement_after_message(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        _record(
            conn,
            _message(
                "3번, 브레이크 늦음. 5미터.",
                created_ns=1_000,
                evidence={"loss_ms": 180, "cause": "brake_late"},
                lap=5,
                corner=3,
            ),
            latency_ms=90,
        )
        _record(
            conn,
            _message(
                "3번, 턴인 늦음. 4미터.",
                created_ns=2_000,
                evidence={"loss_ms": 120, "cause": "turn_in_late"},
                lap=6,
                corner=3,
            ),
            latency_ms=80,
        )
        _record(
            conn,
            _message(
                "3번 좋아짐. 다음 7번.",
                kind=RadioKind.IMPROVEMENT,
                created_ns=2_100,
                evidence={"corner": 3, "loss_ms": 40, "next_corner": 7},
                lap=6,
                corner=3,
            ),
            latency_ms=70,
        )
        _record(
            conn,
            _message(
                "7번 탈출, 스로틀 늦음. 8미터.",
                created_ns=3_000,
                evidence={"loss_ms": 200, "cause": "late_throttle"},
                lap=7,
                corner=7,
            ),
            latency_ms=60,
        )

        with _client(conn) as client:
            response = client.get(f"/api/report?session={SESSION_UID}")
            assert response.status_code == 200
            report = response.json()

        assert report["available"] is True
        assert report["reason"] is None
        assert report["session_uid"] == SESSION_UID
        assert report["data_source"] == "radio_evidence"
        assert report["message_count"] == 4

        # 코너별 손실 순위: 최근 손실이 큰 코너부터.
        assert [item["corner"] for item in report["corner_losses"]] == [7, 3]
        losses = {item["corner"]: item for item in report["corner_losses"]}
        assert losses[7]["latest_loss_ms"] == 200
        assert losses[7]["best_loss_ms"] == 200
        assert losses[7]["messages"] == 1
        assert losses[7]["last_lap"] == 7
        assert losses[7]["trend"] == "unknown"
        assert losses[3]["latest_loss_ms"] == 40
        assert losses[3]["previous_loss_ms"] == 120
        assert losses[3]["best_loss_ms"] == 40
        assert losses[3]["messages"] == 3
        assert losses[3]["trend"] == "improved"
        assert losses[3]["latest_cause"] is None  # 개선 알림에는 원인이 없다.

        # 집중 코너 변화는 개선 알림 기록 그대로다.
        changes = report["focus_changes"]
        assert len(changes) == 1
        change = changes[0]
        assert change["lap"] == 6
        assert change["corner"] == 3
        assert change["next_corner"] == 7
        assert change["loss_ms"] == 40
        assert change["text"] == "3번 좋아짐. 다음 7번."

        # 무전이 나간 코너와 그 뒤 근거 비교.
        improvements = report["message_improvements"]
        assert len(improvements) == 3
        first, second, third = improvements

        assert first["lap"] == 5 and first["corner"] == 3
        assert first["loss_ms"] == 180
        assert first["next_lap"] == 6
        assert first["next_loss_ms"] == 120
        assert first["delta_ms"] == 60
        assert first["status"] == "improved"
        assert first["improved"] is True

        assert second["lap"] == 6 and second["corner"] == 3
        assert second["loss_ms"] == 120
        # 같은 랩의 개선 알림도 무전 뒤에 온 근거다.
        assert second["next_lap"] == 6
        assert second["next_loss_ms"] == 40
        assert second["delta_ms"] == 80
        assert second["status"] == "improved"

        assert third["lap"] == 7 and third["corner"] == 7
        assert third["status"] == "unknown"
        assert third["improved"] is None
        assert third["next_lap"] is None
        assert third["next_loss_ms"] is None

        summary = report["summary"]
        assert summary["messages"] == 4
        assert summary["played"] == 4
        assert summary["latency_avg_ms"] == 75
        assert summary["corners_with_loss"] == 2
        assert summary["focus_changes"] == 1
        assert summary["coaching_messages"] == 3
        assert summary["improved"] == 2
        assert summary["worsened"] == 0
        assert summary["unknown"] == 1
    finally:
        conn.close()


def test_report_marks_worsened_and_steady_after_message(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        _record(
            conn,
            _message(
                "5번, 브레이크 늦음. 3미터.",
                created_ns=1_000,
                evidence={"loss_ms": 100, "cause": "brake_late"},
                lap=2,
                corner=5,
            ),
        )
        _record(
            conn,
            _message(
                "5번, 브레이크 늦음. 9미터.",
                created_ns=2_000,
                evidence={"loss_ms": 180, "cause": "brake_late"},
                lap=3,
                corner=5,
            ),
        )
        _record(
            conn,
            _message(
                "6번, 스로틀 늦음. 4미터.",
                created_ns=1_100,
                evidence={"loss_ms": 100, "cause": "late_throttle"},
                lap=2,
                corner=6,
            ),
        )
        _record(
            conn,
            _message(
                "6번, 스로틀 늦음. 3미터.",
                created_ns=2_100,
                evidence={"loss_ms": 90, "cause": "late_throttle"},
                lap=3,
                corner=6,
            ),
        )

        with _client(conn) as client:
            report = client.get(f"/api/report?session={SESSION_UID}").json()

        statuses = {
            (item["corner"], item["lap"]): item["status"]
            for item in report["message_improvements"]
        }
        assert statuses[(5, 2)] == "worsened"
        assert statuses[(6, 2)] == "steady"
        # 마지막 랩의 무전은 비교할 다음 근거가 아직 없다.
        assert statuses[(5, 3)] == "unknown"
        assert statuses[(6, 3)] == "unknown"

        deltas = {
            (item["corner"], item["lap"]): item["delta_ms"]
            for item in report["message_improvements"]
        }
        assert deltas[(5, 2)] == -80
        assert deltas[(6, 2)] == 10
    finally:
        conn.close()


def test_report_selects_requested_or_latest_session(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    try:
        _record(
            conn,
            _message(
                "옛 세션 3번.",
                created_ns=1_000,
                evidence={"loss_ms": 120},
                lap=5,
                corner=3,
            ),
            session_uid=100,
        )
        _record(
            conn,
            _message(
                "새 세션 7번.",
                created_ns=2_000,
                evidence={"loss_ms": 80},
                lap=5,
                corner=7,
            ),
            session_uid=200,
        )

        with _client(conn) as client:
            latest = client.get("/api/report").json()
            older = client.get("/api/report?session=100").json()
            older_log = client.get("/api/radio/log?session=100").json()

        assert latest["session_uid"] == 200
        assert [item["corner"] for item in latest["corner_losses"]] == [7]
        assert older["session_uid"] == 100
        assert [item["corner"] for item in older["corner_losses"]] == [3]
        assert [message["text"] for message in older_log["messages"]] == [
            "옛 세션 3번."
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DB가 없을 때·화면 연결


def test_report_and_log_without_db_are_honest() -> None:
    client = _client(None)

    log = client.get("/api/radio/log").json()
    assert log["available"] is False
    assert log["reason"]
    assert log["messages"] == []
    assert log["session_uid"] is None
    # 끄기 버튼은 DB 없이도 그릴 수 있다.
    assert {item["kind"] for item in log["kinds"]} == {
        "safety",
        "box",
        "coaching",
        "overtake",
        "improvement",
    }

    report = client.get("/api/report").json()
    assert report["available"] is False
    assert report["reason"]
    assert report["corner_losses"] == []
    assert report["focus_changes"] == []
    assert report["message_improvements"] == []
    assert report["summary"]["messages"] == 0

    # 무전 끄기는 DB와 무관하게 동작한다.
    response = client.post(
        "/api/radio/mute", json={"kind": "overtake", "muted": True}
    )
    assert response.status_code == 200


def test_dashboard_loads_report_module() -> None:
    client = _client(None)

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "/static/report.js" in dashboard.text

    script = client.get("/static/report.js")
    assert script.status_code == 200
    assert "/api/radio/log" in script.text
    assert "/api/radio/mute" in script.text
    assert "/api/report" in script.text
