const sessionState = {
  value: null,
};

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function formatTime(value, { seconds = false } = {}) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: seconds ? "2-digit" : undefined,
    hour12: false,
  }).format(date);
}

export function toast(message, type = "info") {
  const region = document.querySelector("#toast-region");
  if (!region) return;
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "toast-error" : ""}`;
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.textContent = message;
  region.append(node);
  window.setTimeout(() => node.remove(), 3200);
}

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const method = String(options.method || "GET").toUpperCase();
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method) && sessionState.value?.csrf_token) {
    headers.set("X-CSRF-Token", sessionState.value.csrf_token);
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
    cache: method === "GET" ? "no-store" : options.cache,
  });
  if (response.status === 401) {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.replace(`/login?next=${encodeURIComponent(next)}`);
    throw new Error("登录已过期");
  }
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof body === "object" ? body.detail : body;
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg).join("；")
      : detail || `请求失败 (${response.status})`;
    throw new Error(message);
  }
  return body;
}

export function setHealth(online) {
  const health = document.querySelector("#server-health");
  if (!health) return;
  health.className = `server-state ${online ? "is-online" : "is-offline"}`;
  health.innerHTML = `<i></i><span>${online ? "服务正常" : "连接失败"}</span>`;
}

export function showPageError(message) {
  const banner = document.querySelector("#page-error");
  if (!banner) return;
  banner.textContent = message;
  banner.classList.remove("hidden");
}

export function clearPageError() {
  document.querySelector("#page-error")?.classList.add("hidden");
}

async function logout() {
  const buttons = document.querySelectorAll("[data-logout]");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    if (!String(error.message).includes("登录已过期")) toast(error.message, "error");
  } finally {
    window.location.replace("/login");
  }
}

function initializeShell(session) {
  const name = session.username || "admin";
  document.querySelectorAll("[data-account-name]").forEach((element) => {
    element.textContent = name;
  });
  document.querySelectorAll("[data-account-avatar]").forEach((element) => {
    element.textContent = [...name][0]?.toUpperCase() || "A";
  });
  document.querySelectorAll("[data-logout]").forEach((button) => button.addEventListener("click", logout));

  const currentPage = document.body.dataset.page;
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const active = link.dataset.nav === currentPage;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
  });
}

export async function bootstrap(load, { interval = 5000 } = {}) {
  try {
    sessionState.value = await api("/api/auth/session");
    initializeShell(sessionState.value);
    await load();
    setHealth(true);
    if (interval > 0) {
      window.setInterval(async () => {
        try {
          await load({ quiet: true });
          setHealth(true);
        } catch (error) {
          setHealth(false);
          showPageError(error.message || "无法读取服务器状态");
        }
      }, interval);
    }
  } catch (error) {
    if (!String(error.message).includes("登录已过期")) {
      setHealth(false);
      showPageError(error.message || "页面加载失败");
    }
  }
}

export function providerStatus(provider, kind) {
  if (provider.enabled) return { label: "已启用", tone: "success" };
  const configured = kind === "quark"
    ? provider.credential_configured
    : provider.access_token_configured || provider.refresh_token_configured;
  return configured
    ? { label: "未启用", tone: "neutral" }
    : { label: "未配置", tone: "muted" };
}

export function archiveStatus(cloud) {
  if (cloud.running) return { label: "上传中", tone: "recording" };
  if (!cloud.last_run) return { label: cloud.enabled ? "等待执行" : "未配置", tone: "neutral" };
  const labels = {
    success: "执行成功",
    partial: "部分失败",
    failed: "执行失败",
    cancelled: "已取消",
  };
  const isError = ["partial", "failed", "cancelled"].includes(cloud.last_run.status);
  return {
    label: labels[cloud.last_run.status] || cloud.last_run.status,
    tone: isError ? "danger" : "success",
  };
}
