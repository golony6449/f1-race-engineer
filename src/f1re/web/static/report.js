/**
 * 무전 기록·종류별 끄기·세션 뒤 코너 리포트(설계 6.7, 6.9-3·4, 작업 20).
 *
 * 화면은 서버 API를 읽기만 한다. 무전 끄기 버튼만 POST로 상태를 바꾼다.
 * - `/api/radio/log`: 최근 무전(문장 + 근거 수치), 종류별 끄기 상태.
 * - `/api/radio/mute`: 끄기/켜기. 안전 경고는 서버가 409로 거부한다.
 * - `/api/report`: 코너별 손실 순위, 집중 코너 변화, 무전 뒤 개선 여부.
 */

const LOG_URL = "/api/radio/log";
const MUTE_URL = "/api/radio/mute";
const REPORT_URL = "/api/report";
const LOG_REFRESH_MS = 2000;
const REPORT_REFRESH_MS = 5000;

let muteBusy = false;

// 코치가 남기는 원인 키(`coach.Cause.key`)의 화면 이름.
const CAUSE_LABELS = {
  brake_late: "브레이크 늦음",
  brake_early: "브레이크 일찍",
  turn_in_early: "턴인 일찍",
  turn_in_late: "턴인 늦음",
  steer_corrections: "조향 수정",
  min_speed_low: "정점 속도 낮음",
  late_throttle: "스로틀 늦음",
  line_wide: "라인 넓음",
  off_track: "트랙 이탈",
};

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function timeText(ns) {
  if (!Number.isFinite(ns)) return "";
  return new Date(ns / 1e6).toLocaleTimeString("ko-KR", { hour12: false });
}

function evidenceText(evidence) {
  if (!evidence || typeof evidence !== "object") return "";
  const parts = [];
  if (typeof evidence.cause === "string") {
    parts.push(CAUSE_LABELS[evidence.cause] || evidence.cause);
  }
  if (Number.isFinite(evidence.amount)) {
    parts.push(`${evidence.amount}${evidence.unit || ""}`);
  }
  if (Number.isFinite(evidence.loss_ms)) {
    parts.push(`손실 ${Math.round(evidence.loss_ms)}ms`);
  }
  if (parts.length === 0) {
    // 근거 수치를 최대 세 개까지 보여 준다(중첩 객체는 건너뛴다).
    for (const [key, value] of Object.entries(evidence)) {
      if (parts.length >= 3) break;
      if (typeof value === "number") {
        parts.push(`${key} ${Math.round(value * 10) / 10}`);
      } else if (typeof value === "string") {
        parts.push(`${key} ${value}`);
      }
    }
  }
  return parts.join(" · ");
}

function renderMutes(root, kinds) {
  if (!root) return;
  root.replaceChildren();
  if (!Array.isArray(kinds)) return;
  for (const item of kinds) {
    const button = element("button", "mute-button", item.label || item.kind);
    button.type = "button";
    button.dataset.kind = item.kind;
    button.setAttribute("aria-pressed", item.muted ? "true" : "false");
    button.disabled = item.mutable === false;
    if (item.muted) button.classList.add("is-muted");
    if (item.mutable === false) {
      button.title = "안전 경고는 끌 수 없습니다";
    }
    button.addEventListener("click", () => {
      toggleMute(item.kind, !item.muted);
    });
    root.append(button);
  }
}

function renderLog(root, log) {
  if (!root) return;
  root.replaceChildren();
  if (!log.available) {
    root.append(
      element("li", "waiting", log.reason || "무전 기록을 읽지 못했습니다")
    );
    return;
  }
  if (!Array.isArray(log.messages) || log.messages.length === 0) {
    root.append(element("li", "waiting", "아직 무전이 없습니다"));
    return;
  }
  for (const message of log.messages) {
    const item = element("li", "radio-entry");
    const head = element("div", "radio-head");
    head.append(element("span", "radio-time", timeText(message.recorded_at_ns)));
    head.append(
      element("span", "radio-kind", message.kind_label || message.kind)
    );
    if (message.lap !== null && message.lap !== undefined) {
      head.append(element("span", "radio-meta", `랩 ${message.lap}`));
    }
    if (message.corner !== null && message.corner !== undefined) {
      head.append(element("span", "radio-meta", `${message.corner}번 코너`));
    }
    const played = message.played
      ? message.latency_ms === null || message.latency_ms === undefined
        ? "재생"
        : `재생 ${message.latency_ms}ms`
      : "재생 안 됨";
    head.append(element("span", "radio-played", played));
    item.append(head);
    item.append(element("p", "radio-text", message.text));

    const evidence = evidenceText(message.evidence);
    if (evidence) {
      item.append(element("p", "radio-evidence", `근거: ${evidence}`));
    }
    root.append(item);
  }
}

