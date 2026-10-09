"""Launch Clarity as a desktop application (a native window, no local web server or port)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import webview

from backend.api import Api

# PyInstaller unpacks bundled data to sys._MEIPASS; from source it sits next to this file.
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FRONTEND_DIR = BASE_DIR / "frontend"
# Title-bar/taskbar icon. Windows needs the .ico (pywebview loads it as a Windows icon); GTK/Qt take the PNG.
ICON_PATH = BASE_DIR / ("clarity.ico" if os.name == "nt" else "clarity.png")


def main() -> None:
    api = Api()
    window = webview.create_window(
        "Clarity",
        url=(FRONTEND_DIR / "index.html").as_uri(),  # file://, served by nothing
        js_api=api,
        width=1440,
        height=940,
        min_size=(1000, 680),
        background_color="#1c2023",  # matches --bg in styles.css, so there's no white flash on open
        text_select=True,
    )
    api._attach(window)
    webview.start(
        http_server=False,
        debug="--debug" in sys.argv,
        icon=str(ICON_PATH) if ICON_PATH.exists() else None,
    )


if __name__ == "__main__":
    main()
