"""应用资源与用户数据路径。

源码运行时使用仓库内的 ``data/``；Windows 安装版把只读资源放在程序包内，
把可写数据放到当前用户的 LocalAppData，确保升级不会覆盖个人数据。
"""
from __future__ import annotations

import os
import sys

APP_DIR_NAME = "RUC Agent"
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = (
    os.path.abspath(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    if FROZEN
    else SOURCE_ROOT
)

# 兼容已有调用：ROOT 表示只读应用资源根目录。
ROOT = RESOURCE_ROOT


def _default_data_dir() -> str:
    if not FROZEN:
        return os.path.join(SOURCE_ROOT, "data")
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        local = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, APP_DIR_NAME, "data")


# 环境变量供自动化测试和便携部署使用。
DATA_DIR = os.path.abspath(
    os.environ.get("RUC_AGENT_DATA_DIR") or _default_data_dir()
)
STATIC_DIR = os.path.join(RESOURCE_ROOT, "static")
