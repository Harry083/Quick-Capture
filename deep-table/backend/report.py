"""Examination reports (self-contained HTML and JSON) and CSV exports."""
from __future__ import annotations

import base64
import csv
import functools
import html
import io
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import decoders

APP_NAME = "Deep Table"
APP_VERSION = "1.0.0"
FONT_PATH = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "frontend" / "fonts" / "manrope-variable.woff2"
HASH_LABELS = {"md5": "MD5", "sha1": "SHA-1", "sha256": "SHA-256"}
REPORT_QUERY_ROWS = 1000
REPORT_RECOVERED_ROWS = 2000


@functools.lru_cache(maxsize=1)
def _font_src() -> str:
    try:
        return "data:font/woff2;base64," + base64.b64encode(FONT_PATH.read_bytes()).decode("ascii")
    except OSError:
        return ""


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt_bytes(n) -> str:
    if n is None:
        return "-"
    val = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < 1024 or unit == "TiB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024
    return f"{n} B"


def display(value, fmt: str | None = None) -> str:
    """One value as report / CSV text. Blobs become a type + size note; timestamps get their reading."""
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return f"[BLOB {decoders.detect_blob(data)['kind']}, {len(data):,} bytes]"
    if fmt and isinstance(value, (int, float)) and not isinstance(value, bool):
        reading = decoders.format_dt(decoders.convert_timestamp(value, fmt))
        if reading:
            return f"{value} ({reading})"
    return str(value)


def _kv(pairs) -> str:
    return "".join(f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>" for k, v in pairs)


def _grid(columns: list[str], rows: list[list[str]], more: int = 0) -> str:
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in r) + "</tr>" for r in rows)
    extra = f"<p class='muted'>… {more:,} more rows not shown.</p>" if more else ""
    return f"<div class='grid-wrap'><table class='grid'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{extra}"


def build_context(case) -> dict:
    s = case.summary()
    queries = []
    for q in case.saved_queries:
        try:
            cols, rows, more = case.query_raw(q["view"], q["sql"], REPORT_QUERY_ROWS)
            fm = case.detect_formats("query", cols, rows)
            queries.append({**q, "columns": cols, "rows": [[display(v, fm.get(c)) for c, v in zip(cols, r)] for r in rows],
                            "truncated": more, "error": ""})
        except Exception as exc:  # noqa: BLE001  (a broken saved query shouldn't stop the report)
            queries.append({**q, "columns": [], "rows": [], "truncated": False, "error": str(exc)})
    recovered = case.recover()
    counts: dict = {}
    for r in recovered:
        key = (r["table"], r["status"])
        counts[key] = counts.get(key, 0) + 1
    notable = [r for r in recovered if r["status"] != "live"]
    return {
        "tool": f"{APP_NAME} {APP_VERSION}",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "host": platform.node(),
        "platform": platform.platform(),
        "case": case.case_info,
        "summary": s,
        "objects": [{k: o.get(k) for k in ("type", "name", "rows", "rootpage")} for o in case.objects("current")],
        "tags": case.tags,
        "queries": queries,
        "query_log": case.query_log,
        "recovered_counts": [{"table": t, "status": st, "count": n} for (t, st), n in sorted(counts.items())],
        "recovered": notable,
    }


def _rec_values(r: dict) -> str:
    return " | ".join(f"{c}={display(v)}" for c, v in zip(r["columns"], r["values"]))


