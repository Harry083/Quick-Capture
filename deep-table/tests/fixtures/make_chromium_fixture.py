"""Regenerate chromium-stores.zip: real Local Storage, Session Storage and IndexedDB written by Chromium.

    python tests/fixtures/make_chromium_fixture.py      (needs playwright and a Chromium build)

chromium_page.html is loaded twice from http://127.0.0.1:8765: the first visit writes data (including enough
IndexedDB padding to force a flush from the log into an .ldb table), the second edits and deletes some of it.
"""
import functools
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
CHROMIUM = os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium")


def main() -> None:
    work = tempfile.mkdtemp()
    site = os.path.join(work, "site")
    os.makedirs(site)
    shutil.copy(os.path.join(HERE, "chromium_page.html"), os.path.join(site, "page.html"))
    handler = functools.partial(SimpleHTTPRequestHandler, directory=site)
    handler.log_message = lambda *a: None
    server = ThreadingHTTPServer(("127.0.0.1", 8765), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = os.path.join(work, "profile")
    with sync_playwright() as p:
        for phase in (1, 2):
            ctx = p.chromium.launch_persistent_context(profile, executable_path=CHROMIUM, headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"http://127.0.0.1:8765/page.html#{phase}")
            page.wait_for_function(f"document.title === 'done{phase}'", timeout=60000)
            page.wait_for_timeout(1500)
            ctx.close()
    server.shutdown()
    default = os.path.join(profile, "Default")
    out = os.path.join(HERE, "chromium-stores.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for top in ("Local Storage", "Session Storage", "IndexedDB"):
            for root, _dirs, files in os.walk(os.path.join(default, top)):
                for f in files:
                    if f != "LOCK":
                        full = os.path.join(root, f)
                        z.write(full, os.path.relpath(full, default))
    shutil.rmtree(work, ignore_errors=True)
    print("wrote", out)


if __name__ == "__main__":
    sys.exit(main())
