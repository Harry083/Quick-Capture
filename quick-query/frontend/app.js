const state = {
  path: "",
  summary: null,
  view: "current",
  tab: "tables",
  formats: {},
  objects: [],
  table: null,
  tableOffset: 0,
  tableLimit: 200,
  tableSort: { col: "", desc: false },
  tableSearch: "",
  tableData: null,
  queryData: null,
  lastSql: "",
  rec: { table: "", status: "deleted", source: "", search: "", offset: 0, limit: 500, counts: null },
  recData: null,
  pageMap: null,
  walLoaded: false,
  pendingTag: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

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
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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

const num = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString());

function showError(msg) {
  const el = $("#error-section");
  if (!msg) {
    el.classList.add("hidden");
    return;
  }
  el.textContent = msg;
  el.classList.remove("hidden");
}

function busy(btn, on, label) {
  if (!btn) return;
  if (on) {
    btn.dataset.label = btn.innerHTML;
    btn.disabled = true;
    if (label) btn.textContent = label;
  } else {
    btn.disabled = false;
    if (btn.dataset.label) btn.innerHTML = btn.dataset.label;
  }
}

// ---------- Opening a database ----------
const dbInput = $("#db-input");
const openBtn = $("#open-btn");

dbInput.addEventListener("input", () => (openBtn.disabled = !dbInput.value.trim()));
dbInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && dbInput.value.trim()) openCase(dbInput.value.trim());
});
openBtn.addEventListener("click", () => openCase(dbInput.value.trim()));

$("#browse-db").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    const data = await api("pick_database", dbInput.value.trim());
    if (data.path) {
      dbInput.value = data.path;
      openBtn.disabled = false;
      openCase(data.path);
    }
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
  }
});

window.openFromArgs = (path) => {
  dbInput.value = path;
  openBtn.disabled = false;
  openCase(path);
};

function caseInfo() {
  const info = {};
  $$("[data-case]").forEach((el) => (info[el.dataset.case] = el.value.trim()));
  return info;
}

$$("[data-case]").forEach((el) =>
  el.addEventListener("change", () => {
    if (state.summary) api("set_case_info", caseInfo()).catch((e) => showError(e.message));
  })
);

async function openCase(path) {
  if (!path) return;
  showError("");
  const status = $("#db-status");
  status.textContent = "Hashing, copying and verifying the evidence…";
  status.className = "file-status";
  busy(openBtn, true, "Opening…");
  try {
    const summary = await api("open_case", path, caseInfo());
    state.path = path;
    state.summary = summary;
    state.view = "current";
    state.table = null;
    state.tableData = null;
    state.queryData = null;
    state.recData = null;
    state.rec = { ...state.rec, table: "", offset: 0, counts: null };
    state.pageMap = null;
    state.walLoaded = false;
    timelineState.data = null;
    $("#global-results").innerHTML = "";
    $("#global-search-status").textContent = "";
    status.textContent = `Opened read-only · ${formatBytes(summary.file.size)} · working copies verified against the originals`;
    status.className = "file-status ok";
    renderEvidence(summary);
    renderOverview(summary);
    renderViewSelect(summary);
    $("#hero").classList.add("compact");
    $("#workspace").classList.remove("hidden");
    $("#overview").classList.remove("hidden");
    $("#table-grid").innerHTML = `<p class="placeholder">Choose a table.</p>`;
    $("#query-grid").innerHTML = "";
    $("#query-status").textContent = "";
    $("#page-detail").innerHTML = "";
    await loadObjects();
    loadSavedQueries();
    loadTags();
    if (state.tab !== "tables") switchTab(state.tab);
    // Recovery runs in the background so the tables are usable straight away.
    loadRecovered();
  } catch (e) {
    status.textContent = e.message;
    status.className = "file-status err";
  } finally {
    busy(openBtn, false);
  }
}

function renderEvidence(s) {
  const pill = $("#evidence-pill");
  pill.textContent = `${s.header.journal_mode.toUpperCase()} · SQLite ${s.header.sqlite_version || "?"}`;
  pill.classList.add("on");
  const list = $("#evidence-list");
  list.innerHTML = s.evidence
    .map(
      (e) => `<div class="evidence-row">
        <span class="badge ${e.role === "database" ? "" : "rem"}">${escapeHtml(e.role)}</span>
        <div class="ev-main">
          <div class="dev-name">${escapeHtml(e.name)} <span class="hint-inline">${formatBytes(e.size)} · modified ${escapeHtml(e.modified)}</span></div>
          <div class="dev-path" title="SHA-256 ${escapeHtml(e.hashes.sha256)}">MD5 ${escapeHtml(e.hashes.md5)} · SHA-1 ${escapeHtml(e.hashes.sha1)}</div>
        </div>
        <span class="ok-text verified" title="The working copy was re-hashed and matches the original">✓ verified</span>
      </div>`
    )
    .join("");
  list.classList.remove("hidden");
}

function renderOverview(s) {
  const cards = [
    ["Tables", num(s.tables), `${num(s.views)} views · ${num(s.indexes)} indexes`],
    ["Live rows", num(s.total_rows), "across all tables"],
    ["Pages", num(s.page_count), `${formatBytes(s.header.page_size)} each`],
    ["Free pages", num(s.freelist_pages), s.free_space.wiped ? "wiped (secure_delete)" : `${formatBytes(s.free_space.bytes)} free space`],
  ];
  if (s.wal) cards.push(["WAL frames", num(s.wal.frames || 0), s.wal.error ? s.wal.error : `${num(s.wal.commits)} commits · ${num(s.wal.old_frames)} older`]);
  if (s.journal) cards.push(["Journal pages", num(s.journal.pages || 0), s.journal.error || "pre-transaction copies"]);
  cards.push(["Recovered", `<span id="rec-card">…</span>`, `<span id="rec-card-sub">carving free space</span>`]);
  $("#overview-cards").innerHTML = cards
    .map(([name, val, sub]) => `<div class="score-card"><div class="metric-name">${name}</div><div class="metric-value small">${val}</div><div class="metric-sub">${sub}</div></div>`)
    .join("");

  const notes = [];
  if (s.free_space.wiped) notes.push(["weak", "Free space is zeroed", "The database looks like it uses secure_delete, so deleted rows have most likely been overwritten. WAL and journal history may still hold them."]);
  if (s.wal && !s.wal.error && s.wal.old_frames) notes.push(["notable", "Older WAL frames found", `${num(s.wal.old_frames)} frames are from before the last checkpoint. They're not part of the database any more, but their rows are in Recovered.`]);
  if (s.wal && !s.wal.error && s.wal.uncommitted_frames) notes.push(["notable", "Uncommitted WAL frames", `${num(s.wal.uncommitted_frames)} valid frames have no commit after them (a transaction that never finished).`]);
  if (s.journal && !s.journal.error && s.journal.pages) notes.push(["notable", "Rollback journal present", "Its pages are the database as it was before an unfinished or kept transaction. Pick “Before the journalled transaction” in View to read it."]);
  $("#notices").innerHTML = notes.length
    ? `<ul class="findings notices">${notes.map(([lvl, t, d]) => `<li data-level="${lvl}"><span class="level">${lvl === "weak" ? "note" : "info"}</span><strong>${escapeHtml(t)}</strong><span>${escapeHtml(d)}</span></li>`).join("")}</ul>`
    : "";
}

function renderViewSelect(s) {
  const sel = $("#view-select");
  sel.innerHTML = s.view_options.map((o) => `<option value="${escapeHtml(o.key)}">${escapeHtml(o.label)}</option>`).join("");
  sel.value = state.view;
  sel.disabled = s.view_options.length < 2;
}

$("#view-select").addEventListener("change", async (e) => {
  state.view = e.target.value;
  state.tableOffset = 0;
  state.pageMap = null;
  await loadObjects();
  if (state.tab === "pages") loadPageMap();
});

// ---------- Tabs ----------
$$("#main-tabs button").forEach((btn) => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

function switchTab(tab) {
  state.tab = tab;
  $$("#main-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  $$(".tab-pane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== tab));
  if (!state.summary) return;
  if (tab === "pages" && !state.pageMap) loadPageMap();
  if (tab === "wal" && !state.walLoaded) loadWal();
  if (tab === "query") loadBuilderTables();
  if (tab === "report") loadTags();
  if (tab === "search") setTimeout(() => $("#global-search").focus(), 0);
}

// ---------- Data grid ----------
const TS_FORMATS = {};

