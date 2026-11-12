/**
 * 대시보드 연결. 화면은 서버 API를 읽기만 한다(설계 6.9).
 *
 * - `/api/thresholds`: 색 표. 문턱값은 화면에서 계산하지 않고 이 표만 쓴다.
 * - `/api/state`: 첫 화면. WebSocket이 붙기 전에도 바로 값을 보여 준다.
 * - `/ws/state`: 서버가 10Hz로 미는 상태. 끊기면 1초 뒤 다시 연결한다.
 */

import { CarStateView, renderStatusBar, setStale } from "./carstate.js";

const dashboard = document.getElementById("dashboard");
const view = new CarStateView(dashboard);

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
