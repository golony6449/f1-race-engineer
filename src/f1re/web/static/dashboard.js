/**
 * 대시보드 연결. 화면은 서버 API를 읽기만 한다(설계 6.9).
 *
 * - `/api/engine`: 레이스 엔지니어 ON/OFF 상태. 꺼져 있으면 대시보드 본문 대신
 *   안내만 보여 주고 WebSocket에 연결하지 않는다(SCRUM-82).
 * - `/api/thresholds`: 색 표. 문턱값은 화면에서 계산하지 않고 이 표만 쓴다.
 * - `/api/state`: 첫 화면. WebSocket이 붙기 전에도 바로 값을 보여 준다.
 * - `/ws/state`: 서버가 10Hz로 미는 상태. 끊기면 1초 뒤 다시 연결한다.
 *   서버가 엔진을 끄면 `{"type": "engine", "state": "off"}`를 보내고 닫는다.
 *
 * 다시 보기(설계 4.6, SCRUM-101):
 * - `/api/recordings`: 지난 세션 목록. 모달에서 종류별로 거른다.
 * - `/api/replay/start`·`/api/replay/control`: 리플레이 시작·일시정지·배속·시크·중지.
 * - `/ws/replay`: 실시간 `/ws/state`와 같은 상태 + `replay` 메타. 리플레이
 *   모드에서는 같은 차량 상태·서킷 지도·무전 기록·리포트 패널을 그대로 쓰고
 *   데이터 소스만 리플레이 API로 바꾼다.
 * - `/api/replay/track`·`/api/replay/report`·`/api/replay/radio/log`:
 *   리플레이 중 서킷·리포트·무전 기록. report.js가 `/api/replay/*`로 전환한다.
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

const recordingsButton = document.getElementById("btn-open-recordings");
const recordingsModal = document.getElementById("recordings-modal");
const recordingsFilter = document.getElementById("recordings-filter");
const recordingsStatus = document.getElementById("recordings-status");
const recordingsList = document.getElementById("recordings-list");
const recordingsCloseButton = document.getElementById("recordings-close");

const replayBar = document.getElementById("replay-bar");
const replayTitle = document.getElementById("replay-title");
const replayToggle = document.getElementById("replay-toggle");
const replaySpeeds = document.getElementById("replay-speeds");
const replaySeek = document.getElementById("replay-seek");
const replayTime = document.getElementById("replay-time");
const replayBackLive = document.getElementById("replay-back-live");

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

// 다시 보기 목록에 쓰는 이름. 서버가 `category_label`·`end_reason_label`을
// 주면 그 값을 먼저 쓰고, 없을 때만 이 표로 대신한다.
const CATEGORY_LABELS = {
  practice: "연습",
  qualifying: "예선",
  race: "레이스",
  time_trial: "타임 트라이얼",
  unknown: "미분류",
};

const END_REASON_LABELS = {
  recording: "녹화 중",
  session_end: "정상 종료",
  session_switch: "세션 전환",
  inactivity_timeout: "10분 만료",
  engine_off: "엔진 종료",
  recovered: "복구",
};

// 다시 보기 배속 버튼(설계 4.6-3).
const DEFAULT_REPLAY_SPEED = 1.0;
const RECORDINGS_LIMIT = 50;

let engineState = "off";
let socket = null;
let reconnectTimer = null;
let thresholdsLoaded = false;

// `/api/track`은 세션의 트랙 ID가 처음 알려질 때 한 번 받는다. 윤곽이 아직
// 없으면(처음 오는 서킷) 서버가 첫 유효 랩에서 배우므로 주기적으로 다시 묻는다.
const TRACK_RETRY_MS = 5000;
let loadedTrackId = null;
let trackRetryTimer = null;

// 다시 보기 상태. 리플레이 중에는 실시간 WebSocket 대신 `/ws/replay`를 쓴다.
let replayActive = false;
let replaySocket = null;
let replayReconnectTimer = null;
let replayMeta = null;
let replayRecording = null;
let replaySeeking = false;

// 세션 목록 모달 상태.
let recordingsCategory = "";
let recordingsLoading = false;

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
    // 서버가 아직 안 떴거나 잠깐 실패했다. 엔진이 켜져 있거나 다시 보기 중일
    // 때만 다시 시도한다(엔진 OFF면 서버가 503으로 답한다).
    window.setTimeout(() => {
      if (engineState === "on" || replayActive) loadThresholds();
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
  if (replayActive) return;
  setStale(dashboard, payload.connection && payload.connection.stale);
  view.render(payload.car);
  renderStatusBar(payload);
  mapView.render(payload);
  maybeLoadTrack(payload);
}

/**
 * 리플레이 상태 묶음. 구조는 실시간과 같고 `replay` 메타가 더 붙는다.
 * 파일을 되감는 동안에는 "수신 끊김" 개념이 없으므로 흐림(stale)을 풀고,
 * 상태줄에는 리플레이가 살아 있음을 보여 준다.
 */
