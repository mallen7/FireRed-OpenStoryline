const clone = (value) => JSON.parse(JSON.stringify(value));
const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
}

function formatTime(value) {
  const seconds = Math.max(0, Number(value) || 0);
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${(seconds % 60).toFixed(2).padStart(5, "0")}`;
}

function applyEasing(progress, easing) {
  const t = clamp(progress, 0, 1);
  if (easing === "smoothstep") return t * t * (3 - 2 * t);
  if (easing === "ease_in") return t * t;
  if (easing === "ease_out") return 1 - ((1 - t) * (1 - t));
  if (easing === "ease_in_out") return t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2;
  return t;
}

function interpolateAngle(start, end, progress) {
  const delta = ((end - start + 540) % 360) - 180;
  return start + delta * progress;
}

export function sampleTrack(track, timeSeconds) {
  const frames = track?.keyframes || [];
  if (!frames.length) return { yaw: 0, pitch: 0, roll: 0, horizontal_fov: 90 };
  const time = clamp(Number(timeSeconds) || 0, track.source_start_seconds, track.source_end_seconds);
  if (time <= frames[0].time_seconds) return clone(frames[0]);
  if (time >= frames[frames.length - 1].time_seconds) return clone(frames[frames.length - 1]);
  for (let index = 1; index < frames.length; index += 1) {
    const right = frames[index];
    if (time > right.time_seconds) continue;
    const left = frames[index - 1];
    const span = right.time_seconds - left.time_seconds;
    const progress = applyEasing((time - left.time_seconds) / span, left.easing);
    return {
      schema_version: "1.0",
      time_seconds: time,
      yaw: interpolateAngle(left.yaw, right.yaw, progress),
      pitch: left.pitch + (right.pitch - left.pitch) * progress,
      roll: interpolateAngle(left.roll, right.roll, progress),
      horizontal_fov: left.horizontal_fov + (right.horizontal_fov - left.horizontal_fov) * progress,
      easing: left.easing,
      subject_id: progress < 0.5 ? left.subject_id : right.subject_id,
      confidence: progress < 0.5 ? left.confidence : right.confidence,
    };
  }
  return clone(frames[frames.length - 1]);
}

export class CameraTrackEditorModel {
  constructor(track, revision = null) {
    this.track = clone(track);
    this.revision = revision;
    this.history = [];
    this.future = [];
    this.selectedIndex = 0;
    this.dirty = revision == null;
  }

  static create(asset) {
    const duration = Math.max(0.25, Number(asset.duration_seconds) || 0.25);
    const id = `ui-camera-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
    const base = {
      schema_version: "1.0", yaw: 0, pitch: 0, roll: 0,
      horizontal_fov: 90, easing: "smoothstep", subject_id: null, confidence: null,
    };
    return new CameraTrackEditorModel({
      schema_version: "1.0",
      track_id: id,
      asset_id: asset.asset_id,
      name: "Manual camera",
      output_width: 1920,
      output_height: 1080,
      source_start_seconds: 0,
      source_end_seconds: duration,
      keyframes: [
        { ...base, time_seconds: 0 },
        { ...base, time_seconds: duration },
      ],
      created_by: "manual",
      generation_metadata: { origin: "spherical-web-ui" },
    });
  }

  _commit(mutator) {
    this.history.push(clone(this.track));
    if (this.history.length > 100) this.history.shift();
    this.future = [];
    mutator(this.track);
    this.track.keyframes.sort((left, right) => left.time_seconds - right.time_seconds);
    this.selectedIndex = clamp(this.selectedIndex, 0, this.track.keyframes.length - 1);
    this.dirty = true;
  }

  updateTrack(values) { this._commit((track) => Object.assign(track, values)); }

  updateKeyframe(index, values) {
    this.selectedIndex = index;
    this._commit((track) => Object.assign(track.keyframes[index], values));
  }

  addKeyframe(timeSeconds, camera = {}) {
    const time = clamp(Number(timeSeconds), this.track.source_start_seconds, this.track.source_end_seconds);
    if (this.track.keyframes.some((frame) => Math.abs(frame.time_seconds - time) < 0.001)) {
      throw new Error("A keyframe already exists at this time.");
    }
    const sampled = sampleTrack(this.track, time);
    this._commit((track) => track.keyframes.push({
      schema_version: "1.0",
      time_seconds: time,
      yaw: Number(camera.yaw ?? sampled.yaw),
      pitch: Number(camera.pitch ?? sampled.pitch),
      roll: Number(camera.roll ?? sampled.roll),
      horizontal_fov: Number(camera.horizontal_fov ?? sampled.horizontal_fov),
      easing: camera.easing || sampled.easing || "smoothstep",
      subject_id: camera.subject_id ?? sampled.subject_id ?? null,
      confidence: camera.confidence ?? sampled.confidence ?? null,
    }));
    this.selectedIndex = this.track.keyframes.findIndex((frame) => frame.time_seconds === time);
  }

  deleteKeyframe(index) {
    if (this.track.keyframes.length <= 1) throw new Error("A camera track needs at least one keyframe.");
    this._commit((track) => track.keyframes.splice(index, 1));
  }

  undo() {
    if (!this.history.length) return false;
    this.future.push(clone(this.track));
    this.track = this.history.pop();
    this.selectedIndex = clamp(this.selectedIndex, 0, this.track.keyframes.length - 1);
    this.dirty = true;
    return true;
  }

  redo() {
    if (!this.future.length) return false;
    this.history.push(clone(this.track));
    this.track = this.future.pop();
    this.selectedIndex = clamp(this.selectedIndex, 0, this.track.keyframes.length - 1);
    this.dirty = true;
    return true;
  }

  markSaved(revision) {
    this.revision = revision;
    this.dirty = false;
  }
}