function cellHtml(c, colIdx) {
  if (!c) return "";
  switch (c.t) {
    case "null":
      return `<span class="null">NULL</span>`;
    case "int":
    case "real": {
      const raw = c.s ?? c.v;
      if (c.ts) return `<span class="ts num-cell" data-num="${escapeHtml(raw)}" title="${escapeHtml(raw)}">${escapeHtml(c.ts)}</span>`;
      return `<span class="num num-cell" data-num="${escapeHtml(raw)}">${escapeHtml(raw)}</span>`;
    }
    case "text": {
      const v = c.v;
      const short = v.length > 160 ? v.slice(0, 160) + "…" : v;
      const inspect = v.length >= 8 ? `<button type="button" class="inspect-btn" title="Open in the viewer (decode Base64, hex, URL encoding…)">⤢</button>` : "";
      return `<span class="txt" title="${escapeHtml(v.length > 160 ? v.slice(0, 2000) : "")}">${escapeHtml(short)}</span>${inspect}`;
    }
    case "blob":
      return `<button type="button" class="blob-chip" data-blob="${escapeHtml(c.id || "")}" title="${escapeHtml(c.preview)}">${escapeHtml(c.kind)} · ${formatBytes(c.len)}</button>`;
    default:
      return escapeHtml(JSON.stringify(c));
  }
}

/** Render a grid. opts: {columns, rows, formats, scope, sort, onSort, meta: [{label, get(i)}], tag(i) -> tag payload} */
function renderGrid(container, opts) {
  const { columns, rows, formats = {}, scope, sort, meta = [] } = opts;
  if (!rows.length) {
    container.innerHTML = `<p class="placeholder">${opts.empty || "No rows."}</p>`;
    return;
  }
  const head =
    `<th class="act"></th>` +
    meta.map((m) => `<th class="meta">${escapeHtml(m.label)}</th>`).join("") +
    columns
      .map((col) => {
        const fmt = formats[col];
        const arrow = sort && sort.col === col ? (sort.desc ? " ↓" : " ↑") : "";
        return `<th data-col="${escapeHtml(col)}" class="${opts.onSort ? "sortable" : ""}">
          <span class="th-name">${escapeHtml(col)}${arrow}</span>
          ${scope ? `<button type="button" class="fmt-btn ${fmt ? "on" : ""}" data-fmt-col="${escapeHtml(col)}" title="Timestamp format">${fmt ? "⏱ " + escapeHtml(fmt) : "⏱"}</button>` : ""}
        </th>`;
      })
      .join("");
  const body = rows
    .map(
      (r, i) =>
        `<tr data-i="${i}"><td class="act"><button type="button" class="tag-btn" data-tag="${i}" title="Bookmark this row">☆</button></td>` +
        meta.map((m) => `<td class="meta">${m.get(i)}</td>`).join("") +
        r.map((c, j) => `<td>${cellHtml(c, j)}</td>`).join("") +
        `</tr>`
    )
    .join("");
  container.innerHTML = `<table class="data-grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;

  if (opts.onSort) {
    $$("th.sortable", container).forEach((th) =>
      th.addEventListener("click", (e) => {
        if (e.target.closest(".fmt-btn")) return;
        opts.onSort(th.dataset.col);
      })
    );
  }
  $$(".fmt-btn", container).forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      formatMenu(b, scope, b.dataset.fmtCol, formats[b.dataset.fmtCol], opts.reload);
    })
  );
  $$(".blob-chip", container).forEach((b) => b.addEventListener("click", () => openBlob(b.dataset.blob)));
  $$(".inspect-btn", container).forEach((b) =>
    b.addEventListener("click", () => {
      const tr = b.closest("tr");
      const td = b.closest("td");
      const i = Number(tr.dataset.i);
      const j = Array.from(tr.children).indexOf(td) - 1 - meta.length;
      const c = rows[i] && rows[i][j];
      if (c && c.t === "text") inspectText(c.v);
    })
  );
  $$(".num-cell", container).forEach((el) => el.addEventListener("click", (e) => timestampPopover(e.currentTarget)));
  $$(".tag-btn", container).forEach((b) =>
    b.addEventListener("click", () => {
      const i = Number(b.dataset.tag);
      openTagDialog(opts.tag(i), b);
    })
  );
}

function pager(container, { offset, limit, total, onPage }) {
  if (!total) {
    container.innerHTML = "";
    return;
  }
  const end = Math.min(offset + limit, total);
  container.innerHTML = `<button class="btn small-btn" data-p="prev" ${offset <= 0 ? "disabled" : ""} type="button">‹</button>
    <span class="pager-label">${num(offset + 1)}–${num(end)} of ${num(total)}</span>
    <button class="btn small-btn" data-p="next" ${end >= total ? "disabled" : ""} type="button">›</button>`;
  $('[data-p="prev"]', container).addEventListener("click", () => onPage(Math.max(0, offset - limit)));
  $('[data-p="next"]', container).addEventListener("click", () => onPage(offset + limit));
}

// ---------- Popovers ----------
const popover = $("#popover");

function placePopover(anchor) {
  const r = anchor.getBoundingClientRect();
  popover.classList.remove("hidden");
  const w = popover.offsetWidth;
  popover.style.left = `${Math.max(8, Math.min(window.innerWidth - w - 8, r.left))}px`;
  popover.style.top = `${r.bottom + window.scrollY + 6}px`;
}

function closePopover() {
  popover.classList.add("hidden");
  popover.innerHTML = "";
}

document.addEventListener("click", (e) => {
  if (!popover.classList.contains("hidden") && !popover.contains(e.target) && !e.target.closest(".num-cell, .fmt-btn, #blob-export-btn")) closePopover();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closePopover();
    closeModal();
    $("#tag-modal").classList.add("hidden");
  }
});

async function timestampPopover(el) {
  popover.innerHTML = `<div class="pop-title">${escapeHtml(el.dataset.num)}</div><p class="placeholder">Reading…</p>`;
  placePopover(el);
  try {
    const data = await api("timestamp_readings", el.dataset.num);
    popover.innerHTML =
      `<div class="pop-title">Possible timestamps for ${escapeHtml(el.dataset.num)}</div>` +
      (data.readings.length
        ? `<table class="meta-table">${data.readings.map((r) => `<tr><td>${escapeHtml(r.label)}</td><td class="mono">${escapeHtml(r.value)}</td></tr>`).join("")}</table>`
        : `<p class="placeholder">No timestamp format gives a date between 1970 and 2100.</p>`);
    placePopover(el);
  } catch (e) {
    popover.innerHTML = `<p class="placeholder">${escapeHtml(e.message)}</p>`;
  }
}

function formatMenu(anchor, scope, col, current, reload) {
  const opts = [["auto", "Auto-detect"], ["none", "Not a timestamp"], ...Object.entries(TS_FORMATS)];
  popover.innerHTML =
    `<div class="pop-title">Show “${escapeHtml(col)}” as</div>` +
    `<div class="fmt-list">${opts
      .map(([k, label]) => `<button type="button" data-k="${k}" class="${k === current ? "on" : ""}">${escapeHtml(label)}</button>`)
      .join("")}</div>`;
  placePopover(anchor);
  $$(".fmt-list button", popover).forEach((b) =>
    b.addEventListener("click", async () => {
      closePopover();
      try {
        await api("set_format", scope, col, b.dataset.k);
        reload && reload();
      } catch (e) {
        showError(e.message);
      }
    })
  );
}

// ---------- Tables ----------
async function loadObjects() {
  try {
    const data = await api("objects", state.view);
    state.objects = data.objects;
    renderTableList();
    if (state.table && state.objects.some((o) => o.name === state.table)) loadRows();
    else if (state.table) {
      state.table = null;
      $("#table-grid").innerHTML = `<p class="placeholder">That table doesn't exist in this view.</p>`;
    }
  } catch (e) {
    showError(e.message);
  }
}

function renderTableList() {
  const filter = $("#table-filter").value.trim().toLowerCase();
  const items = state.objects.filter((o) => (o.type === "table" || o.type === "view") && (!filter || o.name.toLowerCase().includes(filter)));
  const list = $("#table-list");
  if (!items.length) {
    list.innerHTML = `<p class="placeholder">No tables.</p>`;
    return;
  }
  list.innerHTML = items
    .map(
      (o) => `<button type="button" class="list-row ${o.name === state.table ? "selected" : ""} ${o.rows === 0 ? "empty" : ""}" data-name="${escapeHtml(o.name)}">
        <span class="lr-name">${escapeHtml(o.name)}${o.type === "view" ? ` <span class="badge">view</span>` : ""}${o.virtual ? ` <span class="badge">virtual</span>` : ""}</span>
        <span class="lr-count">${o.rows === null || o.rows === undefined ? "" : num(o.rows)}</span>
      </button>`
    )
    .join("");
  $$(".list-row", list).forEach((b) => b.addEventListener("click", () => selectTable(b.dataset.name)));
}

