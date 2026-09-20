"""本地 JSON 持久化，数据保存在 <仓库根>/data/todos.json 与 data/goals.json。"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import uuid
from datetime import date, datetime

from app.paths import DATA_DIR
from app.core import kinds

DATA_FILE = os.path.join(DATA_DIR, "todos.json")
GOALS_FILE = os.path.join(DATA_DIR, "goals.json")
_LOCK = threading.RLock()
# 供跨模块（如微信线程）复用的公开锁名，与内部 _LOCK 是同一把可重入锁。
TODOS_LOCK = _LOCK


class StorageError(Exception):
    """数据文件存在但内容损坏/不可读。

    读取失败绝不能与“没有数据”混为一谈：写路径必须在遇到该异常时放弃本次
    落盘，避免用空列表覆盖用户原有数据。
    """


def _backup_corrupt(path: str) -> None:
    """把损坏的数据文件另存一份 .corrupt-<时间戳>，保留人工修复的可能。"""
    try:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, "%s.corrupt-%s" % (path, stamp))
    except OSError:
        pass


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
    """读取待办列表。

    文件不存在视为空；文件存在但内容损坏时抛 StorageError，交由调用方决定
    是“只读容忍（返回空视图）”还是“拒绝落盘（避免覆盖）”。
    """
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageError(str(exc))
    if not isinstance(data, list):
        raise StorageError("todos.json 内容不是列表")
    return data


def _write_todos(todos: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = f"{DATA_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DATA_FILE)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _maybe_refresh_profile() -> None:
    """待办是动态画像的重要输入：保存后在锁外触发刷新。

    刷新可能发起外部 LLM 调用，必须放在锁外，否则会阻塞所有读接口与微信
    线程。带最小间隔且失败静默，不影响主流程。
    """
    try:
        from app.ai import profile as ai_profile
        ai_profile.maybe_refresh_state()
    except Exception:
        pass


@contextlib.contextmanager
def todos_transaction():
    """在 TODOS_LOCK 内完成 读→跨日顺延→修改→落盘，供所有写者共用。

    读取损坏时抛 StorageError（不落盘），从而不会用空列表覆盖原文件。
    修改完成后在锁外触发一次画像刷新。
    """
    with _LOCK:
        todos = _read_todos()
        rollover_unfinished_schedules(todos)
        yield todos
        # 落盘前再跑一次顺延：本次新增/改写的条目（例如把日程写到过去的日期）
        # 也要按同一套规则转换，否则响应里返回的还是未顺延的“夹生”数据。
        rollover_unfinished_schedules(todos)
        _write_todos(todos)
    _maybe_refresh_profile()


def load_todos() -> list[dict]:
    with _LOCK:
        try:
            todos = _read_todos()
        except StorageError:
            # 只读路径容忍损坏：备份一份损坏文件后返回空视图，保证应用仍能打开。
            # 写路径会重新严格读取并失败，因此不会把空列表写回去。
            _backup_corrupt(DATA_FILE)
            return []
        if rollover_unfinished_schedules(todos):
            # 读取路径中的跨日转换只做一次原子落盘，不触发画像刷新，避免画像
            # 刷新再次读取 todos 时递归；后续正常保存仍会刷新画像。
            _write_todos(todos)
        return todos


def save_todos(todos: list[dict]) -> None:
    with _LOCK:
        rollover_unfinished_schedules(todos)
        _write_todos(todos)
    _maybe_refresh_profile()


def update_todos(mutator) -> list[dict]:
    """在 TODOS_LOCK 内完成 读→跨日顺延→修改→落盘（不刷新画像），返回最新列表。

    供 HTTP 线程与微信线程等不同写者共用，避免读改写竞态互相覆盖。
    """
    with todos_transaction() as todos:
        mutator(todos)
        return todos


def _read_goals() -> list[dict]:
    if not os.path.exists(GOALS_FILE):
        return []
    try:
        with open(GOALS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # 备份损坏内容（不删除原文件，保持“已初始化”语义），返回空视图。
        _backup_corrupt(GOALS_FILE)
        return []
    return data if isinstance(data, list) else []


def goals_initialized() -> bool:
    """goals.json 是否存在，用于区分“首次使用”与“用户已清空全部目标”。"""
    return os.path.exists(GOALS_FILE)


def load_goals() -> list[dict]:
    with _LOCK:
        return _read_goals()


def save_goals(goals: list[dict]) -> None:
    if not isinstance(goals, list):
        raise StorageError("goals 必须是列表")
    with _LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = f"{GOALS_FILE}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(goals, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, GOALS_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass