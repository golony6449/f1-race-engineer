/**
 * 다시 보기 텔레메트리 분석 화면(SCRUM-136, 설계 5.2).
 *
 * - 서버 `/api/replay/telemetry/*`가 녹화를 1회 스캔해 만든 인덱스만 읽는다.
 *   전역 `ReplayManager` 재생을 시작·정지·시크하지 않으므로 엔진 ON/OFF와
 *   대시보드 다시 보기 상태에 영향을 주지 않는다.
 * - `ReplayClock`은 `requestAnimationFrame` 로컬 타이머로 `position_ms`를
 *   전진시킨다(1x/2x/5x/10x). `MapFrameBuffer`가 30초 청크 `map_frames`를
 *   미리 받아 두므로 재생 중 지도 갱신이 네트워크에 묶이지 않는다.
 * - 메트릭 정의·라벨·단위·4륜 그룹은 서버 `summary.catalog`가 단일 출처다.
 *   이 파일은 표시 순서와 스와치 색만 정한다(서버 카탈로그에 색 필드가 없다).
 * - 차트 본체는 `TelemetryChart` 클래스로 분리한다. 이 작업(Task 5)은 축·격자·
 *   구간 밴드·마커 레인·재생 커서·기본 안내까지 그리는 뼈대이며, 다중 축
 *   시계열·줌/팬·호버 상호작용은 Task 6이 같은 클래스에 이어 붙인다.
 */

import { TrackMapView } from "./map.js";

// `summary`가 `building`일 때 폴링 간격(ms). 설계 7의 300ms.
const POLL_INTERVAL_MS = 300;
// 지도 프레임 청크 길이(ms). 10Hz 300프레임을 한 번에 받는다.
const CHUNK_MS = 30_000;
// 재생 중 지도 갱신 최소 간격(ms). 프레임 자체가 10Hz이므로 그보다 자주
// 요청하지 않는다.
const MAP_UPDATE_MIN_MS = 90;

const METRIC_STORAGE_KEY = "f1re.telemetry.metrics.v1";
const OPTIONS_STORAGE_KEY = "f1re.telemetry.options.v1";
const COLLAPSED_STORAGE_KEY = "f1re.telemetry.collapsed.v1";

// 4륜 그룹의 짧은 이름. 마우스를 올리면 서버가 준 `wheel_label`(앞왼쪽 등)을
// title로 보여 준다.
const WHEEL_LABELS = { fl: "전좌", fr: "전우", rl: "후좌", rr: "후우" };

// 메트릭 스와치·범례 색. 서버 카탈로그에 색 필드가 없어 화면이 정한다.
// 카탈로그 순서에 따라 고정 배정하므로 새로고침해도 같은 메트릭은 같은 색이다.
const SWATCH_COLORS = [
  "#4dabf7",
  "#ffd166",
  "#ff6b6b",
  "#51cf66",
  "#c4b5fd",
  "#22d3ee",
  "#f59e0b",
  "#f783ac",
];

const CATEGORY_LABELS = {
  practice: "연습",
  qualifying: "예선",
  race: "레이스",
  time_trial: "타임 트라이얼",
  unknown: "미분류",
};

const FLAG_LABELS = {
  safety_car: "세이프티 카",
  vsc: "VSC",
  red_flag: "적기",
  formation: "포메이션 랩",
};

const EMPTY_MARKERS = { radio: [], events: [], intervals: [], laps: [] };

// ---------------------------------------------------------------------------
// DOM

const recordingSelect = document.getElementById("recording-select");
const compareSelect = document.getElementById("compare-select");
const playToggle = document.getElementById("play-toggle");
const playbackSpeeds = document.getElementById("playback-speeds");
const playbackSeek = document.getElementById("playback-seek");
const playbackTime = document.getElementById("playback-time");
const zoomReset = document.getElementById("zoom-reset");
const autoFollowButton = document.getElementById("auto-follow");
const radioMarkerToggle = document.getElementById("toggle-radio-markers");
const eventMarkerToggle = document.getElementById("toggle-event-markers");

const presetBar = document.getElementById("preset-bar");
const chartCanvas = document.getElementById("telemetry-canvas");
const chartTooltip = document.getElementById("chart-tooltip");
const chartOverlay = document.getElementById("chart-overlay");
const chartOverlayText = document.getElementById("chart-overlay-text");
const progressWrap = document.getElementById("index-progress-wrap");
const progressFill = document.getElementById("index-progress");
const privateBadge = document.getElementById("private-badge");

const metricTree = document.getElementById("metric-tree");
const metricCount = document.getElementById("metric-count");

const summaryStatic = document.getElementById("summary-static");
const summaryCurrent = document.getElementById("summary-current");
const summaryCorners = document.getElementById("summary-corners");

const mapView = new TrackMapView(document.getElementById("telemetry-map"));

// ---------------------------------------------------------------------------
// 화면 상태

const appState = {
  recordingId: null,
  ready: false,
  durationMs: 0,
  playerIndex: null,
  participants: [],
  catalog: [],
  presets: [],
  metricByKey: new Map(),
  markers: { ...EMPTY_MARKERS },
  recording: null,
  track: null,
  compareCar: null,
  frameBuffer: null,
  frameToken: 0,
  pollToken: 0,
  pollTimer: null,
  lastMapPosition: -Infinity,
};

let selection = new Set();
let seeking = false;

// ---------------------------------------------------------------------------
// 로컬 재생 시계

/**
 * `requestAnimationFrame` 기반 로컬 재생 시계.
 *
 * `position_ms`만 전진시키고 서버에는 아무것도 보내지 않는다. 그래서 이 화면의
 * 재생은 전역 `ReplayManager`와 독립적이다.
 */
class ReplayClock {
  constructor(onChange) {
    this.durationMs = 0;
    this.positionMs = 0;
    this.speed = 1;
    this.playing = false;
    this.onChange = onChange;
    this._raf = null;
    this._lastFrame = null;
    this._loop = this._loop.bind(this);
  }

  setDuration(durationMs) {
    const value = Number(durationMs);
    this.durationMs = Number.isFinite(value) && value > 0 ? value : 0;
    if (this.positionMs > this.durationMs) this.positionMs = this.durationMs;
    this._emit();
  }

  setSpeed(speed) {
    const value = Number(speed);
    if (!Number.isFinite(value) || value <= 0) return;
    this.speed = value;
    this._emit();
  }

  seek(positionMs) {
    const value = Number(positionMs);
    if (!Number.isFinite(value)) return;
    this.positionMs = Math.max(0, Math.min(value, this.durationMs));
    this._emit();
  }

