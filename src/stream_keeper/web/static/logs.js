import {
  api,
  bootstrap,
  clearPageError,
  confirmAction,
  escapeHtml,
  formatRelative,
  formatTime,
  icon,
  setBusy,
  setHealth,
  setHtmlIfChanged,
  setTextIfChanged,
  showPageError,
  toast,
  toggle,
} from "/static/ui.js?v=20260814";

const PAGE_SIZE = 100;
/** Rows kept in the DOM. Beyond this the oldest are dropped and "load earlier" comes back. */
const MAX_ROWS = 600;
const POLL_INTERVAL = 5000;
const SEARCH_DEBOUNCE = 320;
const AUTO_REFRESH_KEY = "stream-keeper-log-auto-refresh";

const LEVELS = {
  info: { tone: "info", icon: "info", label: "提示" },
  success: { tone: "ok", icon: "checkCircle", label: "完成" },
  warning: { tone: "warn", icon: "alert", label: "警告" },
  error: { tone: "bad", icon: "alert", label: "异常" },
};

const CATEGORIES = {
  task: "录制",
  upload: "归档",
  system: "系统",
  auth: "账号",
};

const elements = {
  list: document.querySelector("#log-list"),
  empty: document.querySelector("#log-empty"),
  refresh: document.querySelector("#refresh-button"),
  search: document.querySelector("#log-search"),
  auto: document.querySelector("#log-auto"),
  levelChips: document.querySelector("#log-level-chips"),
  categoryChips: document.querySelector("#log-category-chips"),
  summary: document.querySelector("#log-summary"),
  clearFilters: document.querySelector("#log-clear-filters"),
  scope: document.querySelector("#log-scope"),
  scopeLabel: document.querySelector("#log-scope-label"),
  scopeClear: document.querySelector("#log-scope-clear"),
  moreWrap: document.querySelector("#log-more-wrap"),
  more: document.querySelector("#log-more"),
  exportLink: document.querySelector("#log-export"),
  clear: document.querySelector("#log-clear"),
};

const state = {
  rows: [],
  levels: new Set(),
  categories: new Set(),
  search: "",
  taskId: new URLSearchParams(window.location.search).get("task") || "",
  hasMore: false,
  latestId: null,
  loading: false,
  freshIds: new Set(),
  pollTimer: 0,
  searchTimer: 0,
};

function readAutoRefresh() {
  try {
    return window.localStorage.getItem(AUTO_REFRESH_KEY) !== "off";
  } catch {
    return true;
  }
}

function writeAutoRefresh(enabled) {
  try {
    if (enabled) window.localStorage.removeItem(AUTO_REFRESH_KEY);
    else window.localStorage.setItem(AUTO_REFRESH_KEY, "off");
  } catch {
    /* Private browsing can block storage; the in-memory choice still applies. */
  }
}

/** Shared by the list request and the export link so the two always agree. */
function filterParams() {
  const query = new URLSearchParams();
  state.levels.forEach((level) => query.append("levels", level));
  state.categories.forEach((category) => query.append("categories", category));
  if (state.search) query.set("search", state.search);
  if (state.taskId) query.set("task_id", state.taskId);
  return query;
}

function hasFilters() {
  return state.levels.size > 0 || state.categories.size > 0 || Boolean(state.search);
}

function eventRow(event) {
  const level = LEVELS[event.level] || LEVELS.info;
  const category = CATEGORIES[event.category] || event.category;
  const task = event.task_id
    ? `<a class="log-task" href="/logs?task=${encodeURIComponent(event.task_id)}" title="只看这个任务的记录">${icon("record", "ic-xs")}任务</a>`
    : "";
  return `
    <li class="log-row tone-${level.tone}${state.freshIds.has(event.id) ? " is-new" : ""}" data-id="${event.id}">
      <span class="log-mark" title="${escapeHtml(level.label)}">${icon(level.icon, "ic-sm")}</span>
      <div class="log-body">
        <div class="log-top">
          <strong>${escapeHtml(event.message)}</strong>
          <span class="tag">${escapeHtml(category)}</span>
          ${task}
        </div>
        ${event.detail ? `<small>${escapeHtml(event.detail)}</small>` : ""}
      </div>
      <div class="log-side">
        <time class="log-time" datetime="${escapeHtml(event.created_at)}" title="${escapeHtml(formatTime(event.created_at, { seconds: true }))}">
          ${escapeHtml(formatRelative(event.created_at))}
        </time>
        <button class="log-copy" type="button" data-copy="${event.id}" aria-label="复制这条记录" title="复制">
          ${icon("copy", "ic-xs")}
        </button>
      </div>
    </li>`;
}

