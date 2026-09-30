# PyInstaller build for the Quick Capture desktop app:  python -m PyInstaller --clean QuickCapture.spec
# Produces a single file, dist/QuickCapture.exe (dist/QuickCapture on Linux/macOS), with the app icon.
# On Windows the .exe carries a manifest that requests Administrator, so UAC prompts on launch.
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["app.py"],
    pathex=[],
    datas=[("frontend", "frontend"), ("quickcapture.ico", "."), ("quickcapture.png", ".")],
    hiddenimports=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="QuickCapture",
    console=False,  # windowed app, no console
    uac_admin=True,  # Windows: always run elevated (raw device access needs it)
    icon="quickcapture.ico",  # .exe, taskbar and title-bar icon
    runtime_tmpdir=None,
)
