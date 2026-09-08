"""本地 JSON 持久化，数据保存在 <仓库根>/data/todos.json。"""
from __future__ import annotations

import json
import os
import threading
from datetime import date

from app.paths import DATA_DIR
from app.core import kinds

DATA_FILE = os.path.join(DATA_DIR, "todos.json")
_LOCK = threading.RLock()


def _is_course(item: dict) -> bool:
    """课程由 courses.json 管理；兼容性字段用于防止旧数据被误顺延。"""
    return bool(item.get("course")) or str(item.get("source") or "") == "course"


def rollover_unfinished_schedules(todos: list[dict],
                                  today: date | str | None = None) -> int:
    """把今天之前未完成的普通日程原地转为待办，返回转换数量。

    这是跨日补偿逻辑：应用若连续几天未启动，下次读取时仍会处理所有已过期
    的未完成日程。课程表事件不参与；已完成日程和原本的待办也保持不变。
    """
    today_iso = today.isoformat() if isinstance(today, date) else str(today or date.today().isoformat())
    changed = 0
    for item in todos:
        if item.get("status") == "done" or _is_course(item):
            continue
        kind = kinds.valid_kind(item.get("kind")) or kinds.derive_kind(item)
        scheduled_date = str(item.get("date") or "")
        if kind != kinds.KIND_SCHEDULE or not scheduled_date or scheduled_date >= today_iso:
            continue

        item["kind"] = kinds.KIND_TODO
        item["rolled_over_from"] = scheduled_date
        item["rolled_over_from_time"] = item.get("time") or None
        item["rolled_over_from_end_time"] = item.get("end_time") or None
        item["rolled_over_on"] = today_iso
        item["date"] = None
        item["time"] = None
        item["end_time"] = None
        item["plan_defer_to"] = None
        changed += 1
    return changed


def _read_todos() -> list[dict]:
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_todos(todos: list[dict], refresh_profile: bool = True) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)
    if not refresh_profile:
        return
    # 待办是动态画像的重要输入：保存后事件驱动刷新画像（带最小间隔，
    # 不会每次保存都重算；失败静默，不影响主流程）。
    try:
        from app.ai import profile as ai_profile
        ai_profile.maybe_refresh_state()
    except Exception:
        pass


def load_todos() -> list[dict]:
    with _LOCK:
        todos = _read_todos()
        if rollover_unfinished_schedules(todos):
            # 读取路径中的跨日转换只做一次原子落盘，不触发画像刷新，避免画像
            # 刷新再次读取 todos 时递归；后续正常保存仍会刷新画像。
            _write_todos(todos, refresh_profile=False)
        return todos


def save_todos(todos: list[dict]) -> None:
    with _LOCK:
        rollover_unfinished_schedules(todos)
        _write_todos(todos)
