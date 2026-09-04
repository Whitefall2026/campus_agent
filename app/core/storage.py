"""本地 JSON 持久化，数据保存在 <仓库根>/data/todos.json。"""
from __future__ import annotations

import json
import os

from app.paths import DATA_DIR

DATA_FILE = os.path.join(DATA_DIR, "todos.json")


def load_todos() -> list[dict]:
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_todos(todos: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)
