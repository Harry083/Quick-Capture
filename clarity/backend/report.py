"""Enhancement reports: a self-contained HTML report (same look as the app) and a JSON export.

The report is written so that someone else can understand and repeat the work:
  - the source file, its hashes, and a statement that it was not modified
  - the frame shown, before and after
  - every step of the chain, in order, with its parameters, what the filter does, why it is used, how it
    works, its caveats, and the image after that step
  - measurements, exports (with hashes), and the software environment
  - an appendix describing every filter Clarity offers, used or not
"""
from __future__ import annotations

import base64
import functools
import html
import json
import math
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from .filters import CATEGORIES, FILTERS, catalogue
from .pipeline import Pipeline, Step, chain_json

APP_NAME = "Clarity"
APP_VERSION = "Clarity 1.0"
FONT_PATH = Path(__file__).resolve().parent.parent / "frontend" / "fonts" / "manrope-variable.woff2"
CASE_LABELS = (("case_number", "Case number"), ("exhibit", "Exhibit / item"), ("examiner", "Examiner"),
               ("organisation", "Organisation"), ("request", "Request"), ("notes", "Notes"))
UNIT_FACTORS = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "in": 0.0254}


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
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1000 or unit == "TB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1000
    return f"{n} B"


def fmt_time(seconds) -> str:
    if seconds is None:
        return "-"
    m, s = divmod(float(seconds), 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:06.3f}"


def data_uri(img: np.ndarray, max_side: int = 900, quality: int = 90) -> str:
    h, w = img.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, quality])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""


def param_value(param, value) -> str:
    """A parameter value as a reader would write it."""
    if param.kind == "bool":
        return "Yes" if value else "No"
    if param.kind == "choice":
        return dict(param.choices).get(value, str(value))
    if param.kind == "points":
        if not value:
            return "not set"
        names = param.labels or tuple(f"point {i + 1}" for i in range(len(value)))
        return "; ".join(f"{n} ({x:.1f}, {y:.1f})" for n, (x, y) in zip(names, value))
    if param.kind == "int":
        return f"{int(value)}{' ' + param.unit if param.unit else ''}"
    v = f"{value:.4f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    return f"{v}{'' if not param.unit else (param.unit if param.unit in ('%', '°', '×') else ' ' + param.unit)}"


# ---------------------------------------------------------------- measurements
def _dist(a, b) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def compute_measurements(raw: dict | None, fps: float) -> dict:
    """Recompute every measurement from its points, so the report never relies on numbers typed by the page."""
    raw = raw or {}
    out = {"calibration": None, "items": []}
    cal = raw.get("calibration") or {}
    scale = None  # metres per pixel
    try:
        pts = cal.get("points") or []
        length, unit = float(cal.get("length") or 0), cal.get("unit", "m")
        if len(pts) == 2 and length > 0 and unit in UNIT_FACTORS and _dist(*pts) > 0:
            scale = length * UNIT_FACTORS[unit] / _dist(*pts)
            out["calibration"] = {"points": pts, "pixels": _dist(*pts), "length": length, "unit": unit,
                                  "frame": cal.get("frame"), "metres_per_pixel": scale}
    except (TypeError, ValueError):
        pass
    unit = (out["calibration"] or {}).get("unit", "m")
    for item in raw.get("items") or []:
        try:
            kind = item.get("type")
            if kind == "distance":
                a, b = item["points"]
                px = _dist(a, b)
                entry = {"type": "distance", "label": str(item.get("label") or ""), "points": [a, b], "frame": item.get("frame"),
                         "pixels": px}
                if scale:
                    entry["value"] = px * scale / UNIT_FACTORS[unit]
                    entry["unit"] = unit
                out["items"].append(entry)
            elif kind == "speed":
                a, b = item["a"], item["b"]
                px = _dist(a["point"], b["point"])
                frames = abs(int(b["frame"]) - int(a["frame"]))
                entry = {"type": "speed", "label": str(item.get("label") or ""), "a": a, "b": b, "pixels": px, "frames": frames}
                if fps and frames:
                    entry["seconds"] = frames / fps
                if scale:
                    entry["metres"] = px * scale
                if scale and fps and frames:
                    mps = entry["metres"] / entry["seconds"]
                    entry.update(kmh=mps * 3.6, mph=mps * 2.236936)
                out["items"].append(entry)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------- context
