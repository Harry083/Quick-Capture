# PyInstaller build for the Deep Table desktop app:  python -m PyInstaller --clean DeepTable.spec
# Produces a single file, dist/DeepTable.exe (dist/DeepTable on Linux/macOS), with the app icon.
# Unlike Quick Capture it needs no Administrator rights: it only reads files the user can already open.
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["app.py"],
    pathex=[],
    datas=[("frontend", "frontend"), ("deeptable.ico", "."), ("deeptable.png", ".")],
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
    name="DeepTable",
    console=False,  # windowed app, no console
    icon="deeptable.ico",  # .exe, taskbar and title-bar icon
    runtime_tmpdir=None,
)
