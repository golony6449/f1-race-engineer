/**
 * 대시보드 연결. 화면은 서버 API를 읽기만 한다(설계 6.9).
 *
 * - `/api/engine`: 레이스 엔지니어 ON/OFF 상태. 꺼져 있으면 대시보드 본문 대신
 *   안내만 보여 주고 WebSocket에 연결하지 않는다(SCRUM-82).
 * - `/api/thresholds`: 색 표. 문턱값은 화면에서 계산하지 않고 이 표만 쓴다.
 * - `/api/state`: 첫 화면. WebSocket이 붙기 전에도 바로 값을 보여 준다.
 * - `/ws/state`: 서버가 10Hz로 미는 상태. 끊기면 1초 뒤 다시 연결한다.
 *   서버가 엔진을 끄면 `{"type": "engine", "state": "off"}`를 보내고 닫는다.
 */

import { CarStateView, renderStatusBar, setStale } from "./carstate.js";
import { TrackMapView } from "./map.js";

const dashboard = document.getElementById("dashboard");
const statusbar = document.getElementById("statusbar");
const engineOffNotice = document.getElementById("engine-off");
const settingsButton = document.getElementById("engine-settings");
const engineModal = document.getElementById("engine-modal");
const engineToggle = document.getElementById("engine-toggle");
const engineStateLabel = document.getElementById("engine-state-label");
const engineDescription = document.getElementById("engine-description");
const engineCloseButton = document.getElementById("engine-close");

const view = new CarStateView(dashboard);
const mapView = new TrackMapView(document.getElementById("track-map"));

const ENGINE_TEXT = {
  on: {
    label: "켜짐",
    description: "켜짐: UDP 수신·무전·분석이 동작합니다.",
  },
  off: {
    label: "꺼짐",
    description: "꺼짐: UDP 수신·무전·분석이 모두 멈춥니다.",
  },
};

let engineState = "off";
let socket = null;
let reconnectTimer = null;
let thresholdsLoaded = false;

// `/api/track`은 세션의 트랙 ID가 처음 알려질 때 한 번 받는다. 윤곽이 아직
// 없으면(처음 오는 서킷) 서버가 첫 유효 랩에서 배우므로 주기적으로 다시 묻는다.
const TRACK_RETRY_MS = 5000;
let loadedTrackId = null;
let trackRetryTimer = null;

async function loadTrack(trackId) {
  try {
    const response = await fetch("/api/track", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const track = await response.json();
    mapView.setTrack(track);
    if (track.outline_source === "none") scheduleTrackRetry(trackId);
  } catch (error) {
    scheduleTrackRetry(trackId);
  }
}

function scheduleTrackRetry(trackId) {
  if (trackRetryTimer !== null) return;
  trackRetryTimer = window.setTimeout(() => {
    trackRetryTimer = null;
    if (loadedTrackId === trackId) loadTrack(trackId);
  }, TRACK_RETRY_MS);
}

function maybeLoadTrack(payload) {
  const session =
    (payload.snapshot && payload.snapshot.packets
      && payload.snapshot.packets.session) || {};
  const trackId = session.track_id ?? null;
  if (trackId === loadedTrackId) return;
  loadedTrackId = trackId;
  if (trackId !== null) loadTrack(trackId);
}

async function loadThresholds() {
  try {
    const response = await fetch("/api/thresholds", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const table = await response.json();
    view.setColors(table.colors || {});
    thresholdsLoaded = true;
  } catch (error) {
    // 서버가 아직 안 떴거나 잠깐 실패했다. 엔진이 켜져 있을 때만 다시 시도한다.
    window.setTimeout(() => {
      if (engineState === "on") loadThresholds();
    }, 3000);
  }
}

async function loadInitialState() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (response.ok) applyPayload(await response.json());
  } catch (error) {
    // WebSocket 연결이 이어받는다.
  }
}

function applyPayload(payload) {
  if (!payload) return;
  setStale(dashboard, payload.connection && payload.connection.stale);
  view.render(payload.car);
  renderStatusBar(payload);
  mapView.render(payload);
  maybeLoadTrack(payload);
}

/** 엔진 상태에 맞춰 화면·연결을 바꾼다. */
function setEngineUI(state) {
  const next = state === "on" ? "on" : "off";
  engineState = next;
  const on = next === "on";
  engineOffNotice.hidden = on;
  dashboard.hidden = !on;
  statusbar.hidden = !on;
  settingsButton.dataset.engine = next;
  engineToggle.checked = on;
  engineStateLabel.textContent = ENGINE_TEXT[next].label;
  engineDescription.textContent = ENGINE_TEXT[next].description;

  if (!on) {
    // OFF면 화면 갱신 루프를 멈춘다. 서버도 UDP·분석·무전을 모두 멈춘 상태다.
    stopStreaming();
  }
}

function stopStreaming() {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (trackRetryTimer !== null) {
    window.clearTimeout(trackRetryTimer);
    trackRetryTimer = null;
  }
  if (socket) {
    const closing = socket;
    socket = null;
    try {
      closing.close();
    } catch (error) {
      // 이미 닫힌 소켓은 무시한다.
    }
  }
}

/** 엔진이 켜졌을 때 화면 데이터와 실시간 연결을 시작한다. */
function startStreaming() {
  if (!thresholdsLoaded) loadThresholds();
  loadInitialState();
  connect();
}

function connect() {
  if (engineState !== "on") return;
  if (
    socket &&
    (socket.readyState === WebSocket.OPEN ||
      socket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${window.location.host}/ws/state`);

  socket.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.type === "engine" && payload.state === "off") {
        // 서버가 엔진을 껐다. 다시 연결하지 않고 안내만 보여 준다.
        setEngineUI("off");
        return;
      }
      applyPayload(payload);
    } catch (error) {
      // 잘못된 메시지는 버리고 다음 메시지를 기다린다.
    }
  });

  socket.addEventListener("close", () => {
    socket = null;
    if (engineState !== "on") return;
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, 1000);
  });

  socket.addEventListener("error", () => {
    if (socket) socket.close();
  });
}

function openSettings() {
  engineModal.hidden = false;
  settingsButton.setAttribute("aria-expanded", "true");
  engineToggle.focus();
}

function closeSettings() {
  engineModal.hidden = true;
  settingsButton.setAttribute("aria-expanded", "false");
  settingsButton.focus();
}

settingsButton.addEventListener("click", openSettings);
engineCloseButton.addEventListener("click", closeSettings);
for (const closer of document.querySelectorAll("[data-engine-close]")) {
  closer.addEventListener("click", closeSettings);
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !engineModal.hidden) closeSettings();
});

engineToggle.addEventListener("change", async () => {
  const target = engineToggle.checked ? "on" : "off";
  engineToggle.disabled = true;
  try {
    const response = await fetch("/api/engine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: target }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    setEngineUI(data.state);
    if (data.state === "on") startStreaming();
  } catch (error) {
    // 전환에 실패했다. 토글을 원래 상태로 되돌린다.
    engineToggle.checked = engineState === "on";
  } finally {
    engineToggle.disabled = false;
  }
});

async function initEngine() {
  try {
    const response = await fetch("/api/engine", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    setEngineUI(data.state);
    if (data.state === "on") startStreaming();
  } catch (error) {
    // 서버가 아직 안 떴다. 잠시 뒤 다시 확인한다.
    setEngineUI("off");
    window.setTimeout(initEngine, 3000);
  }
}

initEngine();