def build_context(pipeline: Pipeline, steps: list[Step], index: int, case: dict | None, measurements: dict | None,
                  exports: list[dict], with_images: bool = True) -> dict:
    src = pipeline.source
    src.wait_hashes(timeout=600)
    final, results = pipeline.render(steps, index, stages=with_images)
    original = src.frame(index)
    stages = []
    for r in results:
        f = r.step.filter
        stage = {
            "position": r.step.index + 1, "id": f.id, "name": f.name, "category": f.category, "enabled": r.step.enabled,
            "params": [{"key": p.key, "label": p.label, "value": r.step.params[p.key], "display": param_value(p, r.step.params[p.key]),
                        "default": p.default, "changed": r.step.params[p.key] != p.default, "help": p.help}
                       for p in f.params],
            "notes": r.notes, "error": r.error, "output_size": list(r.shape) if r.shape else None,
            "ms": round(r.ms, 1), "summary": f.summary, "use": f.use, "method": f.method, "caveats": f.caveats,
            "temporal": f.temporal,
        }
        if with_images and r.image is not None:
            stage["thumbnail"] = data_uri(r.image, 520, 85)
        stages.append(stage)
    ctx = {
        "tool": APP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {"platform": platform.platform(), "node": platform.node()},
        "environment": {"python": sys.version.split()[0], "opencv": cv2.__version__, "numpy": np.__version__},
        "case": {k: str((case or {}).get(k, "")) for k, _ in CASE_LABELS},
        "source": src.info(),
        "frame": {"index": index, "time": src.time_of(index), "count": src.count},
        "result_size": [final.shape[1], final.shape[0]],
        "stages": stages,
        "chain": chain_json(steps),
        "measurements": compute_measurements(measurements, src.fps),
        "exports": exports,
        "catalogue": catalogue(),
    }
    if with_images:
        ctx["images"] = {"original": data_uri(original), "result": data_uri(final)}
    return ctx


