import {
  api,
  archiveStatus,
  bootstrap,
  clearPageError,
  escapeHtml,
  formatTime,
  providerStatus,
  setHealth,
  setHtmlIfChanged,
  setTextIfChanged,
  showPageError,
  toast,
} from "/static/ui.js?v=20260721-ui2";

const statusLabels = {
  stopped: "已停止",
  waiting: "值守中",
  checking: "检查中",
  queued: "排队中",
  recording: "录制中",
  error: "异常",
};

let loading = false;

function taskItem(task) {
  const title = task.label || task.anchor_name || "未命名任务";
  return `
    <a class="compact-task" href="/tasks">
      <span class="avatar avatar-small">${escapeHtml([...title][0] || "录")}</span>
      <span class="compact-task-main">
        <strong>${escapeHtml(title)}</strong>
        <small>${escapeHtml(task.status_message || formatTime(task.last_checked_at))}</small>
      </span>
      <span class="status-pill tone-${escapeHtml(task.status)}"><i></i>${escapeHtml(statusLabels[task.status] || task.status)}</span>
    </a>`;
}

function renderTasks(tasks) {
  const active = tasks
    .filter((task) => task.enabled || task.status === "recording" || task.status === "error")
    .sort((a, b) => {
      const priority = { recording: 0, error: 1, checking: 2, queued: 3, waiting: 4 };
      return (priority[a.status] ?? 9) - (priority[b.status] ?? 9);
    })
    .slice(0, 5);
  const list = document.querySelector("#active-task-list");
  const empty = document.querySelector("#active-task-empty");
  setHtmlIfChanged(list, active.map(taskItem).join(""));
  list.classList.toggle("hidden", active.length === 0);
  empty.classList.toggle("hidden", active.length !== 0);
}

function renderArchive(cloud) {
  const status = archiveStatus(cloud);
  const badge = document.querySelector("#overview-archive-status");
  badge.className = `status-pill tone-${status.tone}`;
  badge.innerHTML = `<i></i>${escapeHtml(status.label)}`;

  const lastRun = cloud.last_run;
  setTextIfChanged(
    document.querySelector("#overview-archive-time"),
    cloud.running
      ? "任务正在后台执行"
      : lastRun
        ? formatTime(lastRun.finished_at || lastRun.started_at)
        : "暂无执行记录",
  );
  setTextIfChanged(
    document.querySelector("#overview-next-run"),
    cloud.schedule.next_run_at ? formatTime(cloud.schedule.next_run_at) : "—",
  );

  for (const kind of ["quark", "wopan"]) {
    const provider = cloud[kind];
    const providerState = providerStatus(provider, kind);
    const badgeNode = document.querySelector(`#overview-${kind}-status`);
    setTextIfChanged(badgeNode, providerState.label);
    badgeNode.className = `mini-status tone-${providerState.tone}`;
  }
}

function renderSystem(system) {
  setTextIfChanged(document.querySelector("#stat-disk"), `${system.free_space_gb} GB`);
  setTextIfChanged(
    document.querySelector("#system-recording-limit"),
    `${system.recording_tasks} / ${system.max_concurrent_recordings}`,
  );
  setTextIfChanged(document.querySelector("#system-ffmpeg"), system.ffmpeg_available ? "可用" : "不可用");
  setTextIfChanged(document.querySelector("#system-node"), system.node_available ? "可用" : "不可用");
}

async function load({ quiet = false } = {}) {
  if (loading) return;
  loading = true;
  const refreshButton = document.querySelector("#refresh-button");
  if (!quiet) refreshButton?.classList.add("is-loading");
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
    renderTasks(tasks);
    renderArchive(cloud);
    renderSystem(system);
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法刷新数据：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    loading = false;
    refreshButton?.classList.remove("is-loading");
  }
}

document.querySelector("#refresh-button")?.addEventListener("click", () => load());
bootstrap(load);
