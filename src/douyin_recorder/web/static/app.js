const state = {
  tasks: [],
  system: null,
  filter: "all",
  loading: false,
};

const elements = {
  taskList: document.querySelector("#task-list"),
  emptyState: document.querySelector("#empty-state"),
  errorBanner: document.querySelector("#error-banner"),
  dialog: document.querySelector("#create-dialog"),
  form: document.querySelector("#create-form"),
  submit: document.querySelector("#create-submit"),
  inspectButton: document.querySelector("#inspect-button"),
  inspectResult: document.querySelector("#inspect-result"),
  health: document.querySelector("#server-health"),
  toastRegion: document.querySelector("#toast-region"),
};

const statusLabels = {
  stopped: "已停止",
  waiting: "值守中",
  checking: "检查中",
  queued: "排队中",
  recording: "录制中",
  error: "异常",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(value) {
  if (!value) return "尚未检查";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function toast(message, type = "info") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.textContent = message;
  elements.toastRegion.append(node);
  window.setTimeout(() => node.remove(), 3600);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
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

function matchesFilter(task) {
  if (state.filter === "recording") return task.status === "recording";
  if (state.filter === "enabled") return task.enabled && task.status !== "recording";
  if (state.filter === "error") return task.status === "error";
  return true;
}

function renderTask(task) {
  const title = task.label || task.anchor_name || "未识别主播";
  const avatar = [...title][0] || "录";
  const statusClass = escapeHtml(task.status);
  const segment = task.segment_seconds ? `${task.segment_seconds}s` : "不分段";
  const action = task.enabled
    ? `<button class="task-action stop" type="button" data-action="stop" data-id="${task.id}">停止</button>`
    : `<button class="task-action start" type="button" data-action="start" data-id="${task.id}">启动</button>`;
  const messageClass = task.status === "error" ? "error" : "";
  return `
    <article class="task-card ${statusClass}">
      <div class="task-card-head">
        <div class="task-identity">
          <div class="avatar">${escapeHtml(avatar)}</div>
          <div class="task-title-wrap">
            <h3 title="${escapeHtml(title)}">${escapeHtml(title)}</h3>
            <a href="${escapeHtml(task.url)}" target="_blank" rel="noreferrer" title="${escapeHtml(task.url)}">${escapeHtml(task.url)}</a>
          </div>
        </div>
        <span class="status-badge ${statusClass}"><i></i>${statusLabels[task.status] || task.status}</span>
      </div>
      <p class="task-message ${messageClass}">${escapeHtml(task.status_message || "等待操作")}</p>
      <div class="task-meta">
        <div><span>画质</span><strong>${escapeHtml(task.quality)}</strong></div>
        <div><span>格式</span><strong>${escapeHtml(task.output_format.toUpperCase())}</strong></div>
        <div><span>直播源</span><strong>${escapeHtml(task.source.toUpperCase())}</strong></div>
        <div><span>分段</span><strong>${escapeHtml(segment)}</strong></div>
      </div>
      <div class="task-footer">
        <span class="last-check">上次检查：${escapeHtml(formatTime(task.last_checked_at))}</span>
        <div class="task-actions">
          ${action}
          <button class="task-action delete" type="button" data-action="delete" data-id="${task.id}">删除</button>
        </div>
      </div>
    </article>`;
}

function render() {
  const visibleTasks = state.tasks.filter(matchesFilter);
  elements.taskList.innerHTML = visibleTasks.map(renderTask).join("");
  elements.taskList.classList.toggle("hidden", visibleTasks.length === 0);
  elements.emptyState.classList.toggle("hidden", state.tasks.length !== 0 || state.filter !== "all");

  document.querySelector("#stat-total").textContent = state.tasks.length;
  document.querySelector("#stat-recording").textContent = state.tasks.filter((task) => task.status === "recording").length;
  document.querySelector("#stat-waiting").textContent = state.tasks.filter(
    (task) => task.enabled && task.status !== "recording",
  ).length;

  if (state.system) {
    document.querySelector("#stat-disk").textContent = `${state.system.free_space_gb} GB`;
    const runtime = [
      state.system.ffmpeg_available ? "FFmpeg ✓" : "FFmpeg 缺失",
      state.system.node_available ? "Node ✓" : "Node 缺失",
    ];
    document.querySelector("#stat-runtime").textContent = runtime.join(" · ");
  }
}

async function refresh({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  try {
    const [tasks, system] = await Promise.all([api("/api/tasks"), api("/api/system")]);
    state.tasks = tasks;
    state.system = system;
    elements.errorBanner.classList.add("hidden");
    elements.health.className = "health online";
    elements.health.innerHTML = "<i></i> 服务正常";
    render();
  } catch (error) {
    elements.health.className = "health offline";
    elements.health.innerHTML = "<i></i> 连接失败";
    elements.errorBanner.textContent = `无法读取服务器状态：${error.message}`;
    elements.errorBanner.classList.remove("hidden");
    if (!quiet) toast(error.message, "error");
  } finally {
    state.loading = false;
  }
}

function openDialog() {
  elements.inspectResult.classList.add("hidden");
  elements.dialog.showModal();
  window.setTimeout(() => document.querySelector("#task-url").focus(), 50);
}

function closeDialog() {
  elements.dialog.close();
}

async function inspectRoom() {
  const url = document.querySelector("#task-url").value.trim();
  const quality = elements.form.elements.quality.value;
  if (!url) {
    document.querySelector("#task-url").reportValidity();
    return;
  }
  elements.inspectButton.disabled = true;
  elements.inspectButton.textContent = "检测中";
  elements.inspectResult.className = "inspect-result";
  elements.inspectResult.textContent = "服务器正在解析直播间…";
  try {
    const result = await api("/api/inspect", {
      method: "POST",
      body: JSON.stringify({ url, quality }),
    });
    elements.inspectResult.className = "inspect-result";
    elements.inspectResult.textContent = `${result.anchor_name || "未知主播"} · ${result.is_live ? "正在直播" : "当前未开播"} · ${result.has_flv ? "FLV" : "无 FLV"} / ${result.has_hls ? "HLS" : "无 HLS"}`;
  } catch (error) {
    elements.inspectResult.className = "inspect-result error";
    elements.inspectResult.textContent = `检测失败：${error.message}`;
  } finally {
    elements.inspectButton.disabled = false;
    elements.inspectButton.textContent = "检测";
  }
}

async function createTask(event) {
  event.preventDefault();
  if (!elements.form.reportValidity()) return;
  const data = new FormData(elements.form);
  const payload = {
    url: String(data.get("url") || "").trim(),
    label: String(data.get("label") || "").trim() || null,
    quality: data.get("quality"),
    output_format: data.get("output_format"),
    source: data.get("source"),
    segment_seconds: Number(data.get("segment_seconds")),
    monitor: data.get("monitor") === "on",
    interval_seconds: Number(data.get("interval_seconds")),
    auto_start: data.get("auto_start") === "on",
  };
  elements.submit.disabled = true;
  elements.submit.textContent = "正在保存…";
  try {
    await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
    elements.form.reset();
    elements.form.elements.segment_seconds.value = "1800";
    elements.form.elements.interval_seconds.value = "60";
    elements.form.elements.monitor.checked = true;
    elements.form.elements.auto_start.checked = true;
    closeDialog();
    toast("录制任务已创建");
    await refresh({ quiet: true });
  } catch (error) {
    toast(error.message, "error");
  } finally {
    elements.submit.disabled = false;
    elements.submit.textContent = "保存并启动";
  }
}

async function taskAction(action, taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) return;
  if (action === "delete" && !window.confirm(`确定删除“${task.label || task.anchor_name || "该任务"}”？正在录制时会先停止。`)) {
    return;
  }
  try {
    if (action === "delete") {
      await api(`/api/tasks/${taskId}`, { method: "DELETE" });
      toast("任务已删除");
    } else {
      await api(`/api/tasks/${taskId}/${action}`, { method: "POST" });
      toast(action === "start" ? "任务已启动" : "任务已停止");
    }
    await refresh({ quiet: true });
  } catch (error) {
    toast(error.message, "error");
  }
}

document.querySelector("#open-create-button").addEventListener("click", openDialog);
document.querySelectorAll("[data-open-create]").forEach((button) => button.addEventListener("click", openDialog));
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", closeDialog));
document.querySelector("#refresh-button").addEventListener("click", () => refresh());
elements.inspectButton.addEventListener("click", inspectRoom);
elements.form.addEventListener("submit", createTask);
elements.dialog.addEventListener("click", (event) => {
  if (event.target === elements.dialog) closeDialog();
});
elements.taskList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (button) taskAction(button.dataset.action, button.dataset.id);
});
document.querySelectorAll(".filter-button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".filter-button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    render();
  });
});

refresh();
window.setInterval(() => refresh({ quiet: true }), 5000);
