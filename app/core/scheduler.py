"""日程规划：冲突检测、时间轴聚合、自动建议时间。

输入为 todo 字典列表，输出可直接给前端渲染。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from . import kinds

WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]
CATEGORY_LABEL = {
    "exam": "考试",
    "homework": "作业",
    "class": "上课",
    "deadline": "事务/截止",
    "meeting": "会议",
    "activity": "活动",
    "social": "社交",
    "health": "健康",
    "other": "其他",
}
PRIORITY_LABEL = {"high": "高", "medium": "中", "low": "低"}
SUGGEST_SLOTS = ["09:00", "10:30", "14:00", "16:00", "19:30", "21:00"]


def end_time_of(todo: dict, default_min: int = 60) -> str | None:
    """返回事项结束时间（HH:MM），没有明确时长时按 default_min 估算。"""
    if todo.get("end_time"):
        return todo["end_time"]
    if not todo.get("time"):
        return None
    h, m = (int(x) for x in str(todo["time"]).split(":"))
    dur = todo.get("duration_min") or default_min
    total = h * 60 + m + dur
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _busy_map(todos: list[dict]) -> dict[str, list[tuple[str, str]]]:
    busy = defaultdict(list)
    for t in todos:
        if t.get("status") == "done" or not t.get("date") or not t.get("time"):
            continue
        busy[t["date"]].append((t["time"], end_time_of(t)))
    return busy


def _clash(start: str, end: str, intervals: list[tuple[str, str]]) -> bool:
    return any(start < e and s < end for s, e in intervals)


def _end_after(start: str, dur: int) -> str:
    h, m = (int(x) for x in start.split(":"))
    total = h * 60 + m + dur
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _s_to_min(hm: str) -> int:
    h, m = (int(x) for x in hm.split(":"))
    return h * 60 + m


def find_conflicts(todos: list[dict]) -> list[dict]:
    """返回冲突列表：同一时间重叠 / 计划时间晚于截止时间。"""
    conflicts = []
    by_day = defaultdict(list)
    for t in todos:
        if (t.get("status") == "done" or kinds.is_todo(t)
                or not t.get("date") or not t.get("time")):
            continue
        by_day[t["date"]].append(t)
    for day, items in by_day.items():
        items.sort(key=lambda t: t["time"])
        for i in range(len(items)):
            a = items[i]
            a_end = end_time_of(a)
            for j in range(i + 1, len(items)):
                b = items[j]
                if b["time"] < a_end:
                    conflicts.append({
                        "kind": "conflict",
                        "date": day,
                        "ids": [a["id"], b["id"]],
                        "message": (
                            f"「{a['title']}」与「{b['title']}」时间重叠："
                            f"{a['time']}-{a_end} 与 {b['time']}-{end_time_of(b)}"
                        ),
                    })
    for t in todos:
        if (t.get("status") == "done" or kinds.is_todo(t)
                or not t.get("date") or not t.get("time") or not t.get("deadline")):
            continue
        dl_t = t.get("deadline_time") or "23:59"
        start = datetime.combine(date.fromisoformat(t["date"]), datetime.strptime(t["time"], "%H:%M").time())
        dl = datetime.combine(date.fromisoformat(t["deadline"]), datetime.strptime(dl_t, "%H:%M").time())
        if start > dl:
            conflicts.append({
                "kind": "deadline",
                "date": t["date"],
                "ids": [t["id"]],
                "message": f"「{t['title']}」计划时间晚于截止时间（{t['deadline']} {dl_t}）",
            })
    return conflicts


def slot_conflicts(parsed: dict, todos: list[dict]) -> list[str]:
    """解析预览用：检查新事项与已有事项是否撞时间。"""
    if not parsed.get("date") or not parsed.get("time"):
        return []
    end = parsed.get("end_time") or end_time_of(parsed)
    msgs = []
    for t in todos:
        if t.get("status") == "done" or not t.get("date") or not t.get("time"):
            continue
        if t["date"] != parsed["date"]:
            continue
        t_end = end_time_of(t)
        if parsed["time"] < t_end and t["time"] < end:
            msgs.append(f"与已有事项「{t['title']}」（{t['time']}-{t_end}）冲突")
    return msgs


def suggest_slot(todo: dict, todos: list[dict], today: date, now: datetime) -> dict | None:
    """为未安排时间的事项建议一个可用时段。"""
    if todo.get("date") or todo.get("status") == "done":
        return None
    dur = todo.get("duration_min") or 90
    busy = _busy_map(todos)
    now_min = now.hour * 60 + now.minute

    def first_free(slots, day_iso, skip_before: int | None = None):
        for s in slots:
            if skip_before is not None and _s_to_min(s) < skip_before:
                continue
            e = _end_after(s, dur)
            if not _clash(s, e, busy.get(day_iso, [])):
                return s, e
        return None

    if todo.get("deadline"):
        dl = todo["deadline"]
        if dl < today.isoformat():
            s, e = "09:00", _end_after("09:00", dur)
            return {"date": today.isoformat(), "time": s, "end_time": e, "reason": "截止日期已过，建议今天尽早处理"}
        if dl == today.isoformat():
            hit = first_free(SUGGEST_SLOTS, dl, now_min)
            if hit:
                return {"date": dl, "time": hit[0], "end_time": hit[1], "reason": "今天截止，安排在最早的可用空档"}
            s, e = "21:00", _end_after("21:00", dur)
            return {"date": dl, "time": s, "end_time": e, "reason": "今天时间紧张，建议今晚完成"}
        prev_day = (date.fromisoformat(dl) - timedelta(days=1)).isoformat()
        hit = first_free(["20:00", "19:30", "21:00", "09:00", "10:30"], prev_day)
        if hit:
            return {"date": prev_day, "time": hit[0], "end_time": hit[1], "reason": f"截止还有缓冲，建议前一天 {hit[0]} 完成"}
        s, e = "09:00", _end_after("09:00", dur)
        return {"date": dl, "time": s, "end_time": e, "reason": "截止当天早上完成"}

    hit = first_free(SUGGEST_SLOTS, today.isoformat(), now_min)
    if hit:
        return {"date": today.isoformat(), "time": hit[0], "end_time": hit[1], "reason": "安排在今天最早的可用空档"}
    tomorrow = (today + timedelta(days=1)).isoformat()
    hit = first_free(SUGGEST_SLOTS, tomorrow)
    if hit:
        return {"date": tomorrow, "time": hit[0], "end_time": hit[1], "reason": "今天已无空档，建议明天安排"}
    return None


def _enrich(t: dict, today_iso: str) -> dict:
    out = dict(t)
    out["done"] = t.get("status") == "done"
    kind = kinds.valid_kind(out.get("kind")) or kinds.derive_kind(out)
    out["kind"] = kind
    out["is_schedule"] = kind == kinds.KIND_SCHEDULE
    out["is_todo"] = kind == kinds.KIND_TODO
    out["kind_label"] = "日程" if out["is_schedule"] else "待办"
    out["category_label"] = CATEGORY_LABEL.get(t.get("category"), "其他")
    out["priority_label"] = PRIORITY_LABEL.get(t.get("priority"), "中")
    out["end_time"] = end_time_of(t)
    out["overdue"] = False
    out["deadline_label"] = None
    if t.get("deadline"):
        dl = t["deadline"]
        dl_t = t.get("deadline_time")
        out["deadline_label"] = f"截止 {dl}" + (f" {dl_t}" if dl_t else "")
        if dl < today_iso and not out["done"]:
            out["overdue"] = True
    if (out["is_schedule"] and t.get("date")
            and t["date"] < today_iso and not out["done"]):
        out["overdue"] = True
    return out


def build_state(todos: list[dict], now: datetime | None = None) -> dict:
    """聚合出前端所需的完整状态：统计、冲突、日程时间轴、待办列表、规划建议。"""
    now = now or datetime.now()
    today = now.date()
    today_iso = today.isoformat()

    enriched = [_enrich(t, today_iso) for t in todos]
    conflicts = find_conflicts(enriched)
    conflict_ids = {cid for c in conflicts for cid in c["ids"]}
    for t in enriched:
        t["conflict"] = t["id"] in conflict_ids

    schedules = [t for t in enriched if t["is_schedule"]]
    todo_items = [t for t in enriched if t["is_todo"]]
    pending_todos = [t for t in todo_items if not t["done"]]
    stats = {
        "today_count": sum(1 for t in schedules if t.get("date") == today_iso and not t["done"]),
        "week_count": sum(
            1 for t in schedules
            if t.get("date") and today_iso <= t["date"] <= (today + timedelta(days=7)).isoformat()
            and not t["done"]
        ),
        "overdue_count": sum(1 for t in enriched if not t["done"] and t.get("overdue")),
        "conflict_count": len(conflicts),
        "todo_count": len(pending_todos),
        "todo_done_count": sum(1 for t in todo_items if t["done"]),
        "schedule_count": sum(1 for t in schedules if not t["done"]),
    }

    ds = {today_iso}
    for t in schedules:
        if t.get("date"):
            ds.add(t["date"])
    start_d = date.fromisoformat(min(ds))
    end_d = date.fromisoformat(max(ds))
    if (end_d - start_d).days > 35:
        # 跨度太大时不生成逐日数组，只保留“近 7 天 + 有日程的日期”
        keep = {today + timedelta(days=i) for i in range(7)}
        for iso in ds:
            keep.add(date.fromisoformat(iso))
        day_iter = sorted(keep)
    else:
        day_iter = [start_d + timedelta(days=i)
                    for i in range((end_d - start_d).days + 1)]

    timeline = []
    for d in day_iter:
        iso = d.isoformat()
        items = [t for t in schedules if t.get("date") == iso]
        items.sort(key=lambda t: (t["done"], t.get("time") or "99:99", t.get("created_at") or ""))
        ddl_items = [t for t in todo_items
                     if t.get("deadline") == iso and not t["done"]]
        timeline.append({
            "date": iso,
            "date_label": f"{d.month}月{d.day}日",
            "weekday": "周" + WEEKDAY_CN[d.weekday()],
            "is_today": iso == today_iso,
            "items": items,
            "ddl_items": ddl_items,
        })

    # 待办页：所有 kind=todo 且未完成（带/不带截止时间都展示）
    pending_todos.sort(key=lambda t: (
        t.get("done"),
        t.get("overdue") is not True,
        t.get("deadline") or "9999-99-99",
        t.get("deadline_time") or "99:99",
        t.get("created_at") or "",
    ))

    # 可规划进时间轴的：待办里还没有固定开始时间的（未排期）
    unplanned = [t for t in pending_todos if not t.get("date")]
    suggestions = []
    for t in unplanned:
        s = suggest_slot(t, todos, today, now)
        if s:
            suggestions.append({**s, "id": t["id"], "title": t["title"]})

    return {
        "ok": True,
        "today": today_iso,
        "stats": stats,
        "conflicts": conflicts,
        "timeline": timeline,
        "pool": pending_todos,
        "todo_items": pending_todos,
        "todo_done": [t for t in todo_items if t["done"]],
        "suggestions": suggestions,
        "todos": enriched,
    }
