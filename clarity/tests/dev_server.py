"""Development/test harness: serve the frontend in an ordinary browser, with the real backend behind it.

    python tests/dev_server.py [--port 8765]

The desktop app never opens a port; this is only for UI development and the browser tests. It injects a
stand-in for pywebview's bridge, so window.pywebview.api.<method>(...) becomes a POST to /api/<method> on
127.0.0.1. Native file dialogs are replaced by a queue of answers (POST /__dialog with a JSON list), and
report windows are written to /__window/<n>.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import api as api_mod  # noqa: E402

FRONTEND = ROOT / "frontend"
SHIM = """<script>
window.pywebview = { api: new Proxy({}, { get: (_, method) => async (...args) => {
  const r = await fetch('/api/' + method, { method: 'POST', body: JSON.stringify(args) });
  return r.json();
} }) };
window.dispatchEvent(new Event('pywebviewready'));
</script>"""


class FakeWindow:
    def __init__(self) -> None:
        self.answers: list = []

    def create_file_dialog(self, *args, **kwargs):
        return self.answers.pop(0) if self.answers else None


class FakeWebview:
    """Stands in for the pywebview module: report windows become pages under /__window/."""

    def __init__(self) -> None:
        self.windows: list[str] = []

    def create_window(self, title, html="", **kwargs):
        self.windows.append(html)


API = api_mod.Api()
WINDOW = FakeWindow()
API._attach(WINDOW)
api_mod.webview = WEBVIEW = FakeWebview()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path.startswith("/__window/"):
            i = int(path.rsplit("/", 1)[1])
            return self._send(WEBVIEW.windows[i].encode(), "text/html; charset=utf-8")
        if path == "/__windows":
            return self._send(json.dumps(len(WEBVIEW.windows)).encode(), "application/json")
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        file = (FRONTEND / rel).resolve()
        if FRONTEND not in file.parents or not file.is_file():
            return self._send(b"not found", "text/plain", 404)
        body = file.read_bytes()
        if rel == "index.html":
            body = body.replace(b'<script src="app.js">', SHIM.encode() + b'\n<script src="app.js">')
        self._send(body, mimetypes.guess_type(str(file))[0] or "application/octet-stream")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"null")
        if self.path == "/__dialog":
            WINDOW.answers.extend(payload)
            return self._send(b"{}", "application/json")
        method = self.path.split("/api/", 1)[-1]
        if method.startswith("_") or not hasattr(API, method):
            return self._send(json.dumps({"ok": False, "error": f"no method {method}"}).encode(), "application/json")
        result = getattr(API, method)(*(payload or []))
        self._send(json.dumps(result).encode(), "application/json")


def serve(port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    port = parser.parse_args().port
    print(f"Clarity dev server on http://127.0.0.1:{port}/  (Ctrl+C to stop)")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()
