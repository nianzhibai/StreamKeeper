import {
  api,
  bootstrap,
  clearPageError,
  confirmAction,
  escapeHtml,
  formatBytes,
  formatTime,
  icon,
  paintProgress,
  setBusy,
  setHealth,
  setHtmlIfChanged,
  setTextIfChanged,
  showPageError,
  toast,
  toggle,
} from "/static/ui.js?v=20260809";

const UPLOAD_STAGES = {
  preparing: "准备中",
  uploading: "上传中",
  verifying: "校验中",
};
const UPLOAD_TARGETS = {
  quark: "夸克网盘",
  wopan: "联通云盘",
};

let currentPath = new URLSearchParams(window.location.search).get("path") || "";
let directory = { path: currentPath, entries: [] };
let visible = [];
let searchTerm = "";
let sortMode = "name";
let requestController = null;
let playerInstance = null;
let uploadJobs = new Map();
let uploadTimer = null;
let uploadStream = null;
let uploadStreamWatchdog = 0;
let uploadFallback = false;

const listNode = document.querySelector("#recording-list");
const headNode = document.querySelector(".file-head");
const emptyNode = document.querySelector("#recording-empty");
const searchNode = document.querySelector("#recording-search");
const sortNode = document.querySelector("#recording-sort");
const refreshButton = document.querySelector("#refresh-button");
const uploadAllButton = document.querySelector("#upload-all-button");
const uploadAllLabel = document.querySelector("#upload-all-label");
const uploadQueue = document.querySelector("#upload-queue");
const uploadQueueTitle = document.querySelector("#upload-queue-title");
const uploadQueueDetail = document.querySelector("#upload-queue-detail");
const uploadQueueTrack = document.querySelector("#upload-queue-track");
const uploadQueueCancel = document.querySelector("#upload-queue-cancel");
const playerDialog = document.querySelector("#recording-player-dialog");
const playerContainer = document.querySelector("#recording-player");
const playerLoading = document.querySelector("#recording-player-loading");
const playerError = document.querySelector("#recording-player-error");
const playerLoadingDetail = document.querySelector("#recording-player-loading-detail");
const Artplayer = window.Artplayer;

if (Artplayer) {
  Artplayer.CONTEXTMENU = false;
  Artplayer.FULLSCREEN_WEB_IN_BODY = false;
  Artplayer.RECONNECT_TIME_MAX = 0;
}

function encodePath(path) {
  return path.split("/").map((part) => encodeURIComponent(part)).join("/");
}

function fileUrl(path, { preview = false, download = false } = {}) {
  const kind = preview ? "preview" : "file";
  return `/api/recordings/${kind}/${encodePath(path)}${download ? "?download=true" : ""}`;
}

function renderBreadcrumbs() {
  const breadcrumbs = document.querySelector("#recording-breadcrumbs");
  const parts = currentPath ? currentPath.split("/") : [];
  const nodes = ['<button type="button" data-path="">全部录像</button>'];
  parts.forEach((part, index) => {
    const path = parts.slice(0, index + 1).join("/");
    const current = index === parts.length - 1;
    nodes.push(`<span aria-hidden="true">${icon("chevronRight", "ic-xs")}</span>`);
    nodes.push(`<button type="button" data-path="${escapeHtml(path)}"${current ? ' aria-current="page"' : ""}>${escapeHtml(part)}</button>`);
  });
  breadcrumbs.innerHTML = nodes.join("");
  document.querySelector("#recording-up").disabled = !currentPath;
  document.querySelector("#recording-location").textContent = currentPath ? `/${currentPath}` : "录像根目录";
  setTextIfChanged(uploadAllLabel, currentPath ? "上传本目录" : "全部上传");
  uploadAllButton.title = currentPath
    ? `上传 /${currentPath} 及其子目录下的所有录像`
    : "上传全部录像到网盘";
}

function sortedEntries(entries) {
  const compare = {
    name: (a, b) => a.name.localeCompare(b.name, "zh-CN"),
    modified: (a, b) => new Date(b.modified_at) - new Date(a.modified_at),
    size: (a, b) => (b.size || 0) - (a.size || 0),
  }[sortMode];
  return [...entries].sort((a, b) => {
    const folderFirst = (a.kind !== "directory") - (b.kind !== "directory");
    return folderFirst || compare(a, b);
  });
}

