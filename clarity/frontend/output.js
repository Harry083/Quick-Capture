// Exports (frame / video) with progress, and the enhancement report.
(() => {
  const progress = $("#progress-section");
  const fill = $("#progress-bar-fill");
  const stageEl = $("#progress-stage");
  const label = $("#progress-label");
  const timeEl = $("#progress-time");
  const startInput = $("#range-start");
  const endInput = $("#range-end");
  let jobId = null;
  let pollTimer = null;

  CL.on("catalogue", () => {
    const formats = (CL.health && CL.health.video_formats) || {};
    $("#video-format").innerHTML = Object.entries(formats).map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join("");
  });

  CL.on("source", (info) => {
    $("#video-export-box").classList.toggle("hidden", info.count < 2);
    startInput.max = endInput.max = info.count - 1;
    startInput.value = 0;
    endInput.value = info.count - 1;
    renderExports();
  });

  $("#range-start-here").addEventListener("click", () => (startInput.value = CL.state.index));
  $("#range-end-here").addEventListener("click", () => (endInput.value = CL.state.index));

  function setBusy(busy) {
    ["#export-image", "#export-video"].forEach((s) => ($(s).disabled = busy));
  }

  async function start(method, req) {
    showError("");
    try {
      const { job_id } = await api(method, req);
      if (!job_id) return;
      jobId = job_id;
      setBusy(true);
      progress.classList.remove("hidden");
      fill.style.width = "0%";
      poll();
    } catch (e) {
      showError(e.message);
    }
  }

  async function poll() {
    clearTimeout(pollTimer);
    try {
      const job = await api("job", jobId);
      fill.style.width = `${job.percent.toFixed(1)}%`;
      stageEl.textContent = job.status === "running" ? job.stage : job.status;
      timeEl.textContent = job.eta ? `about ${Math.ceil(job.eta)} s left` : "";
      label.textContent = job.total_frames > 1 ? `${job.done_frames} / ${job.total_frames} frames` : "";
      if (job.status === "running") {
        pollTimer = setTimeout(poll, 300);
        return;
      }
      setBusy(false);
      if (job.status === "done") {
        const r = job.result;
        const h = r.hashes[r.path] || {};
        label.innerHTML = `Saved <span class="mono">${escapeHtml(r.folder || r.path)}</span><br><span class="mono">SHA-256 ${escapeHtml(h.sha256 || "")}</span>`;
        renderExports();
      } else if (job.status === "error") {
        progress.classList.add("hidden");
        showError(`Export failed: ${job.error}`);
      } else {
        label.textContent = "Cancelled. No partial video was kept.";
      }
    } catch (e) {
      setBusy(false);
      showError(e.message);
    }
  }

  $("#cancel-btn").addEventListener("click", async () => {
    if (!jobId) return;
    try {
      await api("cancel", jobId);
    } catch (e) {
      /* already finished */
    }
  });

  $("#export-image").addEventListener("click", () =>
    start("export_image", { chain: CL.serializeChain(), index: CL.state.index, ext: $("#image-format").value, bit_depth: Number($("#image-depth").value) }));

  $("#export-video").addEventListener("click", () => {
    const s = Number(startInput.value);
    const e = Number(endInput.value);
    if (!(e >= s)) return showError("The end frame must be after the start frame");
    start("export_video", { chain: CL.serializeChain(), start: s, end: e, format: $("#video-format").value });
  });

  async function renderExports() {
    const el = $("#exports-list");
    try {
      const { exports } = await api("exports");
      const src = CL.state.source && CL.state.source.path;
      const mine = exports.filter((e) => e.source === src);
      if (!mine.length) return el.classList.add("hidden");
      el.innerHTML = `<div class="meta-block-title">Exported this session</div><table class="meta-table">${mine
        .map((e) => `<tr><td>${escapeHtml(e.kind === "image" ? `Frame ${e.frame}` : `Frames ${e.start}–${e.end}`)}</td>
          <td class="mono">${escapeHtml(e.folder || e.path)}<br>SHA-256 ${escapeHtml((e.hashes[e.path] || {}).sha256 || "")}</td></tr>`)
        .join("")}</table>`;
      el.classList.remove("hidden");
    } catch (e) {
      el.classList.add("hidden");
    }
  }

  // ---------- report ----------
  const reportReq = () => ({ chain: CL.serializeChain(), index: CL.state.index, case: CL.caseData(), measurements: CL.state.measurements });

  async function report(method, ...args) {
    showError("");
    const btns = $$(".report-btn");
    btns.forEach((b) => (b.disabled = true));
    try {
      const res = await api(method, reportReq(), ...args);
      if (res.path) {
        label.textContent = `Report saved to ${res.path}`;
        progress.classList.remove("hidden");
        fill.style.width = "100%";
        stageEl.textContent = "report saved";
        timeEl.textContent = "";
      }
    } catch (e) {
      showError(e.message);
    } finally {
      btns.forEach((b) => (b.disabled = false));
    }
  }
  $("#report-view").addEventListener("click", () => report("view_report"));
  $("#report-html").addEventListener("click", () => report("save_report", "html"));
  $("#report-json").addEventListener("click", () => report("save_report", "json"));
})();