function renderChips(container, key, counts) {
  container.querySelectorAll(`[data-${key}]`).forEach((chip) => {
    const value = chip.dataset[key];
    const count = counts[value] || 0;
    const selected = key === "level" ? state.levels.has(value) : state.categories.has(value);
    setTextIfChanged(chip.querySelector("[data-count]"), String(count));
    chip.setAttribute("aria-pressed", String(selected));
    // A chip with nothing behind it stays visible but unusable, so the row never reflows.
    chip.disabled = count === 0 && !selected;
  });
}

function renderSummary(summary) {
  const badge = document.querySelector("#log-status");
  const status = summary.errors
    ? { tone: "bad", label: "近 24 小时有异常" }
    : summary.warnings
      ? { tone: "warn", label: "近 24 小时有警告" }
      : summary.total
        ? { tone: "ok", label: "运行正常" }
        : { tone: "idle", label: "暂无记录" };
  badge.className = `pill pill-lg tone-${status.tone}`;
  badge.innerHTML = `<i class="dot"></i>${escapeHtml(status.label)}`;

  setTextIfChanged(
    document.querySelector("#log-status-time"),
    summary.latest_at ? `最近事件 ${formatRelative(summary.latest_at)}` : "服务还没有产生事件",
  );
  setTextIfChanged(document.querySelector("#log-errors"), String(summary.errors));
  setTextIfChanged(document.querySelector("#log-warnings"), String(summary.warnings));
  setTextIfChanged(document.querySelector("#log-total"), String(summary.total));
  document.querySelector("#log-errors").classList.toggle("is-bad", Boolean(summary.errors));
}

function renderList() {
  const filtered = hasFilters() || Boolean(state.taskId);
  setHtmlIfChanged(elements.list, state.rows.map(eventRow).join(""));
  toggle(elements.list, state.rows.length > 0);
  toggle(elements.empty, state.rows.length === 0);
  toggle(elements.moreWrap, state.hasMore && state.rows.length > 0);
  toggle(elements.clearFilters, hasFilters());
  toggle(elements.scope, Boolean(state.taskId));

  if (state.rows.length === 0) {
    setTextIfChanged(
      elements.empty.querySelector("strong"),
      filtered ? "没有符合条件的记录" : "暂无运行事件",
    );
    setTextIfChanged(
      elements.empty.querySelector("small"),
      filtered
        ? "换一个级别、类别或关键词再看看"
        : "启动录制任务或执行网盘归档后，这里会记录关键节点",
    );
  }

  const scopeSuffix = state.taskId ? "（限定单个任务）" : "";
  setTextIfChanged(
    elements.summary,
    state.rows.length === 0
      ? filtered
        ? `没有符合筛选条件的记录${scopeSuffix}`
        : "暂无记录"
      : state.hasMore
        ? `显示最近 ${state.rows.length} 条，还有更早的记录${scopeSuffix}`
        : `共 ${state.rows.length} 条记录${scopeSuffix}`,
  );

  // The flash marker is one-shot: keep it only until the row has been painted.
  if (state.freshIds.size) {
    window.setTimeout(() => state.freshIds.clear(), 500);
  }
}

function applyExportLink() {
  const query = filterParams();
  query.set("format", "txt");
  elements.exportLink.href = `/api/events/export?${query}`;
}

function applyPayload(payload) {
  renderSummary(payload.summary);
  renderChips(elements.levelChips, "level", payload.facets.levels);
  renderChips(elements.categoryChips, "category", payload.facets.categories);
}

