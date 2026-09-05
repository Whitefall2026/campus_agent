# -*- coding: utf-8 -*-
"""事前推演与风险评估（对应需求文档 模块 3，确定性简化版）。

数据
----
- 历史依据来自 planner_events.json 的 done/rating 事件：每条带
  weekday(0~6)、时段 bucket、rating(easy/ok/tough)，用于聚合
  「星期几+时段」的历史完成率（数据不足时回退到默认值）；
- record_done 负责在用户勾选完成时记录（供模块 3.1）。

算法
----
- bucket_rate：按 (星期几, 上午/下午/晚上) 聚合并按 rating 加权；
- simulate：对某天的规划方案（plan.entries 按时间有序）跑 N 次
  简化蒙特卡洛（任务 i 依概率完成，未完成按半个时长向后续任务
  溢出，降低后续完成概率），统计每个任务的完成频率；
- 完成概率 < 60% 的任务标记 risk，并生成不制造焦虑的拆分/延后建议。

所有函数纯函数化，便于单测与命令行演示。
"""
from __future__ import annotations

import random
from datetime import date, datetime

from app.planner import store
from app.planner import fields

RISK_THRESHOLD = 0.60
DEFAULT_SIMS = 100

_BUCKET = (
    (7, 12, "morning"),
    (12, 18, "afternoon"),
    (18, 24, "evening"),
)
RATING_WEIGHT = {"easy": 1.0, "ok": 0.8, "tough": 0.5}
DEFAULT_RATE = {  # 无历史时的时段默认完成率
    "morning": 0.82, "afternoon": 0.75, "evening": 0.66,
}
BUCKET_LABEL = {"morning": "上午", "afternoon": "下午", "evening": "晚上"}
WEEKDAY_LABEL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

COPY_RISK = (
    "根据你过往{when}时段完成率约 {rate}% 的记录，这项任务今日完成有风险。"
    "建议先做小步尝试：拆成「{split_hint}」级别的子任务，"
    "或者把它提前到状态更好的时段；也可以降低这一步的完成标准。"
)


def bucket_of(hour: int | None) -> str:
    """按小时划分上午/下午/晚上（夜里归入晚上）。"""
    try:
        h = int(hour or 0) % 24
    except (TypeError, ValueError):
        h = 0
    for lo, hi, name in _BUCKET:
        if lo <= h < hi:
            return name
    return "evening"


def load_history_stats(events: list | None = None) -> dict:
    """聚合历史事件 → {(weekday, bucket): 加权完成率}。

    events 缺省时读 data/planner_events.json。
    """
    if events is None:
        events = store.load_events()
    buckets: dict[tuple, list] = {}
    for ev in events:
        if str(ev.get("type") or "") not in ("done", "rating"):
            continue
        try:
            wd = int(ev.get("weekday"))
            hour = int(ev.get("hour") or 0)
        except (TypeError, ValueError):
            continue
        bucket = bucket_of(hour)
        rating = str(ev.get("rating") or "ok").lower()
        weight = RATING_WEIGHT.get(rating, RATING_WEIGHT["ok"])
        buckets.setdefault((wd % 7, bucket), []).append(weight)
    out = {}
    for k, weights in buckets.items():
        out[k] = round(sum(weights) / len(weights), 3)
    return out


def base_rate(weekday: int, hour: int, stats: dict | None = None) -> float:
    """某任务的基础完成率：优先历史，其次时段默认。"""
    stats = stats or {}
    bucket = bucket_of(hour)
    key = (weekday % 7, bucket)
    if key in stats:
        return float(stats[key])
    return float(DEFAULT_RATE.get(bucket, 0.75))


def record_done(todo: dict, rating: str = "ok",
                hour: int | None = None,
                data_dir: str | None = None) -> None:
    """用户完成某事项时记录一条历史事件（供完成率统计）。"""
    h = hour
    if h is None:
        ts = fields.hm_to_min(str(todo.get("time") or ""))
        if ts is not None:
            h = ts // 60
        else:
            h = datetime.now().hour
    try:
        d = date.fromisoformat(str(todo.get("date") or ""))
    except ValueError:
        d = date.today()
    store.append_event({
        "type": "done",
        "task_id": str(todo.get("id") or ""),
        "title": str(todo.get("title") or ""),
        "date": d.isoformat(),
        "weekday": d.weekday(),
        "hour": h,
        "bucket": bucket_of(h),
        "rating": str(rating or "ok").strip().lower()[:5],
    }, data_dir)