  play() {
    if (this.playing || this.durationMs <= 0) return;
    if (this.positionMs >= this.durationMs) this.positionMs = 0;
    this.playing = true;
    this._lastFrame = null;
    this._raf = window.requestAnimationFrame(this._loop);
    this._emit();
  }

  pause() {
    const wasPlaying = this.playing;
    this.playing = false;
    if (this._raf !== null) {
      window.cancelAnimationFrame(this._raf);
      this._raf = null;
    }
    this._lastFrame = null;
    if (wasPlaying) this._emit();
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }

  _loop(now) {
    if (!this.playing) return;
    if (this._lastFrame === null) this._lastFrame = now;
    const elapsed = Math.max(0, now - this._lastFrame);
    this._lastFrame = now;
    this.positionMs = Math.min(
      this.durationMs,
      this.positionMs + elapsed * this.speed,
    );
    if (this.positionMs >= this.durationMs) {
      this.playing = false;
      this._raf = null;
      this._emit();
      return;
    }
    this._emit();
    this._raf = window.requestAnimationFrame(this._loop);
  }

  _emit() {
    this.onChange(this);
  }
}

// ---------------------------------------------------------------------------
// 10Hz 지도 프레임 청크 버퍼

/**
 * `/api/replay/telemetry/map_frames` 구간 응답을 30초 청크로 캐시한다.
 *
 * 재생 커서가 새 청크에 들어오면 그 청크를 받고 다음 청크를 미리 요청한다.
 * 같은 청크의 중복 요청은 진행 중 프로미스로 합친다. 받은 프레임은 `t_ms`
 * 오름차순으로 합쳐 `frameAt()`이 이진 탐색으로 바로 꺼내게 한다.
 */
class MapFrameBuffer {
  constructor(recordingId, { chunkMs = CHUNK_MS, durationMs = 0 } = {}) {
    this.recordingId = recordingId;
    this.chunkMs = chunkMs;
    this.durationMs = durationMs;
    this.frames = [];
    this.times = [];
    this.loadedChunks = new Set();
    this.pendingChunks = new Map();
  }

  _chunkStart(tMs) {
    return (
      Math.floor(Math.max(0, Number(tMs) || 0) / this.chunkMs) * this.chunkMs
    );
  }

  async fetchChunk(chunkStart, prefetchNext = false) {
    if (this.loadedChunks.has(chunkStart)) return;
    const pending = this.pendingChunks.get(chunkStart);
    if (pending) return pending;
    const promise = this._load(chunkStart, prefetchNext).finally(() => {
      this.pendingChunks.delete(chunkStart);
    });
    this.pendingChunks.set(chunkStart, promise);
    return promise;
  }

  async _load(chunkStart, prefetchNext) {
    const params = new URLSearchParams({
      recording_id: String(this.recordingId),
      from_ms: String(chunkStart),
      to_ms: String(chunkStart + this.chunkMs - 1),
    });
    try {
      const response = await fetch(
        `/api/replay/telemetry/map_frames?${params.toString()}`,
        { cache: "no-store" },
      );
      if (!response.ok) return;
      const data = await response.json();
      const frames = Array.isArray(data.frames) ? data.frames : [];
      this._merge(frames);
      this.loadedChunks.add(chunkStart);
    } catch (error) {
      // 네트워크가 잠깐 끊겼다. 다음 커서 이동이 같은 청크를 다시 요청한다.
      return;
    }
    if (prefetchNext) {
      const next = chunkStart + this.chunkMs;
      // 녹화 길이를 아는 경우에만 다음 청크를 미리 받는다(끝 뒤는 요청하지 않음).
      if (this.durationMs <= 0 || next <= this.durationMs) {
        void this.fetchChunk(next, false);
      }
    }
  }

  _merge(frames) {
    if (!frames.length) return;
    const byTime = new Map();
    for (const frame of this.frames) {
      if (frame && Number.isFinite(frame.t_ms)) byTime.set(frame.t_ms, frame);
    }
    for (const frame of frames) {
      if (frame && Number.isFinite(frame.t_ms)) byTime.set(frame.t_ms, frame);
    }
    this.frames = [...byTime.values()].sort((a, b) => a.t_ms - b.t_ms);
    this.times = this.frames.map((frame) => frame.t_ms);
  }

  /** 캐시에서 `tMs` 이전(같으면 그 시각)의 마지막 프레임. 없으면 `null`. */
  frameAt(tMs) {
    if (!this.frames.length) return null;
    const target = Number(tMs);
    if (!Number.isFinite(target)) return null;
    const index = upperBound(this.times, target) - 1;
    if (index < 0) return this.frames[0];
    return this.frames[index];
  }

  /** 해당 시각이 든 청크를 확보한 뒤 프레임을 돌려준다. */
  async resolve(tMs) {
    await this.fetchChunk(this._chunkStart(tMs), true);
    return this.frameAt(tMs);
  }
}

// ---------------------------------------------------------------------------
// Canvas 2D 차트 뼈대

/**
 * 시간축·구간 밴드·마커 레인·재생 커서를 그리는 차트.
 *
 * Task 6은 이 클래스에 `setSeries()`(서버 `/series` 응답 보관), 줌/팬, 호버
 * 툴팁, 다중 단위 정규화 렌더링을 더한다. 지금은 빈 그래프에서도 시간축과
 * 서버 마커를 볼 수 있게 해 "무엇이 그려질 자리인지"를 분명히 한다.
 */
class TelemetryChart {
  constructor({ canvas, tooltip }) {
    this.canvas = canvas;
    this.ctx = canvas ? canvas.getContext("2d") : null;
    this.tooltip = tooltip;
    this.catalog = new Map();
    this.recording = null;
    this.durationMs = 0;
    this.selection = [];
    this.cursorMs = 0;
    this.markers = { ...EMPTY_MARKERS };
    this.compareCar = null;
    this.autoFollow = false;
    this.showRadioMarkers = true;
    this.showEventMarkers = true;
    this.zoom = null;
    this.metricValues = null;
    this._draw = this._draw.bind(this);
    if (
      this.canvas &&
      this.canvas.parentElement &&
      typeof ResizeObserver === "function"
    ) {
      this.resizeObserver = new ResizeObserver(() => this._draw());
      this.resizeObserver.observe(this.canvas.parentElement);
    }
    window.addEventListener("resize", this._draw);
    this._draw();
  }

  reset() {
    this.catalog = new Map();
    this.recording = null;
    this.durationMs = 0;
    this.selection = [];
    this.cursorMs = 0;
    this.markers = { ...EMPTY_MARKERS };
    this.compareCar = null;
    this.zoom = null;
    this.metricValues = null;
    this.hideTooltip();
    this._draw();
  }

