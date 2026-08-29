import { api, confirmAction, icon, toast } from "/static/ui.js?v=20260882";

const FALLBACK_RECORDING_DEFAULTS = {
  output_format: "ts",
  segment_seconds: 1800,
  segment_count: 0,
};

function taskDialogMarkup() {
  return `
    <dialog id="task-dialog" class="modal" aria-labelledby="dialog-title">
      <form id="task-form" method="dialog">
        <header class="modal-head">
          <div class="modal-heading">
            <h2 id="dialog-title">抖音 | 快手 | 哔哩哔哩</h2>
          </div>
          <button class="btn btn-icon btn-ghost" type="button" data-task-dialog-close aria-label="关闭">
            <svg class="ic" aria-hidden="true"><use href="#ic-close" /></svg>
          </button>
        </header>

        <div class="modal-body">
          <label class="field span-2">
            <span class="field-label">直播间地址<b aria-hidden="true">*</b></span>
            <span class="input-row">
              <input id="task-url" class="input" name="url" type="text" required maxlength="1000" inputmode="url"
                placeholder="粘贴直播间链接或平台分享文案" autocomplete="off" />
              <button id="inspect-button" class="btn btn-soft" type="button">检测</button>
            </span>
          </label>
          <div id="inspect-result" class="inspect hidden" role="status"></div>

          <div class="form-grid">
            <label class="field span-2">
              <span class="field-label">任务名称<em>选填</em></span>
              <input class="input" name="label" type="text" maxlength="80" placeholder="留空时使用主播昵称" />
            </label>
            <label class="field">
              <span class="field-label">画质</span>
              <select class="input" name="quality">
                <option value="OD">原画</option>
                <option value="UHD">超清</option>
                <option value="HD">高清</option>
                <option value="SD">标清</option>
                <option value="LD">流畅</option>
              </select>
            </label>
            <label class="field">
              <span class="field-label">保存格式</span>
              <select class="input" name="output_format">
                <option value="ts">TS</option>
                <option value="mp4">MP4</option>
                <option value="mkv">MKV</option>
                <option value="flv">FLV</option>
              </select>
            </label>
          </div>

          <div class="form-grid">
            <label class="field">
              <span class="field-label">录制段数</span>
              <span class="suffix"><input class="input" name="segment_count" type="number" min="0" max="10000" step="1" value="0" /><em>段</em></span>
            </label>
            <label class="field">
              <span class="field-label">分段时长</span>
              <span class="suffix"><input class="input" name="segment_minutes" type="number" min="0" max="1440" step="any" value="30" /><em>分钟</em></span>
            </label>
            <label class="field">
              <span class="field-label">直播源</span>
              <select class="input" name="source">
                <option value="auto">自动选择</option>
                <option value="flv">优先 FLV</option>
                <option value="hls">使用 HLS</option>
              </select>
            </label>
            <label class="field">
              <span class="field-label">检查间隔</span>
              <span class="suffix"><input class="input" name="interval_seconds" type="number" min="10" max="86400" step="1" value="60" /><em>秒</em></span>
            </label>
          </div>

          <div class="option-row">
            <label id="monitor-field" class="option">
              <input name="monitor" type="checkbox" aria-describedby="monitor-hint segment-hint" checked />
              <span class="switch" aria-hidden="true"></span>
              <span class="option-text"><strong>持续值守</strong></span>
            </label>
            <label id="auto-start-field" class="option">
              <input name="auto_start" type="checkbox" checked />
              <span class="switch" aria-hidden="true"></span>
              <span class="option-text"><strong>立即启动</strong></span>
            </label>
          </div>
          <p id="monitor-hint" class="field-hint">
            <svg class="ic ic-sm" aria-hidden="true"><use href="#ic-info" /></svg>
            <span>持续值守会一直监听直播状态，只要开播就录制</span>
          </p>
          <p id="segment-hint" class="field-hint">
            <svg class="ic ic-sm" aria-hidden="true"><use href="#ic-alert" /></svg>
            <span>配置录制段数，持续值守功能将禁用</span>
          </p>
        </div>

        <footer class="modal-foot">
          <button class="btn btn-soft" type="button" data-task-dialog-close>取消</button>
          <button id="task-submit" class="btn btn-primary" type="submit">创建任务</button>
        </footer>
      </form>
    </dialog>`;
}

function taskTitle(task) {
  return task.label || task.anchor_name || "未命名任务";
}

