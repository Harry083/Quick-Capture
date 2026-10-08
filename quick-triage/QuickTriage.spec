# PyInstaller build for the Quick Triage desktop app:  python -m PyInstaller --clean QuickTriage.spec
# Produces a single file, dist/QuickTriage.exe (dist/QuickTriage on Linux/macOS), with the app icon.
# Unlike Quick Capture it doesn't ask for Administrator: it only reads image files.
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["app.py"],
    pathex=[],
    datas=[("frontend", "frontend"), ("quicktriage.ico", "."), ("quicktriage.png", ".")],
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
    name="QuickTriage",
    console=False,  # windowed app, no console
    icon="quicktriage.ico",  # .exe, taskbar and title-bar icon
    runtime_tmpdir=None,
)
