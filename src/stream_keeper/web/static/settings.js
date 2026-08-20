import {
  api,
  bootstrap,
  clearPageError,
  setHealth,
  showPageError,
  toast,
} from "/static/ui.js?v=20260814";

const form = document.querySelector("#archive-schedule-form");
const saveButton = document.querySelector("#schedule-save-button");
const saveBar = document.querySelector(".save-bar");
const saveHint = document.querySelector("[data-save-hint]");
const resetButton = document.querySelector("[data-reset-form]");
const scheduledField = document.querySelector("[data-scheduled-field]");
const modeHint = document.querySelector("[data-mode-hint]");

let cloud = null;
let recordingDefaults = null;
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

function syncModeFields() {
  const scheduled = form.elements.upload_mode.value === "scheduled";
  form.elements.upload_hour.disabled = !scheduled;
  scheduledField.setAttribute("aria-disabled", String(!scheduled));
  modeHint.textContent = scheduled
    ? "定时扫描时，只有最后修改时间超过「文件稳定时间」的录像才会被上传，避免传走正在写入的片段。"
    : "录制进程正常结束后，该次生成的所有视频分段会立即上传；文件稳定时间仍用于手动归档。";
}

function validateRecordingSettings() {
  const segmentSeconds = Number(form.elements.recording_segment_seconds.value);
  const segmentCount = Number(form.elements.recording_segment_count.value);
  form.elements.recording_segment_count.setCustomValidity(
    segmentCount > 0 && segmentSeconds <= 0 ? "设置录制段数时，分段时长必须大于 0" : "",
  );
}

function populate(cloudValue, recordingValue) {
  cloud = cloudValue;
  recordingDefaults = recordingValue;
  form.reset();
  form.elements.recording_output_format.value = recordingDefaults.output_format;
  form.elements.recording_segment_seconds.value = String(recordingDefaults.segment_seconds);
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
    const [cloudValue, recordingValue] = await Promise.all([
      api("/api/cloud/archive"),
      api("/api/settings/recording-defaults"),
    ]);
    populate(cloudValue, recordingValue);
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
    segment_seconds: Number(fields.recording_segment_seconds.value),
    segment_count: Number(fields.recording_segment_count.value),
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
  saveButton.textContent = "保存中…";
  try {
    const [cloudValue, recordingValue] = await Promise.all([
      scheduleChanged
        ? api("/api/cloud/archive/schedule", { method: "PUT", body: JSON.stringify(schedulePayload) })
        : Promise.resolve(cloud),
      recordingChanged
        ? api("/api/settings/recording-defaults", { method: "PUT", body: JSON.stringify(recordingPayload) })
        : Promise.resolve(recordingDefaults),
    ]);
    populate(cloudValue, recordingValue);
    toast("设置已保存", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    saveButton.textContent = "保存设置";
    updateDirtyState();
  }
}

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
  if (cloud && recordingDefaults) populate(cloud, recordingDefaults);
});
window.addEventListener("beforeunload", (event) => {
  if (formSignature() === pristine) return;
  event.preventDefault();
  event.returnValue = "";
});

bootstrap(load, { interval: 0 });
