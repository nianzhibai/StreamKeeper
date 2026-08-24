import {
  api,
  bootstrap,
  clearPageError,
  confirmAction,
  escapeHtml,
  formatTime,
  icon,
  setBusy,
  setHealth,
  setHtmlIfChanged,
  setTextIfChanged,
  showPageError,
  toast,
  toggle,
} from "/static/ui.js?v=20260876";

/** Small first paint; older pages stream in on demand as the reader scrolls. */
const PAGE_SIZE = 50;
/** Rows kept in the DOM. Beyond this the oldest are dropped and "load earlier" comes back. */
const MAX_ROWS = 600;
const POLL_INTERVAL = 5000;
const SEARCH_DEBOUNCE = 320;
const AUTO_REFRESH_KEY = "stream-keeper-log-auto-refresh";
const ATTENTION_ACK_KEY = "stream-keeper-log-attention-ack";

const LEVELS = {
  info: { tone: "info", label: "提示" },
  success: { tone: "ok", label: "完成" },
  warning: { tone: "warn", label: "警告" },
  error: { tone: "bad", label: "异常" },
};

const CATEGORIES = {
  task: "录制",
  upload: "归档",
  system: "系统",
  auth: "账号",
};

const timeFormatter = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const dayFormatter = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "short" });

const elements = {
  list: document.querySelector("#log-list"),
  empty: document.querySelector("#log-empty"),
  search: document.querySelector("#log-search"),
  auto: document.querySelector("#log-auto"),
  levelChips: document.querySelector("#log-level-chips"),
  category: document.querySelector("#log-category"),
  attention: document.querySelector("#log-attention"),
  attentionText: document.querySelector("#log-attention-text"),
  attentionFilter: document.querySelector("#log-attention-filter"),
  summary: document.querySelector("#log-summary"),
  clearFilters: document.querySelector("#log-clear-filters"),
  scope: document.querySelector("#log-scope"),
  scopeLabel: document.querySelector("#log-scope-label"),
  scopeClear: document.querySelector("#log-scope-clear"),
  more: document.querySelector("#log-more"),
  exportLink: document.querySelector("#log-export"),
  clear: document.querySelector("#log-clear"),
};

const state = {
  rows: [],
  levels: new Set(),
  category: "",
  search: "",
  taskId: new URLSearchParams(window.location.search).get("task") || "",
  hasMore: false,
  latestId: null,
  loading: false,
  freshIds: new Set(),
  openIds: new Set(),
  pollTimer: 0,
  searchTimer: 0,
  summary: { errors: 0, warnings: 0 },
  attentionAck: readAttentionAck(),
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

/* Viewing the flagged entries acknowledges them: the banner stays away for
   this tab until the 24h error or warning count rises above what was seen. */
function readAttentionAck() {
  try {
    const raw = window.sessionStorage.getItem(ATTENTION_ACK_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw);
    return { errors: Number(value.errors) || 0, warnings: Number(value.warnings) || 0 };
  } catch {
    return null;
  }
}

function writeAttentionAck(counts) {
  try {
    window.sessionStorage.setItem(ATTENTION_ACK_KEY, JSON.stringify(counts));
  } catch {
    /* Private browsing can block storage; the in-memory ack still applies. */
  }
}

/** Shared by the list request and the export link so the two always agree. */
function filterParams() {
  const query = new URLSearchParams();
  state.levels.forEach((level) => query.append("levels", level));
  if (state.category) query.append("categories", state.category);
  if (state.search) query.set("search", state.search);
  if (state.taskId) query.set("task_id", state.taskId);
  return query;
}

function hasFilters() {
  return state.levels.size > 0 || Boolean(state.category) || Boolean(state.search);
}

function dayKey(date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function dayLabel(date) {
  const now = new Date();
  const key = dayKey(date);
  if (key === dayKey(now)) return `今天 · ${dayFormatter.format(date)}`;
  if (key === dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1))) {
    return `昨天 · ${dayFormatter.format(date)}`;
  }
  return dayFormatter.format(date);
}

function entryHtml(event) {
  const level = LEVELS[event.level] || LEVELS.info;
  const category = CATEGORIES[event.category] || event.category;
  const date = new Date(event.created_at);
  const clock = Number.isNaN(date.getTime()) ? String(event.created_at) : timeFormatter.format(date);
  const task = event.task_id
    ? `<a class="log-task" href="/logs?task=${encodeURIComponent(event.task_id)}" title="只看这个任务的记录">${icon("record", "ic-xs")}任务</a>`
    : "";
  const flags = [
    state.freshIds.has(event.id) ? " is-new" : "",
    event.detail ? " has-detail" : "",
    event.detail && state.openIds.has(event.id) ? " is-open" : "",
  ].join("");
  return `
    <li class="log-entry tone-${level.tone}${flags}" data-id="${event.id}">
      <time class="log-time" datetime="${escapeHtml(event.created_at)}" title="${escapeHtml(formatTime(event.created_at, { seconds: true }))}">
        ${escapeHtml(clock)}
      </time>
      <span class="log-dot" title="${escapeHtml(level.label)}"></span>
      <div class="log-text">
        <p class="log-message">
          <strong>${escapeHtml(event.message)}</strong>
          <span class="tag">${escapeHtml(category)}</span>
          ${task}
        </p>
        ${event.detail ? `<small class="log-detail">${escapeHtml(event.detail)}</small>` : ""}
      </div>
      <button class="log-copy" type="button" data-copy="${event.id}" aria-label="复制这条记录" title="复制">
        ${icon("copy", "ic-xs")}
      </button>
    </li>`;
}