function renderReport(root, report) {
  if (!root) return;
  root.replaceChildren();
  if (!report.available) {
    root.append(element("p", "waiting", report.reason || "리포트를 읽지 못했습니다"));
    return;
  }
  if (!report.message_count) {
    root.append(element("p", "waiting", "이 세션에 아직 무전 기록이 없습니다"));
    return;
  }

  const summary = report.summary || {};
  root.append(
    element(
      "p",
      "report-summary",
      `세션 ${report.session_uid ?? "-"} · 무전 ${summary.messages ?? 0} · ` +
        `개선 ${summary.improved ?? 0} · ` +
        `악화 ${summary.worsened ?? 0} · 판단 전 ${summary.unknown ?? 0}`
    )
  );
  if (report.data_source === "radio_evidence") {
    root.append(
      element(
        "p",
        "report-source",
        "무전 근거 수치 기준 · 완료 랩 전체 손실은 아직 저장하지 않습니다"
      )
    );
  }

  root.append(element("h3", "report-title", "코너별 손실 순위"));
  const losses = Array.isArray(report.corner_losses) ? report.corner_losses : [];
  if (losses.length === 0) {
    root.append(element("p", "waiting", "손실 근거가 있는 무전이 없습니다"));
  } else {
    const table = element("table", "report-table");
    const head = element("tr");
    for (const label of ["코너", "최근 손실", "최고", "무전"]) {
      head.append(element("th", null, label));
    }
    table.append(head);
    for (const corner of losses) {
      const row = element("tr");
      row.append(element("td", null, `${corner.corner}번`));
      row.append(element("td", null, `${Math.round(corner.latest_loss_ms)}ms`));
      row.append(element("td", null, `${Math.round(corner.best_loss_ms)}ms`));
      row.append(element("td", null, corner.messages));
      table.append(row);
    }
    root.append(table);
  }

  root.append(element("h3", "report-title", "집중 코너 변화"));
  const changes = Array.isArray(report.focus_changes)
    ? report.focus_changes
    : [];
  if (changes.length === 0) {
    root.append(element("p", "waiting", "아직 개선 알림이 없습니다"));
  } else {
    const list = element("ol", "report-list");
    for (const change of changes) {
      const corners = change.next_corner
        ? `${change.corner}번 → ${change.next_corner}번`
        : `${change.corner}번`;
      const loss = Number.isFinite(change.loss_ms)
        ? ` · ${change.loss_ms}ms`
        : "";
      list.append(
        element("li", null, `랩 ${change.lap} · ${corners}${loss}`)
      );
    }
    root.append(list);
  }

  root.append(element("h3", "report-title", "무전이 나간 코너와 그 뒤 결과"));
  const improvements = Array.isArray(report.message_improvements)
    ? report.message_improvements
    : [];
  if (improvements.length === 0) {
    root.append(element("p", "waiting", "코칭 무전이 없습니다"));
  } else {
    const list = element("ol", "report-list");
    for (const item of improvements) {
      const mark = {
        improved: "개선",
        worsened: "악화",
        steady: "비슷",
        unknown: "판단 전",
      }[item.status] || "판단 전";
      const next = Number.isFinite(item.next_loss_ms)
        ? ` · 다음 ${item.next_loss_ms}ms`
        : "";
      list.append(
        element(
          "li",
          `report-${item.status}`,
          `랩 ${item.lap} · ${item.corner}번 · ${item.text} (${mark}${next})`
        )
      );
    }
    root.append(list);
  }
}

async function refreshLog() {
  try {
    const log = await getJson(LOG_URL);
    renderMutes(document.getElementById("radio-mutes"), log.kinds);
    renderLog(document.getElementById("radio-log"), log);
  } catch (error) {
    // 서버가 아직 안 떴거나 잠깐 실패했다. 다음 주기에 다시 시도한다.
  }
}

async function refreshReport() {
  try {
    const report = await getJson(REPORT_URL);
    renderReport(document.getElementById("session-report"), report);
  } catch (error) {
    // 다음 주기에 다시 시도한다.
  }
}

async function toggleMute(kind, muted) {
  if (muteBusy) return;
  muteBusy = true;
  try {
    const response = await fetch(MUTE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, muted }),
    });
    // 안전 경고(409)는 화면이 이미 끌 수 없게 막는다. 응답은 조용히 무시한다.
    if (!response.ok && response.status !== 409) {
      throw new Error(`HTTP ${response.status}`);
    }
    await refreshLog();
  } catch (error) {
    // 다음 새로 고침에서 서버 상태를 다시 읽는다.
  } finally {
    muteBusy = false;
  }
}

if (document.getElementById("radio-log")) {
  refreshLog();
  refreshReport();
  window.setInterval(refreshLog, LOG_REFRESH_MS);
  window.setInterval(refreshReport, REPORT_REFRESH_MS);
}
