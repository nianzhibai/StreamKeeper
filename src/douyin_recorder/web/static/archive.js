import {
  api,
  archiveStatus,
  bootstrap,
  clearPageError,
  confirmAction,
  escapeHtml,
  formatRelative,
  formatTime,
  icon,
  providerStatus,
  setBusy,
  setHealth,
  setTextIfChanged,
  showPageError,
  toast,
} from "/static/ui.js?v=20260809";

const refreshButton = document.querySelector("#refresh-button");
const runButton = document.querySelector("#cloud-run-button");

let cloud = null;
let loading = false;

function renderProvider(kind, provider) {
  const status = providerStatus(provider, kind);
  const statusNode = document.querySelector(`#${kind}-status`);
  statusNode.className = `pill tone-${status.tone}`;
  statusNode.innerHTML = `<i class="dot"></i>${escapeHtml(status.label)}`;
  setTextIfChanged(document.querySelector(`#${kind}-path`), provider.upload_path || "—");

  const credential = kind === "quark"
    ? provider.credential_configured
    : provider.access_token_configured || provider.refresh_token_configured;
  const credentialNode = document.querySelector(`#${kind}-credential`);
  setTextIfChanged(credentialNode, credential ? "凭据已保存" : "未保存凭据");
  credentialNode.className = `chip${credential ? " is-ok" : ""}`;
}

function renderLastRun(lastRun) {
  const summary = lastRun?.summary;
  setTextIfChanged(document.querySelector("#run-scanned"), summary?.scanned_files ?? "—");
  setTextIfChanged(document.querySelector("#run-uploaded"), summary?.uploaded_copies ?? "—");
  setTextIfChanged(document.querySelector("#run-skipped"), summary?.skipped_files ?? "—");
  setTextIfChanged(document.querySelector("#run-deleted"), summary?.deleted_files ?? "—");
  setTextIfChanged(document.querySelector("#run-failed"), summary?.failed_files ?? "—");
  document.querySelector("#run-failed").closest("div").classList.toggle("is-danger", Boolean(summary?.failed_files));

  const time = document.querySelector("#last-run-time");
  const detail = document.querySelector("#last-run-detail");
  if (!lastRun) {
    setTextIfChanged(time, "暂无执行记录");
    setTextIfChanged(detail, "启用网盘后会在计划时间自动执行");
    return;
  }
  const trigger = lastRun.trigger === "manual" ? "手动触发" : "定时执行";
  setTextIfChanged(time, `${trigger} · ${formatRelative(lastRun.finished_at || lastRun.started_at)}`);
  if (lastRun.error) {
    setTextIfChanged(detail, lastRun.error);
    detail.classList.add("is-bad");
    return;
  }
  detail.classList.remove("is-bad");
  if (summary) {
    setTextIfChanged(
      detail,
      summary.failed_files ? `${summary.failed_files} 个文件处理失败` : "全部处理完成",
    );
  } else {
    setTextIfChanged(detail, lastRun.status === "running" ? "正在扫描本地录像" : "—");
  }
}

function render(value) {
  cloud = value;
  const status = archiveStatus(cloud);
  const badge = document.querySelector("#archive-status");
  badge.className = `pill pill-lg tone-${status.tone}`;
  badge.innerHTML = `<i class="dot"></i>${escapeHtml(status.label)}`;

  runButton.disabled = cloud.running || !cloud.enabled;
  runButton.innerHTML = cloud.running
    ? '<span class="spinner"></span>上传中'
    : `${icon("upload", "ic-sm")}立即归档`;
  runButton.title = cloud.enabled ? "" : "请先在设置中启用网盘";

  setTextIfChanged(
    document.querySelector("#archive-status-time"),
    cloud.running
      ? `开始于 ${formatTime(cloud.last_run?.started_at)}`
      : cloud.last_run
        ? `最近执行 ${formatTime(cloud.last_run.finished_at || cloud.last_run.started_at)}`
        : "尚未执行过归档",
  );
  setTextIfChanged(
    document.querySelector("#next-run-time"),
    cloud.schedule.next_run_at ? formatTime(cloud.schedule.next_run_at) : "—",
  );
  setTextIfChanged(document.querySelector("#schedule-hour"), `每天 ${String(cloud.schedule.hour).padStart(2, "0")}:00`);
  setTextIfChanged(document.querySelector("#stable-age"), `${cloud.schedule.min_age_minutes} 分钟`);

  renderProvider("quark", cloud.quark);
  renderProvider("wopan", cloud.wopan);
  renderLastRun(cloud.last_run);
}

async function load({ quiet = false } = {}) {
  if (loading) return;
  loading = true;
  if (!quiet) setBusy(refreshButton, true);
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
    setBusy(refreshButton, false);
  }
}

async function runArchive() {
  if (!cloud?.enabled || cloud.running) return;
  const proceed = await confirmAction({
    title: "立即执行归档",
    message: "将扫描本地录像并上传到已启用的网盘，上传成功的本地文件会被删除。",
    confirmLabel: "开始归档",
    tone: "warn",
  });
  if (!proceed) return;
  runButton.disabled = true;
  runButton.innerHTML = '<span class="spinner"></span>正在启动';
  try {
    render(await api("/api/cloud/archive/run", { method: "POST" }));
    toast("归档任务已启动", "success");
  } catch (error) {
    toast(error.message, "error");
    await load({ quiet: true });
  }
}

refreshButton?.addEventListener("click", () => load());
runButton?.addEventListener("click", runArchive);
bootstrap(load);
