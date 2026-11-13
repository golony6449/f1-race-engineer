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
