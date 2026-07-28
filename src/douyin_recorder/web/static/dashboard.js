import {
  api,
  archiveStatus,
  bootstrap,
  clearPageError,
  escapeHtml,
  formatRelative,
  formatTime,
  paintProgress,
  providerStatus,
  recordingProgressMarkup,
  setBusy,
  setHealth,
  setHtmlIfChanged,
  setTextIfChanged,
  showPageError,
  statusPill,
  syncProgressClock,
  TASK_STATUS,
  toast,
  toggle,
} from "/static/ui.js?v=20260728";

const MAX_VISIBLE_TASKS = 6;
const STATUS_PRIORITY = { recording: 0, error: 1, checking: 2, queued: 3, waiting: 4, stopped: 5 };

const elements = {
  list: document.querySelector("#active-task-list"),
  empty: document.querySelector("#active-task-empty"),
  refresh: document.querySelector("#refresh-button"),
};

let loading = false;
let activeTasks = [];
let progressTimer = 0;

function taskItem(task) {
  const title = task.label || task.anchor_name || "未命名任务";
  const checkedAt = task.last_checked_at ? `最近检查 ${formatRelative(task.last_checked_at)}` : "等待首次检查";
  const detail = task.status_message && task.status_message !== TASK_STATUS[task.status]?.label
    ? task.status_message
    : checkedAt;
  return `
    <a class="mini-item${task.status === "recording" ? " is-recording" : ""}" href="/tasks">
      <span class="avatar avatar-sm" aria-hidden="true">${escapeHtml([...title][0] || "录")}</span>
      <span class="mini-body">
        <span class="mini-top">
          <strong>${escapeHtml(title)}</strong>
          ${statusPill(task.status)}
        </span>
        <small>${escapeHtml(detail)}</small>
        ${recordingProgressMarkup(task)}
      </span>
    </a>`;
}

function ensureProgressTicker() {
  const hasRecording = activeTasks.some((task) => task.status === "recording");
  if (hasRecording && !progressTimer) {
    progressTimer = window.setInterval(() => {
      if (!activeTasks.some((task) => task.status === "recording")) {
        window.clearInterval(progressTimer);
        progressTimer = 0;
        return;
      }
      renderTasks(activeTasks, { tick: true });
    }, 1000);
  }
  if (!hasRecording && progressTimer) {
    window.clearInterval(progressTimer);
    progressTimer = 0;
  }
}

function renderTasks(tasks, { tick = false } = {}) {
  if (!tick) activeTasks = tasks;
  const active = activeTasks
    .filter((task) => task.enabled || task.status === "recording" || task.status === "error")
    .sort((a, b) => (STATUS_PRIORITY[a.status] ?? 9) - (STATUS_PRIORITY[b.status] ?? 9))
    .slice(0, MAX_VISIBLE_TASKS);
  setHtmlIfChanged(elements.list, active.map(taskItem).join(""));
  paintProgress(elements.list);
  toggle(elements.list, active.length > 0);
  toggle(elements.empty, active.length === 0);
  ensureProgressTicker();
}

function renderArchive(cloud) {
  const status = archiveStatus(cloud);
  const badge = document.querySelector("#overview-archive-status");
  badge.className = `pill tone-${status.tone}`;
  badge.innerHTML = `<i class="dot"></i>${escapeHtml(status.label)}`;

  const lastRun = cloud.last_run;
  setTextIfChanged(
    document.querySelector("#overview-archive-time"),
    cloud.running
      ? "任务正在后台执行"
      : lastRun
        ? `最近执行 ${formatRelative(lastRun.finished_at || lastRun.started_at)}`
        : "暂无执行记录",
  );
  setTextIfChanged(
    document.querySelector("#overview-next-run"),
    cloud.schedule.next_run_at ? formatTime(cloud.schedule.next_run_at) : "—",
  );

  for (const kind of ["quark", "wopan"]) {
    const state = providerStatus(cloud[kind], kind);
    const node = document.querySelector(`#overview-${kind}-status`);
    setTextIfChanged(node, state.label);
    node.className = `mini-state tone-${state.tone}`;
  }
}

function renderSystem(system, tasks) {
  const enabled = tasks.filter((task) => task.enabled).length;
  setTextIfChanged(document.querySelector("#stat-disk"), `${system.free_space_gb} GB`);
  setTextIfChanged(
    document.querySelector("#stat-recording-note"),
    `并发上限 ${system.max_concurrent_recordings}`,
  );
  setTextIfChanged(document.querySelector("#stat-total-note"), `${enabled} 个已启用`);
  setTextIfChanged(
    document.querySelector("#system-recording-limit"),
    `${system.recording_tasks} / ${system.max_concurrent_recordings}`,
  );
  setTextIfChanged(document.querySelector("#system-ffmpeg"), system.ffmpeg_available ? "可用" : "未安装");
  setTextIfChanged(document.querySelector("#system-node"), system.node_available ? "可用" : "未安装");
  const directory = document.querySelector("#system-directory");
  setTextIfChanged(directory, system.recordings_dir);
  directory.title = system.recordings_dir;

  document.querySelector("#system-ffmpeg").classList.toggle("is-bad", !system.ffmpeg_available);
  document.querySelector("#system-node").classList.toggle("is-bad", !system.node_available);
}

async function load({ quiet = false } = {}) {
  if (loading) return;
  loading = true;
  if (!quiet) setBusy(elements.refresh, true);
  try {
    const [tasks, system, cloud] = await Promise.all([
      api("/api/tasks"),
      api("/api/system"),
      api("/api/cloud/archive"),
    ]);
    setTextIfChanged(document.querySelector("#stat-total"), String(tasks.length));
    setTextIfChanged(
      document.querySelector("#stat-recording"),
      String(tasks.filter((task) => task.status === "recording").length),
    );
    setTextIfChanged(
      document.querySelector("#stat-waiting"),
      String(tasks.filter((task) => task.enabled && task.status !== "recording").length),
    );
    renderTasks(syncProgressClock(tasks));
    renderArchive(cloud);
    renderSystem(system, tasks);
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法刷新数据：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    loading = false;
    setBusy(elements.refresh, false);
  }
}

elements.refresh?.addEventListener("click", () => load());
bootstrap(load);
