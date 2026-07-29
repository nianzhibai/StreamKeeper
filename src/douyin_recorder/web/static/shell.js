import { icon } from "/static/icons.js?v=20260728";

const NAVIGATION = [
  { page: "dashboard", href: "/", label: "概览", icon: "dashboard" },
  { page: "tasks", href: "/tasks", label: "录制任务", icon: "record" },
  { page: "recordings", href: "/recordings", label: "本地录像", icon: "video" },
  { page: "archive", href: "/archive", label: "网盘归档", icon: "cloud" },
  { page: "logs", href: "/logs", label: "运行日志", icon: "logs" },
  { page: "settings", href: "/settings", label: "设置", icon: "settings" },
];

const THEME_MODES = [
  { value: "system", label: "跟随系统", icon: "monitor" },
  { value: "light", label: "浅色", icon: "sun" },
  { value: "dark", label: "深色", icon: "moon" },
];

function brandMarkup() {
  return `
    <a class="brand" href="/" aria-label="Stream Keeper 首页">
      <span class="brand-mark" aria-hidden="true"><i></i></span>
      <span class="brand-text"><strong>Stream Keeper</strong><small>抖音直播录制</small></span>
    </a>`;
}

function navigationMarkup(current) {
  return NAVIGATION.map((item) => {
    const active = item.page === current;
    return `
      <a class="nav-link${active ? " is-active" : ""}" href="${item.href}" data-nav="${item.page}"${active ? ' aria-current="page"' : ""}>
        ${icon(item.icon)}<span>${item.label}</span>
      </a>`;
  }).join("");
}

export function themeSwitchMarkup() {
  const current = window.streamKeeperTheme?.preference() || "system";
  return `
    <div class="theme-switch" role="radiogroup" aria-label="外观主题">
      ${THEME_MODES.map((mode) => `
        <button type="button" role="radio" data-theme-mode="${mode.value}" title="${mode.label}"
          aria-label="${mode.label}" aria-checked="${String(mode.value === current)}">${icon(mode.icon)}</button>`).join("")}
    </div>`;
}

function sidebarMarkup(current) {
  return `
    <aside class="sidebar">
      <div class="sidebar-top">${brandMarkup()}</div>

      <nav class="nav" aria-label="主要导航">${navigationMarkup(current)}</nav>

      <div class="sidebar-foot">
        <div id="server-health" class="health" title="服务连接状态"><i></i><span>正在连接</span></div>
        <div class="sidebar-tools">
          ${themeSwitchMarkup()}
          <a class="api-link" href="/api/docs" target="_blank" rel="noreferrer">API 文档${icon("external", "ic-sm")}</a>
        </div>
        <div class="account">
          <span class="avatar avatar-sm" data-account-avatar aria-hidden="true">A</span>
          <span class="account-text"><strong data-account-name>—</strong><small>管理员</small></span>
          <button class="btn btn-icon btn-ghost" type="button" data-logout aria-label="退出登录" title="退出登录">${icon("logout")}</button>
        </div>
      </div>
    </aside>`;
}

function confirmDialogMarkup() {
  return `
    <dialog id="confirm-dialog" class="modal modal-sm" aria-labelledby="confirm-title">
      <div class="modal-head">
        <span class="modal-glyph" data-confirm-glyph aria-hidden="true">${icon("alert")}</span>
        <div class="modal-heading">
          <h2 id="confirm-title" data-confirm-title>确认操作</h2>
          <p data-confirm-message></p>
        </div>
      </div>
      <div class="modal-foot">
        <button class="btn btn-soft" type="button" data-confirm-cancel>取消</button>
        <button class="btn btn-primary" type="button" data-confirm-accept>确定</button>
      </div>
    </dialog>`;
}

export function mountThemeSwitch(root) {
  root.querySelectorAll("[data-theme-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      const next = window.streamKeeperTheme?.set(button.dataset.themeMode) || "system";
      root.querySelectorAll("[data-theme-mode]").forEach((item) => {
        item.setAttribute("aria-checked", String(item.dataset.themeMode === next));
      });
    });
  });
}

/** Injects the chrome that every signed-in page shares. Safe to call more than once. */
function mountShell() {
  if (document.body.classList.contains("login-page")) return;

  const app = document.querySelector("#app");
  if (app && !app.querySelector(".sidebar")) {
    app.insertAdjacentHTML("afterbegin", sidebarMarkup(document.body.dataset.page));
    mountThemeSwitch(app.querySelector(".sidebar"));
  }

  if (!document.querySelector("#toast-region")) {
    document.body.insertAdjacentHTML(
      "beforeend",
      '<div id="toast-region" class="toasts" role="region" aria-live="polite" aria-label="通知"></div>',
    );
  }
  if (!document.querySelector("#confirm-dialog")) {
    document.body.insertAdjacentHTML("beforeend", confirmDialogMarkup());
  }
}

mountShell();
