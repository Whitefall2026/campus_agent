# -*- coding: utf-8 -*-
"""动态“缓冲垫”机制（对应需求文档 模块 2，确定性核心版）。

- load_metrics:  负载评估——当前排程任务总能量、未完成任务数、近 3 天
  日程密度等指标，输出 low/medium/high 与理由；
- evaluate_intrusion: 收到“外部插入任务”（如社团团建通知）时做风险评估；
- reply_copy:     生成礼貌的拒绝/延期话术（LLM 未启用或失败时的确定性兜底，
                  语气冷静客观，不制造焦虑）；
- shield_verdict: 组合 verdict：light（宽松可接）/ moderate（谨慎）/ heavy（挡）。

LLM 增强版话术（带个人风格）在后续里程碑接入 AI Gateway，规则文案保持兜底。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.core import kinds
from app.core.extractor import parse_text
from app.planner import fields, planner

LIGHT_COPY = (
    "我看了下安排，最近还比较宽松，这条应该能接下。"
    "我会把它记进待办，具体时间稍后确认。"
)
MODERATE_COPY = (
    "这条我最近几天安排有点密（还有 {undone} 项待办、已排 {energy} 点能量），"
    "可能没法保证到场。我可以先标“待定”，到前一天再确认，或者线上支持？"
)
HEAVY_COPY = (
    "最近我这边负载比较满（还有 {undone} 项待办，今天已排 {energy} 点能量），"
    "这个时间点恐怕腾不出来。建议我线上参与或看回放，先标“待定”可以吗？"
)

# 阈值（可调）
HEAVY_UNDONE = 6          # 未完成待办 ≥6 视为高负载
HEAVY_RATIO = 0.95        # 当日能量排满 ≥95%
MODERATE_UNDONE = 3


def _task_density(todos: list[dict], day: date, days: int = 3) -> int:
    end = (day + timedelta(days=days - 1)).isoformat()
    start = day.isoformat()
    return sum(
        1 for t in todos
        if t.get("status") != "done"
        and kinds.valid_kind(t.get("kind")) == kinds.KIND_SCHEDULE
        and start <= str(t.get("date") or "") <= end
    )


def load_metrics(todos: list[dict], day: date | None = None,
                 plan: dict | None = None) -> dict:
    """评估当天/近期的负载水平，返回 low/medium/high + 依据。"""
    day = day or date.today()
    iso = day.isoformat()
    plan = plan or planner.plan_day(todos, day=day)
    undone = [
        t for t in todos
        if t.get("status") != "done"
        and kinds.valid_kind(t.get("kind")) == kinds.KIND_TODO
    ]
    sched = [
        t for t in todos
        if t.get("status") != "done"
        and kinds.valid_kind(t.get("kind")) == kinds.KIND_SCHEDULE
        and str(t.get("date") or "") == iso
    ]
    budget = plan.get("budget") or {}
    # 总负载包含既有日程/课程；未采纳的方案不能冒充真实占用。
    ratio = float(budget.get("committed_ratio", budget.get("planned_ratio")) or 0.0)
    scheduled = float(budget.get("scheduled_points") or 0.0)
    planned_energy = scheduled + float(budget.get("planned_points") or 0.0)
    horizon = (day + timedelta(days=2)).isoformat()
    relevant = [t for t in undone
                if not str(t.get("plan_defer_to") or "") > horizon
                and (not t.get("deadline") or str(t["deadline"]) <= horizon)]
    effort = sum(fields.normalize_task(t)["energy_cost"] for t in relevant)
    total = float(budget.get("total_points") or 0.0)
    # 待办耗能相对于未来三天实际空闲量，而不是仅按整池条数判忙。
    capacity = float(budget.get("available_points") or 0.0)
    from app.planner import energy
    prof = energy.load_profile()
    for offset in (1, 2):
        capacity += sum(r["points"] for r in planner.free_runs(
            todos, day + timedelta(days=offset), profile=prof,
            include_courses=budget.get("include_courses", True)))
    demand_ratio = effort / capacity if capacity else (1.0 if effort else 0.0)
    due_today = sum(fields.normalize_task(t)["energy_cost"] for t in relevant
                    if t.get("deadline") and str(t["deadline"]) <= iso)
    if total:
        ratio = max(ratio, (scheduled + due_today) / total)
    blocked = bool(plan.get("meta", {}).get("load_high"))
    reasons = []
    level = "low"
    if len(relevant) >= HEAVY_UNDONE or ratio >= HEAVY_RATIO or demand_ratio >= HEAVY_RATIO or blocked:
        level = "high"
    elif len(relevant) >= MODERATE_UNDONE or ratio >= 0.7 or demand_ratio >= 0.7:
        level = "medium"
    if len(relevant) >= MODERATE_UNDONE:
        reasons.append("近期需处理待办 {} 项（全部未完成 {} 项）".format(len(relevant), len(undone)))
    if ratio >= 0.7:
        reasons.append("今日占用及到期负荷约 {}%（已排 {} 点能量）".format(
            int(ratio * 100), round(planned_energy, 1)))
    if plan.get("meta", {}).get("load_high"):
        reasons.append("有硬线任务未排入当天方案")
    if demand_ratio >= 0.7:
        reasons.append("近期待办耗能约 {} 点，占未来三天空闲能量 {}%".format(
            effort, round(demand_ratio * 100)))
    if not reasons:
        reasons.append("安排宽松，还有空闲能量")
    return {
        "level": level,
        "day": iso,
        "undone_todos": len(undone),
        "near_term_todos": len(relevant),
        "near_term_energy": effort,
        "demand_ratio_3d": round(demand_ratio, 3),
        "today_schedules": len(sched),
        "density_3d": _task_density(todos, day),
        "planned_energy": round(planned_energy, 1),
        "planned_ratio": ratio,
        "reasons": reasons,
    }


def evaluate_intrusion(todos: list[dict], text: str,
                       day: date | None = None) -> dict:
    """把外部插入任务与当前负载对比，返回评估结果（纯规则，不落库）。"""
    day = day or date.today()
    try:
        parsed = parse_text(text)
    except Exception:
        parsed = {"ok": False}
    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error", "解析失败"),
                "load": None}
    norm = kinds.normalize_item(
        {k: parsed.get(k) for k in (
            "title", "category", "priority", "date", "time", "end_time",
            "duration_min", "location", "deadline", "deadline_time",
        )},
        raw=text,
    )
    if not norm.get("title"):
        return {"ok": False, "error": "无法从文本中提取事项", "load": None}
    plan = planner.plan_day(todos, day=day)
    metrics = load_metrics(todos, day=day, plan=plan)
    cost = fields.normalize_task({**norm, "kind": "todo"}).get("energy_cost", 2)
    level = metrics["level"]
    if level == "high":
        verdict = "heavy"
    elif level == "medium" and cost >= 3:
        verdict = "heavy"
    elif level == "medium":
        verdict = "moderate"
    else:
        verdict = "light"
    return {
        "ok": True,
        "title": norm["title"],
        "parsed": norm,
        "energy_cost": cost,
        "load": metrics,
        "verdict": verdict,
        "copy": reply_copy(verdict, metrics),
    }


def reply_copy(verdict: str, metrics: dict) -> str:
    """确定性兜底话术；verdict: light / moderate / heavy。"""
    undone = int((metrics or {}).get("undone_todos") or 0)
    energy = (metrics or {}).get("planned_energy") or 0.0
    if verdict == "light":
        return LIGHT_COPY
    if verdict == "moderate":
        return MODERATE_COPY.format(undone=undone, energy=round(energy, 1))
    return HEAVY_COPY.format(undone=undone, energy=round(energy, 1))
