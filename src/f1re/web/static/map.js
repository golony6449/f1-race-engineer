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
    this.stale = false;
    this.teamColors = {};
    this.bounds = null;
    this.cornerHits = [];
    this.selectedCorner = null;
    // 코너 번호 → 추천 구간(`/api/lift-coast`의 recommended 항목).
    this.liftZones = new Map();
    this.liftTimer = null;

    if (this.canvas) {
      this.canvas.addEventListener("click", (event) => this._onClick(event));
    }
    window.addEventListener("resize", () => this._draw());
    this._renderInfo(null);
  }

  /** `/api/track` 응답. 윤곽·코너·팀 색을 한 번만 받는다. */
  setTrack(track) {
    this.track = track || null;
    this.teamColors = (track && track.team_colors) || {};
    if (track && track.flag) this.flag = track.flag;
    this.bounds = outlineBounds(track && track.outline);
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
    this.stale = Boolean(payload.connection && payload.connection.stale);
    if (this.root) this.root.classList.toggle("stale", this.stale);
    this._updateBadge();
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

  _fit(width, height) {
    const bounds = this.bounds;
    if (!bounds) return null;
    const spanX = Math.max(bounds.maxX - bounds.minX, 1);
    const spanZ = Math.max(bounds.maxZ - bounds.minZ, 1);
    const usableWidth = Math.max(width - MAP_PADDING_PX * 2, 1);
    const usableHeight = Math.max(height - MAP_PADDING_PX * 2, 1);
    const scale = Math.min(usableWidth / spanX, usableHeight / spanZ);
    if (!Number.isFinite(scale) || scale <= 0) return null;
    return {
      scale,
      offsetX: (width - spanX * scale) / 2,
      offsetY: (height - spanZ * scale) / 2,
      minX: bounds.minX,
      maxZ: bounds.maxZ,
    };
  }

  _toCanvas(fit, x, z) {
    // 화면 y는 아래로 증가하므로 z를 뒤집어 지도처럼 보이게 한다.
    return [
      fit.offsetX + (x - fit.minX) * fit.scale,
      fit.offsetY + (fit.maxZ - z) * fit.scale,
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
}

function flagStyle(flag) {
  return FLAG_STYLES[flag] || FLAG_STYLES.none;
}

function outlineBounds(outline) {
  const points = Array.isArray(outline) ? outline : [];
  const xs = [];
  const zs = [];
  for (const point of points) {
    if (!Array.isArray(point) || point.length < 3) continue;
    if (!Number.isFinite(point[1]) || !Number.isFinite(point[2])) continue;
    xs.push(point[1]);
    zs.push(point[2]);
  }
  if (xs.length < 2) return null;
  return {
    minX: Math.min(...xs),
    maxX: Math.max(...xs),
    minZ: Math.min(...zs),
    maxZ: Math.max(...zs),
  };
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