/** Full reload: replaces the buffer and resets both cursors. */
async function load({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  if (!quiet) setBusy(elements.refresh, true);
  try {
    const query = filterParams();
    query.set("limit", String(PAGE_SIZE));
    const payload = await api(`/api/events?${query}`);
    state.rows = payload.events;
    state.hasMore = payload.has_more;
    state.latestId = payload.events[0]?.id ?? payload.summary.latest_id ?? null;
    state.freshIds.clear();
    applyPayload(payload);
    renderList();
    applyExportLink();
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法读取运行日志：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    state.loading = false;
    setBusy(elements.refresh, false);
  }
}

/** Incremental poll: asks only for rows newer than the newest one already shown. */
async function poll() {
  if (state.loading || document.hidden) return;
  state.loading = true;
  try {
    const query = filterParams();
    query.set("limit", String(PAGE_SIZE));
    if (state.latestId !== null) query.set("after_id", String(state.latestId));
    const payload = await api(`/api/events?${query}`);
    applyPayload(payload);

    if (payload.events.length) {
      // A burst larger than one page means the tail may have gaps; start over.
      if (payload.events.length >= PAGE_SIZE) {
        state.loading = false;
        await load({ quiet: true });
        return;
      }
      state.freshIds = new Set(payload.events.map((event) => event.id));
      state.rows = [...payload.events, ...state.rows];
      state.latestId = payload.events[0].id;
      if (state.rows.length > MAX_ROWS) {
        state.rows = state.rows.slice(0, MAX_ROWS);
        state.hasMore = true;
      }
      renderList();
    }
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法读取运行日志：${error.message}`);
  } finally {
    state.loading = false;
  }
}

async function loadEarlier() {
  const oldest = state.rows[state.rows.length - 1];
  if (!oldest || state.loading) return;
  state.loading = true;
  setBusy(elements.more, true);
  elements.more.disabled = true;
  try {
    const query = filterParams();
    query.set("limit", String(PAGE_SIZE));
    query.set("before_id", String(oldest.id));
    const payload = await api(`/api/events?${query}`);
    state.rows = [...state.rows, ...payload.events];
    state.hasMore = payload.has_more;
    renderList();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.loading = false;
    setBusy(elements.more, false);
    elements.more.disabled = false;
  }
}

function syncPolling() {
  const enabled = elements.auto.checked;
  if (enabled && !state.pollTimer) {
    state.pollTimer = window.setInterval(poll, POLL_INTERVAL);
  } else if (!enabled && state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = 0;
  }
}

function toggleFilter(set, value) {
  if (set.has(value)) set.delete(value);
  else set.add(value);
  load();
}

async function copyRow(id) {
  const event = state.rows.find((row) => String(row.id) === String(id));
  if (!event) return;
  const level = LEVELS[event.level]?.label || event.level;
  const category = CATEGORIES[event.category] || event.category;
  const text = [
    formatTime(event.created_at, { seconds: true }),
    `[${level}]`,
    `[${category}]`,
    event.message,
    event.detail ? `| ${event.detail}` : "",
  ].filter(Boolean).join(" ");
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制这条记录", "success");
  } catch {
    toast("浏览器拒绝了剪贴板访问", "error");
  }
}

async function clearLog() {
  const proceed = await confirmAction({
    title: "清空运行日志",
    message: "将删除全部运行事件记录，无法恢复。录制任务和已保存的录像不受影响。",
    confirmLabel: "清空日志",
  });
  if (!proceed) return;
  try {
    const payload = await api("/api/events", { method: "DELETE" });
    state.rows = payload.events;
    state.hasMore = payload.has_more;
    state.latestId = payload.events[0]?.id ?? null;
    state.freshIds.clear();
    applyPayload(payload);
    renderList();
    toast("运行日志已清空", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

elements.refresh?.addEventListener("click", () => load());
elements.more?.addEventListener("click", loadEarlier);
elements.clear?.addEventListener("click", clearLog);

elements.auto.checked = readAutoRefresh();
elements.auto.addEventListener("change", () => {
  writeAutoRefresh(elements.auto.checked);
  syncPolling();
  if (elements.auto.checked) poll();
});

elements.search.addEventListener("input", () => {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(() => {
    const next = elements.search.value.trim();
    if (next === state.search) return;
    state.search = next;
    load();
  }, SEARCH_DEBOUNCE);
});

elements.levelChips.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-level]");
  if (chip && !chip.disabled) toggleFilter(state.levels, chip.dataset.level);
});

elements.categoryChips.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-category]");
  if (chip && !chip.disabled) toggleFilter(state.categories, chip.dataset.category);
});

elements.clearFilters.addEventListener("click", () => {
  state.levels.clear();
  state.categories.clear();
  state.search = "";
  elements.search.value = "";
  load();
});

elements.scopeClear.addEventListener("click", () => {
  state.taskId = "";
  window.history.replaceState({}, "", "/logs");
  load();
});

elements.list.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  if (button) copyRow(button.dataset.copy);
});

elements.list.addEventListener("dblclick", (event) => {
  const row = event.target.closest("[data-id]");
  if (row && !event.target.closest("a, button")) copyRow(row.dataset.id);
});

document.addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target instanceof Element && event.target.closest("input, select, textarea")) return;
  if (event.key === "/") {
    event.preventDefault();
    elements.search.focus();
  }
});

if (state.taskId) {
  setTextIfChanged(elements.scopeLabel, `任务 ${state.taskId.slice(0, 8)}`);
}

// The shared bootstrap poll is switched off: this page drives its own incremental
// refresh so a periodic tick never discards the history the operator loaded.
bootstrap(load, { interval: 0 }).then(syncPolling);
