import {
  api,
  archiveStatus,
  bootstrap,
  clearPageError,
  confirmAction,
  escapeHtml,
  formatRelative,
  formatTime,
  icon,
  providerStatus,
  setBusy,
  setHealth,
  setTextIfChanged,
  showPageError,
  toast,
} from "/static/ui.js?v=20260814";

const refreshButton = document.querySelector("#refresh-button");
const runButton = document.querySelector("#cloud-run-button");
const providerDialog = document.querySelector("#provider-config-dialog");
const providerForm = document.querySelector("#provider-config-form");
const providerSaveButton = document.querySelector("#provider-config-save");
const providerCloseButton = document.querySelector("#provider-config-close");
const providerCancelButton = document.querySelector("#provider-config-cancel");
const providerLoginButton = document.querySelector("#provider-login-button");
const providerLogo = document.querySelector("#provider-config-logo");
const providerTitle = document.querySelector("#provider-config-title");
const providerDescription = document.querySelector("#provider-config-description");
const providerCredentialHint = document.querySelector("#provider-credential-hint");
const providerLoginDescription = document.querySelector("#provider-login-description");
const loginDialog = document.querySelector("#cloud-login-dialog");
const loginImage = document.querySelector("#cloud-login-image");
const loginStatus = document.querySelector("#cloud-login-status");
const loginTip = document.querySelector("#cloud-login-tip");
const loginRefresh = document.querySelector("#cloud-login-refresh");
const loginClose = document.querySelector("#cloud-login-close");

const providerMeta = {
  quark: {
    name: "夸克网盘",
    app: "夸克 App",
    description: "配置夸克账号、上传根目录和启用状态",
    loginDescription: "使用夸克 App 扫码，无需手动复制 Cookie",
  },
  wopan: {
    name: "联通云盘",
    app: "联通云盘 App",
    description: "配置联通云盘账号、上传根目录和启用状态",
    loginDescription: "使用联通云盘 App 扫码获取 Token",
  },
};

let cloud = null;
let loading = false;
let activeProvider = null;
let activeLogin = null;
let loginPollTimer = null;
let loginGeneration = 0;

function credentialConfigured(kind, provider) {
  return kind === "quark"
    ? provider.credential_configured
    : provider.access_token_configured || provider.refresh_token_configured;
}

function renderProvider(kind, provider) {
  const status = providerStatus(provider, kind);
  const statusNode = document.querySelector(`#${kind}-status`);
  statusNode.className = `pill tone-${status.tone}`;
  statusNode.innerHTML = `<i class="dot"></i>${escapeHtml(status.label)}`;
  setTextIfChanged(document.querySelector(`#${kind}-path`), provider.upload_path || "—");

  const credential = credentialConfigured(kind, provider);
  const credentialNode = document.querySelector(`#${kind}-credential`);
  setTextIfChanged(credentialNode, credential ? "凭据已保存" : "未保存凭据");
  credentialNode.className = `chip${credential ? " is-ok" : ""}`;

  document.querySelector(`#${kind}-configure`)?.classList.toggle("is-enabled", provider.enabled);
}

function renderLastRun(lastRun) {
  const summary = lastRun?.summary;
  setTextIfChanged(document.querySelector("#run-scanned"), summary?.scanned_files ?? "—");
  setTextIfChanged(document.querySelector("#run-uploaded"), summary?.uploaded_copies ?? "—");
  setTextIfChanged(document.querySelector("#run-skipped"), summary?.skipped_files ?? "—");
  setTextIfChanged(document.querySelector("#run-deleted"), summary?.deleted_files ?? "—");
  setTextIfChanged(document.querySelector("#run-failed"), summary?.failed_files ?? "—");
  document.querySelector("#run-failed").closest("div").classList.toggle("is-danger", Boolean(summary?.failed_files));

  const time = document.querySelector("#last-run-time");
  const detail = document.querySelector("#last-run-detail");
  if (!lastRun) {
    setTextIfChanged(time, "暂无执行记录");
    setTextIfChanged(detail, "启用网盘后会在计划时间自动执行");
    return;
  }
  const trigger = lastRun.trigger === "manual" ? "手动触发" : "定时执行";
  setTextIfChanged(time, `${trigger} · ${formatRelative(lastRun.finished_at || lastRun.started_at)}`);
  if (lastRun.error) {
    setTextIfChanged(detail, lastRun.error);
    detail.classList.add("is-bad");
    return;
  }
  detail.classList.remove("is-bad");
  if (summary) {
    setTextIfChanged(
      detail,
      summary.failed_files ? `${summary.failed_files} 个文件处理失败` : "全部处理完成",
    );
  } else {
    setTextIfChanged(detail, lastRun.status === "running" ? "正在扫描本地录像" : "—");
  }
}

