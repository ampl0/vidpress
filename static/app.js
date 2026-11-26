const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const els = {
  statusDot: $("#statusDot"), statusText: $("#statusText"),
  dropzone: $("#dropzone"), fileInput: $("#fileInput"),
  fileCard: $("#fileCard"), fileName: $("#fileName"), fileMeta: $("#fileMeta"),
  fileChange: $("#fileChange"), fSize: $("#fSize"), fDur: $("#fDur"),
  fRes: $("#fRes"), fCodec: $("#fCodec"),
  optsCard: $("#optsCard"), modeSeg: $("#modeSeg"),
  codecSel: $("#codecSel"), quality: $("#quality"), qualityVal: $("#qualityVal"),
  qualityOpt: $("#qualityOpt"), sizeOpt: $("#sizeOpt"), targetSize: $("#targetSize"),
  presetSeg: $("#presetSeg"), audioSel: $("#audioSel"), resSel: $("#resSel"),
  tuneSel: $("#tuneSel"), compressBtn: $("#compressBtn"),
  progressCard: $("#progressCard"), stageLabel: $("#stageLabel"), pctLabel: $("#pctLabel"),
  barFill: $("#barFill"), progSub: $("#progSub"), cancelBtn: $("#cancelBtn"),
  resultCard: $("#resultCard"), rOrig: $("#rOrig"), rNew: $("#rNew"),
  rOrigMeta: $("#rOrigMeta"), rNewMeta: $("#rNewMeta"), savePct: $("#savePct"),
  rStats: $("#rStats"), downloadBtn: $("#downloadBtn"), againBtn: $("#againBtn"),
  errorCard: $("#errorCard"), errorMsg: $("#errorMsg"), errorClose: $("#errorClose"),
  toast: $("#toast"), ffVersion: $("#ffVersion"),
};

const state = {
  file: null,
  fileBytes: null,
  mode: "quality",
  codec: "h264",
  quality: 70,
  targetSize: 25,
  preset: "balanced",
  audio: "auto",
  res: "0",
  tune: "",
  job: null,
  pollTimer: null,
  busy: false,
};

// ---------- helpers ----------
function toast(msg) {
  els.toast.textContent = msg;
  els.toast.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => els.toast.classList.remove("show"), 2400);
}
function fmtBytes(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(0) + " KB";
  return bytes + " B";
}
function fmtDur(s) {
  if (!s) return "—";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}
