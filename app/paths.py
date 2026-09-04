"""仓库内关键路径。

统一从这里取路径，避免文件搬移后 data/、static/ 定位出错。
"""
from __future__ import annotations

import os

# <仓库根>：app/paths.py 的上一级目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(ROOT, "data")      # 运行数据（已 gitignore，严禁提交）
STATIC_DIR = os.path.join(ROOT, "static")  # 前端页面