function applyReplayPayload(payload) {
  if (!payload) return;
  const replay = payload.replay || {};
  if (replay.active === false) {
    exitReplayMode();
    return;
  }
  replayMeta = replay;
  updateReplayBar(replay);
  setStale(dashboard, false);
  view.render(payload.car);
  renderStatusBar({
    ...payload,
    connection: { ...(payload.connection || {}), stale: false, data_age_ms: 0 },
  });
  // map.js의 리프트 앤 코스트 추천은 실시간 엔진(`/api/lift-coast`)에서 오므로
  // 다시 보기 지도에 섞이지 않게 매 프레임 비운다(map.js는 이 작업 소유가 아님).
  mapView.liftZones.clear();
  mapView.render(payload);
}

/** 엔진 ON이거나 다시 보기 중이면 대시보드 본문을 보여 준다. */
function updateLayout() {
  const showDashboard = replayActive || engineState === "on";
  engineOffNotice.hidden = showDashboard;
  dashboard.hidden = !showDashboard;
  statusbar.hidden = !showDashboard;
  replayBar.hidden = !replayActive;
  recordingsButton.setAttribute("aria-expanded", String(!recordingsModal.hidden));
}

/** 엔진 상태에 맞춰 화면·연결을 바꾼다. */
function setEngineUI(state) {
  const next = state === "on" ? "on" : "off";
  engineState = next;
  const on = next === "on";
  settingsButton.dataset.engine = next;
  engineToggle.checked = on;
  engineStateLabel.textContent = ENGINE_TEXT[next].label;
  engineDescription.textContent = ENGINE_TEXT[next].description;
  updateLayout();

  if (!on && !replayActive) {
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
  if (replayActive) return;
  if (!thresholdsLoaded) loadThresholds();
  loadInitialState();
  connect();
}

function connect() {
  if (engineState !== "on") return;
  if (replayActive) return;
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
    if (engineState !== "on" || replayActive) return;
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, 1000);
  });

  socket.addEventListener("error", () => {
    if (socket) socket.close();
  });
}

// ---------------------------------------------------------------------------
// 다시 보기: 세션 목록 모달

function openRecordings() {
  recordingsModal.hidden = false;
  recordingsButton.setAttribute("aria-expanded", "true");
  loadRecordings();
  if (recordingsCloseButton) recordingsCloseButton.focus();
}

