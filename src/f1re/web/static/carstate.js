/**
 * 차량 상태 화면 렌더링.
 *
 * 색은 서버가 `/api/thresholds`로 내려 준 표(colors)만 쓴다. 이 파일은 문턱값을
 * 계산하지 않고, 서버가 값마다 붙여 준 `severity`(ok/warn/danger/no_data)를
 * 색 표의 키로 바꿔 칠할 뿐이다(설계 6.6·6.9).
 *
 * 값이 끊겨도 칸을 지우지 않는다. 서버가 마지막 값을 계속 내려 주고, 연결이
 * 끊겼다는 표시(`stale`)가 오면 숫자만 흐리게 만든다.
 */

const UNIT_DIGITS = {
  "%": 1,
  "°C": 0,
  psi: 1,
  kg: 2,
  "kg/랩": 2,
  랩: 1,
};

const FUEL_CELLS = [
  ["remaining_kg", "남은 연료"],
  ["per_lap_kg", "랩당 소모"],
  ["remaining_laps", "남은 랩"],
  ["remaining_race_laps", "레이스 남은 랩"],
  ["laps_margin", "연료 여유"],
];

const ERS_CELLS = [
  ["store_pct", "잔량"],
  ["mode", "모드"],
];

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

export class CarStateView {
  constructor(root) {
    this.root = root;
    this.colors = {};
    this.cells = new Map();
    this.tyreMeta = new Map();
    this.built = false;
  }

  /** `/api/thresholds`가 준 색 표. 늦게 와도 이미 그린 칸을 다시 칠한다. */
  setColors(colors) {
    this.colors = colors || {};
    for (const cell of this.cells.values()) this._paint(cell);
  }

  render(car) {
    if (!car) return;
    if (!this.built) this._build(car);
    this._update(car);
  }

  _build(car) {
    this.built = true;

    const tyresRoot = document.getElementById("tyres");
    tyresRoot.replaceChildren();
    this.tyreMeta.clear();
    for (const tyre of car.tyres || []) {
      const card = element("article", "tyre");
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
      tyresRoot.append(card);
    }

    const brakesRoot = document.getElementById("brakes");
    brakesRoot.replaceChildren();
    for (const brake of car.brakes || []) {
      this._buildCell(
        brakesRoot,
        `brake.${brake.position}.temp`,
        brake.label || brake.position,
      );
    }

    const fuelRoot = document.getElementById("fuel");
    fuelRoot.replaceChildren();
    for (const [key, label] of FUEL_CELLS) {
      this._buildCell(fuelRoot, `fuel.${key}`, label);
    }

    const ersRoot = document.getElementById("ers");
    ersRoot.replaceChildren();
    for (const [key, label] of ERS_CELLS) {
      this._buildCell(ersRoot, `ers.${key}`, label);
    }

    const damageRoot = document.getElementById("damage");
    damageRoot.replaceChildren();
    for (const item of car.damage || []) {
      this._buildCell(damageRoot, `damage.${item.key}`, item.label || item.key);
    }
  }

  _buildCell(container, key, label) {
    const box = element("div", "cell");
    box.append(element("span", "cell-label", label));
    const value = element("span", "cell-value");
    const number = element("span", "cell-number", "—");
    const unit = element("span", "cell-unit", "");
    value.append(number, unit);
    box.append(value);
    this.cells.set(key, { number, unit, value, severity: null });
    container.append(box);
  }

  _update(car) {
    for (const tyre of car.tyres || []) {
      this._setCell(`tyre.${tyre.position}.wear`, tyre.wear);
      this._setCell(`tyre.${tyre.position}.surface_temp`, tyre.surface_temp);
      this._setCell(`tyre.${tyre.position}.inner_temp`, tyre.inner_temp);
      this._setCell(`tyre.${tyre.position}.pressure`, tyre.pressure);

      const meta = this.tyreMeta.get(tyre.position);
      if (meta) meta.textContent = compoundText(tyre);
    }

    for (const brake of car.brakes || []) {
      this._setCell(`brake.${brake.position}.temp`, brake.temp);
    }

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
    cell.number.textContent = formatValue(data ? data.value : null, data ? data.unit : null);
    cell.unit.textContent = data && data.unit ? ` ${data.unit}` : "";
    cell.severity = data && data.severity ? data.severity : null;
    if (data && data.metric) cell.value.title = data.metric;
    else cell.value.removeAttribute("title");
    this._paint(cell);
  }

  _paint(cell) {
    const color = cell.severity ? this.colors[cell.severity] : null;
    if (color) cell.value.style.color = color;
    else cell.value.style.removeProperty("color");
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

  setStatusText("voice", status.voice);
  setStatusText("radio_page", status.radio_page);
}

function setStatusText(field, state) {
  const node = document.querySelector(`[data-field="${field}"]`);
  if (!node) return;
  node.textContent = (state && state.label) || "알 수 없음";
}

/** 값이 끊겼을 때 칸은 그대로 두고 숫자만 흐리게 한다. */
export function setStale(root, stale) {
  if (root) root.classList.toggle("stale", Boolean(stale));
}