$("#table-filter").addEventListener("input", renderTableList);

function selectTable(name) {
  state.table = name;
  state.tableOffset = 0;
  state.tableSort = { col: "", desc: false };
  state.tableSearch = "";
  $("#row-search").value = "";
  $("#schema-box").classList.add("hidden");
  renderTableList();
  loadRows();
}

let searchTimer = null;
$("#row-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.tableSearch = e.target.value.trim();
    state.tableOffset = 0;
    loadRows();
  }, 300);
});

async function loadRows() {
  if (!state.table) return;
  const grid = $("#table-grid");
  const obj = state.objects.find((o) => o.name === state.table);
  $("#table-title").textContent = state.table;
  $("#table-sub").textContent = obj && obj.columns ? `${obj.columns.length} columns` : "";
  $("#schema-btn").disabled = !obj;
  $("#table-csv").disabled = false;
  $("#blob-export-btn").disabled = false;
  $("#schema-box").textContent = obj ? obj.sql : "";
  grid.classList.add("loading");
  try {
    const d = await api("rows", state.view, state.table, state.tableOffset, state.tableLimit, state.tableSort.col, state.tableSort.desc, state.tableSearch);
    state.tableData = d;
    renderGrid(grid, {
      columns: d.columns,
      rows: d.rows,
      formats: d.formats,
      scope: state.table,
      sort: state.tableSort,
      empty: state.tableSearch ? "No rows match." : "This table is empty.",
      onSort: (col) => {
        state.tableSort = { col, desc: state.tableSort.col === col ? !state.tableSort.desc : false };
        loadRows();
      },
      reload: loadRows,
      tag: (i) => ({
        source: "table",
        table: state.table,
        view: state.view,
        rowid: d.rowid_index !== null ? d.rows[i][d.rowid_index].v : null,
        columns: d.columns,
        cells: d.rows[i],
      }),
    });
    pager($("#table-pager"), {
      offset: d.offset,
      limit: state.tableLimit,
      total: d.total,
      onPage: (o) => {
        state.tableOffset = o;
        loadRows();
      },
    });
  } catch (e) {
    grid.innerHTML = `<p class="placeholder">${escapeHtml(e.message)}</p>`;
  } finally {
    grid.classList.remove("loading");
  }
}

$("#schema-btn").addEventListener("click", () => $("#schema-box").classList.toggle("hidden"));

$("#blob-export-btn").addEventListener("click", async (e) => {
  const anchor = e.currentTarget;
  try {
    const d = await api("blob_columns", state.view, state.table);
    if (!d.columns.length) {
      popover.innerHTML = `<p class="placeholder">No BLOBs in ${escapeHtml(state.table)}.</p>`;
      placePopover(anchor);
      return;
    }
    popover.innerHTML = `<div class="pop-title">Export every BLOB in…</div><div class="fmt-list">${d.columns
      .map((c) => `<button type="button" data-col="${escapeHtml(c.name)}">${escapeHtml(c.name)} <span class="hint-inline">${num(c.count)} BLOBs</span></button>`)
      .join("")}</div><p class="inline-note">You'll be asked for a folder. Files are named table-rowid-column, with a manifest of hashes.</p>`;
    placePopover(anchor);
    $$(".fmt-list button", popover).forEach((b) =>
      b.addEventListener("click", async () => {
        closePopover();
        try {
          const r = await api("export_blobs", state.view, state.table, b.dataset.col);
          if (r.folder) flash(`Exported ${num(r.count)} BLOBs to ${r.folder}`);
        } catch (err) {
          showError(err.message);
        }
      })
    );
  } catch (err) {
    showError(err.message);
  }
});
$("#table-csv").addEventListener("click", () => exportCsv("table", { view: state.view, table: state.table }));

async function exportCsv(kind, params) {
  try {
    const d = await api("export_csv", kind, params);
    if (d.path) flash(`Saved ${d.path}`);
  } catch (e) {
    showError(e.message);
  }
}

function flash(msg) {
  let el = $("#flash");
  if (!el) {
    el = document.createElement("div");
    el.id = "flash";
    el.className = "flash";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 3500);
}

// ---------- Query ----------
const sqlInput = $("#sql-input");

sqlInput.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    runQuery();
  }
  if (e.key === "Tab") {
    e.preventDefault();
    const { selectionStart: s, selectionEnd: t } = sqlInput;
    sqlInput.setRangeText("  ", s, t, "end");
  }
});

$("#run-query").addEventListener("click", runQuery);
$("#cancel-query").addEventListener("click", () => api("cancel_query"));

async function runQuery() {
  const sql = sqlInput.value.trim();
  if (!sql || !state.summary) return;
  const status = $("#query-status");
  const runBtn = $("#run-query");
  busy(runBtn, true, "Running…");
  $("#cancel-query").classList.remove("hidden");
  status.textContent = "";
  status.className = "file-status";
  try {
    const d = await api("query", state.view, sql, 5000);
    state.queryData = d;
    state.lastSql = sql;
    status.textContent = `${num(d.count)} row${d.count === 1 ? "" : "s"}${d.truncated ? " (first 5,000 shown; Export CSV gets them all)" : ""} · ${d.seconds} s`;
    status.className = "file-status ok";
    renderQuery();
  } catch (e) {
    status.textContent = e.message;
    status.className = "file-status err";
  } finally {
    busy(runBtn, false);
    $("#cancel-query").classList.add("hidden");
  }
}

function renderQuery() {
  const d = state.queryData;
  if (!d) return;
  if (!d.columns.length) {
    $("#query-grid").innerHTML = `<p class="placeholder">The statement returned no columns.</p>`;
    return;
  }
  renderGrid($("#query-grid"), {
    columns: d.columns,
    rows: d.rows,
    formats: d.formats,
    scope: "query",
    empty: "No rows.",
    reload: runQuery,
    tag: (i) => ({ source: "query", view: state.view, columns: d.columns, cells: d.rows[i], detail: state.lastSql.slice(0, 400) }),
  });
}

$("#query-csv").addEventListener("click", () => {
  const sql = sqlInput.value.trim();
  if (sql) exportCsv("query", { view: state.view, sql });
});

$("#save-query").addEventListener("click", async () => {
  const sql = sqlInput.value.trim();
  if (!sql) return;
  const name = prompt("Name this query (it's re-run in the report):", "");
  if (name === null) return;
  try {
    await api("save_query", name, sql, state.view);
    loadSavedQueries();
  } catch (e) {
    showError(e.message);
  }
});

async function loadSavedQueries() {
  try {
    const d = await api("saved_queries");
    const box = $("#saved-queries");
    box.innerHTML = d.queries
      .map((q) => `<button type="button" data-name="${escapeHtml(q.name)}" title="${escapeHtml(q.sql)}">${escapeHtml(q.name)}<span class="x" data-del="${escapeHtml(q.name)}" title="Delete">×</span></button>`)
      .join("");
    $$("button", box).forEach((b) =>
      b.addEventListener("click", async (e) => {
        if (e.target.dataset.del) {
          await api("delete_query", e.target.dataset.del);
          loadSavedQueries();
          return;
        }
        const q = d.queries.find((x) => x.name === b.dataset.name);
        sqlInput.value = q.sql;
        runQuery();
      })
    );
  } catch (e) {
    /* no case open */
  }
}

// ---------- Query builder ----------
let joinsCache = null;

async function loadBuilderTables() {
  const sel = $("#qb-table");
  const tables = state.objects.filter((o) => o.type === "table" || o.type === "view");
  const prev = sel.value;
  sel.innerHTML = `<option value="">—</option>` + tables.map((t) => `<option>${escapeHtml(t.name)}</option>`).join("");
  if (tables.some((t) => t.name === prev)) sel.value = prev;
  try {
    joinsCache = (await api("suggest_joins", state.view)).joins;
  } catch (e) {
    joinsCache = [];
  }
  renderBuilder();
}

$("#qb-table").addEventListener("change", renderBuilder);

function builderTables() {
  const base = $("#qb-table").value;
  const joined = $$("#qb-joins input:checked").map((i) => JSON.parse(i.value));
  return { base, joined };
}

