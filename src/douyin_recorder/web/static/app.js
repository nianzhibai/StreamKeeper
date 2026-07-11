const state = {
  tasks: [],
  system: null,
  session: null,
  filter: "all",
  loading: false,
  editingTaskId: null,
};

const elements = {
  taskList: document.querySelector("#task-list"),
  emptyState: document.querySelector("#empty-state"),
  errorBanner: document.querySelector("#error-banner"),
  dialog: document.querySelector("#create-dialog"),
  dialogTitle: document.querySelector("#dialog-title"),
  form: document.querySelector("#create-form"),
  submit: document.querySelector("#create-submit"),
  autoStartField: document.querySelector("#auto-start-field"),
  inspectButton: document.querySelector("#inspect-button"),
  inspectResult: document.querySelector("#inspect-result"),
  health: document.querySelector("#server-health"),
  accountName: document.querySelector("#account-name"),
  logoutButton: document.querySelector("#logout-button"),
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
  const method = String(options.method || "GET").toUpperCase();
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method) && state.session?.csrf_token) {
    headers.set("X-CSRF-Token", state.session.csrf_token);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 401) {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.replace(`/login?next=${encodeURIComponent(next)}`);
    throw new Error("登录已过期，正在返回登录页");
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

async function logout() {
  elements.logoutButton.disabled = true;
  elements.logoutButton.textContent = "退出中";
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    if (!String(error.message).includes("登录已过期")) toast(error.message, "error");
  } finally {
    window.location.replace("/login");
  }
}

