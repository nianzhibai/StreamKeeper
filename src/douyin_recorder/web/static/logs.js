import {
  api,
  bootstrap,
  clearPageError,
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
} from "/static/ui.js?v=20260728";

const EVENT_LIMIT = 200;

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
  level: document.querySelector("#log-level"),
};

const state = {
  category: "",
  alertsOnly: false,
};

let loading = false;

function eventItem(event) {
  const level = LEVELS[event.level] || LEVELS.info;
  const category = CATEGORIES[event.category] || event.category;
  return `
    <li class="log-row tone-${level.tone}">
      <span class="log-mark" title="${escapeHtml(level.label)}">${icon(level.icon, "ic-sm")}</span>
      <div class="log-body">
        <div class="log-top">
          <strong>${escapeHtml(event.message)}</strong>
          <span class="tag">${escapeHtml(category)}</span>
        </div>
        ${event.detail ? `<small>${escapeHtml(event.detail)}</small>` : ""}
      </div>
      <time class="log-time" datetime="${escapeHtml(event.created_at)}" title="${escapeHtml(formatTime(event.created_at, { seconds: true }))}">
        ${escapeHtml(formatRelative(event.created_at))}
      </time>
    </li>`;
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

function renderEvents(events) {
  setHtmlIfChanged(elements.list, events.map(eventItem).join(""));
  toggle(elements.list, events.length > 0);
  toggle(elements.empty, events.length === 0);

  const filtered = Boolean(state.category) || state.alertsOnly;
  if (events.length === 0) {
    setTextIfChanged(
      elements.empty.querySelector("strong"),
      filtered ? "没有符合条件的记录" : "暂无运行事件",
    );
    setTextIfChanged(
      elements.empty.querySelector("small"),
      filtered ? "换一个类别或级别再看看" : "启动录制任务或执行网盘归档后，这里会记录关键节点",
    );
  }
  setTextIfChanged(
    document.querySelector("#log-summary"),
    events.length === 0
      ? filtered
        ? "没有符合筛选条件的记录"
        : "暂无记录"
      : events.length < EVENT_LIMIT
        ? `共 ${events.length} 条记录`
        : `显示最近 ${EVENT_LIMIT} 条记录`,
  );
}

async function load({ quiet = false } = {}) {
  if (loading) return;
  loading = true;
  if (!quiet) setBusy(elements.refresh, true);
  try {
    const query = new URLSearchParams({ limit: String(EVENT_LIMIT) });
    if (state.category) query.set("category", state.category);
    if (state.alertsOnly) query.set("alerts_only", "true");
    const payload = await api(`/api/events?${query}`);
    renderSummary(payload.summary);
    renderEvents(payload.events);
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法读取运行日志：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    loading = false;
    setBusy(elements.refresh, false);
  }
}

elements.refresh?.addEventListener("click", () => load());
elements.level?.addEventListener("change", () => {
  state.alertsOnly = elements.level.value === "alerts";
  load();
});
document.querySelectorAll("[data-category]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-category]").forEach((item) => item.classList.remove("is-active"));
    button.classList.add("is-active");
    state.category = button.dataset.category;
    load();
  });
});

bootstrap(load);