def generate_html(case) -> str:
    ctx = build_context(case)
    s = ctx["summary"]
    f = s["file"]
    hdr = s["header"]
    case_info = ctx["case"]

    evidence_rows = ""
    for e in s["evidence"]:
        hashes = "<br>".join(f"<span class='muted'>{HASH_LABELS[k]}</span> <span class='mono'>{_esc(v)}</span>"
                             for k, v in e["hashes"].items())
        evidence_rows += (f"<tr><td>{_esc(e['role'])}</td><td><div class='mono'>{_esc(e['path'])}</div>"
                          f"<div class='muted'>{fmt_bytes(e['size'])} ({e['size']:,} bytes) · modified {_esc(e['modified'])}"
                          f" · working copy verified</div>{hashes}</td></tr>")

    wal = s.get("wal")
    wal_html = ""
    if wal:
        wal_html = _kv([
            ("Frames", f"{wal.get('frames', 0):,} ({wal.get('valid_frames', 0):,} valid, "
                       f"{wal.get('old_frames', 0):,} from earlier checkpoints)"),
            ("Commits", f"{wal.get('commits', 0):,}"),
            ("Checkpoint sequence", _esc(wal.get("checkpoint_seq", ""))),
        ]) if not wal.get("error") else _kv([("WAL", _esc(wal["error"]))])
    journal = s.get("journal")
    journal_html = _kv([("Journal pages", f"{journal.get('pages', 0):,}")] + (
        [("Note", "Header zeroed (PERSIST mode); pages read anyway")] if journal.get("zeroed_header") else [])) \
        if journal else ""

    objects = _grid(["Type", "Name", "Rows", "Root page"],
                    [[o["type"], o["name"], "" if o["rows"] is None else f"{o['rows']:,}", o["rootpage"]]
                     for o in ctx["objects"] if o["type"] in ("table", "view")])

    tags_html = ""
    for t in ctx["tags"]:
        vals = "".join(f"<tr><td>{_esc(c)}</td><td>{_esc(v)}</td></tr>" for c, v in zip(t["columns"], t["values"]))
        where = " · ".join(x for x in (t["source"], t["table"], f"rowid {t['rowid']}" if t["rowid"] is not None else "",
                                       t["view"], t["detail"]) if x)
        note = f"<p>{_esc(t['note'])}</p>" if t["note"] else ""
        tags_html += (f"<div class='tag'><div class='tag-head'><span class='label'>{_esc(t['label'])}</span>"
                      f"<span class='muted'>{_esc(where)} · {_esc(t['created'])}</span></div>"
                      f"{note}<table>{vals}</table></div>")

    queries_html = ""
    for q in ctx["queries"]:
        body = f"<p class='bad'>{_esc(q['error'])}</p>" if q["error"] else _grid(q["columns"], q["rows"])
        trunc = f"<p class='muted'>First {REPORT_QUERY_ROWS:,} rows only.</p>" if q["truncated"] else ""
        queries_html += (f"<h3>{_esc(q['name'])} <span class='muted'>· view: {_esc(q['view'])}</span></h3>"
                         f"<pre>{_esc(q['sql'])}</pre>{body}{trunc}")

    counts_html = _grid(["Table", "Status", "Records"],
                        [[c["table"], c["status"], f"{c['count']:,}"] for c in ctx["recovered_counts"]])
    rec_rows = [[r["table"], r["status"], r["source"], "" if r["rowid"] is None else r["rowid"],
                 r["page"] or "", "" if r["frame"] is None else r["frame"], r["confidence"], _rec_values(r),
                 ", ".join(r.get("also") or [])]
                for r in ctx["recovered"][:REPORT_RECOVERED_ROWS]]
    rec_html = _grid(["Table", "Status", "Source", "Rowid", "Page", "Frame", "Confidence", "Values", "Also found in"], rec_rows,
                     max(0, len(ctx["recovered"]) - REPORT_RECOVERED_ROWS))
    free = s["free_space"]
    free_note = ("Free space appears wiped (secure_delete): deleted content has most likely been overwritten."
                 if free["wiped"] else f"{fmt_bytes(free['bytes'])} of free space examined.")

    log_rows = [[q["at"], q["view"], q["rows"], q["sql"]] for q in ctx["query_log"][-200:]]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Deep Table Report — {_esc(f['name'])}</title>