# ---------------------------------------------------------------- HTML
STYLE = """
  @font-face { font-family: "Manrope"; src: url("%(font)s") format("woff2"); font-weight: 200 800; font-display: swap; }
  :root {
    --bg: #1c2023; --panel: #262c30; --panel-2: #30373c;
    --border: rgba(255, 255, 255, 0.08); --border-strong: rgba(255, 255, 255, 0.14);
    --text: #eef1f2; --text-dim: #a9b3b8; --text-faint: #7f8b91;
    --accent: #e8793b; --accent-2: #f39a63; --good: #3ecf8e; --warn: #f5b942; --bad: #f05a5a;
    --font-body: "Manrope", "Segoe UI", system-ui, -apple-system, sans-serif;
    --font-mono: Consolas, "Cascadia Mono", "SFMono-Regular", monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; color: var(--text); font-family: var(--font-body); font-size: 15px; line-height: 1.6;
         background: radial-gradient(90rem 40rem at 50%% -12rem, rgba(154, 170, 178, 0.14), transparent 70%%), var(--bg);
         -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 1040px; margin: 0 auto; padding: clamp(2rem, 1rem + 4vw, 4rem) clamp(1rem, 3vw, 2rem) 3rem; }
  h1, h2, h3, h4 { line-height: 1.15; margin: 0 0 0.5em; }
  h1 { font-size: clamp(2rem, 1.4rem + 2.4vw, 3rem); font-weight: 500; letter-spacing: -0.035em; overflow-wrap: anywhere; }
  h2 { font-size: 1.4rem; font-weight: 500; letter-spacing: -0.025em; margin-bottom: 1rem; }
  h3 { font-family: var(--font-mono); font-size: 0.72rem; font-weight: 400; color: var(--text-faint);
       text-transform: uppercase; letter-spacing: 0.08em; margin: 1.2rem 0 0.6em; }
  h4 { font-size: 1.1rem; font-weight: 600; margin: 0; }
  p { margin: 0 0 0.8em; }
  .eyebrow { font-family: var(--font-mono); font-size: 0.78rem; color: var(--accent-2); text-transform: uppercase;
             letter-spacing: 0.08em; margin: 0 0 1rem; }
  .meta-line { font-family: var(--font-mono); color: var(--text-faint); font-size: 0.78rem; margin-bottom: 2rem; }
  .muted { color: var(--text-faint); font-size: 0.85rem; }
  .mono { font-family: var(--font-mono); font-size: 0.82rem; overflow-wrap: anywhere; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 28px;
           padding: clamp(1.3rem, 1rem + 1.2vw, 2rem); margin-bottom: 14px; break-inside: avoid-page; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
  .card { background: var(--panel-2); border: 1px solid var(--border); border-radius: 18px; padding: 1rem 1.2rem; }
  .card-label { font-family: var(--font-mono); color: var(--accent-2); font-size: 0.72rem; text-transform: uppercase;
                letter-spacing: 0.08em; margin-bottom: 0.5rem; }
  .card-value { font-size: 1.2rem; font-weight: 500; line-height: 1.2; overflow-wrap: anywhere; }
  .two { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 2rem; }
  figure { margin: 0; }
  figure img { width: 100%%; height: auto; border-radius: 12px; background: #000; display: block; }
  figcaption { font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-faint); margin-top: 0.4rem; }
  table { width: 100%%; border-collapse: collapse; font-size: 0.88rem; }
  th { text-align: left; font-family: var(--font-mono); font-size: 0.7rem; font-weight: 400; color: var(--text-faint);
       text-transform: uppercase; letter-spacing: 0.06em; padding: 0 0.6em 0.5em 0; border-bottom: 1px solid var(--border-strong); }
  td { padding: 0.4em 0.6em 0.4em 0; border-bottom: 1px solid var(--border); overflow-wrap: anywhere; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  table.kv td:first-child { color: var(--text-faint); font-family: var(--font-mono); font-size: 0.78rem; width: 34%%; }
  .changed { color: var(--accent-2); }
  .step { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 1.4rem; }
  .step-head { display: flex; align-items: baseline; gap: 0.8rem; flex-wrap: wrap; margin-bottom: 0.8rem; }
  .step-no { font-family: var(--font-mono); color: var(--on-accent, #16191b); background: var(--accent); border-radius: 999px;
             padding: 0.05rem 0.6rem; font-size: 0.78rem; font-weight: 700; }
  .tag { font-family: var(--font-mono); font-size: 0.7rem; color: var(--text-faint); border: 1px solid var(--border-strong);
         border-radius: 999px; padding: 0.05rem 0.55rem; }
  .tag.off { color: var(--warn); border-color: rgba(245, 185, 66, 0.45); }
  .tag.err { color: var(--bad); border-color: rgba(240, 90, 90, 0.45); }
  dl.explain { margin: 0; display: grid; grid-template-columns: 8.5rem minmax(0, 1fr); gap: 0.35rem 1rem; font-size: 0.9rem; }
  dl.explain dt { font-family: var(--font-mono); font-size: 0.72rem; color: var(--accent-2); text-transform: uppercase;
                  letter-spacing: 0.06em; padding-top: 0.2rem; }
  dl.explain dd { margin: 0; color: var(--text-dim); }
  .notes { font-family: var(--font-mono); font-size: 0.78rem; color: var(--good); margin-top: 0.6rem; }
  .statement { border-left: 3px solid var(--good); padding: 0.2rem 0 0.2rem 1rem; color: var(--text-dim); }
  .cat { margin-top: 1.6rem; }
  .cat:first-of-type { margin-top: 0; }
  .ref { padding: 1rem 0; border-bottom: 1px solid var(--border); break-inside: avoid-page; }
  .ref:last-child { border-bottom: none; }
  .ref .used { color: var(--good); font-family: var(--font-mono); font-size: 0.72rem; margin-left: 0.6rem; }
  .params-list { font-size: 0.82rem; color: var(--text-dim); margin: 0.5rem 0 0; padding-left: 1.1rem; }
  pre { background: var(--panel-2); border-radius: 12px; padding: 1rem; font-size: 0.75rem; overflow-x: auto; white-space: pre-wrap;
        word-break: break-all; font-family: var(--font-mono); color: var(--text-dim); }
  .site-footer { color: var(--text-faint); font-size: 0.85rem; padding-top: 1.4rem; border-top: 1px solid var(--border); margin-top: 2rem; }
  @media (max-width: 760px) { .two, .step { grid-template-columns: 1fr; } dl.explain { grid-template-columns: 1fr; } }
  @media print {
    body { background: white; color: black; font-size: 11pt; }
    .panel, .card, pre { background: white; border-color: #ccc; }
    td:first-child, th, .card-label, .meta-line, .muted, h3, dl.explain dd { color: #444; }
    dl.explain dt, .changed, .eyebrow { color: #a4470f; }
  }
"""


