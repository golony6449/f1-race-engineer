/**
 * 서킷 지도(설계 6.9-2).
 *
 * 서버가 준 것만 그린다. 화면은 트랙 윤곽·코너·차 위치를 계산하지 않는다.
 *
 * - 윤곽: `/api/track`의 `outline`(랩 거리 순 `[dist_m, x, z]`).
 * - 차 위치: `/ws/state`의 `cars`. 서버가 랩 거리로 윤곽 위를 보간해 준다
 *   (Motion 패킷이 없어도 그릴 수 있게).
 * - 팀 색: `/api/track`의 `team_colors`. 모르는 팀은 회색.
 * - 내 차는 흰 테두리, 앞차(추월 대상)는 노란 점선 테두리로 강조한다.
 * - SC·VSC·적기면 트랙 선 색을 바꾼다.
 * - 깃발 줄(SCRUM-80)은 `/ws/state`의 `flags`만 보고 켜고 끈다. 옐로 구역
 *   (`yellow_zones`, 랩 거리 비율)은 트랙 선 위에 노랗게 덧칠한다.
 * - 서킷 모양이 캔버스에 가장 크게 들어가도록 윤곽을 0~179도에서 돌려 본
 *   뒤 각도·배율·이동을 하나의 변환으로 만든다. 윤곽·차 점·코너·옐로 구역·
 *   클릭 판정이 모두 이 변환을 쓴다(코너 번호 글자는 똑바로 그린다).
 *   캔버스 크기가 바뀌면(창·패널 높이 변화) 회전·맞춤을 다시 계산한다.
 * - 코너를 누르면 최근 지표(나 / 기준)를 보여 준다. 지표는 작업 15·16이
 *   채우며, 아직 없으면 자리 표시 문구를 보여 준다.
 */

const FLAG_STYLES = {
  none: { line: "#3a4658", label: "" },
  safety_car: { line: "#f2c200", label: "세이프티 카" },
  vsc: { line: "#e0a800", label: "VSC" },
  red_flag: { line: "#e10600", label: "적기" },
  formation: { line: "#9aa7b8", label: "포메이션 랩" },
};

const FALLBACK_TEAM_COLOR = "#8b97a8";
const CORNER_HIT_PX = 16;
const MAP_PADDING_PX = 28;
// 옐로 구역을 트랙 선 위에 덧칠하는 색(SCRUM-80).
const YELLOW_ZONE_COLOR = "#ffd166";
// 서킷을 가장 크게 넣는 회전 각도 후보(0~179도, 1도 간격).
const ROTATION_STEP_DEG = 1;
const ROTATION_LIMIT_DEG = 180;

// 리프트 앤 코스트 추천 구간(SCRUM-71)은 청록 점선 고리로 최소 강조한다.
// 추천은 서버(`/api/lift-coast`)가 계산하고 화면은 그리기만 한다.
const LIFT_COAST_COLOR = "#22d3ee";
const LIFT_COAST_REFRESH_MS = 15000;

const METRIC_LABELS = {
  brake_start_m: "브레이크 지점",
  brake_peak: "최대 브레이크",
  trail_brake_residual: "트레일 브레이크",
  turn_in_m: "턴인 지점",
  steer_peak: "최대 조향",
  min_speed: "최저 속도",
  min_speed_m: "최저 속도 지점",
  steer_corrections: "조향 수정",
  throttle_on_m: "스로틀 지점",
  full_throttle_m: "풀스로틀 지점",
  exit_speed: "탈출 속도",
  max_line_dev_m: "라인 이탈",
  corner_time_ms: "구간 시간",
  loss_ms: "손실",
};

