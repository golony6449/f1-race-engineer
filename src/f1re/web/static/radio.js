/**
 * 무전 페이지. 서버가 보내는 음성 조각을 이어 붙여 재생하고, 연결 상태와
 * 마지막 무전 자막을 크게 보여 준다(설계 6.10).
 *
 * - 브라우저 자동 재생 정책 때문에 "무전 연결" 버튼을 한 번 눌러야 한다.
 *   버튼을 누르면 AudioContext를 만들고 WebSocket을 연다.
 * - 서버 → 브라우저 메시지(JSON):
 *   status / caption / audio(base64 PCM) / interrupt / done
 * - 서버가 재생 길이에 맞춰 조각을 보내므로 도착한 조각은 앞 조각 뒤에 그대로
 *   이어 붙인다. 더 높은 우선순위 무전이 와서 서버가 끊으면 `interrupt`를 받아
 *   재생 중인 조각을 모두 멈춘다.
 * - 서버와의 연결이 끊기면 1초마다 자동으로 다시 연결한다.
 * - 오른쪽 데이터 수신 칸은 대시보드와 같은 `/api/state`를 1초마다 읽어
 *   `carstate.js`의 문구 함수로 그린다(SCRUM-111).
 */

import { connectionText, diagnosticsText } from "./carstate.js";

const KIND_LABELS = {
  safety: "안전",
  box: "박스",
  coaching: "코칭",
  overtake: "추월",
  improvement: "개선",
};

const connectionNode = document.getElementById("connection");
const connectionTextNode = document.getElementById("connection-text");
const dataStatusNode = document.getElementById("data-status");
const dataStatusValue = document.getElementById("data-status-value");
const dataStatusDetail = document.getElementById("data-status-detail");
const dataStatusAge = document.getElementById("data-status-age");
const sessionValue = document.getElementById("session-value");
const currentCornerValue = document.getElementById("current-corner-value");
const nextCornerValue = document.getElementById("next-corner-value");
const nextCornerDistance = document.getElementById("next-corner-distance");
const connectButton = document.getElementById("connect");
const voiceCheckButton = document.getElementById("voice-check");
const voiceNode = document.getElementById("voice");
const captionText = document.getElementById("caption-text");
const captionMeta = document.getElementById("caption-meta");
const engineOffNode = document.getElementById("engine-off");

let socket = null;
let audioContext = null;
let nextStartTime = 0;
let reconnectTimer = null;
let enginePollTimer = null;
let started = false;
// 레이스 엔지니어 ON/OFF(SCRUM-82). OFF면 무전 서버가 없으므로 연결하지 않는다.
let engineState = "off";
// 지금 재생 중인(또는 마지막으로 시작한) 무전의 우선순위. 숫자가 작을수록 높다.
// 서버는 재생 길이에 맞춰 음성을 보내고, 더 높은 우선순위가 오면 `interrupt`를
// 보낸 뒤 새 무전을 보낸다(설계 6.7). 이 값은 그 전에 도착한 caption을 받아
// 두는 보조 수단으로 남긴다.
let currentPriority = null;
const activeSources = new Set();

// 데이터 수신 상태(SCRUM-111 · SCRUM-126). 대시보드가 쓰는 `/api/state`를 약
// 1초마다 읽어 엔진이 켜져 있는 동안만 갱신한다. 마지막 정상 응답 시각과
// 서버가 알려 준 패킷 나이로 "마지막 수신" 시점을 추정해 0.25초마다 초를
// 다시 세고, 3초 넘게 응답이 없으면 빨간 "서버 응답 없음"으로 바꾼다.
const DATA_POLL_MS = 1000;
const DATA_TICK_MS = 250;
const DATA_TIMEOUT_MS = 3000;
let dataPollTimer = null;
let dataTickTimer = null;
let dataRefreshing = false;
// 데이터 폴링을 시작한 시각. 아직 정상 응답이 한 번도 없을 때 무응답 판정의
// 기준으로 쓴다.
let pollStartedMs = null;
let lastSuccessMs = null;
// 클라이언트 시계 기준으로 추정한 마지막 UDP 패킷 수신 시각. 서버 응답이
// 끊겨도 이 값에서 계속 초를 세어 "마지막 수신 N.N초 전"을 보여 준다.
let lastPacketMs = null;

/** 연결 상태 배너의 왼쪽(무전 연결) 칸. state: connected | connecting | disconnected */
function setConnection(state, text) {
  connectionNode.dataset.state = state;
  connectionTextNode.textContent = text;
}

