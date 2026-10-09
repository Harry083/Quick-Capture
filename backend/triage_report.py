"""Triage reports: a self-contained HTML report (same look as the app) and a JSON export."""
from __future__ import annotations

import base64
import functools
import html
import platform
from datetime import datetime, timezone
from pathlib import Path

from .ewf import APP_VERSION

FONT_PATH = Path(__file__).resolve().parent.parent / "frontend" / "fonts" / "manrope-variable.woff2"
OS_LABELS = (
    ("name", "Operating system"), ("edition", "Edition"), ("version", "Version"), ("build", "Build"),
    ("codename", "Codename"), ("architecture", "Architecture"), ("installed", "Installed"),
    ("registered_owner", "Registered owner"), ("registered_org", "Registered organisation"),
    ("product_id", "Product ID"), ("kernel_versions", "Kernels in /boot"),
)
DEVICE_LABELS = (
    ("computer_name", "Computer name"), ("domain", "Domain"), ("manufacturer", "Manufacturer"),
    ("model", "Model"), ("bios", "BIOS"), ("ip_addresses", "IP addresses"), ("time_zone", "Time zone"),
    ("utc_offset", "UTC offset"), ("last_shutdown", "Last shutdown"), ("last_mounted", "Last mounted"),
    ("last_mounted_at", "Last mount point"), ("last_written", "Volume last written"),
)
HEADER_LABELS = (
    ("case_number", "Case number"), ("evidence_number", "Evidence number"), ("examiner", "Examiner"),
    ("description", "Description"), ("notes", "Notes"), ("model", "Source model"), ("serial", "Source serial"),
    ("acquired", "Acquired"), ("acquisition_software", "Acquired with"), ("acquisition_os", "Acquisition OS"),
)


@functools.lru_cache(maxsize=1)
def _font_src() -> str:
    """Manrope as a data: URI, so the report window and saved copies render in the app's font."""
    try:
        return "data:font/woff2;base64," + base64.b64encode(FONT_PATH.read_bytes()).decode("ascii")
    except OSError:
        return ""


def _esc(value) -> str:
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    return html.escape(str(value), quote=True)


def fmt_bytes(n) -> str:
    if n is None:
        return "-"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1000 or unit == "TB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1000
    return f"{n} B"


def _rows(pairs) -> str:
    return "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in pairs if v not in (None, "", []))


def _labelled(d: dict, labels) -> str:
    return _rows((label, d.get(key)) for key, label in labels)


def build_report_context(job) -> dict:
    return {
        "tool": APP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "node": platform.node()},
        "image_path": job.path,
        "result": job.result,
    }


def _users_table(users: list[dict]) -> str:
    if not users:
        return "<p class='muted'>No user accounts found.</p>"
    rows = []
    for u in users:
        saved = u.get("last_saved") or {}
        status = "disabled" if u.get("disabled") else ""
        rows.append(
            f"<tr><td>{_esc(u.get('username', ''))}{' <span class=muted>(' + _esc(u['full_name']) + ')</span>' if u.get('full_name') and u.get('full_name') != u.get('username') else ''}</td>"
            f"<td>{_esc(u.get('account', ''))}{' · ' + status if status else ''}</td>"
            f"<td>{_esc(u.get('last_logon') or '-')}</td>"
            f"<td>{_esc(u.get('logon_count', '')) if u.get('logon_count') is not None else ''}</td>"
            f"<td class='mono'>{_esc(saved.get('path', ''))}<br><span class='muted'>{_esc(saved.get('modified', ''))}</span></td></tr>"
        )
    return ("<table class='grid'><tr><th>User</th><th>Account</th><th>Last logon</th><th>Logons</th>"
            f"<th>Last saved file</th></tr>{''.join(rows)}</table>")


def _system_html(s: dict) -> str:
    vol = s.get("volume", {})
    os_info, dev = s.get("os", {}), s.get("device", {})
    latest = s.get("last_saved")
    recent = "".join(
        f"<tr><td>{_esc(f.get('modified'))}</td><td>{_esc(f.get('user'))}</td><td class='mono'>{_esc(f.get('path'))}</td>"
        f"<td>{fmt_bytes(f.get('size'))}</td></tr>"
        for f in s.get("recent_files", [])
    )
    latest_html = (
        f"<div class='latest'><div class='card-label'>Last saved file</div><div class='mono big'>{_esc(latest['path'])}</div>"
        f"<div class='muted'>{_esc(latest['modified'])} · {_esc(latest['user'])} · {fmt_bytes(latest.get('size'))}</div></div>"
        if latest else "<p class='muted'>No user files found.</p>"
    )
    return f"""
  <div class="panel">
    <p class="eyebrow">Partition {_esc(vol.get('index'))} · {_esc(vol.get('fs'))} · {fmt_bytes(vol.get('size'))}</p>
    <h2>{_esc(os_info.get('name') or s['kind'].title())}</h2>
    <div class="two">
      <div><h3>Operating system</h3><table>{_labelled(os_info, OS_LABELS)}</table></div>
      <div><h3>Device</h3><table>{_labelled(dev, DEVICE_LABELS) or "<tr><td class='muted'>Nothing recorded</td></tr>"}</table></div>
    </div>
    <h3 style="margin-top:1.4rem">Users</h3>
    {_users_table(s.get('users', []))}
    <h3 style="margin-top:1.4rem">Last saved</h3>
    {latest_html}
    {f"<table class='grid' style='margin-top:1rem'><tr><th>Modified</th><th>User</th><th>Path</th><th>Size</th></tr>{recent}</table>" if recent else ""}
  </div>"""


