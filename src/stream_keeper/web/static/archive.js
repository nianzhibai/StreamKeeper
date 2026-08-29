import {
  api,
  bootstrap,
  clearPageError,
  confirmAction,
  escapeHtml,
  formatBytes,
  formatRelative,
  formatTime,
  icon,
  providerIcon,
  providerLogoHtml,
  providerStatus,
  setTextIfChanged,
  showPageError,
  toast,
  toggle,
} from "/static/ui.js?v=20260882";

const runButton = document.querySelector("#cloud-run-button");
const providerDialog = document.querySelector("#provider-config-dialog");
const providerForm = document.querySelector("#provider-config-form");
const providerSaveButton = document.querySelector("#provider-config-save");
const providerCloseButton = document.querySelector("#provider-config-close");
const providerCancelButton = document.querySelector("#provider-config-cancel");
const providerClearButton = document.querySelector("#provider-clear-credentials");
const providerLoginButton = document.querySelector("#provider-login-button");
const providerLoginLabel = document.querySelector("#provider-login-label");
const providerLogo = document.querySelector("#provider-config-logo");
const providerTitle = document.querySelector("#provider-config-title");
const providerAuthRow = document.querySelector(".auth-row");
const loginDialog = document.querySelector("#cloud-login-dialog");
const loginImage = document.querySelector("#cloud-login-image");
const loginStatus = document.querySelector("#cloud-login-status");
const loginTip = document.querySelector("#cloud-login-tip");
const loginRefresh = document.querySelector("#cloud-login-refresh");
const loginClose = document.querySelector("#cloud-login-close");
const baiduOpenListDialog = document.querySelector("#baidu-openlist-dialog");
const baiduOpenListForm = document.querySelector("#baidu-openlist-form");
const baiduOpenListStatus = document.querySelector("#baidu-openlist-status");
const baiduOpenListAuthorize = document.querySelector("#baidu-openlist-authorize");
const baiduOpenListCode = document.querySelector("#baidu-openlist-code");
const baiduOpenListExchange = document.querySelector("#baidu-openlist-exchange");

const providerMeta = {
  quark: {
    name: "夸克网盘",
    app: "夸克 App",
    credentials: ["cookie"],
    options: ["root_id"],
    supportsQr: true,
  },
  wopan: {
    name: "联通云盘",
    app: "联通云盘 App",
    credentials: ["access_token", "refresh_token"],
    options: ["root_id", "family_id"],
    supportsQr: true,
  },
  baidu: {
    name: "百度网盘",
    loginLabel: "在线授权",
    startLogin: startBaiduOpenListLogin,
    supportsQr: false,
    credentials: ["access_token", "refresh_token", "client_id", "client_secret"],
    options: [],
  },
  pan115: {
    name: "115网盘",
    app: "115 App",
    supportsQr: true,
    credentials: ["cookie", "access_token", "refresh_token"],
    options: ["root_id"],
  },
  guangya: {
    name: "光鸭网盘",
    app: "光鸭云盘 App",
    supportsQr: true,
    credentials: ["client_id", "device_id", "access_token", "refresh_token"],
    options: ["root_id"],
  },
};

const targetStatusMeta = {
  pending: { label: "等待", tone: "idle" },
  preparing: { label: "准备中", tone: "info" },
  uploading: { label: "上传中", tone: "live" },
  verifying: { label: "校验中", tone: "info" },
  success: { label: "已确认", tone: "ok" },
  partial: { label: "部分失败", tone: "bad" },
  failed: { label: "失败", tone: "bad" },
  skipped: { label: "未执行", tone: "idle" },
  cancelled: { label: "已取消", tone: "bad" },
};

let cloud = null;
let loading = false;
let activeProvider = null;
let activeLogin = null;
let loginPollTimer = null;
let loginGeneration = 0;

function providerFor(kind) {
  return cloud?.providers?.find((provider) => provider.name === kind) || cloud?.[kind];
}

function credentialConfigured(_kind, provider) {
  return Boolean(provider?.credential_configured);
}

function renderProvider(kind, provider) {
  // The pill covers all three states on its own: 未配置 already means "no credentials",
  // so a separate credential chip would only restate it.
  const status = providerStatus(provider, kind);
  const statusNode = document.querySelector(`#${kind}-status`);
  statusNode.className = `pill tone-${status.tone}`;
  statusNode.innerHTML = `<i class="dot"></i>${escapeHtml(status.label)}`;

  document.querySelector(`#${kind}-configure`)?.classList.toggle("is-enabled", provider.enabled);
}