function closeRecordings() {
  recordingsModal.hidden = true;
  recordingsButton.setAttribute("aria-expanded", "false");
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

function formatDuration(ms) {
  const total = Number.isFinite(ms) && ms > 0 ? Math.floor(ms / 1000) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const two = (value) => String(value).padStart(2, "0");
  if (hours > 0) return `${hours}:${two(minutes)}:${two(seconds)}`;
  return `${minutes}:${two(seconds)}`;
}

function formatSize(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)}GB`;
  return `${Math.max(1, Math.round(mb))}MB`;
}

function trackLabel(recording) {
  if (recording.track_name) return recording.track_name;
  if (recording.track_id !== null && recording.track_id !== undefined) {
    return `서킷 ${recording.track_id}`;
  }
  return "서킷 미상";
}

function categoryLabel(recording) {
  return (
    recording.category_label ||
    CATEGORY_LABELS[recording.category] ||
    "미분류"
  );
}

function endReasonLabel(recording) {
  return (
    recording.end_reason_label ||
    END_REASON_LABELS[recording.end_reason] ||
    recording.end_reason ||
    "알 수 없음"
  );
}

function badge(text, className) {
  const node = document.createElement("span");
  node.className = className;
  node.textContent = text;
  return node;
}

/** 종류·부(2부 등)·종료 사유 배지 묶음. */
function recordingBadges(recording, splitSession) {
  const badges = document.createElement("div");
  badges.className = "recording-badges";
  badges.append(
    badge(categoryLabel(recording), `category-badge category-${recording.category || "unknown"}`),
  );
  const segment = Number.isFinite(recording.segment) ? recording.segment : 1;
  if (segment > 1 || splitSession) {
    badges.append(badge(`${segment}부`, "segment-badge"));
  }
  const reason = recording.end_reason || "recording";
  badges.append(
    badge(endReasonLabel(recording), `end-reason-badge end-reason-${reason}`),
  );
  return badges;
}

// 백업 사유가 길면(rclone stderr 등) 목록 줄을 망치지 않게 줄인다. 서버는
// `backup_error`를 500자까지만 남기지만 화면에는 그보다 짧게 보여 준다.
const BACKUP_ISSUE_MAX_CHARS = 120;

/**
 * `skipped`·`failed` 녹화의 백업 사유 문구. 사유가 없거나 `uploaded`·진행
 * 중이면 빈 문자열이다. 서버가 준 문자열을 그대로 `textContent`로만 쓰므로
 * HTML로 해석되지 않는다(XSS 없음).
 */
function backupIssueText(recording) {
  const status = recording.backup_status;
  if (status !== "skipped" && status !== "failed") return "";
  const reason =
    typeof recording.backup_error === "string"
      ? recording.backup_error.trim()
      : "";
  if (!reason) return "";
  const label = status === "skipped" ? "백업 안 함" : "백업 실패";
  const text = `${label} (${reason})`;
  return text.length > BACKUP_ISSUE_MAX_CHARS
    ? `${text.slice(0, BACKUP_ISSUE_MAX_CHARS - 1)}…`
    : text;
}

/** 로컬/클라우드 보관 위치 표시. 백업 사유가 있으면 함께 붙인다. */
function storageText(recording) {
  const issue = backupIssueText(recording);
  if (recording.local_exists) return "로컬";
  if (recording.backup_status === "uploaded") return "클라우드";
  if (recording.backup_status === "failed") return issue || "백업 실패";
  if (recording.backup_status === "skipped") return issue || "백업 안 함";
  return "백업 대기";
}

/**
 * 서버가 준 목록 항목 하나를 화면으로 만든다.
 *
 * 날짜·서킷·종류 배지·부 표시·종료 사유 배지·길이·보관 위치와 함께
 * 테스트용 다운로드 링크(`/api/recordings/{id}/download`)와 다시 보기 버튼을
 * 둔다. 다운로드·재생이 불가능한 항목은 링크 대신 흐린 글자로 보여 준다.
 */
function recordingItem(recording, splitSession) {
  const item = document.createElement("li");
  item.className = "recording-item";

  const head = document.createElement("div");
  head.className = "recording-head";
  head.append(badge(formatDateTime(recording.started_at_ns), "recording-date"));
  head.append(badge(trackLabel(recording), "recording-track"));
  item.append(head);

  item.append(recordingBadges(recording, splitSession));

  const meta = document.createElement("div");
  meta.className = "recording-meta";
  const parts = [formatDuration(recording.duration_ms)];
  const size = formatSize(recording.file_size_bytes);
  if (size) parts.push(size);
  if (Number.isFinite(recording.packet_count)) {
    parts.push(`패킷 ${recording.packet_count.toLocaleString("ko-KR")}`);
  }
  parts.push(storageText(recording));
  // 로컬 파일이 남아 있으면 보관 위치가 "로컬"만 보여 백업 사유가 묻힌다.
  // 사유가 있으면 보관 위치와 별개로 한 번 더 붙여 화면에서 바로 읽게 한다.
  const backupIssue = backupIssueText(recording);
  if (backupIssue && recording.local_exists) parts.push(backupIssue);
  meta.textContent = parts.filter(Boolean).join(" · ");
  // 줄인 사유도 마우스를 올리면 같은 문구를 볼 수 있게 둔다.
  if (backupIssue) meta.title = backupIssue;
  item.append(meta);

  const actions = document.createElement("div");
  actions.className = "recording-actions";

  const downloadable = recording.downloadable !== false;
  if (downloadable) {
    const download = document.createElement("a");
    download.className = "recording-download";
    download.href = `/api/recordings/${encodeURIComponent(recording.id)}/download`;
    download.setAttribute("download", "");
    download.textContent = "다운로드";
    actions.append(download);
  } else {
    const unavailable = document.createElement("span");
    unavailable.className = "recording-download disabled";
    unavailable.textContent = "다운로드 불가";
    unavailable.title = "로컬 파일도 클라우드 백업도 없습니다";
    actions.append(unavailable);
  }

  const play = document.createElement("button");
  play.className = "recording-play";
  play.type = "button";
  play.textContent = "다시 보기";
  play.disabled = !downloadable;
  play.addEventListener("click", () => startReplay(recording, play));
  actions.append(play);

  // 그래프 분석(SCRUM-136, 설계 5.2). `/telemetry?id=<id>`로 바로 연다.
  // 다시 보기와 같은 기준(파일이 있고 녹화가 끝났는지)을 쓴다. 녹화 중이면
  // 분석 API가 409로 답하므로 링크 대신 흐린 글자로 이유를 보여 준다.
  const analyzable = downloadable && recording.end_reason !== "recording";
  if (analyzable) {
    const analyze = document.createElement("a");
    analyze.className = "recording-analyze";
    analyze.href = `/telemetry?id=${encodeURIComponent(recording.id)}`;
    analyze.textContent = "그래프 분석";
    actions.append(analyze);
  } else {
    const analyze = document.createElement("span");
    analyze.className = "recording-analyze disabled";
    analyze.textContent = "그래프 분석 불가";
    analyze.title =
      recording.end_reason === "recording"
        ? "아직 녹화가 끝나지 않은 세션입니다"
        : "로컬 파일도 클라우드 백업도 없습니다";
    actions.append(analyze);
  }

  item.append(actions);
  return item;
}

/** 목록에서 같은 세션 UID가 여러 부로 나뉘었는지 센다(1부·2부 표시용). */
function countSplitSessions(items) {
  const counts = new Map();
  for (const item of items) {
    const uid = item.session_uid;
    counts.set(uid, (counts.get(uid) || 0) + 1);
  }
  return counts;
}

function renderRecordings(items) {
  const list = Array.isArray(items) ? items : [];
  recordingsList.replaceChildren();
  if (list.length === 0) {
    recordingsStatus.textContent = "녹화된 세션이 아직 없습니다.";
    return;
  }
  recordingsStatus.textContent = `세션 ${list.length}개`;
  const splits = countSplitSessions(list);
  for (const recording of list) {
    recordingsList.append(
      recordingItem(recording, (splits.get(recording.session_uid) || 0) > 1),
    );
  }
}

async function loadRecordings() {
  if (recordingsLoading) return;
  recordingsLoading = true;
  recordingsStatus.textContent = "세션 목록을 불러오는 중…";
  try {
    const params = new URLSearchParams({ limit: String(RECORDINGS_LIMIT) });
    if (recordingsCategory) params.set("category", recordingsCategory);
    const response = await fetch(`/api/recordings?${params.toString()}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const items = Array.isArray(data) ? data : data.recordings || [];
    renderRecordings(items);
  } catch (error) {
    recordingsList.replaceChildren();
    recordingsStatus.textContent =
      "세션 목록을 불러오지 못했습니다. 잠시 뒤 다시 시도하세요.";
  } finally {
    recordingsLoading = false;
  }
}

