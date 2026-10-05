# PyInstaller build for the Quick Query desktop app:  python -m PyInstaller --clean QuickQuery.spec
# Produces a single file, dist/QuickQuery.exe (dist/QuickQuery on Linux/macOS), with the app icon.
# Unlike Quick Capture it needs no Administrator rights: it only reads files the user can already open.
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["app.py"],
    pathex=[],
    datas=[("frontend", "frontend"), ("quickquery.ico", "."), ("quickquery.png", ".")],
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
    name="QuickQuery",
    console=False,  # windowed app, no console
    icon="quickquery.ico",  # .exe, taskbar and title-bar icon
    runtime_tmpdir=None,
)
