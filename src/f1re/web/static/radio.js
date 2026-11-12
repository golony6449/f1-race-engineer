/**
 * 무전 페이지. 서버가 보내는 음성 조각을 이어 붙여 재생하고, 연결 상태와
 * 마지막 무전 자막을 크게 보여 준다(설계 6.10).
 *
 * - 브라우저 자동 재생 정책 때문에 "무전 연결" 버튼을 한 번 눌러야 한다.
 *   버튼을 누르면 AudioContext를 만들고 WebSocket을 연다.
 * - 서버 → 브라우저 메시지(JSON):
 *   status / caption / audio(base64 PCM) / done
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

let socket = null;
let audioContext = null;
let nextStartTime = 0;
let reconnectTimer = null;
let started = false;
// 지금 재생 중인(또는 마지막으로 시작한) 무전의 우선순위. 숫자가 작을수록 높다.
// 서버는 캐시 음성을 한 번에 보내므로, 더 높은 우선순위가 오면 브라우저가
// 재생 중인 소리를 끊고 새 무전을 바로 시작한다(설계 6.7).
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

  // 조각을 순서대로 이어 붙인다. 이미 재생이 밀렸으면 지금부터 다시 맞춘다.
  const now = context.currentTime + 0.02;
  if (nextStartTime < now || nextStartTime > now + 2) {
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
