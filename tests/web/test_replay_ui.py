"""SCRUM-101: 대시보드 다시 보기 UI(세션 목록 모달·리플레이 컨트롤 바).

백엔드 `replay_api.py`(T12)는 이 작업과 병렬로 만들어지므로, 여기서는 계획의
동결 API 계약에 맞춰 서빙되는 정적 프런트(HTML·JS·CSS)를 검증한다. 실제
`rclone`·GCS·엔진 호출은 하지 않는다(설계 4.5·4.6).

공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from f1re.state.store import StateStore
from f1re.web.app import create_app


def _client() -> TestClient:
    return TestClient(create_app(StateStore()))


def _html() -> str:
    response = _client().get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def _static(name: str) -> str:
    response = _client().get(f"/static/{name}")
    assert response.status_code == 200, name
    assert response.text, name
    return response.text


def _section(html: str, element: str, element_id: str) -> str:
    match = re.search(
        rf"<{element}\b[^>]*id=\"{re.escape(element_id)}\"[^>]*>(.*?)</{element}>",
        html,
        re.S,
    )
    assert match, f"{element_id} 요소가 없습니다"
    return match.group(0)


def test_replay_button_is_available_when_engine_is_off() -> None:
    """설계 4.6-3: 엔진 OFF 화면과 상단 상태줄 어디서든 '다시 보기'를 연다."""
    html = _html()

    # 화면 모서리의 항상 보이는 진입점.
    button = re.search(
        r'<button\b[^>]*id="btn-open-recordings"[^>]*>\s*다시 보기\s*</button>',
        html,
        re.S,
    )
    assert button, "다시 보기 버튼(#btn-open-recordings)이 없습니다"
    assert "aria-haspopup=\"dialog\"" in button.group(0)

    # 엔진 OFF 안내와 상단 상태줄에도 같은 진입점이 있다.
    engine_off = _section(html, "section", "engine-off")
    assert "data-open-recordings" in engine_off
    assert "다시 보기" in engine_off

    statusbar = _section(html, "header", "statusbar")
    assert "data-open-recordings" in statusbar
    assert "다시 보기" in statusbar

    # 모서리 버튼과 엔진 OFF 안내 버튼의 스타일이 있다.
    css = _static("style.css")
    assert ".recordings-fab" in css
    assert ".engine-off-button" in css


def test_recordings_modal_has_filter_badges_and_download_links() -> None:
    """설계 4.6-3: 날짜·서킷·종류/부/종료 사유 배지·길이·보관 위치·다운로드."""
    html = _html()
    script = _static("dashboard.js")

    # 모달 골격과 범주 필터.
    assert 'id="recordings-modal"' in html
    assert 'id="recordings-filter"' in html
    assert 'id="recordings-list"' in html
    assert 'id="recordings-status"' in html
    categories = re.findall(r'data-category="([a-z_]*)"', html)
    assert categories == ["", "practice", "qualifying", "race", "time_trial"]

    # 목록은 동결 계약 `GET /api/recordings?category=&limit=`를 읽는다.
    assert "/api/recordings?" in script
    assert 'params.set("category"' in script
    assert "function renderRecordings" in script
    assert "function recordingItem" in script
    assert "function recordingBadges" in script

    # 부(2부) 배지: segment > 1 이거나 같은 세션이 여러 부로 나뉘면 표시한다.
    assert "2부" in script
    assert "segment > 1 || splitSession" in script
    assert "${segment}부" in script

    # 종료 사유 배지 이름. 서버의 end_reason_label을 먼저 쓰고 없으면 이 표.
    for label in ("정상 종료", "10분 만료", "세션 전환", "엔진 종료", "복구"):
        assert label in script

    # 다운로드 링크(테스트용)와 보관 위치 표시.
    assert "recording.downloadable" in script
    assert "/api/recordings/${encodeURIComponent(recording.id)}/download" in script
    assert 'setAttribute("download"' in script
    assert '"다운로드"' in script
    assert '"로컬"' in script
    assert '"클라우드"' in script

    # 배지·모달 스타일.
    css = _static("style.css")
    assert ".recordings-modal" in css
    assert ".recording-item" in css
    assert ".category-badge" in css
    assert ".segment-badge" in css
    assert ".end-reason-badge" in css
    assert ".filter-chip" in css


def test_replay_bar_has_controls_and_speed_buttons() -> None:
    """설계 4.6-3: 재생/일시정지·배속 1/2/5/10x·시크 바·실시간 복귀."""
    html = _html()
    script = _static("dashboard.js")

    assert 'id="replay-bar"' in html
    assert 'id="replay-toggle"' in html
    assert 'id="replay-seek"' in html
    assert 'id="replay-time"' in html
    assert 'id="replay-back-live"' in html
    assert "실시간으로 돌아가기" in html
    speeds = re.findall(r'data-replay-speed="(\d+)"', html)
    assert speeds == ["1", "2", "5", "10"]

    # 컨트롤 바는 처음에 숨겨 두고 리플레이가 시작되면 보인다.
    assert re.search(r'id="replay-bar"[^>]*hidden', html)

    # 동결 계약의 시작·제어·WebSocket·상태 경로.
    assert "/api/replay/start" in script
    assert "/api/replay/control" in script
    assert "/ws/replay" in script
    assert "/api/replay/state" in script
    assert "/api/replay/track" in script
    for action in ("pause", "resume", "seek", "speed", "stop"):
        assert f'action: "{action}"' in script \
            or f'"{action}"' in script
    assert "position_ms" in script
    assert "duration_ms" in script

    # 재생/일시정지와 배속 버튼 상태를 서버 메타로 맞춘다.
    assert "function updateReplayBar" in script
    assert 'paused ? "재생" : "일시정지"' in script
    assert "data-replay-speed" in script

    css = _static("style.css")
    assert ".replay-bar" in css
    assert ".replay-toggle" in css
    assert ".speed-button.on" in css
    assert ".replay-seek" in css
    assert ".replay-live" in css


def test_replay_reuses_existing_panels_with_replay_data_sources() -> None:
    """설계 4.6-1·2: 기존 차량·지도·무전·리포트 UI를 리플레이 소스로 그린다."""
    script = _static("dashboard.js")
    report = _static("report.js")

    # 같은 상태 묶음(snapshot·car·cars·flag·flags·status)을 그대로 쓴다.
    assert "function applyReplayPayload" in script
    assert "view.render(payload.car)" in script
    assert "renderStatusBar(" in script
    assert "mapView.render(payload)" in script
    assert "payload.replay" in script
    assert "replay.active === false" in script

    # 서킷 지도는 리플레이 전용 API에서 받는다.
    assert "function loadReplayTrack" in script
    assert "mapView.setTrack(" in script

    # report.js는 리플레이 중 무전 기록·리포트 주소를 바꾸고,
    # 실시간 주소는 그대로 남겨 복귀할 수 있게 한다.
    assert "/api/replay/radio/log" in report
    assert "/api/replay/report" in report
    assert "f1re:replay-mode" in report
    assert "/api/radio/log" in report
    assert "/api/report" in report
    # 리플레이 중에는 종류별 끄기(POST)를 보내지 않는다.
    assert "if (muteBusy || replayActive) return;" in report

    # 실시간 대시보드 연결은 그대로 유지한다.
    assert "/ws/state" in script
    assert 'payload.type === "engine"' in script
    assert 'if (engineState !== "on") return;' in script
    assert "if (replayActive) return;" in script


def test_replay_ui_avoids_blocking_browser_dialogs() -> None:
    """브라우저 alert/confirm/prompt는 쓰지 않는다(모달·상태 문구로 알린다)."""
    pattern = re.compile(r"\b(?:alert|confirm|prompt)\s*\(")
    for name in ("dashboard.js", "report.js"):
        script = _static(name)
        assert not pattern.search(script), f"{name}에 브라우저 대화상자가 있습니다"


def test_replay_mode_wins_over_engine_off_notice() -> None:
    """엔진 OFF여도 리플레이 중에는 기존 대시보드 패널을 계속 보여 준다."""
    script = _static("dashboard.js")
    assert "function updateLayout" in script
    assert "replayActive || engineState === \"on\"" in script
    assert "replayBar.hidden = !replayActive;" in script
