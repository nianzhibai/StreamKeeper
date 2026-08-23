import {
  api,
  bootstrap,
  clearPageError,
  confirmAction,
  escapeHtml,
  formatBytes,
  formatTime,
  icon,
  setHealth,
  showPageError,
  toast,
  toggle,
} from "/static/ui.js?v=20260875";

let currentPath = new URLSearchParams(window.location.search).get("path") || "";
let directory = { path: currentPath, entries: [] };
let visible = [];
let searchTerm = "";
let sortMode = "name";
let requestController = null;
let playerInstance = null;

const listNode = document.querySelector("#recording-list");
const headNode = document.querySelector(".file-head");
const emptyNode = document.querySelector("#recording-empty");
const searchNode = document.querySelector("#recording-search");
const sortNode = document.querySelector("#recording-sort");
const playerDialog = document.querySelector("#recording-player-dialog");
const playerContainer = document.querySelector("#recording-player");
const playerError = document.querySelector("#recording-player-error");
const playerHint = document.querySelector("#recording-player-hint");
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

function rowActions(entry) {
  if (entry.kind === "directory") {
    return `<button class="btn btn-icon btn-sm btn-ghost" type="button" data-action="open" aria-label="打开 ${escapeHtml(entry.name)}">${icon("chevronRight", "ic-sm")}</button>`;
  }
  return `<button class="btn btn-sm btn-ghost" type="button" data-action="play"${entry.playable ? "" : " disabled"}>${icon("play", "ic-xs")}<span>播放</span></button>
     <a class="btn btn-icon btn-sm btn-ghost" href="${fileUrl(entry.path, { download: true })}" data-action="download" download aria-label="下载 ${escapeHtml(entry.name)}" title="下载原文件">${icon("download", "ic-sm")}</a>
     <button class="btn btn-icon btn-sm btn-ghost is-negative" type="button" data-action="delete" aria-label="删除 ${escapeHtml(entry.name)}" title="删除">${icon("trash", "ic-sm")}</button>`;
}

function rowTemplate(entry, index) {
  const isFolder = entry.kind === "directory";
  const detail = isFolder ? "文件夹" : `${String(entry.extension || "视频").toUpperCase()} 视频`;
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
      <span class="file-size">${escapeHtml(formatBytes(entry.size))}</span>
      <time class="file-time" datetime="${escapeHtml(entry.modified_at)}">${escapeHtml(formatTime(entry.modified_at))}</time>
      <div class="file-ops">${rowActions(entry)}</div>
    </article>`;
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
  toggle(listNode, entries.length > 0);
  toggle(headNode, entries.length > 0);
  toggle(emptyNode, entries.length === 0);
  if (entries.length === 0) {
    // The title covers the plain empty directory; the hint below it only earns its
    // place when a search term is what hid the files.
    document.querySelector("#recording-empty-title").textContent = searchTerm ? "没有匹配的文件" : "这个目录里还没有录像";
    const detail = document.querySelector("#recording-empty-detail");
    detail.textContent = searchTerm ? "换一个关键词再试试" : "";
    toggle(detail, Boolean(searchTerm));
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
  }
}

function navigate(path) {
  if (path === currentPath) return;
  load({ path, history: "push" });
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
  toggle(playerError, false);
  destroyPlayer();

  if (!Artplayer) {
    toggle(playerError, true);
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

    instance.on("video:error", () => {
      if (playerInstance === instance) toggle(playerError, true);
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
    toggle(playerError, true);
    toast("播放器初始化失败，请刷新页面重试", "error");
  }
}

function openPlayer(entry) {
  if (!entry.playable) return;
  toggle(playerHint, entry.playback_mode === "remux");
  document.querySelector("#recording-player-path").textContent = `/${entry.path}`;
  document.querySelector("#recording-player-meta").textContent = [
    String(entry.extension).toUpperCase(),
    formatBytes(entry.size),
    formatTime(entry.modified_at),
  ].join(" · ");
  playerDialog.showModal();
  setPlayerSource(entry);
}

function closePlayer() {
  if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
  destroyPlayer();
  if (playerDialog.open) playerDialog.close();
  toggle(playerError, false);
}

async function deleteRecording(entry, button) {
  const proceed = await confirmAction({
    title: "删除录像文件",
    message: `将永久删除“${entry.name}”及对应的转码文件，此操作无法撤销。`,
    confirmLabel: "删除文件",
  });
  if (!proceed) return;

  button.disabled = true;
  try {
    await api(`/api/recordings/file/${encodePath(entry.path)}`, { method: "DELETE" });
    toast("录像文件已删除", "success");
    await load({ quiet: true });
  } catch (error) {
    button.disabled = false;
    toast(error.message, "error");
  }
}

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
listNode.addEventListener("click", async (event) => {
  const action = event.target.closest("[data-action]");
  const row = event.target.closest("[data-entry-index]");
  if (!action || !row || action.dataset.action === "download") return;
  const entry = visible[Number(row.dataset.entryIndex)];
  if (!entry) return;
  if (action.dataset.action === "open") navigate(entry.path);
  if (action.dataset.action === "play") openPlayer(entry);
  if (action.dataset.action === "delete") await deleteRecording(entry, action);
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
}, { interval: 15000 });