  setCatalog(catalog) {
    this.catalog = new Map();
    for (const category of Array.isArray(catalog) ? catalog : []) {
      for (const metric of category.metrics || []) {
        this.catalog.set(metric.key, metric);
      }
    }
    this._draw();
  }

  setRecording(recording) {
    this.recording = recording || null;
    const duration = recording ? Number(recording.duration_ms) : 0;
    this.durationMs = Number.isFinite(duration) && duration > 0 ? duration : 0;
    this.zoom = null;
    this._draw();
  }

  setMarkers(markers) {
    this.markers =
      markers && typeof markers === "object"
        ? {
            radio: Array.isArray(markers.radio) ? markers.radio : [],
            events: Array.isArray(markers.events) ? markers.events : [],
            intervals: Array.isArray(markers.intervals)
              ? markers.intervals
              : [],
            laps: Array.isArray(markers.laps) ? markers.laps : [],
          }
        : { ...EMPTY_MARKERS };
    this._draw();
  }

  setSelection(keys) {
    this.selection = (Array.isArray(keys) ? keys : []).filter((key) =>
      this.catalog.has(key),
    );
    this._draw();
  }

  setCursor(positionMs) {
    const value = Number(positionMs);
    if (!Number.isFinite(value)) return;
    this.cursorMs = Math.max(0, Math.min(value, this.durationMs));
    this._draw();
  }

  setCompareCar(carIndex) {
    this.compareCar = Number.isInteger(carIndex) ? carIndex : null;
    this._draw();
  }

  setAutoFollow(enabled) {
    this.autoFollow = Boolean(enabled);
  }

  setMarkerVisibility({ radio = true, events = true } = {}) {
    this.showRadioMarkers = Boolean(radio);
    this.showEventMarkers = Boolean(events);
    this._draw();
  }

  /** Task 6이 커서 시점 값을 넘길 자리(현재는 사용하지 않는다). */
  setMetricValues(values) {
    this.metricValues = values || null;
  }

  showTooltip(text, x, y) {
    if (!this.tooltip) return;
    this.tooltip.textContent = text;
    this.tooltip.hidden = false;
    this.tooltip.style.left = `${Math.round(x)}px`;
    this.tooltip.style.top = `${Math.round(y)}px`;
  }

  hideTooltip() {
    if (!this.tooltip) return;
    this.tooltip.hidden = true;
    this.tooltip.textContent = "";
  }

  resetZoom() {
    this.zoom = null;
    this._draw();
  }

  /** 확대 상태에서 커서가 화면 밖으로 나가면 뷰포트를 따라 민다(자동 따라가기). */
  followCursor(positionMs) {
    if (!this.zoom || !(this.durationMs > 0)) return;
    const width = Math.max(1000, this.zoom.toMs - this.zoom.fromMs);
    if (positionMs >= this.zoom.fromMs && positionMs <= this.zoom.toMs) return;
    const maxFrom = Math.max(0, this.durationMs - width);
    const from = Math.max(0, Math.min(positionMs - width * 0.9, maxFrom));
    this.zoom = { fromMs: from, toMs: Math.min(this.durationMs, from + width) };
    this._draw();
  }

  _viewport() {
    if (this.zoom && this.zoom.toMs > this.zoom.fromMs) return this.zoom;
    return { fromMs: 0, toMs: Math.max(1, this.durationMs) };
  }

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

    const pad = { left: 48, right: 16, top: 24, bottom: 26 };
    const plotWidth = Math.max(1, width - pad.left - pad.right);
    const plotHeight = Math.max(1, height - pad.top - pad.bottom);

    if (!(this.durationMs > 0)) {
      ctx.fillStyle = "#8b97a8";
      ctx.font = "600 13px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(
        "녹화를 열면 세션 시간축이 표시됩니다.",
        width / 2,
        height / 2,
      );
      ctx.textAlign = "start";
      return;
    }

    const viewport = this._viewport();
    const span = Math.max(1, viewport.toMs - viewport.fromMs);
    const xOf = (tMs) =>
      pad.left + ((tMs - viewport.fromMs) / span) * plotWidth;

    // SC·VSC·적기·피트 구간 밴드를 그래프 뒤에 깐다.
    for (const interval of this.markers.intervals) {
      const startMs = Number(interval.start_ms);
      const endRaw =
        interval.end_ms === null || interval.end_ms === undefined
          ? NaN
          : Number(interval.end_ms);
      const endMs = Number.isFinite(endRaw) ? endRaw : this.durationMs;
      if (!Number.isFinite(startMs) || endMs <= startMs) continue;
      if (endMs < viewport.fromMs || startMs > viewport.toMs) continue;
      const x1 = Math.max(pad.left, xOf(startMs));
      const x2 = Math.min(pad.left + plotWidth, xOf(endMs));
      if (x2 <= x1) continue;
      ctx.fillStyle = intervalColor(interval.kind);
      ctx.globalAlpha = 0.15;
      ctx.fillRect(x1, pad.top, x2 - x1, plotHeight);
      ctx.globalAlpha = 1;
    }

    // 가로 격자와 시작·중간·끝 시간 눈금.
    ctx.strokeStyle = "#232b38";
    ctx.lineWidth = 1;
    for (let step = 0; step <= 4; step += 1) {
      const y = pad.top + (plotHeight * step) / 4;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + plotWidth, y);
      ctx.stroke();
    }
    ctx.fillStyle = "#8b97a8";
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "center";
    for (const ratio of [0, 0.5, 1]) {
      ctx.fillText(
        formatDuration(viewport.fromMs + span * ratio),
        pad.left + plotWidth * ratio,
        height - 9,
      );
    }
    ctx.textAlign = "start";

    // 마커 레인: 무전(청록)은 위, 사건(주황)은 아래 줄에 짧은 막대로 그린다.
    if (this.showRadioMarkers) {
      ctx.fillStyle = "#22d3ee";
      for (const marker of this.markers.radio) {
        const tMs = Number(marker.t_ms);
        if (!Number.isFinite(tMs)) continue;
        if (tMs < viewport.fromMs || tMs > viewport.toMs) continue;
        ctx.fillRect(xOf(tMs) - 1, 4, 2, 7);
      }
    }
    if (this.showEventMarkers) {
      ctx.fillStyle = "#ff9f1c";
      for (const marker of this.markers.events) {
        const tMs = Number(marker.t_ms);
        if (!Number.isFinite(tMs)) continue;
        if (tMs < viewport.fromMs || tMs > viewport.toMs) continue;
        ctx.fillRect(xOf(tMs) - 1, 13, 2, 7);
      }
    }

    // 선택 메트릭 범례와 안내. 실선(내 차)/점선(비교 차량) 시계열은 Task 6이
    // 이 영역에 그린다.
    const specs = this.selection
      .map((key) => this.catalog.get(key))
      .filter(Boolean);
    if (specs.length > 0) {
      let x = pad.left;
      const y = pad.top + 12;
      ctx.font = "600 11px system-ui, sans-serif";
      for (const spec of specs.slice(0, 6)) {
        const text = spec.unit ? `${spec.label} (${spec.unit})` : spec.label;
        ctx.fillStyle = spec.color;
        ctx.fillRect(x, y - 8, 8, 8);
        ctx.fillStyle = "#e8edf5";
        ctx.fillText(text, x + 12, y - 1);
        x += 12 + ctx.measureText(text).width + 16;
        if (x > pad.left + plotWidth - 120) break;
      }
      ctx.fillStyle = "#8b97a8";
      ctx.font = "600 13px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(
        "선택한 메트릭의 시계열이 이 영역에 그려집니다.",
        pad.left + plotWidth / 2,
        pad.top + plotHeight / 2,
      );
      ctx.textAlign = "start";
    } else {
      ctx.fillStyle = "#6d788a";
      ctx.font = "600 13px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(
        "메트릭을 선택하면 시간 흐름이 여기에 그려집니다.",
        pad.left + plotWidth / 2,
        pad.top + plotHeight / 2,
      );
      ctx.textAlign = "start";
    }

    // 현재 재생 커서(로컬 타이머 위치).
    if (this.cursorMs >= viewport.fromMs && this.cursorMs <= viewport.toMs) {
      const x = xOf(this.cursorMs);
      ctx.save();
      ctx.strokeStyle = "#e10600";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(x, pad.top);
      ctx.lineTo(x, pad.top + plotHeight);
      ctx.stroke();
      ctx.restore();
      ctx.setLineDash([]);
    }
  }
}

