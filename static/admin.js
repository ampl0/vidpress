const $ = (sel, root = document) => root.querySelector(sel);

const els = {
  loginCard: $("#loginCard"), dashCard: $("#dashCard"),
  loginPassword: $("#loginPassword"), loginBtn: $("#loginBtn"), loginError: $("#loginError"),
  logoutBtn: $("#logoutBtn"), memVal: $("#memVal"), diskVal: $("#diskVal"),
  parVal: $("#parVal"), srvVal: $("#srvVal"),
  botStatus: $("#botStatus"), botToken: $("#botToken"),
  apiBase: $("#apiBase"), maxDl: $("#maxDl"), largeStatus: $("#largeStatus"),
  apiId: $("#apiId"), apiHash: $("#apiHash"),
  progressInterval: $("#progressInterval"), queueMax: $("#queueMax"),
  defPreset: $("#defPreset"), defDimension: $("#defDimension"), defAudio: $("#defAudio"),
  defFormat: $("#defFormat"), defCodec: $("#defCodec"), defFps: $("#defFps"),
  saveBot: $("#saveBot"), clearBot: $("#clearBot"),
  jobsBody: $("#jobsBody"), toast: $("#toast"),
};

const KEY = "vidpress_admin_token";
let token = localStorage.getItem(KEY) || "";