function renderBuilder() {
  const base = $("#qb-table").value;
  const joinsBox = $("#qb-joins");
  if (!base) {
    joinsBox.innerHTML = `<p class="placeholder">Choose a base table.</p>`;
    $("#qb-columns").innerHTML = "";
    return;
  }
  const checked = new Set($$("#qb-joins input:checked").map((i) => i.value));
  const related = (joinsCache || []).filter((j) => j.left === base || j.right === base);
  joinsBox.innerHTML = related.length
    ? related
        .map((j) => {
          const other = j.left === base ? { table: j.right, col: j.right_col, mine: j.left_col } : { table: j.left, col: j.left_col, mine: j.right_col };
          const val = JSON.stringify({ table: other.table, col: other.col, mine: other.mine });
          return `<label class="check"><input type="checkbox" value='${escapeHtml(val)}' ${checked.has(val) ? "checked" : ""} />
            <span><b>${escapeHtml(other.table)}</b> on ${escapeHtml(base)}.${escapeHtml(other.mine)} = ${escapeHtml(other.table)}.${escapeHtml(other.col)} <span class="hint-inline">${escapeHtml(j.reason)}</span></span></label>`;
        })
        .join("")
    : `<p class="placeholder">No relationships found for this table. You can still write the join yourself.</p>`;
  $$("input", joinsBox).forEach((i) => i.addEventListener("change", renderBuilderColumns));
  renderBuilderColumns();
}

function renderBuilderColumns() {
  const { base, joined } = builderTables();
  const box = $("#qb-columns");
  const prev = {};
  $$("#qb-columns .qb-col").forEach((row) => {
    prev[row.dataset.key] = { on: $("input", row).checked, fmt: $("select", row).value };
  });
  const tables = [base, ...joined.map((j) => j.table)];
  const fmtOptions = `<option value="">as stored</option>` + Object.entries(TS_FORMATS).map(([k, l]) => `<option value="${k}">${escapeHtml(l)}</option>`).join("");
  box.innerHTML = tables
    .map((t, ti) => {
      const obj = state.objects.find((o) => o.name === t);
      if (!obj || !obj.columns) return "";
      return `<div class="qb-table"><div class="qb-table-name">${escapeHtml(t)}${ti ? ` <span class="hint-inline">t${ti}</span>` : ` <span class="hint-inline">t0</span>`}</div>
        ${obj.columns
          .map((c) => {
            const key = `${ti}.${c.name}`;
            const p = prev[key] || { on: ti === 0, fmt: guessFmt(c) };
            return `<div class="qb-col" data-key="${escapeHtml(key)}" data-t="${ti}" data-col="${escapeHtml(c.name)}">
              <label class="check"><input type="checkbox" ${p.on ? "checked" : ""} /><span>${escapeHtml(c.name)} <span class="hint-inline">${escapeHtml(c.type)}</span></span></label>
              <select class="small-select">${fmtOptions}</select></div>`;
          })
          .join("")}</div>`;
    })
    .join("");
  $$(".qb-col", box).forEach((row) => {
    const p = prev[row.dataset.key];
    $("select", row).value = p ? p.fmt : guessFmt({ name: row.dataset.col }) || "";
  });
}

function guessFmt(col) {
  // the builder only pre-selects a conversion when the table grid has already detected one for the column
  const fmts = (state.tableData && state.tableData.formats) || {};
  return fmts[col.name] || "";
}

const qi = (s) => `"${String(s).replace(/"/g, '""')}"`;

$("#qb-generate").addEventListener("click", () => {
  const { base, joined } = builderTables();
  if (!base) return;
  const cols = [];
  $$("#qb-columns .qb-col").forEach((row) => {
    if (!$("input", row).checked) return;
    const ref = `t${row.dataset.t}.${qi(row.dataset.col)}`;
    const fmt = $("select", row).value;
    const label = joined.length ? `${row.dataset.t === "0" ? base : joined[Number(row.dataset.t) - 1].table}.${row.dataset.col}` : row.dataset.col;
    if (fmt && TS_SQL[fmt]) cols.push(`${TS_SQL[fmt].replace(/\{c\}/g, ref)} AS ${qi(label + " (UTC)")}`);
    else cols.push(joined.length ? `${ref} AS ${qi(label)}` : ref);
  });
  const lines = [`SELECT ${cols.length ? cols.join(",\n       ") : "*"}`, `FROM ${qi(base)} AS t0`];
  joined.forEach((j, i) => lines.push(`LEFT JOIN ${qi(j.table)} AS t${i + 1} ON t0.${qi(j.mine)} = t${i + 1}.${qi(j.col)}`));
  sqlInput.value = lines.join("\n") + ";";
  sqlInput.focus();
});

// SQL fragments matching backend/decoders.py TIMESTAMP_FORMATS (loaded from options()).
const TS_SQL = {};

// ---------- Recovered ----------
async function loadRecovered(extra = {}) {
  if (!state.summary) return;
  const grid = $("#rec-grid");
  if (extra.rerun) grid.innerHTML = `<p class="placeholder">Carving…</p>`;
  try {
    const r = state.rec;
    const d = await api("recovered", { table: r.table, status: r.status, source: r.source, search: r.search, offset: r.offset, limit: r.limit, ...extra });
    const tables = Object.keys(d.counts.table).sort();
    if ((!r.table || !d.counts.table[r.table]) && tables.length) {
      // records are shown one table at a time (each has its own columns): pick the first and reload
      state.rec.table = tables[0];
      const { rerun, ...rest } = extra;
      return loadRecovered(rest);
    }
    state.recData = d;
    state.rec.counts = d.counts;
    const deleted = d.counts.status["deleted"] || 0;
    const older = d.counts.status["older version"] || 0;
    $("#count-recovered").textContent = deleted + older ? `(${num(deleted + older)})` : "";
    const card = $("#rec-card");
    if (card) {
      card.textContent = num(deleted + older);
      $("#rec-card-sub").textContent = `${num(deleted)} deleted · ${num(older)} older versions`;
    }
    renderRecTables(d.counts);
    renderRecSources(d.counts);
    renderRecGrid();
  } catch (e) {
    grid.innerHTML = `<p class="placeholder">${escapeHtml(e.message)}</p>`;
    const card = $("#rec-card");
    if (card) {
      card.textContent = "!";
      $("#rec-card-sub").textContent = e.message;
    }
  }
}

function renderRecTables(counts) {
  const list = $("#rec-table-list");
  const tables = Object.entries(counts.table).sort((a, b) => a[0].localeCompare(b[0]));
  if (!tables.length) {
    list.innerHTML = `<p class="placeholder">Nothing recovered.</p>`;
    return;
  }
  list.innerHTML = tables
    .map(([t, n]) => `<button type="button" class="list-row ${t === state.rec.table ? "selected" : ""}" data-name="${escapeHtml(t)}"><span class="lr-name">${escapeHtml(t)}</span><span class="lr-count">${num(n)}</span></button>`)
    .join("");
  $$(".list-row", list).forEach((b) =>
    b.addEventListener("click", () => {
      state.rec.table = b.dataset.name;
      state.rec.offset = 0;
      loadRecovered();
    })
  );
}

function renderRecSources(counts) {
  const sel = $("#rec-source");
  const cur = state.rec.source;
  sel.innerHTML = `<option value="">All sources</option>` + Object.entries(counts.source).map(([s, n]) => `<option value="${escapeHtml(s)}">${escapeHtml(s)} (${num(n)})</option>`).join("");
  sel.value = cur;
}

const SOURCE_HELP = {
  freeblock: "Deleted cell in a page's freeblock chain",
  unallocated: "Unallocated space inside a page",
  freelist: "A page on the freelist",
  wal: "A WAL frame",
  journal: "A rollback journal page",
  "main file": "The main file, under a newer WAL version",
};