// ---------------------------------------------------------------------------
// 재생 시계·지도 연결

// 클래스 선언이 끝난 뒤에 만든다(클래스는 선언 전에 접근하면 TDZ 오류다).
const chart = new TelemetryChart({
  canvas: chartCanvas,
  tooltip: chartTooltip,
});
const clock = new ReplayClock(onClockChange);

function onClockChange(currentClock) {
  updatePlaybackUI(currentClock);
  chart.setCursor(currentClock.positionMs);
  if (autoFollowButton.getAttribute("aria-pressed") === "true") {
    chart.followCursor(currentClock.positionMs);
  }
  const moved = Math.abs(
    currentClock.positionMs - appState.lastMapPosition,
  );
  // 재생 중에는 10Hz 지도 프레임보다 자주 요청하지 않고, 일시정지·시크는
  // 사용자가 움직인 그 위치를 바로 반영한다.
  if (!currentClock.playing || moved >= MAP_UPDATE_MIN_MS) {
    appState.lastMapPosition = currentClock.positionMs;
    void applyMapAt(currentClock.positionMs);
  }
}

function updatePlaybackUI(currentClock) {
  const duration = Math.max(0, Math.round(currentClock.durationMs));
  playbackSeek.max = String(duration);
  if (!seeking) {
    playbackSeek.value = String(
      Math.min(Math.max(0, Math.round(currentClock.positionMs)), duration),
    );
  }
  playbackTime.textContent =
    `${formatDuration(currentClock.positionMs)} / ` +
    `${formatDuration(currentClock.durationMs)}`;
  playToggle.textContent = currentClock.playing ? "⏸" : "▶";
  playToggle.setAttribute(
    "aria-label",
    currentClock.playing ? "일시정지" : "재생",
  );
  for (const button of playbackSpeeds.querySelectorAll("[data-speed]")) {
    const active =
      Math.abs(Number(button.dataset.speed) - currentClock.speed) < 0.01;
    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  }
}

/** 재생 커서 시각의 지도 프레임을 찾아 우측 지도를 갱신한다. */
async function applyMapAt(positionMs) {
  const buffer = appState.frameBuffer;
  if (!buffer) return;
  const token = ++appState.frameToken;
  const frame = await buffer.resolve(positionMs);
  if (token !== appState.frameToken || !frame) return;
  applyMapFrame(frame, positionMs);
}

function applyMapFrame(frame, positionMs) {
  const playerIndex = appState.playerIndex;
  const cars = (Array.isArray(frame.cars) ? frame.cars : []).map((car) => ({
    ...car,
    // 지도 프레임에는 `is_player`가 없다. 참가자 인덱스로 표시해 내 차 흰
    // 테두리와 순위 배지(SCRUM-139)가 그대로 동작하게 한다.
    is_player: car.car === playerIndex,
  }));
  mapView.render({
    cars,
    flag: frame.flag,
    flags: frame.flags || null,
  });
  summaryCurrent.textContent = summaryText(frame, positionMs);
}

