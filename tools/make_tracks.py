#!/usr/bin/env python3
"""스즈카·모나코 중심선과 코너 데이터를 만든다.

OpenStreetMap의 서킷 관계 도로 형상(ODbL)을 받아 로컬 미터 좌표로 투영하고,
주요 직선을 곧게 편 뒤 공식 서킷 길이에 맞춰 축척한다. 코너 구간(entry~exit)은
공식 코너 번호 순서에 맞춰 사람이 정한 랩 거리 구간이고, apex는 그 안에서
곡률이 가장 큰 지점으로 정한다.

사용:
    uv run python tools/make_tracks.py
    uv run python tools/make_tracks.py --suzuka-osm .ds-tmp/suzuka_map.osm \
        --monaco-osm .ds-tmp/monaco_map.osm

출처와 데이터 형식은 `src/f1re/tracks/data/README.md`를 본다.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

R_EARTH = 6371000.0
USER_AGENT = (
    "f1re-track-builder/0.1 (open data build; "
    "+https://github.com/golony6449/f1-race-engineer)"
)

SUZUKA_OSM_URL = (
    "https://api.openstreetmap.org/api/0.6/map?bbox=136.515,34.835,136.555,34.855"
)
MONACO_OSM_URL = (
    "https://api.openstreetmap.org/api/0.6/map?bbox=7.415,43.730,7.435,43.745"
)

# 관계 ID와 시작 기준점.
SUZUKA = {
    "relation": "284570",
    "origin": "way:184422099",  # 피트 빌딩 중심(정확한 결승선 노드가 OSM에 없음)
    "reverse": False,
    "length_m": 5807.0,  # 공식 서킷 길이
    "track_id": 13,  # EA F1 25 공식 트랙 ID
    "name": "스즈카 서킷",
    # (시작, 끝) 랩 거리. 끝이 시작보다 작으면 랩을 넘어간다.
    "straights": [(5624.0, 500.0), (3970.0, 4800.0)],
    # 공식 코너 번호 순서(1번부터). (entry_m, exit_m)는 이 스크립트가 만든
    # 최종 좌표계 기준이고, apex는 구간 안 최대 곡률 지점으로 채운다.
    "corners": [
        (1, 510.0, 630.0),  # 1코너(오른쪽)
        (2, 645.0, 810.0),  # 2코너(오른쪽)
        (3, 930.0, 1030.0),  # S자 왼쪽
        (4, 1035.0, 1155.0),  # S자 오른쪽
        (5, 1190.0, 1320.0),  # S자 왼쪽
        (6, 1340.0, 1520.0),  # S자 오른쪽
        (7, 1555.0, 2010.0),  # 던롭(왼쪽, 긴 왼쪽 구간 전체)
        (8, 2140.0, 2215.0),  # 데그너 1(오른쪽)
        (9, 2295.0, 2370.0),  # 데그너 2(오른쪽)
        (10, 2610.0, 2700.0),  # 다리 밑 오른쪽 꺾임
        (11, 2745.0, 2860.0),  # 헤어핀(왼쪽)
        (12, 2900.0, 3510.0),  # 200R(오른쪽, 긴 오른쪽 구간 전체)
        (13, 3645.0, 3745.0),  # 스푼 1(왼쪽)
        (14, 3795.0, 3900.0),  # 스푼 2(왼쪽)
        (15, 4810.0, 4900.0),  # 130R(왼쪽)
        (16, 5240.0, 5298.0),  # 시케인 오른쪽
        (17, 5300.0, 5352.0),  # 시케인 왼쪽
        (18, 5356.0, 5620.0),  # 최종 코너(오른쪽)
    ],
}

MONACO = {
    "relation": "148194",
    "origin": "start-node",
    "reverse": True,  # OSM 관계 방향이 주행 방향의 반대라 뒤집는다.
    "length_m": 3337.0,
    "track_id": 5,
    "name": "모나코 서킷",
    "straights": [(3060.0, 195.0)],
    "corners": [
        (1, 170.0, 245.0),  # 생트 데보트(오른쪽)
        (2, 475.0, 530.0),  # 보 리바주(왼쪽)
        (3, 740.0, 858.0),  # 마스네(왼쪽)
        (4, 868.0, 950.0),  # 카지노(오른쪽)
        (5, 1100.0, 1190.0),  # 미라보 상(오른쪽)
        (6, 1235.0, 1310.0),  # 헤어핀(왼쪽)
        (7, 1315.0, 1390.0),  # 미라보 하(오른쪽)
        (8, 1405.0, 1480.0),  # 포르티에(오른쪽)
        (9, 1500.0, 2020.0),  # 터널(오른쪽, 완만한 오른쪽 구간 전체)
        (10, 2070.0, 2110.0),  # 누벨 시케인(왼쪽)
        (11, 2112.0, 2160.0),  # 누벨 시케인(오른쪽)
        (12, 2350.0, 2430.0),  # 타박(왼쪽)
        (13, 2490.0, 2565.0),  # 수영장 1단(왼쪽)
        (14, 2570.0, 2605.0),  # 수영장 1단(오른쪽)
        (15, 2695.0, 2725.0),  # 수영장 2단(오른쪽)
        (16, 2728.0, 2760.0),  # 수영장 2단(왼쪽)
        (17, 2765.0, 2890.0),  # 이름 없는 왼쪽
        (18, 2895.0, 2985.0),  # 라스카스(오른쪽)
        (19, 2988.0, 3055.0),  # 노게(오른쪽)
    ],
}


class TrackBuildError(Exception):
    """OSM 자료가 예상과 다를 때."""


# ---------------------------------------------------------------------------
# OSM XML 읽기·조립


def load_osm(path: Path):
    root = ET.parse(path).getroot()
    nodes = {}
    for n in root.iter("node"):
        nodes[n.get("id")] = (float(n.get("lat")), float(n.get("lon")))
    ways = {}
    for w in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        nd = [nd.get("ref") for nd in w.findall("nd")]
        ways[w.get("id")] = (tags, nd)
    rels = {}
    for r in root.iter("relation"):
        tags = {t.get("k"): t.get("v") for t in r.findall("tag")}
        members = [
            (m.get("type"), m.get("ref"), m.get("role")) for m in r.findall("member")
        ]
        rels[r.get("id")] = (tags, members)
    return nodes, ways, rels


def geo_dist(a, b):
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * R_EARTH * math.asin(math.sqrt(h))


def assemble_relation(rel_id, nodes, ways, rels, *, skip_roles=("pit_lane",)):
    """관계 멤버 도로를 끝점이 맞닿는 순서로 이어 하나의 고리로 만든다."""
    if rel_id not in rels:
        raise TrackBuildError(f"OSM 관계 {rel_id}가 없습니다")
    _, members = rels[rel_id]
    candidates = []
    seen = set()
    for typ, ref, role in members:
        if typ != "way" or role in skip_roles or ref in seen:
            continue
        seen.add(ref)
        if ref not in ways:
            raise TrackBuildError(f"OSM way {ref}가 데이터에 없습니다")
        geom = [nodes[i] for i in ways[ref][1] if i in nodes]
        if len(geom) >= 2:
            candidates.append((ref, ways[ref][0], geom))

    # 피트 출구처럼 같은 시작·끝을 잇는 평행 도로는 짧은 쪽을 버린다.
    filtered = []
    for ref, tags, geom in candidates:
        duplicate = False
        for ref2, _tags2, geom2 in candidates:
            if ref2 == ref:
                continue
            if geo_dist(geom[0], geom2[0]) < 2 and geo_dist(geom[-1], geom2[-1]) < 2:
                if len(geom) < len(geom2):
                    duplicate = True
        if not duplicate:
            filtered.append((ref, tags, geom))
    candidates = filtered

    start_node = None
    for typ, ref, role in members:
        if typ == "node" and role == "start-finish":
            start_node = nodes.get(ref)
    if start_node is None:
        start_node = candidates[0][2][0]

    best = None
    for ref, _tags, geom in candidates:
        for flipped in (False, True):
            g = list(reversed(geom)) if flipped else geom
            d = geo_dist(start_node, g[0])
            if best is None or d < best[0]:
                best = (d, ref, flipped, g)
    if best is None:
        raise TrackBuildError("서킷을 시작할 도로를 찾지 못했습니다")
    _, ref0, _, g0 = best
    used = {ref0}
    chain = [(p, ref0) for p in g0]
    while len(used) < len(candidates):
        end = chain[-1][0]
        choice = None
        for ref, _tags, geom in candidates:
            if ref in used:
                continue
            for flipped in (False, True):
                g = list(reversed(geom)) if flipped else geom
                d = geo_dist(end, g[0])
                if choice is None or d < choice[0]:
                    choice = (d, ref, flipped, g)
        if choice is None:
            break
        d, ref, _flipped, g = choice
        if d > 50:
            raise TrackBuildError(f"도로 연결이 끊어졌습니다(gap {d:.1f}m, way {ref})")
        used.add(ref)
        chain.extend((p, ref) for p in g[1:])
    if len(used) != len(candidates):
        raise TrackBuildError(
            f"서킷 고리를 다 잇지 못했습니다: {len(used)}/{len(candidates)} 도로"
        )
    return chain


# ---------------------------------------------------------------------------
# 기하 처리


def project_xy(lat, lon, lat0, lon0):
    coslat = math.cos(math.radians(lat0))
    return (
        math.radians(lon - lon0) * R_EARTH * coslat,
        math.radians(lat - lat0) * R_EARTH,
    )


def project_chain(chain):
    lat0 = sum(p[0] for p, _ in chain) / len(chain)
    lon0 = sum(p[1] for p, _ in chain) / len(chain)
    pts = [project_xy(lat, lon, lat0, lon0) for (lat, lon), _ in chain]
    return pts, lat0, lon0


def cumulative(pts):
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + math.dist(pts[i - 1], pts[i]))
    return cum


def rotate_to(pts, origin_xy):
    """원점에 가장 가까운 선분 위 점을 찾아 그 점이 거리 0이 되게 회전한다."""
    best = None
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        ab = (b[0] - a[0], b[1] - a[1])
        seg2 = ab[0] ** 2 + ab[1] ** 2
        t = 0.0
        if seg2 > 0:
            t = ((origin_xy[0] - a[0]) * ab[0] + (origin_xy[1] - a[1]) * ab[1]) / seg2
            t = max(0.0, min(1.0, t))
        p = (a[0] + t * ab[0], a[1] + t * ab[1])
        d = math.dist(origin_xy, p)
        if best is None or d < best[0]:
            best = (d, i, t, p)
    if best is None:
        raise TrackBuildError("시작점을 중심선에 맞추지 못했습니다")
    _, i, _t, p = best
    return [(p[0], p[1])] + pts[i + 1 :] + pts[: i + 1] + [p]


def resample(pts, step_m):
    cum = cumulative(pts)
    total = cum[-1]
    n = max(2, int(round(total / step_m)))
    out = []
    j = 0
    for k in range(n):
        s = k * total / n
        while j < len(cum) - 2 and cum[j + 1] < s:
            j += 1
        seg = cum[j + 1] - cum[j]
        t = 0.0 if seg == 0 else (s - cum[j]) / seg
        a, b = pts[j], pts[j + 1]
        out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return out


def smooth_loop(pts, radius_m=10.0, passes=2, step_m=2.0):
    """OSM 도로 조각 사이 작은 단차·꺾임을 둥글게 만든다."""
    k = max(1, int(round(radius_m / step_m)))
    for _ in range(passes):
        n = len(pts)
        smoothed = []
        for i in range(n):
            sx = sz = 0.0
            for j in range(-k, k + 1):
                x, z = pts[(i + j) % n]
                sx += x
                sz += z
            smoothed.append((sx / (2 * k + 1), sz / (2 * k + 1)))
        pts = smoothed
    return pts


def straighten(pts, start_m, end_m):
    """[start_m, end_m] 구간을 두 끝점을 잇는 직선으로 바꾼다(랩 넘김 지원)."""
    cum = cumulative(pts)
    total = cum[-1]
    if not (0.0 <= start_m <= total) or not (0.0 <= end_m <= total):
        raise TrackBuildError(f"직선 구간이 랩 범위를 벗어났습니다: {start_m}~{end_m}")
    wrapped = end_m < start_m
    if wrapped:
        # 랩을 넘는 구간은 경로 순서대로 (시작 이후) + (처음부터 끝까지)를 잇는다.
        idx = [i for i, s in enumerate(cum) if s >= start_m] + [
            i for i, s in enumerate(cum) if s <= end_m
        ]
    else:
        idx = [i for i, s in enumerate(cum) if start_m <= s <= end_m]
    if len(idx) < 2:
        raise TrackBuildError(f"직선 구간에 점이 부족합니다: {start_m}~{end_m}")
    ia, ib = idx[0], idx[-1]
    a, b = pts[ia], pts[ib]
    count = len(idx)
    out = list(pts)
    for k, i in enumerate(idx):
        t = k / (count - 1)
        out[i] = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    return out


def scale_to(pts, target_length_m):
    total = cumulative(pts)[-1]
    factor = target_length_m / total
    return [(x * factor, z * factor) for x, z in pts]


def headings_and_curvature(pts, step_m, half=2, smooth_k=4):
    """각 점의 진행 방향과 곡률(1/m, 왼쪽 양수)."""
    n = len(pts)
    headings = []
    for i in range(n):
        a = pts[(i - half) % n]
        b = pts[(i + half) % n]
        headings.append(math.atan2(b[1] - a[1], b[0] - a[0]))
    raw = []
    for i in range(n):
        delta = headings[(i + 1) % n] - headings[i]
        while delta > math.pi:
            delta -= 2 * math.pi
        while delta < -math.pi:
            delta += 2 * math.pi
        raw.append(delta / step_m)
    curvature = []
    for i in range(n):
        total = 0.0
        for j in range(-smooth_k, smooth_k + 1):
            total += raw[(i + j) % n]
        curvature.append(total / (2 * smooth_k + 1))
    return headings, curvature


def closest_index(cum, dist_m):
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] < dist_m:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------------------
# 빌드


def build_track(spec, nodes, ways, rels):
    chain = assemble_relation(spec["relation"], nodes, ways, rels)
    if spec["reverse"]:
        chain = list(reversed(chain))
    pts, lat0, lon0 = project_chain(chain)

    origin_spec = spec["origin"]
    if origin_spec == "start-node":
        origin = None
        for typ, ref, role in rels[spec["relation"]][1]:
            if typ == "node" and role == "start-finish":
                origin = project_xy(*nodes[ref], lat0, lon0)
                break
        if origin is None:
            raise TrackBuildError("start-finish 노드가 없습니다")
    elif origin_spec.startswith("way:"):
        way_id = origin_spec.split(":", 1)[1]
        if way_id not in ways:
            raise TrackBuildError(f"기준 way {way_id}가 없습니다")
        geom = [nodes[i] for i in ways[way_id][1] if i in nodes]
        if not geom:
            raise TrackBuildError(f"기준 way {way_id}의 점이 없습니다")
        lat = sum(p[0] for p in geom) / len(geom)
        lon = sum(p[1] for p in geom) / len(geom)
        origin = project_xy(lat, lon, lat0, lon0)
    else:
        raise TrackBuildError(f"알 수 없는 시작 기준: {origin_spec}")

    pts = rotate_to(pts, origin)
    pts = resample(pts, 2.0)
    pts = smooth_loop(pts, radius_m=10.0, passes=2, step_m=2.0)
    pts = scale_to(pts, spec["length_m"])
    for start, end in spec["straights"]:
        pts = straighten(pts, start, end)
    # 직선화로 줄어든 길이를 다시 공식 길이에 맞춘다(모양만 아주 조금 늘어난다).
    pts = scale_to(pts, spec["length_m"])
    pts = resample(pts, 5.0)

    cum = cumulative(pts)
    total = cum[-1]
    if abs(total - spec["length_m"]) / spec["length_m"] > 0.01:
        raise TrackBuildError(
            f"{spec['name']}: 중심선 길이 {total:.1f}m가 공식 길이와 1% 넘게 다릅니다"
        )
    _, curvature = headings_and_curvature(pts, 5.0)
    n = len(pts)

    corners = []
    previous_exit = -1.0
    for number, entry, exit_ in spec["corners"]:
        if not (0.0 <= entry < exit_ <= total):
            raise TrackBuildError(
                f"{spec['name']} {number}번: 구간 {entry}~{exit_}가 랩 범위를 벗어납니다"
            )
        if entry < previous_exit:
            raise TrackBuildError(
                f"{spec['name']} {number}번: 앞 코너 구간과 겹칩니다({entry} < {previous_exit})"
            )
        ia = closest_index(cum, entry)
        ib = closest_index(cum, exit_)
        if ia >= ib:
            raise TrackBuildError(f"{spec['name']} {number}번: 구간에 점이 부족합니다")
        # apex는 구간 양 끝에서 조금 떨어진 안쪽에서 곡률이 가장 큰 점으로 둔다.
        span = exit_ - entry
        margin = min(max(1.0, span * 0.15), span * 0.4)
        lo = max(ia, closest_index(cum, entry + margin))
        hi = min(ib, closest_index(cum, exit_ - margin))
        if hi <= lo:
            lo, hi = ia, ib
        apex_index = max(range(lo, hi + 1), key=lambda i: abs(curvature[i % n]))
        apex = cum[apex_index]
        if not (entry < apex < exit_):
            raise TrackBuildError(f"{spec['name']} {number}번: apex를 정하지 못했습니다")
        corners.append(
            {
                "number": number,
                "entry_m": round(entry, 2),
                "apex_m": round(apex, 2),
                "exit_m": round(exit_, 2),
            }
        )
        previous_exit = exit_

    centerline = [
        [round(cum[i], 3), round(pts[i][0], 3), round(pts[i][1], 3)] for i in range(n)
    ]
    return {
        "track_id": spec["track_id"],
        "name": spec["name"],
        "length_m": spec["length_m"],
        "corners": corners,
        "centerline": centerline,
    }


def download_osm(url: str, path: Path) -> None:
    print(f"OSM 내려받기: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
        data = response.read()
    if not data.startswith(b"<?xml"):
        raise TrackBuildError(f"OSM 응답이 XML이 아닙니다: {url}")
    path.write_bytes(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suzuka-osm", type=Path, help="스즈카 OSM XML 경로(없으면 내려받기)")
    parser.add_argument("--monaco-osm", type=Path, help="모나코 OSM XML 경로(없으면 내려받기)")
    parser.add_argument(
        "--out",
        type=Path,
        default=(
            Path(__file__).resolve().parents[1]
            / "src"
            / "f1re"
            / "tracks"
            / "data"
        ),
        help="JSON을 쓸 폴더",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="f1re-tracks-") as tmp:
        tmp_path = Path(tmp)
        suzuka_path = args.suzuka_osm
        if suzuka_path is None:
            suzuka_path = tmp_path / "suzuka.osm"
            download_osm(SUZUKA_OSM_URL, suzuka_path)
        monaco_path = args.monaco_osm
        if monaco_path is None:
            monaco_path = tmp_path / "monaco.osm"
            download_osm(MONACO_OSM_URL, monaco_path)

        args.out.mkdir(parents=True, exist_ok=True)
        for spec, osm_path, filename in (
            (SUZUKA, suzuka_path, "suzuka.json"),
            (MONACO, monaco_path, "monaco.json"),
        ):
            nodes, ways, rels = load_osm(osm_path)
            track = build_track(spec, nodes, ways, rels)
            target = args.out / filename
            target.write_text(
                json.dumps(track, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
            length = track["centerline"][-1][0]
            print(
                f"{filename}: 트랙 ID {track['track_id']}, 코너 {len(track['corners'])}개, "
                f"중심선 점 {len(track['centerline'])}개, 마지막 거리 {length:.1f}m"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
