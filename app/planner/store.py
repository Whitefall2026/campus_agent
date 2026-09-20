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
import shutil
import threading
import uuid
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
    """只读路径：文件损坏/缺失都返回 default，保证界面仍能打开。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


class StoreError(Exception):
    """规划数据文件损坏——写路径必须放弃本次落盘，避免用空表覆盖用户数据。"""


def _backup_corrupt(path: str) -> None:
    """把损坏文件另存 .corrupt-<时间戳>，保留人工修复的可能。"""
    try:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, "%s.corrupt-%s" % (path, stamp))
    except OSError:
        pass


def _read_strict(path, default):
    """严格读取：文件不存在返回 default，损坏则抛 StoreError（与“无数据”区分）。"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError(str(exc))
    if not isinstance(data, type(default)):
        raise StoreError("数据文件结构异常")
    return data


def save_json(path, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 原子写：随机 tmp 名 + fsync，避免并发撞名与断电留下半截文件。
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


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
        try:
            events = _read_strict(path, [])
        except StoreError as exc:
            # 事件日志损坏时只备份、不落盘：否则会把整份历史事件覆盖成这一条。
            # 事件属“尽力而为”的偏好记录，丢一条无碍，丢全表不可接受。
            _backup_corrupt(path)
            print("[store] 规划事件日志损坏，已备份并跳过本次记录: %r" % (exc,))
            return
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
