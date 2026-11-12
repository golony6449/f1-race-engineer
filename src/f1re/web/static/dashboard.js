/**
 * 대시보드 연결. 화면은 서버 API를 읽기만 한다(설계 6.9).
 *
 * - `/api/thresholds`: 색 표. 문턱값은 화면에서 계산하지 않고 이 표만 쓴다.
 * - `/api/state`: 첫 화면. WebSocket이 붙기 전에도 바로 값을 보여 준다.
 * - `/ws/state`: 서버가 10Hz로 미는 상태. 끊기면 1초 뒤 다시 연결한다.
 */

import { CarStateView, renderStatusBar, setStale } from "./carstate.js";
import { TrackMapView } from "./map.js";

const dashboard = document.getElementById("dashboard");
const view = new CarStateView(dashboard);
const mapView = new TrackMapView(document.getElementById("track-map"));

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
  } catch (error) {
    // 서버가 아직 안 떴거나 잠깐 실패했다. 색 없이 그리다가 다시 시도한다.
    window.setTimeout(loadThresholds, 3000);
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

function connect() {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${window.location.host}/ws/state`);

  socket.addEventListener("message", (event) => {
    try {
      applyPayload(JSON.parse(event.data));
    } catch (error) {
      // 잘못된 메시지는 버리고 다음 메시지를 기다린다.
    }
  });

  socket.addEventListener("close", () => {
    window.setTimeout(connect, 1000);
  });

  socket.addEventListener("error", () => {
    socket.close();
  });
}

loadThresholds();
loadInitialState();
connect();
