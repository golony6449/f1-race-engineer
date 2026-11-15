"""서킷별 마크다운 지식 베이스(KB).

`f1re/tracks/kb/*.md`는 LLM 무전 프롬프트와 규칙 엔진이 참고하는 코너별
공략·방어 설명이다. 문서는 `## 코너 <번호>: <이름>` 형식의 2단계 제목으로
섹션을 나누며, 로더가 코너 번호로 그 섹션만 꺼낸다. 트랙 ID와 파일 이름의
연결은 `_KB_FILES`에 둔다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from types import MappingProxyType
from typing import Iterable, Mapping

from f1re.tracks.model import TrackDataError

_KB_PACKAGE = "f1re.tracks.kb"

# 트랙 ID → KB 파일 이름. 파일 이름은 사람이 읽기 쉬운 슬러그로 두고, 게임 트랙
# ID와는 여기서 잇는다(스즈카 13). KB가 없는 트랙은 이 표에 넣지 않는다.
_KB_FILES: dict[int, str] = {
    13: "suzuka.md",
}

# 코너 섹션 제목: "## 코너 16: 카시오 시케인 진입". 콜론은 전각도 받는다.
_CORNER_HEADING = re.compile(r"^##\s*코너\s*(\d+)\s*[:：]")


@dataclass(frozen=True)
class TrackKB:
    """서킷 하나의 코너별 마크다운 KB.

    `sections`는 코너 번호 → 섹션 문자열(제목 포함)이다. 2단계 제목이 코너
    섹션이 아닌 다른 제목(요약·DRS 등)을 만나면 그 앞에서 섹션이 끝난다.
    """

    track_id: int
    sections: Mapping[int, str]

    def get_corner_section(self, corner_number: int) -> str | None:
        """코너 번호의 섹션을 돌려준다. 없는 번호면 `None`."""
        return self.sections.get(corner_number)

    def get_corner_sections(self, corner_numbers: Iterable[int]) -> tuple[str, ...]:
        """여러 코너의 섹션을 요청한 순서대로 돌려준다. 없는 코너는 건너뛴다."""
        sections: list[str] = []
        for corner_number in corner_numbers:
            section = self.sections.get(corner_number)
            if section is not None:
                sections.append(section)
        return tuple(sections)

    def render(self, corner_numbers: Iterable[int] | None = None) -> str:
        """LLM 프롬프트에 넣을 섹션 묶음을 만든다.

        코너 번호를 주면 그 코너들만 요청한 순서대로, 주지 않으면 전체 코너를
        번호 순서대로 이어 붙인다.
        """
        if corner_numbers is None:
            numbers = sorted(self.sections)
        else:
            numbers = list(corner_numbers)
        return "\n\n".join(self.get_corner_sections(numbers))


def load_track_kb(track_id: int) -> TrackKB | None:
    """트랙 ID의 마크다운 KB를 읽는다. KB가 없는 트랙이면 `None`."""
    filename = _KB_FILES.get(track_id)
    if filename is None:
        return None
    try:
        text = (
            resources.files(_KB_PACKAGE)
            .joinpath(filename)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise TrackDataError(
            f"트랙 {track_id} KB 파일을 읽지 못했습니다: {filename}"
        ) from exc
    sections = _parse_sections(text, filename)
    return TrackKB(track_id=track_id, sections=MappingProxyType(sections))


def load_corner_kb(track_id: int, corner_number: int) -> str | None:
    """트랙 ID와 코너 번호의 KB 섹션을 돌려준다.

    KB가 없는 트랙이거나 그 코너 섹션이 없으면 `None`이다.
    """
    track_kb = load_track_kb(track_id)
    if track_kb is None:
        return None
    return track_kb.get_corner_section(corner_number)


def _parse_sections(text: str, filename: str) -> dict[int, str]:
    """마크다운을 코너 번호별 섹션으로 나눈다.

    `## 코너 <번호>:` 줄에서 섹션이 시작하고, 다음 2단계 제목(`## `)이나 문서
    끝에서 끝난다. 코너 번호가 중복되면 데이터 오류로 본다.
    """
    sections: dict[int, list[str]] = {}
    current: int | None = None
    for line in text.splitlines():
        heading = _CORNER_HEADING.match(line)
        if heading is not None:
            number = int(heading.group(1))
            if number in sections:
                raise TrackDataError(f"{filename}: {number}번 코너 섹션이 중복됩니다")
            current = number
            sections[number] = [line]
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is not None:
            sections[current].append(line)

    if not sections:
        raise TrackDataError(f"{filename}: '## 코너 <번호>:' 섹션이 없습니다")
    return {
        number: "\n".join(lines).strip() for number, lines in sections.items()
    }


__all__ = ["TrackKB", "load_corner_kb", "load_track_kb"]
