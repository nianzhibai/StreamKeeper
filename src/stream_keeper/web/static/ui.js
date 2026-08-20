import { icon } from "/static/icons.js?v=20260814";

const sessionState = {
  value: null,
};

export const TASK_STATUS = {
  stopped: { label: "已停止", tone: "idle" },
  waiting: { label: "值守中", tone: "ok" },
  checking: { label: "检查中", tone: "info" },
  queued: { label: "排队中", tone: "warn" },
  recording: { label: "录制中", tone: "live" },
  error: { label: "异常", tone: "bad" },
};

export const QUALITY_LABELS = {
  OD: "原画",
  UHD: "超清",
  HD: "高清",
  SD: "标清",
  LD: "流畅",
};

export const SOURCE_LABELS = {
  auto: "自动源",
  flv: "FLV 优先",
  hls: "HLS",
};

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function setHtmlIfChanged(element, html) {
  if (!element || element.innerHTML === html) return false;
  element.innerHTML = html;
  return true;
}

export function setTextIfChanged(element, text) {
  if (!element) return false;
  const next = String(text ?? "");
  if (element.textContent === next) return false;
  element.textContent = next;
  return true;
}

export function toggle(element, visible) {
  element?.classList.toggle("hidden", !visible);
}

export function formatTime(value, { seconds = false } = {}) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: seconds ? "2-digit" : undefined,
    hour12: false,
  }).format(date);
}

const relativeFormatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });

export function formatRelative(value) {
  if (!value) return "—";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return String(value);
  const seconds = (time - Date.now()) / 1000;
  const distance = Math.abs(seconds);
  if (distance < 45) return "刚刚";
  if (distance < 3600) return relativeFormatter.format(Math.round(seconds / 60), "minute");
  if (distance < 86400) return relativeFormatter.format(Math.round(seconds / 3600), "hour");
  if (distance < 604800) return relativeFormatter.format(Math.round(seconds / 86400), "day");
  return formatTime(value);
}

export function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