function renderRecGrid() {
  const d = state.recData;
  const grid = $("#rec-grid");
  if (!d) return;
  const recs = d.records;
  pager($("#rec-pager"), {
    offset: d.offset,
    limit: state.rec.limit,
    total: d.total,
    onPage: (o) => {
      state.rec.offset = o;
      loadRecovered();
    },
  });
  if (!recs.length) {
    grid.innerHTML = `<p class="placeholder">No records match. Try “All” or another table.</p>`;
    return;
  }
  const columns = recs[0].columns;
  renderGrid(grid, {
    columns,
    rows: recs.map((r) => r.cells),
    formats: recs[0].formats,
    scope: state.rec.table,
    reload: () => loadRecovered(),
    meta: [
      { label: "status", get: (i) => `<span class="status-pill s-${recs[i].status.replace(" ", "-")}">${escapeHtml(recs[i].status)}</span>` },
      {
        label: "source",
        get: (i) => {
          const r = recs[i];
          const where = r.source === "wal" ? `frame ${r.frame}` : r.source === "journal" ? `record ${r.frame}` : r.page ? `page ${r.page} @${r.offset}` : "";
          const pageLink = r.source === "wal" || r.source === "journal" ? `data-src="${r.source}" data-frame="${r.frame}"` : r.page ? `data-src="db-main" data-page="${r.page}"` : "";
          const also = r.also && r.also.length ? ` <span class="also" title="Also found in: ${escapeHtml(r.also.join(", "))}">+${r.also.length}</span>` : "";
          return `<span title="${escapeHtml(SOURCE_HELP[r.source] || "")}${r.note ? " — " + escapeHtml(r.note) : ""}">${escapeHtml(r.source)}</span>${where ? ` <button type="button" class="link page-link" ${pageLink}>${escapeHtml(where)}</button>` : ""}${also}`;
        },
      },
      { label: "rowid", get: (i) => (recs[i].rowid === null ? `<span class="null">lost</span>` : escapeHtml(recs[i].rowid)) },
      {
        label: "conf.",
        get: (i) => `<span class="conf c-${recs[i].confidence}" title="${recs[i].inferred.length ? "Guessed types for: " + escapeHtml(recs[i].inferred.join(", ")) : ""}">${escapeHtml(recs[i].confidence)}${recs[i].inferred.length ? "*" : ""}</span>`,
      },
    ],
    tag: (i) => {
      const r = recs[i];
      return {
        source: `recovered (${r.status}, ${r.source})`,
        table: r.table,
        rowid: r.rowid,
        columns: r.columns,
        cells: r.cells,
        detail: [r.page ? `page ${r.page} offset ${r.offset}` : "", r.frame !== null ? `frame ${r.frame}` : "", r.note].filter(Boolean).join("; "),
      };
    },
  });
  $$(".page-link", grid).forEach((b) =>
    b.addEventListener("click", () => {
      if (b.dataset.src === "wal" || b.dataset.src === "journal") openPage({ source: b.dataset.src, frame: Number(b.dataset.frame) });
      else openPage({ source: "db", view: "main", number: Number(b.dataset.page) });
    })
  );
}

$$("#rec-status button").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#rec-status button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    state.rec.status = b.dataset.status;
    state.rec.offset = 0;
    loadRecovered();
  })
);
$("#rec-source").addEventListener("change", (e) => {
  state.rec.source = e.target.value;
  state.rec.offset = 0;
  loadRecovered();
});
let recTimer = null;
$("#rec-search").addEventListener("input", (e) => {
  clearTimeout(recTimer);
  recTimer = setTimeout(() => {
    state.rec.search = e.target.value.trim();
    state.rec.offset = 0;
    loadRecovered();
  }, 300);
});
$("#rec-rerun").addEventListener("click", async (e) => {
  busy(e.currentTarget, true, "Carving…");
  await loadRecovered({ rerun: true });
  busy(e.currentTarget, false);
});
$("#rec-csv").addEventListener("click", () => exportCsv("recovered", { table: state.rec.table, status: state.rec.status, source: state.rec.source }));

// ---------- Search all ----------
$("#global-search-btn").addEventListener("click", globalSearch);
$("#global-search").addEventListener("keydown", (e) => e.key === "Enter" && globalSearch());

async function globalSearch() {
  const term = $("#global-search").value.trim();
  if (!term || !state.summary) return;
  const status = $("#global-search-status");
  const box = $("#global-results");
  status.textContent = "Searching every table…";
  status.className = "file-status";
  box.innerHTML = "";
  try {
    const d = await api("search_all", state.view, term);
    status.textContent = `${num(d.total)} matching row${d.total === 1 ? "" : "s"} in ${d.tables.length} table${d.tables.length === 1 ? "" : "s"}` +
      (d.recovered.length ? ` · ${num(d.recovered.length)} recovered record${d.recovered.length === 1 ? "" : "s"}` : "");
    status.className = d.total || d.recovered.length ? "file-status ok" : "file-status";
    box.innerHTML =
      d.tables
        .map((t, k) => `<div class="search-group"><div class="search-head"><b>${escapeHtml(t.table)}</b> <span class="hint-inline">${num(t.total)} match${t.total === 1 ? "" : "es"}${t.total > t.rows.length ? `, first ${t.rows.length} shown` : ""}</span>
          <button type="button" class="link open-table" data-table="${escapeHtml(t.table)}">open table filtered ↗</button></div><div class="grid-box" id="sr-${k}"></div></div>`)
        .join("") +
      (d.recovered.length ? `<div class="search-group"><div class="search-head"><b>Recovered records</b> <span class="hint-inline">deleted rows and older versions</span></div><div class="grid-box" id="sr-rec"></div></div>` : "");
    d.tables.forEach((t, k) => {
      const el = $(`#sr-${k}`);
      renderGrid(el, {
        columns: t.columns,
        rows: t.rows,
        formats: t.formats,
        tag: (i) => ({ source: "search", table: t.table, view: state.view, rowid: t.rowid_index !== null ? t.rows[i][t.rowid_index].v : null, columns: t.columns, cells: t.rows[i], detail: `search: ${term}` }),
      });
      // highlight the columns that matched
      $$("tbody tr", el).forEach((tr, i) => {
        const hit = new Set(t.hits[i] || []);
        Array.from(tr.children).slice(1).forEach((td, j) => hit.has(t.columns[j]) && td.classList.add("hit"));
      });
    });
    if (d.recovered.length) {
      const recs = d.recovered;
      $("#sr-rec").innerHTML = `<table class="data-grid"><thead><tr><th>status</th><th>table</th><th>where</th><th>rowid</th><th>values</th></tr></thead><tbody>${recs
        .map((r) => `<tr><td><span class="status-pill s-${r.status.replace(" ", "-")}">${escapeHtml(r.status)}</span></td><td>${escapeHtml(r.table)}</td><td>${escapeHtml(r.source)} ${escapeHtml(r.location)}</td><td>${r.rowid ?? ""}</td>
          <td class="cell-vals">${r.cells.map((c, i) => `<span class="hint-inline">${escapeHtml(r.columns[i])}</span> ${cellHtml(c)}`).join(" <span class='sep'>|</span> ")}</td></tr>`)
        .join("")}</tbody></table>`;
      wirePage($("#sr-rec"));
    }
    $$(".open-table", box).forEach((b) =>
      b.addEventListener("click", () => {
        switchTab("tables");
        selectTable(b.dataset.table);
        $("#row-search").value = term;
        state.tableSearch = term;
        loadRows();
      })
    );
    if (!d.tables.length && !d.recovered.length) box.innerHTML = `<p class="placeholder">Nothing found for “${escapeHtml(term)}”.</p>`;
  } catch (e) {
    status.textContent = e.message;
    status.className = "file-status err";
  }
}

$("#rec-as-db").addEventListener("click", () => {
  setView("recovered");
  switchTab("query");
  const t = state.rec.table || "message";
  sqlInput.value = `SELECT * FROM "${t.replace(/"/g, '""')}"\nWHERE qq_status = 'deleted'\nORDER BY qq_orig_rowid;`;
});

