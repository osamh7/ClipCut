# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Video Cutter.

Bundles:
  - customtkinter theme/assets data
  - libmpv-2.dll from mpv.net
  - All Python source modules
"""

import os
import customtkinter

block_cipher = None

# Paths
ctk_path = os.path.dirname(customtkinter.__file__)
mpv_dll = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Programs", "mpv.net", "libmpv-2.dll",
)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[(mpv_dll, ".")],
    datas=[(ctk_path, "customtkinter")],
    hiddenimports=[
        "customtkinter",
        "mpv",
        "requests",
        "PIL",
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ClipCut",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,          # No console window
    icon="app_icon.ico",
)
