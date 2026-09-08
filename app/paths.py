"""应用资源与用户数据路径。

源码运行时仍使用仓库内的 ``data/``；PyInstaller 安装版把只读静态资源
放在程序包内，把可写数据放到当前用户的 LocalAppData。这样应用安装在
受保护目录后，日程、配置和 API Key 仍能正常保存，升级时也不会被覆盖。
"""
from __future__ import annotations

import os
import sys

APP_DIR_NAME = "RUC Agent"

# 源码根目录；冻结后静态文件由 PyInstaller 解包/复制到 _MEIPASS。
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = (
    os.path.abspath(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    if FROZEN
    else SOURCE_ROOT
)

# 保留 ROOT 供可能的外部调用使用；其语义是“只读应用资源根目录”。
ROOT = RESOURCE_ROOT


def _default_data_dir() -> str:
    if not FROZEN:
        return os.path.join(SOURCE_ROOT, "data")
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        local = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, APP_DIR_NAME, "data")


# 环境变量主要用于自动化测试和便携部署；默认安装版不依赖它。
DATA_DIR = os.path.abspath(
    os.environ.get("RUC_AGENT_DATA_DIR") or _default_data_dir()
)
STATIC_DIR = os.path.join(RESOURCE_ROOT, "static")