export class TrackMapView {
  /** @param {HTMLElement | null} root `.map-wrap` 요소 */
  constructor(root) {
    this.root = root;
    this.canvas = root ? root.querySelector("canvas") : null;
    this.waiting = root ? root.querySelector(".map-waiting") : null;
    this.badge = root ? root.querySelector(".map-flag") : null;
    this.info = root && root.parentElement
      ? root.parentElement.querySelector(".map-info")
      : null;
    this.ctx = this.canvas ? this.canvas.getContext("2d") : null;

    this.track = null;
    this.cars = [];
    this.flag = "none";
    this.flags = null;
    this.yellowZones = [];
    this.stale = false;
    this.teamColors = {};
    // 같은 서킷·같은 캔버스 크기면 회전 각도 계산을 다시 하지 않는다.
    this.rotationCache = null;
    this.cornerHits = [];
    this.selectedCorner = null;
    // 깃발 상태 줄(SCRUM-80). `.map-wrap` 바깥, 지도 패널 제목 아래에 있다.
    this.flagRow = root && root.parentElement
      ? root.parentElement.querySelector(".flag-row")
      : null;
    // 코너 번호 → 추천 구간(`/api/lift-coast`의 recommended 항목).
    this.liftZones = new Map();
    this.liftTimer = null;

    if (this.canvas) {
      this.canvas.addEventListener("click", (event) => this._onClick(event));
    }
    window.addEventListener("resize", () => this._draw());
    // 창 크기뿐 아니라 패널 높이·코너 지표 길이가 바뀌어도 캔버스 크기가
    // 바뀐다. 크기가 바뀔 때마다 회전·맞춤(`_fit`)을 다시 계산한다.
    if (this.root && typeof ResizeObserver === "function") {
      this.resizeObserver = new ResizeObserver(() => this._draw());
      this.resizeObserver.observe(this.root);
    }
    this._renderInfo(null);
  }

  /** `/api/track` 응답. 윤곽·코너·팀 색을 한 번만 받는다. */
  setTrack(track) {
    this.track = track || null;
    this.teamColors = (track && track.team_colors) || {};
    if (track && track.flag) this.flag = track.flag;
    // 윤곽이 새로 오면 같은 서킷 ID라도 각도를 다시 고른다.
    this.rotationCache = null;
    this.selectedCorner = null;
    this._renderInfo(null);
    this._updateBadge();
    this._draw();
    this._loadLiftCoast();
    if (this.liftTimer === null) {
      this.liftTimer = window.setInterval(
        () => this._loadLiftCoast(),
        LIFT_COAST_REFRESH_MS,
      );
    }
  }

  /** 리프트 앤 코스트 추천 구간. 서버가 고른 구간만 지도에 강조한다. */
  async _loadLiftCoast() {
    try {
      const response = await fetch("/api/lift-coast", { cache: "no-store" });
      if (!response.ok) return;
      const data = await response.json();
      const zones = new Map();
      for (const zone of data.recommended || []) {
        zones.set(Number(zone.corner), zone);
      }
      this.liftZones = zones;
      if (this.selectedCorner) this._renderInfo(this.selectedCorner);
      this._draw();
    } catch (error) {
      // 서버가 아직 안 떴거나 잠깐 실패했다. 다음 주기에 다시 묻는다.
    }
  }

  /** `/ws/state`·`/api/state` 응답. 차 위치와 현재 깃발을 갱신한다. */
  render(payload) {
    if (!payload) return;
    this.cars = payload.cars || [];
    if (payload.flag) this.flag = payload.flag;
    this.flags = payload.flags || null;
    this.yellowZones =
      payload.flags && Array.isArray(payload.flags.yellow_zones)
        ? payload.flags.yellow_zones
        : [];
    this.stale = Boolean(payload.connection && payload.connection.stale);
    if (this.root) this.root.classList.toggle("stale", this.stale);
    this._updateBadge();
    this._renderFlags();
    this._draw();
  }

  // ------------------------------------------------------------------
  // 그리기

