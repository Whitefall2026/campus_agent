# -*- coding: utf-8 -*-
"""长期记忆：把跨会话值得记住的用户信息按重要度保存。

与 Evidence（原始观察，可删）区分：memory 是经过整理/确认的结论，
例如“用户处在考试季”“用户喜欢简短直接的回答”，供后续画像与规划引用。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime

from app.paths import DATA_DIR

MEMORY_FILE = os.path.join(DATA_DIR, "user_memory.json")
MAX_MEMORY = 200

_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("memories"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "memories": []}


def _save(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEMORY_FILE)


def list_memories() -> list:
    return list(_load().get("memories") or [])


def add_memory(content: str, importance: float = 0.5,
               category: str = "preference",
               kind: str | None = None) -> dict:
    """写入一条长期记忆；同类别同内容时更新而不是重复堆积。"""
    content = str(content or "").strip()[:300]
    if not content:
        return {}
    if kind not in ("preference", "personality"):
        kind = "personality" if "personality" in str(category) else "preference"
    importance = max(0.0, min(1.0, float(importance or 0.5)))
    item = {
        "id": uuid.uuid4().hex[:10],
        "category": category,
        "kind": kind,
        "content": content,
        "importance": importance,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    with _LOCK:
        data = _load()
        memories = data["memories"]
        for m in memories:
            if m.get("category") == category and m.get("content") == content:
                m.update({
                    "importance": importance,
                    "updated_at": _now_iso(),
                })
                _save(data)
                return m
        memories.append(item)
        memories.sort(key=lambda m: m.get("importance", 0), reverse=True)
        if len(memories) > MAX_MEMORY:
            data["memories"] = memories[:MAX_MEMORY]
        _save(data)
    return item


def remember_preference(content: str, importance: float = 0.5,
                        category: str = "preference") -> dict:
    return add_memory(content, importance=importance,
                      category=category, kind="preference")


def remember_personality(content: str, importance: float = 0.5,
                         category: str = "personality") -> dict:
    return add_memory(content, importance=importance,
                      category=category, kind="personality")


def clear_memories() -> int:
    with _LOCK:
        n = len(_load().get("memories") or [])
        _save({"version": 1, "memories": []})
    return n
