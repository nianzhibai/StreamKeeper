import {
  api,
  bootstrap,
  clearPageError,
  confirmAction,
  showPageError,
  toast,
} from "/static/ui.js?v=20260882";

const form = document.querySelector("#archive-schedule-form");
const accountCard = document.querySelector("#account-settings-card");
const accountForm = document.querySelector("#account-settings-form");
const accountSaveButton = document.querySelector("#account-save-button");
const saveButton = document.querySelector("#schedule-save-button");
const saveBar = document.querySelector(".save-bar");
const saveHint = document.querySelector("[data-save-hint]");
const resetButton = document.querySelector("[data-reset-form]");
const scheduledField = document.querySelector("[data-scheduled-field]");

let cloud = null;
let recordingDefaults = null;
let recordingRuntime = null;
let accountUsername = "";
let accountRedirecting = false;
let pristine = "";

function formSignature() {
  return JSON.stringify([...new FormData(form).entries()]);
}

function updateDirtyState() {
  const dirty = formSignature() !== pristine;
  saveBar.classList.toggle("is-dirty", dirty);
  saveButton.disabled = !dirty;
  resetButton.disabled = !dirty;
  saveHint.textContent = dirty ? "有未保存的更改" : "所有更改已保存";
}

function accountFormDirty() {
  const fields = accountForm.elements;
  return (
    fields.username.value.trim() !== accountUsername
    || Boolean(fields.new_password.value)
  );
}

function populateAccount(session) {
  if (!session.csrf_token) {
    accountCard.classList.add("hidden");
    return;
  }
  accountCard.classList.remove("hidden");
  accountUsername = session.username;
  accountForm.reset();
  accountForm.elements.username.value = accountUsername;
  accountSaveButton.disabled = false;
}

function syncModeFields() {
  const scheduled = form.elements.upload_mode.value === "scheduled";
  form.elements.upload_hour.disabled = !scheduled;
  scheduledField.setAttribute("aria-disabled", String(!scheduled));
}

function validateRecordingSettings() {
  const segmentMinutes = Number(form.elements.recording_segment_minutes.value);
  const segmentCount = Number(form.elements.recording_segment_count.value);
  form.elements.recording_segment_count.setCustomValidity(
    segmentCount > 0 && segmentMinutes <= 0 ? "设置录制段数时，分段时长必须大于 0" : "",
  );
}

function populate(cloudValue, recordingValue, runtimeValue) {
  cloud = cloudValue;
  recordingDefaults = recordingValue;
  recordingRuntime = runtimeValue;
  form.reset();
  form.elements.max_concurrent_recordings.value = String(recordingRuntime.max_concurrent_recordings);
  form.elements.recording_output_format.value = recordingDefaults.output_format;
  form.elements.recording_segment_minutes.value = String(recordingDefaults.segment_seconds / 60);
  form.elements.recording_segment_count.value = String(recordingDefaults.segment_count);
  form.elements.upload_mode.value = cloud.schedule.mode || "scheduled";
  form.elements.upload_hour.value = String(cloud.schedule.hour);
  form.elements.upload_min_age_minutes.value = String(cloud.schedule.min_age_minutes);
  form.elements.upload_timeout_seconds.value = String(cloud.schedule.timeout_seconds);
  syncModeFields();
  validateRecordingSettings();
  pristine = formSignature();
  updateDirtyState();
}

async function load() {
  try {
    const [cloudValue, recordingValue, runtimeValue, accountSession] = await Promise.all([
      api("/api/cloud/archive"),
      api("/api/settings/recording-defaults"),
      api("/api/settings/recording-runtime"),
      api("/api/auth/session"),
    ]);
    populate(cloudValue, recordingValue, runtimeValue);
    populateAccount(accountSession);
    clearPageError();
  } catch (error) {
    showPageError(`无法读取设置：${error.message}`);
    throw error;
  }
}