function visibleEntries() {
  const term = searchTerm.trim().toLocaleLowerCase("zh-CN");
  const matched = term
    ? directory.entries.filter((entry) => entry.name.toLocaleLowerCase("zh-CN").includes(term))
    : directory.entries;
  return sortedEntries(matched);
}

function isUploading(job) {
  return job?.status === "queued" || job?.status === "running";
}

function uploadRatio(job) {
  const total = job.size * Math.max(1, job.target_count);
  if (!total) return 0;
  return Math.min(1, (job.target_index * job.size + job.uploaded_bytes) / total);
}

/** Empty until the server has a wide enough sample window to divide by. */
function uploadSpeed(job) {
  return job.speed_bytes_per_second ? `${formatBytes(job.speed_bytes_per_second)}/s` : "";
}

function uploadProgressMarkup(job) {
  if (!job) return "";
  if (job.status === "queued") {
    return `<p class="file-note">${icon("clock", "ic-xs")}排队中，等待其他上传完成</p>`;
  }
  if (job.status === "running") {
    const ratio = uploadRatio(job);
    const target = UPLOAD_TARGETS[job.target] || job.target || "网盘";
    const stage = UPLOAD_STAGES[job.stage] || "上传中";
    const step = job.target_count > 1 ? `（${job.target_index + 1}/${job.target_count}）` : "";
    const caption = `${target}${step} · ${stage}`;
    const clock = [
      `${formatBytes(job.uploaded_bytes)} / ${formatBytes(job.size)}`,
      `${Math.round(ratio * 100)}%`,
      uploadSpeed(job),
    ].filter(Boolean).join(" · ");
    return `
      <div class="progress">
        <span class="progress-text"><b>${escapeHtml(caption)}</b><em>${escapeHtml(clock)}</em></span>
        <span class="progress-track" data-ratio="${ratio.toFixed(4)}" aria-hidden="true"><i></i></span>
      </div>`;
  }
  if (job.status === "failed") {
    return `<p class="file-note is-bad">${icon("alert", "ic-xs")}上传失败：${escapeHtml(job.error || "未知错误")}</p>`;
  }
  if (job.status === "cancelled") {
    return `<p class="file-note">${icon("info", "ic-xs")}${escapeHtml(job.error || "上传已取消")}</p>`;
  }
  return "";
}

function rowActions(entry, job) {
  if (entry.kind === "directory") {
    return `<button class="btn btn-icon btn-sm btn-ghost" type="button" data-action="open" aria-label="打开 ${escapeHtml(entry.name)}">${icon("chevronRight", "ic-sm")}</button>`;
  }
  const uploadAction = isUploading(job)
    ? `<button class="btn btn-icon btn-sm btn-ghost" type="button" data-action="cancel-upload" aria-label="取消上传 ${escapeHtml(entry.name)}" title="取消上传">${icon("close", "ic-sm")}</button>`
    : `<button class="btn btn-icon btn-sm btn-ghost" type="button" data-action="upload"${entry.playable ? "" : " disabled"} aria-label="上传 ${escapeHtml(entry.name)} 到网盘" title="上传到网盘">${icon("upload", "ic-sm")}</button>`;
  return `<button class="btn btn-sm btn-ghost" type="button" data-action="play"${entry.playable ? "" : " disabled"}>${icon("play", "ic-xs")}<span>播放</span></button>
     ${uploadAction}
     <a class="btn btn-icon btn-sm btn-ghost" href="${fileUrl(entry.path, { download: true })}" data-action="download" download aria-label="下载 ${escapeHtml(entry.name)}" title="下载原文件">${icon("download", "ic-sm")}</a>`;
}