/** 데이터 수신 칸(오른쪽 반). `/api/state` 응답을 대시보드와 같은 문구로 그린다. */
function setDataStatus(payload) {
  const diagnostics = (payload.snapshot && payload.snapshot.diagnostics) || {};
  const connection = payload.connection || {};
  dataStatusValue.textContent = connectionText(connection, diagnostics);
  dataStatusDetail.textContent = diagnosticsText(diagnostics);
  dataStatusNode.dataset.state =
    connection.data_age_ms === null || connection.data_age_ms === undefined
      ? "none"
      : connection.stale
        ? "stale"
        : "live";
  // 서버가 준 패킷 나이를 받은 시각에서 빼면 클라이언트 시계 기준의 마지막
  // 패킷 수신 시점이 된다. 이후 폴링이 실패해도 이 시점에서 초를 계속 센다.
  lastSuccessMs = Date.now();
  lastPacketMs = Number.isFinite(connection.data_age_ms)
    ? lastSuccessMs - connection.data_age_ms
    : null;
  updateDataAge();
  setRaceContext(payload);
}

/** "마지막 수신 N.N초 전"을 다시 그린다. 수신 기록이 없으면 안내 문구. */
function updateDataAge() {
  if (!dataStatusAge) return;
  if (lastPacketMs === null) {
    dataStatusAge.textContent = lastSuccessMs === null ? "" : "수신 기록 없음";
    return;
  }
  const seconds = Math.max(0, Date.now() - lastPacketMs) / 1000;
  dataStatusAge.textContent = `마지막 수신 ${seconds.toFixed(1)}초 전`;
}

/** 0.25초마다 마지막 수신 초를 다시 세고, 3초 넘게 응답이 없으면 오류로 바꾼다. */
function dataTick() {
  updateDataAge();
  if (engineState !== "on") return;
  const anchor = lastSuccessMs === null ? pollStartedMs : lastSuccessMs;
  if (anchor === null) return;
  if (Date.now() - anchor > DATA_TIMEOUT_MS) markDataError();
}

/** `/api/state` 조회 실패·무응답. 마지막 수신 초는 계속 세면서 빨간색으로 알린다. */
function markDataError() {
  dataStatusNode.dataset.state = "error";
  dataStatusValue.textContent = "서버 응답 없음";
  dataStatusDetail.textContent = "";
  updateDataAge();
}

/** 엔진이 꺼졌을 때. 수신이 없으므로 대시보드와 같은 "데이터 없음"으로 둔다. */
function clearDataStatus() {
  dataStatusValue.textContent = connectionText({}, {});
  dataStatusDetail.textContent = "";
  dataStatusNode.dataset.state = "none";
  lastSuccessMs = null;
  lastPacketMs = null;
  if (dataStatusAge) dataStatusAge.textContent = "";
  clearRaceContext();
}

/** 세션 종류·현재/다음 코너·남은 거리. 트랙을 모르면 모두 `—`로 둔다. */
function setRaceContext(payload) {
  const session = payload.session || {};
  const corner = payload.corner || {};
  sessionValue.textContent = session.label || "—";
  if (!corner.track_loaded) {
    currentCornerValue.textContent = "—";
    nextCornerValue.textContent = "—";
    nextCornerDistance.textContent = "—";
    return;
  }
  currentCornerValue.textContent =
    corner.current_corner === null || corner.current_corner === undefined
      ? "직선"
      : `${corner.current_corner}번`;
  nextCornerValue.textContent =
    corner.next_corner === null || corner.next_corner === undefined
      ? "—"
      : `${corner.next_corner}번`;
  nextCornerDistance.textContent =
    typeof corner.distance_to_next_corner_m === "number"
      ? `${corner.distance_to_next_corner_m.toFixed(2)} m`
      : "—";
}

function clearRaceContext() {
  sessionValue.textContent = "—";
  currentCornerValue.textContent = "—";
  nextCornerValue.textContent = "—";
  nextCornerDistance.textContent = "—";
}

function startDataPolling() {
  if (dataTickTimer === null) {
    pollStartedMs = Date.now();
    dataTickTimer = window.setInterval(dataTick, DATA_TICK_MS);
  }
  if (dataPollTimer !== null || dataRefreshing) return;
  refreshDataStatus();
}

function stopDataPolling() {
  if (dataPollTimer !== null) {
    window.clearTimeout(dataPollTimer);
    dataPollTimer = null;
  }
  if (dataTickTimer !== null) {
    window.clearInterval(dataTickTimer);
    dataTickTimer = null;
  }
}

function scheduleDataPoll() {
  if (dataPollTimer !== null || engineState !== "on") return;
  dataPollTimer = window.setTimeout(() => {
    dataPollTimer = null;
    refreshDataStatus();
  }, DATA_POLL_MS);
}

