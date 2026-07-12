import {
  api,
  archiveStatus,
  bootstrap,
  clearPageError,
  escapeHtml,
  formatTime,
  providerStatus,
  setHealth,
  showPageError,
  toast,
} from "/static/ui.js?v=20260712-ui1";

let cloud = null;
let loading = false;

function renderProvider(kind, provider) {
  const status = providerStatus(provider, kind);
  const statusNode = document.querySelector(`#${kind}-status`);
  statusNode.className = `status-pill tone-${status.tone}`;
  statusNode.innerHTML = `<i></i>${escapeHtml(status.label)}`;
  document.querySelector(`#${kind}-path`).textContent = provider.upload_path || "—";
  const credential = kind === "quark"
    ? provider.credential_configured
    : provider.access_token_configured || provider.refresh_token_configured;
  document.querySelector(`#${kind}-credential`).textContent = credential ? "凭据已保存" : "未保存凭据";
}

function renderLastRun(lastRun) {
  const summary = lastRun?.summary;
  document.querySelector("#run-scanned").textContent = summary?.scanned_files ?? "—";
  document.querySelector("#run-uploaded").textContent = summary?.uploaded_copies ?? "—";
  document.querySelector("#run-deleted").textContent = summary?.deleted_files ?? "—";
  document.querySelector("#run-failed").textContent = summary?.failed_files ?? "—";

  const time = document.querySelector("#last-run-time");
  const detail = document.querySelector("#last-run-detail");
  if (!lastRun) {
    time.textContent = "暂无执行记录";
    detail.textContent = "—";
    return;
  }
  const trigger = lastRun.trigger === "manual" ? "手动" : "定时";
  time.textContent = `${trigger} · ${formatTime(lastRun.finished_at || lastRun.started_at)}`;
  if (lastRun.error) {
    detail.textContent = lastRun.error;
  } else if (summary) {
    detail.textContent = summary.failed_files ? `${summary.failed_files} 个文件处理失败` : "全部处理完成";
  } else {
    detail.textContent = lastRun.status === "running" ? "正在扫描本地录像" : "—";
  }
}

function render(value) {
  cloud = value;
  const status = archiveStatus(cloud);
  const badge = document.querySelector("#archive-status");
  badge.className = `status-pill status-large tone-${status.tone}`;
  badge.innerHTML = `<i></i>${escapeHtml(status.label)}`;

  const runButton = document.querySelector("#cloud-run-button");
  runButton.disabled = cloud.running || !cloud.enabled;
  runButton.innerHTML = cloud.running
    ? '<span class="button-spinner"></span>上传中'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M5 20h14" /></svg>立即归档';
  runButton.title = cloud.enabled ? "" : "请先在设置中启用网盘";

  document.querySelector("#archive-status-time").textContent = cloud.running
    ? `开始于 ${formatTime(cloud.last_run?.started_at)}`
    : cloud.last_run
      ? `最近执行 ${formatTime(cloud.last_run.finished_at || cloud.last_run.started_at)}`
      : "尚未执行";
  document.querySelector("#next-run-time").textContent = cloud.schedule.next_run_at
    ? formatTime(cloud.schedule.next_run_at)
    : "—";
  document.querySelector("#schedule-hour").textContent = `${String(cloud.schedule.hour).padStart(2, "0")}:00`;
  document.querySelector("#stable-age").textContent = `${cloud.schedule.min_age_minutes} 分钟`;

  renderProvider("quark", cloud.quark);
  renderProvider("wopan", cloud.wopan);
  renderLastRun(cloud.last_run);
}

async function load({ quiet = false } = {}) {
  if (loading) return;
  loading = true;
  document.querySelector("#refresh-button")?.classList.add("is-loading");
  try {
    render(await api("/api/cloud/archive"));
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法读取归档状态：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    loading = false;
    document.querySelector("#refresh-button")?.classList.remove("is-loading");
  }
}

async function runArchive() {
  if (!cloud?.enabled || cloud.running) return;
  if (!window.confirm("现在扫描并上传录像？上传成功的本地文件将被删除。")) return;
  const button = document.querySelector("#cloud-run-button");
  button.disabled = true;
  button.textContent = "正在启动…";
  try {
    render(await api("/api/cloud/archive/run", { method: "POST" }));
    toast("归档任务已启动");
  } catch (error) {
    toast(error.message, "error");
    await load({ quiet: true });
  }
}

document.querySelector("#refresh-button")?.addEventListener("click", () => load());
document.querySelector("#cloud-run-button")?.addEventListener("click", runArchive);
bootstrap(load);
