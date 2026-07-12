import {
  api,
  bootstrap,
  clearPageError,
  setHealth,
  showPageError,
  toast,
} from "/static/ui.js?v=20260712-ui1";

const form = document.querySelector("#cloud-form");
const saveButton = document.querySelector("#cloud-save-button");
const loginDialog = document.querySelector("#cloud-login-dialog");
const loginImage = document.querySelector("#cloud-login-image");
const loginStatus = document.querySelector("#cloud-login-status");
const loginTip = document.querySelector("#cloud-login-tip");
const loginRefresh = document.querySelector("#cloud-login-refresh");
const loginClose = document.querySelector("#cloud-login-close");
let cloud = null;
let activeLogin = null;
let activeProvider = null;
let loginPollTimer = null;
let loginGeneration = 0;

const providerNames = {
  quark: { name: "夸克网盘", app: "夸克 App" },
  wopan: { name: "联通云盘", app: "联通云盘 App" },
};

function updateProviderAppearance() {
  document.querySelector("#quark-settings")?.classList.toggle("is-enabled", form.elements.quark_enabled.checked);
  document.querySelector("#wopan-settings")?.classList.toggle("is-enabled", form.elements.wopan_enabled.checked);
}

function updateCredentialHints(value) {
  document.querySelector("#quark-credential-hint").textContent = value.quark.credential_configured
    ? "账号已登录"
    : "未登录";
  const wopanConfigured = value.wopan.access_token_configured || value.wopan.refresh_token_configured;
  document.querySelector("#wopan-credential-hint").textContent = wopanConfigured
    ? "账号已登录"
    : "未登录";
}

function populate(value) {
  cloud = value;
  form.reset();
  form.elements.quark_enabled.checked = cloud.quark.enabled;
  form.elements.quark_root_id.value = cloud.quark.root_id;
  form.elements.quark_upload_path.value = cloud.quark.upload_path;
  form.elements.wopan_enabled.checked = cloud.wopan.enabled;
  form.elements.wopan_root_id.value = cloud.wopan.root_id;
  form.elements.wopan_family_id.value = cloud.wopan.family_id;
  form.elements.wopan_upload_path.value = cloud.wopan.upload_path;
  form.elements.upload_hour.value = String(cloud.schedule.hour);
  form.elements.upload_min_age_minutes.value = String(cloud.schedule.min_age_minutes);
  form.elements.upload_timeout_seconds.value = String(cloud.schedule.timeout_seconds);

  updateCredentialHints(cloud);
  updateProviderAppearance();
}

function clearLoginPoll() {
  if (loginPollTimer) window.clearTimeout(loginPollTimer);
  loginPollTimer = null;
}

function setLoginButtonsDisabled(disabled) {
  document.querySelectorAll("[data-cloud-login]").forEach((button) => {
    button.disabled = disabled;
  });
}

async function cancelActiveLogin() {
  loginGeneration += 1;
  clearLoginPoll();
  const current = activeLogin;
  activeLogin = null;
  if (!current) return;
  try {
    await api(`/api/cloud/login/${current.provider}/${current.session_id}`, { method: "DELETE" });
  } catch (error) {
    if (!String(error.message).includes("不存在")) console.warn(error);
  }
}

function renderLogin(value) {
  const provider = providerNames[value.provider];
  loginDialog.querySelector("#cloud-login-title").textContent = `${provider.name}登录`;
  loginStatus.textContent = value.message;
  loginTip.textContent = `使用 ${provider.app} 扫描并确认`;
  if (value.qr_image && loginImage.src !== value.qr_image) loginImage.src = value.qr_image;
  const terminal = ["success", "expired", "error", "cancelled"].includes(value.state);
  loginRefresh.classList.toggle("hidden", !["expired", "error"].includes(value.state));
  loginImage.closest(".qr-image-frame")?.classList.toggle("is-dimmed", terminal && value.state !== "success");
}

async function finishSuccessfulLogin(value) {
  clearLoginPoll();
  const completed = activeLogin;
  activeLogin = null;
  try {
    cloud = await api("/api/cloud/archive");
    updateCredentialHints(cloud);
    form.elements[`${value.provider === "quark" ? "quark_clear_cookie" : "wopan_clear_tokens"}`].checked = false;
    toast(`${providerNames[value.provider].name}登录成功`);
  } catch (error) {
    toast(error.message, "error");
  }
  if (completed) {
    api(`/api/cloud/login/${completed.provider}/${completed.session_id}`, { method: "DELETE" }).catch(() => {});
  }
  window.setTimeout(() => {
    if (loginDialog.open) loginDialog.close();
  }, 700);
}

