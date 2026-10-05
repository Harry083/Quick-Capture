"""Launch Quick Query as a desktop application (a native window, no local web server or port)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import webview

from backend.api import Api

# PyInstaller unpacks bundled data to sys._MEIPASS; from source it sits next to this file.
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FRONTEND_DIR = BASE_DIR / "frontend"
ICON_PATH = BASE_DIR / ("quickquery.ico" if os.name == "nt" else "quickquery.png")


def main() -> None:
    api = Api()
    window = webview.create_window(
        "Quick Query",
        url=(FRONTEND_DIR / "index.html").as_uri(),  # file://, served by nothing
        js_api=api,
        width=1440,
        height=940,
        min_size=(1000, 680),
        background_color="#1c2023",  # matches --bg in styles.css, so there's no white flash on open
        text_select=True,
    )
    api._attach(window)
    # A database path on the command line (or a file dropped on the .exe) opens straight away.
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        window.events.loaded += lambda: window.evaluate_js(
            f"window.openFromArgs && window.openFromArgs({json.dumps(args[0])})")
    webview.start(
        http_server=False,
        debug="--debug" in sys.argv,
        icon=str(ICON_PATH) if ICON_PATH.exists() else None,
    )


if __name__ == "__main__":
    main()