async function bootstrap() {
  try {
    state.session = await api("/api/auth/session");
    elements.accountName.textContent = state.session.username;
    await refresh();
    window.setInterval(() => refresh({ quiet: true }), 5000);
  } catch (error) {
    if (!String(error.message).includes("登录已过期")) {
      elements.errorBanner.textContent = `初始化失败：${error.message}`;
      elements.errorBanner.classList.remove("hidden");
    }
  }
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
  const segment = task.segment_seconds
    ? `${task.segment_seconds}s${task.segment_count ? ` × ${task.segment_count}` : ""}`
    : "不分段";
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
          <button class="task-action edit" type="button" data-action="edit" data-id="${task.id}">编辑</button>
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

function showTaskDialog() {
  elements.dialog.showModal();
  window.setTimeout(() => document.querySelector("#task-url").focus(), 50);
}

function openCreateDialog() {
  state.editingTaskId = null;
  elements.form.reset();
  elements.form.elements.segment_count.setCustomValidity("");
  elements.dialogTitle.textContent = "新建任务";
  elements.submit.textContent = "创建任务";
  elements.autoStartField.classList.remove("hidden");
  elements.inspectResult.className = "inspect-result hidden";
  showTaskDialog();
}

function openEditDialog(task) {
  state.editingTaskId = task.id;
  elements.form.reset();
  elements.form.elements.url.value = task.url;
  elements.form.elements.label.value = task.label || "";
  elements.form.elements.quality.value = task.quality;
  elements.form.elements.output_format.value = task.output_format;
  elements.form.elements.source.value = task.source;
  elements.form.elements.segment_seconds.value = String(task.segment_seconds);
  elements.form.elements.segment_count.value = String(task.segment_count);
  elements.form.elements.monitor.checked = task.monitor;
  elements.form.elements.interval_seconds.value = String(task.interval_seconds);
  elements.form.elements.segment_count.setCustomValidity("");
  elements.dialogTitle.textContent = "编辑任务";
  elements.submit.textContent = "保存";
  elements.autoStartField.classList.add("hidden");
  elements.inspectResult.className = "inspect-result hidden";
  showTaskDialog();
}

function closeDialog() {
  elements.dialog.close();
  state.editingTaskId = null;
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
  elements.inspectResult.textContent = "检测中…";
  try {
    const result = await api("/api/inspect", {
      method: "POST",
      body: JSON.stringify({ url, quality }),
    });
    elements.inspectResult.className = "inspect-result";
    const sources = [result.has_flv && "FLV", result.has_hls && "HLS"].filter(Boolean).join(" / ");
    elements.inspectResult.textContent = [
      result.anchor_name || "未知主播",
      result.is_live ? "直播中" : "未开播",
      sources,
    ].filter(Boolean).join(" · ");
  } catch (error) {
    elements.inspectResult.className = "inspect-result error";
    elements.inspectResult.textContent = `检测失败：${error.message}`;
  } finally {
    elements.inspectButton.disabled = false;
    elements.inspectButton.textContent = "检测";
  }
}

function validateSegmentSettings() {
  const segmentSeconds = Number(elements.form.elements.segment_seconds.value);
  const segmentCount = Number(elements.form.elements.segment_count.value);
  elements.form.elements.segment_count.setCustomValidity(
    segmentCount > 0 && segmentSeconds <= 0 ? "设置段数时，分段时长必须大于 0" : "",
  );
}

async function submitTask(event) {
  event.preventDefault();
  validateSegmentSettings();
  if (!elements.form.reportValidity()) return;
  const editingTaskId = state.editingTaskId;
  const isEditing = Boolean(editingTaskId);
  const editingTask = isEditing ? state.tasks.find((task) => task.id === editingTaskId) : null;
  const data = new FormData(elements.form);
  const payload = {
    url: String(data.get("url") || "").trim(),
    label: String(data.get("label") || "").trim() || null,
    quality: data.get("quality"),
    output_format: data.get("output_format"),
    source: data.get("source"),
    segment_seconds: Number(data.get("segment_seconds")),
    segment_count: Number(data.get("segment_count")),
    monitor: data.get("monitor") === "on",
    interval_seconds: Number(data.get("interval_seconds")),
  };
  const restartsRecording = editingTask && (
    payload.url !== editingTask.url
    || payload.quality !== editingTask.quality
    || payload.output_format !== editingTask.output_format
    || payload.source !== editingTask.source
    || payload.segment_seconds !== editingTask.segment_seconds
    || payload.segment_count !== editingTask.segment_count
    || payload.monitor !== editingTask.monitor
    || payload.interval_seconds !== editingTask.interval_seconds
  );
  if (editingTask?.status === "recording" && restartsRecording
      && !window.confirm("保存后将重新开始录制，继续？")) {
    return;
  }
  if (!isEditing) payload.auto_start = data.get("auto_start") === "on";
  elements.submit.disabled = true;
  elements.submit.textContent = isEditing ? "保存中…" : "创建中…";
  try {
    if (isEditing) {
      await api(`/api/tasks/${editingTaskId}`, { method: "PATCH", body: JSON.stringify(payload) });
    } else {
      await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
    }
    closeDialog();
    toast(isEditing ? "配置已更新" : "任务已创建");
    await refresh({ quiet: true });
  } catch (error) {
    toast(error.message, "error");
  } finally {
    elements.submit.disabled = false;
    elements.submit.textContent = isEditing ? "保存" : "创建任务";
  }
}

async function taskAction(action, taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) return;
  if (action === "edit") {
    openEditDialog(task);
    return;
  }
  if (action === "delete" && !window.confirm(`删除“${task.label || task.anchor_name || "该任务"}”？`)) {
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

document.querySelector("#open-create-button").addEventListener("click", openCreateDialog);
document.querySelectorAll("[data-open-create]").forEach((button) => button.addEventListener("click", openCreateDialog));
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", closeDialog));
document.querySelector("#refresh-button").addEventListener("click", () => refresh());
elements.logoutButton.addEventListener("click", logout);
elements.inspectButton.addEventListener("click", inspectRoom);
elements.form.addEventListener("submit", submitTask);
elements.form.elements.segment_seconds.addEventListener("input", validateSegmentSettings);
elements.form.elements.segment_count.addEventListener("input", validateSegmentSettings);
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

bootstrap();
