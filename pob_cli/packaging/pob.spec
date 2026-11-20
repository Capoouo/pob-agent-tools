# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for standalone Windows x64 `pob` (--onedir for fast CLI startup and daemon persistence)."""

import os
from PyInstaller.utils.hooks import collect_data_files

spec_dir = SPECPATH
src_dir = os.path.abspath(os.path.join(spec_dir, "..", "src"))
entry_script = os.path.join(spec_dir, "pyinstaller_entry.py")

datas = collect_data_files("certifi") + [
    (os.path.join(src_dir, "pob_cli", "data", "bench_costs.json"), os.path.join("pob_cli", "data")),
]
hiddenimports = [
    "socksio",
    "pob_cli.daemon",
    "pob_cli.cli_trade",
    "pob_cli.cli_timeless",
]

a = Analysis(
    [entry_script],
    pathex=[src_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pob",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pob",
)