export class SphericalWorkspace {
  constructor({ getSessionId, getLanguage } = {}) {
    this.getSessionId = getSessionId || (() => null);
    this.getLanguage = getLanguage || (() => "en");
    this.root = document.querySelector("#sphericalWorkspace");
    this.state = null;
    this.sessionId = null;
    this.assetId = null;
    this.trackId = null;
    this.model = null;
    this.busy = false;
    this.bound = false;
    this.playTimer = 0;
    if (this.root) this.bind();
  }

  $(selector) { return this.root?.querySelector(selector); }

  bind() {
    if (this.bound || !this.root) return;
    this.bound = true;
    this.$("#sphericalCloseBtn")?.addEventListener("click", () => this.close());
    this.$("#sphericalRefreshBtn")?.addEventListener("click", () => this.load());
    this.$("#sphericalAssetSelect")?.addEventListener("change", (event) => this.selectAsset(event.target.value));
    this.$("#sphericalNewTrackBtn")?.addEventListener("click", () => this.newTrack());
    this.$("#sphericalSaveBtn")?.addEventListener("click", () => this.saveTrack());
    this.$("#sphericalUndoBtn")?.addEventListener("click", () => this.changeHistory("undo"));
    this.$("#sphericalRedoBtn")?.addEventListener("click", () => this.changeHistory("redo"));
    this.$("#sphericalAddKeyframeBtn")?.addEventListener("click", () => this.addKeyframe());
    this.$("#sphericalDeleteKeyframeBtn")?.addEventListener("click", () => this.deleteKeyframe());
    this.$("#sphericalPreviewBtn")?.addEventListener("click", () => this.renderTrack("preview"));
    this.$("#sphericalRenderBtn")?.addEventListener("click", () => this.renderTrack("render"));
    this.$("#sphericalPlayBtn")?.addEventListener("click", () => this.togglePlayback());
    this.$("#sphericalTimeSlider")?.addEventListener("input", (event) => this.seek(Number(event.target.value)));
    this.$("#sphericalTrackName")?.addEventListener("change", (event) => {
      if (this.model) this.model.updateTrack({ name: event.target.value.trim() || "Manual camera" });
      this.renderEditor();
    });
    this.$("#sphericalAspect")?.addEventListener("change", (event) => {
      if (!this.model) return;
      const [output_width, output_height] = event.target.value.split("x").map(Number);
      this.model.updateTrack({ output_width, output_height });
      this.renderEditor();
    });
    this.$("#sphericalKeyframeRows")?.addEventListener("click", (event) => {
      const row = event.target.closest("tr[data-keyframe-index]");
      if (!row || !this.model) return;
      this.model.selectedIndex = Number(row.dataset.keyframeIndex);
      this.seek(this.model.track.keyframes[this.model.selectedIndex].time_seconds);
      this.renderEditor();
    });
    this.$("#sphericalKeyframeRows")?.addEventListener("change", (event) => this.editKeyframe(event));
    this.$("#sphericalTrackList")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-track-id]");
      if (button) this.selectTrack(button.dataset.trackId);
    });
    this.$("#sphericalSuggestionsPanel")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-accept-kind]");
      if (button) this.acceptProposal(button.dataset.acceptKind, button.dataset.acceptId);
    });
    this.root.querySelectorAll("[data-spherical-tab]").forEach((button) => {
      button.addEventListener("click", () => this.selectTab(button.dataset.sphericalTab));
    });
    const video = this.$("#sphericalSourceVideo");
    video?.addEventListener("loadedmetadata", () => this.updateScrubber());
    video?.addEventListener("timeupdate", () => this.updateScrubber());
    document.addEventListener("keydown", (event) => {
      if (this.root.classList.contains("hidden")) return;
      if (event.key === "Escape") this.close();
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        this.changeHistory(event.shiftKey ? "redo" : "undo");
      }
    });
  }

  async open() {
    if (!this.root) return;
    this.sessionId = this.getSessionId();
    this.root.classList.remove("hidden");
    this.root.setAttribute("aria-hidden", "false");
    document.body.classList.add("spherical-open");
    await this.load();
    setTimeout(() => this.$("#sphericalScene")?.resize?.(), 0);
  }

  close() {
    if (!this.root) return;
    this.pausePlayback();
    this.root.classList.add("hidden");
    this.root.setAttribute("aria-hidden", "true");
    document.body.classList.remove("spherical-open");
  }

  setSession(sessionId) {
    if (this.sessionId && sessionId !== this.sessionId) this.close();
    this.sessionId = sessionId;
    this.state = null;
    this.model = null;
  }

  setLanguage() {}

  async request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: options.body ? { "Content-Type": "application/json", ...(options.headers || {}) } : options.headers,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload?.detail;
      const message = typeof detail === "string" ? detail : (detail?.message || payload?.message || `HTTP ${response.status}`);
      const error = new Error(message);
      error.status = response.status;
      error.details = detail?.details || null;
      throw error;
    }
    return payload;
  }

  setNotice(kind, message = "") {
    ["Loading", "Error", "Disabled"].forEach((name) => {
      const element = this.$(`#spherical${name}`);
      element?.classList.toggle("hidden", kind.toLowerCase() !== name.toLowerCase());
    });
    const target = this.$(`#spherical${kind[0].toUpperCase()}${kind.slice(1)}`);
    if (target && message) target.textContent = message;
  }

  setBusy(busy, label = "Ready") {
    this.busy = busy;
    this.root?.classList.toggle("is-busy", busy);
    const status = this.$("#sphericalSaveState");
    if (status) status.textContent = label;
    this.root?.querySelectorAll("button").forEach((button) => {
      if (button.id !== "sphericalCloseBtn") button.disabled = busy;
    });
  }

  async load() {
    const sessionId = this.sessionId || this.getSessionId();
    if (!sessionId) return this.setNotice("error", "No active editing session.");
    this.sessionId = sessionId;
    this.$("#sphericalWorkspaceBody")?.classList.add("hidden");
    this.setNotice("loading", "Loading spherical session…");
    try {
      this.state = await this.request(`/api/sessions/${encodeURIComponent(sessionId)}/spherical`);
      if (!this.state.enabled) {
        this.setNotice("disabled", this.state.warnings?.[0] || "Spherical editing is disabled.");
        return;
      }
      this.setNotice("loading", "");
      this.$("#sphericalLoading")?.classList.add("hidden");
      this.$("#sphericalWorkspaceBody")?.classList.remove("hidden");
      this.renderState();
    } catch (error) {
      this.setNotice("error", `Could not load the 360° workspace: ${error.message}`);
    }
  }

  renderState() {
    const assets = this.state?.assets || [];
    const select = this.$("#sphericalAssetSelect");
    if (select) {
      select.innerHTML = assets.length
        ? assets.map((asset) => `<option value="${escapeHtml(asset.asset_id)}">${escapeHtml(asset.master_name)} · ${asset.width}×${asset.height}</option>`).join("")
        : '<option value="">No imported spherical assets</option>';
    }
    const preferred = assets.some((asset) => asset.asset_id === this.assetId) ? this.assetId : assets[0]?.asset_id;
    this.renderWorker();
    this.renderWarnings();
    this.renderInsights();
    if (preferred) this.selectAsset(preferred, { preserveTrack: true });
    else this.showEmptyViewer(true);
  }

  renderWorker() {
    const worker = this.state?.worker || {};
    const element = this.$("#sphericalWorkerStatus");
    if (!element) return;
    element.innerHTML = `
      <div><span class="spherical-status-dot ${worker.status === "ready" ? "ready" : ""}"></span>${escapeHtml(worker.status || "disabled")}</div>
      <dl><dt>Transport</dt><dd>${escapeHtml(worker.transport || "local")}</dd><dt>Raw .insv</dt><dd>${worker.can_decode_insv ? "available" : "unavailable"}</dd><dt>Stitch</dt><dd>${worker.can_stitch ? "available" : "Studio export"}</dd></dl>`;
  }

  renderWarnings() {
    const element = this.$("#sphericalWarnings");
    if (!element) return;
    const warnings = this.state?.warnings || [];
    element.innerHTML = warnings.length
      ? warnings.map((warning) => `<div class="spherical-warning">${escapeHtml(warning)}</div>`).join("")
      : '<div class="spherical-muted">No active warnings.</div>';
  }

  selectAsset(assetId, { preserveTrack = false } = {}) {
    this.assetId = assetId;
    const asset = this.state?.assets?.find((item) => item.asset_id === assetId);
    const select = this.$("#sphericalAssetSelect");
    if (select) select.value = assetId || "";
    if (!asset) return this.showEmptyViewer(true);
    this.showEmptyViewer(false);
    this.loadPanorama(asset);
    const details = this.$("#sphericalAssetDetails");
    if (details) details.innerHTML = `
      <span class="spherical-badge">${escapeHtml(asset.badge)}</span>
      <dl><dt>Source</dt><dd>${escapeHtml(asset.source_names.join(", "))}</dd><dt>Proxy</dt><dd>${escapeHtml(asset.proxy_name || "master")}</dd><dt>Stitch</dt><dd>${escapeHtml(asset.stitch_status.status)}</dd><dt>Duration</dt><dd>${formatTime(asset.duration_seconds)}</dd><dt>Codec</dt><dd>${escapeHtml(asset.source_video_codec || "unknown")}</dd></dl>`;
    this.renderTracks();
    const records = (this.state?.tracks || []).filter((item) => !item.deleted && item.track.asset_id === assetId);
    const canPreserve = preserveTrack && records.some((item) => item.track.track_id === this.trackId);
    if (canPreserve) this.selectTrack(this.trackId);
    else if (records.length) this.selectTrack(records[0].track.track_id);
    else {
      this.trackId = null;
      this.model = null;
      this.$("#sphericalEditor")?.classList.add("hidden");
    }
  }

  renderTracks() {
    const element = this.$("#sphericalTrackList");
    if (!element) return;
    const records = (this.state?.tracks || []).filter((item) => !item.deleted && item.track.asset_id === this.assetId);
    element.innerHTML = records.length ? records.map((record) => `
      <button type="button" data-track-id="${escapeHtml(record.track.track_id)}" class="spherical-track-card ${record.track.track_id === this.trackId ? "active" : ""}">
        <strong>${escapeHtml(record.track.name)}</strong>
        <span>${escapeHtml(record.origin)} · r${record.revision} · ${record.track.keyframes.length} keyframes</span>
      </button>`).join("") : '<div class="spherical-empty-card">No camera tracks yet.</div>';
  }

  loadPanorama(asset) {
    const source = this.$("#sphericalSourceVideo");
    const fallback = this.$("#sphericalFallbackVideo");
    if (!source || !asset) return;
    source.pause();
    source.src = asset.preview_url;
    source.load();
    if (window.AFRAME) {
      this.$("#sphericalScene")?.classList.remove("hidden");
      fallback?.classList.add("hidden");
      const sphere = this.$("#sphericalVideoSphere");
      sphere?.setAttribute("material", "src", "#sphericalSourceVideo");
      setTimeout(() => this.$("#sphericalScene")?.resize?.(), 0);
    } else if (fallback) {
      this.$("#sphericalScene")?.classList.add("hidden");
      fallback.src = asset.preview_url;
      fallback.classList.remove("hidden");
    }
    const slider = this.$("#sphericalTimeSlider");
    if (slider) {
      slider.min = "0";
      slider.max = String(asset.duration_seconds);
      slider.value = "0";
    }
    this.updateScrubber();
  }

  showEmptyViewer(empty) {
    this.$("#sphericalViewerEmpty")?.classList.toggle("hidden", !empty);
  }

  selectTrack(trackId) {
    const record = this.state?.tracks?.find((item) => !item.deleted && item.track.track_id === trackId);
    if (!record) return;
    this.trackId = trackId;
    this.assetId = record.track.asset_id;
    this.model = new CameraTrackEditorModel(record.track, record.revision);
    this.$("#sphericalEditor")?.classList.remove("hidden");
    this.renderTracks();
    this.renderEditor();
    this.seek(record.track.source_start_seconds);
  }

  newTrack() {
    const asset = this.state?.assets?.find((item) => item.asset_id === this.assetId);
    if (!asset) return;
    this.trackId = null;
    this.model = CameraTrackEditorModel.create(asset);
    this.$("#sphericalEditor")?.classList.remove("hidden");
    this.renderTracks();
    this.renderEditor();
    this.seek(0);
  }

  renderEditor() {
    if (!this.model) return;
    const track = this.model.track;
    const name = this.$("#sphericalTrackName");
    if (name && document.activeElement !== name) name.value = track.name;
    const aspect = this.$("#sphericalAspect");
    if (aspect) aspect.value = `${track.output_width}x${track.output_height}`;
    const subjects = (this.state?.subjects || []).filter((subject) => subject.asset_id === track.asset_id);
    const subjectOptions = (selected) => [
      `<option value="" ${selected ? "" : "selected"}>—</option>`,
      ...subjects.map((subject) => `<option value="${escapeHtml(subject.subject_id)}" ${subject.subject_id === selected ? "selected" : ""}>${escapeHtml(subject.subject_id)}</option>`),
    ].join("");
    const body = this.$("#sphericalKeyframeRows");
    if (body) body.innerHTML = track.keyframes.map((frame, index) => `
      <tr data-keyframe-index="${index}" class="${index === this.model.selectedIndex ? "selected" : ""}">
        <td><input data-field="time_seconds" type="number" min="${track.source_start_seconds}" max="${track.source_end_seconds}" step="0.01" value="${frame.time_seconds}"></td>
        <td><input data-field="yaw" type="number" min="-180" max="180" step="0.1" value="${frame.yaw.toFixed(1)}"></td>
        <td><input data-field="pitch" type="number" min="-90" max="90" step="0.1" value="${frame.pitch.toFixed(1)}"></td>
        <td><input data-field="roll" type="number" min="-180" max="180" step="0.1" value="${frame.roll.toFixed(1)}"></td>
        <td><input data-field="horizontal_fov" type="number" min="1.1" max="178.9" step="0.1" value="${frame.horizontal_fov.toFixed(1)}"></td>
        <td><select data-field="easing">${["linear", "smoothstep", "ease_in", "ease_out", "ease_in_out"].map((value) => `<option value="${value}" ${value === frame.easing ? "selected" : ""}>${value}</option>`).join("")}</select></td>
        <td><select data-field="subject_id">${subjectOptions(frame.subject_id)}</select></td>
        <td><input data-field="confidence" type="number" min="0" max="1" step="0.01" placeholder="—" value="${frame.confidence ?? ""}"></td>
      </tr>`).join("");
    this.$("#sphericalUndoBtn").disabled = this.busy || !this.model.history.length;
    this.$("#sphericalRedoBtn").disabled = this.busy || !this.model.future.length;
    const status = this.$("#sphericalSaveState");
    if (status && !this.busy) status.textContent = this.model.dirty ? "Unsaved changes" : `Saved · r${this.model.revision}`;
  }

  editKeyframe(event) {
    if (!this.model) return;
    const row = event.target.closest("tr[data-keyframe-index]");
    const field = event.target.dataset.field;
    if (!row || !field) return;
    const index = Number(row.dataset.keyframeIndex);
    let value = event.target.value;
    if (["time_seconds", "yaw", "pitch", "roll", "horizontal_fov", "confidence"].includes(field)) {
      value = value === "" && field === "confidence" ? null : Number(value);
    }
    if (field === "subject_id" && !value) value = null;
    try {
      this.model.updateKeyframe(index, { [field]: value });
      this.model.selectedIndex = this.model.track.keyframes.findIndex((frame) => frame === this.model.track.keyframes[index]);
      this.seek(this.model.track.keyframes[this.model.selectedIndex]?.time_seconds ?? 0);
      this.renderEditor();
    } catch (error) {
      this.showOperationError(error);
    }
  }

  changeHistory(action) {
    if (!this.model) return;
    this.model[action]();
    this.renderEditor();
    this.seek(this.model.track.keyframes[this.model.selectedIndex].time_seconds);
  }

  addKeyframe() {
    if (!this.model) return;
    try {
      const time = Number(this.$("#sphericalTimeSlider")?.value || 0);
      this.model.addKeyframe(time, this.currentCamera());
      this.renderEditor();
    } catch (error) { this.showOperationError(error); }
  }

  deleteKeyframe() {
    if (!this.model) return;
    try {
      this.model.deleteKeyframe(this.model.selectedIndex);
      this.renderEditor();
      this.seek(this.model.track.keyframes[this.model.selectedIndex].time_seconds);
    } catch (error) { this.showOperationError(error); }
  }

  currentCamera() {
    const time = Number(this.$("#sphericalTimeSlider")?.value || 0);
    return this.model ? sampleTrack(this.model.track, time) : { yaw: 0, pitch: 0, roll: 0, horizontal_fov: 90 };
  }

  async saveTrack({ quiet = false } = {}) {
    if (!this.model) return null;
    if (!this.model.dirty && this.model.revision != null) return this.model.track;
    this.setBusy(true, "Saving…");
    try {
      const isNew = this.model.revision == null;
      const path = isNew
        ? `/api/sessions/${encodeURIComponent(this.sessionId)}/spherical/tracks`
        : `/api/sessions/${encodeURIComponent(this.sessionId)}/spherical/tracks/${encodeURIComponent(this.model.track.track_id)}`;
      const body = isNew
        ? { schema_version: "1.0", track: this.model.track }
        : { schema_version: "1.0", expected_revision: this.model.revision, track: this.model.track };
      const result = await this.request(path, { method: isNew ? "POST" : "PUT", body: JSON.stringify(body) });
      this.trackId = result.track.track_id;
      this.model.track = clone(result.track);
      this.model.markSaved(result.revision);
      await this.load();
      this.selectTrack(result.track.track_id);
      if (!quiet) this.$("#sphericalSaveState").textContent = `Saved · r${result.revision}`;
      return result.track;
    } catch (error) {
      this.showOperationError(error);
      throw error;
    } finally {
      this.setBusy(false);
      this.renderEditor();
    }
  }

  async renderTrack(kind) {
    if (!this.model) return;
    try {
      await this.saveTrack({ quiet: true });
      const path = `/api/sessions/${encodeURIComponent(this.sessionId)}/spherical/tracks/${encodeURIComponent(this.trackId)}/${kind}`;
      const body = kind === "preview"
        ? { schema_version: "1.0", center_seconds: Number(this.$("#sphericalTimeSlider")?.value || 0), duration_seconds: Number(this.$("#sphericalPreviewDuration")?.value || 4), max_dimension: 640, use_cache: true }
        : { schema_version: "1.0", use_cache: true };
      this.setBusy(true, kind === "preview" ? "Rendering preview…" : "Rendering final MP4…");
      const result = await this.request(path, { method: "POST", body: JSON.stringify(body) });
      const element = this.$("#sphericalRenderResult");
      if (element) {
        element.classList.remove("hidden");
        element.innerHTML = `<video controls playsinline src="${escapeHtml(result.preview_url)}"></video><div><strong>${kind === "preview" ? "Segment preview" : "Flat MP4 ready"}</strong><span>${result.output_width}×${result.output_height} · ${escapeHtml(result.selected_backend)} · ${result.cache_hit ? "cache hit" : `${Number(result.render_duration_seconds).toFixed(2)}s`}</span>${result.ordinary_media ? `<span>Added to media: ${escapeHtml(result.ordinary_media.name)}</span>` : ""}${(result.warnings || []).map((warning) => `<span class="warning-text">${escapeHtml(warning)}</span>`).join("")}</div>`;
      }
      await this.load();
      this.selectTrack(this.trackId);
    } catch (error) {
      this.showOperationError(error);
    } finally {
      this.setBusy(false);
      this.renderEditor();
    }
  }

  async acceptProposal(kind, id) {
    const segment = kind === "director" ? `director/${encodeURIComponent(id)}` : `suggestions/${encodeURIComponent(id)}`;
    this.setBusy(true, "Accepting proposal…");
    try {
      const result = await this.request(`/api/sessions/${encodeURIComponent(this.sessionId)}/spherical/${segment}/accept`, { method: "POST" });
      this.trackId = result.track.track_id;
      await this.load();
      this.selectAsset(result.track.asset_id, { preserveTrack: true });
      this.selectTrack(result.track.track_id);
    } catch (error) { this.showOperationError(error); }
    finally { this.setBusy(false); }
  }

  renderInsights() {
    const suggestions = this.state?.suggestions || [];
    const proposals = this.state?.director_proposals || [];
    const suggestionPanel = this.$("#sphericalSuggestionsPanel");
    if (suggestionPanel) suggestionPanel.innerHTML = [...suggestions.map((item) => ({ ...item, kind: "suggestion", id: item.suggestion_id, title: item.policy, score: item.confidence })), ...proposals.map((item) => ({ ...item, kind: "director", id: item.proposal_id, title: `${item.profile_id} · ${item.variant}`, score: item.aggregate_score }))].map((item) => `
      <article class="spherical-insight-card"><div class="spherical-card-top"><strong>${escapeHtml(item.title)}</strong><span>${Math.round((Number(item.score) || 0) * 100)}%</span></div><div class="spherical-muted">${escapeHtml(item.status)} · ${shortId(item.id)}</div>${(item.warnings || []).map((warning) => `<div class="warning-text">${escapeHtml(warning)}</div>`).join("")}${item.status === "proposed" ? `<button type="button" class="spherical-button primary wide" data-accept-kind="${item.kind}" data-accept-id="${escapeHtml(item.id)}">Accept into editor</button>` : ""}</article>`).join("") || '<div class="spherical-empty-card">No AI or director suggestions yet.</div>';
    const subjectPanel = this.$("#sphericalSubjectsPanel");
    if (subjectPanel) subjectPanel.innerHTML = (this.state?.subjects || []).map((item) => `<article class="spherical-insight-card"><div class="spherical-card-top"><strong>${escapeHtml(item.subject_id)}</strong><span>${Math.round((Number(item.mean_confidence) || 0) * 100)}%</span></div><div>${formatTime(item.start_seconds)}–${formatTime(item.end_seconds)}</div><div class="spherical-muted">${escapeHtml((item.labels || []).join(", ") || "unlabelled")}</div></article>`).join("") || '<div class="spherical-empty-card">Run spherical analysis to populate subjects.</div>';
    const rendersPanel = this.$("#sphericalRendersPanel");
    if (rendersPanel) rendersPanel.innerHTML = (this.state?.renders || []).slice().reverse().map((item) => `<article class="spherical-render-card"><video controls preload="metadata" src="${escapeHtml(item.preview_url)}"></video><strong>${escapeHtml(item.kind.replaceAll("_", " "))}</strong><span>${item.output_width}×${item.output_height} · ${item.cache_hit ? "cache hit" : escapeHtml(item.selected_backend || "rendered")}</span></article>`).join("") || '<div class="spherical-empty-card">No spherical renders yet.</div>';
  }

  selectTab(name) {
    this.root.querySelectorAll("[data-spherical-tab]").forEach((button) => button.classList.toggle("active", button.dataset.sphericalTab === name));
    ["Suggestions", "Subjects", "Renders"].forEach((panel) => this.$(`#spherical${panel}Panel`)?.classList.toggle("hidden", panel.toLowerCase() !== name));
  }

  seek(time) {
    const asset = this.state?.assets?.find((item) => item.asset_id === this.assetId);
    const bounded = clamp(Number(time) || 0, 0, Number(asset?.duration_seconds) || 0);
    const slider = this.$("#sphericalTimeSlider");
    if (slider) slider.value = String(bounded);
    const source = this.$("#sphericalSourceVideo");
    if (source && Number.isFinite(source.duration)) source.currentTime = bounded;
    this.updateViewerCamera();
    this.updateScrubber();
  }

  updateViewerCamera() {
    const time = Number(this.$("#sphericalTimeSlider")?.value || 0);
    const camera = this.model ? sampleTrack(this.model.track, time) : { yaw: 0, pitch: 0, roll: 0, horizontal_fov: 90 };
    this.$("#sphericalCameraRig")?.setAttribute("rotation", `${camera.pitch} ${-camera.yaw} ${-camera.roll}`);
    this.$("#sphericalCamera")?.setAttribute("camera", "fov", camera.horizontal_fov);
    const readout = this.$("#sphericalViewerReadout");
    if (readout) readout.textContent = `Yaw ${camera.yaw.toFixed(1)}° · Pitch ${camera.pitch.toFixed(1)}° · Roll ${camera.roll.toFixed(1)}° · FOV ${camera.horizontal_fov.toFixed(1)}°`;
  }

  updateScrubber() {
    const source = this.$("#sphericalSourceVideo");
    const asset = this.state?.assets?.find((item) => item.asset_id === this.assetId);
    const current = Number(source?.currentTime ?? this.$("#sphericalTimeSlider")?.value ?? 0);
    const duration = Number(source?.duration) || Number(asset?.duration_seconds) || 0;
    const slider = this.$("#sphericalTimeSlider");
    if (slider && !slider.matches(":active")) slider.value = String(current);
    const output = this.$("#sphericalTimeReadout");
    if (output) output.textContent = `${formatTime(current)} / ${formatTime(duration)}`;
    this.updateViewerCamera();
  }

  togglePlayback() {
    const source = this.$("#sphericalSourceVideo");
    if (!source) return;
    if (source.paused) {
      source.play().then(() => {
        this.$("#sphericalPlayBtn").textContent = "❚❚";
        this.playTimer = window.setInterval(() => this.updateScrubber(), 50);
      }).catch((error) => this.showOperationError(error));
    } else this.pausePlayback();
  }

  pausePlayback() {
    this.$("#sphericalSourceVideo")?.pause();
    this.$("#sphericalFallbackVideo")?.pause();
    if (this.playTimer) window.clearInterval(this.playTimer);
    this.playTimer = 0;
    const button = this.$("#sphericalPlayBtn");
    if (button) button.textContent = "▶";
  }

  showOperationError(error) {
    const element = this.$("#sphericalError");
    if (!element) return;
    element.textContent = error?.message || String(error);
    element.classList.remove("hidden");
    window.setTimeout(() => element.classList.add("hidden"), 7000);
  }
}