// ---------------------------------------------------------------------------
// 다시 보기: 시작·재생 제어

function normalizeReplayMeta(data, recording) {
  const source = (data && data.replay) || data || {};
  const duration =
    Number.isFinite(source.duration_ms)
      ? source.duration_ms
      : Number.isFinite(recording.duration_ms)
        ? recording.duration_ms
        : 0;
  return {
    active: source.active !== false,
    recording_id: source.recording_id ?? recording.id,
    paused: Boolean(source.paused),
    speed: Number.isFinite(source.speed) ? source.speed : DEFAULT_REPLAY_SPEED,
    position_ms: Number.isFinite(source.position_ms) ? source.position_ms : 0,
    duration_ms: duration,
    category: source.category ?? recording.category,
    track_id: source.track_id ?? recording.track_id,
  };
}

function replayLabel(recording, meta) {
  if (!recording) return "지난 세션";
  const category = (meta && meta.category) || recording.category;
  const parts = [
    recording.category_label || CATEGORY_LABELS[category] || "미분류",
    trackLabel(recording),
    formatDateTime(recording.started_at_ns),
  ];
  const segment = Number.isFinite(recording.segment) ? recording.segment : 1;
  if (segment > 1) parts.push(`${segment}부`);
  return parts.join(" · ");
}

