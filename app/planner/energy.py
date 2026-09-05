# -*- coding: utf-8 -*-
"""用户精力曲线模型。

概念
----
- 精力系数：一天 24 个小时各有系数（上午约 1.2，下午约 0.8，晚间约 0.5，
  夜间为 0 不可排）。1 个能量点 = “系数 1.0 下的 30 分钟标准精力”；
- 时段能量 Y：某块空闲时段能提供的能量 = Σ(每 30 分钟格子的系数)；
- 校准：用户完成任务后反馈「轻松/吃力」（或系统观测到完成率变化）时，
  按指数移动平均微调对应小时系数，默认曲线只是冷启动初值。
"""
from __future__ import annotations

import threading

from app.planner import store

_LOCK = threading.RLock()

# 默认逐小时系数（索引 = 小时）。窗口外的时段（23:00~07:00）系数为 0，不排任务。
DEFAULT_HOUR_COEF = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,   # 0~6 点 睡眠
    0.5,                                 # 7 点  起床/早读（缓冲）
    1.2, 1.2, 1.2,                       # 8~10 上午黄金档
    1.0,                                 # 11 点
    0.5,                                 # 12 点 午休
    0.6,                                 # 13 点 午后缓冲
    1.0, 0.9, 0.9, 0.8,                  # 14~17 下午
    0.7, 0.7,                            # 18~19 傍晚
    0.5, 0.4,                            # 20~21 晚上
    0.2,                                 # 22 点 收尾
    0.0,                                 # 23 点后 不再排
]
COEF_MIN, COEF_MAX = 0.1, 1.8
EMA_RATE = 0.15          # 一次反馈更新的学习率
FEEDBACK_DELTA = {"easy": 0.10, "ok": 0.0, "tough": -0.10}
STATE_MULTIPLIER = {"low": 0.65, "medium": 1.0, "high": 1.15}

PLAN_DAY_START = 7 * 60    # 07:00
PLAN_DAY_END = 23 * 60     # 23:00（不含）


def default_profile() -> dict:
    return {"hours": list(DEFAULT_HOUR_COEF), "version": 1, "updated_at": None}


def load_profile(data_dir: str | None = None) -> dict:
    """读 profile；文件缺失/损坏时回退默认曲线。"""
    with _LOCK:
        prof = store.load_profile(data_dir)
        hours = list(prof.get("hours") or [])
        if len(hours) != 24 or not all(
            isinstance(v, (int, float)) for v in hours
        ):
            return default_profile()
        return {
            "hours": hours,
            "version": int(prof.get("version") or 1),
            "updated_at": prof.get("updated_at"),
        }


def save_profile(profile: dict, data_dir: str | None = None) -> dict:
    with _LOCK:
        return store.save_profile(profile, data_dir)


def clamp(v: float) -> float:
    return max(COEF_MIN, min(COEF_MAX, v))


def coefficient_at(hour_float: float, profile: dict | None = None) -> float:
    """某一时刻的精力系数（hour_float 可为小数，取所在整小时）。"""
    prof = profile or default_profile()
    hours = list(prof.get("hours") or DEFAULT_HOUR_COEF)
    try:
        h = int(float(hour_float)) % 24
    except (TypeError, ValueError):
        h = 0
    if h < 0 or h >= len(hours):
        return 0.0
    return float(hours[h])


def block_points(start_min: int, dur_min: int,
                 profile: dict | None = None) -> float:
    """[start_min, start_min+dur_min) 这块时段可提供的能量点数。

    每 30 分钟一个格子，格子能量 = 该小时系数；不足 30 分钟按比例折算。
    """
    prof = profile or default_profile()
    total = 0.0
    end = start_min + dur_min
    # 起点对齐到半小时格子
    cell = start_min - (start_min % 30)
    while cell < end:
        h = coefficient_at(cell / 60.0, prof)
        overlap = min(cell + 30, end) - max(cell, start_min)
        total += h * (overlap / 30.0)
        cell += 30
    return total


def available_total(profile: dict | None = None) -> float:
    """一天（07:00~23:00）无课程占用时的理论总能量（点数）。"""
    return sum(coefficient_at(h, profile) for h in range(PLAN_DAY_START // 60, PLAN_DAY_END // 60))


def profile_for_current_state(profile: dict | None, state: dict | None) -> dict:
    """将一次「此刻精力」自评临时应用到今天的曲线。

    长期曲线只由完成反馈校准；即时状态只影响当日建议，避免一次疲惫
    把用户的长期画像永久拉低。
    """
    base = profile or default_profile()
    level = str((state or {}).get("energy") or "unknown").lower()
    multiplier = STATE_MULTIPLIER.get(level, 1.0)
    hours = [round(float(v) * multiplier, 3) if float(v) > 0 else 0.0
             for v in (base.get("hours") or DEFAULT_HOUR_COEF)]
    return {**base, "hours": hours, "state_level": level,
            "state_multiplier": multiplier}


def record_feedback(hour_float, rating: str, profile: dict | None = None) -> dict:
    """按「轻松/吃力」反馈做一次 EMA 校准，返回更新后的 profile。"""
    prof = {k: list(v) if isinstance(v, list) else v for k, v in
            (profile or default_profile()).items()} or default_profile()
    if "hours" not in prof or len(prof["hours"]) != 24:
        prof = default_profile()
    hours = list(prof["hours"])
    delta = FEEDBACK_DELTA.get(str(rating or "").strip().lower())
    if delta is None:
        return prof
    h = int(float(hour_float)) % 24
    hours[h] = clamp(hours[h] + EMA_RATE * delta)
    prof["hours"] = hours
    return prof
