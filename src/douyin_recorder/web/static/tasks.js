import {
  api,
  bootstrap,
  clearPageError,
  escapeHtml,
  formatTime,
  setHealth,
  setHtmlIfChanged,
  showPageError,
  toast,
} from "/static/ui.js?v=20260721-ui2";

const state = {
  tasks: [],
  filter: "all",
  loading: false,
  editingTaskId: null,
};

const elements = {
  list: document.querySelector("#task-list"),
  empty: document.querySelector("#task-empty"),
  emptyTitle: document.querySelector("#task-empty-title"),
  dialog: document.querySelector("#task-dialog"),
  dialogTitle: document.querySelector("#dialog-title"),
  form: document.querySelector("#task-form"),
  submit: document.querySelector("#task-submit"),
  autoStartField: document.querySelector("#auto-start-field"),
  inspectButton: document.querySelector("#inspect-button"),
  inspectResult: document.querySelector("#inspect-result"),
  refreshButton: document.querySelector("#refresh-button"),
};

const statusLabels = {
  stopped: "已停止",
  waiting: "值守中",
  checking: "检查中",
  queued: "排队中",
  recording: "录制中",
  error: "异常",
};

const qualityLabels = {
  OD: "原画",
  UHD: "超清",
  HD: "高清",
  SD: "标清",
  LD: "流畅",
};

function matchesFilter(task) {
  if (state.filter === "recording") return task.status === "recording";
  if (state.filter === "enabled") return task.enabled && task.status !== "recording";
  if (state.filter === "error") return task.status === "error";
  return true;
}

function renderTask(task) {
  const title = task.label || task.anchor_name || "未命名任务";
  const status = statusLabels[task.status] || task.status;
  const source = task.source === "auto" ? "自动源" : task.source.toUpperCase();
  const specs = [qualityLabels[task.quality] || task.quality, task.output_format.toUpperCase(), source];
  return `
    <article class="task-row ${escapeHtml(task.status)}">
      <div class="task-main">
        <span class="avatar">${escapeHtml([...title][0] || "录")}</span>
        <span class="task-name">
          <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
          <a href="${escapeHtml(task.url)}" target="_blank" rel="noreferrer" title="${escapeHtml(task.url)}">${escapeHtml(task.anchor_name || task.url)}</a>
        </span>
      </div>
      <div class="task-state">
        <span class="status-pill tone-${escapeHtml(task.status)}"><i></i>${escapeHtml(status)}</span>
        <small title="${escapeHtml(task.status_message || "")}">${escapeHtml(task.status_message || "等待操作")}</small>
      </div>
      <div class="task-specs">${specs.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
      <time class="task-time" datetime="${escapeHtml(task.last_checked_at || "")}">${escapeHtml(formatTime(task.last_checked_at))}</time>
      <div class="task-actions">
        <button class="small-button" type="button" data-action="edit" data-id="${escapeHtml(task.id)}">编辑</button>
        ${task.enabled
          ? `<button class="small-button danger-hover" type="button" data-action="stop" data-id="${escapeHtml(task.id)}">停止</button>`
          : `<button class="small-button success-hover" type="button" data-action="start" data-id="${escapeHtml(task.id)}">启动</button>`}
        <button class="icon-button subtle danger-hover" type="button" data-action="delete" data-id="${escapeHtml(task.id)}" aria-label="删除 ${escapeHtml(title)}" title="删除">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5" /></svg>
        </button>
      </div>
    </article>`;
}

function updateFilterCounts() {
  const counts = {
    all: state.tasks.length,
    recording: state.tasks.filter((task) => task.status === "recording").length,
    enabled: state.tasks.filter((task) => task.enabled && task.status !== "recording").length,
    error: state.tasks.filter((task) => task.status === "error").length,
  };
  document.querySelectorAll("[data-filter]").forEach((button) => {
    const count = button.querySelector("span");
    if (count) count.textContent = counts[button.dataset.filter];
  });
}

function render() {
  const visible = state.tasks.filter(matchesFilter);
  setHtmlIfChanged(elements.list, visible.map(renderTask).join(""));
  elements.list.classList.toggle("hidden", visible.length === 0);
  elements.empty.classList.toggle("hidden", visible.length !== 0);
  elements.emptyTitle.textContent = state.tasks.length === 0 ? "还没有录制任务" : "没有符合条件的任务";
  updateFilterCounts();
}

