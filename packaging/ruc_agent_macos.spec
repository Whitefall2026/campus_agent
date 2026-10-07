# -*- mode: python ; coding: utf-8 -*-
"""Native macOS bundle; no Windows WeChat packages or user data."""
import os
import platform
import sys

from PIL import Image

if sys.platform != "darwin":
    raise RuntimeError("Build the macOS app on a macOS host")

project_root = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, project_root)
from app.version import __version__

icon_dir = os.environ.get("RUC_AGENT_BUILD_DIR") or os.path.join(project_root, "build", "macos-icon")
os.makedirs(icon_dir, exist_ok=True)
icon_file = os.path.join(icon_dir, "app_icon.icns")
logo = os.path.join(project_root, "assets", "brand", "ruc-logo.png")
with Image.open(logo) as image:
    if image.width != image.height:
        raise ValueError("App icon must be square")
    image.convert("RGBA").resize((1024, 1024), Image.Resampling.LANCZOS).save(icon_file, format="ICNS")

a = Analysis(
    [os.path.join(project_root, "desktop.py")],
    pathex=[project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, "static"), "static"),
        (logo, "assets/brand"),
    ],
    # Select only Cocoa: collecting every pystray backend would pull Linux
    # or Windows modules into the macOS bundle.
    hiddenimports=["pystray._darwin", "AppKit", "Foundation", "objc"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "wechatauto", "uiautomation", "wx", "win32api", "win32con",
              "win32gui", "win32com", "pystray._win32", "pystray._xorg",
              "pystray._gtk", "pystray._appindicator"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="RUCAgent", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=platform.machine(), codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="RUCAgent")
app = BUNDLE(
    coll, name="RUC Agent.app", icon=icon_file,
    bundle_identifier="org.rucagent.desktop",
    version=__version__,
    info_plist={
        "CFBundleDisplayName": "RUC Agent",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
