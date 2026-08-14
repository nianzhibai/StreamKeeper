import {
  api,
  bootstrap,
  clearPageError,
  confirmAction,
  escapeHtml,
  formatRelative,
  formatTime,
  icon,
  paintProgress,
  QUALITY_LABELS,
  recordingProgressMarkup,
  setBusy,
  setHealth,
  setHtmlIfChanged,
  setTextIfChanged,
  showPageError,
  SOURCE_LABELS,
  statusPill,
  syncProgressClock,
  TASK_STATUS,
  toast,
  toggle,
} from "/static/ui.js?v=20260814";

const state = {
  tasks: [],
  filter: "all",
  search: "",
  loading: false,
  editingTaskId: null,
  progressTimer: 0,
};

const elements = {
  list: document.querySelector("#task-list"),
  head: document.querySelector(".task-head"),
  empty: document.querySelector("#task-empty"),
  emptyTitle: document.querySelector("#task-empty-title"),
  emptyDetail: document.querySelector("#task-empty-detail"),
  search: document.querySelector("#task-search"),
  dialog: document.querySelector("#task-dialog"),
  dialogTitle: document.querySelector("#dialog-title"),
  form: document.querySelector("#task-form"),
  submit: document.querySelector("#task-submit"),
  autoStartField: document.querySelector("#auto-start-field"),
  inspectButton: document.querySelector("#inspect-button"),
  inspectResult: document.querySelector("#inspect-result"),
  refresh: document.querySelector("#refresh-button"),
};

function taskTitle(task) {
  return task.label || task.anchor_name || "未命名任务";
}

function matchesFilter(task) {
  if (state.filter === "recording") return task.status === "recording";
  if (state.filter === "enabled") return task.enabled && task.status !== "recording";
  if (state.filter === "error") return task.status === "error";
  return true;
}

function matchesSearch(task) {
  const term = state.search.trim().toLocaleLowerCase("zh-CN");
  if (!term) return true;
  return [task.label, task.anchor_name, task.live_title, task.url]
    .filter(Boolean)
    .some((value) => String(value).toLocaleLowerCase("zh-CN").includes(term));
}

function platformLabel(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    if (["live.douyin.com", "v.douyin.com", "www.douyin.com"].includes(host)) return "抖音";
    if (["live.bilibili.com", "b23.tv", "www.b23.tv"].includes(host)) return "哔哩哔哩";
    if (["live.kuaishou.com", "v.kuaishou.com", "www.v.kuaishou.com"].includes(host)) return "快手";
  } catch (_error) {
    // Stored task URLs have already passed server validation; keep the task
    // usable if legacy data contains something the browser URL parser rejects.
  }
  return "直播";
}

function taskTags(task) {
  const tags = [
    platformLabel(task.url),
    QUALITY_LABELS[task.quality] || task.quality,
    task.output_format.toUpperCase(),
    SOURCE_LABELS[task.source] || task.source.toUpperCase(),
  ];
  if (task.segment_seconds > 0) {
    const minutes = Math.round(task.segment_seconds / 60);
    tags.push(task.segment_count > 0 ? `${minutes} 分 × ${task.segment_count}` : `${minutes} 分分段`);
  }
  return tags.map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("");
}