function escapeHtml(v) { return String(v).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function showError(msg) { els.errorMsg.textContent = msg; els.errorCard.hidden = false; }
function hideError() { els.errorCard.hidden = true; }

// ---------- health / ffmpeg version ----------
async function loadHealth() {
  try {
    const r = await fetch("/api/health", { cache: "no-store" });
    if (!r.ok) throw 0;
    const h = await r.json();
    els.statusDot.className = "dot online";
    els.statusText.textContent = `online · server ready`;
    const avail = h.available_codecs || [];
    $$("#codecSel option").forEach(opt => {
      opt.disabled = avail.length > 0 && !avail.includes(opt.value);
    });
  } catch (e) {
    els.statusDot.className = "dot offline";
    els.statusText.textContent = "offline";
  }
}

// ---------- file selection ----------
function readFileMetadata(file) {
  return new Promise(resolve => {
    if (!file.type.startsWith("video/")) return resolve(null);
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "metadata";
    v.src = url;
    const done = () => {
      const info = { duration: v.duration || 0, width: v.videoWidth || 0, height: v.videoHeight || 0 };
      URL.revokeObjectURL(url);
      resolve(info);
    };
    v.onloadedmetadata = done;
    v.onerror = () => resolve(null);
    setTimeout(() => { if (!v.videoWidth) done(); }, 2500);
  });
}

async function selectFile(file) {
  if (!file) return;
  if (!file.type.startsWith("video/")) { toast("Please choose a video file."); return; }
  if (file.size > 512 * 1024 * 1024) { showError("File exceeds the 512 MB server limit."); return; }
  state.file = file;
  state.fileBytes = await file.arrayBuffer();

  els.fileName.textContent = file.name;
  els.fSize.textContent = fmtBytes(file.size);
  els.fileCard.hidden = false;
  els.optsCard.hidden = false;
  els.resultCard.hidden = true;
  els.errorCard.hidden = true;

  const meta = await readFileMetadata(file);
  if (meta) {
    els.fDur.textContent = fmtDur(meta.duration);
    els.fRes.textContent = meta.width ? `${meta.width}×${meta.height}` : "—";
    els.fileMeta.textContent = meta.duration ? `${fmtDur(meta.duration)} · ${meta.width}×${meta.height}` : "";
  } else {
    els.fDur.textContent = "—"; els.fRes.textContent = "—";
    els.fileMeta.textContent = "";
  }
}

// ---------- options UI ----------
function bindOptions() {
  $$(".seg button", els.modeSeg).forEach(b => {
    b.addEventListener("click", () => {
      state.mode = b.dataset.mode;
      $$(".seg button", els.modeSeg).forEach(x => x.classList.toggle("on", x === b));
      els.qualityOpt.hidden = state.mode !== "quality";
      els.sizeOpt.hidden = state.mode !== "size";
    });
  });
  els.codecSel.addEventListener("change", () => {
    if (els.codecSel.selectedOptions[0] && els.codecSel.selectedOptions[0].disabled) {
      els.codecSel.value = state.codec;
      return;
    }
    state.codec = els.codecSel.value;
  });
  els.quality.addEventListener("input", () => {
    state.quality = +els.quality.value;
    els.qualityVal.textContent = state.quality + "%";
  });
  els.targetSize.addEventListener("input", () => { state.targetSize = parseFloat(els.targetSize.value) || 25; });
  $$(".seg button", els.presetSeg).forEach(b => {
    b.addEventListener("click", () => {
      state.preset = b.dataset.preset;
      $$(".seg button", els.presetSeg).forEach(x => x.classList.toggle("on", x === b));
    });
  });
  els.audioSel.addEventListener("change", () => { state.audio = els.audioSel.value; });
  els.resSel.addEventListener("change", () => { state.res = els.resSel.value; });
  els.tuneSel.addEventListener("change", () => { state.tune = els.tuneSel.value; });
  els.fileChange.addEventListener("click", () => els.fileInput.click());
  els.fileInput.addEventListener("change", (e) => { if (e.target.files[0]) selectFile(e.target.files[0]); });
  els.errorClose.addEventListener("click", () => hideError());
}

// ---------- drag & drop ----------
function bindDropzone() {
  const dz = els.dropzone;
  dz.addEventListener("click", () => els.fileInput.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); els.fileInput.click(); } });
  ["dragenter", "dragover"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", e => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) selectFile(f);
  });
}

// ---------- submit ----------
function buildOptions() {
  const opts = {
    codec: state.codec,
    mode: state.mode,
    preset: state.preset,
    audio: state.audio,
    tune: state.tune,
    max_width: parseInt(state.res, 10) || 0,
    max_height: parseInt(state.res, 10) || 0,
    single_pass: false,
  };
  if (state.mode === "quality") opts.quality = state.quality;
  else opts.target_size_mb = state.targetSize;
  return opts;
}