function renderRunTargets(lastRun) {
  const container = document.querySelector("#run-targets");
  const targets = lastRun?.targets || [];
  if (!targets.length) {
    container.innerHTML = '<p class="run-targets-empty">尚无目标执行记录</p>';
    return;
  }

  container.innerHTML = targets
    .map((target) => {
      const state = targetStatusMeta[target.status] || { label: target.status, tone: "idle" };
      const active = ["preparing", "uploading", "verifying"].includes(target.status);
      const total = Number(target.total_bytes || 0);
      const transferred = Number(target.transferred_bytes || 0);
      const percent = total > 0 ? Math.min(100, Math.max(0, (transferred / total) * 100)) : 0;
      let detail;
      if (active) {
        const file = target.current_file || "正在准备录像";
        detail = total > 0
          ? `${file} · ${formatBytes(transferred)} / ${formatBytes(total)}`
          : file;
      } else if (target.verified_files || target.failed_files) {
        detail = `确认 ${target.verified_files} 个 · 新上传 ${target.uploaded_copies} 个 · 失败 ${target.failed_files} 个`;
      } else {
        detail = target.status === "success" ? "本轮没有待归档文件" : "本轮未执行";
      }
      if (target.error) detail += ` · ${target.error}`;
      return `
        <article class="run-target${target.failed_files ? " has-error" : ""}">
          ${providerLogoHtml(target.name)}
          <div class="run-target-info">
            <strong>${escapeHtml(target.label || providerMeta[target.name]?.name || target.name)}</strong>
            <small title="${escapeHtml(detail)}">${escapeHtml(detail)}</small>
            ${active ? `<span class="run-target-progress" aria-hidden="true"><i style="width:${percent.toFixed(1)}%"></i></span>` : ""}
          </div>
          <span class="pill tone-${state.tone}"><i class="dot"></i>${escapeHtml(state.label)}</span>
        </article>`;
    })
    .join("");
}

function renderLastRun(lastRun) {
  renderRunTargets(lastRun);
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
    // Before the first run there is nothing to report, so both lines stay hidden
    // rather than restating that archiving has not happened yet.
    setTextIfChanged(time, "");
    setTextIfChanged(detail, "");
    detail.classList.remove("is-bad");
    toggle(time, false);
    toggle(detail, false);
    return;
  }
  toggle(time, true);
  toggle(detail, true);
  const trigger = {
    manual: "手动触发",
    scheduled: "定时执行",
    recording_completed: "录制完成后执行",
  }[lastRun.trigger] || lastRun.trigger;
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
  const completedMode = cloud.schedule.mode === "recording_completed";
  setTextIfChanged(
    document.querySelector("#next-run-time"),
    cloud.schedule.next_run_at
      ? formatTime(cloud.schedule.next_run_at)
      : cloud.enabled && completedMode
        ? "录制完成后"
        : "—",
  );
  setTextIfChanged(
    document.querySelector("#upload-mode"),
    completedMode ? "录制完成后上传" : `定时上传 · ${String(cloud.schedule.hour).padStart(2, "0")}:00`,
  );
  setTextIfChanged(document.querySelector("#stable-age"), `${cloud.schedule.min_age_minutes} 分钟`);

  Object.keys(providerMeta).forEach((kind) => renderProvider(kind, providerFor(kind)));
  renderLastRun(cloud.last_run);
}

function metaFor(kind) {
  return providerMeta[kind] || {};
}

function populateProviderForm(kind) {
  const provider = providerFor(kind);
  if (!provider) return;
  const meta = providerMeta[kind];
  providerForm.reset();
  providerForm.elements.enabled.checked = provider.enabled;
  const field = (name) => providerForm.elements[`${kind}_${name}`];
  meta.options.forEach((name) => {
    if (field(name)) field(name).value = provider.options?.[name] || "";
  });
  const pathField = field("upload_path");
  if (pathField) pathField.value = provider.upload_path || "";
  const supportsQr = provider.supports_qr_login ?? meta.supportsQr;
  const hasLogin = supportsQr || Boolean(meta.startLogin);
  providerForm.querySelectorAll("details").forEach((details) => {
    details.open = !hasLogin;
  });
  providerForm.querySelectorAll("[data-provider-fields]").forEach((section) => {
    section.classList.toggle("hidden", section.dataset.providerFields !== kind);
  });

  providerLogo.className = `provider-logo ${kind} has-image`;
  providerLogo.innerHTML = `<img class="provider-logo-image" src="${escapeHtml(providerIcon(kind))}" alt="" />`;
  providerTitle.textContent = meta.name;
  providerLoginLabel.textContent = meta.loginLabel || "扫码登录";
  providerLoginButton.dataset.cloudLogin = kind;
  providerAuthRow.classList.toggle("hidden", !hasLogin);
  providerLoginButton.disabled = !hasLogin;
  providerClearButton.disabled = !credentialConfigured(kind, provider);
}

function openProviderConfig(kind) {
  if (!cloud || !providerMeta[kind]) return;
  activeProvider = kind;
  populateProviderForm(kind);
  if (!providerDialog.open) providerDialog.showModal();
}

