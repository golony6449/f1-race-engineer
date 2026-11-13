/**
 * 차량 상태 화면 렌더링.
 *
 * 타이어·브레이크는 위에서 본 F1 차 그림(SVG) 하나와 바퀴 바깥쪽 정보 상자로
 * 보여 준다(SCRUM-77). 차 그림은 HTML에 있고, 이 파일은 정보 상자를 채우고
 * 바퀴 색만 바꾼다.
 *
 * 색은 서버가 `/api/thresholds`로 내려 준 표(colors)만 쓴다. 이 파일은 문턱값을
 * 계산하지 않고, 서버가 값마다 붙여 준 `severity`(ok/warn/danger/no_data)를
 * 색 표의 키로 바꿔 칠할 뿐이다(설계 6.6·6.9). 타이어 사각형은 그 바퀴의 마모·
 * 표면 온도·내부 온도 중 가장 나쁜 단계, 브레이크 디스크 원은 브레이크 온도
 * 단계의 색을 쓴다.
 *
 * 값이 끊겨도 칸을 지우지 않는다. 서버가 마지막 값을 계속 내려 주고, 연결이
 * 끊겼다는 표시(`stale`)가 오면 숫자와 그림을 흐리게 만든다.
 */

const UNIT_DIGITS = {
  "%": 1,
  "°C": 0,
  psi: 1,
  kg: 2,
  "kg/랩": 2,
  랩: 1,
  MJ: 2,
  kW: 0,
};

// 서버 API는 게임 원래 단위(J, W)를 준다. 화면에서만 MJ·kW로 바꾼다.
const DISPLAY_UNITS = {
  J: { unit: "MJ", scale: 1e-6 },
  W: { unit: "kW", scale: 1e-3 },
};

const FUEL_CELLS = [
  ["remaining_kg", "남은 연료"],
  ["per_lap_kg", "랩당 소모"],
  ["remaining_laps", "남은 랩"],
  ["remaining_race_laps", "레이스 남은 랩"],
  ["laps_margin", "연료 여유"],
];

// ERS 여섯 칸은 2열 격자에서 잔량·모드 / 사용량·출력 / 회수 K·회수 H 순서로
// 놓인다(SCRUM-86). 좁은 화면에서는 CSS가 한 열로 쌓는다.
const ERS_CELLS = [
  ["store_pct", "잔량"],
  ["mode", "모드"],
  ["deployed_this_lap", "이번 랩 사용"],
  ["mguk_power", "MGU-K 출력"],
  ["harvested_mguk", "MGU-K 회수"],
  ["harvested_mguh", "MGU-H 회수"],
];

// 기준값(`max`) 대비 막대를 그리는 ERS 칸. 기준값은 서버가 상수 하나로 준다.
const ERS_BAR_CELLS = new Set(["deployed_this_lap", "mguk_power"]);

// 타이어 그림 색을 고르는 순서다. 값이 없는 칸(no_data)은 초록(ok)보다
// 나쁘게 본다. 일부 값이 비었는데 초록으로 보이면 상태를 오해하기 쉽다.
const SEVERITY_RANK = { ok: 0, no_data: 1, warn: 2, danger: 3 };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatValue(value, unit) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number" || !Number.isFinite(value)) return String(value);
  const digits = UNIT_DIGITS[unit];
  if (digits !== undefined) return value.toFixed(digits);
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

/** 게임 단위(J·W)를 화면 단위(MJ·kW)로 바꾼다. 숫자가 아니면 그대로 둔다. */
function displayValue(data) {
  if (!data) return null;
  const conversion = DISPLAY_UNITS[data.unit];
  if (
    !conversion ||
    typeof data.value !== "number" ||
    !Number.isFinite(data.value)
  ) {
    return data;
  }
  return { value: data.value * conversion.scale, unit: conversion.unit };
}

/** 기준값(`max`) 대비 비율(%). 값이나 기준이 없으면 0%로 둔다. */
function barWidth(data) {
  if (!data || typeof data.value !== "number" || !Number.isFinite(data.value)) {
    return "0%";
  }
  if (
    typeof data.max !== "number" ||
    !Number.isFinite(data.max) ||
    data.max <= 0
  ) {
    return "0%";
  }
  const ratio = Math.min(1, Math.max(0, data.value / data.max));
  return `${(ratio * 100).toFixed(1)}%`;
}

export class CarStateView {
  constructor(root) {
    this.root = root;
    this.colors = {};
    this.cells = new Map();
    this.tyreMeta = new Map();
    // 차 그림의 바퀴 요소와 바퀴별 단계. 색 표가 늦게 와도 다시 칠한다.
    this.tyreShapes = new Map();
    this.brakeDiscs = new Map();
    this.tyreSeverity = new Map();
    this.brakeSeverity = new Map();
    this.built = false;
  }

  /** `/api/thresholds`가 준 색 표. 늦게 와도 이미 그린 칸과 그림을 다시 칠한다. */
  setColors(colors) {
    this.colors = colors || {};
    for (const cell of this.cells.values()) this._paint(cell);
    this._paintWheels();
  }

