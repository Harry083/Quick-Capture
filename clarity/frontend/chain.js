// The filter chain editor: add, reorder, enable and tune filters. Every change re-renders the preview.
(() => {
  const list = $("#chain-list");
  const addSelect = $("#add-select");
  const bypassAll = $("#bypass-all");
  let uidCounter = 0;

  const clone = (v) => JSON.parse(JSON.stringify(v));
  const isLog = (p) => p.min > 0 && p.max / p.min >= 1000;
  const decimals = (p) => (p.kind === "int" ? 0 : Math.min(4, Math.max(0, -Math.floor(Math.log10(p.step || 0.01)))));

  function defaults(f) {
    return Object.fromEntries(f.params.map((p) => [p.key, clone(p.default)]));
  }

  CL.serializeChain = (opts = {}) =>
    CL.state.chain
      .slice(0, opts.upto === undefined ? undefined : opts.upto)
      .map((s) => ({ id: s.id, enabled: s.enabled && !bypassAll.checked, params: s.params }));

  function changed(structure = false) {
    if (structure) render();
    updateCount();
    CL.emit("chain");
  }

  function updateCount() {
    const n = CL.state.chain.length;
    const on = CL.state.chain.filter((s) => s.enabled).length;
    const pill = $("#chain-count");
    pill.textContent = n ? (bypassAll.checked ? "bypassed" : `${on} of ${n} on`) : "empty";
    pill.classList.toggle("on", n > 0 && !bypassAll.checked);
  }

  CL.loadChain = (chain) => {
    CL.state.chain = (chain || [])
      .filter((s) => CL.state.filters[s.id])
      .map((s) => ({ uid: ++uidCounter, id: s.id, enabled: s.enabled !== false, params: { ...defaults(CL.state.filters[s.id]), ...clone(s.params || {}) }, open: false }));
    changed(true);
  };

  CL.addFilter = (id) => {
    const f = CL.state.filters[id];
    if (!f) return;
    CL.state.chain.forEach((s) => (s.open = false));
    CL.state.chain.push({ uid: ++uidCounter, id, enabled: true, params: defaults(f), open: true });
    changed(true);
    const last = list.lastElementChild;
    if (last) last.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };

  // ---------- add select ----------
  CL.on("catalogue", (cat) => {
    addSelect.innerHTML = `<option value="">Choose a filter…</option>` + cat.categories
      .map(([cid, cname]) => {
        const opts = cat.filters.filter((f) => f.category === cid).map((f) => `<option value="${f.id}">${escapeHtml(f.name)}</option>`).join("");
        return `<optgroup label="${escapeHtml(cname)}">${opts}</optgroup>`;
      })
      .join("");
  });

  $("#add-btn").addEventListener("click", () => {
    if (addSelect.value) CL.addFilter(addSelect.value);
    addSelect.value = "";
  });
  addSelect.addEventListener("change", () => {
    if (addSelect.value) CL.addFilter(addSelect.value);
    addSelect.value = "";
  });

  $("#chain-clear").addEventListener("click", () => {
    if (!CL.state.chain.length || !confirm("Remove every filter from the chain?")) return;
    CL.cancelPick && CL.cancelPick();
    CL.state.chain = [];
    changed(true);
  });
  bypassAll.addEventListener("change", () => changed(false));

  // ---------- parameter controls ----------
  function formatValue(p, v) {
    return Number(v).toFixed(decimals(p));
  }

  function pointsText(p, pts) {
    if (!pts || !pts.length) return `<span class="muted">not set</span>`;
    return pts.map((pt, i) => `${escapeHtml(p.labels[i] || `point ${i + 1}`)} <span class="mono">(${pt[0].toFixed(0)}, ${pt[1].toFixed(0)})</span>`).join("<br>");
  }

  function paramHtml(step, p) {
    const v = step.params[p.key];
    const help = p.help ? `<div class="param-help">${escapeHtml(p.help)}</div>` : "";
    const unit = p.unit ? `<span class="unit">${escapeHtml(p.unit)}</span>` : "";
    if (p.kind === "bool") {
      return `<div class="param"><label class="check"><input type="checkbox" data-key="${p.key}" ${v ? "checked" : ""} /><span>${escapeHtml(p.label)}</span></label>${help}</div>`;
    }
    if (p.kind === "choice") {
      const opts = p.choices.map(([val, label]) => `<option value="${escapeHtml(val)}" ${val === v ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
      return `<div class="param"><label class="param-label">${escapeHtml(p.label)}<select data-key="${p.key}">${opts}</select></label>${help}</div>`;
    }
    if (p.kind === "points") {
      return `<div class="param points-param" data-points="${p.key}">
        <div class="param-label">${escapeHtml(p.label)} <span class="hint-inline">${p.count} point${p.count > 1 ? "s" : ""}</span></div>
        <div class="points-value">${pointsText(p, v)}</div>
        <div class="points-actions">
          <button type="button" class="btn small-btn btn-primary" data-pick="${p.key}">⌖ Pick on image</button>
          <button type="button" class="btn small-btn" data-clear-points="${p.key}">Clear</button>
        </div>${help}</div>`;
    }
    const sliderAttrs = isLog(p) ? `min="0" max="1000" step="1" value="${Math.round((1000 * Math.log(v / p.min)) / Math.log(p.max / p.min))}"` : `min="${p.min}" max="${p.max}" step="${p.step}" value="${v}"`;
    return `<div class="param"><label class="param-label">${escapeHtml(p.label)} ${unit}</label>
      <div class="param-slider"><input type="range" data-slider="${p.key}" ${sliderAttrs} />
      <input type="number" class="path-input num" data-key="${p.key}" min="${p.min}" max="${p.max}" step="${p.step}" value="${formatValue(p, v)}" /></div>${help}</div>`;
  }

  function stepHtml(step, i) {
    const f = CL.state.filters[step.id];
    return `<div class="step-card ${step.open ? "open" : ""} ${step.enabled ? "" : "disabled"}" data-uid="${step.uid}">
      <div class="step-head">
        <label class="step-toggle" title="Apply this step"><input type="checkbox" data-enable ${step.enabled ? "checked" : ""} /></label>
        <button type="button" class="step-title" data-toggle-open>
          <span class="step-no">${i + 1}</span><span class="step-name">${escapeHtml(f.name)}</span>${f.temporal ? `<span class="badge">multi-frame</span>` : ""}
        </button>
        <span class="step-tools">
          <button type="button" class="icon-btn btn" data-move="-1" title="Move up" ${i === 0 ? "disabled" : ""}>▲</button>
          <button type="button" class="icon-btn btn" data-move="1" title="Move down" ${i === CL.state.chain.length - 1 ? "disabled" : ""}>▼</button>
          <button type="button" class="icon-btn btn" data-remove title="Remove">✕</button>
        </span>
      </div>
      <div class="step-notes" data-notes></div>
      <div class="step-body">
        <p class="step-summary">${escapeHtml(f.summary)}</p>
        ${f.params.map((p) => paramHtml(step, p)).join("")}
        <div class="step-foot">
          <details class="notes about"><summary>About this filter</summary>
            <dl class="explain"><dt>Use</dt><dd>${escapeHtml(f.use)}</dd><dt>Method</dt><dd>${escapeHtml(f.method)}</dd><dt>Caveats</dt><dd>${escapeHtml(f.caveats)}</dd></dl>
          </details>
          ${f.params.length ? `<button type="button" class="link" data-reset>Reset to defaults</button>` : ""}
        </div>
      </div>
    </div>`;
  }

  function render() {
    if (!CL.state.chain.length) {
      list.innerHTML = `<p class="placeholder">Add a filter to start. Nothing is changed in the source.</p>`;
      return;
    }
    list.innerHTML = CL.state.chain.map(stepHtml).join("");
    if (CL.lastPreview) showNotes(CL.lastPreview);
  }
  CL.renderChain = render;

  const stepOf = (el) => {
    const card = el.closest(".step-card");
    return card ? CL.state.chain.find((s) => s.uid === Number(card.dataset.uid)) : null;
  };
  const paramOf = (step, key) => CL.state.filters[step.id].params.find((p) => p.key === key);

  list.addEventListener("click", (e) => {
    const step = stepOf(e.target);
    if (!step) return;
    const idx = CL.state.chain.indexOf(step);
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.hasAttribute("data-toggle-open")) {
      step.open = !step.open;
      btn.closest(".step-card").classList.toggle("open", step.open);
    } else if (btn.dataset.move) {
      const to = idx + Number(btn.dataset.move);
      if (to < 0 || to >= CL.state.chain.length) return;
      CL.cancelPick && CL.cancelPick();
      CL.state.chain.splice(to, 0, CL.state.chain.splice(idx, 1)[0]);
      changed(true);
    } else if (btn.hasAttribute("data-remove")) {
      CL.cancelPick && CL.cancelPick();
      CL.state.chain.splice(idx, 1);
      changed(true);
    } else if (btn.hasAttribute("data-reset")) {
      step.params = defaults(CL.state.filters[step.id]);
      changed(true);
    } else if (btn.dataset.pick) {
      CL.startPick(step, paramOf(step, btn.dataset.pick));
    } else if (btn.dataset.clearPoints) {
      step.params[btn.dataset.clearPoints] = [];
      CL.refreshPoints(step);
      changed(false);
    }
  });

  list.addEventListener("input", (e) => {
    const step = stepOf(e.target);
    if (!step) return;
    const el = e.target;
    const card = el.closest(".step-card");
    if (el.hasAttribute("data-enable")) {
      step.enabled = el.checked;
      card.classList.toggle("disabled", !step.enabled);
      changed(false);
      return;
    }
    const key = el.dataset.slider || el.dataset.key;
    if (!key) return;
    const p = paramOf(step, key);
    let v;
    if (p.kind === "bool") v = el.checked;
    else if (p.kind === "choice") v = el.value;
    else if (el.dataset.slider) {
      v = isLog(p) ? p.min * Math.pow(p.max / p.min, Number(el.value) / 1000) : Number(el.value);
      const num = card.querySelector(`input[type=number][data-key="${key}"]`);
      if (num) num.value = formatValue(p, v);
    } else {
      v = Number(el.value);
      if (!isFinite(v) || el.value === "") return;
      v = Math.min(p.max, Math.max(p.min, v));
      const slider = card.querySelector(`[data-slider="${key}"]`);
      if (slider) slider.value = isLog(p) ? Math.round((1000 * Math.log(v / p.min)) / Math.log(p.max / p.min)) : v;
    }
    if (p.kind === "int") v = Math.round(v);
    else if (p.kind === "float") v = Number(Number(v).toFixed(6));
    step.params[key] = v;
    changed(false);
  });

  // ---------- points (picked in the viewer) ----------
  CL.refreshPoints = (step) => {
    const card = list.querySelector(`.step-card[data-uid="${step.uid}"]`);
    if (!card) return;
    CL.state.filters[step.id].params.filter((p) => p.kind === "points").forEach((p) => {
      const el = card.querySelector(`[data-points="${p.key}"] .points-value`);
      if (el) el.innerHTML = pointsText(p, step.params[p.key]);
    });
  };
  CL.pointsChanged = (step) => {
    CL.refreshPoints(step);
    changed(false);
  };

  // ---------- per-step results from the last preview ----------
  function showNotes(data) {
    const cards = $$(".step-card", list);
    (data.steps || []).forEach((r) => {
      const card = cards[r.position];
      if (!card) return;
      const el = card.querySelector("[data-notes]");
      const parts = [];
      if (r.error) parts.push(`<span class="err">✗ ${escapeHtml(r.error)}</span>`);
      else if (r.size) {
        if (r.notes && r.notes.length) parts.push(escapeHtml(r.notes.join(" · ")));
        parts.push(`<span class="muted">${r.size[0]}×${r.size[1]} · ${r.ms < 1 ? "<1" : Math.round(r.ms)} ms</span>`);
      }
      el.innerHTML = parts.join(" ");
      card.classList.toggle("failed", !!r.error);
    });
  }
  CL.on("preview", (data) => {
    if (!data.partial) showNotes(data);
  });
})();