async function pollLogin() {
  const current = activeLogin;
  if (!current) return;
  try {
    const value = await api(`/api/cloud/login/${current.provider}/${current.session_id}`);
    if (!activeLogin || activeLogin.session_id !== current.session_id) return;
    activeLogin = value;
    renderLogin(value);
    if (value.state === "success") {
      await finishSuccessfulLogin(value);
      return;
    }
    if (["expired", "error", "cancelled"].includes(value.state)) return;
    loginPollTimer = window.setTimeout(pollLogin, 1800);
  } catch (error) {
    if (!activeLogin || activeLogin.session_id !== current.session_id) return;
    loginStatus.textContent = error.message;
    loginRefresh.classList.remove("hidden");
  }
}

async function startCloudLogin(provider) {
  activeProvider = provider;
  await cancelActiveLogin();
  const generation = ++loginGeneration;
  setLoginButtonsDisabled(true);
  loginDialog.querySelector("#cloud-login-title").textContent = `${providerNames[provider].name}登录`;
  loginStatus.textContent = "正在生成二维码";
  loginTip.textContent = "";
  loginRefresh.classList.add("hidden");
  loginImage.removeAttribute("src");
  loginImage.closest(".qr-image-frame")?.classList.remove("is-dimmed");
  if (!loginDialog.open) loginDialog.showModal();
  try {
    const created = await api(`/api/cloud/login/${provider}`, { method: "POST" });
    if (generation !== loginGeneration || !loginDialog.open) {
      api(`/api/cloud/login/${created.provider}/${created.session_id}`, { method: "DELETE" }).catch(() => {});
      return;
    }
    activeLogin = created;
    renderLogin(activeLogin);
    loginPollTimer = window.setTimeout(pollLogin, 1200);
  } catch (error) {
    loginStatus.textContent = error.message;
    loginTip.textContent = "";
    loginRefresh.classList.remove("hidden");
  } finally {
    setLoginButtonsDisabled(false);
  }
}

async function load() {
  try {
    populate(await api("/api/cloud/archive"));
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法读取设置：${error.message}`);
    throw error;
  }
}

async function submit(event) {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const fields = form.elements;
  const secret = (name) => String(fields[name].value || "").trim() || null;
  const payload = {
    quark: {
      enabled: fields.quark_enabled.checked,
      cookie: secret("quark_cookie"),
      clear_cookie: fields.quark_clear_cookie.checked,
      root_id: fields.quark_root_id.value.trim(),
      upload_path: fields.quark_upload_path.value.trim(),
    },
    wopan: {
      enabled: fields.wopan_enabled.checked,
      access_token: secret("wopan_access_token"),
      refresh_token: secret("wopan_refresh_token"),
      clear_tokens: fields.wopan_clear_tokens.checked,
      root_id: fields.wopan_root_id.value.trim(),
      family_id: fields.wopan_family_id.value.trim(),
      upload_path: fields.wopan_upload_path.value.trim(),
    },
    schedule: {
      hour: Number(fields.upload_hour.value),
      min_age_minutes: Number(fields.upload_min_age_minutes.value),
      timeout_seconds: Number(fields.upload_timeout_seconds.value),
    },
  };
  saveButton.disabled = true;
  saveButton.textContent = "保存中…";
  try {
    populate(await api("/api/cloud/archive", { method: "PUT", body: JSON.stringify(payload) }));
    toast("设置已保存");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = "保存设置";
  }
}

form.addEventListener("submit", submit);
form.elements.quark_enabled.addEventListener("change", updateProviderAppearance);
form.elements.wopan_enabled.addEventListener("change", updateProviderAppearance);
document.querySelectorAll("[data-cloud-login]").forEach((button) => {
  button.addEventListener("click", () => startCloudLogin(button.dataset.cloudLogin));
});
loginRefresh.addEventListener("click", () => startCloudLogin(activeProvider));
loginClose.addEventListener("click", () => loginDialog.close());
loginDialog.addEventListener("close", () => {
  cancelActiveLogin();
});
bootstrap(load, { interval: 0 });