export function formatBytes(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value;
  let unit = -1;
  do {
    size /= 1024;
    unit += 1;
  } while (size >= 1024 && unit < units.length - 1);
  return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[unit]}`;
}

export function statusPill(status, { large = false } = {}) {
  const meta = TASK_STATUS[status] || { label: status, tone: "idle" };
  const glyph = meta.tone === "bad" ? icon("alert", "ic-xs") : '<i class="dot"></i>';
  return `<span class="pill tone-${meta.tone}${large ? " pill-lg" : ""}">${glyph}${escapeHtml(meta.label)}</span>`;
}

export function recordingProgress(task, now = Date.now()) {
  if (task?.status !== "recording") return null;

  let elapsed = Number(task.recording_elapsed_seconds);
  if (Number.isFinite(elapsed) && elapsed >= 0) {
    if (task._progressSyncedAt) {
      elapsed += Math.max(0, (now - task._progressSyncedAt) / 1000);
    }
  } else if (task.started_at) {
    const started = new Date(task.started_at).getTime();
    elapsed = Number.isNaN(started) ? 0 : Math.max(0, (now - started) / 1000);
  } else {
    return null;
  }

  const segmentSeconds = Number(task.segment_seconds) || 0;
  const segmentCount = Number(task.segment_count) || 0;
  if (segmentSeconds > 0) {
    let index = Math.floor(elapsed / segmentSeconds) + 1;
    if (segmentCount > 0) index = Math.min(index, segmentCount);
    const segmentElapsed = elapsed % segmentSeconds;
    const ratio = Math.min(1, segmentElapsed / segmentSeconds);
    return {
      elapsed,
      index,
      ratio,
      segmented: true,
      caption: segmentCount > 0 ? `第 ${index}/${segmentCount} 段` : `第 ${index} 段`,
      clock: `${formatDuration(segmentElapsed)} / ${formatDuration(segmentSeconds)}`,
    };
  }

  return {
    elapsed,
    index: null,
    ratio: null,
    segmented: false,
    caption: "已录制",
    clock: formatDuration(elapsed),
  };
}

export function recordingProgressMarkup(task, now = Date.now()) {
  const progress = recordingProgress(task, now);
  if (!progress) return "";
  if (!progress.segmented) {
    return `<div class="progress"><span class="progress-text"><b>${escapeHtml(progress.caption)}</b><em>${escapeHtml(progress.clock)}</em></span></div>`;
  }
  return `
    <div class="progress" title="${escapeHtml(`${progress.caption} ${progress.clock}`)}">
      <span class="progress-text"><b>${escapeHtml(progress.caption)}</b><em>${escapeHtml(progress.clock)}</em></span>
      <span class="progress-track" data-ratio="${progress.ratio.toFixed(4)}" aria-hidden="true"><i></i></span>
    </div>`;
}

/**
 * Applies progress-bar widths through the CSSOM. The page CSP forbids inline
 * style attributes, so the ratio travels in a data attribute instead.
 */
export function paintProgress(root = document) {
  root.querySelectorAll(".progress-track[data-ratio]").forEach((track) => {
    const fill = track.firstElementChild;
    if (!fill) return;
    const width = `${(Number(track.dataset.ratio) * 100).toFixed(2)}%`;
    if (fill.style.width !== width) fill.style.width = width;
  });
}

export function syncProgressClock(tasks) {
  const syncedAt = Date.now();
  return tasks.map((task) => (task.status === "recording" ? { ...task, _progressSyncedAt: syncedAt } : task));
}

const TOAST_GLYPHS = {
  info: "info",
  success: "checkCircle",
  error: "alert",
};

export function toast(message, type = "info") {
  const region = document.querySelector("#toast-region");
  if (!region) return;
  const node = document.createElement("div");
  node.className = `toast toast-${type}`;
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.innerHTML = `${icon(TOAST_GLYPHS[type] || TOAST_GLYPHS.info, "ic-sm")}<p></p>
    <button class="toast-close" type="button" aria-label="关闭通知">${icon("close", "ic-sm")}</button>`;
  node.querySelector("p").textContent = message;

  const dismiss = () => {
    node.classList.add("is-leaving");
    window.setTimeout(() => node.remove(), 200);
  };
  const timer = window.setTimeout(dismiss, type === "error" ? 6000 : 3600);
  node.querySelector(".toast-close").addEventListener("click", () => {
    window.clearTimeout(timer);
    dismiss();
  });
  region.append(node);
  while (region.children.length > 4) region.firstElementChild.remove();
}

/** Promise-based replacement for window.confirm so destructive actions stay on-brand. */
export function confirmAction({
  title = "确认操作",
  message = "",
  confirmLabel = "确定",
  cancelLabel = "取消",
  tone = "danger",
} = {}) {
  const dialog = document.querySelector("#confirm-dialog");
  if (!dialog) return Promise.resolve(window.confirm(message || title));

  const toneClass = { danger: "bad", warn: "warn" }[tone] || "info";
  dialog.querySelector("[data-confirm-title]").textContent = title;
  dialog.querySelector("[data-confirm-message]").textContent = message;
  dialog.querySelector("[data-confirm-glyph]").className = `modal-glyph tone-${toneClass}`;
  const accept = dialog.querySelector("[data-confirm-accept]");
  const cancel = dialog.querySelector("[data-confirm-cancel]");
  accept.textContent = confirmLabel;
  accept.className = `btn ${tone === "danger" ? "btn-danger" : "btn-primary"}`;
  cancel.textContent = cancelLabel;

  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      accept.removeEventListener("click", onAccept);
      cancel.removeEventListener("click", onCancel);
      dialog.removeEventListener("close", onClose);
      dialog.removeEventListener("click", onBackdrop);
      if (dialog.open) dialog.close();
      resolve(value);
    };
    const onAccept = () => finish(true);
    const onCancel = () => finish(false);
    const onClose = () => finish(false);
    const onBackdrop = (event) => {
      if (event.target === dialog) finish(false);
    };

    accept.addEventListener("click", onAccept);
    cancel.addEventListener("click", onCancel);
    dialog.addEventListener("close", onClose);
    dialog.addEventListener("click", onBackdrop);
    dialog.showModal();
    cancel.focus();
  });
}

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const method = String(options.method || "GET").toUpperCase();
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method) && sessionState.value?.csrf_token) {
    headers.set("X-CSRF-Token", sessionState.value.csrf_token);
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
    cache: method === "GET" ? "no-store" : options.cache,
  });
  if (response.status === 401) {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.replace(`/login?next=${encodeURIComponent(next)}`);
    throw new Error("登录已过期");
  }
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof body === "object" ? body.detail : body;
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg).join("；")
      : detail || `请求失败 (${response.status})`;
    throw new Error(message);
  }
  return body;
}

export function setHealth(online) {
  const health = document.querySelector("#server-health");
  if (!health) return;
  health.className = `health ${online ? "is-online" : "is-offline"}`;
  health.innerHTML = `<i></i><span>${online ? "服务正常" : "连接失败"}</span>`;
}

export function showPageError(message) {
  const banner = document.querySelector("#page-error");
  if (!banner) return;
  const text = banner.querySelector("[data-error-text]");
  if (text) text.textContent = message;
  else banner.textContent = message;
  banner.classList.remove("hidden");
}

export function clearPageError() {
  document.querySelector("#page-error")?.classList.add("hidden");
}

/** Marks a refresh control as busy while a request is in flight. */
export function setBusy(element, busy) {
  if (!element) return;
  element.classList.toggle("is-busy", Boolean(busy));
  if (busy) element.setAttribute("aria-busy", "true");
  else element.removeAttribute("aria-busy");
}

async function logout() {
  document.querySelectorAll("[data-logout]").forEach((button) => {
    button.disabled = true;
  });
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    if (!String(error.message).includes("登录已过期")) toast(error.message, "error");
  } finally {
    window.location.replace("/login");
  }
}

function initializeShell(session) {
  const name = session.username || "admin";
  document.querySelectorAll("[data-account-name]").forEach((element) => {
    element.textContent = name;
  });
  document.querySelectorAll("[data-account-avatar]").forEach((element) => {
    element.textContent = [...name][0]?.toUpperCase() || "A";
  });
  document.querySelectorAll("[data-logout]").forEach((button) => button.addEventListener("click", logout));
}

export async function bootstrap(load, { interval = 5000 } = {}) {
  try {
    sessionState.value = await api("/api/auth/session");
    initializeShell(sessionState.value);
    await load();
    setHealth(true);
    document.body.classList.add("is-ready");
    if (interval > 0) {
      window.setInterval(async () => {
        if (document.hidden) return;
        try {
          await load({ quiet: true });
          setHealth(true);
        } catch (error) {
          setHealth(false);
          showPageError(error.message || "无法读取服务器状态");
        }
      }, interval);
    }
  } catch (error) {
    document.body.classList.add("is-ready");
    if (!String(error.message).includes("登录已过期")) {
      setHealth(false);
      showPageError(error.message || "页面加载失败");
    }
  }
}

export function providerStatus(provider, _kind) {
  const configured = Boolean(
    provider?.credential_configured || provider?.access_token_configured || provider?.refresh_token_configured,
  );
  if (provider?.enabled) return { label: "已启用", tone: "ok" };
  return configured ? { label: "未启用", tone: "idle" } : { label: "未配置", tone: "idle" };
}

export function archiveStatus(cloud) {
  if (cloud.running) return { label: "上传中", tone: "live" };
  if (!cloud.last_run) return { label: cloud.enabled ? "等待执行" : "未配置", tone: cloud.enabled ? "info" : "idle" };
  const labels = {
    success: "执行成功",
    partial: "部分失败",
    failed: "执行失败",
    cancelled: "已取消",
  };
  const isError = ["partial", "failed", "cancelled"].includes(cloud.last_run.status);
  return {
    label: labels[cloud.last_run.status] || cloud.last_run.status,
    tone: isError ? "bad" : "ok",
  };
}

export { icon };
