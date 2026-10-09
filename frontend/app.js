const state = {
  source: "",
  sourceInfo: null,
  outputDir: "",
  format: "e01",
  devices: [],
  deviceFilter: "physical",
  jobId: null,
  pollTimer: null,
  alertShownFor: null,
  reportJobId: null,
  lastImagePath: null,
};

const $ = (sel) => document.querySelector(sel);

const sourceInput = $("#source-input");
const outputInput = $("#output-input");
const nameInput = $("#name-input");
const deviceList = $("#device-list");
const runBtn = $("#run-btn");
const scanBtn = $("#scan-btn");
const cancelBtn = $("#cancel-btn");
const progressSection = $("#progress-section");
const progressFill = $("#progress-bar-fill");
const progressLabel = $("#progress-label");
const progressStage = $("#progress-stage");
const progressSpeed = $("#progress-speed");
const errorSection = $("#error-section");
const alertSection = $("#alert-section");
const scanSection = $("#scan-section");
const resultsSection = $("#results-section");
const depthSelect = $("#depth-select");
const scanEnabled = $("#scan-enabled");
const scanOptions = $("#scan-options");

const SCAN_INFO = {
  quick: { title: "Quick", desc: "SMART health + 512 reads spread across the whole disk. Takes seconds." },
  thorough: { title: "Thorough", desc: "SMART health + 8,192 spread reads. Under a minute on a hard drive." },
  full: { title: "Full surface", desc: "Reads every sector with no hashing or writing. Takes about as long as imaging." },
};
const blockSelect = $("#block-select");
const compressionSelect = $("#compression-select");
const segmentSelect = $("#segment-select");

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