function rowTemplate(entry, index) {
  const isFolder = entry.kind === "directory";
  const detail = isFolder ? "文件夹" : `${String(entry.extension || "视频").toUpperCase()} 视频`;
  const job = isFolder ? null : uploadJobs.get(entry.path);
  return `
    <article class="file-row${isFolder ? " is-folder" : ""}${isUploading(job) ? " is-uploading" : ""}" data-entry-index="${index}">
      <button class="file-main" type="button" data-action="${isFolder ? "open" : "play"}"${entry.playable || isFolder ? "" : " disabled"}>
        <span class="file-icon ${isFolder ? "is-folder" : "is-video"}">
          ${icon(isFolder ? "folder" : "video")}${isFolder ? "" : `<b>${escapeHtml(entry.extension || "")}</b>`}
        </span>
        <span class="file-copy">
          <strong>${escapeHtml(entry.name)}</strong>
          <small>${escapeHtml(detail)}</small>
        </span>
      </button>
      <span class="file-size">${isFolder ? "—" : escapeHtml(formatBytes(entry.size))}</span>
      <time class="file-time" datetime="${escapeHtml(entry.modified_at)}">${escapeHtml(formatTime(entry.modified_at))}</time>
      <div class="file-ops">${rowActions(entry, job)}</div>
      <div class="file-progress">${uploadProgressMarkup(job)}</div>
    </article>`;
}

/**
 * Summarises the whole queue above the list. Without it a batch started from the
 * root shows no progress at all, because that view holds only folders.
 */
function renderUploadQueue() {
  const jobs = [...uploadJobs.values()];
  const active = jobs.filter(isUploading);
  toggle(uploadQueue, active.length > 0);
  if (!active.length) return;

  const running = active.find((job) => job.status === "running");
  const done = jobs.filter((job) => job.status === "success").length;
  const total = done + active.length;
  const ratio = total ? (done + (running ? uploadRatio(running) : 0)) / total : 0;
  setTextIfChanged(uploadQueueTitle, `正在上传 ${done + 1}/${total}`);
  setTextIfChanged(
    uploadQueueDetail,
    running
      ? [
          running.name,
          `${UPLOAD_TARGETS[running.target] || running.target || "网盘"} ${Math.round(uploadRatio(running) * 100)}%`,
          uploadSpeed(running),
        ].filter(Boolean).join(" · ")
      : `${active.length} 个文件排队中`,
  );
  uploadQueueTrack.dataset.ratio = ratio.toFixed(4);
  paintProgress(uploadQueue);
}

/** Repaints only the upload parts of each row so a 1s poll never disturbs the rest. */
function renderUploads() {
  listNode.querySelectorAll("[data-entry-index]").forEach((row) => {
    const entry = visible[Number(row.dataset.entryIndex)];
    if (!entry || entry.kind === "directory") return;
    const job = uploadJobs.get(entry.path);
    setHtmlIfChanged(row.querySelector(".file-progress"), uploadProgressMarkup(job));
    setHtmlIfChanged(row.querySelector(".file-ops"), rowActions(entry, job));
    row.classList.toggle("is-uploading", isUploading(job));
  });
  paintProgress(listNode);
  renderUploadQueue();
}

function renderList() {
  const entries = visibleEntries();
  visible = entries;
  const folders = directory.entries.filter((entry) => entry.kind === "directory").length;
  const videos = directory.entries.length - folders;
  const size = directory.entries.reduce((total, entry) => total + (entry.size || 0), 0);
  const summary = [
    folders ? `${folders} 个文件夹` : "",
    `${videos} 个视频`,
    size ? formatBytes(size) : "",
  ].filter(Boolean).join(" · ");
  document.querySelector("#recording-summary").textContent = summary;

  listNode.innerHTML = entries.map(rowTemplate).join("");
  paintProgress(listNode);
  toggle(listNode, entries.length > 0);
  toggle(headNode, entries.length > 0);
  toggle(emptyNode, entries.length === 0);
  if (entries.length === 0) {
    document.querySelector("#recording-empty-title").textContent = searchTerm ? "没有匹配的文件" : "这个目录里还没有录像";
    document.querySelector("#recording-empty-detail").textContent = searchTerm
      ? "换一个关键词再试试"
      : "录制完成后，视频会自动出现在这里";
  }
}

function updateUrl(path, { replace = false } = {}) {
  const url = new URL(window.location.href);
  if (path) url.searchParams.set("path", path);
  else url.searchParams.delete("path");
  window.history[replace ? "replaceState" : "pushState"]({ path }, "", url);
}

