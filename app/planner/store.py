# -*- coding: utf-8 -*-
"""规划数据的本地 JSON 持久化。

- planner_profile.json   精力曲线（逐小时系数）+ 更新时间
- planner_events.json    事件日志（plan_apply / defer / move / feedback /
                         rating / done …），供偏好校准与历史完成率统计

所有函数都支持传入 data_dir（测试时指向临时目录），缺省走 data/。
写入一律原子替换 + 进程内锁，与 app/core/storage.py 风格一致。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from app.paths import DATA_DIR

PROFILE_FILE = "planner_profile.json"
EVENTS_FILE = "planner_events.json"
MAX_EVENTS = 5000

_LOCK = threading.RLock()


def _paths(data_dir: str | None = None):
    root = data_dir or DATA_DIR
    return {
        "profile": os.path.join(root, PROFILE_FILE),
        "events": os.path.join(root, EVENTS_FILE),
    }


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 事件日志
# ---------------------------------------------------------------------------
def append_event(entry: dict, data_dir: str | None = None) -> None:
    """记录一条规划事件。entry 会被补上 created_at 并限制日志长度。"""
    ev = dict(entry or {})
    ev.setdefault("created_at", now_iso())
    with _LOCK:
        path = _paths(data_dir)["events"]
        events = load_json(path, [])
        events.append(ev)
        if len(events) > MAX_EVENTS:
            events = events[-MAX_EVENTS:]
        save_json(path, events)


def load_events(data_dir: str | None = None) -> list:
    with _LOCK:
        path = _paths(data_dir)["events"]
        return load_json(path, [])


def clear_events(data_dir: str | None = None) -> None:
    with _LOCK:
        save_json(_paths(data_dir)["events"], [])


# ---------------------------------------------------------------------------
# 精力曲线 profile
# ---------------------------------------------------------------------------
def load_profile(data_dir: str | None = None) -> dict:
    """profile: {hours: [24 个 float], updated_at: iso, version: int}"""
    with _LOCK:
        path = _paths(data_dir)["profile"]
        return load_json(path, {})


def save_profile(profile: dict, data_dir: str | None = None) -> dict:
    clean = {"hours": list(profile.get("hours") or []), "version": 1}
    clean["updated_at"] = profile.get("updated_at") or now_iso()
    with _LOCK:
        save_json(_paths(data_dir)["profile"], clean)
    return clean
