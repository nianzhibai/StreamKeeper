import { mountThemeSwitch, themeSwitchMarkup } from "/static/shell.js?v=20260728";

const form = document.querySelector("#login-form");
const username = document.querySelector("#login-username");
const password = document.querySelector("#login-password");
const submit = document.querySelector("#login-submit");
const errorBox = document.querySelector("#login-error");
const themeSlot = document.querySelector("#login-theme");

if (themeSlot) {
  themeSlot.innerHTML = themeSwitchMarkup();
  mountThemeSwitch(themeSlot);
}

function destination() {
  const candidate = new URLSearchParams(window.location.search).get("next") || "/";
  return candidate.startsWith("/") && !candidate.startsWith("//") ? candidate : "/";
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
}

async function checkExistingSession() {
  try {
    const response = await fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" });
    if (response.ok) window.location.replace(destination());
  } catch {
    // The form remains usable when the initial session check cannot connect.
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;

  submit.disabled = true;
  submit.textContent = "正在登录…";
  errorBox.classList.add("hidden");
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value.trim(), password: password.value }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const retryAfter = response.headers.get("Retry-After");
      const suffix = response.status === 429 && retryAfter ? `（约 ${retryAfter} 秒后可重试）` : "";
      throw new Error(`${body.detail || `登录失败 (${response.status})`}${suffix}`);
    }
    window.location.replace(destination());
  } catch (error) {
    password.value = "";
    password.focus();
    showError(error.message || "无法连接服务器，请稍后重试");
    submit.disabled = false;
    submit.textContent = "登录";
  }
});

checkExistingSession();