def generate_report_html(job) -> str:
    ctx = build_report_context(job)
    r = ctx["result"] or {}
    img = r.get("image", {})
    generated = datetime.fromisoformat(ctx["generated_at"]).strftime("%Y-%m-%d %H:%M UTC")
    systems = r.get("systems", [])
    first = systems[0] if systems else {}
    latest = first.get("last_saved") or {}
    findings = "".join(
        f"<li class='finding finding-{_esc(f['level'])}'><strong>{_esc(f['title'])}</strong> — {_esc(f['detail'])}</li>"
        for f in r.get("findings", [])
    )
    vols = "".join(
        f"<tr><td>{_esc(v['index'])}</td><td>{_esc(v.get('type'))}{' · ' + _esc(v['name']) if v.get('name') else ''}</td>"
        f"<td>{_esc(v.get('fs'))}</td><td>{fmt_bytes(v.get('size'))}</td><td>{_esc(v.get('os', ''))}</td></tr>"
        for v in r.get("volumes", [])
    )
    header = img.get("header") or {}
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Quick Capture Triage — {_esc(Path(ctx['image_path']).name)}</title>
<style>
  /* Same tokens as the app (frontend/styles.css). Manrope is embedded so saved copies keep it. */
  @font-face {{
    font-family: "Manrope";
    src: url("{_font_src()}") format("woff2");
    font-weight: 200 800;
    font-display: swap;
  }}
  :root {{
    --bg: #1c2023; --panel: #262c30; --panel-2: #30373c;
    --border: rgba(255, 255, 255, 0.08); --border-strong: rgba(255, 255, 255, 0.14);
    --text: #eef1f2; --text-dim: #a9b3b8; --text-faint: #7f8b91;
    --accent: #e8793b; --accent-2: #f39a63; --good: #3ecf8e; --warn: #f5b942; --bad: #f05a5a;
    --font-body: "Manrope", "Segoe UI", system-ui, -apple-system, sans-serif;
    --font-mono: Consolas, "Cascadia Mono", "SFMono-Regular", monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; color: var(--text); font-family: var(--font-body); font-size: 15px; line-height: 1.6;
          background: radial-gradient(90rem 40rem at 50% -12rem, rgba(154, 170, 178, 0.14), transparent 70%), var(--bg);
          -webkit-font-smoothing: antialiased; }}
  .wrap {{ max-width: 1000px; margin: 0 auto; padding: clamp(2rem, 1rem + 4vw, 4rem) clamp(1rem, 3vw, 2rem) 3rem; }}
  h1, h2, h3 {{ line-height: 1.1; margin: 0 0 0.5em; }}
  h1 {{ font-size: clamp(2rem, 1.4rem + 2.4vw, 3rem); font-weight: 500; letter-spacing: -0.035em; }}
  h2 {{ font-size: 1.4rem; font-weight: 500; letter-spacing: -0.025em; margin-bottom: 1rem; }}
  h3 {{ font-family: var(--font-mono); font-size: 0.72rem; font-weight: 400; color: var(--text-faint);
        text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.8em; }}
  .eyebrow {{ font-family: var(--font-mono); font-size: 0.78rem; color: var(--accent-2); text-transform: uppercase;
              letter-spacing: 0.08em; margin: 0 0 1rem; }}
  .meta-line {{ font-family: var(--font-mono); color: var(--text-faint); font-size: 0.78rem; margin-bottom: 2rem; }}
  .muted {{ color: var(--text-faint); font-family: var(--font-mono); font-size: 0.8rem; }}
  .mono {{ font-family: var(--font-mono); font-size: 0.85rem; word-break: break-all; }}
  .big {{ font-size: 1.05rem; color: var(--text); }}
  .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 28px;
            padding: clamp(1.3rem, 1rem + 1.2vw, 2rem); margin-bottom: 14px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }}
  .card, .latest {{ background: var(--panel-2); border: 1px solid var(--border); border-radius: 18px; padding: 1.1rem 1.2rem; }}
  .card-label {{ font-family: var(--font-mono); color: var(--accent-2); font-size: 0.72rem; text-transform: uppercase;
                 letter-spacing: 0.08em; margin-bottom: 0.7rem; }}
  .card-value {{ font-size: 1.25rem; font-weight: 500; line-height: 1.2; letter-spacing: -0.02em; overflow-wrap: anywhere; }}
  .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0 2rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; font-family: var(--font-mono); font-size: 0.72rem; font-weight: 400; color: var(--text-faint);
        text-transform: uppercase; letter-spacing: 0.06em; padding: 0 0.6em 0.5em 0; border-bottom: 1px solid var(--border-strong); }}
  td {{ padding: 0.45em 0.6em 0.45em 0; border-bottom: 1px solid var(--border); word-break: break-word; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  table:not(.grid) td:first-child {{ color: var(--text-faint); font-family: var(--font-mono); font-size: 0.8rem; width: 38%; }}
  .findings {{ list-style: none; padding: 0; margin: 0; }}
  .finding {{ padding: 0.6rem 0 0.6rem 1rem; border-left: 2px solid var(--text-faint); border-bottom: 1px solid var(--border); }}
  .finding-ok {{ border-left-color: var(--good); }} .finding-bad {{ border-left-color: var(--bad); }}
  .finding strong {{ font-weight: 600; }}
  .site-footer {{ color: var(--text-faint); font-size: 0.85rem; padding-top: 1.4rem; border-top: 1px solid var(--border); margin-top: 2rem; }}
  @media (max-width: 760px) {{ .two {{ grid-template-columns: 1fr; }} }}
  @media print {{
    body {{ background: white; color: black; }}
    .panel, .card, .latest {{ background: white; border-color: #ccc; }}
    td:first-child, th, .card-label, .meta-line, .muted, h3 {{ color: #555; }}
    .big {{ color: black; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <p class="eyebrow">Quick Capture · triage report</p>
  <h1>{_esc(Path(ctx['image_path']).name)}</h1>
  <div class="meta-line">Generated {_esc(generated)} · {_esc(ctx['tool'])} · {_esc(ctx['host']['node'])}
    ({_esc(ctx['host']['platform'])}) · took {r.get('duration', 0):.1f}s</div>

  <div class="panel">
    <h2>Summary</h2>
    <div class="cards">
      <div class="card"><div class="card-label">Operating system</div><div class="card-value">{_esc((first.get('os') or {}).get('name') or 'Not found')}</div></div>
      <div class="card"><div class="card-label">Device</div><div class="card-value">{_esc((first.get('device') or {}).get('computer_name') or '-')}</div></div>
      <div class="card"><div class="card-label">Users</div><div class="card-value">{len(first.get('users', [])) if systems else '-'}</div></div>
      <div class="card"><div class="card-label">Last saved</div><div class="card-value">{_esc(latest.get('name') or '-')}</div>
        <div class="muted">{_esc(latest.get('modified') or '')}</div></div>
    </div>
  </div>

  {''.join(_system_html(s) for s in systems)}

  <div class="panel">
    <h2>Findings</h2>
    {f"<ul class='findings'>{findings}</ul>" if findings else "<p class='muted'>Nothing to flag.</p>"}
  </div>

  <div class="panel">
    <h2>Image</h2>
    <div class="two">
      <div><h3>Evidence</h3><table>{_rows([
          ("Path", img.get('path')), ("Format", img.get('format')), ("Segments", len(img.get('segments', []))),
          ("Media size", f"{fmt_bytes(img.get('size'))} ({img.get('size', 0):,} bytes)"),
          ("Bytes per sector", img.get('bytes_per_sector')), ("Stored MD5", img.get('md5')), ("Stored SHA-1", img.get('sha1')),
      ])}</table></div>
      <div><h3>Acquisition header</h3><table>{_labelled(header, HEADER_LABELS) or "<tr><td class='muted'>No header (raw image)</td></tr>"}</table></div>
    </div>
    <h3 style="margin-top:1.4rem">Partitions ({_esc(r.get('partition_scheme', '-'))})</h3>
    <table class="grid"><tr><th>#</th><th>Type</th><th>Filesystem</th><th>Size</th><th>System</th></tr>{vols}</table>
    <p class="muted" style="margin:1rem 0 0">Stored hashes are read from the image, not recomputed. Triage reads metadata only.</p>
  </div>

  <div class="site-footer">Quick Capture · a Harry Smallwood tool</div>
</div>
</body>
</html>
"""


def generate_report_json(job) -> dict:
    return build_report_context(job)
