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
 * - 차트 본체는 `TelemetryChart` 클래스로 분리한다(SCRUM-137). 단위 그룹별
 *   0~100% 정규화·좌우 2개 Y축·내 차 실선/비교 차 점선·계단형 선, 상단 무전/
 *   사건 마커 레인과 랩 경계, 드래그·휠 줌과 Shift+드래그 팬, 120ms 디바운스
 *   `/series` 재조회, 호버 툴팁(내 차·비교·Δ)·지도 미리보기·클릭 시크를 모두
 *   이 클래스와 아래 화면 연결 코드가 담당한다.
 */

import { TrackMapView } from "./map.js";

// `summary`가 `building`일 때 폴링 간격(ms). 설계 7의 300ms.
const POLL_INTERVAL_MS = 300;
// 지도 프레임 청크 길이(ms). 10Hz 300프레임을 한 번에 받는다.
const CHUNK_MS = 30_000;
// 재생 중 지도 갱신 최소 간격(ms). 프레임 자체가 10Hz이므로 그보다 자주
// 요청하지 않는다.
const MAP_UPDATE_MIN_MS = 90;

// 그래프 상호작용(설계 5.2-2, SCRUM-137).
// 뷰포트가 바뀌면 이만큼 기다렸다가 `/series`를 다시 부른다(휠·드래그 연속
// 변경을 한 번의 요청으로 합친다).
const SERIES_DEBOUNCE_MS = 120;
// 호버 지도 미리보기 요청 간격(ms). 마우스가 빠르게 움직여도 50ms에 한 번만
// 보낸다.
const PREVIEW_DEBOUNCE_MS = 50;
// 클릭과 드래그 구간 선택을 가르는 이동 거리(px).
const CLICK_DRAG_PX = 5;
// 확대 최소 폭(ms). 너무 좁히면 축·툴팁이 무의미해진다.
const MIN_ZOOM_MS = 1_000;
// `/series` 요청 점 수 범위. 캔버스 폭에 맞춰 이 사이에서 고른다.
const MIN_SERIES_POINTS = 400;
const MAX_SERIES_POINTS = 2_000;
// 상단 마커 레인 높이(px). 무전은 윗줄, 사건은 아랫줄이다.
const MARKER_LANE_HEIGHT = 24;
// 마커 아이콘 클릭 판정 반경(px).
const MARKER_HIT_PX = 8;
// 시간축 눈금 후보(ms). 보이는 폭에 맞춰 가장 촘촘한 것을 고른다.
const TIME_TICK_STEPS_MS = [
  1_000,
  2_000,
  5_000,
  10_000,
  15_000,
  30_000,
  60_000,
  120_000,
  300_000,
  600_000,
  1_200_000,
  1_800_000,
];
// 사건 종류별 아이콘(설계 5.2 "사건(🚩/↺/⚠️) 마커").
const EVENT_ICONS = {
  overtake: "🚩",
  overtaken: "🚩",
  flashback: "↺",
  penalty: "⚠️",
  lap_complete: "🏁",
};
const EVENT_LABELS = {
  overtake: "추월",
  overtaken: "피추월",
  flashback: "플래시백",
  penalty: "페널티",
  lap_complete: "랩 완료",
  safety_car: "세이프티 카",
  vsc: "VSC",
  red_flag: "적기",
  pit: "피트",
};
// 사건 `detail`(원본 이벤트 데이터)의 한국어 라벨. 모르는 키는 키 이름을 쓴다.
const EVENT_DETAIL_LABELS = {
  penalty_type: "페널티",
  infringement_type: "위반",
  time_ms: "시간(ms)",
  lap_num: "랩",
  places_gained: "순위 상승",
  flashback_frame_identifier: "프레임",
  flashback_session_time: "세션 시각(s)",
  overtaking_vehicle_idx: "추월 차량",
  being_overtaken_vehicle_idx: "피추월 차량",
};

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
  // 시계열 조회·호버 미리보기(SCRUM-137).
  seriesToken: 0,
  seriesTimer: null,
  hoverMs: null,
  previewTimer: null,
  lastValuePosition: -Infinity,
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

  /** 해당 시각이 든 청크가 이미 캐시에 있는지. 호버 미리보기의 즉시 응답 판단. */
  hasFrameAt(tMs) {
    if (!this.frames.length) return false;
    const chunkStart = this._chunkStart(tMs);
    if (!this.loadedChunks.has(chunkStart)) return false;
    const frame = this.frameAt(tMs);
    return Boolean(frame && frame.t_ms >= chunkStart);
  }

  /** 단건 응답 프레임 하나를 캐시에 합친다(호버 미리보기 재사용). */
  put(frame) {
    if (frame && Number.isFinite(frame.t_ms)) this._merge([frame]);
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
    this.participants = [];
    this.autoFollow = false;
    this.showRadioMarkers = true;
    this.showEventMarkers = true;
    this.zoom = null;
    this.metricValues = null;
    // `/series` 응답의 `series` 맵(메트릭 키 → 시리즈·비교·비공개 플래그).
    this.series = null;
    // 이번 프레임에서 그린 마커 위치(호버 판정용).
    this.markerHits = [];
    this.markerHover = null;
    this.hoverMs = null;
    this.pointer = null;
    this.drag = null;
    this.layout = null;
    // 화면(app)이 연결하는 콜백. 차트는 값만 알리고 화면 전환은 하지 않는다.
    this.onHover = null;
    this.onSeek = null;
    this.onViewportChange = null;
    this._draw = this._draw.bind(this);
    this._bindInteractions();
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
    this.participants = [];
    this.zoom = null;
    this.metricValues = null;
    this.series = null;
    this.markerHits = [];
    this.markerHover = null;
    this.hoverMs = null;
    this.pointer = null;
    this.drag = null;
    this.layout = null;
    this._setPanningClass(false);
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

  /** 커서·호버 시점 값을 넘길 자리(툴팁·하단 값 표시가 함께 쓴다). */
  setMetricValues(values) {
    this.metricValues = values || null;
  }

  /** 참가자 목록. 사건 툴팁에서 `car_index`를 이름으로 바꿀 때 쓴다. */
  setParticipants(participants) {
    this.participants = Array.isArray(participants) ? participants : [];
    this._draw();
  }

  /** `/api/replay/telemetry/series` 응답. 메트릭별 시계열·비교·비공개 플래그. */
  setSeries(payload) {
    this.series =
      payload && payload.series && typeof payload.series === "object"
        ? payload.series
        : null;
    this._draw();
  }

  /** 캔버스 폭에 맞는 `/series` 점 수(설계 5.1.D 기본 800·상한 2000). */
  pointBudget() {
    const width = this.canvas ? this.canvas.clientWidth : 800;
    return Math.max(
      MIN_SERIES_POINTS,
      Math.min(MAX_SERIES_POINTS, Math.round(width * 1.5)),
    );
  }

  /** 현재 뷰포트. 확대하지 않았으면 전체 세션 구간이다. */
  viewport() {
    if (this.zoom && this.zoom.toMs > this.zoom.fromMs) return this.zoom;
    return { fromMs: 0, toMs: Math.max(1, this.durationMs) };
  }

  /**
   * `tMs` 시점의 메트릭별 값. 호버 툴팁과 하단 체크박스 현재 값이 함께 쓴다.
   * 이산(`step`) 값은 그 시점 이전의 마지막 값, 연속 값은 가장 가까운 점을 쓴다.
   */
  valuesAt(tMs) {
    const result = {};
    if (!this.series) return result;
    for (const key of this.selection) {
      const entry = this.series[key];
      if (!entry) continue;
      result[key] = {
        value: sampleSeries(entry.series, tMs, Boolean(entry.step)),
        compare: sampleSeries(entry.compare_series, tMs, Boolean(entry.step)),
        restricted: Boolean(entry.compare_restricted),
        available: Boolean(entry.compare_available),
      };
    }
    return result;
  }

  showTooltip(text, x, y) {
    this.showTooltipAt(text, x, y);
  }

  /** 툴팁이 캔버스 밖으로 나가지 않게 위치를 뒤집어 배치한다. */
  showTooltipAt(text, x, y) {
    if (!this.tooltip || !text) return;
    this.tooltip.textContent = text;
    this.tooltip.hidden = false;
    const wrap = this.canvas ? this.canvas.parentElement : null;
    const maxX = wrap ? wrap.clientWidth : this.canvas ? this.canvas.clientWidth : 0;
    const maxY = wrap ? wrap.clientHeight : this.canvas ? this.canvas.clientHeight : 0;
    const tooltipWidth = this.tooltip.offsetWidth || 240;
    const tooltipHeight = this.tooltip.offsetHeight || 80;
    let left = x + 14;
    let top = y + 14;
    if (left + tooltipWidth > maxX - 8) left = Math.max(8, x - tooltipWidth - 14);
    if (top + tooltipHeight > maxY - 8) top = Math.max(8, y - tooltipHeight - 14);
    this.tooltip.style.left = `${Math.round(left)}px`;
    this.tooltip.style.top = `${Math.round(top)}px`;
  }

  hideTooltip() {
    if (!this.tooltip) return;
    this.tooltip.hidden = true;
    this.tooltip.textContent = "";
  }

  /** 더블클릭·`[줌 초기화]` 버튼. 전체 세션 보기로 돌아간다. */
  resetZoom() {
    const hadZoom = Boolean(this.zoom);
    this.zoom = null;
    this._draw();
    if (hadZoom) this._notifyViewport();
  }

  /** 확대 상태에서 커서가 화면 밖으로 나가면 뷰포트를 따라 민다(자동 따라가기). */
  followCursor(positionMs) {
    if (!this.zoom || !(this.durationMs > 0)) return;
    const width = Math.max(MIN_ZOOM_MS, this.zoom.toMs - this.zoom.fromMs);
    if (positionMs >= this.zoom.fromMs && positionMs <= this.zoom.toMs) return;
    const maxFrom = Math.max(0, this.durationMs - width);
    const from = Math.max(0, Math.min(positionMs - width * 0.9, maxFrom));
    const next = {
      fromMs: from,
      toMs: Math.min(this.durationMs, from + width),
    };
    if (next.fromMs === this.zoom.fromMs && next.toMs === this.zoom.toMs) {
      return;
    }
    this.zoom = next;
    this._draw();
    this._notifyViewport();
  }

  // --- 마우스 상호작용(줌·팬·호버·클릭) -------------------------------

  _bindInteractions() {
    if (!this.canvas) return;
    this.canvas.addEventListener("pointerdown", (event) =>
      this._onPointerDown(event),
    );
    this.canvas.addEventListener("pointermove", (event) =>
      this._onPointerMove(event),
    );
    this.canvas.addEventListener("pointerup", (event) =>
      this._onPointerUp(event),
    );
    this.canvas.addEventListener("pointercancel", () => this._onPointerCancel());
    this.canvas.addEventListener("pointerleave", () => this._onPointerLeave());
    this.canvas.addEventListener("wheel", (event) => this._onWheel(event), {
      passive: false,
    });
    this.canvas.addEventListener("dblclick", (event) => {
      event.preventDefault();
      this.resetZoom();
    });
  }

  _point(event) {
    const rect = this.canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  /** 캔버스 x 좌표 → 현재 뷰포트의 시각(ms). */
  _timeAt(x) {
    const viewport = this.viewport();
    if (!this.layout) return viewport.fromMs;
    const ratio = Math.max(
      0,
      Math.min(1, (x - this.layout.pad.left) / this.layout.plotWidth),
    );
    return viewport.fromMs + (viewport.toMs - viewport.fromMs) * ratio;
  }

  _insidePlot(point) {
    if (!this.layout || !point) return false;
    const { pad, plotWidth, plotHeight } = this.layout;
    return (
      point.x >= pad.left &&
      point.x <= pad.left + plotWidth &&
      point.y >= pad.top &&
      point.y <= pad.top + plotHeight
    );
  }

  _setPanningClass(on) {
    const wrap = this.canvas ? this.canvas.parentElement : null;
    if (wrap) wrap.classList.toggle("panning", Boolean(on));
  }

  _onPointerDown(event) {
    if (!this.canvas || event.button !== 0 || !(this.durationMs > 0)) return;
    const point = this._point(event);
    // 마커는 툴팁만 보여 준다(드래그·시크 시작점이 아니다).
    if (this._markerAt(point.x, point.y)) return;
    if (!this._insidePlot(point)) return;
    this.drag = {
      mode: event.shiftKey ? "pan" : "select",
      pointerId: event.pointerId,
      startX: point.x,
      startY: point.y,
      currentX: point.x,
      currentY: point.y,
      startViewport: { ...this.viewport() },
      moved: false,
    };
    if (this.canvas.setPointerCapture) {
      try {
        this.canvas.setPointerCapture(event.pointerId);
      } catch (error) {
        // 포인터 캡처를 지원하지 않는 환경은 mouse 이벤트로 계속 동작한다.
      }
    }
    if (this.drag.mode === "pan") this._setPanningClass(true);
    this.hideTooltip();
    event.preventDefault();
  }

  _onPointerMove(event) {
    if (!this.canvas || !(this.durationMs > 0)) return;
    const point = this._point(event);
    this.pointer = point;

    if (this.drag) {
      const drag = this.drag;
      const dx = point.x - drag.startX;
      const dy = point.y - drag.startY;
      if (Math.hypot(dx, dy) >= CLICK_DRAG_PX) drag.moved = true;
      if (drag.mode === "pan" && drag.moved) {
        const start = drag.startViewport;
        const span = start.toMs - start.fromMs;
        const deltaMs = (-dx / Math.max(1, this.layout.plotWidth)) * span;
        const from = Math.max(
          0,
          Math.min(start.fromMs + deltaMs, this.durationMs - span),
        );
        this.zoom =
          from <= 0 && from + span >= this.durationMs
            ? null
            : { fromMs: from, toMs: Math.min(this.durationMs, from + span) };
      } else if (drag.mode === "select") {
        drag.currentX = point.x;
        drag.currentY = point.y;
      }
      this._draw();
      return;
    }

    // 마커 레인 호버가 시계열 호버보다 우선한다.
    const marker = this._markerAt(point.x, point.y);
    if (marker) {
      const hadHover = this.hoverMs !== null;
      this.markerHover = marker;
      this.hoverMs = null;
      if (hadHover && this.onHover) this.onHover(null);
      this._draw();
      this.showTooltipAt(this._markerTooltip(marker), point.x, point.y);
      return;
    }
    this.markerHover = null;

    if (this._insidePlot(point)) {
      const tMs = this._timeAt(point.x);
      this.hoverMs = tMs;
      this._draw();
      this.showTooltipAt(this._hoverTooltip(tMs), point.x, point.y);
      if (this.onHover) this.onHover(tMs);
    } else if (this.hoverMs !== null) {
      this.hoverMs = null;
      this.hideTooltip();
      this._draw();
      if (this.onHover) this.onHover(null);
    }
  }

  _onPointerUp(event) {
    if (!this.drag) return;
    const drag = this.drag;
    const point = this._point(event);
    this.drag = null;
    this._setPanningClass(false);
    if (this.canvas.releasePointerCapture) {
      try {
        this.canvas.releasePointerCapture(event.pointerId);
      } catch (error) {
        // 이미 해제됐거나 캡처하지 않은 포인터는 무시한다.
      }
    }

    if (drag.mode === "select" && drag.moved) {
      const startX = Math.min(drag.startX, point.x);
      const endX = Math.max(drag.startX, point.x);
      const t1 = this._timeAt(startX);
      const t2 = this._timeAt(endX);
      if (t2 - t1 >= MIN_ZOOM_MS) {
        this._setViewport(t1, t2);
      } else {
        // 너무 좁은 드래그는 확대 대신 그 시점으로 이동한다.
        this.hoverMs = null;
        this.hideTooltip();
        if (this.onSeek) this.onSeek(this._timeAt(point.x));
      }
    } else if (drag.mode === "select") {
      // 드래그 거리 < 5px인 단순 클릭은 그 시점으로 즉시 이동(0ms)이다.
      // 호버 상태를 먼저 풀어야 지도가 미리보기가 아니라 새 재생 커서를 따른다.
      this.hoverMs = null;
      this.hideTooltip();
      if (this.onSeek) this.onSeek(this._timeAt(point.x));
    } else if (drag.mode === "pan" && drag.moved) {
      this._notifyViewport();
    }
    this._draw();
  }

  _onPointerCancel() {
    this.drag = null;
    this._setPanningClass(false);
    this.hoverMs = null;
    this.markerHover = null;
    this.hideTooltip();
    this._draw();
    if (this.onHover) this.onHover(null);
  }

  _onPointerLeave() {
    const hadHover = this.hoverMs !== null;
    this.pointer = null;
    this.hoverMs = null;
    this.markerHover = null;
    // 포인터 캡처 중 드래그는 캔버스 밖으로 나가도 이어서 유지한다.
    if (!this.drag) this._setPanningClass(false);
    this.hideTooltip();
    this._draw();
    if (hadHover && this.onHover) this.onHover(null);
  }

  _onWheel(event) {
    if (!(this.durationMs > 0)) return;
    const point = this._point(event);
    if (!this._insidePlot(point)) return;
    event.preventDefault();
    const viewport = this.viewport();
    const span = Math.max(1, viewport.toMs - viewport.fromMs);
    const factor = Math.exp((Number(event.deltaY) || 0) * 0.0015);
    const minSpan = Math.min(MIN_ZOOM_MS, this.durationMs);
    const nextSpan = Math.max(
      minSpan,
      Math.min(this.durationMs, span * factor),
    );
    if (!(nextSpan > 0)) return;
    const anchor = this._timeAt(point.x);
    const ratio = (anchor - viewport.fromMs) / span;
    let from = anchor - ratio * nextSpan;
    let to = from + nextSpan;
    if (from < 0) {
      from = 0;
      to = nextSpan;
    }
    if (to > this.durationMs) {
      to = this.durationMs;
      from = to - nextSpan;
    }
    this.zoom =
      nextSpan >= this.durationMs
        ? null
        : { fromMs: Math.max(0, from), toMs: Math.min(this.durationMs, to) };
    this._notifyViewport();
    this._draw();
  }

  /** 확대 구간을 `[fromMs, toMs]`로 정한다(최소 폭·전체 보기 정규화). */
  _setViewport(fromMs, toMs) {
    const duration = Math.max(1, this.durationMs);
    let start = Math.max(0, Math.min(Number(fromMs), Number(toMs)));
    let end = Math.min(duration, Math.max(Number(fromMs), Number(toMs)));
    if (!Number.isFinite(start) || !Number.isFinite(end)) return;
    if (end - start < MIN_ZOOM_MS) {
      const center = (start + end) / 2;
      start = Math.max(0, center - MIN_ZOOM_MS / 2);
      end = Math.min(duration, start + MIN_ZOOM_MS);
      start = Math.max(0, end - MIN_ZOOM_MS);
    }
    if (start <= 0 && end >= duration) this.zoom = null;
    else this.zoom = { fromMs: start, toMs: end };
    this._notifyViewport();
    this._draw();
  }

  _notifyViewport() {
    if (typeof this.onViewportChange !== "function") return;
    const viewport = this.viewport();
    this.onViewportChange({
      fromMs: viewport.fromMs,
      toMs: viewport.toMs,
    });
  }

  /** 마커 레인에서 `x`·`y`에 가장 가까운 마커(없으면 `null`). */
  _markerAt(x, y) {
    if (y < 0 || y > MARKER_LANE_HEIGHT + 2) return null;
    const lane = y <= 12 ? "radio" : "event";
    let best = null;
    let bestDistance = MARKER_HIT_PX;
    for (const hit of this.markerHits) {
      if (hit.lane !== lane) continue;
      const distance = Math.abs(hit.x - x);
      if (distance <= bestDistance) {
        best = hit;
        bestDistance = distance;
      }
    }
    return best;
  }

  _carName(carIndex) {
    const index = Number(carIndex);
    const participant = this.participants.find(
      (item) => Number(item.car_index) === index,
    );
    if (participant && participant.name) return participant.name;
    return Number.isInteger(index) ? `차량 ${index + 1}` : "상대 차량";
  }

  /** 마커 상세 툴팁: 무전 문구 전체, 페널티·추월·플래시백 사유. */
  _markerTooltip(hit) {
    const marker = hit.marker;
    if (hit.type === "radio") {
      const lines = [`🔊 무전 · ${formatClock(marker.t_ms)}`];
      if (marker.text) lines.push(String(marker.text));
      const meta = [];
      if (Number.isInteger(marker.corner)) meta.push(`코너 ${marker.corner}`);
      if (Number.isInteger(marker.lap)) meta.push(`랩 ${marker.lap}`);
      if (Number.isFinite(marker.priority)) {
        meta.push(`우선순위 ${marker.priority}`);
      }
      if (meta.length) lines.push(meta.join(" · "));
      return lines.join("\n");
    }
    if (hit.interval) {
      const interval = hit.interval;
      const label = EVENT_LABELS[interval.kind] || interval.kind || "구간";
      const start = formatClock(interval.start_ms);
      const endRaw = Number(interval.end_ms);
      const end = Number.isFinite(endRaw) ? formatClock(endRaw) : "진행 중";
      return `${intervalIcon(interval.kind)} ${label} · ${start} ~ ${end}`;
    }
    const event = marker;
    const label = EVENT_LABELS[event.kind] || event.kind || "사건";
    const icon = EVENT_ICONS[event.kind] || "🚩";
    const lines = [`${icon} ${label} · ${formatClock(event.t_ms)}`];
    if (event.kind === "overtake" || event.kind === "overtaken") {
      const other = Number.isInteger(event.other_car_index)
        ? this._carName(event.other_car_index)
        : "상대 차량";
      lines.push(
        event.kind === "overtake"
          ? `${other} 추월`
          : `${other}에게 추월당함`,
      );
    }
    if (event.kind === "lap_complete") {
      const lapTime = Number(event.lap_time_ms);
      lines.push(
        Number.isFinite(lapTime)
          ? `랩 ${event.lap} · ${formatDuration(lapTime)}`
          : `랩 ${event.lap}`,
      );
    }
    const detail = formatEventDetail(event.detail);
    if (detail) lines.push(detail);
    return lines.join("\n");
  }

  /** 시계열 호버 툴팁: 내 차 값 · 비교 값 · 차이 Δ(비교 − 내 차). */
  _hoverTooltip(tMs) {
    const lines = [`시점 ${formatClock(tMs)}`];
    const values = this.valuesAt(tMs);
    let count = 0;
    for (const spec of this._selectedSpecs()) {
      const sample = values[spec.key];
      if (!sample) continue;
      const unit = spec.unit ? ` ${spec.unit}` : "";
      const parts = [
        Number.isFinite(sample.value)
          ? `${spec.label} ${formatMetricNumber(sample.value, spec)}${unit}`
          : `${spec.label} —`,
      ];
      if (this.compareCar !== null) {
        if (sample.restricted) parts.push("비교 비공개 텔레메트리");
        else if (Number.isFinite(sample.compare)) {
          parts.push(
            `비교 ${formatMetricNumber(sample.compare, spec)}${unit}`,
          );
        } else parts.push("비교 —");
      }
      lines.push(parts.join(" · "));
      if (Number.isFinite(sample.value) && Number.isFinite(sample.compare)) {
        lines.push(`Δ ${formatSigned(sample.compare - sample.value, spec)}${unit}`);
      }
      count += 1;
      if (count >= 6) {
        if (this.selection.length > 6) {
          lines.push(`…외 ${this.selection.length - 6}개`);
        }
        break;
      }
    }
    return lines.join("\n");
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

    const specs = this._selectedSpecs();
    const groups = this._unitGroups(specs);
    const axes = groups.slice(0, 2);
    // 상단은 마커 레인(0~24)·범례(32) 자리, 좌우는 최대 2개 Y축 눈금 자리다.
    const pad = {
      left: 56,
      right: axes.length > 1 ? 56 : 18,
      top: 46,
      bottom: 28,
    };
    const plotWidth = Math.max(1, width - pad.left - pad.right);
    const plotHeight = Math.max(1, height - pad.top - pad.bottom);
    this.layout = { width, height, pad, plotWidth, plotHeight };
    this.markerHits = [];

    if (!(this.durationMs > 0)) {
      this._drawCenteredText(
        ctx,
        "녹화를 열면 세션 시간축이 표시됩니다.",
        width,
        height,
      );
      return;
    }

    const viewport = this.viewport();
    const span = Math.max(1, viewport.toMs - viewport.fromMs);
    const xOf = (tMs) =>
      pad.left + ((tMs - viewport.fromMs) / span) * plotWidth;

    // SC·VSC·적기·피트 구간 밴드는 그래프 뒤에 반투명으로 깐다.
    if (this.showEventMarkers) {
      this._drawIntervalBands(ctx, xOf, viewport, pad, plotWidth, plotHeight);
    }
    this._drawGrid(ctx, axes, pad, plotWidth, plotHeight);
    this._drawTimeAxis(ctx, xOf, viewport, span, pad, plotWidth, height);
    this._drawLapBoundaries(ctx, xOf, viewport, pad, plotHeight);

    if (specs.length === 0) {
      this._drawCenteredText(
        ctx,
        "메트릭을 선택하면 시간 흐름이 여기에 그려집니다.",
        width,
        height,
      );
    } else if (!this._hasSeriesEntries(specs)) {
      this._drawCenteredText(
        ctx,
        "선택한 메트릭의 시계열을 불러오는 중…",
        width,
        height,
      );
    } else {
      this._drawSeries(ctx, specs, groups, pad, plotWidth, plotHeight, xOf);
      if (!this._hasVisiblePoints(specs, viewport)) {
        this._drawCenteredText(
          ctx,
          "이 구간에는 선택한 메트릭 데이터가 없습니다.",
          width,
          height,
        );
      }
    }

    this._drawLegend(ctx, specs, pad, plotWidth);
    this._drawMarkerLane(ctx, xOf, viewport);
    this._drawHover(ctx, specs, groups, pad, plotWidth, plotHeight, xOf, viewport);
    this._drawSelection(ctx, pad, plotHeight);
    this._drawCursor(ctx, xOf, viewport, pad, plotHeight);
  }

  _selectedSpecs() {
    return this.selection
      .map((key) => this.catalog.get(key))
      .filter(Boolean);
  }

  /**
   * 선택 메트릭을 `unit_group`별로 묶는다. 같은 단위(예: %)는 Y축을 공유하고,
   * 그룹마다 독립 0~100% 높이로 정규화한다(설계 5.2-2).
   */
  _unitGroups(specs) {
    const groups = new Map();
    for (const spec of specs) {
      const key = spec.unit_group || spec.unit || spec.key;
      let group = groups.get(key);
      if (!group) {
        group = {
          key,
          unit: spec.unit || "",
          color: spec.color || "#8b97a8",
          specs: [],
          min: Infinity,
          max: -Infinity,
        };
        groups.set(key, group);
      }
      group.specs.push(spec);
      if (Array.isArray(spec.value_range) && spec.value_range.length === 2) {
        const min = Number(spec.value_range[0]);
        const max = Number(spec.value_range[1]);
        if (Number.isFinite(min) && Number.isFinite(max)) {
          group.min = Math.min(group.min, min);
          group.max = Math.max(group.max, max);
        }
      }
    }
    for (const group of groups.values()) {
      if (
        Number.isFinite(group.min) &&
        Number.isFinite(group.max) &&
        group.max > group.min
      ) {
        continue;
      }
      // 고정 범위가 없는 메트릭은 현재 시리즈에서 범위를 구한다.
      const lists = [];
      for (const spec of group.specs) {
        const entry = this.series ? this.series[spec.key] : null;
        if (!entry) continue;
        lists.push(entry.series, entry.compare_series);
      }
      const bounds = seriesBounds(lists);
      group.min = bounds ? bounds[0] : 0;
      group.max = bounds ? bounds[1] : 1;
      if (group.max - group.min < 1e-9) {
        group.min -= 1;
        group.max += 1;
      }
    }
    return [...groups.values()];
  }

  /** 단위 그룹 값 → 캔버스 y 좌표(위가 최댓값). */
  _valueY(group, value, top, height) {
    const span = group.max - group.min;
    if (!(span > 0) || !Number.isFinite(value)) return top + height / 2;
    return top + height * (1 - (value - group.min) / span);
  }

  _drawCenteredText(ctx, text, width, height) {
    ctx.save();
    ctx.fillStyle = "#8b97a8";
    ctx.font = "600 13px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(text, width / 2, height / 2);
    ctx.restore();
  }

  _drawIntervalBands(ctx, xOf, viewport, pad, plotWidth, plotHeight) {
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
      ctx.save();
      ctx.globalAlpha = 0.16;
      ctx.fillStyle = intervalColor(interval.kind);
      ctx.fillRect(x1, pad.top, x2 - x1, plotHeight);
      ctx.restore();
    }
  }

  _drawGrid(ctx, axes, pad, plotWidth, plotHeight) {
    const plotLeft = pad.left;
    const plotRight = pad.left + plotWidth;
    const plotTop = pad.top;
    ctx.save();
    ctx.strokeStyle = "#232b38";
    ctx.lineWidth = 1;
    for (let step = 0; step <= 4; step += 1) {
      const y = plotTop + (plotHeight * step) / 4;
      ctx.beginPath();
      ctx.moveTo(plotLeft, y);
      ctx.lineTo(plotRight, y);
      ctx.stroke();
    }
    // 좌·우 최대 2개 대표 Y축 눈금. 축 색은 그 단위 그룹의 대표 메트릭 색이다.
    ctx.font = "600 10px system-ui, sans-serif";
    axes.forEach((group, index) => {
      const onLeft = index === 0;
      const x = onLeft ? plotLeft - 7 : plotRight + 7;
      ctx.textAlign = onLeft ? "right" : "left";
      ctx.fillStyle = group.color;
      for (let step = 0; step <= 4; step += 1) {
        const ratio = step / 4;
        const value = group.max - (group.max - group.min) * ratio;
        const y = plotTop + plotHeight * ratio;
        ctx.fillText(
          formatAxisTick(value, group.max - group.min),
          x,
          y + 3,
        );
      }
    });
    ctx.restore();
  }

  _drawTimeAxis(ctx, xOf, viewport, span, pad, plotWidth, height) {
    const step = chooseTimeStep(span, plotWidth);
    const first = Math.ceil(viewport.fromMs / step) * step;
    ctx.save();
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "center";
    for (let t = first; t <= viewport.toMs; t += step) {
      const x = xOf(t);
      if (x < pad.left - 1 || x > pad.left + plotWidth + 1) continue;
      ctx.strokeStyle = "rgba(35, 43, 56, 0.6)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, pad.top);
      ctx.lineTo(x, pad.top + this.layout.plotHeight);
      ctx.stroke();
      ctx.fillStyle = "#8b97a8";
      ctx.fillText(formatDuration(t), x, height - 9);
    }
    ctx.restore();
  }

  /** 랩 시작 수직선과 `L1`·`L2`… 라벨(설계 5.2 "랩 경계 수직선"). */
  _drawLapBoundaries(ctx, xOf, viewport, pad, plotHeight) {
    let lastLabelX = -Infinity;
    ctx.save();
    ctx.font = "600 9px system-ui, sans-serif";
    ctx.textAlign = "center";
    for (const lap of this.markers.laps) {
      const tMs = Number(lap.start_ms);
      if (!Number.isFinite(tMs)) continue;
      if (tMs < viewport.fromMs || tMs > viewport.toMs) continue;
      const x = xOf(tMs);
      ctx.strokeStyle = "rgba(139, 151, 168, 0.5)";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 4]);
      ctx.beginPath();
      ctx.moveTo(x, pad.top);
      ctx.lineTo(x, pad.top + plotHeight);
      ctx.stroke();
      ctx.setLineDash([]);
      if (x - lastLabelX >= 24) {
        ctx.fillStyle = "#aab6c8";
        ctx.fillText(`L${lap.lap}`, x, pad.top - 3);
        lastLabelX = x;
      }
    }
    ctx.restore();
  }

  _drawSeries(ctx, specs, groups, pad, plotWidth, plotHeight, xOf) {
    const groupByKey = new Map(groups.map((group) => [group.key, group]));
    ctx.save();
    ctx.beginPath();
    ctx.rect(pad.left, pad.top, plotWidth, plotHeight);
    ctx.clip();
    for (const spec of specs) {
      const entry = this.series ? this.series[spec.key] : null;
      if (!entry) continue;
      const group = groupByKey.get(spec.unit_group || spec.unit || spec.key);
      if (!group) continue;
      const color = spec.color || group.color;
      const yOf = (value) =>
        this._valueY(group, value, pad.top, plotHeight);
      // 내 차는 실선.
      this._strokePoints(ctx, entry.series, spec, yOf, xOf, {
        color,
        dashed: false,
      });
      // 비교 차량은 같은 색 점선(`setLineDash([5, 4])`).
      if (
        this.compareCar !== null &&
        Array.isArray(entry.compare_series) &&
        entry.compare_series.length > 0
      ) {
        this._strokePoints(ctx, entry.compare_series, spec, yOf, xOf, {
          color,
          dashed: true,
        });
      }
    }
    ctx.restore();
  }

  /**
   * 점 배열을 선으로 잇는다. `step` 메트릭은 계단형, `dashed`는 비교 차량
   * 점선이다. 데이터가 끊긴 구간(NaN 제거로 생긴 시간 간격)은 선을 끊는다.
   */
  _strokePoints(ctx, points, spec, yOf, xOf, { color, dashed }) {
    if (!Array.isArray(points) || points.length === 0) return;
    const firstTime = Number(points[0][0]);
    const lastTime = Number(points[points.length - 1][0]);
    const dataSpan = Math.max(1, lastTime - firstTime);
    // 다운샘플 버킷 간격에서 크게 벗어난 간격만 실제 데이터 공백으로 본다.
    const typical = dataSpan / Math.max(1, points.length - 1);
    const gapLimit = Math.max(typical * 4, 120);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = dashed ? 1.2 : 1.6;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.setLineDash(dashed ? [5, 4] : []);
    ctx.beginPath();
    let previous = null;
    for (const point of points) {
      const tMs = Number(point && point[0]);
      const value = Number(point && point[1]);
      if (!Number.isFinite(tMs) || !Number.isFinite(value)) {
        previous = null;
        continue;
      }
      const x = xOf(tMs);
      const y = yOf(value);
      if (previous === null || tMs - previous.tMs > gapLimit) {
        if (previous !== null) ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(x, y);
      } else if (spec.step) {
        ctx.lineTo(x, previous.y);
        ctx.lineTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
      previous = { tMs, x, y };
    }
    ctx.stroke();
    ctx.restore();
  }

  /** 선택 메트릭 중 하나라도 시리즈 응답을 받았는지(로딩 안내 구분). */
  _hasSeriesEntries(specs) {
    if (!this.series) return false;
    return specs.some((spec) => Boolean(this.series[spec.key]));
  }

  _hasVisiblePoints(specs, viewport) {
    for (const spec of specs) {
      const entry = this.series ? this.series[spec.key] : null;
      if (!entry) continue;
      for (const points of [entry.series, entry.compare_series]) {
        if (!Array.isArray(points)) continue;
        for (const point of points) {
          const tMs = Number(point && point[0]);
          if (Number.isFinite(tMs) && tMs >= viewport.fromMs && tMs <= viewport.toMs) {
            return true;
          }
        }
      }
    }
    return false;
  }

  /** 범례: 실선(내 차)·점선(비교 차량) 색과 비공개 텔레메트리 안내. */
  _drawLegend(ctx, specs, pad, plotWidth) {
    if (specs.length === 0) return;
    const y = 32;
    const maxX = pad.left + plotWidth;
    let x = pad.left;
    let drawn = 0;
    ctx.save();
    ctx.font = "600 10.5px system-ui, sans-serif";
    ctx.textAlign = "start";
    for (const spec of specs) {
      const entry = this.series ? this.series[spec.key] : null;
      const restricted = Boolean(
        entry && entry.compare_restricted && this.compareCar !== null,
      );
      const suffix = restricted ? " · 비공개 텔레메트리" : "";
      const text = `${spec.label}${spec.unit ? ` (${spec.unit})` : ""}${suffix}`;
      const sampleWidth = this.compareCar !== null ? 32 : 16;
      const width = sampleWidth + ctx.measureText(text).width + 12;
      if (x + width > maxX && drawn > 0) {
        ctx.fillStyle = "#8b97a8";
        ctx.fillText(`+${specs.length - drawn}`, x, y);
        break;
      }
      ctx.strokeStyle = spec.color || "#8b97a8";
      ctx.lineWidth = 2;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(x, y - 3);
      ctx.lineTo(x + 12, y - 3);
      ctx.stroke();
      if (this.compareCar !== null) {
        ctx.setLineDash([5, 4]);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(x + 15, y - 3);
        ctx.lineTo(x + 27, y - 3);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.fillStyle = restricted ? "#ffd166" : "#e8edf5";
      ctx.fillText(text, x + sampleWidth, y);
      x += width;
      drawn += 1;
    }
    ctx.restore();
  }

  /** 상단 마커 레인: 무전(🔊) 윗줄, 사건(🚩/↺/⚠️/🏁)·구간 아이콘 아랫줄. */
  _drawMarkerLane(ctx, xOf, viewport) {
    ctx.save();
    ctx.strokeStyle = "#1c2430";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, MARKER_LANE_HEIGHT + 0.5);
    ctx.lineTo(this.layout.width, MARKER_LANE_HEIGHT + 0.5);
    ctx.stroke();

    const hits = [];
    ctx.font =
      "9px 'Segoe UI Emoji', 'Apple Color Emoji', system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    if (this.showRadioMarkers) {
      for (const marker of this.markers.radio) {
        const tMs = Number(marker.t_ms);
        if (!Number.isFinite(tMs)) continue;
        if (tMs < viewport.fromMs || tMs > viewport.toMs) continue;
        const x = xOf(tMs);
        this._drawMarkerIcon(ctx, "🔊", x, 8);
        hits.push({ lane: "radio", x, type: "radio", marker });
      }
    }
    if (this.showEventMarkers) {
      for (const marker of this.markers.events) {
        const tMs = Number(marker.t_ms);
        if (!Number.isFinite(tMs)) continue;
        if (tMs < viewport.fromMs || tMs > viewport.toMs) continue;
        const x = xOf(tMs);
        this._drawMarkerIcon(ctx, EVENT_ICONS[marker.kind] || "🚩", x, 19);
        hits.push({ lane: "event", x, type: "event", marker });
      }
      for (const interval of this.markers.intervals) {
        const startMs = Number(interval.start_ms);
        if (!Number.isFinite(startMs)) continue;
        if (startMs < viewport.fromMs || startMs > viewport.toMs) continue;
        const x = xOf(startMs);
        this._drawMarkerIcon(ctx, intervalIcon(interval.kind), x, 19);
        hits.push({
          lane: "event",
          x,
          type: "event",
          marker: interval,
          interval,
        });
      }
    }
    if (this.markerHover) {
      ctx.fillStyle = "#e8edf5";
      ctx.fillRect(
        this.markerHover.x - 6,
        this.markerHover.lane === "radio" ? 14 : 23,
        12,
        2,
      );
    }
    this.markerHits = hits;
    ctx.restore();
  }

  _drawMarkerIcon(ctx, icon, x, y) {
    ctx.globalAlpha = 0.95;
    ctx.fillText(icon, x, y);
    ctx.globalAlpha = 1;
  }

  _drawHover(ctx, specs, groups, pad, plotWidth, plotHeight, xOf, viewport) {
    if (!Number.isFinite(this.hoverMs)) return;
    if (this.hoverMs < viewport.fromMs || this.hoverMs > viewport.toMs) return;
    const x = xOf(this.hoverMs);
    ctx.save();
    ctx.strokeStyle = "#e8edf5";
    ctx.globalAlpha = 0.55;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    ctx.moveTo(x, pad.top);
    ctx.lineTo(x, pad.top + plotHeight);
    ctx.stroke();
    ctx.restore();
    if (!this.series) return;

    const groupByKey = new Map(groups.map((group) => [group.key, group]));
    const values = this.valuesAt(this.hoverMs);
    ctx.save();
    for (const spec of specs) {
      const sample = values[spec.key];
      if (!sample || !Number.isFinite(sample.value)) continue;
      const group = groupByKey.get(spec.unit_group || spec.unit || spec.key);
      if (!group) continue;
      const y = this._valueY(group, sample.value, pad.top, plotHeight);
      ctx.beginPath();
      ctx.arc(x, y, 2.8, 0, Math.PI * 2);
      ctx.fillStyle = spec.color || "#8b97a8";
      ctx.fill();
      if (this.compareCar !== null && Number.isFinite(sample.compare)) {
        const compareY = this._valueY(
          group,
          sample.compare,
          pad.top,
          plotHeight,
        );
        ctx.beginPath();
        ctx.arc(x, compareY, 2.6, 0, Math.PI * 2);
        ctx.strokeStyle = spec.color || "#8b97a8";
        ctx.lineWidth = 1.2;
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  _drawSelection(ctx, pad, plotHeight) {
    const drag = this.drag;
    if (!drag || drag.mode !== "select" || !drag.moved) return;
    const x1 = Math.min(drag.startX, drag.currentX);
    const x2 = Math.max(drag.startX, drag.currentX);
    if (x2 - x1 < 1) return;
    ctx.save();
    ctx.fillStyle = "rgba(34, 211, 238, 0.16)";
    ctx.fillRect(x1, pad.top, x2 - x1, plotHeight);
    ctx.strokeStyle = "#22d3ee";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.strokeRect(x1 + 0.5, pad.top + 0.5, x2 - x1 - 1, plotHeight - 1);
    ctx.setLineDash([]);
    ctx.fillStyle = "#22d3ee";
    ctx.font = "600 10px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(
      formatDuration(this._timeAt(x2) - this._timeAt(x1)),
      (x1 + x2) / 2,
      pad.top + 14,
    );
    ctx.restore();
  }

  _drawCursor(ctx, xOf, viewport, pad, plotHeight) {
    if (this.cursorMs < viewport.fromMs || this.cursorMs > viewport.toMs) return;
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

// 차트 → 화면 연결(SCRUM-137). 클릭 시크·호버 미리보기·뷰포트 재조회.
chart.onSeek = (positionMs) => {
  // 클릭 시크는 미리보기보다 우선한다. 호버 상태를 풀고 커서·지도를 함께 옮긴다.
  if (appState.hoverMs !== null) restoreAfterHover();
  appState.lastValuePosition = -Infinity;
  clock.seek(positionMs);
};
chart.onHover = (positionMs) => {
  if (positionMs === null) restoreAfterHover();
  else handleChartHover(positionMs);
};
chart.onViewportChange = () => {
  scheduleSeriesFetch();
};

function onClockChange(currentClock) {
  updatePlaybackUI(currentClock);
  chart.setCursor(currentClock.positionMs);
  if (
    appState.hoverMs === null &&
    autoFollowButton.getAttribute("aria-pressed") === "true"
  ) {
    chart.followCursor(currentClock.positionMs);
  }
  // 하단 체크박스 옆 현재 값은 재생 중 100ms 간격, 일시정지·시크는 즉시 갱신.
  if (appState.hoverMs === null) {
    const valueMoved = Math.abs(
      currentClock.positionMs - appState.lastValuePosition,
    );
    if (!currentClock.playing || valueMoved >= 100) {
      appState.lastValuePosition = currentClock.positionMs;
      updateMetricValues(valuesAt(currentClock.positionMs));
    }
  }
  const moved = Math.abs(
    currentClock.positionMs - appState.lastMapPosition,
  );
  // 재생 중에는 10Hz 지도 프레임보다 자주 요청하지 않고, 일시정지·시크는
  // 사용자가 움직인 그 위치를 바로 반영한다. 그래프 호버 중에는 호버
  // 미리보기가 지도를 소유하므로 재생 커서가 덮어쓰지 않는다.
  if (
    appState.hoverMs === null &&
    (!currentClock.playing || moved >= MAP_UPDATE_MIN_MS)
  ) {
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
  mapView.setPreviewLabel(null);
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
// 시계열 조회·호버 연동(설계 5.2-2, SCRUM-137)

/**
 * 현재 선택·비교 차량·뷰포트로 `/series`를 다시 부른다. 뷰포트 변경(줌·팬·
 * 자동 따라가기)은 120ms 디바운스로 합치고, 선택·비교 변경은 즉시 부른다.
 */
function scheduleSeriesFetch({ immediate = false } = {}) {
  cancelSeriesFetch();
  if (!appState.ready || appState.recordingId === null) return;
  if (selection.size === 0) {
    appState.seriesToken += 1;
    chart.setSeries(null);
    updateMetricValues(null);
    return;
  }
  if (immediate) {
    void fetchSeries();
    return;
  }
  appState.seriesTimer = window.setTimeout(() => {
    appState.seriesTimer = null;
    void fetchSeries();
  }, SERIES_DEBOUNCE_MS);
}

function cancelSeriesFetch() {
  if (appState.seriesTimer !== null) {
    window.clearTimeout(appState.seriesTimer);
    appState.seriesTimer = null;
  }
}

async function fetchSeries() {
  if (!appState.ready || appState.recordingId === null || selection.size === 0) {
    return;
  }
  const viewport = chart.viewport();
  const params = new URLSearchParams({
    recording_id: String(appState.recordingId),
    metrics: [...selection].join(","),
    from_ms: String(Math.round(viewport.fromMs)),
    to_ms: String(Math.round(viewport.toMs)),
    max_points: String(chart.pointBudget()),
  });
  if (appState.compareCar !== null) {
    params.set("compare_car", String(appState.compareCar));
  }
  const token = ++appState.seriesToken;
  try {
    const response = await fetch(
      `/api/replay/telemetry/series?${params.toString()}`,
      { cache: "no-store" },
    );
    if (!response.ok) return;
    const data = await response.json();
    if (token !== appState.seriesToken) return;
    chart.setSeries(data);
    const at = appState.hoverMs !== null ? appState.hoverMs : clock.positionMs;
    updateMetricValues(valuesAt(at));
  } catch (error) {
    // 네트워크가 잠깐 끊기면 다음 뷰포트·선택 변경이 다시 요청한다.
  }
}

/** 차트 시리즈에서 `positionMs`의 메트릭별 내 차 값을 뽑는다. */
function valuesAt(positionMs) {
  const samples = chart.valuesAt(positionMs);
  const values = {};
  for (const [key, sample] of Object.entries(samples)) {
    if (sample && Number.isFinite(sample.value)) values[key] = sample.value;
  }
  return values;
}

function handleChartHover(positionMs) {
  appState.hoverMs = positionMs;
  updateMetricValues(valuesAt(positionMs));
  schedulePreview(positionMs);
}

/** 그래프에서 마우스가 벗어나면 재생 커서 위치·지도·값 표시를 되돌린다. */
function restoreAfterHover() {
  if (appState.hoverMs === null) return;
  appState.hoverMs = null;
  if (appState.previewTimer !== null) {
    window.clearTimeout(appState.previewTimer);
    appState.previewTimer = null;
  }
  mapView.setPreviewLabel(null);
  updateMetricValues(valuesAt(clock.positionMs));
  void applyMapAt(clock.positionMs);
}

function schedulePreview(positionMs) {
  if (appState.previewTimer !== null) {
    window.clearTimeout(appState.previewTimer);
  }
  appState.previewTimer = window.setTimeout(() => {
    appState.previewTimer = null;
    void previewMapAt(positionMs);
  }, PREVIEW_DEBOUNCE_MS);
}

/**
 * 호버 시각의 지도 프레임을 우측 지도에 미리보기로 그린다. 이미 받아 둔
 * 청크에 있으면 즉시 쓰고, 아니면 `/map_frames?t_ms=` 단건만 받아 캐시에
 * 합친다(재생 루프·전역 `ReplayManager`는 건드리지 않는다).
 */
async function previewMapAt(positionMs) {
  const buffer = appState.frameBuffer;
  if (!buffer) return;
  const token = ++appState.frameToken;
  if (buffer.hasFrameAt(positionMs)) {
    const frame = buffer.frameAt(positionMs);
    if (frame && token === appState.frameToken && appState.hoverMs !== null) {
      applyPreviewFrame(frame, positionMs);
    }
    return;
  }
  const params = new URLSearchParams({
    recording_id: String(appState.recordingId),
    t_ms: String(Math.round(positionMs)),
  });
  try {
    const response = await fetch(
      `/api/replay/telemetry/map_frames?${params.toString()}`,
      { cache: "no-store" },
    );
    if (!response.ok) return;
    const data = await response.json();
    const frame =
      data.frame || (Array.isArray(data.frames) ? data.frames[0] : null);
    if (!frame) return;
    buffer.put(frame);
    if (token !== appState.frameToken || appState.hoverMs === null) return;
    applyPreviewFrame(frame, positionMs);
  } catch (error) {
    // 미리보기 실패는 재생·분석에 영향을 주지 않는다.
  }
}

function applyPreviewFrame(frame, positionMs) {
  mapView.setPreviewLabel(`미리보기 ${formatClock(positionMs)}`);
  applyMapFrame(frame, positionMs);
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
  appState.lastValuePosition = -Infinity;
  appState.compareCar = null;
  // 이전 녹화의 시계열·호버 미리보기 요청을 취소하고 늦은 응답을 버린다.
  cancelSeriesFetch();
  appState.seriesToken += 1;
  appState.hoverMs = null;
  if (appState.previewTimer !== null) {
    window.clearTimeout(appState.previewTimer);
    appState.previewTimer = null;
  }

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
  // 사건 툴팁이 `car_index`를 드라이버 이름으로 바꿀 수 있게 넘긴다.
  chart.setParticipants(appState.participants);

  const options = loadOptions();
  applyOptions(options);
  restoreSelection();

  clock.setDuration(durationMs);
  appState.lastValuePosition = -Infinity;
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
  // 체크박스 변경은 즉시 그래프에 반영한다(뷰포트 변경만 120ms 디바운스).
  scheduleSeriesFetch({ immediate: true });
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
  const privateDriver = Boolean(participant && participant.your_telemetry === 0);
  privateBadge.hidden = !privateDriver;
  if (privateDriver) {
    privateBadge.textContent =
      "비교 차량의 비공개 텔레메트리(CarTelemetry·CarStatus·CarDamage)는 " +
      "비교가 제한됩니다.";
  }
  saveOption("compareCar", index);
  scheduleSeriesFetch({ immediate: true });
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

/** 구간 종류별 마커 아이콘. 피트만 공구, 나머지 깃발류는 🚩다. */
function intervalIcon(kind) {
  return kind === "pit" ? "🔧" : "🚩";
}

/**
 * 시리즈 점 배열에서 `tMs`의 값. `step`이면 그 시점 이전의 마지막 값(계단
 * 상태), 아니면 시간상 가장 가까운 점을 쓴다. 점은 `[[t_ms, value], ...]`.
 */
function sampleSeries(points, tMs, step) {
  if (!Array.isArray(points) || points.length === 0) return null;
  const target = Number(tMs);
  if (!Number.isFinite(target)) return null;

  let low = 0;
  let high = points.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    const middleTime = Number(points[middle] && points[middle][0]);
    if (Number.isFinite(middleTime) && middleTime <= target) low = middle + 1;
    else high = middle;
  }
  const beforeIndex = low - 1;

  if (step) {
    if (beforeIndex < 0) return null;
    const value = Number(points[beforeIndex][1]);
    return Number.isFinite(value) ? value : null;
  }
  if (beforeIndex < 0) {
    const value = Number(points[0][1]);
    return Number.isFinite(value) ? value : null;
  }
  const afterIndex = Math.min(beforeIndex + 1, points.length - 1);
  const before = points[beforeIndex];
  const after = points[afterIndex];
  const beforeTime = Number(before[0]);
  const afterTime = Number(after[0]);
  const beforeValue = Number(before[1]);
  if (!Number.isFinite(beforeValue)) return null;
  if (!Number.isFinite(afterTime) || afterTime === beforeTime) {
    return beforeValue;
  }
  const ratio = Math.max(
    0,
    Math.min(1, (target - beforeTime) / (afterTime - beforeTime)),
  );
  const afterValue = Number(after[1]);
  if (!Number.isFinite(afterValue)) return beforeValue;
  return beforeValue + (afterValue - beforeValue) * ratio;
}

/** 시리즈 점 목록들에서 유한한 값의 [최솟값, 최댓값]. 점이 없으면 `null`. */
function seriesBounds(lists) {
  let min = Infinity;
  let max = -Infinity;
  for (const points of lists) {
    if (!Array.isArray(points)) continue;
    for (const point of points) {
      const value = Number(point && point[1]);
      if (!Number.isFinite(value)) continue;
      if (value < min) min = value;
      if (value > max) max = value;
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
  return [min, max];
}

/** Y축 눈금 문자열. 단위 폭에 따라 소수 자리를 줄인다. */
function formatAxisTick(value, span) {
  if (!Number.isFinite(value)) return "";
  const absSpan = Math.abs(Number(span));
  if (absSpan >= 200) return String(Math.round(value));
  if (absSpan >= 20) return value.toFixed(0);
  if (absSpan >= 2) return value.toFixed(1);
  return value.toFixed(2);
}

/** 보이는 시간 폭에 맞는 시간축 눈금 간격(ms). */
function chooseTimeStep(spanMs, plotWidth) {
  const target = Math.max(2, Math.floor(plotWidth / 90));
  for (const step of TIME_TICK_STEPS_MS) {
    if (spanMs / step <= target) return step;
  }
  return TIME_TICK_STEPS_MS[TIME_TICK_STEPS_MS.length - 1];
}

/** 메트릭 값 표기. `step`·큰 값은 정수, 작은 값은 소수 1~2자리. */
function formatMetricNumber(value, spec) {
  if (!Number.isFinite(value)) return "—";
  if ((spec && spec.step) || Number.isInteger(value)) {
    return String(Math.round(value));
  }
  const abs = Math.abs(value);
  if (abs >= 1000) return String(Math.round(value));
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

/** 부호를 항상 붙인 메트릭 값(호버 툴팁의 Δ). */
function formatSigned(value, spec) {
  const sign = value >= 0 ? "+" : "-";
  return `${sign}${formatMetricNumber(Math.abs(value), spec)}`;
}

/** 사건 `detail`(원본 이벤트 데이터)을 `라벨: 값` 줄로 만든다. */
function formatEventDetail(detail) {
  if (!detail || typeof detail !== "object") return "";
  const parts = [];
  for (const [key, value] of Object.entries(detail)) {
    if (value === null || value === undefined || value === "") continue;
    if (typeof value === "object") continue;
    const label = EVENT_DETAIL_LABELS[key] || key;
    parts.push(`${label}: ${String(value)}`);
    if (parts.length >= 4) break;
  }
  return parts.join(" · ");
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
