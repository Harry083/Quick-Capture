# PyInstaller build for the Clarity desktop app:  python -m PyInstaller --clean Clarity.spec
# Produces a single file, dist/Clarity.exe (dist/Clarity on Linux/macOS), with the app icon.
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["app.py"],
    pathex=[],
    datas=[("frontend", "frontend"), ("clarity.ico", "."), ("clarity.png", ".")],
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
    name="Clarity",
    console=False,  # windowed app, no console
    icon="clarity.ico",  # .exe, taskbar and title-bar icon
    runtime_tmpdir=None,
)
