# -*- coding: utf-8 -*-
"""LLM 文案/分类增强（对应需求文档 模块 2.2 / 3.3 / 4.1 的模型侧）。

- refusal_copy:    输入插入任务与当前负载概况 → 生成可直接复制的礼貌回复；
                   风格冷静客观，不出现“拖延”等负面词（失败返回 None）；
- ddl_classify:    输入任务文本 → 输出 {deadline_type, float_days}；
- risk_advice:     为低概率任务生成拆分/延后建议。

调用方负责兜底：AI 未启用、输出不合法或调用失败时返回 None，
由调用方改用规则文案（shield.reply_copy / risk.risk_copy / fields 推断）。
"""
from __future__ import annotations

import json

from app.planner import fields
from app.planner import shield as shield_mod
from app.planner import risk as risk_mod

REFUSAL_SYSTEM = """你是用户的“日程守门人”助手：当朋友/社团发来新的活动邀请时，
根据用户的真实负载情况，帮用户回一段礼貌、冷静、不制造焦虑的话。

硬性规则：
1. 只输出严格 JSON：{"copy": "可以直接复制的话术"}；
2. 语气自然像本人发的消息，不卑微、不生硬；不要出现“拖延/懒/拒绝不了”
   等负面词；婉拒时可以建议“线上参与 / 看回放 / 先标待定”；
3. 依据输入里的负载概况组织理由（几项待办、已排多少能量、近期日程密度），
   不要编造输入里没有的数字；
4. 控制在一到两句话，不要加表情符号堆砌，不要加引号外的说明。"""

DDL_SYSTEM = """你是截止日期分类器。判断一条校园任务属于：
- hard：硬线，错过即失效或代价大（考试、答辩、报名、缴费、提交系统等）；
- soft：软线，内容类可浮动 1~2 天（初稿、周报、心得、整理等）。
只输出严格 JSON：{"deadline_type": "hard|soft", "float_days": 1 或 2}。
无法判断时给 hard。不要输出任何其他文字。"""

RISK_SYSTEM = """你是“不制造焦虑”的计划助手。用户某项计划任务今天完成的把握不大
（完成概率低于 60%，并有历史依据）。请给出 1~2 句可执行建议：
- 具体：拆成更小的第一步（参考交付物），或挪到状态更好的时段，或调整完成标准；
- 冷静客观，禁止指责、禁止贩卖焦虑，禁止出现“拖延”字样。
只输出严格 JSON：{"copy": "建议文案"}。"""


def _ready(cfg) -> bool:
    from app.ai import gateway as ai_gateway
    return bool(cfg) and ai_gateway.is_ready(cfg)


def _chat(cfg, system: str, user: str, completion=None) -> str:
    from app.ai import gateway as ai_gateway
    call = completion or ai_gateway.chat_completion
    return call(cfg, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])


def _parse_copy(content: str) -> str | None:
    from app.ai import gateway as ai_gateway
    text = ai_gateway._strip_json_fence(content or "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    copy = str(data.get("copy") or "").strip()
    return copy[:300] or None


def refusal_copy(cfg: dict, ctx: dict, completion=None) -> str | None:
    """生成婉拒/待定话术；不可用或失败返回 None（调用方走规则兜底）。"""
    if not _ready(cfg):
        return None
    load = str(ctx.get("load") or "")
    undone = int(ctx.get("undone") or 0)
    energy = float(ctx.get("energy") or 0.0)
    reasons = "、".join(str(x) for x in (ctx.get("reasons") or [])) or "安排较满"
    background = str(ctx.get("background") or "").strip()
    background_lines = ("\n用户背景：\n" + background) if background else ""
    user = (
        "插入任务：{task}\n当前负载：{load}\n待办 {undone} 项，"
        "已排约 {energy} 点能量；理由：{reasons}\n风格偏好：{style}"
        "{background}\n\n请按系统规则输出 JSON。"
    ).format(
        task=str(ctx.get("task") or "未命名活动"),
        load=load,
        undone=undone,
        energy=energy,
        reasons=reasons,
        style=str(ctx.get("style") or "简洁、像本人说话"),
        background=background_lines,
    )
    try:
        return _parse_copy(_chat(cfg, REFUSAL_SYSTEM, user, completion))
    except Exception:
        return None


def ddl_classify(cfg: dict, text: str, completion=None) -> dict | None:
    """LLM 判断硬/软线与浮动天数；返回 None 时由调用方用规则推断。"""
    if not _ready(cfg):
        return None
    try:
        content = _chat(cfg, DDL_SYSTEM, "任务：" + (text or "")[:200], completion)
    except Exception:
        return None
    from app.ai import gateway as ai_gateway
    cleaned = ai_gateway._strip_json_fence(content or "")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    ddl = str(data.get("deadline_type") or "").strip().lower()
    if ddl not in fields.VALID_DEADLINE_TYPES:
        return None
    try:
        days = int(float(data.get("float_days") or 0))
    except (TypeError, ValueError):
        days = 1
    return {"deadline_type": ddl, "float_days": max(0, min(7, days))}


def risk_advice(cfg: dict, task: dict, when_label: str,
                completion=None) -> str | None:
    """低概率任务的 LLM 建议；失败返回 None（调用方用 risk.risk_copy 兜底）。"""
    if not _ready(cfg):
        return None
    user = (
        "任务：{title}\n交付物：{deliverable}\n时段：{when}\n"
        "完成概率约 {prob}%（低于 60%）\n请按系统规则输出 JSON。"
    ).format(
        title=str(task.get("title") or ""),
        deliverable=str(task.get("deliverable") or "未说明"),
        when=when_label,
        prob=int(round(float(task.get("probability") or 0.5) * 100)),
    )
    try:
        return _parse_copy(_chat(cfg, RISK_SYSTEM, user, completion))
    except Exception:
        return None
