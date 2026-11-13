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
 */

const KIND_LABELS = {
  safety: "안전",
  box: "박스",
  coaching: "코칭",
  overtake: "추월",
  improvement: "개선",
};

const connectionNode = document.getElementById("connection");
const connectButton = document.getElementById("connect");
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

/** 연결 상태 배너. state: connected | connecting | disconnected */
function setConnection(state, text) {
  connectionNode.dataset.state = state;
  connectionNode.textContent = text;
}

function setVoice(label) {
  voiceNode.textContent = label || "음성 오프라인";
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
  if (status.voice) setVoice(status.voice.label);
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