function summaryText(frame, positionMs) {
  const parts = [`시점 ${formatClock(positionMs)}`];
  if (Number.isInteger(frame.lap)) parts.push(`랩 ${frame.lap}`);
  if (Number.isInteger(frame.position)) parts.push(`P${frame.position}`);
  const flag = FLAG_LABELS[frame.flag];
  if (flag) parts.push(flag);
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// 녹화 열기: open → summary 300ms 폴링 → 초기화

async function init() {
  const params = new URLSearchParams(window.location.search);
  const requestedId = toInteger(params.get("id"));
  const requestedPosition = toInteger(params.get("t"));

  let recordings = [];
  let listFailed = false;
  try {
    const response = await fetch("/api/recordings?limit=50", {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    recordings = Array.isArray(data) ? data : data.recordings || [];
    if (!Array.isArray(recordings)) recordings = [];
  } catch (error) {
    listFailed = true;
    recordings = [];
  }

  // 아직 녹화 중이거나 파일이 없는 항목은 분석 API가 409/404로 답하므로 뺀다.
  const analyzable = recordings.filter(
    (recording) =>
      recording &&
      recording.end_reason !== "recording" &&
      recording.downloadable !== false,
  );
  let activeId = requestedId;
  if (activeId === null && analyzable.length > 0) {
    activeId = Number(analyzable[0].id);
  }
  if (activeId === null) {
    showOverlay(
      listFailed
        ? "녹화 목록을 불러오지 못했습니다. 잠시 뒤 새로고침하세요."
        : "분석할 녹화가 없습니다. 먼저 세션을 녹화하세요.",
    );
    recordingSelect.disabled = true;
    return;
  }

  fillRecordingSelect(analyzable, activeId);
  await openRecording(activeId, { initialPosition: requestedPosition });
}

/** 드롭다운에 분석 가능한 녹화를 최신순으로 채운다. */
function fillRecordingSelect(recordings, activeId) {
  recordingSelect.replaceChildren();
  for (const recording of recordings) {
    const option = document.createElement("option");
    option.value = String(recording.id);
    option.textContent = recordingLabel(recording);
    recordingSelect.append(option);
  }
  // URL로 바로 들어온 녹화가 목록(최근 50개) 밖이면 항목을 하나 더 만든다.
  if (!recordings.some((recording) => Number(recording.id) === activeId)) {
    const option = document.createElement("option");
    option.value = String(activeId);
    option.textContent = `녹화 #${activeId}`;
    recordingSelect.append(option);
  }
  recordingSelect.value = String(activeId);
  recordingSelect.disabled = false;
}

function recordingLabel(recording) {
  const parts = [];
  if (Number.isFinite(recording.started_at_ns)) {
    parts.push(formatDateTime(recording.started_at_ns));
  }
  parts.push(
    recording.track_name ||
      (recording.track_id !== null && recording.track_id !== undefined
        ? `서킷 ${recording.track_id}`
        : "서킷 미상"),
  );
  if (recording.category_label) parts.push(recording.category_label);
  parts.push(formatDuration(recording.duration_ms));
  return parts.join(" · ");
}

async function openRecording(recordingId, { initialPosition = null } = {}) {
  stopPolling();
  appState.pollToken += 1;
  appState.recordingId = recordingId;
  appState.ready = false;
  appState.durationMs = 0;
  appState.frameBuffer = null;
  appState.frameToken += 1;
  appState.lastMapPosition = -Infinity;
  appState.compareCar = null;

  chart.reset();
  mapView.setTrack(null, { skipLiftCoast: true });
  mapView.setCompareCar(null);
  mapView.setPreviewLabel(null);
  presetBar.replaceChildren();
  metricTree.replaceChildren();
  metricCount.textContent = "0/0";
  compareSelect.replaceChildren(newOption("없음", ""));
  compareSelect.disabled = true;
  privateBadge.hidden = true;
  summaryStatic.replaceChildren(summaryHint("녹화를 여는 중…"));
  summaryCurrent.textContent = "—";
  summaryCorners.replaceChildren();
  recordingSelect.value = String(recordingId);
  clock.pause();
  clock.setDuration(0);
  clock.seek(0);
  showOverlay("녹화를 여는 중…");
  try {
    window.history.replaceState(null, "", `/telemetry?id=${recordingId}`);
  } catch (error) {
    // 파일 URL 등 히스토리를 못 바꾸는 환경은 무시한다.
  }

  try {
    const response = await fetch("/api/replay/telemetry/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recording_id: recordingId }),
    });
    if (!response.ok) {
      await showOpenFailure(response);
      return;
    }
    const data = await response.json().catch(() => ({}));
    if (data.status === "ready") {
      const summary = await fetchSummary(recordingId);
      await applySummary(recordingId, summary, initialPosition);
      return;
    }
    pollSummary(recordingId, initialPosition, appState.pollToken);
  } catch (error) {
    showOverlay("녹화를 열지 못했습니다. 서버 연결을 확인하세요.");
  }
}

async function fetchSummary(recordingId) {
  const response = await fetch(
    `/api/replay/telemetry/summary?recording_id=${recordingId}`,
    { cache: "no-store" },
  );
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function pollSummary(recordingId, initialPosition, token) {
  appState.pollTimer = window.setTimeout(async () => {
    if (token !== appState.pollToken) return;
    try {
      const data = await fetchSummary(recordingId);
      if (token !== appState.pollToken) return;
      if (data.status === "ready") {
        await applySummary(recordingId, data, initialPosition);
        return;
      }
      if (data.status === "building") {
        const progress = Number.isFinite(data.progress) ? data.progress : 0;
        showOverlay(
          `텔레메트리 인덱스를 만드는 중… ${Math.round(progress * 100)}%`,
          progress,
        );
        pollSummary(recordingId, initialPosition, token);
        return;
      }
      showOverlay("텔레메트리 인덱스를 준비하지 못했습니다.");
    } catch (error) {
      if (token !== appState.pollToken) return;
      showOverlay("요약을 불러오지 못했습니다. 잠시 뒤 다시 시도합니다.");
      pollSummary(recordingId, initialPosition, token);
    }
  }, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (appState.pollTimer !== null) {
    window.clearTimeout(appState.pollTimer);
    appState.pollTimer = null;
  }
}

async function showOpenFailure(response) {
  if (response.status === 409) {
    showOverlay(
      "아직 녹화가 끝나지 않은 세션입니다. 녹화가 끝난 뒤 다시 열어 주세요.",
    );
    return;
  }
  if (response.status === 404) {
    showOverlay("녹화를 찾을 수 없습니다.");
    return;
  }
  if (response.status === 503) {
    showOverlay("녹화 파일을 복원할 수 없습니다. 클라우드 설정을 확인하세요.");
    return;
  }
  let message = `녹화를 열지 못했습니다(HTTP ${response.status}).`;
  try {
    const payload = await response.json();
    const detail = payload && payload.detail;
    if (detail && typeof detail.message === "string") {
      message = detail.message;
    }
  } catch (error) {
    // 응답 본문이 JSON이 아니면 기본 문구를 쓴다.
  }
  showOverlay(message);
}

/** 준비된 `summary`로 카탈로그 트리·프리셋·지도·재생을 초기화한다. */
async function applySummary(recordingId, data, initialPosition) {
  if (!data || data.status !== "ready") {
    showOverlay("텔레메트리 인덱스를 준비하지 못했습니다.");
    return;
  }

  const durationMs = Number.isFinite(data.duration_ms)
    ? data.duration_ms
    : Number(data.recording && data.recording.duration_ms) || 0;
  appState.ready = true;
  appState.durationMs = durationMs;
  appState.playerIndex = Number.isInteger(data.player_index)
    ? data.player_index
    : null;
  appState.participants = Array.isArray(data.participants)
    ? data.participants
    : [];
  appState.catalog = enrichCatalog(data.catalog);
  appState.presets = Array.isArray(data.presets) ? data.presets : [];
  appState.metricByKey = new Map();
  for (const category of appState.catalog) {
    for (const metric of category.metrics) {
      appState.metricByKey.set(metric.key, metric);
    }
  }
  appState.markers = data.markers && typeof data.markers === "object"
    ? data.markers
    : { ...EMPTY_MARKERS };
  appState.recording = data.recording || {};
  appState.track = data.track || null;

  buildMetricTree(appState.catalog);
  renderPresets(appState.presets);
  fillCompareSelect(appState.participants);
  renderSummaryStatic(data, durationMs);

  mapView.setTrack(appState.track, { skipLiftCoast: true });
  chart.setCatalog(appState.catalog);
  chart.setRecording({ duration_ms: durationMs });
  chart.setMarkers(appState.markers);

  const options = loadOptions();
  applyOptions(options);
  restoreSelection();

  clock.setDuration(durationMs);
  appState.frameBuffer = new MapFrameBuffer(recordingId, {
    chunkMs: CHUNK_MS,
    durationMs,
  });
  const start = Number.isFinite(initialPosition)
    ? Math.max(0, Math.min(initialPosition, durationMs))
    : 0;
  clock.seek(start);
  applyMapAt(start);

  hideOverlay();
  recordingSelect.disabled = false;
  const trackName = appState.track && appState.track.name;
  document.title = `${trackName || "텔레메트리 분석"} · F1 레이스 엔지니어`;
}

// ---------------------------------------------------------------------------
// 메트릭 카탈로그 트리

/** 서버 카탈로그에 화면 전용 색만 더한다(라벨·단위·그룹은 서버 값 그대로). */
function enrichCatalog(catalog) {
  let index = 0;
  return (Array.isArray(catalog) ? catalog : []).map((category) => ({
    ...category,
    metrics: (category.metrics || []).map((metric) => {
      const enriched = {
        ...metric,
        color: SWATCH_COLORS[index % SWATCH_COLORS.length],
      };
      index += 1;
      return enriched;
    }),
  }));
}

function buildMetricTree(catalog) {
  metricTree.replaceChildren();
  const collapsed = loadCollapsed();
  for (const category of catalog) {
    const section = document.createElement("section");
    section.className = "metric-category";
    section.dataset.category = category.id;

    const head = document.createElement("div");
    head.className = "metric-category-head";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "tree-toggle";
    toggle.setAttribute("aria-expanded", "true");
    toggle.textContent = "▾";
    const categoryCheck = document.createElement("input");
    categoryCheck.type = "checkbox";
    categoryCheck.className = "category-check";
    categoryCheck.dataset.category = category.id;
    categoryCheck.setAttribute("aria-label", `${category.label} 전체 선택`);
    const label = document.createElement("span");
    label.className = "category-label";
    label.textContent = category.label;
    const count = document.createElement("span");
    count.className = "category-count";
    count.textContent = `(0/${category.metrics.length})`;
    head.append(toggle, categoryCheck, label, count);
    section.append(head);

    const list = document.createElement("div");
    list.className = "metric-list";
    let index = 0;
    while (index < category.metrics.length) {
      const metric = category.metrics[index];
      if (metric.wheel_group) {
        const group = [];
        while (
          index < category.metrics.length &&
          category.metrics[index].wheel_group === metric.wheel_group
        ) {
          group.push(category.metrics[index]);
          index += 1;
        }
        list.append(buildWheelRow(group));
      } else {
        list.append(buildMetricRow(metric));
        index += 1;
      }
    }
    section.append(list);

    if (collapsed[category.id]) {
      section.classList.add("collapsed");
      toggle.textContent = "▸";
      toggle.setAttribute("aria-expanded", "false");
    }
    metricTree.append(section);
  }
}

function buildMetricRow(metric) {
  const row = document.createElement("label");
  row.className = "metric-row";
  row.append(
    metricCheckbox(metric, "metric-check"),
    metricSwatch(metric),
    span("metric-label", metric.label),
    span("metric-unit", metric.unit || ""),
    valueSpan(metric.key, "metric-value"),
  );
  return row;
}

function buildWheelRow(group) {
  const first = group[0];
  const row = document.createElement("div");
  row.className = "metric-row wheel-row";
  row.dataset.wheelGroup = first.wheel_group;

  const groupCheck = document.createElement("input");
  groupCheck.type = "checkbox";
  groupCheck.className = "wheel-check";
  groupCheck.dataset.wheelGroup = first.wheel_group;
  groupCheck.setAttribute("aria-label", `${first.label} 네 바퀴 전체 선택`);

  const chips = document.createElement("div");
  chips.className = "wheel-chips";
  for (const metric of group) {
    const suffix = metric.key.slice(metric.key.lastIndexOf("_") + 1);
    const chip = document.createElement("label");
    chip.className = "wheel-chip";
    chip.style.setProperty("--metric-color", metric.color);
    chip.title = metric.wheel_label || metric.label;

    const check = metricCheckbox(metric, "metric-check");
    check.setAttribute(
      "aria-label",
      `${metric.label} ${metric.wheel_label || suffix}`,
    );
    const name = span("", WHEEL_LABELS[suffix] || metric.wheel_label || suffix);
    const value = valueSpan(metric.key, "wheel-value");
    chip.append(check, name, value);
    chips.append(chip);
  }

  row.append(
    groupCheck,
    span("metric-label", first.label),
    span("metric-unit", first.unit || ""),
    chips,
  );
  return row;
}

function metricCheckbox(metric, className) {
  const check = document.createElement("input");
  check.type = "checkbox";
  check.className = className;
  check.dataset.metric = metric.key;
  check.setAttribute("aria-label", `${metric.label} 선택`);
  return check;
}

function metricSwatch(metric) {
  const swatch = document.createElement("span");
  swatch.className = "metric-swatch";
  swatch.style.setProperty("--metric-color", metric.color);
  return swatch;
}

function valueSpan(key, className) {
  const node = document.createElement("span");
  node.className = className;
  node.dataset.valueFor = key;
  node.textContent = "—";
  return node;
}

function span(className, text) {
  const node = document.createElement("span");
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

/** 현재 선택을 체크박스·카테고리 indeterminate·개수에 반영한다. */
function syncTreeChecks() {
  metricCount.textContent = `${selection.size}/${appState.metricByKey.size}`;
  for (const section of metricTree.querySelectorAll(".metric-category")) {
    const inputs = [...section.querySelectorAll("input[data-metric]")];
    let selectedCount = 0;
    for (const input of inputs) {
      const checked = selection.has(input.dataset.metric);
      input.checked = checked;
      const chip = input.closest(".wheel-chip");
      if (chip) chip.classList.toggle("on", checked);
      if (checked) selectedCount += 1;
    }
    const categoryCheck = section.querySelector(".category-check");
    if (categoryCheck) {
      categoryCheck.checked =
        inputs.length > 0 && selectedCount === inputs.length;
      categoryCheck.indeterminate =
        selectedCount > 0 && selectedCount < inputs.length;
    }
    const count = section.querySelector(".category-count");
    if (count) count.textContent = `(${selectedCount}/${inputs.length})`;
  }
  for (const row of metricTree.querySelectorAll(".wheel-row")) {
    const inputs = [...row.querySelectorAll("input[data-metric]")];
    const selectedCount = inputs.filter((input) =>
      selection.has(input.dataset.metric),
    ).length;
    const groupCheck = row.querySelector(".wheel-check");
    if (groupCheck) {
      groupCheck.checked =
        inputs.length > 0 && selectedCount === inputs.length;
      groupCheck.indeterminate =
        selectedCount > 0 && selectedCount < inputs.length;
    }
  }
}

/** 선택 키 집합을 바꾸고 트리·차트·프리셋·localStorage에 함께 반영한다. */
function setSelection(keys) {
  const valid = [];
  for (const key of Array.isArray(keys) ? keys : []) {
    if (appState.metricByKey.has(key) && !valid.includes(key)) valid.push(key);
  }
  selection = new Set(valid);
  syncTreeChecks();
  chart.setSelection(valid);
  updateMetricValues(null);
  updatePresetActive();
  saveSelection(valid);
}

function restoreSelection() {
  const stored = loadSelection();
  let initialKeys = null;
  if (stored !== null) {
    const valid = stored.filter((key) => appState.metricByKey.has(key));
    // 저장된 키가 전부 사라졌으면(카탈로그 변경) 기본 프리셋으로 돌아간다.
    if (valid.length > 0 || stored.length === 0) initialKeys = valid;
  }
  if (initialKeys === null) {
    initialKeys =
      appState.presets.length > 0 && Array.isArray(appState.presets[0].metrics)
        ? appState.presets[0].metrics
        : [];
  }
  setSelection(initialKeys);
}

/** 커서 시점 값 자리를 채운다. Task 6이 `/series` 응답 값으로 부른다. */
function updateMetricValues(values) {
  for (const node of metricTree.querySelectorAll("[data-value-for]")) {
    const key = node.dataset.valueFor;
    const value = values ? values[key] : undefined;
    node.textContent = formatMetricValue(appState.metricByKey.get(key), value);
  }
}

function formatMetricValue(spec, value) {
  if (!Number.isFinite(value)) return "—";
  if ((spec && spec.step) || Number.isInteger(value)) {
    return String(Math.round(value));
  }
  return value.toFixed(1);
}

// ---------------------------------------------------------------------------
// 프리셋·비교 차량

function renderPresets(presets) {
  presetBar.replaceChildren();
  for (const preset of presets) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "preset-button";
    button.dataset.preset = preset.id;
    button.textContent = preset.label;
    if (preset.description) button.title = preset.description;
    button.addEventListener("click", () => {
      setSelection(Array.isArray(preset.metrics) ? preset.metrics : []);
    });
    presetBar.append(button);
  }
}

function updatePresetActive() {
  for (const button of presetBar.querySelectorAll(".preset-button")) {
    const preset = appState.presets.find(
      (item) => item.id === button.dataset.preset,
    );
    const active = preset ? sameMetricSet(preset.metrics, selection) : false;
    button.classList.toggle("on", Boolean(active));
  }
}

function sameMetricSet(metrics, selected) {
  if (!Array.isArray(metrics) || metrics.length !== selected.size) return false;
  return metrics.every((key) => selected.has(key));
}

function fillCompareSelect(participants) {
  compareSelect.replaceChildren(newOption("없음", ""));
  for (const participant of participants) {
    if (participant.is_player) continue;
    const option = document.createElement("option");
    option.value = String(participant.car_index);
    option.textContent =
      participant.your_telemetry === 0
        ? `${participant.name} (비공개)`
        : participant.name;
    compareSelect.append(option);
  }
  compareSelect.disabled = false;
}

/** 비교 차량 지정. 비공개 드라이버면 상단 제한 안내 배지를 켠다. */
function applyCompareCar(rawValue) {
  const numeric =
    rawValue === "" || rawValue === null || rawValue === undefined
      ? NaN
      : Number(rawValue);
  const participant = Number.isInteger(numeric)
    ? appState.participants.find(
        (item) => item.car_index === numeric && !item.is_player,
      )
    : null;
  const index = participant ? numeric : null;
  appState.compareCar = index;
  mapView.setCompareCar(index);
  chart.setCompareCar(index);
  privateBadge.hidden = !(participant && participant.your_telemetry === 0);
  saveOption("compareCar", index);
}

// ---------------------------------------------------------------------------
// 요약 패널

function renderSummaryStatic(data, durationMs) {
  summaryStatic.replaceChildren();
  const track = data.track || {};
  const recording = data.recording || {};
  const category =
    CATEGORY_LABELS[recording.category] ||
    (recording.category ? String(recording.category) : "미분류");
  const player = appState.participants.find((item) => item.is_player);
  const cornerCount = Array.isArray(track.corners) ? track.corners.length : 0;
  const totalMetrics = appState.metricByKey.size;
  const lines = [
    `${track.name || "서킷 미상"} · ${category} · ${formatDuration(durationMs)}`,
    `내 차: ${player ? player.name : "알 수 없음"}`,
    `메트릭 ${totalMetrics}개 · 코너 ${cornerCount}개`,
  ];
  for (const text of lines) {
    summaryStatic.append(summaryHint(text));
  }

  summaryCorners.replaceChildren();
  for (const corner of (track.corners || []).slice(0, 30)) {
    const chip = document.createElement("span");
    chip.className = "corner-chip";
    chip.textContent = String(corner.number);
    summaryCorners.append(chip);
  }
}

function summaryHint(text) {
  const node = document.createElement("p");
  node.className = "summary-hint";
  node.textContent = text;
  return node;
}

// ---------------------------------------------------------------------------
// 저장소(localStorage)

function loadSelection() {
  try {
    const raw = window.localStorage.getItem(METRIC_STORAGE_KEY);
    if (raw === null) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((key) => typeof key === "string")
      : null;
  } catch (error) {
    return null;
  }
}

function saveSelection(keys) {
  try {
    window.localStorage.setItem(METRIC_STORAGE_KEY, JSON.stringify(keys));
  } catch (error) {
    // 저장 공간이 없거나 사생활 보호 모드면 이번 세션에서만 유지한다.
  }
}

function loadCollapsed() {
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(COLLAPSED_STORAGE_KEY) || "{}",
    );
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (error) {
    return {};
  }
}

function saveCollapsed(state) {
  try {
    window.localStorage.setItem(COLLAPSED_STORAGE_KEY, JSON.stringify(state));
  } catch (error) {
    // 저장 실패는 무시한다.
  }
}

function loadOptions() {
  const defaults = {
    autoFollow: false,
    radioMarkers: true,
    eventMarkers: true,
    compareCar: null,
  };
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(OPTIONS_STORAGE_KEY) || "null",
    );
    return parsed && typeof parsed === "object"
      ? { ...defaults, ...parsed }
      : defaults;
  } catch (error) {
    return defaults;
  }
}

function saveOptions(options) {
  try {
    window.localStorage.setItem(OPTIONS_STORAGE_KEY, JSON.stringify(options));
  } catch (error) {
    // 저장 실패는 무시한다.
  }
}

function saveOption(key, value) {
  const options = loadOptions();
  options[key] = value;
  saveOptions(options);
}

/** 저장된 옵션(자동 따라가기·마커 토글·비교 차량)을 화면과 차트에 반영한다. */
function applyOptions(options) {
  const autoFollow = Boolean(options.autoFollow);
  autoFollowButton.setAttribute("aria-pressed", autoFollow ? "true" : "false");
  chart.setAutoFollow(autoFollow);

  radioMarkerToggle.checked = options.radioMarkers !== false;
  eventMarkerToggle.checked = options.eventMarkers !== false;
  chart.setMarkerVisibility({
    radio: radioMarkerToggle.checked,
    events: eventMarkerToggle.checked,
  });

  const compare = Number.isInteger(options.compareCar)
    ? String(options.compareCar)
    : "";
  const valid = appState.participants.some(
    (item) => String(item.car_index) === compare && !item.is_player,
  );
  compareSelect.value = valid ? compare : "";
  applyCompareCar(valid ? compare : "");
}

// ---------------------------------------------------------------------------
// 로딩 오버레이

function showOverlay(text, progress = null) {
  chartOverlay.hidden = false;
  chartOverlayText.textContent = text;
  if (Number.isFinite(progress)) {
    progressWrap.hidden = false;
    progressFill.style.width = `${Math.round(progress * 100)}%`;
  } else {
    progressWrap.hidden = true;
    progressFill.style.width = "0%";
  }
}

function hideOverlay() {
  chartOverlay.hidden = true;
}

// ---------------------------------------------------------------------------
// 이벤트 연결

recordingSelect.addEventListener("change", () => {
  const value = Number(recordingSelect.value);
  if (!Number.isInteger(value)) return;
  void openRecording(value);
});

compareSelect.addEventListener("change", () => {
  applyCompareCar(compareSelect.value);
});

playToggle.addEventListener("click", () => {
  clock.toggle();
});

playbackSpeeds.addEventListener("click", (event) => {
  const button = event.target.closest("[data-speed]");
  if (!button) return;
  clock.setSpeed(Number(button.dataset.speed));
});

playbackSeek.addEventListener("pointerdown", () => {
  seeking = true;
});

playbackSeek.addEventListener("input", () => {
  seeking = true;
  clock.seek(Number(playbackSeek.value));
});

playbackSeek.addEventListener("change", () => {
  seeking = false;
  clock.seek(Number(playbackSeek.value));
});

zoomReset.addEventListener("click", () => {
  chart.resetZoom();
});

autoFollowButton.addEventListener("click", () => {
  const next = autoFollowButton.getAttribute("aria-pressed") !== "true";
  autoFollowButton.setAttribute("aria-pressed", next ? "true" : "false");
  chart.setAutoFollow(next);
  saveOption("autoFollow", next);
});

radioMarkerToggle.addEventListener("change", () => {
  chart.setMarkerVisibility({
    radio: radioMarkerToggle.checked,
    events: eventMarkerToggle.checked,
  });
  saveOption("radioMarkers", radioMarkerToggle.checked);
});

eventMarkerToggle.addEventListener("change", () => {
  chart.setMarkerVisibility({
    radio: radioMarkerToggle.checked,
    events: eventMarkerToggle.checked,
  });
  saveOption("eventMarkers", eventMarkerToggle.checked);
});

metricTree.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const toggle = target.closest(".tree-toggle");
  if (!toggle) return;
  const section = toggle.closest(".metric-category");
  if (!section) return;
  const collapsed = section.classList.toggle("collapsed");
  toggle.textContent = collapsed ? "▸" : "▾";
  toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  const stored = loadCollapsed();
  stored[section.dataset.category] = collapsed;
  saveCollapsed(stored);
});

