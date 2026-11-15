"""SCRUM-111: 무전 페이지에 대시보드와 같은 데이터 수신 상태를 보여 준다.

무전 페이지의 연결 배너를 가로 반반으로 나눠 왼쪽은 무전 연결, 오른쪽은
데이터 수신 상태다. 문구는 `carstate.js`의 공용 함수를 써서 대시보드와
어긋나지 않게 한다. 공용 conftest를 만들지 않는 규칙에 따라 도우미는 이
파일 안에 둔다.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from f1re.state.store import StateStore
from f1re.web.app import create_app


def _client() -> TestClient:
    return TestClient(create_app(StateStore()))


def _radio_html() -> str:
    response = _client().get("/radio")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def _radio_css() -> str:
    match = re.search(r"<style>(.*?)</style>", _radio_html(), re.S)
    assert match, "무전 페이지에 인라인 스타일이 없습니다"
    return match.group(1)


def test_radio_page_splits_banner_into_radio_and_data_halves() -> None:
    html = _radio_html()

    start = html.index('id="connection"')
    end = html.index('class="connect-row"', start)
    banner = html[start:end]

    # 배너는 가로 반반 두 칸이다.
    assert banner.count('class="banner-half"') == 2
    # 왼쪽은 기존 무전 연결 상태를 그대로 보여 준다.
    assert "무전 연결" in banner
    assert 'id="connection-text"' in banner
    assert "무전 연결 대기" in banner
    # 오른쪽은 대시보드와 같은 데이터 수신 상태(라벨·값·진단 한 줄)다.
    assert "데이터 수신" in banner
    assert 'id="data-status"' in banner
    assert 'id="data-status-value"' in banner
    assert 'id="data-status-detail"' in banner
    assert "데이터 없음" in banner


def test_banner_halves_stay_side_by_side_on_phone() -> None:
    css = _radio_css()

    banner = re.search(r"\n\s*\.banner \{(.*?)\}", css, re.S)
    assert banner, ".banner 규칙이 없습니다"
    assert "display: flex;" in banner.group(1)

    # 두 칸은 폭을 반씩 나눠 갖고, 좁아져도 칸 자체는 유지한다.
    half = re.search(r"\n\s*\.banner-half \{(.*?)\}", css, re.S)
    assert half, ".banner-half 규칙이 없습니다"
    rule = half.group(1)
    assert "flex: 1 1 50%;" in rule
    assert "min-width: 0;" in rule

    # 긴 문구는 칸 안에서 줄바꿈한다(휴대폰 폭에서 넘침 방지).
    for selector in (r"\.banner-value", r"\.banner-detail"):
        match = re.search(rf"\n\s*{selector} \{{(.*?)\}}", css, re.S)
        assert match, f"{selector} 규칙이 없습니다"
        assert "overflow-wrap: anywhere;" in match.group(1)

    # 휴대폰 폭에서도 반반을 유지한 채 안쪽 여백만 줄인다.
    phone = re.search(r"@media \(max-width: 480px\) \{(.*?)\n      \}", css, re.S)
    assert phone, "휴대폰 폭(480px 이하) 규칙이 없습니다"
    assert ".banner " in phone.group(1)
    assert ".banner-half {" in phone.group(1)


def test_radio_script_polls_state_and_reuses_dashboard_wording() -> None:
    script = _client().get("/static/radio.js").text

    # 배너 전체 글자를 덮지 않고 왼쪽 연결 칸만 바꾼다(오른쪽 데이터 칸 유지).
    assert "connectionTextNode.textContent = text" in script
    assert "connectionNode.textContent" not in script

    # 대시보드와 같은 원천(`/api/state`)을 약 1초마다 읽는다.
    assert 'fetch("/api/state"' in script
    assert "const DATA_POLL_MS = 1000;" in script

    # 문구는 carstate.js의 공용 함수만 쓴다(대시보드와 어긋나지 않게).
    assert 'from "./carstate.js"' in script
    assert "connectionText(" in script
    assert "diagnosticsText(" in script
    assert "데이터 안 옴" not in script
    assert "형식 ${" not in script

    # 엔진이 꺼지면 데이터 폴링을 멈추고 "데이터 없음"으로 되돌린다.
    assert "stopDataPolling();" in script
    assert "clearDataStatus();" in script

    # 기존 무전 재생·재연결·엔진 OFF 안내는 그대로다.
    assert "/ws/radio" in script
    assert "scheduleReconnect" in script
    assert 'case "engine":' in script


def test_carstate_shares_connection_wording_with_radio_page() -> None:
    script = _client().get("/static/carstate.js").text

    assert "export function connectionText(" in script
    assert "export function diagnosticsText(" in script
    # 대시보드 상태줄도 같은 함수를 쓴다(동작은 그대로).
    assert "connectionValue.textContent = connectionText(" in script
    assert "detail.textContent = diagnosticsText(" in script

    for text in (
        "데이터 없음",
        "데이터 안 옴 · 마지막 수신 ${seconds}초 전",
        "수신 중 · ${Math.round(diagnostics.packets_per_sec || 0)} pkt/s",
        "형식 ${format} · 패킷 ${packetId} · 길이 ${length}",
    ):
        assert text in script


def test_state_api_has_fields_radio_page_renders() -> None:
    payload = _client().get("/api/state").json()

    connection = payload["connection"]
    assert "data_age_ms" in connection
    assert "stale" in connection

    diagnostics = payload["snapshot"]["diagnostics"]
    for key in ("packets_per_sec", "last_format", "last_packet_id", "last_len"):
        assert key in diagnostics
