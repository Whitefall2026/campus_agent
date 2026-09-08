# -*- mode: python ; coding: utf-8 -*-
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = os.path.abspath(os.path.join(SPECPATH, ".."))
wechat_root = os.path.join(project_root, "wechatauto-replica-main")

# The WeChat library is vendored in this repository.  Add it explicitly so a
# clean build does not depend on an editable package having been installed in
# the developer's Python environment.  Its public package imports several
# implementation modules dynamically, hence collect_submodules is required.
sys.path.insert(0, wechat_root)
hiddenimports = collect_submodules("wechatauto")
hiddenimports += collect_submodules("pystray")
datas = [
    (os.path.join(project_root, "static"), "static"),
    (os.path.join(project_root, "packaging", "app_icon.ico"), "packaging"),
]
datas += collect_data_files("wechatauto", include_py_files=False)
datas += collect_data_files("uiautomation", include_py_files=False)

a = Analysis(
    [os.path.join(project_root, "desktop.py")],
    pathex=[project_root, wechat_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RUCAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, "packaging", "app_icon.ico"),
    version=os.path.join(project_root, "packaging", "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RUCAgent",
)