metricTree.addEventListener("change", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement)) return;

  if (input.classList.contains("category-check")) {
    const category = appState.catalog.find(
      (item) => item.id === input.dataset.category,
    );
    if (!category) return;
    const next = new Set(selection);
    for (const metric of category.metrics) {
      if (input.checked) next.add(metric.key);
      else next.delete(metric.key);
    }
    setSelection([...next]);
    return;
  }

  if (input.classList.contains("wheel-check")) {
    const next = new Set(selection);
    for (const category of appState.catalog) {
      for (const metric of category.metrics) {
        if (metric.wheel_group !== input.dataset.wheelGroup) continue;
        if (input.checked) next.add(metric.key);
        else next.delete(metric.key);
      }
    }
    setSelection([...next]);
    return;
  }

  if (input.dataset.metric) {
    const next = new Set(selection);
    if (input.checked) next.add(input.dataset.metric);
    else next.delete(input.dataset.metric);
    setSelection([...next]);
  }
});

// ---------------------------------------------------------------------------
// 도우미

function intervalColor(kind) {
  if (kind === "safety_car") return "#f2c200";
  if (kind === "vsc") return "#e0a800";
  if (kind === "red_flag") return "#e10600";
  if (kind === "pit") return "#22d3ee";
  return "#8b97a8";
}