def _rows(pairs) -> str:
    return "".join(f"<tr><td>{_esc(k)}</td><td>{v if isinstance(v, Markup) else _esc(v)}</td></tr>"
                   for k, v in pairs if v not in (None, "", []))


class Markup(str):
    """Already-escaped HTML for _rows."""


def _source_html(ctx: dict) -> str:
    s = ctx["source"]
    kind = {"image": "Still image", "sequence": f"Image sequence ({s['file_count']} files)", "video": "Video"}[s["kind"]]
    d = s.get("details") or {}
    rows = [("Type", kind), ("Resolution", f"{s['width']} × {s['height']} px")]
    if s["kind"] == "video":
        rows += [("Codec", d.get("codec")), ("Frame rate", f"{s['fps']:.3f} fps" if s["fps"] else "unknown"),
                 ("Frames", f"{s['count']:,}"), ("Duration", fmt_time(s.get("duration")) if s.get("duration") else None),
                 ("Decoder", d.get("backend"))]
    else:
        rows += [("Bit depth", f"{d.get('bit_depth')}-bit, {d.get('channels')} channel(s)" if d.get("bit_depth") else None)]
    files = s["files"]
    file_rows = "".join(
        f"<tr><td class='mono'>{_esc(f['path'])}</td><td>{fmt_bytes(f['size'])}</td><td class='mono'>{_esc(f.get('modified'))}</td>"
        f"<td class='mono'>MD5 {_esc(f.get('md5', 'not computed'))}<br>SHA-256 {_esc(f.get('sha256', 'not computed'))}</td></tr>"
        for f in files
    )
    more = f"<p class='muted'>…and {s['file_count'] - len(files)} more files (see the JSON report).</p>" if s["file_count"] > len(files) else ""
    notes = "".join(f"<li>{_esc(n)}</li>" for n in s.get("notes", []))
    return f"""
  <div class="panel">
    <h2>Source evidence</h2>
    <p class="statement">The source was opened read-only. All processing was applied to a decoded copy in memory; the
      original file was not modified. The hashes below were computed by {APP_NAME} when the source was opened.</p>
    <table class="kv">{_rows(rows)}</table>
    <h3>File{'s' if len(files) > 1 else ''}</h3>
    <table><tr><th>Path</th><th>Size</th><th>Modified (UTC)</th><th>Hashes</th></tr>{file_rows}</table>{more}
    {f"<h3>Notes on decoding</h3><ul class='muted'>{notes}</ul>" if notes else ""}
  </div>"""