async function submitAccount(event) {
  event.preventDefault();
  if (!accountForm.reportValidity()) return;
  const fields = accountForm.elements;
  const username = fields.username.value.trim();
  const newPassword = fields.new_password.value;
  if (username === accountUsername && !newPassword) {
    toast("用户名和密码均未更改", "info");
    return;
  }

  const unsavedWarning = formSignature() !== pristine ? " 当前尚未保存的其他设置会丢失。" : "";
  const confirmed = await confirmAction({
    title: "更新管理员账号",
    message: `更新后，包括当前浏览器在内的所有登录会话都会立即失效。${unsavedWarning}`,
    confirmLabel: "更新并退出",
  });
  if (!confirmed) return;

  accountSaveButton.disabled = true;
  accountSaveButton.textContent = "正在更新…";
  try {
    const result = await api("/api/settings/account", {
      method: "PUT",
      body: JSON.stringify({
        username,
        new_password: newPassword || null,
      }),
    });
    accountUsername = result.username;
    accountForm.reset();
    accountForm.elements.username.value = accountUsername;
    accountRedirecting = true;
    toast(`管理员账号已更新为 ${accountUsername}，请重新登录`, "success");
    window.setTimeout(() => window.location.replace("/login"), 700);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    accountSaveButton.disabled = false;
    accountSaveButton.textContent = "更新账号";
  }
}

async function submit(event) {
  event.preventDefault();
  validateRecordingSettings();
  if (!form.reportValidity()) return;
  const fields = form.elements;
  const schedulePayload = {
    mode: fields.upload_mode.value,
    hour: Number(fields.upload_hour.value),
    min_age_minutes: Number(fields.upload_min_age_minutes.value),
    timeout_seconds: Number(fields.upload_timeout_seconds.value),
  };
  saveButton.disabled = true;
  const recordingPayload = {
    output_format: fields.recording_output_format.value,
    segment_seconds: Math.round(Number(fields.recording_segment_minutes.value) * 60),
    segment_count: Number(fields.recording_segment_count.value),
  };
  const runtimePayload = {
    max_concurrent_recordings: Number(fields.max_concurrent_recordings.value),
  };
  const scheduleChanged = (
    schedulePayload.mode !== cloud.schedule.mode
    || schedulePayload.hour !== cloud.schedule.hour
    || schedulePayload.min_age_minutes !== cloud.schedule.min_age_minutes
    || schedulePayload.timeout_seconds !== cloud.schedule.timeout_seconds
  );
  const recordingChanged = (
    recordingPayload.output_format !== recordingDefaults.output_format
    || recordingPayload.segment_seconds !== recordingDefaults.segment_seconds
    || recordingPayload.segment_count !== recordingDefaults.segment_count
  );
  const runtimeChanged = (
    runtimePayload.max_concurrent_recordings !== recordingRuntime.max_concurrent_recordings
  );
  saveButton.textContent = "保存中…";
  try {
    const [cloudValue, recordingValue, runtimeValue] = await Promise.all([
      scheduleChanged
        ? api("/api/cloud/archive/schedule", { method: "PUT", body: JSON.stringify(schedulePayload) })
        : Promise.resolve(cloud),
      recordingChanged
        ? api("/api/settings/recording-defaults", { method: "PUT", body: JSON.stringify(recordingPayload) })
        : Promise.resolve(recordingDefaults),
      runtimeChanged
        ? api("/api/settings/recording-runtime", { method: "PUT", body: JSON.stringify(runtimePayload) })
        : Promise.resolve(recordingRuntime),
    ]);
    populate(cloudValue, recordingValue, runtimeValue);
    toast("设置已保存", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    saveButton.textContent = "保存设置";
    updateDirtyState();
  }
}

accountForm.addEventListener("submit", submitAccount);
form.addEventListener("submit", submit);
form.addEventListener("input", () => {
  validateRecordingSettings();
  updateDirtyState();
});
form.addEventListener("change", () => {
  syncModeFields();
  validateRecordingSettings();
  updateDirtyState();
});
resetButton.addEventListener("click", () => {
  if (cloud && recordingDefaults && recordingRuntime) populate(cloud, recordingDefaults, recordingRuntime);
});
window.addEventListener("beforeunload", (event) => {
  if (accountRedirecting || (formSignature() === pristine && !accountFormDirty())) return;
  event.preventDefault();
  event.returnValue = "";
});

bootstrap(load, { interval: 0 });