async function submit() {
  if (!state.fileBytes || state.busy) return;
  hideError(); state.busy = true;
  els.compressBtn.disabled = true;
  els.compressBtn.classList.add("is-loading");

  const fd = new FormData();
  fd.append("file", new Blob([state.fileBytes]), state.file.name);
  fd.append("options", JSON.stringify(buildOptions()));

  try {
    const r = await fetch("/api/jobs", { method: "POST", body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    state.job = data.id;
    showProgress();
    poll();
  } catch (e) {
    showError(e.message || "Upload failed. Try again.");
    state.busy = false;
    els.compressBtn.disabled = false;
    els.compressBtn.classList.remove("is-loading");
  }
}

// ---------- progress ----------
function showProgress() {
  els.optsCard.hidden = true;
  els.fileCard.hidden = true;
  els.resultCard.hidden = true;
  els.progressCard.hidden = false;
  els.barFill.style.width = "0%";
  els.pctLabel.textContent = "0%";
}

async function poll() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    if (!state.job) return;
    try {
      const r = await fetch(`/api/jobs/${state.job}`, { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      renderJob(j);
      if (["done", "error", "cancelled"].includes(j.status)) {
        clearInterval(state.pollTimer);
        state.busy = false;
        els.compressBtn.classList.remove("is-loading");
        if (j.status === "done") renderResult(j);
        else if (j.status === "error") { showError(j.error || "Encoding failed."); els.progressCard.hidden = true; els.optsCard.hidden = false; els.fileCard.hidden = false; }
        else { toast("Cancelled."); els.progressCard.hidden = true; els.optsCard.hidden = false; els.fileCard.hidden = false; }
      }
    } catch (e) { /* transient */ }
  }, 900);
}

function renderJob(j) {
  els.stageLabel.textContent = j.stage || j.status;
  els.pctLabel.textContent = Math.round(j.progress) + "%";
  els.barFill.style.width = j.progress + "%";
  if (j.status === "analyzing") els.progSub.textContent = "reading your file…";
  else if (j.status === "running") els.progSub.textContent = "encoding — this can take a while on a small server…";
  else if (j.error) els.progSub.textContent = j.error;
}

// ---------- result ----------
function renderResult(j) {
  els.progressCard.hidden = true;
  els.resultCard.hidden = false;
  els.optsCard.hidden = true;
  els.fileCard.hidden = true;

  const inp = j.input, out = j.output;
  els.rOrig.textContent = fmtBytes(inp.size_bytes);
  els.rOrigMeta.textContent = `${inp.resolution || "?"} · ${inp.video_codec || "?"}`;
  els.rNew.textContent = fmtBytes(out.size_bytes);
  els.rNewMeta.textContent = `${out.resolution || "?"} · ${out.video_codec || "?"}`;

  const saved = Math.max(0, out.savings_pct);
  els.savePct.textContent = `-${saved.toFixed(0)}%`;
  document.querySelector(".save-ring").style.setProperty("--p", saved + "%");

  const stats = [
    ["Original", fmtBytes(inp.size_bytes)],
    ["Compressed", fmtBytes(out.size_bytes)],
    ["Saved", saved.toFixed(1) + "%"],
    ["Duration", fmtDur(out.duration)],
    ["Video", out.video_codec],
    ["Container", out.container],
    ["Speed", (inp.duration / (j.elapsed || 0.001)).toFixed(1) + "× real-time"],
  ];
  els.rStats.innerHTML = stats.map(([k, v]) => `<div><span class="stat-label">${k}</span><b>${escapeHtml(v)}</b></div>`).join("");

  els.downloadBtn.href = `/api/download/${j.id}`;
  els.downloadBtn.setAttribute("download", "");

  // note when the engine auto-backed-off quality to actually shrink the file
  const note = document.getElementById("retryNote");
  if (j.retried && note) {
    note.hidden = false;
  } else if (note) {
    note.hidden = true;
  }
}

// ---------- restart ----------
function bindActionButtons() {
  els.compressBtn.addEventListener("click", submit);
  els.againBtn.addEventListener("click", () => {
    state.job = null;
    els.resultCard.hidden = true;
    els.fileCard.hidden = false;
    els.optsCard.hidden = false;
  });
  els.cancelBtn.addEventListener("click", async () => {
    if (!state.job) return;
    try {
      await fetch(`/api/jobs/${state.job}`, { method: "DELETE" });
      toast("Cancelling…");
    } catch (e) {}
  });
}

// ---------- init ----------
function init() {
  bindDropzone();
  bindOptions();
  bindActionButtons();
  loadHealth();
}
document.addEventListener("DOMContentLoaded", init);
