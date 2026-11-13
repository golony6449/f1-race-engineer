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

    css_response = client.get("/static/style.css")
    assert css_response.status_code == 200
    css = css_response.text
    assert ".car-view" in css
    assert ".car-svg .tyre-shape" in css
    assert ".car-svg .brake-disc" in css
    # 좁은 화면에서는 상자를 그림 위/아래로 접어 가로 스크롤을 만들지 않는다.
    assert "@media (max-width: 720px)" in css