// ---------- WAL & journal ----------
async function loadWal() {
  const body = $("#wal-body");
  const s = state.summary;
  state.walLoaded = true;
  if (!s.wal && !s.journal) {
    body.innerHTML = `<p class="placeholder">No -wal or -journal file sits next to this database.</p>`;
    return;
  }
  let html = "";
  if (s.journal) {
    const j = s.journal;
    html += `<h3>Rollback journal</h3>`;
    if (j.error) html += `<p class="placeholder">${escapeHtml(j.error)}</p>`;
    else
      html += `<p class="guide">${num(j.pages)} page records${j.zeroed_header ? " (header zeroed by PERSIST mode; read anyway)" : ""}. Each is a page as it was
        <em>before</em> the journalled transaction. Pages: ${j.page_numbers.map((p) => `<button type="button" class="link jpage" data-page="${p}">${p}</button>`).join(", ")}.
        <button type="button" class="btn small-btn" id="view-journal">View database before this transaction</button></p>`;
  }
  if (s.wal) {
    html += `<h3>Write-ahead log</h3>`;
    if (s.wal.error) {
      html += `<p class="placeholder">${escapeHtml(s.wal.error)}</p>`;
    } else {
      body.innerHTML = html + `<p class="placeholder">Reading frames…</p>`;
      try {
        const d = await api("wal_frames");
        html += `<div class="score-cards wal-cards">
          <div class="score-card"><div class="metric-name">Frames</div><div class="metric-value small">${num(s.wal.frames)}</div><div class="metric-sub">${num(s.wal.valid_frames)} valid</div></div>
          <div class="score-card"><div class="metric-name">Commits</div><div class="metric-value small">${num(s.wal.commits)}</div><div class="metric-sub">checkpoint seq ${s.wal.checkpoint_seq}</div></div>
          <div class="score-card"><div class="metric-name">Older frames</div><div class="metric-value small">${num(s.wal.old_frames)}</div><div class="metric-sub">from earlier checkpoints</div></div>
          <div class="score-card"><div class="metric-name">Salts</div><div class="metric-value small mono-val">${s.wal.salt1.toString(16)}</div><div class="metric-sub mono">${s.wal.salt2.toString(16)}</div></div>
        </div>`;
        if (d.commits.length) {
          html += `<h3>Transactions</h3><div class="commit-list">${d.commits
            .map(
              (c) => `<div class="commit-row"><span class="commit-no">#${c.commit}</span>
              <span>frames ${c.first_frame}–${c.last_frame} · ${c.frames} page write${c.frames === 1 ? "" : "s"} · db ${num(c.db_pages)} pages</span>
              <span class="hint-inline">pages ${c.pages.slice(0, 20).join(", ")}${c.pages.length > 20 ? "…" : ""}</span>
              <button type="button" class="btn small-btn view-commit" data-k="commit:${c.commit}">View as of this commit</button></div>`
            )
            .join("")}</div>`;
        }
        html += `<h3>Timeline <span class="hint-inline">what each transaction did, row by row</span></h3><div id="timeline-box"><p class="placeholder">Replaying the WAL…</p></div>`;
        html += `<h3>Frames</h3><div class="grid-box frames-box"><table class="data-grid"><thead><tr><th>#</th><th>page</th><th>type</th><th>table</th><th>commit</th><th>state</th><th>cells</th><th></th></tr></thead><tbody>${d.frames
          .map(
            (f) => `<tr class="${f.state === "valid" ? "" : "dim"}"><td>${f.index}</td><td>${f.page}</td><td>${escapeHtml(f.type)}</td><td>${escapeHtml(f.owner)}</td>
            <td>${f.commit ? `#${f.commit}` : f.commit_size ? "commit" : ""}</td><td><span class="status-pill s-${f.state.replace(/ /g, "-")}">${escapeHtml(f.state)}</span></td><td>${f.cells ?? ""}</td>
            <td><button type="button" class="link frame-link" data-frame="${f.index}">hex</button></td></tr>`
          )
          .join("")}</tbody></table></div>`;
      } catch (e) {
        html += `<p class="placeholder">${escapeHtml(e.message)}</p>`;
      }
    }
  }
  body.innerHTML = html;
  if ($("#timeline-box", body)) loadTimeline();
  $$(".view-commit", body).forEach((b) => b.addEventListener("click", () => setView(b.dataset.k)));
  $$(".frame-link", body).forEach((b) => b.addEventListener("click", () => openPage({ source: "wal", frame: Number(b.dataset.frame) })));
  $$(".jpage", body).forEach((b) => b.addEventListener("click", () => openPage({ source: "db", view: "journal", number: Number(b.dataset.page) })));
  const vj = $("#view-journal", body);
  if (vj) vj.addEventListener("click", () => setView("journal"));
}

const timelineState = { data: null, op: "", table: "" };

async function loadTimeline() {
  const box = $("#timeline-box");
  try {
    timelineState.data = await api("timeline");
    renderTimeline();
  } catch (e) {
    box.innerHTML = `<p class="placeholder">${escapeHtml(e.message)}</p>`;
  }
}

function changeSummary(e) {
  const pick = (cells, cols) => cells.map((c, i) => [cols[i], c]);
  if (e.op === "update") {
    return e.changed
      .map((col) => {
        const i = e.columns.indexOf(col);
        return `<span class="hint-inline">${escapeHtml(col)}</span> <span class="before">${cellHtml(e.before[i])}</span> → <span class="after">${cellHtml(e.after[i])}</span>`;
      })
      .join("<br>");
  }
  const cells = e.after || e.before;
  return pick(cells, e.columns)
    .filter(([, c]) => c.t !== "null")
    .slice(0, 5)
    .map(([col, c]) => `<span class="hint-inline">${escapeHtml(col)}</span> ${cellHtml(c)}`)
    .join(" <span class='sep'>|</span> ");
}

function renderTimeline() {
  const box = $("#timeline-box");
  const d = timelineState.data;
  if (!d || !d.events.length) {
    box.innerHTML = `<p class="placeholder">No row changes found in the WAL's transactions.</p>`;
    return;
  }
  const tables = [...new Set(d.events.map((e) => e.table))].sort();
  const evs = d.events.filter((e) => (!timelineState.op || e.op === timelineState.op) && (!timelineState.table || e.table === timelineState.table));
  const totals = { insert: 0, update: 0, delete: 0 };
  d.commits.forEach((c) => ["insert", "update", "delete"].forEach((k) => (totals[k] += c[k])));
  box.innerHTML = `<div class="grid-toolbar">
      <div class="tabs compact" id="tl-ops">${[["", "All"], ["insert", `Inserts ${num(totals.insert)}`], ["update", `Updates ${num(totals.update)}`], ["delete", `Deletes ${num(totals.delete)}`]]
        .map(([k, l]) => `<button type="button" data-op="${k}" aria-selected="${timelineState.op === k}">${l}</button>`)
        .join("")}</div>
      <select id="tl-table" class="small-select"><option value="">All tables</option>${tables.map((t) => `<option ${t === timelineState.table ? "selected" : ""}>${escapeHtml(t)}</option>`).join("")}</select>
      ${d.truncated ? `<span class="hint-inline">first ${num(d.events.length)} changes shown</span>` : ""}
    </div>
    <div class="grid-box timeline-grid"><table class="data-grid"><thead><tr><th class="act"></th><th>commit</th><th>frames</th><th>change</th><th>table</th><th>rowid</th><th>row time</th><th>what changed</th></tr></thead><tbody>${evs
      .map((e, i) => `<tr data-i="${i}"><td class="act"><button type="button" class="tag-btn" data-ev="${i}" title="Bookmark this change">☆</button></td>
        <td><button type="button" class="link view-commit-tl" data-k="commit:${e.commit}" title="View the database as of this commit">#${e.commit}</button></td>
        <td class="mono-cell">${e.first_frame}–${e.last_frame}</td>
        <td><span class="op op-${e.op}">${e.op}</span></td><td>${escapeHtml(e.table)}</td><td>${e.rowid ?? ""}</td>
        <td>${e.ts ? `<span class="ts" title="${escapeHtml(e.ts_column)}">${escapeHtml(e.ts)}</span>` : ""}</td>
        <td class="cell-vals">${changeSummary(e)}</td></tr>`)
      .join("")}</tbody></table></div>`;
  $$("#tl-ops button", box).forEach((b) => b.addEventListener("click", () => ((timelineState.op = b.dataset.op), renderTimeline())));
  $("#tl-table", box).addEventListener("change", (e) => ((timelineState.table = e.target.value), renderTimeline()));
  $$(".view-commit-tl", box).forEach((b) => b.addEventListener("click", () => setView(b.dataset.k)));
  wirePage(box);
  $$(".tag-btn", box).forEach((b) =>
    b.addEventListener("click", () => {
      const e = evs[Number(b.dataset.ev)];
      const cells = e.after || e.before;
      openTagDialog({ source: `WAL ${e.op} (commit ${e.commit})`, table: e.table, rowid: e.rowid, columns: e.columns, cells,
        detail: e.op === "update" ? `changed: ${e.changed.join(", ")}; before: ${e.changed.map((c) => `${c}=${cellText(e.before[e.columns.indexOf(c)])}`).join(", ")}` : `frames ${e.first_frame}–${e.last_frame}` }, b);
    })
  );
}

function cellText(c) {
  if (!c || c.t === "null") return "NULL";
  if (c.t === "blob") return `[BLOB ${c.kind}]`;
  return String(c.s ?? c.v);
}

function setView(key) {
  const sel = $("#view-select");
  sel.value = key;
  sel.dispatchEvent(new Event("change"));
  switchTab("tables");
  window.scrollTo({ top: $("#workspace").offsetTop - 80, behavior: "smooth" });
}

