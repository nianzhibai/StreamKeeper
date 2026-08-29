import {
  api,
  archiveStatus,
  bootstrap,
  clearPageError,
  escapeHtml,
  formatBytes,
  formatRelative,
  formatTime,
  paintProgress,
  providerConfigured,
  providerLogoHtml,
  providerStatus,
  recordingProgressMarkup,
  setHtmlIfChanged,
  setTextIfChanged,
  showPageError,
  statusPill,
  syncProgressClock,
  TASK_STATUS,
  toast,
  toggle,
} from "/static/ui.js?v=20260881";
import { createTaskDialog } from "/static/task-dialog.js?v=20260881";

const MAX_VISIBLE_TASKS = 6;
const STATUS_PRIORITY = { recording: 0, error: 1, checking: 2, queued: 3, waiting: 4, stopped: 5 };

const elements = {
  list: document.querySelector("#active-task-list"),
  empty: document.querySelector("#active-task-empty"),
};

let loading = false;
let activeTasks = [];
let progressTimer = 0;

function taskItem(task) {
  const title = task.label || task.anchor_name || "未命名任务";
  // While recording, the pill and the progress bar already say so; the scheduler's
  // "正在录制" message would only repeat them.
  const recording = task.status === "recording";
  const checkedAt = task.last_checked_at ? `最近检查 ${formatRelative(task.last_checked_at)}` : "等待首次检查";
  const detail = task.status_message && task.status_message !== TASK_STATUS[task.status]?.label
    ? task.status_message
    : checkedAt;
  return `
    <a class="mini-item" href="/tasks">
      <span class="mini-body">
        <span class="mini-top">
          <strong>${escapeHtml(title)}</strong>
          ${statusPill(task.status)}
        </span>
        ${recording ? "" : `<small>${escapeHtml(detail)}</small>`}
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

function archiveProviderItem(provider) {
  const state = providerStatus(provider, provider.name);
  return `
    <li>
      ${providerLogoHtml(provider.name)}
      <strong>${escapeHtml(provider.label)}</strong>
      <span class="mini-state tone-${escapeHtml(state.tone)}">${escapeHtml(state.label)}</span>
    </li>`;
}

function renderArchive(cloud) {
  // The badge already carries the outcome of the last run (执行成功 / 部分失败 / …),
  // so the card only needs to add when it ran and which drives are set up.
  const status = archiveStatus(cloud);
  const badge = document.querySelector("#overview-archive-status");
  badge.className = `pill tone-${status.tone}`;
  badge.innerHTML = `<i class="dot"></i>${escapeHtml(status.label)}`;

  const lastRun = cloud.last_run;
  setTextIfChanged(
    document.querySelector("#overview-last-run"),
    lastRun ? formatTime(lastRun.finished_at || lastRun.started_at) : "—",
  );

  // Only drives that actually hold credentials; the badge says 未配置 when none do.
  // `providers` is the generic list, so the legacy per-provider fields are not read here.
  const configured = (cloud.providers || []).filter(providerConfigured);
  const list = document.querySelector("#overview-archive-providers");
  setHtmlIfChanged(list, configured.map(archiveProviderItem).join(""));
  toggle(list, configured.length > 0);
}

function renderSystem(system) {
  setTextIfChanged(document.querySelector("#stat-disk"), `${system.free_space_gb} GB`);
  setTextIfChanged(
    document.querySelector("#system-recording-limit"),
    `${system.recording_tasks} / ${system.max_concurrent_recordings}`,
  );
  setTextIfChanged(document.querySelector("#system-ffmpeg"), system.ffmpeg_available ? "可用" : "未安装");
  setTextIfChanged(document.querySelector("#system-local-usage"), formatBytes(system.local_usage_bytes));
  const directory = document.querySelector("#system-directory");
  setTextIfChanged(directory, system.recordings_dir);
  directory.title = system.recordings_dir;

  document.querySelector("#system-ffmpeg").classList.toggle("is-bad", !system.ffmpeg_available);
}

async function load({ quiet = false } = {}) {
  if (loading) return;
  loading = true;
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
    renderSystem(system);
    clearPageError();
  } catch (error) {
    showPageError(`无法刷新数据：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    loading = false;
  }
}

const taskDialog = createTaskDialog({
  onSaved: () => load({ quiet: true }),
});

document.querySelector("[data-open-create]").addEventListener("click", taskDialog.openCreate);

bootstrap((options) => Promise.all([load(options), taskDialog.prepare()]));
