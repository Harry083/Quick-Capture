// The Triage tab: OS, device, users and last saved file from an E01 or raw image (backend/triage.py).
// Shares api(), escapeHtml() and formatBytes() with app.js; everything else is scoped to this function.
(() => {
  const state = {  // the Triage tab's own state; the Capture tab's lives in app.js
    image: "",
    imageInfo: null,
    jobId: null,
    pollTimer: null,
    result: null,
    systemIndex: 0,
    startedAt: 0,
  };

  const $ = (sel) => document.querySelector(sel);

  const imageInput = $("#tr-image-input");
  const runBtn = $("#tr-run-btn");
  const cancelBtn = $("#tr-cancel-btn");
  const progressSection = $("#tr-progress-section");
  const progressFill = $("#tr-progress-bar-fill");
  const progressLabel = $("#tr-progress-label");
  const progressStage = $("#tr-progress-stage");
  const progressTime = $("#tr-progress-time");
  const errorSection = $("#tr-error-section");
  const resultsSection = $("#tr-results-section");

  const OS_LABELS = [
    ["name", "Operating system"], ["edition", "Edition"], ["version", "Version"], ["build", "Build"],
    ["codename", "Codename"], ["architecture", "Architecture"], ["installed", "Installed"],
    ["registered_owner", "Registered owner"], ["registered_org", "Registered organisation"],
    ["product_id", "Product ID"], ["kernel_versions", "Kernels in /boot"],
  ];
  const DEVICE_LABELS = [
    ["computer_name", "Computer name"], ["domain", "Domain"], ["manufacturer", "Manufacturer"], ["model", "Model"],
    ["bios", "BIOS"], ["ip_addresses", "IP addresses"], ["time_zone", "Time zone"], ["utc_offset", "UTC offset"],
    ["last_shutdown", "Last shutdown"], ["last_mounted", "Last mounted"], ["last_mounted_at", "Last mount point"],
    ["last_written", "Volume last written"],
  ];
  const HEADER_LABELS = [
    ["case_number", "Case number"], ["evidence_number", "Evidence number"], ["examiner", "Examiner"],
    ["description", "Description"], ["notes", "Notes"], ["model", "Source model"], ["serial", "Source serial"],
    ["acquired", "Acquired"], ["acquisition_software", "Acquired with"], ["acquisition_os", "Acquisition OS"],
  ];

  function formatSeconds(s) {
    if (s === null || s === undefined || !isFinite(s)) return "–";
    return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  }

  function show(value) {
    return Array.isArray(value) ? value.join(", ") : value;
  }

  function table(pairs, empty = "Nothing recorded") {
    const rows = pairs
      .filter(([, v]) => v !== undefined && v !== null && v !== "" && !(Array.isArray(v) && !v.length))
      .map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(show(v))}</td></tr>`)
      .join("");
    return rows ? `<table class="meta-table">${rows}</table>` : `<p class="placeholder">${escapeHtml(empty)}</p>`;
  }

  const labelled = (obj, labels) => labels.map(([key, label]) => [label, (obj || {})[key]]);

  function card(label, value, sub = "", extraClass = "") {
    return `<div class="score-card"><div class="metric-name">${escapeHtml(label)}</div>
      <div class="metric-value ${extraClass}">${value}</div>${sub ? `<div class="metric-sub">${sub}</div>` : ""}</div>`;
  }

  // ---------- Image ----------
  async function setImage(path) {
    state.image = path;
    state.imageInfo = null;
    imageInput.value = path;
    const statusEl = $("#tr-image-status");
    const meta = $("#tr-image-meta");
    const pill = $("#tr-image-pill");
    meta.classList.add("hidden");
    pill.textContent = "no image";
    pill.classList.remove("on");
    updateButtons();
    if (!path) {
      statusEl.textContent = "";
      statusEl.className = "file-status";
      return;
    }
    statusEl.textContent = "Opening…";
    statusEl.className = "file-status";
    try {
      const info = await api("image_info", path);
      state.imageInfo = info;
      const segs = info.segments.length > 1 ? ` · ${info.segments.length} segments` : "";
      statusEl.textContent = `Readable · ${info.format} · ${formatBytes(info.size)} (${info.size.toLocaleString()} bytes)${segs}`;
      statusEl.className = "file-status ok";
      pill.textContent = info.format;
      pill.classList.add("on");
      const h = info.header || {};
      const bits = [
        h.case_number && `Case ${h.case_number}`, h.evidence_number && `Evidence ${h.evidence_number}`,
        h.examiner && `Examiner ${h.examiner}`, h.model && `Source ${h.model}${h.serial ? " · " + h.serial : ""}`,
        h.acquired && `Acquired ${h.acquired}`,
      ].filter(Boolean);
      if (bits.length) {
        meta.innerHTML = bits.map((b) => `<span class="badge">${escapeHtml(b)}</span>`).join("");
        meta.classList.remove("hidden");
      }
    } catch (e) {
      statusEl.textContent = e.message;
      statusEl.className = "file-status err";
    }
    updateButtons();
  }

  imageInput.addEventListener("change", () => setImage(imageInput.value.trim().replace(/^"|"$/g, "")));

  $("#tr-browse-image").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      const data = await api("pick", "image", imageInput.value);
      if (data.path) setImage(data.path);
    } catch (e) {
      $("#tr-image-status").textContent = e.message;
      $("#tr-image-status").className = "file-status err";
    } finally {
      btn.disabled = false;
    }
  });

  function updateButtons() {
    runBtn.disabled = !!state.pollTimer || !state.imageInfo;
  }

  // ---------- Run ----------
  runBtn.addEventListener("click", start);
  cancelBtn.addEventListener("click", async () => {
    if (state.jobId) await api("triage_cancel", state.jobId).catch(console.error);
  });

  async function start() {
    errorSection.classList.add("hidden");
    resultsSection.classList.add("hidden");
    progressSection.classList.remove("hidden");
    progressFill.style.width = "0%";
    progressStage.textContent = "Starting…";
    progressLabel.textContent = "";
    progressTime.textContent = "";
    state.startedAt = Date.now();
    try {
      const data = await api("triage", state.image);
      state.jobId = data.job_id;
      cancelBtn.classList.remove("hidden");
      poll();
    } catch (e) {
      showError(e.message);
      resetControls();
    }
  }

  function poll() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      try {
        const job = await api("triage_job", state.jobId);
        const pct = Math.max(0, Math.min(100, job.percent || 0));
        progressFill.style.width = `${pct}%`;
        progressStage.textContent = job.stage;
        progressLabel.textContent = `${pct.toFixed(0)}%`;
        progressTime.textContent = formatSeconds((Date.now() - state.startedAt) / 1000);
        if (job.status === "done") {
          stopPolling();
          progressSection.classList.add("hidden");
          showResults(job.result);
          resetControls();
        } else if (job.status === "error") {
          stopPolling();
          showError(job.error || "Triage failed");
          resetControls();
        } else if (job.status === "cancelled") {
          stopPolling();
          progressStage.textContent = "Cancelled";
          progressLabel.textContent = "";
          resetControls();
        }
      } catch (e) {
        stopPolling();
        showError(e.message);
        resetControls();
      }
    }, 300);
    updateButtons();
  }

  function stopPolling() {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
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

  // ---------- Results ----------
  const LEVEL_LABELS = { ok: "OK", bad: "Problem", info: "Note" };

  function showResults(r) {
    state.result = r;
    state.systemIndex = 0;
    resultsSection.classList.remove("hidden");
    $("#tr-saved-files").textContent = "";
    $("#tr-results-time").textContent = `took ${formatSeconds(r.duration)}`;

    const tabs = $("#tr-system-tabs");
    tabs.classList.toggle("hidden", r.systems.length < 2);
    tabs.innerHTML = r.systems
      .map((s, i) => `<button type="button" role="tab" data-i="${i}" aria-selected="${i === 0}">${escapeHtml((s.os && s.os.name) || s.kind)}
        <span class="count">partition ${escapeHtml(s.volume.index)}</span></button>`)
      .join("");
    tabs.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => {
        state.systemIndex = Number(b.dataset.i);
        tabs.querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
        renderSystem();
      })
    );
    renderSystem();

    $("#tr-findings").innerHTML = r.findings.length
      ? r.findings
          .map((f) => `<li data-level="${escapeHtml(f.level)}"><span class="level">${LEVEL_LABELS[f.level] || escapeHtml(f.level)}</span>
            <strong>${escapeHtml(f.title)}</strong><span>${escapeHtml(f.detail)}</span></li>`)
          .join("")
      : `<li data-level="ok"><span class="level">OK</span><strong>Nothing to flag</strong><span>Every partition was read and nothing was encrypted or damaged.</span></li>`;

    const img = r.image;
    $("#tr-evidence-table").innerHTML = `<div class="meta-block-title">Evidence</div>` + table([
      ["Path", img.path], ["Format", img.format], ["Segments", img.segments.length],
      ["Media size", `${formatBytes(img.size)} (${img.size.toLocaleString()} bytes)`],
      ["Bytes per sector", img.bytes_per_sector], ["Stored MD5", img.md5], ["Stored SHA-1", img.sha1],
    ]);
    $("#tr-header-table").innerHTML = `<div class="meta-block-title">Acquisition header</div>` +
      table(labelled(img.header, HEADER_LABELS), img.format === "E01" ? "No header fields" : "Raw images carry no header");
    $("#tr-volume-table").innerHTML = `<div class="meta-block-title">Partitions · ${escapeHtml(r.partition_scheme)}</div>
      <table class="meta-table grid-table"><tr><th>#</th><th>Type</th><th>Filesystem</th><th>Size</th><th>System</th></tr>${r.volumes
        .map((v) => `<tr><td>${v.index}</td><td>${escapeHtml(v.type)}${v.name ? ` <span class="hint-inline">${escapeHtml(v.name)}</span>` : ""}</td>
          <td>${escapeHtml(v.fs)}</td><td>${formatBytes(v.size)}</td><td>${escapeHtml(v.os || "")}</td></tr>`)
        .join("")}</table>`;
    resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderSystem() {
    const r = state.result;
    const s = r.systems[state.systemIndex];
    const view = $("#tr-system-view");
    if (!s) {
      $("#tr-summary-cards").innerHTML =
        card("Operating system", "Not found", "", "small") + card("Partitions", String(r.volumes.length), "", "small");
      view.innerHTML = "";
      return;
    }
    const os = s.os || {};
    const dev = s.device || {};
    const latest = s.last_saved;
    const osSub = [os.version, os.build && `build ${os.build}`].filter(Boolean).join(" · ");
    const devSub = [dev.manufacturer, dev.model].filter(Boolean).join(" ");
    $("#tr-summary-cards").innerHTML =
      card("Operating system", escapeHtml(os.name || s.kind), escapeHtml(osSub), "text") +
      card("Device", escapeHtml(dev.computer_name || "–"), escapeHtml(devSub || dev.domain || ""), "text") +
      card("Users", String(s.users.length), escapeHtml(s.users.map((u) => u.username).slice(0, 4).join(", ") + (s.users.length > 4 ? "…" : "")), "") +
      card("Last saved", escapeHtml(latest ? latest.name : "–"), escapeHtml(latest ? `${latest.modified} · ${latest.user}` : "No user files found"), "text");

    const users = s.users.length
      ? `<table class="meta-table grid-table users-table"><tr><th>User</th><th>Account</th><th>Last logon</th><th>Logons</th><th>Last saved file</th></tr>${s.users
          .map((u) => {
            const saved = u.last_saved;
            const name = u.full_name && u.full_name !== u.username ? ` <span class="hint-inline">${escapeHtml(u.full_name)}</span>` : "";
            const flags = [u.disabled ? `<span class="badge sys">disabled</span>` : "", u.uid === 0 || u.rid === 500 ? `<span class="badge rem">admin</span>` : ""].join(" ");
            const recent = u.recent_doc ? `<div class="hint-inline">RecentDocs: ${escapeHtml(u.recent_doc.name)}</div>` : "";
            return `<tr><td><strong>${escapeHtml(u.username)}</strong>${name} ${flags}<div class="hint-inline mono">${escapeHtml(u.sid || (u.uid !== undefined ? "uid " + u.uid : ""))}</div></td>
              <td>${escapeHtml(u.account || "")}</td>
              <td>${escapeHtml(u.last_logon || "–")}</td>
              <td>${u.logon_count !== undefined && u.logon_count !== null ? u.logon_count : ""}</td>
              <td>${saved ? `<span class="mono path">${escapeHtml(saved.path)}</span><div class="hint-inline">${escapeHtml(saved.modified)}</div>` : `<span class="hint-inline">–</span>`}${recent}</td></tr>`;
          })
          .join("")}</table>`
      : `<p class="placeholder">No user accounts found.</p>`;

    const recentRows = (s.recent_files || [])
      .map((f) => `<tr><td class="nowrap">${escapeHtml(f.modified)}</td><td>${escapeHtml(f.user)}</td>
        <td class="mono path">${escapeHtml(f.path)}</td><td class="nowrap">${formatBytes(f.size)}</td></tr>`)
      .join("");

    view.innerHTML = `
      <section class="panel">
        <div class="panel-title">02 · System</div>
        <h2>${escapeHtml(os.name || s.kind)} <span class="hint">partition ${escapeHtml(s.volume.index)} · ${escapeHtml(s.volume.fs)} · ${formatBytes(s.volume.size)}</span></h2>
        <div class="two-col">
          <div class="metadata-panel"><div class="meta-block-title">Operating system</div>${table(labelled(os, OS_LABELS))}</div>
          <div class="metadata-panel"><div class="meta-block-title">Device</div>${table(labelled(dev, DEVICE_LABELS))}</div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-title">03 · Users</div>
        <h2>System users <span class="hint">${s.kind === "windows" ? "SAM accounts and profiles" : "/etc/passwd, login accounts only"}</span></h2>
        <div class="metadata-panel">${users}</div>
      </section>
      <section class="panel">
        <div class="panel-title">04 · Last saved</div>
        <h2>Last saved file <span class="hint">newest modified file in a user folder${s.kind === "windows" ? ", from one MFT sweep" : ""}; app data and system files skipped</span></h2>
        ${latest ? `<div class="latest-file">
            <div class="metric-name">${escapeHtml(latest.user)} · ${escapeHtml(latest.modified)} · ${formatBytes(latest.size)}</div>
            <div class="latest-path mono">${escapeHtml(latest.path)}</div>
          </div>` : `<p class="placeholder">No files found in user folders.</p>`}
        ${recentRows ? `<details class="raw-details" open><summary>Recently modified (${s.recent_files.length})</summary>
          <div class="metadata-panel"><table class="meta-table grid-table"><tr><th>Modified</th><th>User</th><th>Path</th><th>Size</th></tr>${recentRows}</table></div>
        </details>` : ""}
      </section>`;
  }

  // ---------- Reports ----------
  async function reportAction(btn, method, ...args) {
    if (!state.jobId) return;
    btn.disabled = true;
    try {
      const data = await api(method, state.jobId, ...args);
      if (data.path) {
        $("#tr-saved-files").textContent = `Saved ${data.path}`;
        $("#tr-saved-files").className = "file-status ok";
      }
    } catch (e) {
      showError(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  $("#tr-report-view-btn").addEventListener("click", (e) => reportAction(e.currentTarget, "triage_view_report"));
  $("#tr-report-html-btn").addEventListener("click", (e) => reportAction(e.currentTarget, "triage_save_report", "html"));
  $("#tr-report-json-btn").addEventListener("click", (e) => reportAction(e.currentTarget, "triage_save_report", "json"));

  // Capture -> Triage handoff: open a finished image in this tab and start straight away.
  window.triageImage = async (path) => {
    await setImage(path);
    if (state.imageInfo && !state.pollTimer) start();
  };

  updateButtons();
})();