// ---------- Pages ----------
const PAGE_TYPES = {
  table_leaf: "Table leaf",
  table_interior: "Table interior",
  index_leaf: "Index leaf",
  index_interior: "Index interior",
  overflow: "Overflow",
  freelist_trunk: "Freelist trunk",
  freelist_leaf: "Freelist leaf",
  ptrmap: "Pointer map",
  lock_byte: "Lock byte",
  unknown: "Unreferenced",
};

async function loadPageMap() {
  const box = $("#page-map");
  box.innerHTML = `<p class="placeholder">Mapping pages…</p>`;
  try {
    const d = await api("page_map", state.view);
    state.pageMap = d.pages;
    const counts = {};
    d.pages.forEach((p) => (counts[p.type] = (counts[p.type] || 0) + 1));
    $("#page-legend").innerHTML =
      Object.entries(PAGE_TYPES)
        .filter(([k]) => counts[k])
        .map(([k, label]) => `<span class="legend-item"><span class="pg pg-${k}"></span>${label} <span class="hint-inline">${num(counts[k])}</span></span>`)
        .join("") + `<span class="legend-item"><span class="pg pg-table_leaf wal-mark"></span>changed in WAL</span>`;
    box.innerHTML = d.pages
      .map((p) => `<button type="button" class="pg pg-${p.type} ${p.in_wal ? "wal-mark" : ""} ${p.free_bytes > 0 && p.type === "table_leaf" ? "has-free" : ""}" data-page="${p.page}" title="Page ${p.page} · ${PAGE_TYPES[p.type] || p.type}${p.owner ? " · " + escapeHtml(p.owner) : ""}${p.free_bytes ? ` · ${p.free_bytes} B free` : ""}"></button>`)
      .join("");
    $$(".pg", box).forEach((b) => b.addEventListener("click", () => showPage(Number(b.dataset.page))));
    $("#page-number").max = d.pages.length;
  } catch (e) {
    box.innerHTML = `<p class="placeholder">${escapeHtml(e.message)}</p>`;
  }
}

$("#page-go").addEventListener("click", () => showPage(Number($("#page-number").value)));
$("#page-number").addEventListener("keydown", (e) => e.key === "Enter" && showPage(Number(e.target.value)));

async function showPage(n) {
  if (!n) return;
  $$("#page-map .pg").forEach((b) => b.classList.toggle("sel", Number(b.dataset.page) === n));
  const box = $("#page-detail");
  box.innerHTML = `<p class="placeholder">Reading page ${n}…</p>`;
  try {
    const d = await api("page", state.view, n, "db", null);
    const info = state.pageMap ? state.pageMap.find((p) => p.page === n) : null;
    box.innerHTML = pageHtml(d, info);
    wirePage(box, d);
  } catch (e) {
    box.innerHTML = `<p class="placeholder">${escapeHtml(e.message)}</p>`;
  }
}

async function openPage({ source, frame = null, view = state.view, number = 0 }) {
  openModal(source === "wal" ? `WAL frame ${frame}` : source === "journal" ? `Journal record ${frame}` : `Page ${number}`, `<p class="placeholder">Reading…</p>`);
  try {
    const d = await api("page", view, number, source === "db" ? "db" : source, frame);
    $("#modal-title").textContent = source === "wal" ? `WAL frame ${frame} · page ${d.page}` : source === "journal" ? `Journal record ${frame} · page ${d.page}` : `Page ${d.page}`;
    $("#modal-body").innerHTML = pageHtml(d, null);
    wirePage($("#modal-body"), d);
  } catch (e) {
    $("#modal-body").innerHTML = `<p class="placeholder">${escapeHtml(e.message)}</p>`;
  }
}