async function refreshDataStatus() {
  if (dataRefreshing) return;
  dataRefreshing = true;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (response.status === 503) {
      // 서버는 떠 있지만 엔진이 꺼진 상태다. 엔진 안내·상태 폴링으로 넘긴다.
      setEngineState("off");
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (engineState === "on") setDataStatus(payload);
  } catch (error) {
    // 서버가 죽었거나 네트워크가 끊겼다. 마지막 수신 초는 계속 세면서 빨간
    // 상태로 바꾸고, 다음 주기에 다시 묻는다.
    if (engineState === "on") markDataError();
  } finally {
    dataRefreshing = false;
    scheduleDataPoll();
  }
}

function setVoice(label, reason) {
  const text = label || "음성 오프라인";
  // 실패 이유는 서버가 준 문자열을 textContent로만 넣는다(HTML 삽입 금지).
  voiceNode.textContent = reason ? `${text} (${reason})` : text;
}

/**
 * 엔진 상태에 맞춰 무전 페이지를 바꾼다.
 * OFF면 안내를 보여 주고 연결·재연결을 멈춘다. 웹 서버는 살아 있으므로
 * 몇 초마다 `/api/engine`을 다시 확인해 켜지면 버튼을 되살린다.
 */
function setEngineState(state, { pollWhenOff = true } = {}) {
  engineState = state === "on" ? "on" : "off";
  const on = engineState === "on";
  if (engineOffNode) engineOffNode.hidden = on;
  connectButton.disabled = !on;
  if (on) {
    if (connectionNode.dataset.state === "disconnected") {
      setConnection("disconnected", "무전 연결 대기");
    }
    startDataPolling();
    return;
  }
  started = false;
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
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
  stopAudio();
  currentPriority = null;
  stopDataPolling();
  clearDataStatus();
  setConnection("disconnected", "레이스 엔지니어 꺼짐");
  if (pollWhenOff) scheduleEnginePoll();
}

function scheduleEnginePoll() {
  if (enginePollTimer !== null) return;
  enginePollTimer = window.setTimeout(async () => {
    enginePollTimer = null;
    await refreshEngineState({ pollWhenOff: false });
    if (engineState !== "on") scheduleEnginePoll();
  }, 3000);
}

async function refreshEngineState({ pollWhenOff = true } = {}) {
  try {
    const response = await fetch("/api/engine", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    setEngineState(data.state, { pollWhenOff });
  } catch (error) {
    // 서버가 아직 안 떴거나 잠깐 실패했다. 다시 확인한다.
    if (engineState !== "on") scheduleEnginePoll();
  }
}

function showCaption(text, meta) {
  captionText.textContent = text || "아직 무전이 없습니다.";
  captionMeta.textContent = meta || "";
}

function ensureAudioContext() {
  if (audioContext) return audioContext;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) {
    setConnection("disconnected", "이 브라우저는 소리 재생을 지원하지 않습니다");
    return null;
  }
  audioContext = new Ctor();
  return audioContext;
}

function stopAudio() {
  for (const source of activeSources) {
    try {
      source.stop();
    } catch (error) {
      // 이미 끝난 소스는 무시한다.
    }
  }
  activeSources.clear();
  nextStartTime = 0;
}

function base64ToSamples(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  // Int16Array는 짝수 길이 버퍼가 필요하다. 잘린 마지막 바이트는 버린다.
  const usable = bytes.byteLength - (bytes.byteLength % 2);
  const copy = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + usable);
  return new Int16Array(copy);
}

function playAudio(payload) {
  const context = ensureAudioContext();
  if (!context) return;
  if (context.state === "suspended") {
    // 탭이 백그라운드였다가 돌아온 경우. 사용자 클릭 뒤라 재개할 수 있다.
    context.resume().catch(() => {});
  }

  const samples = base64ToSamples(payload.pcm || "");
  if (samples.length === 0) return;

  const buffer = context.createBuffer(
    1,
    samples.length,
    payload.sample_rate || 24000,
  );
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i += 1) {
    channel[i] = samples[i] / 32768;
  }

  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(context.destination);

  // 조각을 순서대로 이어 붙인다. 이미 재생이 밀렸을 때만 지금부터 다시 맞추고,
  // 앞 조각보다 뒤에 있으면 아무리 멀어도 그 뒤에 붙인다(겹쳐 재생 금지).
  const now = context.currentTime + 0.02;
  if (nextStartTime < now) {
    nextStartTime = now;
  }
  source.start(nextStartTime);
  nextStartTime += buffer.duration;
  activeSources.add(source);
  source.onended = () => activeSources.delete(source);
}