function renderTask(task) {
  const title = taskTitle(task);
  const subtitle = task.anchor_name && task.label ? `${task.anchor_name} · ${task.url}` : task.url;
  const progress = recordingProgressMarkup(task);
  const statusLabel = TASK_STATUS[task.status]?.label;
  const message = task.status_message && task.status_message !== statusLabel
    ? task.status_message
    : "";
  return `
    <article class="task" data-status="${escapeHtml(task.status)}">
      <div class="task-main">
        <span class="avatar" aria-hidden="true">${escapeHtml([...title][0] || "录")}</span>
        <span class="task-name">
          <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
          <a class="task-link" href="${escapeHtml(task.url)}" target="_blank" rel="noreferrer" title="${escapeHtml(task.url)}">
            ${icon("link", "ic-xs")}<span>${escapeHtml(subtitle)}</span>
          </a>
        </span>
      </div>
      <div class="task-state">
        ${statusPill(task.status)}
        ${progress || (message ? `<small title="${escapeHtml(message)}">${escapeHtml(message)}</small>` : "")}
      </div>
      <div class="task-tags">${taskTags(task)}</div>
      <time class="task-time" datetime="${escapeHtml(task.last_checked_at || "")}" title="${escapeHtml(formatTime(task.last_checked_at, { seconds: true }))}">
        ${escapeHtml(formatRelative(task.last_checked_at))}
      </time>
      <div class="task-ops">
        <a class="btn btn-icon btn-sm btn-ghost" href="/logs?task=${encodeURIComponent(task.id)}" aria-label="查看 ${escapeHtml(title)} 的运行日志" title="查看这个任务的运行日志">${icon("logs", "ic-sm")}</a>
        <button class="btn btn-sm btn-ghost" type="button" data-action="edit" data-id="${escapeHtml(task.id)}">${icon("edit", "ic-xs")}编辑</button>
        ${task.enabled
          ? `<button class="btn btn-sm btn-ghost" type="button" data-action="stop" data-id="${escapeHtml(task.id)}">${icon("stop", "ic-xs")}停止</button>`
          : `<button class="btn btn-sm btn-ghost is-positive" type="button" data-action="start" data-id="${escapeHtml(task.id)}">${icon("play", "ic-xs")}启动</button>`}
        <button class="btn btn-icon btn-sm btn-ghost is-negative" type="button" data-action="delete" data-id="${escapeHtml(task.id)}" aria-label="删除 ${escapeHtml(title)}" title="删除">
          ${icon("trash", "ic-sm")}
        </button>
      </div>
    </article>`;
}

function ensureProgressTicker() {
  const hasRecording = state.tasks.some((task) => task.status === "recording");
  if (hasRecording && !state.progressTimer) {
    state.progressTimer = window.setInterval(() => {
      if (!state.tasks.some((task) => task.status === "recording")) {
        window.clearInterval(state.progressTimer);
        state.progressTimer = 0;
        return;
      }
      render();
    }, 1000);
  }
  if (!hasRecording && state.progressTimer) {
    window.clearInterval(state.progressTimer);
    state.progressTimer = 0;
  }
}

function updateFilterCounts() {
  const counts = {
    all: state.tasks.length,
    recording: state.tasks.filter((task) => task.status === "recording").length,
    enabled: state.tasks.filter((task) => task.enabled && task.status !== "recording").length,
    error: state.tasks.filter((task) => task.status === "error").length,
  };
  document.querySelectorAll("[data-filter]").forEach((button) => {
    const badge = button.querySelector("span");
    if (badge) setTextIfChanged(badge, counts[button.dataset.filter]);
    button.classList.toggle("is-empty", !counts[button.dataset.filter]);
  });
}

function render() {
  const visible = state.tasks.filter((task) => matchesFilter(task) && matchesSearch(task));
  setHtmlIfChanged(elements.list, visible.map(renderTask).join(""));
  paintProgress(elements.list);
  toggle(elements.list, visible.length > 0);
  toggle(elements.head, visible.length > 0);
  toggle(elements.empty, visible.length === 0);
  if (state.tasks.length === 0) {
    setTextIfChanged(elements.emptyTitle, "还没有录制任务");
    setTextIfChanged(elements.emptyDetail, "粘贴直播间链接或分享文案，创建第一个任务");
  } else {
    setTextIfChanged(elements.emptyTitle, "没有符合条件的任务");
    setTextIfChanged(elements.emptyDetail, "换一个筛选条件或清空搜索关键词再试试");
  }
  updateFilterCounts();
  ensureProgressTicker();
}

