// Clarity: shared helpers, app state, the Enhance/Guide tabs, the source and project files.
// The other scripts (chain, viewer, measure, output, guide) hang off the CL object defined here.
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const CL = {
  state: {
    source: null,      // source info from the backend (null until something is opened)
    chain: [],         // [{uid, id, enabled, params, open}]
    index: 0,          // current frame
    catalogue: null,   // {categories, filters}
    filters: {},       // id -> filter description
    measurements: { calibration: null, items: [] },
  },
  _handlers: {},
  on(name, fn) {
    (this._handlers[name] = this._handlers[name] || []).push(fn);
  },
  emit(name, data) {
    (this._handlers[name] || []).forEach((fn) => fn(data));
  },
};

// ---------- Backend calls ----------
// The page talks to Python directly through pywebview's bridge; there is no HTTP server or port.
const bridgeReady = new Promise((resolve) => {
  if (window.pywebview && window.pywebview.api) resolve();
  else window.addEventListener("pywebviewready", resolve, { once: true });
});

async function api(method, ...args) {
  await bridgeReady;
  const res = await window.pywebview.api[method](...args);
  if (!res || !res.ok) throw new Error((res && res.error) || `${method} failed`);
  return res.data;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str === null || str === undefined ? "" : String(str);
  return div.innerHTML;
}

