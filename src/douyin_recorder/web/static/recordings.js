import {
  api,
  bootstrap,
  clearPageError,
  escapeHtml,
  setHealth,
  showPageError,
  toast,
} from "/static/ui.js?v=20260721-ui3";

const icons = {
  folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 7.5h6l1.7 2H20v8.75A1.75 1.75 0 0 1 18.25 20H5.25a1.75 1.75 0 0 1-1.75-1.75z" /></svg>',
  video: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2" /><path d="m10 9 5 3-5 3z" /></svg>',
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="m10.5 9 4.5 3-4.5 3z" /></svg>',
  pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M10 9v6M14 9v6" /></svg>',
  download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M5 20h14" /></svg>',
  chevron: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>',
  fullscreen: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4H4v5M15 4h5v5M20 15v5h-5M4 15v5h5" /></svg>',
};

let currentPath = new URLSearchParams(window.location.search).get("path") || "";
let directory = { path: currentPath, entries: [] };
let searchTerm = "";
let requestController = null;
let activeEntry = null;
let closingPlayer = false;
let seeking = false;

const listNode = document.querySelector("#recording-list");
const emptyNode = document.querySelector("#recording-empty");
const searchNode = document.querySelector("#recording-search");
const playerDialog = document.querySelector("#recording-player-dialog");
const playerShell = document.querySelector(".player-shell");
const player = document.querySelector("#recording-player");
const playerLoading = document.querySelector("#recording-player-loading");
const playerError = document.querySelector("#recording-player-error");
const playerLoadingDetail = document.querySelector("#recording-player-loading-detail");
const playToggle = document.querySelector("#recording-play-toggle");
const seekBar = document.querySelector("#recording-player-seek");
const timeLabel = document.querySelector("#recording-player-time");
const fullscreenButton = document.querySelector("#recording-fullscreen");

function encodePath(path) {
  return path.split("/").map((part) => encodeURIComponent(part)).join("/");
}

function fileUrl(path, { preview = false, download = false } = {}) {
  const kind = preview ? "preview" : "file";
  return `/api/recordings/${kind}/${encodePath(path)}${download ? "?download=true" : ""}`;
}

