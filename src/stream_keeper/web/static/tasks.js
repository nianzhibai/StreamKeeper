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
  setHtmlIfChanged,
  setTextIfChanged,
  showPageError,
  SOURCE_LABELS,
  statusPill,
  syncProgressClock,
  TASK_STATUS,
  toast,
  toggle,
} from "/static/ui.js?v=20260881";
import { createTaskDialog } from "/static/task-dialog.js?v=20260881";

const state = {
  tasks: [],
  filter: "all",
  search: "",
  loading: false,
  progressTimer: 0,
};

const elements = {
  list: document.querySelector("#task-list"),
  head: document.querySelector(".task-head"),
  empty: document.querySelector("#task-empty"),
  emptyTitle: document.querySelector("#task-empty-title"),
  emptyDetail: document.querySelector("#task-empty-detail"),
  search: document.querySelector("#task-search"),
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
  // 录制中 is carried by the progress bar and the red spine, 已停止 is the resting
  // state; the rest (值守中/检查中/排队中/异常) still need their pill to be readable.
  const hideStatusPill = task.status === "stopped" || (task.status === "recording" && Boolean(progress));
  return `
    <article class="task" data-status="${escapeHtml(task.status)}">
      <div class="task-main">
        <span class="task-name">
          <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
          <a class="task-link" href="${escapeHtml(task.url)}" target="_blank" rel="noreferrer" title="${escapeHtml(task.url)}">
            <span>${escapeHtml(subtitle)}</span>
          </a>
        </span>
      </div>
      <div class="task-state">
        ${hideStatusPill ? "" : statusPill(task.status)}
        ${progress || (message ? `<small title="${escapeHtml(message)}">${escapeHtml(message)}</small>` : "")}
      </div>
      <div class="task-tags">${taskTags(task)}</div>
      <time class="task-time" datetime="${escapeHtml(task.last_checked_at || "")}" title="${escapeHtml(formatTime(task.last_checked_at, { seconds: true }))}">
        ${escapeHtml(formatRelative(task.last_checked_at))}
      </time>
      <div class="task-ops">
        <button class="btn btn-sm btn-ghost" type="button" data-action="edit" data-id="${escapeHtml(task.id)}">${icon("edit", "ic-xs")}编辑</button>
        ${task.enabled
          ? `<button class="btn btn-sm btn-ghost" type="button" data-action="stop" data-id="${escapeHtml(task.id)}">${icon("stop", "ic-xs")}停止</button>`
          : `<button class="btn btn-sm btn-ghost is-positive" type="button" data-action="start" data-id="${escapeHtml(task.id)}">${icon("play", "ic-xs")}启动</button>`}
        <a class="btn btn-sm btn-ghost" href="/logs?task=${encodeURIComponent(task.id)}" aria-label="查看 ${escapeHtml(title)} 的运行日志" title="查看这个任务的运行日志">${icon("logs", "ic-xs")}日志</a>
        <button class="btn btn-sm btn-ghost is-negative" type="button" data-action="delete" data-id="${escapeHtml(task.id)}" aria-label="删除 ${escapeHtml(title)}">
          ${icon("trash", "ic-xs")}删除
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
  // The title alone carries the first-run case; the hint below it only earns its
  // place when a filter or search is what hid everything.
  const filtered = state.tasks.length > 0;
  setTextIfChanged(elements.emptyTitle, filtered ? "没有符合条件的任务" : "还没有录制任务");
  setTextIfChanged(elements.emptyDetail, filtered ? "换一个筛选条件或清空搜索关键词再试试" : "");
  toggle(elements.emptyDetail, filtered);
  updateFilterCounts();
  ensureProgressTicker();
}

async function load({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  try {
    const tasks = await api("/api/tasks");
    state.tasks = syncProgressClock(tasks);
    render();
    clearPageError();
  } catch (error) {
    showPageError(`无法读取任务：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    state.loading = false;
  }
}

const taskDialog = createTaskDialog({
  onSaved: () => load({ quiet: true }),
});

async function taskAction(action, taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) return;
  if (action === "edit") {
    taskDialog.openEdit(task);
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

document.querySelectorAll("[data-open-create]").forEach((button) => button.addEventListener("click", taskDialog.openCreate));
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
  if (typing || taskDialog.isOpen() || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "/") {
    event.preventDefault();
    elements.search?.focus();
  }
  if (event.key === "n") {
    event.preventDefault();
    taskDialog.openCreate();
  }
});

bootstrap((options) => Promise.all([load(options), taskDialog.prepare()]));
