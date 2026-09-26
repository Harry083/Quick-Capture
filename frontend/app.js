const state = {
  source: "",
  sourceInfo: null,
  outputDir: "",
  format: "e01",
  devices: [],
  deviceFilter: "physical",
  browseField: null,
  browsePath: "",
  jobId: null,
  pollTimer: null,
  alertShownFor: null,
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
const triageSection = $("#triage-section");
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

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ---------- Health / options ----------
async function loadHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    const banner = $("#admin-banner");
    const notes = [];
    if (!data.admin) {
      notes.push("Not running as Administrator/root — physical devices can't be opened. Restart Quick Capture from an elevated prompt.");
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
  const res = await fetch("/api/options");
  const data = await res.json();
  scanOptions.innerHTML = Object.keys(data.triage_modes)
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
    const res = await fetch("/api/devices");
    if (!res.ok) throw new Error((await res.json()).detail || "Device scan failed");
    const data = await res.json();
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
    const res = await fetch(`/api/source-info?path=${encodeURIComponent(path)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Cannot open source");
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
    document.querySelectorAll("#device-filter button").forEach((b) => b.classList.toggle("active", b === btn));
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
    const res = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Folder not found");
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
    document.querySelectorAll("#format-toggle button").forEach((b) => b.classList.toggle("active", b === btn));
    $("#compression-label").classList.toggle("hidden", state.format !== "e01");
  });
});

function updateButtons() {
  const busy = !!state.pollTimer;
  scanBtn.disabled = busy || !state.source;
  runBtn.disabled = busy || !(state.source && state.outputDir && nameInput.value.trim());
}

// ---------- Browse modal ----------
const modal = $("#browse-modal");
const browseEntries = $("#browse-entries");
const browseCurrentPath = $("#browse-current-path");
const browseUpBtn = $("#browse-up-btn");
const browseSelectBtn = $("#browse-select-btn");

$("#browse-source").addEventListener("click", () => openBrowse("source"));
$("#browse-output").addEventListener("click", () => openBrowse("output"));
$("#browse-modal-close").addEventListener("click", closeBrowse);
modal.addEventListener("click", (e) => {
  if (e.target === modal) closeBrowse();
});
browseSelectBtn.addEventListener("click", () => {
  if (!state.browsePath) return;
  setOutput(state.browsePath);
  closeBrowse();
});

function parentOf(path) {
  const trimmed = path.replace(/[\\/]+$/, "");
  const idx = Math.max(trimmed.lastIndexOf("\\"), trimmed.lastIndexOf("/"));
  return idx >= 0 ? trimmed.substring(0, idx + 1) : "";
}

function openBrowse(field) {
  state.browseField = field;
  $("#browse-modal-title").textContent = field === "output" ? "Select output folder" : "Select an image file as source";
  browseSelectBtn.classList.toggle("hidden", field !== "output");
  modal.classList.remove("hidden");
  const start = field === "output" ? state.outputDir : state.source && !state.source.startsWith("\\\\.\\") && !state.source.startsWith("/dev/") ? parentOf(state.source) : "";
  loadBrowse(start);
}

function closeBrowse() {
  modal.classList.add("hidden");
}

async function loadBrowse(path) {
  browseEntries.innerHTML = `<div class="browse-entry"><span class="name">Loading…</span></div>`;
  try {
    const mode = state.browseField === "output" ? "dir" : "file";
    const res = await fetch(`/api/browse?path=${encodeURIComponent(path || "")}&mode=${mode}`);
    if (!res.ok) throw new Error("Could not list directory");
    const data = await res.json();
    state.browsePath = data.path;
    browseCurrentPath.textContent = data.path || "Drives";
    browseUpBtn.disabled = !data.parent && !data.path;
    browseSelectBtn.disabled = !data.path;

    let html = "";
    if (data.parent !== null && data.parent !== undefined) {
      html += entryRow({ name: "..", path: data.parent, type: "dir" });
    } else if (data.path) {
      html += entryRow({ name: "..", path: "", type: "dir" });
    }
    for (const e of data.entries) html += entryRow(e);
    browseEntries.innerHTML = html || `<div class="browse-entry"><span class="name">(empty)</span></div>`;

    browseEntries.querySelectorAll(".browse-entry[data-type='dir']").forEach((el) => {
      el.addEventListener("click", () => loadBrowse(el.dataset.path));
    });
    browseEntries.querySelectorAll(".browse-entry[data-type='file']").forEach((el) => {
      el.addEventListener("click", () => {
        setSource(el.dataset.path);
        closeBrowse();
      });
    });
  } catch (e) {
    browseEntries.innerHTML = `<div class="browse-entry"><span class="name">Error: ${escapeHtml(e.message)}</span></div>`;
  }
}

function entryRow(entry) {
  const icon = entry.type === "dir" ? "&#128193;" : "&#128190;";
  const size = entry.type === "file" ? `<span class="size">${formatBytes(entry.size)}</span>` : "";
  return `<div class="browse-entry" data-type="${entry.type}" data-path="${escapeHtml(entry.path)}">
    <span class="icon">${icon}</span>
    <span class="name">${escapeHtml(entry.name)}</span>
    ${size}
  </div>`;
}

browseUpBtn.addEventListener("click", () => {
  if (!state.browsePath) return;
  loadBrowse(parentOf(state.browsePath));
});

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
  triageSection.classList.add("hidden");
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
    triage_mode: scanEnabled.checked || scanOnly ? selectedScanMode() : "skip",
    triage_only: scanOnly,
    verify: $("#verify-check").checked,
    case: collectCase(),
  };

  try {
    const res = await fetch("/api/acquire", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({ detail: "Failed to start" }));
    if (!res.ok) throw new Error(data.detail || "Failed to start");
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
      const res = await fetch(`/api/jobs/${state.jobId}`);
      if (!res.ok) throw new Error("Lost track of job");
      const job = await res.json();
      updateProgress(job);
      if (job.triage) showTriage(job.triage);

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

function findingsHtml(findings) {
  return findings
    .map((f) => `<li class="lvl-${f.level}"><strong>${escapeHtml(f.title)}</strong> <span class="detail">— ${escapeHtml(f.detail)}</span></li>`)
    .join("");
}

function showAlert(job) {
  if (state.alertShownFor === job.id) return;
  state.alertShownFor = job.id;
  $("#alert-findings").innerHTML = findingsHtml(job.triage.findings.filter((f) => f.level === "bad"));
  alertSection.classList.remove("hidden");
  progressStage.textContent = "Waiting for your decision";
}

function card(label, value, extraClass = "", style = "") {
  return `<div class="score-card"><div class="metric-name">${escapeHtml(label)}</div>
    <div class="metric-value ${extraClass}" style="${style}">${value}</div></div>`;
}

function showTriage(t) {
  triageSection.classList.remove("hidden");
  const verdict = $("#triage-verdict");
  verdict.textContent = t.verdict.toUpperCase();
  verdict.className = `verdict ${t.verdict}`;
  const bad = t.bad_sectors.length;
  $("#triage-cards").innerHTML =
    card("Bad sectors found", bad.toLocaleString(), "", `color:${bad ? "var(--bad)" : "var(--good)"}`) +
    card("Probe read speed", formatSpeed(t.read_speed), "small") +
    card("Est. imaging time", formatDuration(t.estimated_seconds), "small") +
    card("SMART", t.smart && t.smart.available ? (t.smart.issues.length ? "Defects" : "Healthy") : "N/A", "small",
      t.smart && t.smart.available ? `color:${t.smart.issues.length ? "var(--bad)" : "var(--good)"}` : "");
  $("#triage-findings").innerHTML = findingsHtml(t.findings);

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
  $("#report-html-link").href = `/api/jobs/${job.id}/report.html`;
  const jsonLink = $("#report-json-link");
  jsonLink.href = `/api/jobs/${job.id}/report.json`;
  jsonLink.setAttribute("download", `quick-capture-report-${job.id}.json`);

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
}

async function cancelJob() {
  if (!state.jobId) return;
  try {
    await fetch(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
  } catch (e) {
    console.error(e);
  }
}

async function proceedJob() {
  if (!state.jobId) return;
  alertSection.classList.add("hidden");
  try {
    await fetch(`/api/jobs/${state.jobId}/proceed`, { method: "POST" });
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