  _draw() {
    const ctx = this.ctx;
    const canvas = this.canvas;
    if (!ctx || !canvas) return;

    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (width <= 0 || height <= 0) return;

    const dpr = window.devicePixelRatio || 1;
    const pixelWidth = Math.max(1, Math.round(width * dpr));
    const pixelHeight = Math.max(1, Math.round(height * dpr));
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const outline = (this.track && this.track.outline) || [];
    const hasOutline = outline.length >= 2;
    if (this.waiting) {
      this.waiting.hidden = hasOutline;
      this.waiting.textContent = hasOutline
        ? ""
        : "지도 없음 · 첫 유효 랩의 위치를 기다리는 중";
    }
    if (!hasOutline) {
      this.cornerHits = [];
      return;
    }

    const fit = this._fit(width, height);
    if (!fit) return;

    // 트랙 선. SC·VSC·적기면 색을 바꾼다.
    ctx.save();
    ctx.lineWidth = 9;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.strokeStyle = flagStyle(this.flag).line;
    ctx.beginPath();
    outline.forEach((point, index) => {
      const [x, y] = this._toCanvas(fit, point[1], point[2]);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.stroke();
    ctx.restore();

    // 옐로 구역은 트랙 선 위에 노랗게 덧칠한다(SCRUM-80).
    this._drawYellowZones(ctx, fit, outline);

    this._drawCorners(ctx, fit);
    this._drawCars(ctx, fit);

    if (this.stale) {
      ctx.save();
      ctx.fillStyle = "rgba(232, 237, 245, 0.55)";
      ctx.font = "600 12px system-ui, sans-serif";
      ctx.fillText("데이터 안 옴 · 마지막 위치", 12, 20);
      ctx.restore();
    }
  }

  /**
   * 옐로 구역을 트랙 선 위에 덧칠한다. 구역은 랩 거리 비율(0~1)이므로
   * 서버가 준 랩 길이를 곱해 윤곽 위의 구간으로 자른다.
   */
  _drawYellowZones(ctx, fit, outline) {
    if (!this.yellowZones.length) return;
    const lapLength = this.track ? Number(this.track.length_m) : NaN;
    if (!Number.isFinite(lapLength) || lapLength <= 0) return;

    ctx.save();
    ctx.lineWidth = 9;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.strokeStyle = YELLOW_ZONE_COLOR;
    for (const zone of this.yellowZones) {
      const start = Number(zone && zone.start);
      const end = Number(zone && zone.end);
      if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
      const points = outlineSlice(outline, start * lapLength, end * lapLength);
      if (points.length < 2) continue;
      ctx.beginPath();
      points.forEach(([x, z], index) => {
        const [px, py] = this._toCanvas(fit, x, z);
        if (index === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }
    ctx.restore();
  }

  _drawCorners(ctx, fit) {
    const track = this.track || {};
    const focus = new Set((track.focus_corners || []).map(Number));
    this.cornerHits = [];

    ctx.save();
    for (const corner of track.corners || []) {
      if (!Number.isFinite(corner.x) || !Number.isFinite(corner.z)) continue;
      const [x, y] = this._toCanvas(fit, corner.x, corner.z);
      const selected =
        this.selectedCorner !== null &&
        this.selectedCorner.number === corner.number;
      const highlighted = selected || focus.has(corner.number);
      this.cornerHits.push({ corner, x, y });

      ctx.beginPath();
      ctx.arc(x, y, highlighted ? 5 : 2.6, 0, Math.PI * 2);
      ctx.fillStyle = highlighted ? "#e10600" : "#7f8da3";
      ctx.fill();
      if (selected) {
        ctx.beginPath();
        ctx.arc(x, y, 8.5, 0, Math.PI * 2);
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      // 리프트 앤 코스트 추천 구간: 청록 점선 고리 하나만 더 그린다.
      if (this.liftZones.has(corner.number)) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(x, y, 9.5, 0, Math.PI * 2);
        ctx.setLineDash([2, 3]);
        ctx.strokeStyle = LIFT_COAST_COLOR;
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.restore();
      }

      // 공식 번호를 그대로 쓴다(설계 6.2). 자동 탐지 코너도 번호는 표시한다.
      ctx.font = `${highlighted ? "700 11px" : "600 9.5px"} system-ui, sans-serif`;
      ctx.fillStyle = highlighted ? "#ffffff" : "#aab6c8";
      ctx.fillText(String(corner.number), x + 6, y - 5);
    }
    ctx.restore();
  }

  _drawCars(ctx, fit) {
    // 내 차를 마지막에 그려 다른 차 위에 보이게 한다.
    const ordered = [...this.cars].sort(
      (a, b) => Number(a.is_player) - Number(b.is_player),
    );
    ctx.save();
    for (const car of ordered) {
      if (!Number.isFinite(car.x) || !Number.isFinite(car.z)) continue;
      const [x, y] = this._toCanvas(fit, car.x, car.z);
      const color = this.teamColors[String(car.team_id)] || FALLBACK_TEAM_COLOR;

      if (car.is_target) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(x, y, 9, 0, Math.PI * 2);
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = "#ffd166";
        ctx.lineWidth = 1.6;
        ctx.stroke();
        ctx.restore();
      }

      ctx.beginPath();
      ctx.arc(x, y, car.is_player ? 5.5 : 4, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = "#0b0e13";
      ctx.lineWidth = 1;
      ctx.stroke();

      if (car.is_player) {
        ctx.beginPath();
        ctx.arc(x, y, 8, 0, Math.PI * 2);
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  /**
   * 그리기 변환(회전·배율·이동)을 만든다. 윤곽을 돌려 캔버스에 가장 크게
   * 들어가는 각도를 고르고, 같은 서킷·같은 캔버스 크기면 캐시를 쓴다.
   */
  _fit(width, height) {
    const outline = (this.track && this.track.outline) || [];
    const trackId = this.track ? this.track.track_id : null;
    const key = `${trackId}:${outline.length}:${width}x${height}`;
    if (!this.rotationCache || this.rotationCache.key !== key) {
      this.rotationCache = {
        key,
        rotation: chooseRotation(outline, width, height),
      };
    }
    const rotation = this.rotationCache.rotation;
    if (!rotation) return null;

    const spanX = Math.max(rotation.maxX - rotation.minX, 1);
    const spanZ = Math.max(rotation.maxZ - rotation.minZ, 1);
    const scale = rotation.scale;
    if (!Number.isFinite(scale) || scale <= 0) return null;
    return {
      scale,
      cos: rotation.cos,
      sin: rotation.sin,
      offsetX: (width - spanX * scale) / 2,
      offsetY: (height - spanZ * scale) / 2,
      minX: rotation.minX,
      maxZ: rotation.maxZ,
    };
  }

  _toCanvas(fit, x, z) {
    // 회전한 좌표를 캔버스로 옮긴다. 화면 y는 아래로 증가하므로 z를 뒤집어
    // 지도처럼 보이게 한다.
    const rotatedX = x * fit.cos - z * fit.sin;
    const rotatedZ = x * fit.sin + z * fit.cos;
    return [
      fit.offsetX + (rotatedX - fit.minX) * fit.scale,
      fit.offsetY + (fit.maxZ - rotatedZ) * fit.scale,
    ];
  }

  // ------------------------------------------------------------------
  // 코너 클릭·지표

  _onClick(event) {
    if (!this.canvas) return;
    const rect = this.canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;

    let nearest = null;
    let nearestDistance = CORNER_HIT_PX;
    for (const hit of this.cornerHits) {
      const distance = Math.hypot(hit.x - x, hit.y - y);
      if (distance <= nearestDistance) {
        nearest = hit;
        nearestDistance = distance;
      }
    }
    if (!nearest) return;
    this.selectedCorner = nearest.corner;
    this._renderInfo(nearest.corner);
    this._draw();
  }

  _renderInfo(corner) {
    if (!this.info) return;
    this.info.replaceChildren();
    if (!corner) {
      const hint = document.createElement("p");
      hint.className = "map-hint";
      hint.textContent = "코너를 누르면 최근 지표(나 / 기준)를 보여 줍니다.";
      this.info.append(hint);
      return;
    }

    const title = document.createElement("p");
    title.className = "map-info-title";
    title.textContent = corner.official
      ? `코너 ${corner.number}`
      : `${corner.number}번째 코너`;
    this.info.append(title);

    const range = document.createElement("p");
    range.className = "map-info-range";
    range.textContent = `진입 ${formatMeters(corner.entry_m)} · 정점 ${formatMeters(
      corner.apex_m,
    )} · 탈출 ${formatMeters(corner.exit_m)}`;
    this.info.append(range);

    const lift = this.liftZones.get(corner.number);
    if (lift) {
      const note = document.createElement("p");
      note.className = "map-info-lift";
      note.textContent = `리프트 앤 코스트 추천 · 목표 ${formatMeters(
        lift.target_m,
      )}`;
      this.info.append(note);
    }

    const metrics = (this.track && this.track.corner_metrics
      ? this.track.corner_metrics[String(corner.number)]
      : null);
    if (!metrics) {
      const note = document.createElement("p");
      note.className = "map-hint";
      note.textContent = "최근 지표: 아직 없음 (작업 15·16에서 채웁니다)";
      this.info.append(note);
      return;
    }
    this.info.append(buildMetrics(metrics));
  }

  _updateBadge() {
    if (!this.badge) return;
    const style = flagStyle(this.flag);
    this.badge.textContent = style.label;
    this.badge.hidden = !style.label;
    this.badge.style.color = style.line;
    this.badge.style.borderColor = style.line;
  }

  /** 깃발 줄 다섯 칸을 서버가 준 `flags` 값으로 켜고 끈다. */
  _renderFlags() {
    if (!this.flagRow) return;
    const flags = this.flags || {};
    for (const chip of this.flagRow.querySelectorAll("[data-flag]")) {
      chip.classList.toggle("on", Boolean(flags[chip.dataset.flag]));
    }
  }
}

function flagStyle(flag) {
  return FLAG_STYLES[flag] || FLAG_STYLES.none;
}

/**
 * 윤곽([dist, x, z] 점 배열)을 0~179도로 1도씩 돌려 보고, 캔버스 그리기
 * 영역(가장자리 여백 제외)에 `min(W/w, H/h)`가 가장 큰 각도를 고른다.
 * 캔버스·DOM을 보지 않는 순수 함수다.
 *
 * 반환값의 `cos`·`sin`·`minX`·`maxZ`가 `_toCanvas`의 회전·이동 변환이 된다.
 * 그릴 것이 부족하면 `null`.
 */
export function chooseRotation(
  outline,
  width,
  height,
  padding = MAP_PADDING_PX,
) {
  const points = outlinePoints(outline);
  if (points.length < 2) return null;
  const usableWidth = Math.max(width - padding * 2, 1);
  const usableHeight = Math.max(height - padding * 2, 1);
  if (!(usableWidth > 0) || !(usableHeight > 0)) return null;

  let best = null;
  for (
    let angleDeg = 0;
    angleDeg < ROTATION_LIMIT_DEG;
    angleDeg += ROTATION_STEP_DEG
  ) {
    const radians = (angleDeg * Math.PI) / 180;
    const cos = Math.cos(radians);
    const sin = Math.sin(radians);
    const bounds = rotatedBounds(points, cos, sin);
    const spanX = Math.max(bounds.maxX - bounds.minX, 1);
    const spanZ = Math.max(bounds.maxZ - bounds.minZ, 1);
    const scale = Math.min(usableWidth / spanX, usableHeight / spanZ);
    if (!Number.isFinite(scale)) continue;
    if (!best || scale > best.scale) {
      best = {
        angleDeg,
        cos,
        sin,
        scale,
        minX: bounds.minX,
        maxX: bounds.maxX,
        minZ: bounds.minZ,
        maxZ: bounds.maxZ,
      };
    }
  }
  return best;
}

/** 점들을 회전한 뒤의 가로·세로 범위. */
function rotatedBounds(points, cos, sin) {
  let minX = Infinity;
  let maxX = -Infinity;
  let minZ = Infinity;
  let maxZ = -Infinity;
  for (const [x, z] of points) {
    const rotatedX = x * cos - z * sin;
    const rotatedZ = x * sin + z * cos;
    if (rotatedX < minX) minX = rotatedX;
    if (rotatedX > maxX) maxX = rotatedX;
    if (rotatedZ < minZ) minZ = rotatedZ;
    if (rotatedZ > maxZ) maxZ = rotatedZ;
  }
  return { minX, maxX, minZ, maxZ };
}

/** 윤곽에서 유한한 `[x, z]` 점만 꺼낸다. */
function outlinePoints(outline) {
  const points = [];
  if (!Array.isArray(outline)) return points;
  for (const point of outline) {
    if (!Array.isArray(point) || point.length < 3) continue;
    if (!Number.isFinite(point[1]) || !Number.isFinite(point[2])) continue;
    points.push([point[1], point[2]]);
  }
  return points;
}

/**
 * 윤곽에서 `startM`~`endM` 구간의 점 목록(양 끝은 보간)을 만든다.
 * 윤곽은 `[dist_m, x, z]`가 거리 순으로 들어 있다.
 */
function outlineSlice(outline, startM, endM) {
  if (!Array.isArray(outline) || outline.length < 2) return [];
  if (!(endM > startM)) return [];
  const points = [];
  const start = pointAtDistance(outline, startM);
  if (start) points.push(start);
  for (const point of outline) {
    if (!Array.isArray(point) || point.length < 3) continue;
    if (!Number.isFinite(point[1]) || !Number.isFinite(point[2])) continue;
    if (point[0] > startM && point[0] < endM) points.push([point[1], point[2]]);
  }
  const end = pointAtDistance(outline, endM);
  if (end) points.push(end);
  return points;
}

/** 윤곽 위 `distance` 지점을 이웃한 두 점 사이 보간으로 구한다. */
function pointAtDistance(outline, distance) {
  if (!Array.isArray(outline) || outline.length === 0) return null;
  const first = outline[0];
  const last = outline[outline.length - 1];
  if (!Array.isArray(first) || first.length < 3) return null;
  if (distance <= first[0] || outline.length === 1) return [first[1], first[2]];
  if (!Array.isArray(last) || last.length < 3) return null;
  if (distance >= last[0]) return [last[1], last[2]];

  for (let index = 1; index < outline.length; index += 1) {
    const previous = outline[index - 1];
    const current = outline[index];
    if (!Array.isArray(previous) || !Array.isArray(current)) continue;
    if (distance > current[0]) continue;
    const span = current[0] - previous[0];
    const ratio = span > 0 ? (distance - previous[0]) / span : 0;
    return [
      previous[1] + (current[1] - previous[1]) * ratio,
      previous[2] + (current[2] - previous[2]) * ratio,
    ];
  }
  return [last[1], last[2]];
}

function buildMetrics(metrics) {
  const box = document.createElement("div");
  box.className = "map-metrics";

  const loss = metrics.loss_ms;
  if (typeof loss === "number" && Number.isFinite(loss)) {
    const headline = document.createElement("p");
    headline.className = "map-info-title";
    headline.textContent = `손실 ${(loss / 1000).toFixed(2)}초`;
    box.append(headline);
  }

  const player = metrics.player || {};
  const reference = metrics.reference || {};
  const keys = [];
  for (const key of [...Object.keys(player), ...Object.keys(reference)]) {
    if (key !== "loss_ms" && !keys.includes(key)) keys.push(key);
  }
  if (keys.length === 0) {
    const note = document.createElement("p");
    note.className = "map-hint";
    note.textContent = "최근 지표: 값 없음";
    box.append(note);
    return box;
  }

  const table = document.createElement("table");
  const head = document.createElement("tr");
  head.append(headerCell(""), headerCell("나"), headerCell("기준"));
  table.append(head);
  for (const key of keys.slice(0, 8)) {
    const row = document.createElement("tr");
    row.append(headerCell(METRIC_LABELS[key] || key));
    row.append(valueCell(formatMetric(key, player[key])));
    row.append(valueCell(formatMetric(key, reference[key])));
    table.append(row);
  }
  box.append(table);
  return box;
}

function headerCell(text) {
  const cell = document.createElement("th");
  cell.textContent = text;
  return cell;
}

function valueCell(text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  return cell;
}

function formatMeters(value) {
  if (!Number.isFinite(value)) return "—";
  return `${Math.round(value)}m`;
}

function formatMetric(key, value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number" || !Number.isFinite(value)) return String(value);
  if (key.endsWith("_ms")) return `${(value / 1000).toFixed(2)}초`;
  if (key === "min_speed" || key === "exit_speed") return `${Math.round(value)}km/h`;
  if (key.endsWith("_m")) return `${Math.round(value)}m`;
  if (key === "steer_corrections") return `${Math.round(value)}회`;
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}
