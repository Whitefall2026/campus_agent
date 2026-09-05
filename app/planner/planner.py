# -*- coding: utf-8 -*-
"""贪心匹配调度器 + 阻塞兜底 + 软线自动延期。

流程（对应需求文档 模块 1.2 与 模块 4.2）：

1. 收集某一天的空闲时段：把该日已有「日程/课程」占用的时间段扣除，
   以 30 分钟为粒度切成连续空闲块（07:00~23:00）；
2. 候选任务 = 待办池里尚未排期、且（截止在今天 / 无截止 / 顺延到今天）的
   未完成任务，按 截止日期 → 耗能 排序；
3. 贪心匹配：对每个空闲块起点，从最高优先级任务开始试：
   - 任务耗能 X ≤ 时段能量 Y × 1.15 → 整块放入；
     其中 X 超过 Y 的部分为「容差适配」（≤15%），会给温和提示文案；
   - 否则向下扫描任务池找能放入者；均不满足 → 该时段标记为休息/机动；
   - 一天结束后仍未排入的高优先级任务 → 生成阻塞预警，进入「明日优先」；
4. 负载过高（硬线任务未排入 / 排入能量接近预算）时，对未排入的软线任务
   自动顺延 1~2 天（仅产出方案与文案，是否落库由调用方决定）。

输出为纯 dict 方案（plan），不直接修改任务池 —— 调用方可用
apply_placements / apply_deferrals 把方案落库，便于“先预览、再采纳”。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.core import kinds
from app.core import courses as course_mod
from app.planner import energy as energy_mod
from app.planner import fields

TOLERANCE = 1.15                      # 容差：X ≤ Y × 1.15
PLAN_START = energy_mod.PLAN_DAY_START
PLAN_END = energy_mod.PLAN_DAY_END
STEP = 30
DEFAULT_DEADLINE_TIME = "23:59"

BLOCKED_COPY = "「{title}」今日未启动，建议明早优先处理，或评估是否需降低完成标准。"
TOLERANCE_COPY = (
    "这项任务需要约 {cost} 点精力，比该时段标准能量稍多，"
    "但在可承受范围内（容差 ≤15%），AI 认为你今天状态可以拿下。"
)
DEFER_COPY = (
    "根据你目前的负载，我把「{title}」自动延后至{to}，"
    "因为当天还有「{hard_names}」等硬线安排需要优先完成。"
    "如果你不同意，可以手动拖回。"
)
REST_COPY = "本时段没有能匹配的任务，标记为休息/机动。"


# ---------------------------------------------------------------------------
# 空闲时段
# ---------------------------------------------------------------------------
def _interval_min(t: dict) -> tuple | None:
    """返回 (start_min, end_min)；无开始时间的条目不算占用。"""
    s = fields.hm_to_min(t.get("time"))
    if s is None:
        return None
    e = fields.hm_to_min(t.get("end_time"))
    if e is None or e <= s:
        e = s + int(t.get("duration_min") or 60)
    return s, e


def day_busy(todos: list[dict], day: date, include_courses: bool = True) -> list:
    """某天已被占用的分钟区间列表（日程 + 课程）。"""
    iso = day.isoformat()
    busy = []
    for t in todos:
        if t.get("status") == "done":
            continue
        if str(t.get("date") or "") != iso:
            continue
        if kinds.valid_kind(t.get("kind")) != kinds.KIND_SCHEDULE:
            continue
        iv = _interval_min(t)
        if iv:
            busy.append(iv)
    if include_courses:
        try:
            for ev in course_mod.term_events():
                if str(ev.get("date") or "") == iso:
                    iv = _interval_min(ev)
                    if iv:
                        busy.append(iv)
        except Exception:
            pass  # 课程数据缺失不影响规划
    busy.sort()
    return busy


def _occupied_at(minute: int, busy: list) -> bool:
    return any(s <= minute < e for s, e in busy)


def free_runs(todos: list[dict], day: date, include_courses: bool = True) -> list[dict]:
    """把一天的 30 分钟格子切分成连续空闲块。

    返回 [{start: 分钟, end: 分钟, points: 整块可提供能量}]。
    """
    busy = day_busy(todos, day, include_courses=include_courses)
    runs = []
    cur = None
    m = PLAN_START
    while m < PLAN_END:
        free = not _occupied_at(m, busy)
        if free and cur is None:
            cur = {"start": m, "end": m}
        elif free:
            cur["end"] = m + STEP
        elif cur is not None:
            runs.append(cur)
            cur = None
        m += STEP
    if cur is not None:
        runs.append(cur)
    for r in runs:
        r["points"] = energy_mod.block_points(r["start"], r["end"] - r["start"])
    return runs


# ---------------------------------------------------------------------------
# 候选任务
# ---------------------------------------------------------------------------
def candidate_tasks(todos: list[dict], day: date) -> list[dict]:
    """某天参与排程的候选：未完成、未排期（无 date/time）的待办。

    含：截止今天 / 已逾期（兜底“尽早处理”）/ 无截止（待办池）的任务；
    被自动顺延到未来的任务（plan_defer_to > 今天）等顺延日再排。
    """
    iso = day.isoformat()
    out = []
    for t in todos:
        if t.get("status") == "done":
            continue
        if kinds.valid_kind(t.get("kind")) != kinds.KIND_TODO:
            continue
        if t.get("date") or t.get("time"):
            continue  # 已排期的不再规划
        dl = str(t.get("deadline") or "")
        defer_to = str(t.get("plan_defer_to") or "")
        if defer_to and defer_to > iso:
            continue  # 顺延日在未来，等那天再排
        if dl and dl > iso and not defer_to:
            continue  # 截止在未来的任务等它截止那天（或顺延日）再排
        out.append(t)
    return out


def _deadline_limit_min(t: dict, day: date):
    """同一天截止的硬限制：返回 (允许最晚开始分钟 或 None)。"""
    dl = str(t.get("deadline") or "")
    if dl != day.isoformat():
        return None
    return fields.hm_to_min(t.get("deadline_time") or DEFAULT_DEADLINE_TIME)


def _task_key(t: dict) -> tuple:
    """排序：截止（无截止最后）→ 耗能降序 → 硬线优先 → 优先级 → 创建时间。"""
    dl = str(t.get("deadline") or "9999-99-99")
    dlt = str(t.get("deadline_time") or DEFAULT_DEADLINE_TIME)
    prio = {"high": 0, "medium": 1, "low": 2}.get(str(t.get("priority") or "medium"), 1)
    hard = 0 if t.get("deadline_type") == "hard" else 1
    return (dl, dlt, -int(t.get("energy_cost") or 1), hard, prio,
            str(t.get("created_at") or ""))


# ---------------------------------------------------------------------------
# 主调度
# ---------------------------------------------------------------------------
def plan_day(
    todos: list[dict],
    day: date | None = None,
    profile: dict | None = None,
    include_courses: bool = True,
    seed: int | None = None,
    focus_cap_min: int | None = None,
) -> dict:
    """为某天生成完整规划方案（纯函数，不落库）。

    返回 {
      date, budget, entries[], warnings[], deferrals[], tomorrow[], meta
    }
    """
    day = day or date.today()
    iso = day.isoformat()
    prof = profile or energy_mod.default_profile()
    runs = free_runs(todos, day, include_courses=include_courses)
    cands = [fields.normalize_task(t) for t in candidate_tasks(todos, day)]
    cands.sort(key=_task_key)

    entries, warnings = [], []
    rest_marks = []
    remain = list(cands)
    focus_minutes = 0

    # —— 贪心放置 ——
    for run in runs:
        cursor = run["start"]
        while cursor < run["end"] and remain:
            placed = None
            # 从最高优先级向下扫描：找第一个“放得下”的任务
            for idx, task in enumerate(remain):
                dur = int(task.get("duration_min") or 60)
                # 除了今天硬截止的事，不把用户的空白时间全部占满。
                # cap 由调用方按当下精力给出；None 保持旧行为，便于复用。
                hard_today = (
                    task.get("deadline_type") == "hard"
                    and str(task.get("deadline") or "") == iso
                )
                if focus_cap_min is not None and not hard_today and focus_minutes + dur > focus_cap_min:
                    continue
                limit = _deadline_limit_min(task, day)
                if limit is not None and cursor + dur > limit:
                    continue
                if cursor + dur > run["end"]:
                    continue
                y = energy_mod.block_points(cursor, dur, prof)
                x = int(task.get("energy_cost") or 1)
                if x <= y * TOLERANCE:
                    placed = (idx, task, cursor, dur, y)
                    break
            if placed is None:
                # 当前 30 分钟格里没有能匹配的任务 → 该格休息/机动，继续下一格
                rest_marks.append({
                    "start": fields.min_to_hm(cursor),
                    "end": fields.min_to_hm(cursor + STEP),
                    "type": "rest",
                    "note": REST_COPY,
                })
                cursor += STEP
                continue
            idx, task, cursor, dur, y = placed
            x = int(task.get("energy_cost") or 1)
            end_min = cursor + dur
            entries.append({
                "task_id": task.get("id"),
                "title": task.get("title"),
                "deliverable": task.get("deliverable"),
                "parent_id": task.get("parent_id"),
                "deadline": task.get("deadline"),
                "deadline_time": task.get("deadline_time"),
                "deadline_type": task.get("deadline_type"),
                "energy_cost": x,
                "duration_min": dur,
                "start": fields.min_to_hm(cursor),
                "end": fields.min_to_hm(end_min),
                "coefficient": energy_mod.coefficient_at(cursor / 60.0, prof),
                "slot_points": round(y, 2),
                "tolerance": x > y,
            })
            if x > y:
                warnings.append({
                    "task_id": task.get("id"),
                    "title": task.get("title"),
                    "level": "tolerance",
                    "copy": TOLERANCE_COPY.format(cost=x, title=task.get("title")),
                })
            remain.pop(idx)
            focus_minutes += dur
            cursor = end_min
        if not remain:
            break

    # —— 预算统计 ——
    total_pts = sum(r["points"] for r in runs)
    used_pts = sum(energy_mod.block_points(
        fields.hm_to_min(e["start"]), e["duration_min"], prof) for e in entries)
    budget = {
        "date": iso,
        "free_runs": len(runs),
        "available_points": round(total_pts, 2),
        "planned_points": round(used_pts, 2),
        "planned_ratio": round(used_pts / total_pts, 3) if total_pts else 0.0,
        "focus_minutes": focus_minutes,
        "focus_cap_min": focus_cap_min,
        "protected_free_minutes": max(0, sum(r["end"] - r["start"] for r in runs) - focus_minutes),
    }

    # 相邻的休息/机动格子合并成整段（避免逐格刷屏）
    rests = []
    for r in rest_marks:
        if rests and rests[-1]["end"] == r["start"] and rests[-1]["note"] == r["note"]:
            rests[-1]["end"] = r["end"]
        else:
            rests.append(dict(r))

    # —— 阻塞兜底 & 软线延期 ——
    tomorrow = []
    deferrals = []
    blocked_hard = [t for t in remain if t.get("deadline_type") == "hard"]
    blocked_soft = [t for t in remain if t.get("deadline_type") == "soft"]
    load_high = bool(blocked_hard) or (
        bool(remain) and budget["planned_ratio"] >= 0.95
    )

    for t in remain:
        if t.get("deadline_type") == "hard":
            warnings.append({
                "task_id": t.get("id"),
                "title": t.get("title"),
                "level": "blocked",
                "copy": BLOCKED_COPY.format(title=t.get("title")),
            })
        elif not load_high:
            # 负载不高但实在没塞进去：同样进“明日优先”，不自动顺延
            warnings.append({
                "task_id": t.get("id"),
                "title": t.get("title"),
                "level": "blocked",
                "copy": BLOCKED_COPY.format(title=t.get("title")),
            })
        tomorrow.append({
            "task_id": t.get("id"),
            "title": t.get("title"),
            "deadline": t.get("deadline"),
            "deadline_type": t.get("deadline_type"),
            "energy_cost": t.get("energy_cost"),
        })

    # 负载过高时自动顺延低优先级软线任务（仅供采纳的“建议方案”）
    if load_high:
        hard_names = "、".join(
            str(t.get("title")) for t in blocked_hard[:2]
        ) or "硬线安排"
        blocked_soft.sort(key=lambda t: (
            {"high": 0, "medium": 1, "low": 2}.get(str(t.get("priority") or "medium"), 1),
            str(t.get("created_at") or ""),
        ))
        for t in blocked_soft:
            days = int(t.get("ddl_float_days") or 0)
            if days < 1:
                continue
            to = day + timedelta(days=1)
            deferrals.append({
                "task_id": t.get("id"),
                "title": t.get("title"),
                "deadline": t.get("deadline"),
                "deadline_type": t.get("deadline_type"),
                "float_days": min(days, 2),
                "from_date": iso,
                "to_date": to.isoformat(),
                "copy": DEFER_COPY.format(
                    title=t.get("title"),
                    to=to.strftime("%m月%d日"),
                    hard_names=hard_names,
                ),
            })
            tomorrow = [x for x in tomorrow if x.get("task_id") != t.get("id")]
    else:
        # 负载不高时不触碰软线；软线若剩着也只是“明日优先”候选
        pass

    meta = {
        "engine": "greedy-v1",
        "tolerance": TOLERANCE,
        "candidates": len(cands),
        "planned": len(entries),
        "rest": len(rests),
        "load_high": load_high,
    }
    return {
        "ok": True,
        "date": iso,
        "budget": budget,
        "entries": entries,
        "warnings": warnings,
        "rests": rests,
        "deferrals": deferrals,
        "tomorrow": tomorrow,
        "meta": meta,
        "seed": seed,
    }


# ---------------------------------------------------------------------------
# 方案落库
# ---------------------------------------------------------------------------
def apply_placements(todos: list[dict], plan: dict,
                     task_ids: list | None = None,
                     data_dir: str | None = None) -> dict:
    """把方案里的若干任务落库：待办 → 日程（date/time/end_time）。

    与页面「规划到日程」行为一致；返回 (新列表, 应用明细)。
    """
    ids = None if task_ids is None else set(task_ids)
    todos = [dict(t) for t in todos]
    by_id = {str(t.get("id")): t for t in todos}
    applied = []
    for e in plan.get("entries") or []:
        t = by_id.get(str(e.get("task_id")))
        if t is None or t.get("status") == "done":
            continue
        if ids is not None and str(e.get("task_id")) not in ids:
            continue
        t["kind"] = kinds.KIND_SCHEDULE
        t["date"] = plan.get("date")
        t["time"] = e.get("start")
        t["end_time"] = e.get("end")
        t["duration_min"] = e.get("duration_min") or t.get("duration_min")
        applied.append(e)
    for e in applied:
        _log_event("plan_apply", e, plan, data_dir)
    return todos, applied


def apply_deferrals(todos: list[dict], plan: dict,
                    task_ids: list | None = None,
                    data_dir: str | None = None) -> dict:
    """把方案里的软线顺延落库：deadline 顺延 1 天、状态置 deferred。

    返回 (新列表, 已应用明细)。
    """
    ids = None if task_ids is None else set(task_ids)
    todos = [dict(t) for t in todos]
    by_id = {str(t.get("id")): t for t in todos}
    applied = []
    for d in plan.get("deferrals") or []:
        t = by_id.get(str(d.get("task_id")))
        if t is None or t.get("status") == "done":
            continue
        if ids is not None and str(d.get("task_id")) not in ids:
            continue
        old = {
            "deadline": t.get("deadline"),
            "deadline_time": t.get("deadline_time"),
            "status": t.get("status"),
        }
        try:
            dl = date.fromisoformat(str(d.get("deadline") or ""))
            new_dl = (dl + timedelta(days=1)).isoformat()
        except ValueError:
            new_dl = None
        t["deadline"] = new_dl or d.get("deadline")
        t["status"] = "deferred"
        t["plan_defer_to"] = d.get("to_date")
        applied.append({**d, "old_deadline": old.get("deadline"),
                        "new_deadline": t["deadline"]})
    for e in applied:
        _log_event("defer", e, plan, data_dir)
    return todos, applied


def record_move(todos: list[dict], task_id: str, date_s: str,
                time_s: str | None, end_time_s: str | None = None,
                data_dir: str | None = None) -> dict:
    """用户拖拽调整（偏好记录）：返回 (新列表, 记录)。"""
    todos = [dict(t) for t in todos]
    t = next((x for x in todos if str(x.get("id")) == task_id), None)
    if t is None:
        return todos, None
    old = {k: t.get(k) for k in ("date", "time", "end_time")}
    t["date"] = date_s
    t["time"] = time_s
    t["end_time"] = end_time_s or t.get("end_time")
    t["kind"] = kinds.valid_kind(t.get("kind")) or kinds.KIND_SCHEDULE
    record = {
        "task_id": task_id,
        "title": t.get("title"),
        "from_date": old.get("date"),
        "from_time": old.get("time"),
        "to_date": date_s,
        "to_time": time_s,
        "type": "move",
    }
    _log_event("move", record, {"date": date_s}, data_dir)
    return todos, record


def _log_event(kind_s: str, payload: dict, plan: dict | None, data_dir) -> None:
    from app.planner import store
    store.append_event({
        "type": kind_s,
        "plan_date": (plan or {}).get("date"),
        **payload,
    }, data_dir)
