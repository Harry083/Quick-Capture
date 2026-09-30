"""Launch Quick Capture as a desktop application (a native window, no local web server or port)."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path

import webview

from backend import devices
from backend.api import Api

# PyInstaller unpacks bundled data to sys._MEIPASS; from source it sits next to this file.
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FRONTEND_DIR = BASE_DIR / "frontend"
# Title-bar/taskbar icon. Windows needs the .ico (pywebview loads it as a Windows icon); GTK/Qt take the PNG.
ICON_PATH = BASE_DIR / ("quickcapture.ico" if os.name == "nt" else "quickcapture.png")


def relaunch_elevated() -> bool:
    """Windows only: re-run this program through the UAC prompt. Returns True if an elevated copy started.

    Raw device access needs Administrator. The packaged .exe asks for it in its manifest already, so this
    mostly matters when running from source (python app.py)."""
    if os.name != "nt" or devices.is_admin() or "--no-elevate" in sys.argv:
        return False
    import ctypes

    if getattr(sys, "frozen", False):
        exe, args = sys.executable, sys.argv[1:]
    else:
        # pythonw.exe avoids leaving a console window open behind the app
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe, args = str(pythonw if pythonw.exists() else sys.executable), [str(Path(__file__).resolve()), *sys.argv[1:]]
    params = subprocess.list2cmdline([*args, "--no-elevate"])
    # > 32 means the elevated process started; the user declining UAC returns an error code instead.
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, str(BASE_DIR), 1) > 32


def main() -> None:
    if relaunch_elevated():
        return  # the elevated copy takes over (if the user declines, carry on and show the admin banner)

    # Imaging jobs are asyncio-based (backend/jobs.py); give them their own event loop on a background thread.
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="jobs-loop", daemon=True).start()

    api = Api(loop)
    window = webview.create_window(
        "Quick Capture",
        url=(FRONTEND_DIR / "index.html").as_uri(),  # file://, served by nothing
        js_api=api,
        width=1280,
        height=900,
        min_size=(900, 640),
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
