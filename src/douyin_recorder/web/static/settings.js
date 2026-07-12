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
let cloud = null;

function updateProviderAppearance() {
  document.querySelector("#quark-settings")?.classList.toggle("is-enabled", form.elements.quark_enabled.checked);
  document.querySelector("#wopan-settings")?.classList.toggle("is-enabled", form.elements.wopan_enabled.checked);
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

  document.querySelector("#quark-credential-hint").textContent = cloud.quark.credential_configured
    ? "Cookie 已保存"
    : "未保存 Cookie";
  const tokens = [
    cloud.wopan.access_token_configured && "Access Token",
    cloud.wopan.refresh_token_configured && "Refresh Token",
  ].filter(Boolean);
  document.querySelector("#wopan-credential-hint").textContent = tokens.length
    ? `${tokens.join(" / ")} 已保存`
    : "未保存 Token";
  updateProviderAppearance();
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
bootstrap(load, { interval: 0 });