function formatBytes(bytes) {
  if (bytes === undefined || bytes === null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let val = bytes;
  let i = 0;
  while (val >= 1000 && i < units.length - 1) {
    val /= 1000;
    i++;
  }
  return `${val.toFixed(val < 10 && i > 0 ? 2 : i > 0 ? 1 : 0)} ${units[i]}`;
}

function formatTime(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${h}:${String(m).padStart(2, "0")}:${s.toFixed(3).padStart(6, "0")}`;
}

function showError(message) {
  const el = $("#error-section");
  if (!message) {
    el.classList.add("hidden");
    return;
  }
  el.textContent = message;
  el.classList.remove("hidden");
}

function setStatus(el, text, state = "") {
  el.textContent = text || "";
  if (state) el.dataset.state = state;
  else delete el.dataset.state;
}

// ---------- Enhance / Guide tabs ----------
function setMode(mode) {
  $$("#mode-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  $("#enhance-view").classList.toggle("hidden", mode !== "enhance");
  $("#guide-view").classList.toggle("hidden", mode !== "guide");
  try {
    localStorage.setItem("cl-mode", mode);
  } catch (e) {
    /* storage can be unavailable; the tab just isn't remembered */
  }
}

$$("#mode-tabs button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
try {
  if (localStorage.getItem("cl-mode") === "guide") setMode("guide");
} catch (e) {
  /* ignore */
}

// ---------- Case details ----------
CL.caseData = () => Object.fromEntries($$("[data-case]").map((el) => [el.dataset.case, el.value.trim()]));
CL.setCaseData = (data) => $$("[data-case]").forEach((el) => (el.value = (data || {})[el.dataset.case] || ""));

// ---------- Source ----------
const sourceInput = $("#source-input");
const sourceStatus = $("#source-status");
let hashTimer = null;

function sourceKindLabel(info) {
  if (info.kind === "video") return "Video";
  if (info.kind === "sequence") return `Sequence · ${info.count} images`;
  return "Image";
}

function renderSourceMeta() {
  const info = CL.state.source;
  const meta = $("#source-meta");
  if (!info) {
    meta.classList.add("hidden");
    return;
  }
  const d = info.details || {};
  const badges = [
    sourceKindLabel(info),
    `${info.width} × ${info.height}`,
    info.kind === "video" && info.fps ? `${info.fps.toFixed(3).replace(/\.?0+$/, "")} fps` : "",
    info.kind === "video" ? `${info.count.toLocaleString()} frames` : "",
    info.duration ? formatTime(info.duration) : "",
    d.codec ? `codec ${d.codec}` : "",
    d.bit_depth && info.kind !== "video" ? `${d.bit_depth}-bit` : "",
    formatBytes(info.total_size),
  ].filter(Boolean);
  const first = info.files[0] || {};
  let hash;
  if (info.hash_error) hash = `<span class="hash bad">hashing failed: ${escapeHtml(info.hash_error)}</span>`;
  else if (!info.hashed) hash = `<span class="hash">hashing… ${Math.round((info.hash_progress || 0) * 100)}%</span>`;
  else if (info.file_count > 1) hash = `<span class="hash ok">MD5 + SHA-256 computed for ${info.file_count} files</span>`;
  else hash = `<span class="hash ok mono">SHA-256 ${escapeHtml(first.sha256)}</span>`;
  if (info.hash_match === true) hash += ` <span class="hash ok">✓ matches the project</span>`;
  if (info.hash_match === false) hash += ` <span class="hash bad">✗ does not match the hash saved in the project</span>`;
  const notes = (info.notes || []).map((n) => `<li>${escapeHtml(n)}</li>`).join("");
  meta.innerHTML = `<div class="badges">${badges.map((b) => `<span class="badge">${escapeHtml(b)}</span>`).join("")}</div>
    <div class="hash-line">${hash}</div>${notes ? `<ul class="source-notes">${notes}</ul>` : ""}`;
  meta.classList.remove("hidden");
}

async function pollHashes() {
  clearTimeout(hashTimer);
  try {
    const info = await api("source_info");
    if (!info) return;
    CL.state.source = info;
    renderSourceMeta();
    if (!info.hashed && !info.hash_error) hashTimer = setTimeout(pollHashes, 400);
  } catch (e) {
    /* the meta line just stays as it was */
  }
}

async function openSource(paths, expected = null, keep = false) {
  if (!paths || !paths.length) return;
  showError("");
  sourceInput.value = paths.length > 1 ? `${paths[0]}  (+${paths.length - 1} more)` : paths[0];
  setStatus(sourceStatus, "Opening…");
  try {
    const info = await api("open_source", paths, expected);
    CL.state.source = info;
    CL.state.sourcePaths = paths;
    if (!keep) {
      CL.state.index = 0;
      CL.state.measurements = { calibration: null, items: [] };
    }
    CL.state.index = Math.min(CL.state.index, info.count - 1);
    setStatus(sourceStatus, `Opened ${info.name}`, "success");
    ["#workspace", "#measure-panel", "#case-panel", "#output-panel"].forEach((s) => $(s).classList.remove("hidden"));
    $("#save-project").disabled = false;
    renderSourceMeta();
    CL.emit("source", info);
    pollHashes();
  } catch (e) {
    setStatus(sourceStatus, e.message, "error");
  }
}
CL.openSource = openSource;

$("#browse-media").addEventListener("click", async () => {
  try {
    const { paths } = await api("pick", "media");
    if (paths.length) openSource(paths);
  } catch (e) {
    setStatus(sourceStatus, e.message, "error");
  }
});

$("#browse-sequence").addEventListener("click", async () => {
  try {
    const { paths } = await api("pick", "sequence");
    if (paths.length) openSource(paths);
  } catch (e) {
    setStatus(sourceStatus, e.message, "error");
  }
});

sourceInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && sourceInput.value.trim()) openSource([sourceInput.value.trim().replace(/^"|"$/g, "")]);
});
sourceInput.addEventListener("change", () => {
  const v = sourceInput.value.trim().replace(/^"|"$/g, "");
  if (v && !v.includes("  (+") && (!CL.state.sourcePaths || CL.state.sourcePaths[0] !== v)) openSource([v]);
});

// ---------- Projects ----------
CL.projectData = () => ({
  chain: CL.serializeChain(),
  index: CL.state.index,
  case: CL.caseData(),
  measurements: CL.state.measurements,
});

$("#save-project").addEventListener("click", async () => {
  try {
    const { path } = await api("save_project", CL.projectData());
    if (path) setStatus(sourceStatus, `Project saved to ${path}`, "success");
  } catch (e) {
    showError(e.message);
  }
});

$("#open-project").addEventListener("click", async () => {
  try {
    const proj = await api("load_project");
    if (proj.cancelled) return;
    CL.setCaseData(proj.case);
    CL.state.measurements = proj.measurements && proj.measurements.items ? proj.measurements : { calibration: null, items: [] };
    CL.state.index = proj.index || 0;
    CL.loadChain(proj.chain);
    if (proj.missing.length) {
      setStatus(sourceStatus, `Project loaded, but its source is missing: ${proj.missing[0]}. Open the source to continue.`, "error");
      return;
    }
    await openSource(proj.source_paths, proj.expected, true);
    CL.emit("measurements");
  } catch (e) {
    showError(e.message);
  }
});

// ---------- Start-up ----------
(async () => {
  try {
    const [health, cat] = await Promise.all([api("health"), api("catalogue")]);
    $("#footer-version").textContent = `${health.version} · OpenCV ${health.opencv} · offline`;
    CL.state.catalogue = cat;
    cat.filters.forEach((f) => (CL.state.filters[f.id] = f));
    CL.health = health;
    CL.emit("catalogue", cat);
  } catch (e) {
    showError(`Could not start: ${e.message}`);
  }
})();