function render(value) {
  cloud = value;
  const status = archiveStatus(cloud);
  const badge = document.querySelector("#archive-status");
  badge.className = `pill pill-lg tone-${status.tone}`;
  badge.innerHTML = `<i class="dot"></i>${escapeHtml(status.label)}`;

  runButton.disabled = cloud.running || !cloud.enabled;
  runButton.innerHTML = cloud.running
    ? '<span class="spinner"></span>上传中'
    : `${icon("upload", "ic-sm")}立即归档`;
  runButton.title = cloud.enabled ? "" : "请点击存储目标配置并启用网盘";

  setTextIfChanged(
    document.querySelector("#archive-status-time"),
    cloud.running
      ? `开始于 ${formatTime(cloud.last_run?.started_at)}`
      : cloud.last_run
        ? `最近执行 ${formatTime(cloud.last_run.finished_at || cloud.last_run.started_at)}`
        : "尚未执行过归档",
  );
  setTextIfChanged(
    document.querySelector("#next-run-time"),
    cloud.schedule.next_run_at ? formatTime(cloud.schedule.next_run_at) : "—",
  );
  setTextIfChanged(document.querySelector("#schedule-hour"), `每天 ${String(cloud.schedule.hour).padStart(2, "0")}:00`);
  setTextIfChanged(document.querySelector("#stable-age"), `${cloud.schedule.min_age_minutes} 分钟`);

  renderProvider("quark", cloud.quark);
  renderProvider("wopan", cloud.wopan);
  renderLastRun(cloud.last_run);
}

function updateProviderCredentialHint(kind) {
  const provider = cloud?.[kind];
  if (!provider) return;
  const configured = credentialConfigured(kind, provider);
  providerCredentialHint.textContent = configured ? "账号已登录，可作为归档目标使用" : "尚未登录，扫码后即可上传";
  providerCredentialHint.classList.toggle("is-ok", configured);
}

function populateProviderForm(kind) {
  const provider = cloud?.[kind];
  if (!provider) return;
  const meta = providerMeta[kind];
  providerForm.reset();
  providerForm.elements.enabled.checked = provider.enabled;
  providerForm.elements.quark_root_id.value = cloud.quark.root_id;
  providerForm.elements.quark_upload_path.value = cloud.quark.upload_path;
  providerForm.elements.wopan_root_id.value = cloud.wopan.root_id;
  providerForm.elements.wopan_family_id.value = cloud.wopan.family_id;
  providerForm.elements.wopan_upload_path.value = cloud.wopan.upload_path;
  providerForm.querySelectorAll("details").forEach((details) => {
    details.open = false;
  });
  providerForm.querySelectorAll("[data-provider-fields]").forEach((section) => {
    section.classList.toggle("hidden", section.dataset.providerFields !== kind);
  });

  providerLogo.className = `provider-logo ${kind}`;
  providerLogo.textContent = kind === "quark" ? "夸" : "联";
  providerTitle.textContent = `配置${meta.name}`;
  providerDescription.textContent = meta.description;
  providerLoginDescription.textContent = meta.loginDescription;
  providerLoginButton.dataset.cloudLogin = kind;
  updateProviderCredentialHint(kind);
}

function openProviderConfig(kind) {
  if (!cloud || !providerMeta[kind]) return;
  activeProvider = kind;
  populateProviderForm(kind);
  if (!providerDialog.open) providerDialog.showModal();
}

function providerPayload() {
  const fields = providerForm.elements;
  const text = (name) => String(fields[name].value || "").trim();
  const secret = (name) => text(name) || null;
  if (activeProvider === "quark") {
    return {
      enabled: fields.enabled.checked,
      cookie: secret("quark_cookie"),
      clear_cookie: fields.quark_clear_cookie.checked,
      root_id: text("quark_root_id"),
      upload_path: text("quark_upload_path"),
    };
  }
  return {
    enabled: fields.enabled.checked,
    access_token: secret("wopan_access_token"),
    refresh_token: secret("wopan_refresh_token"),
    clear_tokens: fields.wopan_clear_tokens.checked,
    root_id: text("wopan_root_id"),
    family_id: text("wopan_family_id"),
    upload_path: text("wopan_upload_path"),
  };
}

async function saveProviderConfig(event) {
  event.preventDefault();
  if (!activeProvider || !providerForm.reportValidity()) return;
  const provider = activeProvider;
  providerSaveButton.disabled = true;
  providerSaveButton.textContent = "保存中…";
  try {
    render(
      await api(`/api/cloud/archive/providers/${provider}`, {
        method: "PUT",
        body: JSON.stringify(providerPayload()),
      }),
    );
    toast(`${providerMeta[provider].name}设置已保存`, "success");
    providerDialog.close();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    providerSaveButton.disabled = false;
    providerSaveButton.textContent = "保存网盘";
  }
}

function clearLoginPoll() {
  if (loginPollTimer) window.clearTimeout(loginPollTimer);
  loginPollTimer = null;
}

function setLoginButtonDisabled(disabled) {
  providerLoginButton.disabled = disabled;
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
  const provider = providerMeta[value.provider];
  loginDialog.querySelector("#cloud-login-title").textContent = `${provider.name}登录`;
  loginStatus.textContent = value.message;
  loginTip.textContent = `使用 ${provider.app} 扫描并确认`;
  if (value.qr_image && loginImage.src !== value.qr_image) loginImage.src = value.qr_image;
  const terminal = ["success", "expired", "error", "cancelled"].includes(value.state);
  loginRefresh.classList.toggle("hidden", !["expired", "error"].includes(value.state));
  loginDialog.querySelector(".qr-frame")?.classList.toggle("is-dimmed", terminal && value.state !== "success");
  loginStatus.classList.toggle("is-ok", value.state === "success");
}

async function finishSuccessfulLogin(value) {
  clearLoginPoll();
  const completed = activeLogin;
  activeLogin = null;
  try {
    render(await api("/api/cloud/archive"));
    if (providerDialog.open && activeProvider === value.provider) {
      if (value.provider === "quark") {
        providerForm.elements.quark_cookie.value = "";
        providerForm.elements.quark_clear_cookie.checked = false;
      } else {
        providerForm.elements.wopan_access_token.value = "";
        providerForm.elements.wopan_refresh_token.value = "";
        providerForm.elements.wopan_clear_tokens.checked = false;
      }
      updateProviderCredentialHint(value.provider);
    }
    toast(`${providerMeta[value.provider].name}登录成功`, "success");
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
  if (!provider || !providerMeta[provider]) return;
  await cancelActiveLogin();
  const generation = ++loginGeneration;
  setLoginButtonDisabled(true);
  loginDialog.querySelector("#cloud-login-title").textContent = `${providerMeta[provider].name}登录`;
  loginStatus.textContent = "正在生成二维码";
  loginStatus.classList.remove("is-ok");
  loginTip.textContent = "";
  loginRefresh.classList.add("hidden");
  loginImage.removeAttribute("src");
  loginDialog.querySelector(".qr-frame")?.classList.remove("is-dimmed");
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
    setLoginButtonDisabled(false);
  }
}

async function load({ quiet = false } = {}) {
  if (loading) return;
  loading = true;
  if (!quiet) setBusy(refreshButton, true);
  try {
    render(await api("/api/cloud/archive"));
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法读取归档状态：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    loading = false;
    setBusy(refreshButton, false);
  }
}

async function runArchive() {
  if (!cloud?.enabled || cloud.running) return;
  const proceed = await confirmAction({
    title: "立即执行归档",
    message: "将扫描本地录像并上传到已启用的网盘，上传成功的本地文件会被删除。",
    confirmLabel: "开始归档",
    tone: "warn",
  });
  if (!proceed) return;
  runButton.disabled = true;
  runButton.innerHTML = '<span class="spinner"></span>正在启动';
  try {
    render(await api("/api/cloud/archive/run", { method: "POST" }));
    toast("归档任务已启动", "success");
  } catch (error) {
    toast(error.message, "error");
    await load({ quiet: true });
  }
}

refreshButton?.addEventListener("click", () => load());
runButton?.addEventListener("click", runArchive);
document.querySelectorAll("[data-provider-configure]").forEach((button) => {
  button.addEventListener("click", () => openProviderConfig(button.dataset.providerConfigure));
});
providerForm.addEventListener("submit", saveProviderConfig);
providerCloseButton.addEventListener("click", () => providerDialog.close());
providerCancelButton.addEventListener("click", () => providerDialog.close());
providerDialog.addEventListener("close", () => {
  activeProvider = null;
  providerForm.reset();
});
providerLoginButton.addEventListener("click", () => startCloudLogin(activeProvider));
loginRefresh.addEventListener("click", () => startCloudLogin(activeLogin?.provider || activeProvider));
loginClose.addEventListener("click", () => loginDialog.close());
loginDialog.addEventListener("close", () => {
  cancelActiveLogin();
});

bootstrap(load);