/** 정렬된 숫자 배열에서 `target`보다 큰 첫 인덱스(이진 탐색). */
function upperBound(sorted, target) {
  let low = 0;
  let high = sorted.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (sorted[middle] <= target) low = middle + 1;
    else high = middle;
  }
  return low;
}

function formatDuration(ms) {
  const total = Number.isFinite(ms) && ms > 0 ? Math.floor(ms / 1000) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const two = (value) => String(value).padStart(2, "0");
  if (hours > 0) return `${hours}:${two(minutes)}:${two(seconds)}`;
  return `${minutes}:${two(seconds)}`;
}

/** 미리보기·현재 시점처럼 0.1초까지 보여 주는 시각 표기(12:34.2). */
function formatClock(ms) {
  const total = Number.isFinite(ms) && ms > 0 ? ms : 0;
  const minutes = Math.floor(total / 60_000);
  const seconds = Math.floor((total % 60_000) / 1000);
  const tenths = Math.floor((total % 1000) / 100);
  return `${minutes}:${String(seconds).padStart(2, "0")}.${tenths}`;
}

function formatDateTime(ns) {
  if (!Number.isFinite(ns)) return "시각 미상";
  const date = new Date(ns / 1e6);
  return date.toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function toInteger(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isInteger(number) ? number : null;
}

function newOption(text, value) {
  const option = document.createElement("option");
  option.textContent = text;
  option.value = value;
  return option;
}

void init();
