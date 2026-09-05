# -*- coding: utf-8 -*-
"""命令行演示：为「明天」生成一份能量规划方案。

运行：python -m app.planner.demo [YYYY-MM-DD]
输出：当日空闲能量预算、贪心排程表、容差/阻塞预警与软线顺延建议。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.planner import fields, planner


def _mk(id_s, title, deadline=None, kind="todo", status="pending", **kw):
    t = {
        "id": id_s,
        "title": title,
        "kind": kind,
        "status": status,
        "category": kw.pop("category", "other"),
        "priority": kw.pop("priority", "medium"),
        "deadline": deadline,
        "deadline_time": kw.pop("deadline_time", None),
        "date": kw.pop("date", None),
        "time": kw.pop("time", None),
        "end_time": kw.pop("end_time", None),
        "duration_min": kw.pop("duration_min", None),
        "raw": title,
        "created_at": kw.pop("created_at", datetime.now().isoformat(timespec="seconds")),
    }
    for k, v in kw.items():
        t[k] = v
    return t


def demo(day: date | None = None) -> None:
    day = day or date.today() + timedelta(days=1)
    iso = day.isoformat()

    todos = [
        # 已有的固定日程（占用上午 / 下午时段）
        _mk("c1", "数据结构课", kind="schedule", category="class", date=iso,
            time="08:00", end_time="09:30"),
        _mk("c2", "社团例会", kind="schedule", category="meeting", date=iso,
            time="15:00", end_time="16:00"),
        # 今天截止的待办（硬线 / 软线）
        _mk("t1", "提交课程设计实验报告", deadline=iso, category="homework",
            priority="high", duration_min=90),
        _mk("t2", "完成职业规划课论文初稿", deadline=iso, category="homework",
            priority="medium", duration_min=120),
        # 无截止的待办池
        _mk("t3", "帮社团写招新推文", category="activity", priority="medium",
            duration_min=60),
        _mk("t4", "整理上周课堂笔记", category="homework", priority="low",
            duration_min=30),
        # 顺延到今天的任务
        _mk("t5", "数学作业订正", deadline=(day - timedelta(days=1)).isoformat(),
            category="homework", priority="high", duration_min=60,
            status="deferred", plan_defer_to=iso),
    ]

    plan = planner.plan_day(todos, day=day)
    print("=" * 64)
    print("规划日：{}（{}） 引擎：{}  容差 ×{}".format(
        plan["date"], "周" + "日一二三四五六"[day.weekday()],
        plan["meta"]["engine"], plan["meta"]["tolerance"]))
    b = plan["budget"]
    print("能量预算：可用 {} 点（{} 个空闲块），已排 {} 点（{}%）".format(
        b["available_points"], b["free_runs"],
        b["planned_points"], int(b["planned_ratio"] * 100)))

    print("-" * 64)
    print("排程：")
    for e in plan["entries"]:
        mark = " ⚡容差适配" if e["tolerance"] else ""
        print("  {}-{}  {}（耗能 {} / 时段 {} 点）{}".format(
            e["start"], e["end"], e["title"], e["energy_cost"],
            e["slot_points"], mark))
    for r in plan["rests"]:
        print("  {}-{}  [休息/机动]".format(r["start"], r["end"]))

    print("-" * 64)
    if plan["warnings"]:
        print("预警（{} 条）：".format(len(plan["warnings"])))
        for w in plan["warnings"]:
            print("  [{}] {}".format(w["level"], w["copy"]))
    if plan["deferrals"]:
        print("软线自动顺延建议（{} 条）：".format(len(plan["deferrals"])))
        for d in plan["deferrals"]:
            print("  {} → {}".format(d["copy"], d["to_date"]))
    if plan["tomorrow"]:
        names = "、".join(str(t["title"]) for t in plan["tomorrow"])
        print("明日优先：{}".format(names))
    if not plan["warnings"] and not plan["deferrals"] and not plan["tomorrow"]:
        print("无预警，负载合理 ✅")

    # 展示字段归一化结果
    print("-" * 64)
    print("字段归一化示例：")
    for t in todos:
        n = fields.normalize_task(t)
        print("  {} 耗能{} 时长{}min {}线 顺延{}天".format(
            n["title"], n["energy_cost"], n["duration_min"],
            {"hard": "硬", "soft": "软"}[n["deadline_type"]],
            n["ddl_float_days"]))


if __name__ == "__main__":
    day = None
    if len(sys.argv) > 1:
        try:
            day = date.fromisoformat(sys.argv[1])
        except ValueError:
            print("日期格式应为 YYYY-MM-DD")
            sys.exit(1)
    demo(day)
