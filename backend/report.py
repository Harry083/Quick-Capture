"""Acquisition reports: a self-contained HTML report, a JSON export, and a plain-text log saved next to
the image (the usual companion file examiners expect alongside an E01/DD)."""
from __future__ import annotations

import html
import platform
from datetime import datetime, timezone
from pathlib import Path

from .ewf import APP_VERSION

HASH_LABELS = {"md5": "MD5", "sha1": "SHA-1", "sha256": "SHA-256"}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def fmt_bytes(n) -> str:
    if n is None:
        return "-"
    val = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < 1024 or unit == "TiB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024
    return f"{n} B"


def fmt_speed(bps) -> str:
    return f"{(bps or 0) / 1e6:.1f} MB/s"


def fmt_duration(seconds) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _ts(epoch) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_report_context(job) -> dict:
    opts = job.options
    return {
        "job_id": job.id,
        "tool": APP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "node": platform.node()},
        "case": opts.get("case", {}),
        "source": opts["source"],
        "device": job.device,
        "options": {k: v for k, v in opts.items() if k != "case"},
        "triage": job.triage,
        "result": job.result,
    }


def _rows(pairs) -> str:
    return "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in pairs if v not in (None, ""))


def _triage_html(triage: dict | None) -> str:
    if not triage:
        return "<p class='muted'>No pre-imaging scan was run for this acquisition.</p>"
    items = "".join(
        f"<li class='finding finding-{_esc(f['level'])}'><strong>{_esc(f['title'])}</strong> — {_esc(f['detail'])}</li>"
        for f in triage.get("findings", [])
    )
    smart = triage.get("smart") or {}
    attrs = "".join(
        f"<tr><td>{_esc(a['name'])}</td><td{' class=bad' if a.get('flag') else ''}>{_esc(a['value'])}</td></tr>"
        for a in smart.get("attributes", [])
    )
    bad = triage.get("bad_sectors", [])
    verdict = triage.get("verdict", "-")
    return f"""
<div class="verdict verdict-{_esc(verdict)}">Verdict: {_esc(verdict.upper())}</div>
<ul class="findings">{items}</ul>
<table>{_rows([
    ("Mode", triage.get("mode")),
    ("Probe read speed", fmt_speed(triage.get("read_speed"))),
    ("Estimated imaging time", fmt_duration(triage.get("estimated_seconds"))),
    ("Bytes scanned", fmt_bytes(triage.get("scanned_bytes"))),
    ("Bad sectors found", len(bad)),
    ("First bad LBAs", ", ".join(str(x) for x in bad[:20]) if bad else ""),
    ("Duration", fmt_duration(triage.get("duration"))),
])}</table>
{f"<h3 style='margin-top:14px'>SMART ({_esc(smart.get('device', ''))})</h3><table>{attrs}</table>" if attrs else ""}
"""


def generate_report_html(job) -> str:
    ctx = build_report_context(job)
    res = ctx["result"] or {}
    dev = ctx["device"] or {}
    case = ctx["case"]
    generated = datetime.fromisoformat(ctx["generated_at"]).strftime("%Y-%m-%d %H:%M UTC")
    hashes = res.get("hashes", {})
    verify = res.get("verify")

    hash_rows = "".join(
        f"<tr><td>{HASH_LABELS.get(a, a)}</td><td class='mono'>{_esc(h)}</td>"
        f"<td>{'' if not verify else ('✔ verified' if verify['hashes'].get(a) == h else '✖ MISMATCH')}</td></tr>"
        for a, h in hashes.items()
    )
    bad_rows = "".join(
        f"<tr><td>LBA {s:,}</td><td>{c:,} sector(s)</td></tr>" for s, c in res.get("bad_ranges", [])[:200]
    )
    files = "".join(f"<li class='mono'>{_esc(p)}</li>" for p in res.get("paths", []))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Quick Capture Report — {_esc(job.id)}</title>