function formatSpeed(bps) {
  if (!bps) return "–";
  return `${(bps / 1e6).toFixed(bps < 1e7 ? 1 : 0)} MB/s`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return "–";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(sec).padStart(2, "0")}s`;
}

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

// ---------- Capture / Triage tabs ----------
function setMode(mode) {
  document.querySelectorAll("#mode-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  $("#capture-view").classList.toggle("hidden", mode !== "capture");
  $("#triage-view").classList.toggle("hidden", mode !== "triage");
  try {
    localStorage.setItem("qc-mode", mode);
  } catch (e) {
    /* storage can be unavailable; the tab just isn't remembered */
  }
}

document.querySelectorAll("#mode-tabs button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
try {
  if (localStorage.getItem("qc-mode") === "triage") setMode("triage");
} catch (e) {
  /* ignore */
}

// ---------- Health / options ----------
async function loadHealth() {
  try {
    const data = await api("health");
    const banner = $("#admin-banner");
    const notes = [];
    if (!data.admin) {
      notes.push("Not running as Administrator/root — physical devices can't be opened. Restart Quick Capture as Administrator (Windows) or with sudo/pkexec (Linux/macOS).");
    }
    if (!data.smartctl) {
      notes.push("smartctl not found — the scan will rely on reading sectors only. Install smartmontools for SMART health checks.");
    }
    if (notes.length) {
      banner.innerHTML = notes.map(escapeHtml).join("<br>");
      banner.classList.remove("hidden");
    }
  } catch (e) {
    console.error("Health check failed", e);
  }
}

async function loadOptions() {
  const data = await api("options");
  scanOptions.innerHTML = Object.keys(data.scan_modes)
    .filter((mode) => mode !== "skip" && SCAN_INFO[mode])
    .map((mode) => `<label class="scan-option">
        <input type="radio" name="scan-mode" value="${mode}" ${mode === "quick" ? "checked" : ""} />
        <span><div class="opt-title">${SCAN_INFO[mode].title}</div><div class="opt-desc">${SCAN_INFO[mode].desc}</div></span>
      </label>`)
    .join("");
  scanOptions.querySelectorAll("input").forEach((el) => el.addEventListener("change", updateScanUi));
  blockSelect.innerHTML = "";
  for (const mb of data.block_sizes_mb) {
    blockSelect.add(new Option(`${mb} MiB`, mb, mb === 8, mb === 8));
  }
  depthSelect.innerHTML = "";
  for (const d of data.io_depths) {
    depthSelect.add(new Option(d === 2 ? "2 (default)" : d >= 4 ? `${d} (NVMe)` : String(d), d, d === 2, d === 2));
  }
  updateScanUi();
}

// ---------- Scan box ----------
function selectedScanMode() {
  const checked = scanOptions.querySelector("input:checked");
  return checked ? checked.value : "quick";
}

function updateScanUi() {
  const on = scanEnabled.checked;
  const pill = $("#scan-summary");
  pill.textContent = on ? `${SCAN_INFO[selectedScanMode()].title} · before imaging` : "manual only";
  pill.classList.toggle("on", on);
}

scanEnabled.addEventListener("change", updateScanUi);

// ---------- Devices ----------
async function loadDevices() {
  deviceList.innerHTML = `<p class="placeholder">Scanning for devices…</p>`;
  try {
    const data = await api("devices");
    state.devices = data.devices;
    renderDevices();
  } catch (e) {
    deviceList.innerHTML = `<p class="placeholder">${escapeHtml(e.message)}</p>`;
  }
}

const isPhysical = (d) => d.kind === "disk";

function renderDevices() {
  document.querySelector('[data-count="physical"]').textContent = state.devices.length ? `(${state.devices.filter(isPhysical).length})` : "";
  document.querySelector('[data-count="logical"]').textContent = state.devices.length ? `(${state.devices.filter((d) => !isPhysical(d)).length})` : "";
  const shown = state.devices.filter((d) => (state.deviceFilter === "physical") === isPhysical(d));
  if (!shown.length) {
    const what = state.deviceFilter === "physical" ? "physical drives" : "logical volumes or partitions";
    deviceList.innerHTML = `<p class="placeholder">No ${what} found. Enter a device path or image file below.</p>`;
    return;
  }
  deviceList.innerHTML = shown
    .map((d) => {
      const badges = [
        d.kind !== "disk" ? `<span class="badge">${escapeHtml(d.kind)}</span>` : "",
        d.removable ? `<span class="badge rem">removable</span>` : "",
        d.system ? `<span class="badge sys">system</span>` : "",
      ].join("");
      const mounts = d.mountpoints && d.mountpoints.length ? ` · ${escapeHtml(d.mountpoints.join(", "))}` : "";
      return `<div class="device-row ${d.kind === "partition" ? "partition" : ""} ${d.path === state.source ? "selected" : ""}"
                   data-path="${escapeHtml(d.path)}">
        <div class="dev-main">
          <div class="dev-name">${escapeHtml(d.model || d.name)}</div>
          <div class="dev-path">${escapeHtml(d.path)}${mounts}</div>
        </div>
        ${badges}
        <span class="dev-size">${formatBytes(d.size)}</span>
      </div>`;
    })
    .join("");
  deviceList.querySelectorAll(".device-row").forEach((el) => {
    el.addEventListener("click", () => setSource(el.dataset.path));
  });
}

async function setSource(path) {
  state.source = path;
  state.sourceInfo = null;
  sourceInput.value = path;
  renderDevices();
  updateButtons();
  const statusEl = $("#source-status");
  if (!path) {
    statusEl.textContent = "";
    statusEl.className = "file-status";
    return;
  }
  statusEl.textContent = "Opening…";
  statusEl.className = "file-status";
  try {
    const data = await api("source_info", path);
    state.sourceInfo = data;
    const dev = state.devices.find((d) => d.path === path);
    const sysWarn = dev && dev.system ? " — ⚠ this holds the running OS; contents will change while imaging" : "";
    statusEl.textContent = `Readable · ${formatBytes(data.size)} (${data.size.toLocaleString()} bytes) · ${data.sector_size}-byte sectors${sysWarn}`;
    statusEl.className = `file-status ${sysWarn ? "err" : "ok"}`;
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = "file-status err";
  }
  updateButtons();
}

sourceInput.addEventListener("change", () => setSource(sourceInput.value.trim()));
$("#refresh-devices").addEventListener("click", loadDevices);
document.querySelectorAll("#device-filter button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.deviceFilter = btn.dataset.filter;
    document.querySelectorAll("#device-filter button").forEach((b) => b.setAttribute("aria-selected", String(b === btn)));
    renderDevices();
  });
});

// ---------- Destination ----------
async function setOutput(path) {
  state.outputDir = path;
  outputInput.value = path;
  const statusEl = $("#output-status");
  updateButtons();
  if (!path) {
    statusEl.textContent = "";
    return;
  }
  try {
    const data = await api("folder_info", path);
    statusEl.textContent = data.free !== null ? `${formatBytes(data.free)} free` : "OK";
    statusEl.className = "file-status ok";
    if (state.sourceInfo && data.free !== null && data.free < state.sourceInfo.size) {
      statusEl.textContent += ` — less than the source size (${formatBytes(state.sourceInfo.size)}); only a compressed E01 may fit`;
      statusEl.className = "file-status err";
    }
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = "file-status err";
  }
}

outputInput.addEventListener("change", () => setOutput(outputInput.value.trim()));
nameInput.addEventListener("input", updateButtons);

document.querySelectorAll("#format-toggle button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.format = btn.dataset.format;
    document.querySelectorAll("#format-toggle button").forEach((b) => b.setAttribute("aria-selected", String(b === btn)));
    $("#compression-label").classList.toggle("hidden", state.format !== "e01");
  });
});

function updateButtons() {
  const busy = !!state.pollTimer;
  scanBtn.disabled = busy || !state.source;
  runBtn.disabled = busy || !(state.source && state.outputDir && nameInput.value.trim());
}

// ---------- Browse (the OS file / folder dialog) ----------
document.querySelectorAll("#capture-view .browse-btn").forEach((btn) =>
  btn.addEventListener("click", async () => {
    const input = btn.closest(".path-input-row").querySelector(".path-input");
    const isDevice = input.value.startsWith("\\\\.\\") || input.value.startsWith("/dev/");
    btn.disabled = true;
    try {
      const data = await api("pick", btn.dataset.browse || "file", isDevice ? "" : input.value);
      if (data.path) {
        input.value = data.path;
        input.dispatchEvent(new Event("change"));
      }
    } catch (e) {
      const statusEl = btn.closest(".picker-card, .details-field").querySelector(".file-status");
      statusEl.textContent = e.message;
      statusEl.className = "file-status err";
    } finally {
      btn.disabled = false;
    }
  })
);

// ---------- Run ----------
runBtn.addEventListener("click", () => start(false));
scanBtn.addEventListener("click", () => start(true));
cancelBtn.addEventListener("click", cancelJob);
$("#abort-btn").addEventListener("click", cancelJob);
$("#proceed-btn").addEventListener("click", proceedJob);

function collectCase() {
  const c = {};
  document.querySelectorAll("[data-case]").forEach((el) => (c[el.dataset.case] = el.value.trim()));
  return c;
}

async function start(scanOnly) {
  errorSection.classList.add("hidden");
  alertSection.classList.add("hidden");
  scanSection.classList.add("hidden");
  resultsSection.classList.add("hidden");
  progressSection.classList.remove("hidden");
  progressFill.style.width = "0%";
  progressStage.textContent = "Starting…";
  progressSpeed.textContent = "";
  progressLabel.textContent = "";
  state.alertShownFor = null;

  const hashes = [...document.querySelectorAll("#hash-checks input:checked")].map((el) => el.value);
  if (!scanOnly && !hashes.length) {
    showError("Select at least one hash algorithm.");
    resetControls();
    return;
  }

  const body = {
    source: state.source,
    output_dir: state.outputDir,
    name: nameInput.value.trim(),
    format: state.format,
    hashes: hashes.length ? hashes : ["md5"],
    block_size_mb: Number(blockSelect.value),
    io_depth: Number(depthSelect.value),
    compression: compressionSelect.value,
    segment_size_mb: Number(segmentSelect.value),
    scan_mode: scanEnabled.checked || scanOnly ? selectedScanMode() : "skip",
    scan_only: scanOnly,
    verify: $("#verify-check").checked,
    case: collectCase(),
  };

  try {
    const data = await api("acquire", body);
    state.jobId = data.job_id;
    cancelBtn.classList.remove("hidden");
    pollJob();
  } catch (e) {
    showError(e.message);
    resetControls();
  }
}

function pollJob() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      const job = await api("job", state.jobId).catch(() => {
        throw new Error("Lost track of job");
      });
      updateProgress(job);
      if (job.scan) showScan(job.scan);

      if (job.status === "awaiting") {
        showAlert(job);
      } else {
        alertSection.classList.add("hidden");
      }

      if (job.status === "done") {
        stopPolling();
        progressSection.classList.add("hidden");
        if (job.result) showResults(job);
        resetControls();
      } else if (job.status === "error") {
        stopPolling();
        showError(job.error || "Acquisition failed");
        resetControls();
      } else if (job.status === "cancelled") {
        stopPolling();
        progressStage.textContent = "Cancelled";
        progressSpeed.textContent = "";
        progressLabel.textContent = "No image was kept.";
        resetControls();
      }
    } catch (e) {
      stopPolling();
      showError(e.message);
      resetControls();
    }
  }, 500);
  updateButtons();
}

function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

function updateProgress(job) {
  const pct = Math.max(0, Math.min(100, job.percent || 0));
  progressFill.style.width = `${pct}%`;
  progressStage.textContent = job.stage;
  if (job.stage === "imaging" || job.stage === "verifying") {
    progressSpeed.textContent = formatSpeed(job.speed);
    const bad = job.bad_sectors ? ` · <span class="bad-text">${job.bad_sectors} bad sector(s) zero-filled</span>` : "";
    progressLabel.innerHTML = `${pct.toFixed(1)}% · ${formatBytes(job.bytes_done)} of ${formatBytes(job.total_bytes)}
      · avg ${formatSpeed(job.avg_speed)} · ETA ${formatDuration(job.eta)}${bad}`;
  } else {
    progressSpeed.textContent = "";
    progressLabel.textContent = `${pct.toFixed(1)}%${job.detail ? " · " + job.detail : ""}`;
  }
}

const LEVEL_LABELS = { ok: "OK", bad: "Problem", info: "Note" };

function findingsHtml(findings) {
  return findings
    .map((f) => `<li data-level="${escapeHtml(f.level)}"><span class="level">${LEVEL_LABELS[f.level] || escapeHtml(f.level)}</span>
      <strong>${escapeHtml(f.title)}</strong><span>${escapeHtml(f.detail)}</span></li>`)
    .join("");
}

function showAlert(job) {
  if (state.alertShownFor === job.id) return;
  state.alertShownFor = job.id;
  $("#alert-findings").innerHTML = findingsHtml(job.scan.findings.filter((f) => f.level === "bad"));
  alertSection.classList.remove("hidden");
  progressStage.textContent = "Waiting for your decision";
}

function card(label, value, extraClass = "", style = "") {
  return `<div class="score-card"><div class="metric-name">${escapeHtml(label)}</div>
    <div class="metric-value ${extraClass}" style="${style}">${value}</div></div>`;
}

function showScan(t) {
  scanSection.classList.remove("hidden");
  const verdict = $("#scan-verdict");
  verdict.textContent = t.verdict.toUpperCase();
  verdict.className = `verdict ${t.verdict}`;
  const bad = t.bad_sectors.length;
  $("#scan-cards").innerHTML =
    card("Bad sectors found", bad.toLocaleString(), "", `color:${bad ? "var(--bad)" : "var(--good)"}`) +
    card("Probe read speed", formatSpeed(t.read_speed), "small") +
    card("Est. imaging time", formatDuration(t.estimated_seconds), "small") +
    card("SMART", t.smart && t.smart.available ? (t.smart.issues.length ? "Defects" : "Healthy") : "N/A", "small",
      t.smart && t.smart.available ? `color:${t.smart.issues.length ? "var(--bad)" : "var(--good)"}` : "");
  $("#scan-findings").innerHTML = findingsHtml(t.findings);

  const smartDetails = $("#smart-details");
  const attrs = (t.smart && t.smart.attributes) || [];
  smartDetails.classList.toggle("hidden", !attrs.length);
  $("#smart-table").innerHTML = `<table class="meta-table">${attrs
    .map((a) => `<tr><td>${escapeHtml(a.name)}</td><td class="${a.flag ? "bad-text" : ""}">${escapeHtml(String(a.value))}</td></tr>`)
    .join("")}</table>`;
}

function showResults(job) {
  const r = job.result;
  resultsSection.classList.remove("hidden");
  state.reportJobId = job.id;

  $("#result-cards").innerHTML =
    card("Average speed", formatSpeed(r.avg_speed)) +
    card("Duration", formatDuration(r.duration), "small") +
    card("Media size", formatBytes(r.total_bytes), "small") +
    card("Image size", formatBytes(r.image_bytes), "small") +
    card("Bad sectors", r.bad_sectors.toLocaleString(), "small", `color:${r.bad_sectors ? "var(--bad)" : "var(--good)"}`) +
    (r.bottleneck ? card("Limited by", escapeHtml(r.bottleneck.label), "small limiter") : "");

  const labels = { md5: "MD5", sha1: "SHA-1", sha256: "SHA-256" };
  let rows = "";
  for (const [algo, digest] of Object.entries(r.hashes)) {
    let status = "";
    if (r.verify) {
      status = r.verify.hashes[algo] === digest ? `<span class="ok-text">✔ verified</span>` : `<span class="bad-text">✖ mismatch</span>`;
    }
    rows += `<tr><td>${labels[algo] || algo}</td><td class="mono">${digest}</td><td>${status}</td></tr>`;
  }
  $("#hash-table").innerHTML = `<table class="meta-table">${rows}</table>`;
  $("#output-files").textContent = [...r.paths, r.log_path].filter(Boolean).join("\n");
  state.lastImagePath = r.paths[0] || null;
}

// ---------- Reports ----------
async function reportAction(btn, method, ...args) {
  if (!state.reportJobId) return;
  btn.disabled = true;
  try {
    const data = await api(method, state.reportJobId, ...args);
    if (data.path) $("#output-files").textContent += `\n${data.path}`;
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
  }
}

// Hand the finished image to the Triage tab (E01 or DD; the first segment opens the whole set).
$("#triage-handoff-btn").addEventListener("click", () => {
  const first = state.lastImagePath;
  if (!first) return;
  setMode("triage");
  window.triageImage(first);
  window.scrollTo({ top: 0, behavior: "smooth" });
});

$("#report-view-btn").addEventListener("click", (e) => reportAction(e.currentTarget, "view_report"));
$("#report-html-btn").addEventListener("click", (e) => reportAction(e.currentTarget, "save_report", "html"));
$("#report-json-btn").addEventListener("click", (e) => reportAction(e.currentTarget, "save_report", "json"));

async function cancelJob() {
  if (!state.jobId) return;
  try {
    await api("cancel", state.jobId);
  } catch (e) {
    console.error(e);
  }
}

async function proceedJob() {
  if (!state.jobId) return;
  alertSection.classList.add("hidden");
  try {
    await api("proceed", state.jobId);
  } catch (e) {
    console.error(e);
  }
}

function resetControls() {
  cancelBtn.classList.add("hidden");
  updateButtons();
}

function showError(message) {
  errorSection.textContent = message;
  errorSection.classList.remove("hidden");
  progressSection.classList.add("hidden");
}

// ---------- init ----------
loadHealth();
loadOptions();
loadDevices();
updateButtons();