async function load({ quiet = false, path = currentPath, history = null } = {}) {
  requestController?.abort();
  requestController = new AbortController();
  if (!quiet) setBusy(refreshButton, true);
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    const value = await api(`/api/recordings${query}`, { signal: requestController.signal });
    const changedDirectory = value.path !== currentPath;
    directory = value;
    currentPath = value.path;
    if (changedDirectory || history) {
      searchTerm = "";
      searchNode.value = "";
    }
    if (history === "push") updateUrl(currentPath);
    if (history === "replace") updateUrl(currentPath, { replace: true });
    renderBreadcrumbs();
    renderList();
    clearPageError();
    setHealth(true);
  } catch (error) {
    if (error.name === "AbortError") return;
    setHealth(false);
    showPageError(`无法读取录像：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    setBusy(refreshButton, false);
  }
}

function navigate(path) {
  if (path === currentPath) return;
  load({ path, history: "push" });
}

function announceFinishedUploads(previous) {
  let removedLocalFile = false;
  uploadJobs.forEach((job, path) => {
    if (!isUploading(previous.get(path)) || isUploading(job)) return;
    if (job.status === "success") {
      removedLocalFile = true;
      toast(`${job.name} 已上传到网盘，本地文件已删除`, "success");
    } else if (job.status === "failed") {
      toast(`${job.name} 上传失败：${job.error || "未知错误"}`, "error");
    } else if (job.status === "cancelled") {
      toast(`${job.name} 上传已取消`);
    }
  });
  return removedLocalFile;
}

function applyJobs(jobs) {
  const previous = uploadJobs;
  uploadJobs = new Map(jobs.map((job) => [job.path, job]));
  const removedLocalFile = announceFinishedUploads(previous);
  syncUploadPolling();
  if (removedLocalFile) return load({ quiet: true });
  renderUploads();
  return Promise.resolve();
}

async function loadUploads() {
  try {
    const value = await api("/api/recordings/uploads");
    await applyJobs(value.jobs);
  } catch (_error) {
    // The directory poll already surfaces connectivity problems.
  }
}

/** Polling exists only for when the event stream cannot get through. */
function syncUploadPolling() {
  const active = uploadFallback && [...uploadJobs.values()].some(isUploading);
  if (active && uploadTimer === null) {
    uploadTimer = window.setInterval(() => {
      if (!document.hidden) loadUploads();
    }, 1000);
  } else if (!active && uploadTimer !== null) {
    window.clearInterval(uploadTimer);
    uploadTimer = null;
  }
}

function fallBackToPolling() {
  uploadStream?.close();
  uploadStream = null;
  window.clearTimeout(uploadStreamWatchdog);
  if (uploadFallback) return;
  uploadFallback = true;
  loadUploads();
}

/**
 * Progress arrives as server-sent events. A proxy that buffers responses leaves
 * the connection open but silent, so a watchdog falls back to polling when the
 * server's opening snapshot never lands.
 */
function connectUploadStream() {
  // One attempt per page load: once polling has taken over, reconnect churn buys
  // nothing that a refresh would not fix.
  if (uploadStream || uploadFallback) return;
  const stream = new EventSource("/api/recordings/uploads/stream");
  uploadStream = stream;
  uploadStreamWatchdog = window.setTimeout(fallBackToPolling, 8000);

  stream.addEventListener("message", (event) => {
    if (uploadStream !== stream) return;
    window.clearTimeout(uploadStreamWatchdog);
    uploadFallback = false;
    applyJobs(JSON.parse(event.data).jobs);
  });
  stream.addEventListener("error", () => {
    // EventSource reconnects on its own, so only a permanently closed stream
    // is worth abandoning; a silent one is the watchdog's job.
    if (uploadStream === stream && stream.readyState === EventSource.CLOSED) fallBackToPolling();
  });
}

async function startUpload(entry) {
  const proceed = await confirmAction({
    title: "上传到网盘",
    message: `将「${entry.name}」上传到已启用的网盘，上传成功后会删除本地文件。`,
    confirmLabel: "开始上传",
    tone: "warn",
  });
  if (!proceed) return;
  try {
    const job = await api("/api/recordings/uploads", {
      method: "POST",
      body: JSON.stringify({ path: entry.path }),
    });
    uploadJobs.set(job.path, job);
    renderUploads();
    syncUploadPolling();
    toast("已加入上传队列", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function cancelUpload(entry) {
  try {
    await api(`/api/recordings/uploads/${encodePath(entry.path)}`, { method: "DELETE" });
  } catch (error) {
    toast(error.message, "error");
    await loadUploads();
  }
}

async function startBatchUpload() {
  const scope = currentPath ? `/${currentPath}` : "全部录像";
  setBusy(uploadAllButton, true);
  uploadAllButton.disabled = true;
  let preview;
  try {
    const query = currentPath ? `?path=${encodeURIComponent(currentPath)}` : "";
    preview = await api(`/api/recordings/uploads/candidates${query}`);
  } catch (error) {
    toast(error.message, "error");
    return;
  } finally {
    setBusy(uploadAllButton, false);
    uploadAllButton.disabled = false;
  }

  if (!preview.count) {
    toast(`${scope} 下没有可上传的录像`, "error");
    return;
  }
  const proceed = await confirmAction({
    title: "上传到网盘",
    message: `将上传 ${scope} 及其子目录下的 ${preview.count} 个录像（共 ${formatBytes(preview.total_size)}），逐个排队执行，上传成功后会删除本地文件。`,
    confirmLabel: `上传 ${preview.count} 个`,
    tone: "warn",
  });
  if (!proceed) return;

  try {
    const value = await api("/api/recordings/uploads/batch", {
      method: "POST",
      body: JSON.stringify({ path: currentPath }),
    });
    value.jobs.forEach((job) => uploadJobs.set(job.path, job));
    renderUploads();
    syncUploadPolling();
    toast(`已加入 ${value.jobs.length} 个上传任务`, "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function cancelAllUploads() {
  const pending = [...uploadJobs.values()].filter(isUploading).length;
  const proceed = await confirmAction({
    title: "取消全部上传",
    message: `将取消 ${pending} 个排队或进行中的上传任务，本地文件保持不变。`,
    confirmLabel: "全部取消",
    tone: "warn",
  });
  if (!proceed) return;
  try {
    await api("/api/recordings/uploads", { method: "DELETE" });
  } catch (error) {
    toast(error.message, "error");
    await loadUploads();
  }
}

function showPlayerMessage(kind) {
  toggle(playerLoading, kind === "loading");
  toggle(playerError, kind === "error");
}

function destroyPlayer() {
  const instance = playerInstance;
  playerInstance = null;
  playerDialog.classList.remove("is-web-fullscreen");
  if (!instance) {
    playerContainer.replaceChildren();
    return;
  }
  if (instance.fullscreenWeb) instance.fullscreenWeb = false;
  instance.destroy();
}

function setPlayerSource(entry) {
  if (!entry) return;
  const remux = entry.playback_mode === "remux";
  playerLoadingDetail.textContent = remux
    ? "首次播放会无损封装为 MP4，完成后可直接拖动进度"
    : "正在加载视频";
  showPlayerMessage("loading");
  destroyPlayer();

  if (!Artplayer) {
    showPlayerMessage("error");
    toast("ArtPlayer 加载失败，请刷新页面重试", "error");
    return;
  }

  try {
    const instance = new Artplayer({
      container: playerContainer,
      url: fileUrl(entry.path, { preview: remux }),
      lang: "zh-cn",
      theme: "var(--brand)",
      autoplay: false,
      autoOrientation: true,
      backdrop: true,
      fullscreen: true,
      fullscreenWeb: true,
      hotkey: false,
      lock: true,
      miniProgressBar: true,
      mutex: true,
      pip: true,
      playbackRate: true,
      aspectRatio: true,
      playsInline: true,
      setting: true,
      moreVideoAttr: {
        preload: "metadata",
      },
    });
    playerInstance = instance;

    const clearPreparingState = () => {
      if (playerInstance === instance) showPlayerMessage(null);
    };
    instance.on("video:loadedmetadata", clearPreparingState);
    instance.on("video:canplay", clearPreparingState);
    instance.on("video:playing", clearPreparingState);
    instance.on("video:error", () => {
      if (playerInstance === instance) showPlayerMessage("error");
    });
    instance.on("fullscreenError", () => {
      toast("浏览器拒绝了全屏请求", "error");
    });
    instance.on("fullscreenWeb", (active) => {
      playerDialog.classList.toggle("is-web-fullscreen", active);
    });
    instance.hotkey.add("Space", () => instance.toggle());
    instance.hotkey.add("ArrowLeft", () => {
      instance.backward = Artplayer.SEEK_STEP;
    });
    instance.hotkey.add("ArrowRight", () => {
      instance.forward = Artplayer.SEEK_STEP;
    });
    instance.hotkey.add("ArrowUp", () => {
      instance.volume += Artplayer.VOLUME_STEP;
    });
    instance.hotkey.add("ArrowDown", () => {
      instance.volume -= Artplayer.VOLUME_STEP;
    });
    instance.hotkey.add("KeyK", () => instance.toggle());
    instance.hotkey.add("KeyM", () => {
      instance.muted = !instance.muted;
    });
    instance.hotkey.add("KeyF", () => {
      instance.fullscreen = !instance.fullscreen;
    });
    instance.play().catch(() => {});
  } catch (_error) {
    playerInstance = null;
    playerContainer.replaceChildren();
    showPlayerMessage("error");
    toast("播放器初始化失败，请刷新页面重试", "error");
  }
}

function openPlayer(entry) {
  if (!entry.playable) return;
  document.querySelector("#recording-player-title").textContent = entry.name;
  document.querySelector("#recording-player-path").textContent = `/${entry.path}`;
  document.querySelector("#recording-player-meta").textContent = [
    String(entry.extension).toUpperCase(),
    formatBytes(entry.size),
    formatTime(entry.modified_at),
  ].join(" · ");
  document.querySelector("#recording-download").href = fileUrl(entry.path, { download: true });
  playerDialog.showModal();
  setPlayerSource(entry);
}

function closePlayer() {
  if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
  destroyPlayer();
  if (playerDialog.open) playerDialog.close();
  showPlayerMessage("loading");
}

refreshButton?.addEventListener("click", () => load());
uploadAllButton?.addEventListener("click", startBatchUpload);
uploadQueueCancel?.addEventListener("click", cancelAllUploads);
document.querySelector("#recording-up")?.addEventListener("click", () => {
  if (!currentPath) return;
  navigate(currentPath.split("/").slice(0, -1).join("/"));
});
document.querySelector("#recording-breadcrumbs")?.addEventListener("click", (event) => {
  const target = event.target.closest("[data-path]");
  if (target) navigate(target.dataset.path || "");
});
searchNode.addEventListener("input", () => {
  searchTerm = searchNode.value;
  renderList();
});
sortNode?.addEventListener("change", () => {
  sortMode = sortNode.value;
  renderList();
});
listNode.addEventListener("click", (event) => {
  const action = event.target.closest("[data-action]");
  const row = event.target.closest("[data-entry-index]");
  if (!action || !row || action.dataset.action === "download") return;
  const entry = visible[Number(row.dataset.entryIndex)];
  if (!entry) return;
  if (action.dataset.action === "open") navigate(entry.path);
  if (action.dataset.action === "play") openPlayer(entry);
  if (action.dataset.action === "upload") startUpload(entry);
  if (action.dataset.action === "cancel-upload") cancelUpload(entry);
});
document.querySelector("#recording-player-close")?.addEventListener("click", closePlayer);
playerDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  if (playerInstance?.fullscreenWeb) {
    playerInstance.fullscreenWeb = false;
    return;
  }
  closePlayer();
});
playerDialog.addEventListener("click", (event) => {
  if (event.target === playerDialog) closePlayer();
});
window.addEventListener("pagehide", () => {
  destroyPlayer();
  uploadStream?.close();
});
window.addEventListener("popstate", () => {
  const path = new URLSearchParams(window.location.search).get("path") || "";
  load({ path, quiet: true });
});
document.addEventListener("keydown", (event) => {
  if (playerDialog.open || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target instanceof Element && event.target.closest("input, select, textarea")) return;
  if (event.key === "/") {
    event.preventDefault();
    searchNode.focus();
  }
});

let firstLoad = true;
bootstrap(async (options = {}) => {
  const history = firstLoad ? "replace" : null;
  firstLoad = false;
  await load({ ...options, history });
  connectUploadStream();
  if (uploadFallback) await loadUploads();
}, { interval: 15000 });