function toast(msg, err) {
  els.toast.textContent = msg;
  els.toast.style.borderColor = err ? "rgba(255,107,129,.5)" : "";
  els.toast.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => els.toast.classList.remove("show"), 2600);
}
function esc(v) { return String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtBytes(b) { return b >= 1048576 ? (b/1048576).toFixed(1)+" MB" : b >= 1024 ? (b/1024).toFixed(0)+" KB" : b+" B"; }

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers["Authorization"] = "Bearer " + token;
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401) { logout(); throw new Error("unauthorized"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function logout() {
  token = ""; localStorage.removeItem(KEY);
  els.dashCard.hidden = true; els.loginCard.hidden = false;
}

async function login() {
  const password = els.loginPassword.value;
  els.loginError.hidden = true;
  try {
    const r = await fetch("/api/admin/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Login failed");
    token = d.token; localStorage.setItem(KEY, token);
    els.dashCard.hidden = false; els.loginCard.hidden = true;
    populateForm();
    refreshStatus();
  } catch (e) {
    els.loginError.textContent = e.message; els.loginError.hidden = false;
  }
}

// Populate form fields ONCE (initial login / after save). Never called by the
// periodic refresh, so typed values are not wiped.
async function populateForm() {
  try {
    const s = await api("/api/admin/settings");
    els.apiBase.value = s.telegram_api_base !== "https://api.telegram.org" ? s.telegram_api_base : "";
    els.maxDl.value = s.telegram_max_dl_mb || 200;
    els.apiId.value = s.telegram_api_id || "";
    els.apiHash.value = "";
    els.apiHash.placeholder = s.telegram_api_hash_set ? "saved: " + s.telegram_api_hash : "from my.telegram.org";
    els.progressInterval.value = s.progress_interval || 30;
    els.queueMax.value = s.queue_max || 10;
    const d = s.default_settings || {};
    const setSel = (el, opts, cur) => { el.value = cur || ""; };
    setSel(els.defPreset, null, d.preset || "");
    setSel(els.defDimension, null, d.dimension || "");
    setSel(els.defAudio, null, d.audio || "");
    setSel(els.defFormat, null, d.format || "");
    setSel(els.defCodec, null, d.codec || "");
    els.defFps.value = d.fps || 0;
    if (s.telegram_bot_token_set) {
      els.botToken.placeholder = "saved: " + s.telegram_bot_token;
      els.botToken.value = "";
    } else {
      els.botToken.placeholder = "not set — token from @BotFather";
      els.botToken.value = "";
    }
  } catch (e) { /* ignore */ }
}

// Live refresh: status chips, bot state, large-files note, jobs. Does NOT touch form inputs.
async function refreshStatus() {
  try {
    const h = await api("/api/health");
    els.memVal.textContent = h.free_mem_mb ? h.free_mem_mb + " MB" : "—";
    els.diskVal.textContent = h.free_disk_gb != null ? h.free_disk_gb + " GB" : "—";
    els.parVal.textContent = h.max_parallel;
    els.srvVal.textContent = "v" + h.version;

    const s = await api("/api/admin/settings");
    const b = s.bot || {};
    if (b.ready) {
      els.botStatus.innerHTML = `<span class="status-good">● active as @${esc(b.username)}</span>`;
    } else if (b.last_error) {
      els.botStatus.innerHTML = `<span class="status-bad">● error: ${esc(b.last_error)}</span>`;
    } else {
      els.botStatus.innerHTML = `<span class="status-bad">● offline</span>`;
    }
    if (s.large_files_enabled) {
      els.largeStatus.innerHTML = '✅ <b>Large files enabled</b> — downloads/uploads via Telethon (beyond 20 MB).';
    } else {
      els.largeStatus.innerHTML = '📍 Set <b>api_id</b> + <b>api_hash</b> (from <a href="https://my.telegram.org" target="_blank" rel="noopener" style="color:var(--accent)">my.telegram.org</a>) to enable <b>large files (&gt;20 MB)</b> via the Telethon MTProto client.';
    }
    loadJobs();
  } catch (e) { /* ignore */ }
}

async function loadJobs() {
  try {
    const d = await api("/api/admin/jobs");
    const jobs = d.jobs || [];
    if (!jobs.length) { els.jobsBody.innerHTML = '<tr><td colspan="6" class="note">No jobs yet.</td></tr>'; return; }
    els.jobsBody.innerHTML = jobs.map(j => {
      const inp = j.input, out = j.output;
      const tag = `<span class="tag ${j.status}">${esc(j.status)}</span>`;
      const result = out ? `${fmtBytes(out.size_bytes)} (${out.savings_pct.toFixed(0)}%)` : "—";
      const err = j.error ? `<div class="stderr">${esc(j.error)}</div>` : "";
      const cancel = ["queued","running","analyzing"].includes(j.status)
        ? `<button class="ghost danger" data-cancel="${j.id}">Cancel</button>` : "";
      return `<tr>
        <td>${esc(j.id)}</td>
        <td>${tag}</td>
        <td>${j.status==="done" ? "100%" : Math.round(j.progress)+"%"}<br><span class="note">${esc(j.stage||"")}</span></td>
        <td>${inp ? fmtBytes(inp.size_bytes) : "—"}<br><span class="note">${inp? inp.resolution : ""}</span></td>
        <td>${result}${err}</td>
        <td>${cancel}</td>
      </tr>`;
    }).join("");
    els.jobsBody.querySelectorAll("[data-cancel]").forEach(btn => {
      btn.addEventListener("click", () => cancelJob(btn.dataset.cancel));
    });
  } catch (e) { /* ignore */ }
}

async function cancelJob(id) {
  try { await api(`/api/admin/jobs/${id}/cancel`, { method: "POST" }); toast("Cancelled"); loadJobs(); }
  catch (e) { toast(e.message, true); }
}

async function saveBot() {
  const value = els.botToken.value.trim();
  const body = { telegram_bot_token: value };
  const base = els.apiBase.value.trim();
  body.telegram_api_base = base;
  const mb = parseInt(els.maxDl.value, 10);
  if (!isNaN(mb) && mb > 0) body.telegram_max_dl_mb = mb;
  body.telegram_api_id = els.apiId.value.trim();
  body.telegram_api_hash = els.apiHash.value.trim();
  const interval = parseInt(els.progressInterval.value, 10);
  if (!isNaN(interval)) body.progress_interval = interval;
  const qm = parseInt(els.queueMax.value, 10);
  if (!isNaN(qm)) body.queue_max = qm;
  const ds = {};
  for (const [k, el] of [["preset", els.defPreset], ["dimension", els.defDimension], ["audio", els.defAudio], ["format", els.defFormat], ["codec", els.defCodec]]) {
    if (el.value) ds[k] = el.value;
  }
  const fps = parseInt(els.defFps.value, 10);
  if (!isNaN(fps) && fps > 0) ds.fps = fps;
  if (Object.keys(ds).length) body.default_settings = ds;
  try {
    await api("/api/admin/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    toast(value ? "Settings saved — bot restarting…" : "Settings saved");
    populateForm();
    refreshStatus();
  } catch (e) { toast(e.message, true); }
}

els.loginBtn.addEventListener("click", login);
els.loginPassword.addEventListener("keydown", e => { if (e.key === "Enter") login(); });
els.logoutBtn.addEventListener("click", logout);
els.saveBot.addEventListener("click", saveBot);
els.clearBot.addEventListener("click", () => { els.botToken.value = ""; saveBot(); });
// refresh live status/jobs every 4s — does NOT touch form fields
setInterval(() => { if (!els.dashCard.hidden) refreshStatus(); }, 4000);

async function init() {
  if (!token) { els.loginCard.hidden = false; return; }
  try {
    await api("/api/admin/status");
    els.loginCard.hidden = true; els.dashCard.hidden = false;
    populateForm();
    refreshStatus();
  } catch (e) { els.loginCard.hidden = false; }
}
init();