async function load({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  if (!quiet) elements.refreshButton?.classList.add("is-loading");
  try {
    state.tasks = await api("/api/tasks");
    render();
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法读取任务：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    state.loading = false;
    elements.refreshButton?.classList.remove("is-loading");
  }
}

function showTaskDialog() {
  elements.dialog.showModal();
  window.setTimeout(() => document.querySelector("#task-url").focus(), 50);
}

function resetDialog() {
  elements.form.reset();
  elements.form.elements.segment_count.setCustomValidity("");
  elements.inspectResult.className = "inspect-result hidden";
  elements.inspectResult.textContent = "";
  const advanced = elements.form.querySelector("details");
  if (advanced) advanced.open = false;
}

function openCreateDialog() {
  state.editingTaskId = null;
  resetDialog();
  elements.dialogTitle.textContent = "新建任务";
  elements.submit.textContent = "创建任务";
  elements.autoStartField.classList.remove("hidden");
  showTaskDialog();
}

function openEditDialog(task) {
  state.editingTaskId = task.id;
  resetDialog();
  const form = elements.form.elements;
  form.url.value = task.url;
  form.label.value = task.label || "";
  form.quality.value = task.quality;
  form.output_format.value = task.output_format;
  form.source.value = task.source;
  form.segment_seconds.value = String(task.segment_seconds);
  form.segment_count.value = String(task.segment_count);
  form.monitor.checked = task.monitor;
  form.interval_seconds.value = String(task.interval_seconds);
  elements.dialogTitle.textContent = "编辑任务";
  elements.submit.textContent = "保存";
  elements.autoStartField.classList.add("hidden");
  showTaskDialog();
}

function closeDialog() {
  elements.dialog.close();
  state.editingTaskId = null;
}

async function inspectRoom() {
  const urlInput = document.querySelector("#task-url");
  const url = urlInput.value.trim();
  if (!url) {
    urlInput.reportValidity();
    return;
  }
  elements.inspectButton.disabled = true;
  elements.inspectButton.textContent = "检测中";
  elements.inspectResult.className = "inspect-result is-loading";
  elements.inspectResult.textContent = "正在检测直播间…";
  try {
    const result = await api("/api/inspect", {
      method: "POST",
      body: JSON.stringify({ url, quality: elements.form.elements.quality.value }),
    });
    const sources = [result.has_flv && "FLV", result.has_hls && "HLS"].filter(Boolean).join(" / ");
    elements.inspectResult.className = "inspect-result is-success";
    elements.inspectResult.textContent = [
      result.anchor_name || "未知主播",
      result.is_live ? "直播中" : "未开播",
      sources,
    ].filter(Boolean).join(" · ");
  } catch (error) {
    elements.inspectResult.className = "inspect-result is-error";
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
      && !window.confirm("保存后会重新开始录制，继续吗？")) return;
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
    toast(isEditing ? "任务已更新" : "任务已创建");
    await load({ quiet: true });
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
  if (action === "delete" && !window.confirm(`删除“${task.label || task.anchor_name || "该任务"}”？`)) return;
  try {
    if (action === "delete") {
      await api(`/api/tasks/${taskId}`, { method: "DELETE" });
      toast("任务已删除");
    } else {
      await api(`/api/tasks/${taskId}/${action}`, { method: "POST" });
      toast(action === "start" ? "任务已启动" : "任务已停止");
    }
    await load({ quiet: true });
  } catch (error) {
    toast(error.message, "error");
  }
}

document.querySelectorAll("[data-open-create]").forEach((button) => button.addEventListener("click", openCreateDialog));
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", closeDialog));
elements.refreshButton?.addEventListener("click", () => load());
elements.inspectButton.addEventListener("click", inspectRoom);
elements.form.addEventListener("submit", submitTask);
elements.form.elements.segment_seconds.addEventListener("input", validateSegmentSettings);
elements.form.elements.segment_count.addEventListener("input", validateSegmentSettings);
elements.dialog.addEventListener("click", (event) => {
  if (event.target === elements.dialog) closeDialog();
});
elements.dialog.addEventListener("close", () => {
  state.editingTaskId = null;
});
elements.list.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (button) taskAction(button.dataset.action, button.dataset.id);
});
document.querySelectorAll("[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-filter]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    render();
  });
});

bootstrap(async (options) => {
  await load(options);
  const query = new URLSearchParams(window.location.search);
  if (query.get("create") === "1" && !elements.dialog.open) {
    openCreateDialog();
    window.history.replaceState({}, "", "/tasks");
  }
});
