# -*- coding: utf-8 -*-
"""规划字段归一化与推断。

给任务池中的 todo 补上「基于处境的推理与规划」所需字段：
- energy_cost     耗能 1~5（1≈半小时轻量，5≈2.5 小时高强度）
- duration_min    实际占用时长（缺省按 energy_cost×30 估算）
- deliverable     明确产出物（缺省空，由用户/LLM 补充）
- deadline_type   hard（硬线，不可延期）/ soft（软线，可浮动 1~2 天）
- ddl_float_days  软线允许顺延的天数（硬线恒为 0）
- parent_id       子任务指向父任务 id
- plan_defer_to   「明日优先」顺延目标日期（YYYY-MM-DD 或 None）

约定：energy_cost 采用与时段能量一致的单位——1 个耗能点 ≈
“系数 1.0 下的 30 分钟标准精力”。后端始终给出可推断的默认值，
用户或 LLM 可在前端/接口里显式覆盖。
"""
from __future__ import annotations

import re

VALID_STATUS = {"pending", "done", "deferred"}
VALID_DEADLINE_TYPES = {"hard", "soft"}
VALID_ENERGY = {1, 2, 3, 4, 5}
DEFAULT_DURATION_BY_COST = {1: 30, 2: 60, 3: 90, 4: 120, 5: 150}
DEFAULT_FLOAT_DAYS = 2        # 软线默认可顺延 1~2 天
HARD_FLOAT_DAYS = 0

# 硬线信号：不可延期 / 错过即失效
_HARD_WORDS = (
    "考试", "期末考", "答辩", "面试", "报名截止", "截止报名", "缴费", "交学费",
    "交报名费", "选课", "抢课", "注册", "体检", "签证", "证件", "办证", "退宿",
    "离校", "复试", "提交系统", "系统提交", "线上提交", "演出", "比赛", "现场",
    "典礼", "军训", "复查", "返校", "开学", "报到", "领取毕业证", "deadline",
    "ddl",
)
# 软线信号：内容类、可浮动 1~2 天的产出
_SOFT_WORDS = (
    "初稿", "草稿", "一稿", "二稿", "修改稿", "周报", "月报", "日报", "心得",
    "总结", "复习", "预习", "笔记", "整理", "大纲", "计划书", "初版", "排版",
    "推文", "推送", "ppt初稿", "素材", "收集", "准备材料", "采购", "买", "取件",
    "打印", "复印", "收拾", "打扫", "练", "背单词", "阅读",
)
_HARD_RE = re.compile("|".join(re.escape(w) for w in _HARD_WORDS))
_SOFT_RE = re.compile("|".join(re.escape(w) for w in _SOFT_WORDS))

# 类别 → 无时长信息时的默认耗能
COST_BY_CATEGORY = {
    "exam": 4, "homework": 3, "deadline": 3, "meeting": 2,
    "activity": 2, "social": 1, "health": 1, "class": 3, "other": 2,
}


def hm_to_min(hm: str | None) -> int | None:
    """'HH:MM' → 当天第几分钟；非法输入返回 None。"""
    if not hm:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hm).strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def min_to_hm(total: int) -> str:
    total = int(total) % (24 * 60)
    return "%02d:%02d" % (total // 60, total % 60)


def clamp_energy(v) -> int:
    """把任意值规整到 1~5；无意义值返回 None。"""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if n in VALID_ENERGY else None


def infer_energy_cost(t: dict) -> int:
    """根据时长/类别/优先级给出缺省耗能。"""
    dur = t.get("duration_min")
    if isinstance(dur, (int, float)) and dur > 0:
        # 30 分钟 ≈ 1 点；高强度可由用户/LLM 上调
        return max(1, min(5, int(round(float(dur) / 30))))
    base = COST_BY_CATEGORY.get(str(t.get("category") or "other"), 2)
    prio = str(t.get("priority") or "medium")
    if prio == "high":
        base += 1
    elif prio == "low":
        base -= 1
    return max(1, min(5, base))


def infer_deadline_type(t: dict) -> str:
    """从标题/原文推断硬线还是软线；无法判断时按“硬线”保守处理。"""
    text = " ".join(str(x) for x in (
        t.get("title"), t.get("raw"), t.get("deliverable"),
    ) if x)
    if _SOFT_RE.search(text) and not _HARD_RE.search(text):
        return "soft"
    return "hard"


def normalize_task(t: dict) -> dict:
    """返回补全了规划字段的副本（不改原对象）。缺失字段一律给出默认值。"""
    out = dict(t or {})
    # 耗能
    cost = clamp_energy(out.get("energy_cost"))
    if cost is None:
        cost = infer_energy_cost(out)
    out["energy_cost"] = cost
    # 时长
    dur = out.get("duration_min")
    try:
        dur = int(float(dur)) if dur not in (None, "") else None
    except (TypeError, ValueError):
        dur = None
    out["duration_min"] = dur if dur and dur > 0 else DEFAULT_DURATION_BY_COST[cost]
    # 产出物（显式 None 视为未提供，保持 None 由上层填充）
    if "deliverable" not in out:
        out["deliverable"] = None
    # 截止类型
    ddl_type = str(out.get("deadline_type") or "").strip().lower()
    if ddl_type not in VALID_DEADLINE_TYPES:
        ddl_type = infer_deadline_type(out)
    out["deadline_type"] = ddl_type
    # 软线浮动天数：未显式提供时软线默认 2 天；硬线恒为 0
    if ddl_type == "soft":
        if "ddl_float_days" in out and out.get("ddl_float_days") not in (None, ""):
            try:
                float_days = int(float(out.get("ddl_float_days")))
            except (TypeError, ValueError):
                float_days = DEFAULT_FLOAT_DAYS
        else:
            float_days = DEFAULT_FLOAT_DAYS
        out["ddl_float_days"] = max(0, min(7, float_days))
    else:
        out["ddl_float_days"] = HARD_FLOAT_DAYS
    # 状态归一（保留用户自定义扩展状态，但必须能被“完成”语义识别）
    status = str(out.get("status") or "pending").strip().lower()
    out["status"] = status if status in VALID_STATUS else "pending"
    # 父任务
    out["parent_id"] = out.get("parent_id") or None
    # 明日优先标记
    out["plan_defer_to"] = str(out.get("plan_defer_to") or "").strip() or None
    return out


def is_deferred(t: dict) -> bool:
    return str(t.get("status") or "").lower() == "deferred"