export function createTaskDialog({ onSaved } = {}) {
  if (document.querySelector("#task-dialog")) {
    throw new Error("任务编辑器已经挂载");
  }
  document.body.insertAdjacentHTML("beforeend", taskDialogMarkup());

  const state = {
    editingTask: null,
    inspection: null,
    recordingDefaults: { ...FALLBACK_RECORDING_DEFAULTS },
    defaultsPromise: null,
  };
  const dialog = document.querySelector("#task-dialog");
  const elements = {
    dialog,
    title: dialog.querySelector("#dialog-title"),
    form: dialog.querySelector("#task-form"),
    submit: dialog.querySelector("#task-submit"),
    autoStartField: dialog.querySelector("#auto-start-field"),
    monitorField: dialog.querySelector("#monitor-field"),
    inspectButton: dialog.querySelector("#inspect-button"),
    inspectResult: dialog.querySelector("#inspect-result"),
    url: dialog.querySelector("#task-url"),
  };

  function prepare() {
    if (!state.defaultsPromise) {
      state.defaultsPromise = api("/api/settings/recording-defaults")
        .then((defaults) => {
          state.recordingDefaults = defaults;
          return defaults;
        })
        .catch((error) => {
          state.defaultsPromise = null;
          throw error;
        });
    }
    return state.defaultsPromise;
  }

  function show() {
    elements.dialog.showModal();
    window.setTimeout(() => elements.url.focus(), 60);
  }

  function syncMonitorAvailability() {
    const monitor = elements.form.elements.monitor;
    const hasSegmentLimit = Number(elements.form.elements.segment_count.value) > 0;
    monitor.disabled = hasSegmentLimit;
    if (hasSegmentLimit) monitor.checked = false;
    elements.monitorField.classList.toggle("is-disabled", hasSegmentLimit);
  }

  function validateSegmentSettings() {
    const segmentMinutes = Number(elements.form.elements.segment_minutes.value);
    const segmentCount = Number(elements.form.elements.segment_count.value);
    elements.form.elements.segment_count.setCustomValidity(
      segmentCount > 0 && segmentMinutes <= 0 ? "设置段数时，分段时长必须大于 0" : "",
    );
    syncMonitorAvailability();
  }

  function clearInspection() {
    state.inspection = null;
    elements.inspectResult.className = "inspect hidden";
    elements.inspectResult.innerHTML = "";
  }

  function reset() {
    elements.form.reset();
    elements.form.elements.segment_count.setCustomValidity("");
    clearInspection();
    syncMonitorAvailability();
  }

  function openCreate() {
    state.editingTask = null;
    reset();
    const form = elements.form.elements;
    form.output_format.value = state.recordingDefaults.output_format;
    form.segment_minutes.value = String(state.recordingDefaults.segment_seconds / 60);
    form.segment_count.value = String(state.recordingDefaults.segment_count);
    validateSegmentSettings();
    elements.title.textContent = "抖音 | 快手 | 哔哩哔哩";
    elements.submit.textContent = "创建任务";
    elements.autoStartField.classList.remove("hidden");
    show();
  }

  function openEdit(task) {
    state.editingTask = task;
    reset();
    const form = elements.form.elements;
    form.url.value = task.url;
    form.label.value = task.label || "";
    form.quality.value = task.quality;
    form.output_format.value = task.output_format;
    form.source.value = task.source;
    form.segment_minutes.value = String(task.segment_seconds / 60);
    form.segment_count.value = String(task.segment_count);
    form.monitor.checked = task.monitor;
    form.interval_seconds.value = String(task.interval_seconds);
    syncMonitorAvailability();
    elements.title.textContent = "编辑任务";
    elements.submit.textContent = "保存更改";
    elements.autoStartField.classList.add("hidden");
    show();
  }

  function close() {
    elements.dialog.close();
    state.editingTask = null;
  }

  function renderInspection({ tone, glyph, title, detail }) {
    elements.inspectResult.className = `inspect is-${tone}`;
    elements.inspectResult.innerHTML = `
      <span class="inspect-icon">${icon(glyph, "ic-sm")}</span>
      <span class="inspect-copy"><strong></strong><small></small></span>`;
    elements.inspectResult.querySelector("strong").textContent = title;
    elements.inspectResult.querySelector("small").textContent = detail;
  }

  async function inspectRoom() {
    const url = elements.url.value.trim();
    if (!url) {
      elements.url.reportValidity();
      return;
    }
    const quality = elements.form.elements.quality.value;
    state.inspection = null;
    elements.inspectButton.disabled = true;
    elements.inspectButton.textContent = "检测中";
    renderInspection({ tone: "loading", glyph: "clock", title: "正在检测直播间", detail: "识别平台并读取直播状态…" });
    try {
      const result = await api("/api/inspect", {
        method: "POST",
        body: JSON.stringify({ url, quality }),
      });
      if (elements.url.value.trim() !== url || elements.form.elements.quality.value !== quality) return;
      state.inspection = { token: result.inspection_token, url, quality };
      const sources = [result.has_flv && "FLV", result.has_hls && "HLS"].filter(Boolean).join(" / ");
      renderInspection({
        tone: "success",
        glyph: result.is_live ? "record" : "checkCircle",
        title: result.anchor_name || "未知主播",
        detail: [result.platform, result.is_live ? "直播中" : "当前未开播", result.title, sources && `可用源 ${sources}`]
          .filter(Boolean)
          .join(" · "),
      });
    } catch (error) {
      if (elements.url.value.trim() === url && elements.form.elements.quality.value === quality) {
        renderInspection({ tone: "error", glyph: "alert", title: "检测失败", detail: error.message });
      }
    } finally {
      elements.inspectButton.disabled = false;
      elements.inspectButton.textContent = "检测";
    }
  }

  function buildPayload() {
    const data = new FormData(elements.form);
    const segmentCount = Number(data.get("segment_count"));
    return {
      url: String(data.get("url") || "").trim(),
      label: String(data.get("label") || "").trim() || null,
      quality: data.get("quality"),
      output_format: data.get("output_format"),
      source: data.get("source"),
      segment_seconds: Math.round(Number(data.get("segment_minutes")) * 60),
      segment_count: segmentCount,
      monitor: segmentCount === 0 && data.get("monitor") === "on",
      interval_seconds: Number(data.get("interval_seconds")),
    };
  }

  async function confirmRecordingRestart(task, payload) {
    if (!task || task.status !== "recording") return true;
    const restartsRecording = (
      payload.url !== task.url
      || payload.quality !== task.quality
      || payload.output_format !== task.output_format
      || payload.source !== task.source
      || payload.segment_seconds !== task.segment_seconds
      || payload.segment_count !== task.segment_count
      || payload.monitor !== task.monitor
      || payload.interval_seconds !== task.interval_seconds
    );
    if (!restartsRecording) return true;
    return confirmAction({
      title: "保存后将重新开始录制",
      message: `“${taskTitle(task)}”正在录制中，修改录制参数会中断当前片段并立即重新开始。`,
      confirmLabel: "保存并重启",
      tone: "warn",
    });
  }

  async function submit(event) {
    event.preventDefault();
    validateSegmentSettings();
    if (!elements.form.reportValidity()) return;

    const editingTask = state.editingTask;
    const payload = buildPayload();
    if (!await confirmRecordingRestart(editingTask, payload)) return;
    if (!editingTask) {
      const data = new FormData(elements.form);
      payload.auto_start = data.get("auto_start") === "on";
      const inspection = state.inspection;
      if (payload.auto_start && inspection?.url === payload.url && inspection.quality === payload.quality) {
        payload.inspection_token = inspection.token;
      }
    }

    const originalLabel = editingTask ? "保存更改" : "创建任务";
    elements.submit.disabled = true;
    elements.submit.textContent = editingTask ? "保存中…" : "创建中…";
    try {
      if (editingTask) {
        await api(`/api/tasks/${editingTask.id}`, { method: "PATCH", body: JSON.stringify(payload) });
      } else {
        await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
      }
    } catch (error) {
      toast(error.message, "error");
      return;
    } finally {
      elements.submit.disabled = false;
      elements.submit.textContent = originalLabel;
    }

    close();
    toast(editingTask ? "任务已更新" : "任务已创建", "success");
    try {
      await onSaved?.();
    } catch (_error) {
      // The owning page reports refresh failures without turning a successful
      // task mutation into a misleading form submission error.
    }
  }

  elements.dialog.querySelectorAll("[data-task-dialog-close]").forEach((button) => button.addEventListener("click", close));
  elements.inspectButton.addEventListener("click", inspectRoom);
  elements.form.addEventListener("submit", submit);
  elements.form.elements.url.addEventListener("input", clearInspection);
  elements.form.elements.quality.addEventListener("change", clearInspection);
  elements.form.elements.segment_minutes.addEventListener("input", validateSegmentSettings);
  elements.form.elements.segment_count.addEventListener("input", validateSegmentSettings);
  elements.url.addEventListener("paste", () => {
    window.setTimeout(() => {
      if (elements.url.value.trim()) inspectRoom();
    }, 120);
  });
  elements.dialog.addEventListener("click", (event) => {
    if (event.target === elements.dialog) close();
  });
  elements.dialog.addEventListener("close", () => {
    state.editingTask = null;
  });

  return {
    prepare,
    openCreate,
    openEdit,
    isOpen: () => elements.dialog.open,
  };
}