  render(car) {
    if (!car) return;
    if (!this.built) this._build(car);
    this._update(car);
  }

  _build(car) {
    this.built = true;

    const waiting = document.getElementById("tyres-waiting");
    if (waiting) waiting.remove();

    this.tyreMeta.clear();
    for (const tyre of car.tyres || []) {
      const card = document.querySelector(
        `[data-tyre-box="${tyre.position}"]`,
      );
      if (!card) continue;
      card.replaceChildren();

      const head = element("header", "tyre-head");
      head.append(element("span", "tyre-pos", tyre.label || tyre.position));
      const meta = element("span", "tyre-meta", "");
      head.append(meta);
      this.tyreMeta.set(tyre.position, meta);

      const grid = element("div", "cell-grid");
      this._buildCell(grid, `tyre.${tyre.position}.wear`, "마모");
      this._buildCell(grid, `tyre.${tyre.position}.surface_temp`, "표면 온도");
      this._buildCell(grid, `tyre.${tyre.position}.inner_temp`, "내부 온도");
      this._buildCell(grid, `tyre.${tyre.position}.pressure`, "압력");

      card.append(head, grid);
    }

    // 브레이크 온도는 같은 바퀴 정보 상자에 붙인다. 셀 id 규칙은 그대로 둔다.
    for (const brake of car.brakes || []) {
      const card = document.querySelector(
        `[data-tyre-box="${brake.position}"]`,
      );
      if (!card) continue;
      const cell = this._buildCell(
        card,
        `brake.${brake.position}.temp`,
        "브레이크 온도",
      );
      cell.classList.add("brake-cell");
    }

    this.tyreShapes.clear();
    for (const shape of document.querySelectorAll(".tyre-shape[data-tyre]")) {
      this.tyreShapes.set(shape.dataset.tyre, shape);
    }
    this.brakeDiscs.clear();
    for (const disc of document.querySelectorAll(".brake-disc[data-brake]")) {
      this.brakeDiscs.set(disc.dataset.brake, disc);
    }

    const fuelRoot = document.getElementById("fuel");
    fuelRoot.replaceChildren();
    for (const [key, label] of FUEL_CELLS) {
      this._buildCell(fuelRoot, `fuel.${key}`, label);
    }

    const ersRoot = document.getElementById("ers");
    ersRoot.replaceChildren();
    for (const [key, label] of ERS_CELLS) {
      this._buildCell(ersRoot, `ers.${key}`, label, ERS_BAR_CELLS.has(key));
    }

    const damageRoot = document.getElementById("damage");
    damageRoot.replaceChildren();
    for (const item of car.damage || []) {
      this._buildCell(damageRoot, `damage.${item.key}`, item.label || item.key);
    }
  }

  _buildCell(container, key, label, withBar = false) {
    const box = element("div", "cell");
    box.append(element("span", "cell-label", label));
    const value = element("span", "cell-value");
    const number = element("span", "cell-number", "—");
    const unit = element("span", "cell-unit", "");
    value.append(number, unit);
    box.append(value);

    let barFill = null;
    if (withBar) {
      const bar = element("div", "cell-bar");
      barFill = element("div", "cell-bar-fill");
      bar.append(barFill);
      box.append(bar);
    }

    this.cells.set(key, { number, unit, value, barFill, severity: null });
    container.append(box);
    return box;
  }

  _update(car) {
    for (const tyre of car.tyres || []) {
      this._setCell(`tyre.${tyre.position}.wear`, tyre.wear);
      this._setCell(`tyre.${tyre.position}.surface_temp`, tyre.surface_temp);
      this._setCell(`tyre.${tyre.position}.inner_temp`, tyre.inner_temp);
      this._setCell(`tyre.${tyre.position}.pressure`, tyre.pressure);

      const meta = this.tyreMeta.get(tyre.position);
      if (meta) meta.textContent = compoundText(tyre);

      this.tyreSeverity.set(
        tyre.position,
        this._worstTyreSeverity(tyre.position),
      );
    }

    for (const brake of car.brakes || []) {
      this._setCell(`brake.${brake.position}.temp`, brake.temp);
      this.brakeSeverity.set(
        brake.position,
        brake.temp && brake.temp.severity ? brake.temp.severity : "no_data",
      );
    }

    this._paintWheels();

    for (const item of car.damage || []) {
      this._setCell(`damage.${item.key}`, item.value);
    }

    for (const [key] of FUEL_CELLS) {
      this._setCell(`fuel.${key}`, car.fuel ? car.fuel[key] : null);
    }

    for (const [key] of ERS_CELLS) {
      this._setCell(`ers.${key}`, car.ers ? car.ers[key] : null);
    }
  }

