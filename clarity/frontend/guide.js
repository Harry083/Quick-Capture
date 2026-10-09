// The Filter guide tab: every filter with what it does, when to use it, how it works and its caveats.
(() => {
  const listEl = $("#guide-list");
  const search = $("#guide-search");
  const catsEl = $("#guide-cats");
  let category = "";

  function render() {
    const cat = CL.state.catalogue;
    if (!cat) return;
    const q = search.value.trim().toLowerCase();
    const shown = cat.filters.filter((f) => (!category || f.category === category) &&
      (!q || [f.name, f.summary, f.use, f.method, f.caveats, f.category_name].join(" ").toLowerCase().includes(q)));
    if (!shown.length) {
      listEl.innerHTML = `<p class="placeholder">No filter matches “${escapeHtml(q)}”.</p>`;
      return;
    }
    listEl.innerHTML = shown.map((f) => `
      <article class="guide-card">
        <div class="guide-head">
          <div><div class="cat-title">${escapeHtml(f.category_name)}${f.temporal ? " · multi-frame" : ""}</div><h3 class="guide-name">${escapeHtml(f.name)}</h3></div>
          <button type="button" class="btn small-btn" data-add="${f.id}" ${CL.state.source ? "" : "disabled"} title="${CL.state.source ? "Add to the chain" : "Open a source first"}">+ Add to chain</button>
        </div>
        <p class="guide-summary">${escapeHtml(f.summary)}</p>
        <dl class="explain"><dt>Use</dt><dd>${escapeHtml(f.use)}</dd><dt>Method</dt><dd>${escapeHtml(f.method)}</dd><dt>Caveats</dt><dd>${escapeHtml(f.caveats)}</dd></dl>
        ${f.params.length ? `<details class="notes"><summary>${f.params.length} parameter${f.params.length > 1 ? "s" : ""}</summary><ul class="param-list">${f.params
          .map((p) => `<li><strong>${escapeHtml(p.label)}</strong>${p.unit ? ` (${escapeHtml(p.unit)})` : ""}${p.help ? ` — ${escapeHtml(p.help)}` : ""}</li>`).join("")}</ul></details>` : ""}
      </article>`).join("");
  }

  CL.on("catalogue", (cat) => {
    catsEl.innerHTML = [["", `All (${cat.filters.length})`], ...cat.categories]
      .map(([id, name]) => `<button type="button" data-cat="${id}" aria-pressed="${id === category}">${escapeHtml(name)}</button>`)
      .join("");
    render();
  });
  CL.on("source", render);

  catsEl.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    category = b.dataset.cat;
    $$("button", catsEl).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    render();
  });
  search.addEventListener("input", render);

  listEl.addEventListener("click", (e) => {
    const b = e.target.closest("[data-add]");
    if (!b) return;
    CL.addFilter(b.dataset.add);
    setMode("enhance");
    $(".chain-panel").scrollIntoView({ block: "start", behavior: "smooth" });
  });

  $("#reference-view").addEventListener("click", () => api("view_reference").catch((e) => showError(e.message)));
  $("#reference-save").addEventListener("click", () => api("save_reference").catch((e) => showError(e.message)));
})();
