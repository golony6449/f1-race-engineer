"""SCRUM-77: 타이어·브레이크를 F1 차 그림 하나로 보여 주는 대시보드.

공용 conftest를 만들지 않는 규칙에 따라 도우미는 이 파일 안에 둔다.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from f1re.state.store import StateStore
from f1re.web.app import create_app

TYRE_POSITIONS = {"front_left", "front_right", "rear_left", "rear_right"}


def _client() -> TestClient:
    return TestClient(create_app(StateStore()))


def _dashboard_html() -> str:
    response = _client().get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def test_dashboard_combines_tyres_and_brakes_into_one_section() -> None:
    html = _dashboard_html()

    # 두 패널(타이어·브레이크)을 한 섹션으로 합쳤다.
    assert 'id="tyres-brakes"' in html
    assert "타이어·브레이크" in html
    assert 'id="tyres"' not in html
    assert 'id="brakes"' not in html

    # 위에서 본 차 그림은 외부 이미지가 아니라 인라인 SVG로 직접 그린다.
    svg = re.search(r"<svg\b[^>]*class=\"car-svg\".*?</svg>", html, re.S)
    assert svg, "대시보드에 차 그림 SVG가 없습니다"
    svg_text = svg.group(0)
    assert 'viewBox="0 0 400 690"' in svg_text
    assert "앞날개" in svg_text
    assert "콕핏" in svg_text
    assert "사이드포드" in svg_text
    assert "뒷날개" in svg_text

    # 네 바퀴 자리에 타이어 사각형과 안쪽 브레이크 디스크 원이 있다.
    tyre_shapes = re.findall(
        r'class="tyre-shape"\s+data-tyre="([a-z_]+)"', svg_text
    )
    brake_discs = re.findall(
        r'class="brake-disc" data-brake="([a-z_]+)"', svg_text
    )
    assert set(tyre_shapes) == TYRE_POSITIONS
    assert set(brake_discs) == TYRE_POSITIONS

    # 정보 상자는 바퀴마다 하나씩 있고, carstate.js가 여기에 값을 채운다.
    boxes = re.findall(r'data-tyre-box="([a-z_]+)"', html)
    assert set(boxes) == TYRE_POSITIONS


def test_statusbar_merges_voice_and_radio_page_into_one_cell() -> None:
    html = _dashboard_html()

    # 음성·무전 페이지 두 칸을 "무전" 한 칸으로 합쳤다.
    assert 'id="status-radio"' in html
    assert 'id="status-voice"' not in html
    assert 'id="status-radio-page"' not in html
    assert html.count('class="status"') == 2

    # 한 칸 안에 두 상태를 함께 보여 주고, 아래에 출처를 적는다.
    assert len(re.findall(r'data-field="radio"', html)) == 1
    assert "음성 오프라인 · 무전 페이지 미연결" in html
    assert "Qwen TTS · 게임 PC /radio" in html


def test_statusbar_links_to_radio_page() -> None:
    """SCRUM-79: 무전 칸에서 무전 페이지(/radio)를 새 탭으로 연다."""
    html = _dashboard_html()
    link = re.search(
        r'<a\b[^>]*href="/radio"[^>]*>\s*무전 페이지 열기\s*</a\s*>', html, re.S
    )
    assert link, "무전 페이지로 가는 버튼이 없습니다"
    assert 'target="_blank"' in link.group(0)
    assert 'rel="noopener"' in link.group(0)

    # 상태 글자 오른쪽(좁은 화면에서는 아래)에 작게 붙는 버튼 모양이다.
    css = _client().get("/static/style.css").text
    assert ".status-line" in css
    assert ".status-button" in css


def test_dashboard_ers_section_shows_usage_power_and_harvest() -> None:
    """SCRUM-86: ERS에 이번 랩 사용량·MGU-K 출력·MGU-K/H 회수량을 더한다.

    칸은 다른 섹션처럼 carstate.js가 만들므로, 대시보드가 서빙하는 스크립트에
    새 셀 키와 표시 단위 변환(J→MJ, W→kW), 기준값 막대가 있는지 확인한다.
    """
    client = _client()
    html = client.get("/").text
    assert 'id="ers"' in html

    script = client.get("/static/carstate.js").text
    for key, label in (
        ("deployed_this_lap", "이번 랩 사용"),
        ("mguk_power", "MGU-K 출력"),
        ("harvested_mguk", "MGU-K 회수"),
        ("harvested_mguh", "MGU-H 회수"),
    ):
        assert f'["{key}", "{label}"]' in script

    # 내부 단위는 게임 값 그대로 두고 화면에서만 MJ·kW로 바꾼다.
    assert 'J: { unit: "MJ"' in script
    assert 'W: { unit: "kW"' in script
    # 사용량·출력은 한도(서버가 준 `max`) 대비 막대를 그린다.
    assert "cell-bar-fill" in script
    assert "data.max" in script

    css = client.get("/static/style.css").text
    assert ".cell-bar" in css
    assert ".cell-bar-fill" in css
    assert "#dashboard.stale .cell-bar-fill" in css

    # 좁은 화면에서는 ERS 여섯 칸을 한 열로 쌓는다.
    phone = re.search(r"@media \(max-width: 480px\) \{(.*?)\n\}", css, re.S)
    assert phone, "휴대폰 세로(480px 이하)용 규칙이 없습니다"
    assert ".ers .cell-grid {" in phone.group(1)
    assert "grid-template-columns: 1fr;" in phone.group(1)


def test_car_section_static_assets_are_served() -> None:
    client = _client()

    script_response = client.get("/static/carstate.js")
    assert script_response.status_code == 200
    script = script_response.text

    # 기존 셀 id 규칙을 유지한다(타이어 4값·브레이크 온도).
    assert "tyre.${tyre.position}.wear" in script
    assert "tyre.${tyre.position}.surface_temp" in script
    assert "tyre.${tyre.position}.inner_temp" in script
    assert "tyre.${tyre.position}.pressure" in script
    assert "brake.${brake.position}.temp" in script
    # 바퀴 색은 서버가 준 단계를 색 표(this.colors)의 키로만 고른다.
    assert "this.colors" in script
    assert "this.tyreShapes" in script
    assert "this.brakeDiscs" in script
    # 무전 칸은 음성·무전 페이지를 한 값으로 합쳐 정상/경고 색을 고른다.
    assert '[data-field="radio"]' in script
    assert "음성 온라인" in script
    assert "무전 페이지 연결" in script

    css_response = client.get("/static/style.css")
    assert css_response.status_code == 200
    css = css_response.text
    assert ".car-view" in css
    assert ".car-svg .tyre-shape" in css
    assert ".car-svg .brake-disc" in css
    # 정상/경고 색은 상태줄 클래스가 칠한다.
    assert ".status-value.ok" in css
    assert ".status-value.warn" in css
    # 좁은 화면에서는 상자를 그림 위/아래로 접어 가로 스크롤을 만들지 않는다.
    assert "@media (max-width: 720px)" in css


def _wide_screen_css() -> str:
    """`style.css`에서 1100px 이상 넓은 화면 규칙 묶음만 꺼낸다."""
    css = _client().get("/static/style.css").text
    match = re.search(r"@media \(min-width: 1100px\) \{(.*?)\n\}", css, re.S)
    assert match, "넓은 화면(1100px 이상)용 규칙이 없습니다"
    return match.group(1)


def test_map_section_has_flag_status_row() -> None:
    """SCRUM-80: 서킷 지도 제목 아래 레드·블루·옐로·SC·VSC 다섯 칸."""
    client = _client()
    html = client.get("/").text

    row = re.search(
        r'<div class="flag-row"[^>]*>(.*?)</div>', html, re.S
    )
    assert row, "서킷 지도 제목 아래 깃발 상태 줄이 없습니다"
    chips = re.findall(r'data-flag="([a-z]+)"', row.group(1))
    assert chips == ["red", "blue", "yellow", "sc", "vsc"]
    for label in ("레드", "블루", "옐로", "SC", "VSC"):
        assert f">{label}</span>" in row.group(1)

    # 꺼짐은 회색 테두리, 켜짐은 깃발 색인 클래스가 있다.
    css = client.get("/static/style.css").text
    assert ".flag-row" in css
    assert ".flag-chip" in css
    assert '.flag-chip.on[data-flag="red"]' in css
    assert '.flag-chip.on[data-flag="blue"]' in css
    assert '.flag-chip.on[data-flag="yellow"]' in css
    assert '.flag-chip.on[data-flag="sc"]' in css
    assert '.flag-chip.on[data-flag="vsc"]' in css

    # map.js가 서버의 flags로 칸을 켜고, 옐로 구역을 지도 선 위에 칠한다.
    script = client.get("/static/map.js").text
    assert 'querySelectorAll("[data-flag]")' in script
    assert "yellow_zones" in script
    assert "_drawYellowZones" in script


def test_map_title_and_flag_row_share_one_head_line() -> None:
    """SCRUM-88: 서킷 지도 제목과 깃발 칩을 한 줄 머리 요소에 둔다."""
    client = _client()
    html = client.get("/").text

    head = re.search(
        r'<div class="map-head">(.*?)\s*</div>\s*<div class="map-wrap"',
        html,
        re.S,
    )
    assert head, "서킷 지도 제목과 깃발 줄을 묶은 머리 요소가 없습니다"
    head_html = head.group(1)
    # 제목이 왼쪽(먼저), 깃발 줄이 오른쪽(다음)에 온다.
    assert "<h2>서킷 지도</h2>" in head_html
    assert 'class="flag-row"' in head_html
    assert head_html.index("<h2>서킷 지도</h2>") < head_html.index(
        'class="flag-row"'
    )
    # 깃발 칩 다섯 개와 기존 id는 그대로다.
    assert 'id="flag-row"' in head_html
    chips = re.findall(r'data-flag="([a-z]+)"', head_html)
    assert chips == ["red", "blue", "yellow", "sc", "vsc"]

    # 머리 줄은 양 끝 정렬·세로 가운데, 좁으면 줄바꿈한다.
    # 깃발 줄은 스스로 아래 여백을 두지 않는다.
    css = client.get("/static/style.css").text
    head_rule = re.search(r"\n\.map-head \{(.*?)\}", css, re.S)
    assert head_rule, ".map-head 규칙이 없습니다"
    rule = head_rule.group(1)
    assert "display: flex;" in rule
    assert "flex-wrap: wrap;" in rule
    assert "align-items: center;" in rule
    assert "justify-content: space-between;" in rule

    flag_rule = re.search(r"\n\.flag-row \{(.*?)\}", css, re.S)
    assert flag_rule
    assert "margin: 0;" in flag_rule.group(1)


def test_map_fills_remaining_panel_height() -> None:
    """SCRUM-88: 지도 패널은 세로 flex, 지도 칸이 남는 높이를 모두 쓴다."""
    client = _client()
    css = client.get("/static/style.css").text

    map_rule = re.search(r"\n\.map \{(.*?)\}", css, re.S)
    assert map_rule, ".map 패널 규칙이 없습니다"
    rule = map_rule.group(1)
    assert "display: flex;" in rule
    assert "flex-direction: column;" in rule

    # 1열(좁은 화면)에서는 기존 고정 높이를 그대로 쓴다.
    wrap_rule = re.search(r"\n\.map-wrap \{(.*?)\}", css, re.S)
    assert wrap_rule
    assert "flex: 1 1 auto;" in wrap_rule.group(1)
    assert "min-height: 220px;" in wrap_rule.group(1)
    assert "height: clamp(220px, 36vh, 440px);" in wrap_rule.group(1)

    # 넓은 화면에서는 높이를 풀어 그리드 행 stretch로 타이어 패널과 같은
    # 높이가 되고, 지도가 그 안의 남는 높이를 모두 쓴다.
    wide = _wide_screen_css()
    tyres_rule = re.search(r"\.tyres-brakes \{(.*?)\}", wide, re.S)
    assert tyres_rule, "타이어 패널 높이 규칙이 없습니다"
    assert "height: clamp(400px, 46vh, 450px);" in tyres_rule.group(1)

    wide_wrap = re.search(r"\.map \.map-wrap \{(.*?)\}", wide, re.S)
    assert wide_wrap
    assert "height: auto;" in wide_wrap.group(1)

    # 지도 최소 높이를 지키려면 코너 지표 칸이 그 안에서 스크롤한다.
    wide_info = re.search(r"\.map \.map-info \{(.*?)\}", wide, re.S)
    assert wide_info
    assert "min-height: 0;" in wide_info.group(1)
    assert "overflow-y: auto;" in wide_info.group(1)

    # 캔버스 크기가 바뀌면 회전·맞춤을 다시 계산한다(ResizeObserver).
    script = client.get("/static/map.js").text
    assert "ResizeObserver" in script
    assert ".observe(this.root)" in script


def test_wide_screen_puts_tyres_and_map_side_by_side() -> None:
    """SCRUM-79: 넓은 화면에서 타이어·브레이크와 지도를 좌우 반반으로 둔다."""
    block = _wide_screen_css()

    # 12칼럼 그리드에서 두 섹션이 6칼럼씩(50%) 차지해 한 줄에 선다.
    assert ".tyres-brakes," in block
    assert ".map {" in block
    assert "grid-column: span 6;" in block
    # 두 섹션 높이를 같게 묶어 화면을 가득 채우지 않는다(약 400~450px).
    assert "height: clamp(400px, 46vh, 450px);" in block
    # 반 폭에서도 차 그림과 값 여섯 개가 다 보이게 줄인다.
    assert ".tyres-brakes .car-svg" in block
    assert "height: clamp(260px, 32vh, 320px);" in block
    assert ".tyres-brakes .cell-value" in block


def test_pages_and_static_assets_disable_cache() -> None:
    """SCRUM-79: 업데이트 뒤 예전 JS/CSS를 쓰지 않도록 재검증을 요구한다."""
    client = _client()

    for path in ("/", "/radio", "/static/dashboard.js", "/static/style.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == "no-cache", path

    # 상태 API는 화면 캐시 정책과 무관하므로 그대로 둔다.
    api = client.get("/api/state")
    assert api.status_code == 200
    assert "no-cache" not in api.headers.get("cache-control", "").lower()


def test_dashboard_has_engine_settings_button_and_modal() -> None:
    """SCRUM-82: 우측 하단 톱니바퀴 버튼 → 레이스 엔지니어 ON/OFF 모달."""
    client = _client()
    html = client.get("/").text

    fab = re.search(
        r'<button\b[^>]*id="engine-settings"[^>]*>.*?</button>', html, re.S
    )
    assert fab, "우측 하단 설정 플로팅 버튼이 없습니다"
    assert 'aria-label="설정"' in fab.group(0)
    # 아이콘은 외부 이미지가 아니라 인라인 SVG다.
    assert "<svg" in fab.group(0)

    # 모달: 제목, ON/OFF 토글, 상태 설명, 닫기.
    assert 'id="engine-modal"' in html
    assert 'id="engine-modal-title"' in html
    assert "레이스 엔지니어" in html
    assert 'id="engine-toggle"' in html
    assert 'role="switch"' in html
    assert "꺼짐: UDP 수신·무전·분석이 모두 멈춥니다" in html
    assert 'id="engine-close"' in html
    assert "닫기" in html

    # OFF면 본문 대신 안내만 보여 준다.
    assert "레이스 엔지니어가 꺼져 있습니다." in html
    assert "우측 아래 설정에서 켜세요." in html

    js = client.get("/static/dashboard.js").text
    assert '"/api/engine"' in js
    assert 'payload.type === "engine"' in js
    # OFF에서는 WebSocket을 열지 않는다.
    assert 'if (engineState !== "on") return;' in js

    css = client.get("/static/style.css").text
    assert ".engine-fab" in css
    assert "position: fixed;" in css
    assert ".engine-modal" in css
    assert ".engine-toggle" in css
    assert ".engine-off" in css


def test_radio_page_shows_engine_off_notice() -> None:
    """SCRUM-82: 무전 페이지도 엔진이 꺼져 있으면 '꺼짐'을 안내한다."""
    client = _client()
    html = client.get("/radio").text

    assert 'id="engine-off"' in html
    assert "레이스 엔지니어가 꺼져 있습니다." in html

    js = client.get("/static/radio.js").text
    assert '"/api/engine"' in js
    assert 'case "engine":' in js
    # OFF에서는 연결 버튼과 자동 재연결을 멈춘다.
    assert 'if (engineState !== "on") return;' in js
