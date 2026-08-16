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

let cloud = null;
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

function populate(value) {
  cloud = value;
  form.reset();
  form.elements.upload_hour.value = String(cloud.schedule.hour);
  form.elements.upload_min_age_minutes.value = String(cloud.schedule.min_age_minutes);
  form.elements.upload_timeout_seconds.value = String(cloud.schedule.timeout_seconds);
  pristine = formSignature();
  updateDirtyState();
}

async function load() {
  try {
    populate(await api("/api/cloud/archive"));
    clearPageError();
    setHealth(true);
  } catch (error) {
    setHealth(false);
    showPageError(`无法读取归档计划：${error.message}`);
    throw error;
  }
}

async function submit(event) {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const fields = form.elements;
  const payload = {
    hour: Number(fields.upload_hour.value),
    min_age_minutes: Number(fields.upload_min_age_minutes.value),
    timeout_seconds: Number(fields.upload_timeout_seconds.value),
  };
  saveButton.disabled = true;
  saveButton.textContent = "保存中…";
  try {
    populate(await api("/api/cloud/archive/schedule", { method: "PUT", body: JSON.stringify(payload) }));
    toast("归档计划已保存", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    saveButton.textContent = "保存计划";
    updateDirtyState();
  }
}

form.addEventListener("submit", submit);
form.addEventListener("input", updateDirtyState);
form.addEventListener("change", updateDirtyState);
resetButton.addEventListener("click", () => {
  if (cloud) populate(cloud);
});
window.addEventListener("beforeunload", (event) => {
  if (formSignature() === pristine) return;
  event.preventDefault();
  event.returnValue = "";
});

bootstrap(load, { interval: 0 });