async function load({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  if (!quiet) setBusy(elements.refresh, true);
  try {
    state.tasks = syncProgressClock(await api("/api/tasks"));
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
    setBusy(elements.refresh, false);
  }
}

function showTaskDialog() {
  elements.dialog.showModal();
  window.setTimeout(() => document.querySelector("#task-url").focus(), 60);
}

function resetDialog() {
  elements.form.reset();
  elements.form.elements.segment_count.setCustomValidity("");
  elements.inspectResult.className = "inspect hidden";
  elements.inspectResult.innerHTML = "";
  elements.form.querySelectorAll("details").forEach((node) => {
    node.open = false;
  });
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
  elements.submit.textContent = "保存更改";
  elements.autoStartField.classList.add("hidden");
  showTaskDialog();
}

function closeDialog() {
  elements.dialog.close();
  state.editingTaskId = null;
}

function renderInspectResult({ tone, glyph, title, detail }) {
  elements.inspectResult.className = `inspect is-${tone}`;
  elements.inspectResult.innerHTML = `
    <span class="inspect-icon">${icon(glyph, "ic-sm")}</span>
    <span class="inspect-copy"><strong></strong><small></small></span>`;
  elements.inspectResult.querySelector("strong").textContent = title;
  elements.inspectResult.querySelector("small").textContent = detail;
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
  renderInspectResult({ tone: "loading", glyph: "clock", title: "正在检测直播间", detail: "识别平台并读取直播状态…" });
  try {
    const result = await api("/api/inspect", {
      method: "POST",
      body: JSON.stringify({ url, quality: elements.form.elements.quality.value }),
    });
    const sources = [result.has_flv && "FLV", result.has_hls && "HLS"].filter(Boolean).join(" / ");
    renderInspectResult({
      tone: "success",
      glyph: result.is_live ? "record" : "checkCircle",
      title: result.anchor_name || "未知主播",
      detail: [result.platform, result.is_live ? "直播中" : "当前未开播", result.title, sources && `可用源 ${sources}`]
        .filter(Boolean)
        .join(" · "),
    });
  } catch (error) {
    renderInspectResult({ tone: "error", glyph: "alert", title: "检测失败", detail: error.message });
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
  if (editingTask?.status === "recording" && restartsRecording) {
    const proceed = await confirmAction({
      title: "保存后将重新开始录制",
      message: `“${taskTitle(editingTask)}”正在录制中，修改录制参数会中断当前片段并立即重新开始。`,
      confirmLabel: "保存并重启",
      tone: "warn",
    });
    if (!proceed) return;
  }
  if (!isEditing) payload.auto_start = data.get("auto_start") === "on";

  elements.submit.disabled = true;
  const originalLabel = isEditing ? "保存更改" : "创建任务";
  elements.submit.textContent = isEditing ? "保存中…" : "创建中…";
  try {
    if (isEditing) {
      await api(`/api/tasks/${editingTaskId}`, { method: "PATCH", body: JSON.stringify(payload) });
    } else {
      await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
    }
    closeDialog();
    toast(isEditing ? "任务已更新" : "任务已创建", "success");
    await load({ quiet: true });
  } catch (error) {
    toast(error.message, "error");
  } finally {
    elements.submit.disabled = false;
    elements.submit.textContent = originalLabel;
  }
}

async function taskAction(action, taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) return;
  if (action === "edit") {
    openEditDialog(task);
    return;
  }
  if (action === "delete") {
    const proceed = await confirmAction({
      title: "删除录制任务",
      message: `将删除“${taskTitle(task)}”并停止值守。已保存的录像文件不会被删除。`,
      confirmLabel: "删除任务",
    });
    if (!proceed) return;
  }
  try {
    if (action === "delete") {
      await api(`/api/tasks/${taskId}`, { method: "DELETE" });
      toast("任务已删除", "success");
    } else {
      await api(`/api/tasks/${taskId}/${action}`, { method: "POST" });
      toast(action === "start" ? "任务已启动" : "任务已停止", "success");
    }
    await load({ quiet: true });
  } catch (error) {
    toast(error.message, "error");
  }
}

document.querySelectorAll("[data-open-create]").forEach((button) => button.addEventListener("click", openCreateDialog));
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", closeDialog));
elements.refresh?.addEventListener("click", () => load());
elements.inspectButton.addEventListener("click", inspectRoom);
elements.form.addEventListener("submit", submitTask);
elements.form.elements.segment_seconds.addEventListener("input", validateSegmentSettings);
elements.form.elements.segment_count.addEventListener("input", validateSegmentSettings);
document.querySelector("#task-url").addEventListener("paste", () => {
  window.setTimeout(() => {
    if (document.querySelector("#task-url").value.trim()) inspectRoom();
  }, 120);
});
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
elements.search?.addEventListener("input", () => {
  state.search = elements.search.value;
  render();
});
document.querySelectorAll("[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-filter]").forEach((item) => item.classList.remove("is-active"));
    button.classList.add("is-active");
    state.filter = button.dataset.filter;
    render();
  });
});
document.addEventListener("keydown", (event) => {
  const typing = event.target instanceof Element && event.target.closest("input, select, textarea");
  if (typing || elements.dialog.open || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "/") {
    event.preventDefault();
    elements.search?.focus();
  }
  if (event.key === "n") {
    event.preventDefault();
    openCreateDialog();
  }
});

bootstrap(async (options) => {
  await load(options);
  const query = new URLSearchParams(window.location.search);
  if (query.get("create") === "1" && !elements.dialog.open) {
    openCreateDialog();
    window.history.replaceState({}, "", "/tasks");
  }
});