def _stage_html(st: dict) -> str:
    tags = [f"<span class='tag'>{_esc(dict(CATEGORIES)[st['category']])}</span>"]
    if st["temporal"]:
        tags.append("<span class='tag'>multi-frame</span>")
    if not st["enabled"]:
        tags.append("<span class='tag off'>disabled: not applied</span>")
    if st["error"]:
        tags.append("<span class='tag err'>failed</span>")
    params = "".join(
        f"<tr><td>{_esc(p['label'])}</td><td class='{'changed' if p['changed'] else ''}'>{_esc(p['display'])}</td></tr>"
        for p in st["params"]
    ) or "<tr><td colspan='2' class='muted'>No parameters</td></tr>"
    notes = "; ".join(st["notes"])
    out_size = f"Output {st['output_size'][0]} × {st['output_size'][1]} px" if st.get("output_size") else ""
    thumb = (f"<figure><img src='{st['thumbnail']}' alt='After step {st['position']}'/><figcaption>After step {st['position']}"
             f"{' · ' + out_size if out_size else ''}</figcaption></figure>") if st.get("thumbnail") else \
        f"<p class='muted'>{'Not applied.' if not st['enabled'] else 'No image (the chain stopped before this step).'}</p>"
    return f"""
  <div class="panel">
    <div class="step-head"><span class="step-no">{st['position']}</span><h4>{_esc(st['name'])}</h4>{''.join(tags)}</div>
    <div class="step">
      <div>
        <dl class="explain">
          <dt>What it does</dt><dd>{_esc(st['summary'])}</dd>
          <dt>Why it's used</dt><dd>{_esc(st['use'])}</dd>
          <dt>How it works</dt><dd>{_esc(st['method'])}</dd>
          <dt>Caveats</dt><dd>{_esc(st['caveats'])}</dd>
        </dl>
        <h3>Parameters <span class="muted" style="text-transform:none;letter-spacing:0">(changed from default in orange)</span></h3>
        <table class="kv">{params}</table>
        {f"<div class='notes'>Result: {_esc(notes)}</div>" if notes else ""}
        {f"<div class='notes' style='color:var(--bad)'>Error: {_esc(st['error'])}</div>" if st['error'] else ""}
      </div>
      <div>{thumb}</div>
    </div>
  </div>"""


def _measurements_html(m: dict) -> str:
    if not m["calibration"] and not m["items"]:
        return ""
    cal = m["calibration"]
    cal_html = (f"<p>Scale set from a reference of <strong>{cal['length']:g} {_esc(cal['unit'])}</strong> spanning "
                f"{cal['pixels']:.1f} px (frame {_esc(cal.get('frame'))}): {cal['metres_per_pixel'] * 1000:.3f} mm per pixel.</p>"
                if cal else "<p class='muted'>No scale reference was set: distances are in pixels only.</p>")
    rows = []
    for i, it in enumerate(m["items"], 1):
        if it["type"] == "distance":
            val = f"{it['value']:.3f} {it['unit']}" if "value" in it else "-"
            rows.append(f"<tr><td>{i}</td><td>Distance{' · ' + _esc(it['label']) if it['label'] else ''}</td>"
                        f"<td>frame {_esc(it.get('frame'))}</td><td>{it['pixels']:.1f} px</td><td>{_esc(val)}</td></tr>")
        else:
            val = f"{it['kmh']:.1f} km/h · {it['mph']:.1f} mph" if "kmh" in it else "needs a scale and a frame rate"
            span = f"frames {it['a']['frame']}→{it['b']['frame']}" + (f" ({it['seconds']:.3f} s)" if "seconds" in it else "")
            dist = f"{it['pixels']:.1f} px" + (f" = {it['metres']:.2f} m" if "metres" in it else "")
            rows.append(f"<tr><td>{i}</td><td>Speed{' · ' + _esc(it['label']) if it['label'] else ''}</td>"
                        f"<td>{_esc(span)}</td><td>{_esc(dist)}</td><td>{_esc(val)}</td></tr>")
    return f"""
  <div class="panel">
    <h2>Measurements</h2>
    {cal_html}
    {f"<table><tr><th>#</th><th>Type</th><th>When</th><th>Image distance</th><th>Result</th></tr>{''.join(rows)}</table>" if rows else ""}
    <p class="muted" style="margin-top:1rem">Measurements are made on the processed image, in its pixel coordinates.
      They are only valid in the plane of the scale reference (rectify that plane with perspective and lens
      correction first), and their accuracy is limited by resolution, compression and point placement. Speeds use
      the container frame rate, which may differ from the true capture rate on some DVRs. Verify against
      the system's timing (e.g. an on-screen clock) before relying on them.</p>
  </div>"""