/** 서버가 준 리플레이 메타로 컨트롤 바(시각·배속·재생 상태)를 맞춘다. */
function updateReplayBar(replay) {
  const meta = replay || {};
  const duration = Number.isFinite(meta.duration_ms) ? meta.duration_ms : 0;
  const position = Number.isFinite(meta.position_ms) ? meta.position_ms : 0;
  const max = Math.max(0, Math.round(duration));
  replaySeek.max = String(max);
  if (!replaySeeking) {
    replaySeek.value = String(Math.min(Math.max(0, Math.round(position)), max));
  }
  const shown = replaySeeking ? Number(replaySeek.value) : position;
  replayTime.textContent = `${formatDuration(shown)} / ${formatDuration(duration)}`;

  const paused = Boolean(meta.paused);
  replayToggle.textContent = paused ? "재생" : "일시정지";
  replayToggle.setAttribute("aria-label", paused ? "재생" : "일시정지");

  const speed = Number.isFinite(meta.speed) ? meta.speed : DEFAULT_REPLAY_SPEED;
  for (const button of replaySpeeds.querySelectorAll("[data-replay-speed]")) {
    const active = Math.abs(Number(button.dataset.replaySpeed) - speed) < 0.01;
    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  }
}

async function startReplay(recording, button) {
  if (button) button.disabled = true;
  recordingsStatus.textContent = "리플레이를 준비하는 중… (클라우드 복원은 시간이 걸릴 수 있습니다)";
  try {
    const response = await fetch("/api/replay/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recording_id: recording.id,
        speed: DEFAULT_REPLAY_SPEED,
      }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json().catch(() => ({}));
    enterReplayMode(recording, normalizeReplayMeta(data, recording));
  } catch (error) {
    recordingsStatus.textContent =
      "리플레이를 시작하지 못했습니다. 잠시 뒤 다시 시도하세요.";
    if (button) button.disabled = false;
  }
}