function formatBytes(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value;
  let unit = -1;
  do {
    size /= 1024;
    unit += 1;
  } while (size >= 1024 && unit < units.length - 1);
  return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[unit]}`;
}

function formatModified(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function renderBreadcrumbs() {
  const breadcrumbs = document.querySelector("#recording-breadcrumbs");
  const parts = currentPath ? currentPath.split("/") : [];
  const nodes = ['<button type="button" data-path="">全部录像</button>'];
  parts.forEach((part, index) => {
    const path = parts.slice(0, index + 1).join("/");
    nodes.push(`<span>${icons.chevron}</span><button type="button" data-path="${escapeHtml(path)}"${index === parts.length - 1 ? ' aria-current="page"' : ""}>${escapeHtml(part)}</button>`);
  });
  breadcrumbs.innerHTML = nodes.join("");
  document.querySelector("#recording-up").disabled = !currentPath;
  document.querySelector("#recording-location").textContent = currentPath || "录像根目录";
}

function visibleEntries() {
  const term = searchTerm.trim().toLocaleLowerCase("zh-CN");
  if (!term) return directory.entries;
  return directory.entries.filter((entry) => entry.name.toLocaleLowerCase("zh-CN").includes(term));
}

function rowTemplate(entry, index) {
  const isFolder = entry.kind === "directory";
  const icon = isFolder ? icons.folder : icons.video;
  const detail = isFolder ? "文件夹" : `${String(entry.extension || "视频").toUpperCase()} 视频`;
  const action = isFolder
    ? `<button class="icon-button subtle file-open-icon" type="button" data-action="open" aria-label="打开 ${escapeHtml(entry.name)}">${icons.chevron}</button>`
    : `<button class="small-button file-play" type="button" data-action="play"${entry.playable ? "" : " disabled"}>${icons.play}<span>播放</span></button><a class="icon-button subtle" href="${fileUrl(entry.path, { download: true })}" data-action="download" download aria-label="下载 ${escapeHtml(entry.name)}" title="下载原文件">${icons.download}</a>`;
  return `
    <article class="file-row${isFolder ? " is-folder" : ""}" data-entry-index="${index}">
      <button class="file-main" type="button" data-action="${isFolder ? "open" : "play"}"${entry.playable || isFolder ? "" : " disabled"}>
        <span class="file-icon ${isFolder ? "folder" : "video"}">${icon}${isFolder ? "" : `<b>${escapeHtml(entry.extension || "")}</b>`}</span>
        <span class="file-copy"><strong>${escapeHtml(entry.name)}</strong><small>${escapeHtml(detail)}</small></span>
      </button>
      <span class="file-size">${isFolder ? "—" : formatBytes(entry.size)}</span>
      <time class="file-modified" datetime="${escapeHtml(entry.modified_at)}">${formatModified(entry.modified_at)}</time>
      <div class="file-actions">${action}</div>
    </article>`;
}

function renderList() {
  const entries = visibleEntries();
  const folders = directory.entries.filter((entry) => entry.kind === "directory").length;
  const videos = directory.entries.length - folders;
  const size = directory.entries.reduce((total, entry) => total + (entry.size || 0), 0);
  const sizeText = size ? ` · ${formatBytes(size)}` : "";
  document.querySelector("#recording-summary").textContent = `${folders} 个文件夹 · ${videos} 个视频${sizeText}`;

  listNode.innerHTML = entries.map(rowTemplate).join("");
  listNode.classList.toggle("hidden", entries.length === 0);
  emptyNode.classList.toggle("hidden", entries.length !== 0);
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
  const refreshButton = document.querySelector("#refresh-button");
  if (!quiet) refreshButton?.classList.add("is-loading");
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
    refreshButton?.classList.remove("is-loading");
  }
}

function navigate(path) {
  if (path === currentPath) return;
  load({ path, history: "push" });
}

function showPlayerMessage(kind) {
  playerLoading.classList.toggle("hidden", kind !== "loading" && kind !== "buffering");
  playerLoading.classList.toggle("player-buffering", kind === "buffering");
  playerError.classList.toggle("hidden", kind !== "error");
}

function formatClock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function renderPlayToggle() {
  const playing = !player.paused && !player.ended;
  playToggle.innerHTML = playing ? icons.pause : icons.play;
  playToggle.setAttribute("aria-label", playing ? "暂停" : "播放");
  playToggle.setAttribute("aria-pressed", String(playing));
}

function syncSeekBar() {
  const duration = Number.isFinite(player.duration) ? player.duration : 0;
  seekBar.max = duration > 0 ? String(duration) : "0";
  seekBar.disabled = !(duration > 0);
  if (!seeking) seekBar.value = String(player.currentTime || 0);
  timeLabel.textContent = `${formatClock(player.currentTime || 0)} / ${formatClock(duration)}`;
}

function resetPlayer() {
  player.pause();
  player.removeAttribute("src");
  player.load();
  seeking = false;
  seekBar.value = "0";
  seekBar.max = "0";
  seekBar.disabled = true;
  timeLabel.textContent = "0:00 / 0:00";
  renderPlayToggle();
}

function togglePlayback() {
  if (player.paused || player.ended) {
    player.play().catch(() => toast("浏览器阻止了播放，请再试一次", "error"));
  } else {
    player.pause();
  }
}

function requestPlayerFullscreen() {
  const target = playerShell || player;
  try {
    if (document.fullscreenElement) {
      document.exitFullscreen?.();
      return;
    }
    if (target.requestFullscreen) {
      target.requestFullscreen().catch(() => toast("浏览器拒绝了全屏请求", "error"));
      return;
    }
    if (player.webkitEnterFullscreen) {
      player.webkitEnterFullscreen();
      return;
    }
    toast("当前浏览器不支持视频全屏", "error");
  } catch (_error) {
    toast("无法进入全屏，请检查浏览器权限", "error");
  }
}

function setPlayerSource(entry) {
  if (!entry) return;
  const remux = entry.playback_mode === "remux";
  playerLoadingDetail.textContent = remux
    ? "首次播放会无损封装为 MP4，完成后可直接拖动进度"
    : "正在加载视频";
  showPlayerMessage("loading");
  resetPlayer();
  player.controls = false;
  player.src = fileUrl(entry.path, { preview: remux });
  player.load();
  player.play().catch(renderPlayToggle);
}

function openPlayer(entry) {
  if (!entry.playable) return;
  activeEntry = entry;
  document.querySelector("#recording-player-title").textContent = entry.name;
  document.querySelector("#recording-player-path").textContent = entry.path;
  document.querySelector("#recording-player-meta").textContent = `${String(entry.extension).toUpperCase()} · ${formatBytes(entry.size)} · ${formatModified(entry.modified_at)}`;
  document.querySelector("#recording-download").href = fileUrl(entry.path, { download: true });
  playerDialog.showModal();
  setPlayerSource(entry);
}

function closePlayer() {
  closingPlayer = true;
  if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
  resetPlayer();
  activeEntry = null;
  if (playerDialog.open) playerDialog.close();
  showPlayerMessage("loading");
  window.setTimeout(() => {
    closingPlayer = false;
  }, 0);
}

document.querySelector("#refresh-button")?.addEventListener("click", () => load());
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
listNode.addEventListener("click", (event) => {
  const action = event.target.closest("[data-action]");
  const row = event.target.closest("[data-entry-index]");
  if (!action || !row || action.dataset.action === "download") return;
  const entry = visibleEntries()[Number(row.dataset.entryIndex)];
  if (!entry) return;
  if (action.dataset.action === "open") navigate(entry.path);
  if (action.dataset.action === "play") openPlayer(entry);
});
document.querySelector("#recording-player-close")?.addEventListener("click", closePlayer);
playToggle.addEventListener("click", togglePlayback);
fullscreenButton.addEventListener("click", requestPlayerFullscreen);
seekBar.addEventListener("pointerdown", () => {
  seeking = true;
});
seekBar.addEventListener("input", () => {
  timeLabel.textContent = `${formatClock(Number(seekBar.value) || 0)} / ${formatClock(player.duration || 0)}`;
});
seekBar.addEventListener("change", () => {
  player.currentTime = Number(seekBar.value) || 0;
  seeking = false;
  syncSeekBar();
});
seekBar.addEventListener("pointerup", () => {
  if (!seeking) return;
  player.currentTime = Number(seekBar.value) || 0;
  seeking = false;
  syncSeekBar();
});
playerDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closePlayer();
});
playerDialog.addEventListener("click", (event) => {
  if (event.target === playerDialog) closePlayer();
});
function clearPlayerLoading() {
  if (!playerError.classList.contains("hidden")) return;
  showPlayerMessage(null);
}

player.addEventListener("loadedmetadata", () => {
  clearPlayerLoading();
  syncSeekBar();
});
player.addEventListener("loadeddata", clearPlayerLoading);
player.addEventListener("canplay", () => {
  clearPlayerLoading();
  syncSeekBar();
});
player.addEventListener("playing", clearPlayerLoading);
player.addEventListener("timeupdate", syncSeekBar);
player.addEventListener("durationchange", syncSeekBar);
player.addEventListener("play", renderPlayToggle);
player.addEventListener("pause", renderPlayToggle);
player.addEventListener("ended", () => {
  renderPlayToggle();
  syncSeekBar();
});
player.addEventListener("waiting", () => {
  if (!player.paused && playerError.classList.contains("hidden")) {
    showPlayerMessage("buffering");
  }
});
player.addEventListener("error", () => {
  if (closingPlayer || !activeEntry) return;
  showPlayerMessage("error");
});
player.addEventListener("click", togglePlayback);
window.addEventListener("popstate", () => {
  const path = new URLSearchParams(window.location.search).get("path") || "";
  load({ path, quiet: true });
});

let firstLoad = true;
bootstrap((options = {}) => {
  const history = firstLoad ? "replace" : null;
  firstLoad = false;
  return load({ ...options, history });
}, { interval: 15000 });