# ---------------------------------------------------------------------------
# 蒙特卡洛（简化）
# ---------------------------------------------------------------------------
def _entry_modifiers(e: dict, plan: dict) -> float:
    """对基础完成率的调整项（确定性规则）。"""
    p = 0.0
    if e.get("tolerance"):
        p -= 0.08                    # 容差适配：状态可能吃紧
    start = fields.hm_to_min(e.get("start"))
    if start is not None and start >= 20 * 60:
        p -= 0.06                    # 20:00 后精力下滑
    if e.get("deadline_type") == "soft":
        p += 0.03                    # 软线压力小，反而更容易完成
    if int(e.get("energy_cost") or 1) >= 4:
        p -= 0.05
    ratio = (plan.get("budget") or {}).get("planned_ratio") or 0.0
    if ratio >= 0.9:
        p -= 0.05                    # 当日排得太满
    if plan.get("warnings"):
        p -= 0.02
    return p


def simulate(plan: dict, stats: dict | None = None, n: int = DEFAULT_SIMS,
             seed: int | None = None) -> list[dict]:
    """对 plan 的每个 entry 做简化蒙特卡洛，返回带概率的结果列表。

    假设：任务按 entries 顺序执行；某任务未完成时按半个时长的能量
    向后溢出，摊薄后续任务的完成概率。
    """
    stats = stats or {}
    entries = sorted(
        (list(plan.get("entries") or [])),
        key=lambda e: (str(e.get("start") or "99:99"), str(e.get("task_id") or "")),
    )
    rng = random.Random(seed)
    probs = []
    for e in entries:
        weekday = _weekday_of(plan.get("date"))
        hour = (fields.hm_to_min(e.get("start")) or 0) // 60
        p = base_rate(weekday, hour, stats) + _entry_modifiers(e, plan)
        p = max(0.03, min(0.97, p))
        dur_h = int(e.get("duration_min") or 60) / 60.0
        probs.append({"p": p, "dur": dur_h})

    counts = [0] * len(entries)
    for _ in range(max(1, int(n))):
        ov = 0.0
        for i, item in enumerate(probs):
            eff = max(0.02, item["p"] - 0.12 * ov)
            if rng.random() < eff:
                counts[i] += 1
                ov = max(0.0, ov - 0.25)
            else:
                ov += item["dur"] * 0.5
    total_ok = 0
    out = []
    for i, e in enumerate(entries):
        freq = counts[i] / max(1, int(n))
        out.append({
            "task_id": e.get("task_id"),
            "title": e.get("title"),
            "start": e.get("start"),
            "end": e.get("end"),
            "date": plan.get("date"),
            "probability": round(freq, 3),
            "risk": freq < RISK_THRESHOLD,
            "entry": e,
        })
        if freq >= RISK_THRESHOLD:
            total_ok += 1
    return out


def _weekday_of(date_s: str | None) -> int:
    try:
        return date.fromisoformat(str(date_s or "")).weekday()
    except ValueError:
        return date.today().weekday()


def risk_copy(e: dict, weekday: int | None = None,
              rate: float | None = None) -> str:
    """为低概率任务生成“不制造焦虑”的确定性建议文案。"""
    if weekday is None:
        weekday = _weekday_of(e.get("date"))
    when = "{}{}".format(
        WEEKDAY_LABEL[weekday % 7], BUCKET_LABEL.get(bucket_of(hour_of(e)), "该")
    )
    pct = int(round((rate if rate is not None else 0.5) * 100))
    deliverable = str(e.get("deliverable") or "").strip()
    split_hint = "只完成开头一小步"
    if deliverable and len(deliverable) <= 20:
        split_hint = "先完成「" + deliverable + "」的骨架版"
    return COPY_RISK.format(
        when=when, rate=pct, split_hint=split_hint,
    )


def hour_of(e: dict) -> int | None:
    m = fields.hm_to_min(e.get("start"))
    return (m // 60) if m is not None else None


def plan_with_risk(plan: dict, stats: dict | None = None,
                   n: int = DEFAULT_SIMS, seed: int | None = None) -> dict:
    """把风险评估结果并进 plan 的 entries（不修改入参），返回新 plan。

    每条 entry 增加 probability / risk / risk_copy（risk 时才有文案）。
    """
    plan = dict(plan)
    sims = simulate(plan, stats=stats, n=n, seed=seed)
    entries = []
    for s in sims:
        e = dict(s["entry"])
        e["probability"] = s["probability"]
        e["risk"] = bool(s["risk"])
        merged = {**s, "deliverable": e.get("deliverable")}
        e["risk_copy"] = risk_copy(merged) if s["risk"] else None
        entries.append(e)
    plan["entries"] = entries
    plan["risk_sim"] = {"n": max(1, int(n)), "seed": seed}
    return plan