<style>
  @font-face {{ font-family: "Manrope"; src: url("{_font_src()}") format("woff2"); font-weight: 200 800; font-display: swap; }}
  :root {{
    --bg: #1c2023; --panel: #262c30; --panel-2: #30373c;
    --border: rgba(255, 255, 255, 0.08); --text: #eef1f2; --text-dim: #a9b3b8; --text-faint: #7f8b91;
    --accent: #e8793b; --accent-2: #f39a63; --good: #3ecf8e; --warn: #f5b942; --bad: #f05a5a;
    --font-body: "Manrope", "Segoe UI", system-ui, -apple-system, sans-serif;
    --font-mono: Consolas, "Cascadia Mono", "SFMono-Regular", monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; color: var(--text); font-family: var(--font-body); font-size: 15px; line-height: 1.6;
          background: radial-gradient(90rem 40rem at 50% -12rem, rgba(154, 170, 178, 0.14), transparent 70%), var(--bg); }}
  .wrap {{ max-width: 1200px; margin: 0 auto; padding: clamp(2rem, 1rem + 4vw, 4rem) clamp(1rem, 3vw, 2rem) 3rem; }}
  h1, h2, h3 {{ line-height: 1.1; margin: 0 0 0.5em; }}
  h1 {{ font-size: clamp(2rem, 1.4rem + 2.4vw, 3rem); font-weight: 500; letter-spacing: -0.035em; }}
  h2 {{ font-size: 1.4rem; font-weight: 500; letter-spacing: -0.025em; margin-bottom: 1rem; }}
  h3 {{ font-size: 1rem; font-weight: 600; margin: 1.4rem 0 0.6rem; }}
  .eyebrow {{ font-family: var(--font-mono); font-size: 0.78rem; color: var(--accent-2); text-transform: uppercase;
              letter-spacing: 0.08em; margin: 0 0 1rem; }}
  .meta-line {{ font-family: var(--font-mono); color: var(--text-faint); font-size: 0.78rem; margin-bottom: 2rem; }}
  .muted {{ color: var(--text-faint); font-family: var(--font-mono); font-size: 0.78rem; }}
  .mono {{ font-family: var(--font-mono); font-size: 0.82rem; word-break: break-all; }}
  .bad {{ color: var(--bad); }}
  .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 28px;
            padding: clamp(1.3rem, 1rem + 1.2vw, 2rem); margin-bottom: 14px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }}
  .card {{ background: var(--panel-2); border: 1px solid var(--border); border-radius: 18px; padding: 1rem 1.1rem; }}
  .card-label {{ font-family: var(--font-mono); color: var(--accent-2); font-size: 0.7rem; text-transform: uppercase;
                 letter-spacing: 0.08em; margin-bottom: 0.6rem; }}
  .card-value {{ font-size: 1.6rem; font-weight: 500; line-height: 1; letter-spacing: -0.04em; font-variant-numeric: tabular-nums; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
  td, th {{ padding: 0.4em 0.6em 0.4em 0; border-bottom: 1px solid var(--border); word-break: break-word; vertical-align: top; text-align: left; }}
  tr:last-child td {{ border-bottom: none; }}
  .kv td:first-child, .tag td:first-child {{ color: var(--text-faint); font-family: var(--font-mono); font-size: 0.78rem; width: 26%; }}
  .grid-wrap {{ overflow-x: auto; }}
  .grid th {{ font-family: var(--font-mono); font-size: 0.72rem; color: var(--accent-2); font-weight: 400; text-transform: uppercase; letter-spacing: 0.05em; white-space: nowrap; }}
  .grid td {{ font-size: 0.82rem; max-width: 40rem; }}
  pre {{ background: var(--panel-2); border-radius: 12px; padding: 0.8rem 1rem; font-family: var(--font-mono); font-size: 0.8rem; white-space: pre-wrap; }}
  .tag {{ border-left: 3px solid var(--accent); padding: 0.4rem 0 0.4rem 1rem; margin-bottom: 1.2rem; }}
  .tag-head {{ display: flex; gap: 1rem; align-items: baseline; flex-wrap: wrap; margin-bottom: 0.3rem; }}
  .label {{ font-weight: 600; color: var(--accent-2); }}
  .site-footer {{ color: var(--text-faint); font-size: 0.85rem; padding-top: 1.4rem; border-top: 1px solid var(--border); margin-top: 2rem; }}
  @media print {{
    body {{ background: white; color: black; }}
    .panel, .card, pre {{ background: white; border-color: #ccc; }}
    .muted, .meta-line, .kv td:first-child {{ color: #555; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <p class="eyebrow">Deep Table · SQLite examination report</p>
  <h1>{_esc(f['name'])}</h1>
  <div class="meta-line">Generated {_esc(ctx['generated'])} · {_esc(ctx['tool'])} · opened {_esc(s['opened_at'])} · {_esc(ctx['host'])}</div>

  <div class="panel">
    <h2>Summary</h2>
    <div class="cards">
      <div class="card"><div class="card-label">Tables</div><div class="card-value">{s['tables']:,}</div></div>
      <div class="card"><div class="card-label">Live rows</div><div class="card-value">{s['total_rows']:,}</div></div>
      <div class="card"><div class="card-label">Pages</div><div class="card-value">{s['page_count']:,}</div></div>
      <div class="card"><div class="card-label">Free pages</div><div class="card-value">{s['freelist_pages']:,}</div></div>
      <div class="card"><div class="card-label">Recovered</div><div class="card-value">{len(ctx['recovered']):,}</div></div>
      <div class="card"><div class="card-label">Bookmarks</div><div class="card-value">{len(ctx['tags']):,}</div></div>
    </div>
    <p class="muted" style="margin-bottom:0">{_esc(free_note)}</p>
  </div>

  <div class="panel">
    <h2>Case</h2>
    <table class="kv">{_kv([(k.replace('_', ' ').capitalize(), _esc(v)) for k, v in case_info.items() if v]) or "<tr><td>Case</td><td>Not recorded</td></tr>"}</table>
  </div>

  <div class="panel">
    <h2>Evidence</h2>
    <p class="muted">Originals are hashed, then copied and the copies re-hashed; every view is built from the copies and opened read-only.</p>
    <table class="kv">{evidence_rows}</table>
  </div>

  <div class="panel">
    <h2>Database header</h2>
    <table class="kv">{_kv([(k.replace('_', ' '), _esc(v)) for k, v in hdr.items()])}</table>
    {f"<h3>Write-ahead log</h3><table class='kv'>{wal_html}</table>" if wal_html else ""}
    {f"<h3>Rollback journal</h3><table class='kv'>{journal_html}</table>" if journal_html else ""}
  </div>

  <div class="panel"><h2>Tables and views</h2>{objects}</div>

  {f'<div class="panel"><h2>Bookmarked records</h2>{tags_html}</div>' if tags_html else ""}
  {f'<div class="panel"><h2>Saved queries</h2>{queries_html}</div>' if queries_html else ""}

  <div class="panel">
    <h2>Recovered records</h2>
    <p class="muted">Deleted records and earlier versions carved from freeblocks, unallocated space, freelist pages, WAL
      frames and journal pages. Copies of rows that are still live are counted but not listed.</p>
    {counts_html}
    {f"<h3>Deleted and earlier versions</h3>{rec_html}" if rec_rows else ""}
  </div>

  {f'<div class="panel"><h2>Query log</h2>{_grid(["When", "View", "Rows", "SQL"], log_rows)}</div>' if log_rows else ""}

  <div class="site-footer">{_esc(ctx['tool'])} · {_esc(ctx['platform'])}</div>
</div>
</body>
</html>"""


def generate_json(case) -> dict:
    ctx = build_context(case)

    def clean(v):
        if isinstance(v, (bytes, bytearray, memoryview)):
            return {"$blob_base64": base64.b64encode(bytes(v)).decode()}
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        return v

    return clean(ctx)


def to_csv(columns: list[str], rows: list[list], formats: dict | None = None) -> str:
    """CSV with blobs as hex, and an extra '<col> (UTC)' column next to each timestamp column."""
    formats = formats or {}
    buf = io.StringIO()
    w = csv.writer(buf)
    header = []
    for c in columns:
        header.append(c)
        if c in formats:
            header.append(f"{c} (UTC, {formats[c]})")
    w.writerow(header)
    for r in rows:
        out = []
        for c, v in zip(columns, r):
            if isinstance(v, (bytes, bytearray, memoryview)):
                out.append(bytes(v).hex())
            else:
                out.append("" if v is None else v)
            if c in formats:
                out.append(decoders.format_dt(decoders.convert_timestamp(v, formats[c])))
        w.writerow(out)
    return buf.getvalue()