  _setCell(key, data) {
    const cell = this.cells.get(key);
    if (!cell) return;
    const shown = displayValue(data);
    cell.number.textContent = formatValue(
      shown ? shown.value : null,
      shown ? shown.unit : null,
    );
    cell.unit.textContent = shown && shown.unit ? ` ${shown.unit}` : "";
    cell.severity = data && data.severity ? data.severity : null;
    if (data && data.metric) cell.value.title = data.metric;
    else cell.value.removeAttribute("title");
    if (cell.barFill) cell.barFill.style.width = barWidth(data);
    this._paint(cell);
  }

  _paint(cell) {
    const color = cell.severity ? this.colors[cell.severity] : null;
    if (color) cell.value.style.color = color;
    else cell.value.style.removeProperty("color");
  }

  /** 마모·표면 온도·내부 온도 중 가장 나쁜 단계. 그림 사각형 색에 쓴다. */
  _worstTyreSeverity(position) {
    let worst = "ok";
    for (const metric of ["wear", "surface_temp", "inner_temp"]) {
      const cell = this.cells.get(`tyre.${position}.${metric}`);
      const level = cell && cell.severity ? cell.severity : "no_data";
      if (SEVERITY_RANK[level] > SEVERITY_RANK[worst]) worst = level;
    }
    return worst;
  }

  /** 바퀴 색은 서버가 준 단계(severity)를 색 표의 키로 바꿔 칠할 뿐이다. */
  _paintWheels() {
    for (const [position, shape] of this.tyreShapes) {
      this._paintShape(shape, this.tyreSeverity.get(position));
    }
    for (const [position, disc] of this.brakeDiscs) {
      this._paintShape(disc, this.brakeSeverity.get(position));
    }
  }

  _paintShape(node, level) {
    const color = this.colors[level || "no_data"];
    if (color) node.style.fill = color;
    else node.style.removeProperty("fill");
  }
}

function compoundText(tyre) {
  const compound = tyre.compound && tyre.compound.value;
  const age = tyre.age_laps && tyre.age_laps.value;
  const parts = [];
  if (compound) parts.push(compound);
  if (age !== null && age !== undefined) parts.push(`${Math.round(age)}랩`);
  return parts.join(" · ");
}

/** 상단 상태줄(수신 진단·음성·무전 페이지). */
export function renderStatusBar(payload) {
  const diagnostics = (payload.snapshot && payload.snapshot.diagnostics) || {};
  const connection = payload.connection || {};
  const status = payload.status || {};

  const connectionValue = document.querySelector('[data-field="connection"]');
  if (connectionValue) {
    if (connection.data_age_ms === null || connection.data_age_ms === undefined) {
      connectionValue.textContent = "데이터 없음";
    } else if (connection.stale) {
      const seconds = (connection.data_age_ms / 1000).toFixed(1);
      connectionValue.textContent = `데이터 안 옴 · 마지막 수신 ${seconds}초 전`;
    } else {
      connectionValue.textContent = `수신 중 · ${Math.round(diagnostics.packets_per_sec || 0)} pkt/s`;
    }
  }

  const detail = document.getElementById("connection-detail");
  if (detail) {
    const format = diagnostics.last_format ?? "—";
    const packetId = diagnostics.last_packet_id ?? "—";
    const length = diagnostics.last_len ?? "—";
    detail.textContent = `형식 ${format} · 패킷 ${packetId} · 길이 ${length}`;
  }

  const detail2 = document.getElementById("connection-detail-2");
  if (detail2) {
    const unsupported = diagnostics.unsupported_count ?? 0;
    const errors = diagnostics.error_count ?? 0;
    const session = diagnostics.session_uid ?? "—";
    detail2.textContent = `미지원 ${unsupported} · 오류 ${errors} · 세션 ${session}`;
  }

  renderRadioStatus(status.voice, status.radio_page);
}

/**
 * 무전 칸: 음성과 무전 페이지 상태를 한 줄로 합친다.
 *
 * 둘 다 정상일 때만 정상 색(ok), 하나라도 문제면 경고 색(warn)이다.
 * 색은 상태줄 클래스(.status-value.ok / .status-value.warn)가 칠한다.
 */
function renderRadioStatus(voice, radioPage) {
  const node = document.querySelector('[data-field="radio"]');
  if (!node) return;

  const online = Boolean(voice && voice.state === "online");
  const connected = Boolean(radioPage && radioPage.connected);
  const clients =
    radioPage && Number.isFinite(radioPage.clients) ? radioPage.clients : 0;

  const voiceText = online ? "음성 온라인" : "음성 오프라인";
  const radioText = connected
    ? `무전 페이지 연결 ${clients}`
    : "무전 페이지 미연결";
  node.textContent = `${voiceText} · ${radioText}`;

  const healthy = online && connected;
  node.classList.toggle("ok", healthy);
  node.classList.toggle("warn", !healthy);
}

/** 값이 끊겼을 때 칸은 그대로 두고 숫자만 흐리게 한다. */
export function setStale(root, stale) {
  if (root) root.classList.toggle("stale", Boolean(stale));
}