def _exports_html(exports: list[dict]) -> str:
    if not exports:
        return ""
    rows = []
    for e in exports:
        h = e["hashes"].get(e["path"], {})
        what = {"image": f"Frame {e.get('frame')}", "video": f"Frames {e.get('start')}–{e.get('end')}",
                "frames": f"Frames {e.get('start')}–{e.get('end')} ({e.get('file_count')} PNG files; hash is of the SHA256SUMS manifest)"}[e["kind"]]
        rows.append(f"<tr><td class='mono'>{_esc(e.get('folder') or e['path'])}</td><td>{_esc(what)}<br><span class='muted'>"
                    f"{_esc(e.get('format'))} · {_esc(e.get('width'))}×{_esc(e.get('height'))}{' · lossy' if e.get('lossy') else ''}</span></td>"
                    f"<td class='mono'>MD5 {_esc(h.get('md5'))}<br>SHA-256 {_esc(h.get('sha256'))}</td><td class='mono'>{_esc(e['created'])}</td></tr>")
    return f"""
  <div class="panel">
    <h2>Exported files</h2>
    <table><tr><th>File</th><th>Content</th><th>Hashes</th><th>Written (UTC)</th></tr>{''.join(rows)}</table>
    <p class="muted" style="margin-top:1rem">Each export used the chain that was active when it was written; the JSON report
      records the exact chain per export.</p>
  </div>"""


def _catalogue_html(cat: list[dict], used: set[str]) -> str:
    parts = []
    for cid, cname in CATEGORIES:
        items = [f for f in cat if f["category"] == cid]
        refs = []
        for f in items:
            params = "".join(
                f"<li><strong>{_esc(p['label'])}</strong>{' (' + _esc(p['unit']) + ')' if p['unit'] else ''}"
                f"{': ' + _esc(p['help']) if p['help'] else ''}</li>" for p in f["params"])
            refs.append(f"""
      <div class="ref" id="ref-{_esc(f['id'])}">
        <h4>{_esc(f['name'])}{"<span class='used'>● used in this chain</span>" if f['id'] in used else ""}</h4>
        <dl class="explain" style="margin-top:0.6rem">
          <dt>What it does</dt><dd>{_esc(f['summary'])}</dd>
          <dt>Why it's used</dt><dd>{_esc(f['use'])}</dd>
          <dt>How it works</dt><dd>{_esc(f['method'])}</dd>
          <dt>Caveats</dt><dd>{_esc(f['caveats'])}</dd>
        </dl>
        {f"<ul class='params-list'>{params}</ul>" if params else ""}
      </div>""")
        parts.append(f"<div class='cat'><h3>{_esc(cname)}</h3>{''.join(refs)}</div>")
    return "".join(parts)