function pageHtml(d, info) {
  const facts = [
    ["Type", d.type],
    info && info.owner ? ["Belongs to", info.owner] : null,
    d.cell_count !== undefined ? ["Cells", d.cell_count] : null,
    d.first_freeblock !== undefined ? ["First freeblock", d.first_freeblock || "none"] : null,
    d.content_start !== undefined ? ["Cell content starts", d.content_start] : null,
    d.fragmented !== undefined ? ["Fragmented bytes", d.fragmented] : null,
    d.right_child ? ["Right child", d.right_child] : null,
    ["File offset", `0x${d.base.toString(16)}`],
  ].filter(Boolean);
  const cells = (d.cells || [])
    .slice(0, 400)
    .map(
      (c) => `<tr><td>${c.offset}</td><td>${c.size}</td><td>${c.rowid ?? ""}</td><td>${c.left_child ?? ""}</td><td>${c.overflow || ""}</td>
      <td class="cell-vals">${c.error ? `<span class="bad-text">${escapeHtml(c.error)}</span>` : c.values.map((v) => cellHtml(v)).join(" <span class='sep'>|</span> ")}</td></tr>`
    )
    .join("");
  return `<div class="page-facts"><table class="meta-table">${facts.map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(v)}</td></tr>`).join("")}</table>
    <div class="region-legend"><span class="rg rg-header">header</span><span class="rg rg-pointers">cell pointers</span><span class="rg rg-cell">cell</span><span class="rg rg-freeblock">freeblock</span><span class="rg rg-unallocated">unallocated</span></div></div>
    ${cells ? `<details class="raw-details" open><summary>Cells</summary><div class="grid-box cells-box"><table class="data-grid"><thead><tr><th>offset</th><th>size</th><th>rowid</th><th>child</th><th>overflow</th><th>values</th></tr></thead><tbody>${cells}</tbody></table></div></details>` : ""}
    <details class="raw-details" open><summary>Hex</summary><div class="hex-view">${hexHtml(d)}</div></details>`;
}

function hexHtml(d) {
  const bytes = d.raw.match(/../g) || [];
  const kind = new Array(bytes.length).fill("");
  for (const r of d.regions) for (let i = r.start; i < Math.min(r.end, bytes.length); i++) kind[i] = r.kind;
  const rows = [];
  for (let off = 0; off < bytes.length; off += 16) {
    let hex = "";
    let asc = "";
    for (let i = off; i < Math.min(off + 16, bytes.length); i++) {
      const b = parseInt(bytes[i], 16);
      const cls = kind[i] ? ` class="rg-${kind[i]}"` : "";
      hex += `<span${cls}>${bytes[i]}</span>`;
      const ch = b >= 32 && b < 127 ? escapeHtml(String.fromCharCode(b)) : ".";
      asc += `<span${cls}>${ch}</span>`;
    }
    rows.push(`<div class="hx"><span class="hx-off">${(d.base + off).toString(16).padStart(8, "0")}</span><span class="hx-hex">${hex}</span><span class="hx-asc">${asc}</span></div>`);
  }
  return rows.join("");
}

function wirePage(root) {
  $$(".blob-chip", root).forEach((b) => b.addEventListener("click", () => openBlob(b.dataset.blob)));
  $$(".num-cell", root).forEach((el) => el.addEventListener("click", (e) => timestampPopover(e.currentTarget)));
}

// ---------- Modal / BLOB viewer ----------
const modal = $("#modal");

function openModal(title, html, tools = "") {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal-tools").innerHTML = tools;
  modal.classList.remove("hidden");
}

function closeModal() {
  modal.classList.add("hidden");
}

$("#modal-close").addEventListener("click", closeModal);
modal.addEventListener("click", (e) => e.target === modal && closeModal());

function jsonTree(v, depth = 0) {
  if (v === null || v === undefined) return `<span class="null">null</span>`;
  if (Array.isArray(v)) {
    if (!v.length) return "[]";
    return `<details ${depth < 2 ? "open" : ""}><summary>[${v.length}]</summary><ol start="0">${v.map((x) => `<li>${jsonTree(x, depth + 1)}</li>`).join("")}</ol></details>`;
  }
  if (typeof v === "object") {
    const keys = Object.keys(v);
    if (!keys.length) return "{}";
    if ("$bytes" in v) return `<span class="bytes" title="${escapeHtml(v.hex)}">‹${num(v.$bytes)} bytes› ${escapeHtml(v.hex.slice(0, 48))}</span>`;
    return `<details ${depth < 2 ? "open" : ""}><summary>{${keys.length}}</summary><ul>${keys.map((k) => `<li><span class="k">${escapeHtml(k)}</span>: ${jsonTree(v[k], depth + 1)}</li>`).join("")}</ul></details>`;
  }
  if (typeof v === "string") return `<span class="s">"${escapeHtml(v)}"</span>`;
  return `<span class="n">${escapeHtml(v)}</span>`;
}

function blobBody(d) {
  let preview = "";
  if (d.data_uri) preview = `<div class="blob-image"><img src="${d.data_uri}" alt="BLOB image" /></div>`;
  if (d.decoded !== undefined) preview += `<h3>${escapeHtml(d.decoded_label || "Decoded")}</h3><div class="json-tree">${jsonTree(d.decoded)}</div>`;
  if (d.text !== undefined) preview += `<h3>Text</h3><pre class="report-text blob-text">${escapeHtml(d.text)}</pre>`;
  if (d.inner) preview += `<h3>${escapeHtml(d.decoded_label)}</h3><div class="inner-blob">${blobBody(d.inner)}</div>`;
  if (d.decode_error) preview += `<p class="bad-text">${escapeHtml(d.decode_error)}</p>`;
  return `<div class="blob-facts"><span class="badge">${escapeHtml(d.kind)}</span> <span class="mono">${num(d.size)} bytes</span> <span class="hint-inline">${escapeHtml(d.mime)}</span></div>
    ${preview}
    <details class="raw-details" ${preview ? "" : "open"}><summary>Hex${d.truncated_hex ? " (first 4 KiB)" : ""}</summary><pre class="report-text hexdump">${escapeHtml(d.hex)}</pre></details>`;
}

// The viewer decodes the original bytes through a chain of transforms; each step can be undone.
const viewer = { root: null, chain: [], current: null, title: "BLOB" };
const TRANSFORMS = {};

async function openBlob(id, title = "BLOB") {
  if (!id) return;
  viewer.root = id;
  viewer.chain = [];
  viewer.title = title;
  openModal(title, `<p class="placeholder">Decoding…</p>`, `<button class="btn small-btn" id="blob-save" type="button">Save…</button>`);
  $("#blob-save").addEventListener("click", async () => {
    try {
      const r = await api("save_blob", viewer.current || viewer.root);
      if (r.path) flash(`Saved ${r.path}`);
    } catch (e) {
      showError(e.message);
    }
  });
  renderViewer();
}

async function inspectText(text) {
  try {
    const d = await api("put_text", text);
    openBlob(d.id, "Text value");
  } catch (e) {
    showError(e.message);
  }
}

async function renderViewer() {
  const body = $("#modal-body");
  try {
    const d = await api("decode", viewer.root, viewer.chain);
    viewer.current = d.id;
    $("#modal-title").textContent = `${viewer.title} · ${d.kind}`;
    const steps = [`<button type="button" class="crumb" data-step="0">original</button>`]
      .concat(viewer.chain.map((t, i) => `<span class="crumb-sep">→</span><button type="button" class="crumb" data-step="${i + 1}">${escapeHtml(TRANSFORMS[t] || t)}</button>`))
      .join("");
    const suggested = new Set(d.suggest || []);
    const chips = Object.entries(TRANSFORMS)
      .map(([k, label]) => `<button type="button" data-t="${k}" class="${suggested.has(k) ? "suggested" : ""}" title="${suggested.has(k) ? "Looks applicable" : ""}">${escapeHtml(label)}</button>`)
      .join("");
    const hashes = d.hashes ? Object.entries(d.hashes).map(([k, v]) => `<tr><td>${k.toUpperCase()}</td><td class="mono">${v}</td></tr>`).join("") : "";
    body.innerHTML = `<div class="decode-bar">
        <div class="crumbs">${steps}</div>
        <div class="chips decode-chips"><span class="hint-inline">Decode as:</span>${chips}</div>
      </div>
      <div class="blob-stats"><span>entropy <b>${d.entropy ?? "–"}</b> bits/byte${d.entropy > 7.5 ? " · looks compressed or encrypted" : ""}</span>
        <span>${num(d.strings)} strings</span>
        <details class="inline-details"><summary>hashes</summary><table class="meta-table">${hashes}</table></details></div>
      ${blobBody(d)}`;
    $$(".decode-chips button", body).forEach((b) =>
      b.addEventListener("click", () => {
        viewer.chain.push(b.dataset.t);
        renderViewer();
      })
    );
    $$(".crumb", body).forEach((b) =>
      b.addEventListener("click", () => {
        viewer.chain = viewer.chain.slice(0, Number(b.dataset.step));
        renderViewer();
      })
    );
  } catch (e) {
    const failed = viewer.chain.pop();
    body.insertAdjacentHTML("afterbegin", `<p class="bad-text decode-err">${escapeHtml(e.message)}</p>`);
    if (failed === undefined) body.innerHTML = `<p class="placeholder">${escapeHtml(e.message)}</p>`;
  }
}

// ---------- Bookmarks ----------
const tagModal = $("#tag-modal");

function openTagDialog(payload, btn) {
  state.pendingTag = { payload, btn };
  const where = [payload.source, payload.table, payload.rowid !== null && payload.rowid !== undefined ? `rowid ${payload.rowid}` : ""].filter(Boolean).join(" · ");
  $("#tag-what").textContent = where;
  $("#tag-note").value = "";
  tagModal.classList.remove("hidden");
  $("#tag-label").select();
}

$("#tag-close").addEventListener("click", () => tagModal.classList.add("hidden"));
$("#tag-save").addEventListener("click", saveTag);
$("#tag-note").addEventListener("keydown", (e) => (e.ctrlKey || e.metaKey) && e.key === "Enter" && saveTag());

async function saveTag() {
  const p = state.pendingTag;
  if (!p) return;
  try {
    await api("add_tag", { ...p.payload, label: $("#tag-label").value.trim(), note: $("#tag-note").value.trim() });
    tagModal.classList.add("hidden");
    if (p.btn) {
      p.btn.textContent = "★";
      p.btn.classList.add("tagged");
    }
    loadTags();
  } catch (e) {
    showError(e.message);
  }
}

async function loadTags() {
  try {
    const d = await api("tags");
    $("#count-tags").textContent = d.tags.length ? `(${d.tags.length})` : "";
    const box = $("#tag-list");
    if (!d.tags.length) {
      box.innerHTML = `<p class="placeholder">No bookmarks yet. Use ☆ on any row.</p>`;
      return;
    }
    box.innerHTML = d.tags
      .map(
        (t) => `<div class="tag-card" data-id="${t.id}">
        <div class="tag-head">
          <input class="tag-label-input" value="${escapeHtml(t.label)}" maxlength="60" />
          <span class="hint-inline">${escapeHtml([t.source, t.table, t.rowid !== null ? "rowid " + t.rowid : "", t.view].filter(Boolean).join(" · "))} · ${escapeHtml(t.created)}</span>
          <button type="button" class="link tag-del">Remove</button>
        </div>
        <textarea class="path-input full tag-note-input" rows="2" placeholder="Note">${escapeHtml(t.note)}</textarea>
        <table class="meta-table">${t.columns.map((c, i) => `<tr><td>${escapeHtml(c)}</td><td>${escapeHtml(t.values[i])}</td></tr>`).join("")}</table>
      </div>`
      )
      .join("");
    $$(".tag-card", box).forEach((card) => {
      const id = Number(card.dataset.id);
      $(".tag-del", card).addEventListener("click", async () => {
        await api("delete_tag", id);
        loadTags();
      });
      $(".tag-label-input", card).addEventListener("change", (e) => api("update_tag", id, e.target.value, null));
      $(".tag-note-input", card).addEventListener("change", (e) => api("update_tag", id, null, e.target.value));
    });
  } catch (e) {
    /* no case open */
  }
}

// ---------- Report ----------
$("#report-view-btn").addEventListener("click", async (e) => {
  busy(e.currentTarget, true, "Building…");
  try {
    await api("view_report");
  } catch (err) {
    showError(err.message);
  } finally {
    busy(e.currentTarget, false);
  }
});
for (const [id, kind] of [["#report-html-btn", "html"], ["#report-json-btn", "json"]]) {
  $(id).addEventListener("click", async () => {
    try {
      const d = await api("save_report", kind);
      if (d.path) flash(`Saved ${d.path}`);
    } catch (e) {
      showError(e.message);
    }
  });
}

// ---------- Start ----------
(async function init() {
  try {
    const opts = await api("options");
    Object.assign(TS_FORMATS, opts.timestamp_formats);
    Object.assign(TS_SQL, opts.timestamp_sql || {});
    Object.assign(TRANSFORMS, (await api("transforms")).transforms);
  } catch (e) {
    console.error(e);
  }
})();
