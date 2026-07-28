import {
  api,
  bootstrap,
  clearPageError,
  escapeHtml,
  formatBytes,
  formatTime,
  icon,
  setBusy,
  setHealth,
  showPageError,
  toast,
  toggle,
} from "/static/ui.js?v=20260728";

let currentPath = new URLSearchParams(window.location.search).get("path") || "";
let directory = { path: currentPath, entries: [] };
let searchTerm = "";
let sortMode = "name";
let requestController = null;
let activeEntry = null;
let closingPlayer = false;
let seeking = false;

const listNode = document.querySelector("#recording-list");
const headNode = document.querySelector(".file-head");
const emptyNode = document.querySelector("#recording-empty");
const searchNode = document.querySelector("#recording-search");
const sortNode = document.querySelector("#recording-sort");
const refreshButton = document.querySelector("#refresh-button");
const playerDialog = document.querySelector("#recording-player-dialog");
const playerShell = document.querySelector(".player-shell");
const player = document.querySelector("#recording-player");
const playerLoading = document.querySelector("#recording-player-loading");
const playerError = document.querySelector("#recording-player-error");
const playerLoadingDetail = document.querySelector("#recording-player-loading-detail");
const playToggle = document.querySelector("#recording-play-toggle");
const seekBar = document.querySelector("#recording-player-seek");
const timeLabel = document.querySelector("#recording-player-time");
const volumeButton = document.querySelector("#recording-volume");
const fullscreenButton = document.querySelector("#recording-fullscreen");

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

function rowTemplate(entry, index) {
  const isFolder = entry.kind === "directory";
  const detail = isFolder ? "文件夹" : `${String(entry.extension || "视频").toUpperCase()} 视频`;
  const actions = isFolder
    ? `<button class="btn btn-icon btn-sm btn-ghost" type="button" data-action="open" aria-label="打开 ${escapeHtml(entry.name)}">${icon("chevronRight", "ic-sm")}</button>`
    : `<button class="btn btn-sm btn-ghost" type="button" data-action="play"${entry.playable ? "" : " disabled"}>${icon("play", "ic-xs")}<span>播放</span></button>
       <a class="btn btn-icon btn-sm btn-ghost" href="${fileUrl(entry.path, { download: true })}" data-action="download" download aria-label="下载 ${escapeHtml(entry.name)}" title="下载原文件">${icon("download", "ic-sm")}</a>`;
  return `
    <article class="file-row${isFolder ? " is-folder" : ""}" data-entry-index="${index}">
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
      <div class="file-ops">${actions}</div>
    </article>`;
}

function renderList() {
  const entries = visibleEntries();
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

function showPlayerMessage(kind) {
  toggle(playerLoading, kind === "loading" || kind === "buffering");
  playerLoading.classList.toggle("is-buffering", kind === "buffering");
  toggle(playerError, kind === "error");
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
  playToggle.innerHTML = icon(playing ? "pause" : "play");
  playToggle.setAttribute("aria-label", playing ? "暂停" : "播放");
  playToggle.setAttribute("aria-pressed", String(playing));
}

function renderFullscreenButton() {
  const active = Boolean(document.fullscreenElement);
  fullscreenButton.innerHTML = icon(active ? "collapse" : "expand");
  fullscreenButton.setAttribute("aria-label", active ? "退出全屏" : "全屏");
}

function renderVolumeButton() {
  const muted = player.muted || player.volume === 0;
  volumeButton.innerHTML = icon(muted ? "volumeOff" : "volume");
  volumeButton.setAttribute("aria-label", muted ? "取消静音" : "静音");
}

function paintSeekBar() {
  const max = Number(seekBar.max) || 0;
  const ratio = max > 0 ? Math.min(1, Number(seekBar.value) / max) : 0;
  seekBar.style.setProperty("--seek-progress", `${(ratio * 100).toFixed(2)}%`);
}

function syncSeekBar() {
  const duration = Number.isFinite(player.duration) ? player.duration : 0;
  seekBar.max = duration > 0 ? String(duration) : "0";
  seekBar.disabled = !(duration > 0);
  if (!seeking) seekBar.value = String(player.currentTime || 0);
  timeLabel.textContent = `${formatClock(player.currentTime || 0)} / ${formatClock(duration)}`;
  paintSeekBar();
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
  paintSeekBar();
  renderPlayToggle();
}

function togglePlayback() {
  if (player.paused || player.ended) {
    player.play().catch(() => toast("浏览器阻止了播放，请再试一次", "error"));
  } else {
    player.pause();
  }
}

function skip(seconds) {
  if (!Number.isFinite(player.duration) || player.duration <= 0) return;
  player.currentTime = Math.min(player.duration, Math.max(0, player.currentTime + seconds));
  syncSeekBar();
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
  document.querySelector("#recording-player-path").textContent = `/${entry.path}`;
  document.querySelector("#recording-player-meta").textContent = [
    String(entry.extension).toUpperCase(),
    formatBytes(entry.size),
    formatTime(entry.modified_at),
  ].join(" · ");
  document.querySelector("#recording-download").href = fileUrl(entry.path, { download: true });
  playerDialog.showModal();
  playToggle.focus();
  renderVolumeButton();
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

refreshButton?.addEventListener("click", () => load());
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
  const entry = visibleEntries()[Number(row.dataset.entryIndex)];
  if (!entry) return;
  if (action.dataset.action === "open") navigate(entry.path);
  if (action.dataset.action === "play") openPlayer(entry);
});
document.querySelector("#recording-player-close")?.addEventListener("click", closePlayer);
playToggle.addEventListener("click", togglePlayback);
volumeButton.addEventListener("click", () => {
  player.muted = !player.muted;
  renderVolumeButton();
});
fullscreenButton.addEventListener("click", requestPlayerFullscreen);
seekBar.addEventListener("pointerdown", () => {
  seeking = true;
});
seekBar.addEventListener("input", () => {
  timeLabel.textContent = `${formatClock(Number(seekBar.value) || 0)} / ${formatClock(player.duration || 0)}`;
  paintSeekBar();
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
playerDialog.addEventListener("keydown", (event) => {
  if (event.target instanceof Element && event.target.closest("input, select")) return;
  const shortcuts = {
    " ": togglePlayback,
    k: togglePlayback,
    ArrowLeft: () => skip(-5),
    ArrowRight: () => skip(5),
    f: requestPlayerFullscreen,
    m: () => {
      player.muted = !player.muted;
      renderVolumeButton();
    },
  };
  const handler = shortcuts[event.key];
  if (!handler) return;
  event.preventDefault();
  handler();
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
player.addEventListener("volumechange", renderVolumeButton);
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
document.addEventListener("fullscreenchange", renderFullscreenButton);
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
bootstrap((options = {}) => {
  const history = firstLoad ? "replace" : null;
  firstLoad = false;
  return load({ ...options, history });
}, { interval: 15000 });