function handleStatus(payload) {
  const status = payload.status || {};
  const voiceOffline = status.voice && status.voice.state === "offline";
  if (status.voice) setVoice(status.voice.label, status.voice.reason);
  if (status.radio_page) {
    const connected = Boolean(status.radio_page.connected);
    if (!connected) {
      setConnection("connecting", "서버에 연결하는 중…");
    } else if (voiceOffline) {
      // 페이지는 붙었지만 음성 API가 안 되면 캐시에 있는 문장만 나간다.
      setConnection("connecting", "무전 페이지 연결됨 · 음성 오프라인");
    } else {
      setConnection("connected", "무전 페이지 연결됨");
    }
  }
  const last = payload.last;
  if (last && last.text) {
    showCaption(last.text, metaFor(last));
  }
}

function metaFor(payload) {
  const parts = [];
  if (payload.kind) parts.push(KIND_LABELS[payload.kind] || payload.kind);
  if (payload.lap) parts.push(`${payload.lap}랩`);
  return parts.join(" · ");
}

function handleMessage(payload) {
  switch (payload.type) {
    case "engine":
      // 서버가 엔진을 껐다. 안내로 바꾸고 재연결하지 않는다(SCRUM-82).
      setEngineState(payload.state);
      break;
    case "status":
      handleStatus(payload);
      break;
    case "caption":
      if (typeof payload.priority === "number") {
        if (
          currentPriority !== null &&
          payload.priority < currentPriority &&
          activeSources.size > 0
        ) {
          // 말하는 중에 더 높은 우선순위가 왔다. 지금 소리를 끊는다.
          stopAudio();
        }
        currentPriority = payload.priority;
      }
      showCaption(payload.text, metaFor(payload));
      break;
    case "audio":
      playAudio(payload);
      break;
    case "interrupt":
      // 서버가 더 높은 우선순위 무전으로 끊었다. 재생 중인 조각을 모두 멈추고
      // 다음 조각을 지금부터 이어 붙인다.
      stopAudio();
      if (audioContext) nextStartTime = audioContext.currentTime;
      break;
    case "done":
      if (payload.interrupted) stopAudio();
      if (payload.played === false) {
        captionMeta.textContent = `${metaFor(payload)} · 음성 재생 실패`.replace(
          /^ · /,
          "",
        );
      }
      break;
    default:
      break;
  }
}

/**
 * 음성 상태를 서버에 다시 확인한다(SCRUM-92). 서버가 Qwen을 한 번 호출하고,
 * 돌아온 상태를 화면에 그대로 반영한다. 실패해도 버튼은 다시 켠다.
 */
async function refreshVoiceStatus() {
  voiceCheckButton.disabled = true;
  try {
    const response = await fetch("/api/voice/check", { method: "POST" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data && data.status) handleStatus({ status: data.status });
  } catch (error) {
    // 서버가 잠깐 응답하지 않을 수 있다. 다음 상태 메시지가 오면 갱신된다.
  } finally {
    voiceCheckButton.disabled = false;
  }
}

function connect() {
  if (engineState !== "on") return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  setConnection("connecting", "서버에 연결하는 중…");

  socket = new WebSocket(`${scheme}://${window.location.host}/ws/radio`);

  socket.addEventListener("open", () => {
    setConnection("connected", "무전 페이지 연결됨");
  });

  socket.addEventListener("message", (event) => {
    try {
      handleMessage(JSON.parse(event.data));
    } catch (error) {
      // 잘못된 메시지는 버리고 다음 무전을 기다린다.
    }
  });

  socket.addEventListener("close", () => {
    socket = null;
    if (engineState !== "on") {
      setConnection("disconnected", "레이스 엔지니어 꺼짐");
      return;
    }
    setConnection("disconnected", "서버 연결 끊김 · 다시 연결 중…");
    scheduleReconnect();
  });

  socket.addEventListener("error", () => {
    if (socket) socket.close();
  });
}

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    if (started) connect();
  }, 1000);
}

voiceCheckButton.addEventListener("click", () => {
  refreshVoiceStatus();
});

connectButton.addEventListener("click", () => {
  if (engineState !== "on") return;
  // 사용자 클릭 안에서 AudioContext를 만들어야 자동 재생 정책을 통과한다.
  ensureAudioContext();
  if (audioContext && audioContext.state === "suspended") {
    audioContext.resume().catch(() => {});
  }
  started = true;
  connectButton.disabled = true;
  connect();
});

// 탭이 다시 보이면 소리 재생을 재개한다(백그라운드에서 잠든 경우).
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && audioContext && audioContext.state === "suspended") {
    audioContext.resume().catch(() => {});
  }
});

// 페이지를 열면 엔진 상태를 먼저 확인한다. OFF면 안내만 보여 주고 3초마다
// 다시 확인해, 대시보드에서 켜면 이 페이지의 연결 버튼도 되살아난다.
refreshEngineState();