def _page(title: str, eyebrow: str, meta: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{_esc(title)}</title>
<style>{STYLE % {"font": _font_src()}}</style>
</head>
<body>
<div class="wrap">
  <p class="eyebrow">{_esc(eyebrow)}</p>
  <h1>{_esc(title)}</h1>
  <div class="meta-line">{meta}</div>
  {body}
  <div class="site-footer">{APP_NAME} · a Harry Smallwood tool</div>
</div>
</body>
</html>
"""


def generate_report_html(ctx: dict) -> str:
    src, frame = ctx["source"], ctx["frame"]
    generated = datetime.fromisoformat(ctx["generated_at"]).strftime("%Y-%m-%d %H:%M UTC")
    applied = [s for s in ctx["stages"] if s["enabled"]]
    case_rows = _rows((label, ctx["case"].get(key)) for key, label in CASE_LABELS)
    when = f"Frame {frame['index']} of {frame['count']}" + (f" · {fmt_time(frame['time'])}" if frame.get("time") is not None else "") \
        if src["kind"] != "image" else "Still image"
    imgs = ctx.get("images") or {}
    summary_steps = "".join(f"<li>{_esc(s['name'])}{' — ' + _esc('; '.join(s['notes'])) if s['notes'] else ''}</li>" for s in applied)
    used = {s["id"] for s in applied}
    body = f"""
  <div class="panel">
    <h2>Summary</h2>
    <div class="cards">
      <div class="card"><div class="card-label">Source</div><div class="card-value">{_esc(src['name'])}</div></div>
      <div class="card"><div class="card-label">Shown</div><div class="card-value">{_esc(when)}</div></div>
      <div class="card"><div class="card-label">Steps applied</div><div class="card-value">{len(applied)}</div></div>
      <div class="card"><div class="card-label">Result size</div><div class="card-value">{ctx['result_size'][0]} × {ctx['result_size'][1]}</div></div>
    </div>
    {f"<h3>Case</h3><table class='kv'>{case_rows}</table>" if case_rows else ""}
    {f"<h3>Processing, in order</h3><ol>{summary_steps}</ol>" if summary_steps else "<p class='muted' style='margin-top:1rem'>No filters were applied.</p>"}
  </div>

  <div class="panel">
    <h2>Before and after</h2>
    <div class="two">
      <figure><img src="{imgs.get('original', '')}" alt="Original" /><figcaption>Original · {_esc(when)} · {src['width']} × {src['height']} px</figcaption></figure>
      <figure><img src="{imgs.get('result', '')}" alt="Processed" /><figcaption>Processed · {ctx['result_size'][0]} × {ctx['result_size'][1]} px</figcaption></figure>
    </div>
    <p class="muted" style="margin-top:1rem">Images in this report are reduced-size JPEG previews for illustration. Use the exported
      files for examination.</p>
  </div>

  {_source_html(ctx)}

  <h2 style="margin:2.2rem 0 1rem">Processing chain</h2>
  {''.join(_stage_html(s) for s in ctx['stages']) or "<div class='panel'><p class='muted'>The chain is empty.</p></div>"}

  {_measurements_html(ctx['measurements'])}
  {_exports_html(ctx['exports'])}

  <div class="panel">
    <h2>Reproducibility</h2>
    <p>Loading the source in {_esc(ctx['tool'])} and applying the chain below (also saved by <em>Save project</em>)
      reproduces the result exactly on the same software versions. Filters are deterministic.</p>
    <table class="kv">{_rows([("Software", ctx['tool']), ("Python", ctx['environment']['python']), ("OpenCV", ctx['environment']['opencv']),
                             ("NumPy", ctx['environment']['numpy']), ("Computer", ctx['host']['node']), ("Platform", ctx['host']['platform'])])}</table>
    <h3>Chain (JSON)</h3>
    <pre>{_esc(json.dumps(ctx['chain'], indent=1))}</pre>
  </div>

  <div class="panel">
    <h2>Appendix: filter reference</h2>
    <p class="muted">Every filter available in {_esc(ctx['tool'])}, whether used here or not, with what it does and its
      limitations. Filters used in this chain are marked.</p>
    {_catalogue_html(ctx['catalogue'], used)}
  </div>"""
    meta = f"Generated {_esc(generated)} · {_esc(ctx['tool'])} · {_esc(ctx['host']['node'])}"
    return _page(src["name"], f"{APP_NAME} · enhancement report", meta, body)


def generate_reference_html() -> str:
    """The filter reference on its own, e.g. for a lab's procedures folder."""
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = f"""
  <div class="panel">
    <p class="muted">{len(FILTERS)} filters. Each entry gives what the filter does, when an examiner would use it,
      how it works (precisely enough to reproduce), and its caveats.</p>
    {_catalogue_html(catalogue(), set())}
  </div>"""
    return _page("Filter reference", f"{APP_NAME} · filter reference", f"Generated {_esc(generated)} · {_esc(APP_VERSION)}", body)


def generate_report_json(ctx: dict) -> dict:
    out = {k: v for k, v in ctx.items() if k != "images"}
    out["stages"] = [{k: v for k, v in s.items() if k != "thumbnail"} for s in ctx["stages"]]
    return out