<style>
  :root {{
    --bg: #0f1216; --panel: #171b21; --panel-2: #1e242c; --border: #2a313b;
    --text: #e6e9ef; --text-dim: #9aa4b2; --accent: #7fa8ff; --good: #3ecf8e; --warn: #f5b942; --bad: #f05a5a;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
          font-family: "Segoe UI", system-ui, -apple-system, sans-serif; font-size: 14px; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 32px 20px 60px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 0 0 12px; }}
  h3 {{ font-size: 12.5px; margin: 0 0 8px; color: var(--text-dim); text-transform: uppercase; letter-spacing: .04em; }}
  .meta-line {{ color: var(--text-dim); font-size: 12.5px; margin-bottom: 24px; }}
  .muted {{ color: var(--text-dim); }}
  .mono {{ font-family: Consolas, "Cascadia Mono", monospace; word-break: break-all; }}
  .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 20px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }}
  .card {{ background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }}
  .card-label {{ color: var(--text-dim); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }}
  .card-value {{ font-size: 22px; font-weight: 700; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 4px 6px; border-bottom: 1px solid var(--border); word-break: break-word; vertical-align: top; }}
  td:first-child {{ color: var(--text-dim); width: 34%; white-space: nowrap; }}
  td.bad {{ color: var(--bad); font-weight: 600; }}
  .verdict {{ display: inline-block; font-weight: 700; padding: 4px 10px; border-radius: 6px; margin-bottom: 10px; }}
  .verdict-clear {{ color: var(--good); border: 1px solid var(--good); }}
  .verdict-attention {{ color: var(--warn); border: 1px solid var(--warn); }}
  .findings {{ padding-left: 18px; margin: 0 0 12px; }}
  .finding-ok {{ color: var(--good); }} .finding-bad {{ color: var(--bad); }} .finding-info {{ color: var(--text-dim); }}
  ul.files {{ margin: 0; padding-left: 18px; }}
  @media print {{
    body {{ background: white; color: black; }}
    .panel, .card {{ background: white; border-color: #ccc; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Quick Capture Acquisition Report</h1>
  <div class="meta-line">Generated {_esc(generated)} · job {_esc(ctx['job_id'])} · {_esc(ctx['tool'])}
    · {_esc(ctx['host']['node'])} ({_esc(ctx['host']['platform'])})</div>

  <div class="panel">
    <h2>Summary</h2>
    <div class="cards">
      <div class="card"><div class="card-label">Media size</div><div class="card-value">{fmt_bytes(res.get('total_bytes'))}</div></div>
      <div class="card"><div class="card-label">Duration</div><div class="card-value">{fmt_duration(res.get('duration'))}</div></div>
      <div class="card"><div class="card-label">Average speed</div><div class="card-value">{fmt_speed(res.get('avg_speed'))}</div></div>
      <div class="card"><div class="card-label">Image size</div><div class="card-value">{fmt_bytes(res.get('image_bytes'))}</div></div>
      <div class="card"><div class="card-label">Bad sectors</div><div class="card-value" style="color:{'var(--bad)' if res.get('bad_sectors') else 'var(--good)'}">{res.get('bad_sectors', 0):,}</div></div>
    </div>
  </div>

  <div class="panel">
    <h2>Case</h2>
    <table>{_rows([
        ("Case number", case.get("case_number")), ("Evidence number", case.get("evidence_number")),
        ("Examiner", case.get("examiner")), ("Description", case.get("description")), ("Notes", case.get("notes")),
    ]) or "<tr><td colspan=2 class='muted'>No case details entered</td></tr>"}</table>
  </div>

  <div class="panel">
    <h2>Source</h2>
    <table>{_rows([
        ("Path", ctx["source"]), ("Model", dev.get("model")), ("Serial", dev.get("serial")),
        ("Type", dev.get("kind")), ("Bytes per sector", res.get("sector_size")),
        ("Size", f"{res.get('total_bytes', 0):,} bytes"),
    ])}</table>
  </div>

  <div class="panel">
    <h2>Hashes (computed while reading the source)</h2>
    <table>{hash_rows}</table>
    {f"<p class='muted' style='margin-bottom:0'>Image re-read and verified in {fmt_duration(verify['duration'])}.</p>" if verify else "<p class='muted' style='margin-bottom:0'>Post-acquisition verification was not run.</p>"}
  </div>

  <div class="panel">
    <h2>Output</h2>
    <table>{_rows([
        ("Format", "E01 (EnCase 6)" if res.get("format") == "e01" else "Raw (dd)"),
        ("Compression", res.get("compression")),
        ("Segment size", fmt_bytes(res.get("segment_size")) if res.get("segment_size") else "no split"),
        ("Read block size", fmt_bytes(res.get("block_size"))),
        ("Reads in flight", res.get("io_depth")),
        ("Speed limited by", (res.get("bottleneck") or {}).get("label")),
        ("Started", _ts(res.get("started_at"))), ("Finished", _ts(res.get("finished_at"))),
    ])}</table>
    <h3 style="margin-top:14px">Files</h3>
    <ul class="files">{files}</ul>
  </div>

  <div class="panel">
    <h2>Scan</h2>
    {_triage_html(ctx['triage'])}
  </div>

  {f'''<div class="panel"><h2>Unreadable sectors (zero-filled)</h2><table>{bad_rows}</table></div>''' if bad_rows else ""}
</div>
</body>
</html>
"""


def generate_report_json(job) -> dict:
    return build_report_context(job)


def write_acquisition_log(job) -> str:
    """Write `<image name>.txt` next to the image and return its path."""
    ctx = build_report_context(job)
    res = ctx["result"]
    case = ctx["case"]
    dev = ctx["device"] or {}
    triage = ctx["triage"]
    lines = [
        f"Created by {ctx['tool']} (Quick Capture)",
        f"Host: {ctx['host']['node']} ({ctx['host']['platform']})",
        "",
        "[Case]",
        f"Case number:     {case.get('case_number', '')}",
        f"Evidence number: {case.get('evidence_number', '')}",
        f"Examiner:        {case.get('examiner', '')}",
        f"Description:     {case.get('description', '')}",
        f"Notes:           {case.get('notes', '')}",
        "",
        "[Source]",
        f"Path:            {ctx['source']}",
        f"Model:           {dev.get('model', '')}",
        f"Serial:          {dev.get('serial', '')}",
        f"Size:            {res['total_bytes']:,} bytes ({fmt_bytes(res['total_bytes'])})",
        f"Sector size:     {res['sector_size']}",
        "",
        "[Scan]",
    ]
    if triage:
        lines.append(f"Mode:            {triage['mode']}   Verdict: {triage['verdict'].upper()}")
        for f in triage["findings"]:
            lines.append(f"  - [{f['level']}] {f['title']}: {f['detail']}")
    else:
        lines.append("Skipped")
    lines += [
        "",
        "[Acquisition]",
        f"Format:          {'E01 (EnCase 6)' if res['format'] == 'e01' else 'Raw (dd)'}",
        f"Compression:     {res.get('compression') or '-'}",
        f"Started:         {_ts(res['started_at'])}",
        f"Finished:        {_ts(res['finished_at'])}",
        f"Duration:        {fmt_duration(res['duration'])}",
        f"Average speed:   {fmt_speed(res['avg_speed'])}",
        f"Limited by:      {(res.get('bottleneck') or {}).get('label', '-')}",
        f"Bad sectors:     {res['bad_sectors']:,} (zero-filled)",
    ]
    for s, c in res.get("bad_ranges", []):
        lines.append(f"  LBA {s} (+{c})")
    lines += ["", "[Hashes]"]
    for a, h in res["hashes"].items():
        lines.append(f"{HASH_LABELS.get(a, a) + ':':<17}{h}")
    if res.get("verify"):
        v = res["verify"]
        lines.append(f"Verification:    {'MATCH' if v['match'] else 'MISMATCH'} ({fmt_duration(v['duration'])})")
    lines += ["", "[Files]"] + res["paths"]

    log_path = Path(job.options["output_dir"]) / f"{job.options['name']}.txt"
    with open(log_path, "x", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    return str(log_path)