async function replayControl(body) {
  const response = await fetch("/api/replay/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json().catch(() => ({}));
}

function enterReplayMode(recording, meta) {
  replayActive = true;
  replayRecording = recording || null;
  replayMeta = meta;
  replaySeeking = false;
  // 실시간 엔진과의 연결을 끊고 화면 데이터 소스를 리플레이로 바꾼다.
  stopStreaming();
  loadedTrackId = null;
  if (!thresholdsLoaded) loadThresholds();
  updateLayout();
  replayTitle.textContent = replayLabel(recording, meta);
  updateReplayBar(meta);
  mapView.setTrack(null);
  loadReplayTrack();
  connectReplay();
  window.dispatchEvent(
    new CustomEvent("f1re:replay-mode", {
      detail: { active: true, recording },
    }),
  );
  closeRecordings();
}

function exitReplayMode() {
  if (!replayActive) return;
  replayActive = false;
  stopReplayConnection();
  replayRecording = null;
  replayMeta = null;
  replaySeeking = false;
  loadedTrackId = null;
  mapView.setTrack(null);
  updateLayout();
  window.dispatchEvent(
    new CustomEvent("f1re:replay-mode", { detail: { active: false } }),
  );
  if (engineState === "on") startStreaming();
}

function stopReplayConnection() {
  if (replayReconnectTimer !== null) {
    window.clearTimeout(replayReconnectTimer);
    replayReconnectTimer = null;
  }
  if (replaySocket) {
    const closing = replaySocket;
    replaySocket = null;
    try {
      closing.close();
    } catch (error) {
      // 이미 닫힌 소켓은 무시한다.
    }
  }
}

async function fetchReplayState() {
  try {
    const response = await fetch("/api/replay/state", { cache: "no-store" });
    if (!response.ok) return null;
    return await response.json();
  } catch (error) {
    return null;
  }
}

function connectReplay() {
  if (!replayActive) return;
  if (
    replaySocket &&
    (replaySocket.readyState === WebSocket.OPEN ||
      replaySocket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  replaySocket = new WebSocket(`${scheme}://${window.location.host}/ws/replay`);

  replaySocket.addEventListener("message", (event) => {
    try {
      applyReplayPayload(JSON.parse(event.data));
    } catch (error) {
      // 잘못된 메시지는 버리고 다음 메시지를 기다린다.
    }
  });

  replaySocket.addEventListener("close", () => {
    replaySocket = null;
    if (!replayActive) return;
    replayReconnectTimer = window.setTimeout(async () => {
      replayReconnectTimer = null;
      if (!replayActive) return;
      // 서버가 리플레이를 끝냈으면 실시간 화면으로 돌아간다.
      const state = await fetchReplayState();
      if (state && state.replay && state.replay.active === false) {
        exitReplayMode();
        return;
      }
      if (replayActive) connectReplay();
    }, 1000);
  });

  replaySocket.addEventListener("error", () => {
    if (replaySocket) replaySocket.close();
  });
}

/** 세션 서킷 윤곽. `/api/replay/track`은 `/api/track`과 같은 모양이다. */
async function loadReplayTrack(attempt = 0) {
  if (!replayActive) return;
  try {
    const response = await fetch("/api/replay/track", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const track = await response.json();
    if (!replayActive) return;
    mapView.liftZones.clear();
    mapView.setTrack(track);
    if (track && track.outline_source === "none" && attempt < 3) {
      window.setTimeout(() => loadReplayTrack(attempt + 1), 2000);
    }
  } catch (error) {
    if (replayActive && attempt < 3) {
      window.setTimeout(() => loadReplayTrack(attempt + 1), 2000);
    }
  }
}

// ---------------------------------------------------------------------------
// 화면 연결

recordingsButton.addEventListener("click", openRecordings);
for (const trigger of document.querySelectorAll("[data-open-recordings]")) {
  trigger.addEventListener("click", openRecordings);
}
for (const closer of document.querySelectorAll("[data-recordings-close]")) {
  closer.addEventListener("click", closeRecordings);
}

recordingsFilter.addEventListener("click", (event) => {
  const button = event.target.closest("[data-category]");
  if (!button) return;
  recordingsCategory = button.dataset.category || "";
  for (const chip of recordingsFilter.querySelectorAll("[data-category]")) {
    const active = chip === button;
    chip.classList.toggle("on", active);
    chip.setAttribute("aria-pressed", active ? "true" : "false");
  }
  loadRecordings();
});

replayToggle.addEventListener("click", () => {
  if (!replayActive) return;
  const paused = Boolean(replayMeta && replayMeta.paused);
  replayMeta = { ...(replayMeta || {}), paused: !paused };
  updateReplayBar(replayMeta);
  replayControl({ action: paused ? "resume" : "pause" }).catch(() => {});
});

replaySpeeds.addEventListener("click", (event) => {
  const button = event.target.closest("[data-replay-speed]");
  if (!button || !replayActive) return;
  const speed = Number(button.dataset.replaySpeed);
  if (!Number.isFinite(speed)) return;
  replayMeta = { ...(replayMeta || {}), speed };
  updateReplayBar(replayMeta);
  replayControl({ action: "speed", speed }).catch(() => {});
});

replaySeek.addEventListener("pointerdown", () => {
  replaySeeking = true;
});
replaySeek.addEventListener("input", () => {
  replaySeeking = true;
  replayTime.textContent =
    `${formatDuration(Number(replaySeek.value))} / ` +
    `${formatDuration(Number(replaySeek.max))}`;
});
replaySeek.addEventListener("change", () => {
  replaySeeking = false;
  if (!replayActive) return;
  const position = Math.max(0, Math.round(Number(replaySeek.value)));
  replayMeta = { ...(replayMeta || {}), position_ms: position };
  updateReplayBar(replayMeta);
  replayControl({ action: "seek", position_ms: position }).catch(() => {});
});

replayBackLive.addEventListener("click", async () => {
  try {
    await replayControl({ action: "stop" });
  } catch (error) {
    // 이미 끝났거나 서버가 잠깐 실패해도 화면은 실시간으로 돌린다.
  }
  exitReplayMode();
});

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
  if (event.key !== "Escape") return;
  if (!recordingsModal.hidden) {
    closeRecordings();
  } else if (!engineModal.hidden) {
    closeSettings();
  }
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
    if (data.state === "on" && !replayActive) startStreaming();
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
    if (data.state === "on" && !replayActive) startStreaming();
  } catch (error) {
    // 서버가 아직 안 떴다. 잠시 뒤 다시 확인한다.
    setEngineUI("off");
    window.setTimeout(initEngine, 3000);
  }
}

initEngine();
