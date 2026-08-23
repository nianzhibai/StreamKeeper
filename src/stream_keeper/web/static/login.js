const form = document.querySelector("#login-form");
const username = document.querySelector("#login-username");
const password = document.querySelector("#login-password");
const confirmationField = document.querySelector("#setup-confirm-field");
const passwordConfirmation = document.querySelector("#setup-password-confirmation");
const submit = document.querySelector("#login-submit");
const errorBox = document.querySelector("#login-error");
const securityText = document.querySelector("#login-security-text");
const themeSlot = document.querySelector("#login-theme");

// shell.js runs as a blocking classic script, so it hands the theme switch over
// on a global rather than through a module export.
const shell = window.streamKeeperShell;

if (themeSlot && shell) {
  themeSlot.innerHTML = shell.themeSwitchMarkup();
  shell.mountThemeSwitch(themeSlot);
}

let setupRequired = false;
let authStateReady = false;

function destination() {
  const candidate = new URLSearchParams(window.location.search).get("next") || "/";
  return candidate.startsWith("/") && !candidate.startsWith("//") ? candidate : "/";
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
}

function setMode(status) {
  setupRequired = status.setup_required;
  authStateReady = true;
  confirmationField.classList.toggle("hidden", !setupRequired);
  passwordConfirmation.disabled = !setupRequired;
  passwordConfirmation.required = setupRequired;
  password.autocomplete = setupRequired ? "new-password" : "current-password";
  if (setupRequired) {
    password.minLength = 10;
    securityText.textContent = "首次访问，请设置管理员密码";
    submit.textContent = "保存并进入";
  } else {
    password.removeAttribute("minlength");
    submit.textContent = "登录";
  }
  submit.disabled = false;
}

async function checkExistingSession() {
  try {
    const response = await fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" });
    if (response.ok) window.location.replace(destination());
  } catch {
    // The form remains usable when the initial session check cannot connect.
  }
}

async function loadAuthState() {
  try {
    const response = await fetch("/api/auth/status", { credentials: "same-origin", cache: "no-store" });
    const status = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(status.detail || `无法读取认证状态 (${response.status})`);
    if (!status.authentication_enabled) {
      window.location.replace(destination());
      return;
    }
    setMode(status);
    if (!setupRequired) await checkExistingSession();
  } catch (error) {
    showError(error.message || "无法连接服务器，请稍后重试");
    submit.disabled = true;
  }
}

passwordConfirmation.addEventListener("input", () => {
  passwordConfirmation.setCustomValidity("");
});

for (const toggle of document.querySelectorAll(".password-toggle")) {
  const input = toggle.parentElement.querySelector("input");
  const icon = toggle.querySelector("use");
  toggle.addEventListener("click", () => {
    const reveal = input.type === "password";
    input.type = reveal ? "text" : "password";
    icon.setAttribute("href", reveal ? "#ic-eyeOff" : "#ic-eye");
    toggle.setAttribute("aria-label", reveal ? "隐藏密码" : "显示密码");
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!authStateReady) return;
  if (setupRequired && password.value !== passwordConfirmation.value) {
    passwordConfirmation.setCustomValidity("两次输入的密码不一致");
  } else {
    passwordConfirmation.setCustomValidity("");
  }
  if (!form.reportValidity()) return;

  submit.disabled = true;
  submit.textContent = setupRequired ? "正在保存…" : "正在登录…";
  errorBox.classList.add("hidden");
  const payload = {
    username: username.value.trim(),
    password: password.value,
  };
  if (setupRequired) payload.password_confirmation = passwordConfirmation.value;
  try {
    const response = await fetch(setupRequired ? "/api/auth/setup" : "/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const retryAfter = response.headers.get("Retry-After");
      const suffix = response.status === 429 && retryAfter ? `（约 ${retryAfter} 秒后可重试）` : "";
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).join("；")
        : body.detail;
      throw new Error(`${detail || `请求失败 (${response.status})`}${suffix}`);
    }
    window.location.replace(destination());
  } catch (error) {
    password.value = "";
    passwordConfirmation.value = "";
    password.focus();
    showError(error.message || "无法连接服务器，请稍后重试");
    submit.disabled = false;
    submit.textContent = setupRequired ? "保存并进入" : "登录";
  }
});

loadAuthState();