function providerPayload() {
  const fields = providerForm.elements;
  const meta = providerMeta[activeProvider];
  const text = (name) => String(fields[name]?.value || "").trim();
  const secret = (name) => text(name) || null;
  const credentials = {};
  meta.credentials.forEach((name) => {
    const value = secret(`${activeProvider}_${name}`);
    if (value) credentials[name] = value;
  });
  const options = {};
  meta.options.forEach((name) => {
    options[name] = text(`${activeProvider}_${name}`);
  });
  return {
    enabled: fields.enabled.checked,
    credentials,
    options,
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
      await api(`/api/cloud/archive/providers/${provider}/config`, {
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

async function clearProviderCredentials() {
  const provider = activeProvider;
  if (!provider) return;
  const proceed = await confirmAction({
    title: "清理已配置凭证",
    message: `将删除${providerMeta[provider].name}已保存的登录凭据，并停用该归档目标。`,
    confirmLabel: "清理凭证",
  });
  if (!proceed || activeProvider !== provider) return;
  providerClearButton.disabled = true;
  try {
    render(
      await api(`/api/cloud/archive/providers/${provider}/config`, {
        method: "PUT",
        body: JSON.stringify({ enabled: false, clear_credentials: true }),
      }),
    );
    if (providerDialog.open && activeProvider === provider) populateProviderForm(provider);
    toast(`${providerMeta[provider].name}凭据已清除`, "success");
  } catch (error) {
    toast(error.message, "error");
    providerClearButton.disabled = false;
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
      const meta = providerMeta[value.provider];
      meta.credentials.forEach((name) => {
        const input = providerForm.elements[`${value.provider}_${name}`];
        if (input) input.value = "";
      });
      providerClearButton.disabled = false;
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

async function startBaiduOpenListLogin() {
  setLoginButtonDisabled(true);
  baiduOpenListStatus.textContent = "正在准备百度授权地址…";
  baiduOpenListAuthorize.classList.add("hidden");
  baiduOpenListAuthorize.removeAttribute("href");
  baiduOpenListCode.value = "";
  if (!baiduOpenListDialog.open) baiduOpenListDialog.showModal();
  try {
    const value = await api("/api/cloud/login/baidu/openlist", { method: "POST" });
    baiduOpenListAuthorize.href = value.authorization_url;
    baiduOpenListAuthorize.classList.remove("hidden");
    baiduOpenListStatus.textContent = "打开百度授权页面，授权后复制页面显示的授权码";
  } catch (error) {
    baiduOpenListStatus.textContent = error.message;
    toast(error.message, "error");
  } finally {
    setLoginButtonDisabled(false);
  }
}

async function exchangeBaiduOpenListCode(event) {
  event.preventDefault();
  const authorizationCode = baiduOpenListCode.value.trim();
  if (!authorizationCode) return;
  baiduOpenListExchange.disabled = true;
  baiduOpenListExchange.textContent = "交换中…";
  try {
    render(await api("/api/cloud/login/baidu/openlist/exchange", {
      method: "POST",
      body: JSON.stringify({ authorization_code: authorizationCode }),
    }));
    baiduOpenListCode.value = "";
    baiduOpenListDialog.close();
    toast("百度 OAuth2 授权成功", "success");
  } catch (error) {
    baiduOpenListStatus.textContent = error.message;
    toast(error.message, "error");
  } finally {
    baiduOpenListExchange.disabled = false;
    baiduOpenListExchange.textContent = "保存授权";
  }
}

async function load({ quiet = false } = {}) {
  if (loading) return;
  loading = true;
  try {
    render(await api("/api/cloud/archive"));
    clearPageError();
  } catch (error) {
    showPageError(`无法读取归档状态：${error.message}`);
    if (!quiet) toast(error.message, "error");
    throw error;
  } finally {
    loading = false;
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

runButton?.addEventListener("click", runArchive);
document.querySelectorAll("[data-provider-configure]").forEach((button) => {
  button.addEventListener("click", () => openProviderConfig(button.dataset.providerConfigure));
});
providerForm.addEventListener("submit", saveProviderConfig);
providerClearButton.addEventListener("click", clearProviderCredentials);
providerCloseButton.addEventListener("click", () => providerDialog.close());
providerCancelButton.addEventListener("click", () => providerDialog.close());
providerDialog.addEventListener("close", () => {
  activeProvider = null;
  providerForm.reset();
});
providerLoginButton.addEventListener("click", () => {
  const start = metaFor(activeProvider).startLogin;
  if (start) start();
  else startCloudLogin(activeProvider);
});
loginRefresh.addEventListener("click", () => startCloudLogin(activeLogin?.provider || activeProvider));
loginClose.addEventListener("click", () => loginDialog.close());
loginDialog.addEventListener("close", () => {
  cancelActiveLogin();
});
baiduOpenListForm.addEventListener("submit", exchangeBaiduOpenListCode);
document.querySelector("#baidu-openlist-close").addEventListener("click", () => baiduOpenListDialog.close());
document.querySelector("#baidu-openlist-cancel").addEventListener("click", () => baiduOpenListDialog.close());
baiduOpenListDialog.addEventListener("close", () => {
  baiduOpenListCode.value = "";
  baiduOpenListAuthorize.classList.add("hidden");
  baiduOpenListAuthorize.removeAttribute("href");
});

bootstrap(load);
