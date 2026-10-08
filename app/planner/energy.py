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

import math
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

PLAN_DAY_START = 7 * 60    # 07:00
PLAN_DAY_END = 23 * 60     # 23:00（不含）


def default_profile() -> dict:
    return {"hours": list(DEFAULT_HOUR_COEF), "version": 1, "updated_at": None}


def load_profile(data_dir: str | None = None) -> dict:
    """读 profile；文件缺失/损坏时回退默认曲线。"""
    with _LOCK:
        prof = store.load_profile(data_dir)
        hours = prof.get("hours")
        if not _valid_hours(hours):
            return default_profile()
        return {
            "hours": hours,
            "version": 1,
            "updated_at": prof.get("updated_at"),
        }


def save_profile(profile: dict, data_dir: str | None = None) -> dict:
    with _LOCK:
        return store.save_profile(profile, data_dir)


def clamp(v: float) -> float:
    return max(COEF_MIN, min(COEF_MAX, v))


def _valid_hours(hours) -> bool:
    return isinstance(hours, list) and len(hours) == 24 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        and math.isfinite(v) and 0 <= v <= COEF_MAX for v in hours)


def coefficient_at(hour_float: float, profile: dict | None = None) -> float:
    """某一时刻的精力系数（hour_float 可为小数，取所在整小时）。"""
    prof = profile or default_profile()
    hours = prof.get("hours")
    if not _valid_hours(hours):
        hours = DEFAULT_HOUR_COEF
    try:
        value = float(hour_float)
        h = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not 0 <= value < 24 or h < PLAN_DAY_START // 60 or h >= PLAN_DAY_END // 60:
        return 0.0
    return float(hours[h])


def block_points(start_min: int, dur_min: int,
                 profile: dict | None = None) -> float:
    """[start_min, start_min+dur_min) 这块时段可提供的能量点数。

    每 30 分钟一个格子，格子能量 = 该小时系数；不足 30 分钟按比例折算。
    """
    prof = profile or default_profile()
    total = 0.0
    end = min(24 * 60, start_min + max(0, dur_min))
    start_min = max(0, start_min)
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
    return block_points(PLAN_DAY_START, PLAN_DAY_END - PLAN_DAY_START, profile)


def record_feedback(hour_float, rating: str, profile: dict | None = None,
                    duration_min: int = 30) -> dict:
    """按「轻松/吃力」反馈做一次 EMA 校准，返回更新后的 profile。"""
    prof = {k: list(v) if isinstance(v, list) else v for k, v in
            (profile or default_profile()).items()} or default_profile()
    if not _valid_hours(prof.get("hours")):
        prof = default_profile()
    hours = list(prof["hours"])
    delta = FEEDBACK_DELTA.get(str(rating or "").strip().lower())
    if delta is None:
        return prof
    try:
        start = float(hour_float) * 60
        duration = max(1, float(duration_min))
    except (TypeError, ValueError, OverflowError):
        return prof
    if not math.isfinite(start) or not math.isfinite(duration) or not 0 <= start < 1440:
        return prof
    # 一次反馈按任务覆盖的小时分配权重，避免长任务只校准起始小时。
    # 睡眠和用户设为不可用的零系数时段保持为零。
    for h in range(PLAN_DAY_START // 60, PLAN_DAY_END // 60):
        overlap = max(0, min((h + 1) * 60, start + duration) - max(h * 60, start))
        if overlap and hours[h] > 0:
            hours[h] = clamp(hours[h] + EMA_RATE * delta * overlap / duration)
    prof["hours"] = hours
    prof["updated_at"] = None  # 保存时生成新的校准时间。
    return prof