/** Rows arrive newest-first; a separator opens every day the feed crosses. */
function feedHtml(rows) {
  const pieces = [];
  let currentDay = "";
  rows.forEach((event) => {
    const date = new Date(event.created_at);
    const key = Number.isNaN(date.getTime()) ? "unknown" : dayKey(date);
    if (key !== currentDay) {
      currentDay = key;
      pieces.push(`<li class="log-day">${escapeHtml(Number.isNaN(date.getTime()) ? "未知日期" : dayLabel(date))}</li>`);
    }
    pieces.push(entryHtml(event));
  });
  return pieces.join("");
}

function renderChips(counts) {
  elements.levelChips.querySelectorAll("[data-level]").forEach((chip) => {
    const value = chip.dataset.level;
    const count = counts[value] || 0;
    const selected = state.levels.has(value);
    setTextIfChanged(chip.querySelector("[data-count]"), String(count));
    chip.setAttribute("aria-pressed", String(selected));
    // A chip with nothing behind it stays visible but unusable, so the row never reflows.
    chip.disabled = count === 0 && !selected;
  });
}

function renderCategoryOptions(counts) {
  elements.category.querySelectorAll("option[value]").forEach((option) => {
    const value = option.value;
    if (!value) return;
    const count = counts[value] || 0;
    setTextIfChanged(option, count ? `${CATEGORIES[value]} · ${count}` : CATEGORIES[value]);
  });
}

/** The banner only exists when the last day produced warnings or errors the
    operator has not viewed yet; warnings alone render it without an accent. */
function renderAttention(summary) {
  const errors = summary.errors || 0;
  const warnings = summary.warnings || 0;
  state.summary = { errors, warnings };
  const ack = state.attentionAck;
  const acknowledged = Boolean(ack) && errors <= ack.errors && warnings <= ack.warnings;
  const show = (errors > 0 || warnings > 0) && !acknowledged;
  toggle(elements.attention, show);
  if (!show) return;
  elements.attention.classList.toggle("tone-bad", errors > 0);
  const parts = [];
  if (errors) parts.push(`${errors} 次异常`);
  if (warnings) parts.push(`${warnings} 次警告`);
  setTextIfChanged(elements.attentionText, `过去 24 小时出现 ${parts.join("、")}`);
  elements.attentionFilter.dataset.level = errors > 0 ? "error" : "warning";
  setTextIfChanged(elements.attentionFilter, errors > 0 ? "查看异常" : "查看警告");
}

function renderList() {
  const filtered = hasFilters() || Boolean(state.taskId);
  setHtmlIfChanged(elements.list, feedHtml(state.rows));
  toggle(elements.list, state.rows.length > 0);
  toggle(elements.empty, state.rows.length === 0);
  toggle(elements.more, state.hasMore && state.rows.length > 0);
  toggle(elements.clearFilters, hasFilters() && state.rows.length === 0);
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
  renderAttention(payload.summary);
  renderChips(payload.facets.levels);
  renderCategoryOptions(payload.facets.categories);
}

/** Full reload: replaces the buffer and resets both cursors. */
async function load({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  try {
    const query = filterParams();
    query.set("limit", String(PAGE_SIZE));
    const payload = await api(`/api/events?${query}`);
    state.rows = payload.events;
    state.hasMore = payload.has_more;
    state.latestId = payload.events[0]?.id ?? payload.summary.latest_id ?? null;
    state.freshIds.clear();
    state.openIds.clear();
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
    armEarlierObserver();
  }
}

/* Older pages load themselves as the reader approaches the end of the feed.
   The observer waits on the "load earlier" button, which is display:none when
   nothing older remains, so a hidden sentinel never fires; re-arming after
   each page re-fires it when the sentinel is still inside the viewport. */
let earlierObserver = null;

function armEarlierObserver() {
  if (!earlierObserver) return;
  earlierObserver.unobserve(elements.more);
  earlierObserver.observe(elements.more);
}

if ("IntersectionObserver" in window) {
  earlierObserver = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadEarlier();
    },
    { rootMargin: "240px 0px" },
  );
  earlierObserver.observe(elements.more);
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
    state.openIds.clear();
    applyPayload(payload);
    renderList();
    toast("运行日志已清空", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

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
  if (!chip || chip.disabled) return;
  if (state.levels.has(chip.dataset.level)) state.levels.delete(chip.dataset.level);
  else state.levels.add(chip.dataset.level);
  load();
});

elements.category.addEventListener("change", () => {
  state.category = elements.category.value;
  load();
});

elements.attentionFilter.addEventListener("click", () => {
  state.attentionAck = { ...state.summary };
  writeAttentionAck(state.attentionAck);
  toggle(elements.attention, false);
  state.levels = new Set([elements.attentionFilter.dataset.level || "error"]);
  load();
});

elements.clearFilters.addEventListener("click", () => {
  state.levels.clear();
  state.category = "";
  state.search = "";
  elements.category.value = "";
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
  if (button) {
    copyRow(button.dataset.copy);
    return;
  }
  if (event.target.closest("a, button")) return;
  // A clamped detail expands in place; the choice sticks across poll re-renders.
  const row = event.target.closest(".log-entry.has-detail");
  if (!row) return;
  const id = Number(row.dataset.id);
  if (state.openIds.has(id)) state.openIds.delete(id);
  else state.openIds.add(id);
  row.classList.toggle("is-open");
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
