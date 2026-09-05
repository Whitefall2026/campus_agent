# -*- coding: utf-8 -*-
"""任务拆解层（对应需求文档 模块 1.1）。

- needs_decomposition: 判断一条复杂任务是否需要拆解（规则）；
- PROMPT / build_prompt: 让 LLM 输出 3~7 个里程碑子任务，每个子任务必须含
  name / deliverable / energy_cost(1~5) / deadline；
- parse_subtasks: 校验并清洗模型输出（缺失产出物、非法耗能一律丢弃），
  防止“写 200 字”这类过程性描述混入；
- ai_decompose: 走 AI Gateway 的完整调用入口；
- child_todo: 把合法子任务构造成任务池条目（parent_id 关联父任务）。

AI 未启用 / 调用失败 / 任务太简单时返回 None（不拆解），由调用方决定
“整块执行”或提示用户手动拆——沿用仓库“AI 失败自动退回规则”的惯例。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime

from app.planner import fields

DEFAULT_MIN_ENERGY = 4        # 耗能 ≥4 或时长 ≥120 分钟才值得拆
DEFAULT_MIN_DURATION = 120

# 拆解提示词（任务 1.1.1 的交付物）
SYSTEM_PROMPT = """你是一位克制、务实的任务规划助手，只做一件事：把“复杂任务”拆成 3~7 个
里程碑级子任务，不做评价、不给鸡汤。

硬性规则：
1. 每个子任务必须是里程碑：有明确产出物 deliverable（文件、图表、段落、数据、
   可交付成果…），禁止把过程性动作（“写 200 字”“看一小时视频”“翻资料”）
   当作子任务；
2. 只输出 3~7 条；父任务若本身很小（1 小时内能完成），不要拆，返回空数组；
3. energy_cost 是 1~5 的整数：1≈30 分钟轻量，5≈2.5 小时高强度；拆出来的子任务
   单个能量 ≤3，宁可多拆一步也不要出现一个 5 分巨块；
4. deadline 用 YYYY-MM-DD，无法估计就 null（子任务建议时间必须在父任务截止
   之前或同一天，若父任务无截止则 null 或合理建议）；
5. 只输出严格 JSON，不要 Markdown：{"subtasks":[{"name":"...",
   "deliverable":"...","energy_cost":1~5,"deadline":"YYYY-MM-DD 或 null"}]}
6. 无法拆或输入信息不足时返回 {"subtasks":[]}，不要编造。"""

# 结果清洗
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def build_prompt(todo: dict) -> str:
    t = fields.normalize_task(todo)
    dl = t.get("deadline")
    dl_t = t.get("deadline_time")
    deadline_desc = ""
    if dl:
        deadline_desc = "；截止日期 " + str(dl) + ((" " + str(dl_t)) if dl_t else "")
    progress = str(t.get("progress") or "").strip() or "尚未开始"
    return (
        "任务：{title}\n"
        "交付物期望：{deliverable}\n"
        "类别：{category}；预计耗能：{cost} 点{deadline}\n"
        "当前进度：{progress}\n\n"
        "请按系统规则拆成里程碑子任务。"
    ).format(
        title=t.get("title") or "",
        deliverable=t.get("deliverable") or "（未说明，请根据任务内容给出合理建议）",
        category=str(t.get("category") or "other"),
        cost=t.get("energy_cost"),
        deadline=deadline_desc,
        progress=progress,
    )


def needs_decomposition(todo: dict) -> bool:
    """规则预筛：值得拆的复杂任务才送 LLM。"""
    t = fields.normalize_task(todo)
    if t.get("parent_id") or t.get("status") == "done":
        return False
    if t.get("energy_cost", 0) >= DEFAULT_MIN_ENERGY:
        return True
    try:
        if int(t.get("duration_min") or 0) >= DEFAULT_MIN_DURATION:
            return True
    except (TypeError, ValueError):
        pass
    # 标题里明显的“大工程”词
    text = str(t.get("title") or "") + str(t.get("deliverable") or "")
    return any(k in text for k in ("论文", "毕设", "大作业", "项目", "策划", "复习计划"))


def parse_subtasks(content: str) -> list[dict]:
    """解析/清洗 LLM 返回的拆解结果；丢弃不满足约束的子任务。"""
    from app.ai import gateway as ai_gateway  # 复用 JSON 清理工具
    text = ai_gateway._strip_json_fence(content or "")
    try:
        import json
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    raw = data.get("subtasks") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        deliverable = str(it.get("deliverable") or "").strip()
        # 硬性约束：名字与产出物都必须具体，禁止“写X字”式的过程描述
        if not name or len(name) > 60:
            continue
        if not deliverable or len(deliverable) < 3:
            continue
        if re.search(r"(写|背|看|读|听|抄)\s*\d+\s*(字|页|分钟|小时|集|遍)", deliverable):
            continue
        cost = fields.clamp_energy(it.get("energy_cost"))
        if cost is None or cost > 3:
            continue  # 单个子任务 ≤3 点
        dl = str(it.get("deadline") or "").strip()
        if dl and not _DATE_RE.match(dl):
            dl = None
        out.append({
            "name": name,
            "deliverable": deliverable,
            "energy_cost": cost,
            "deadline": dl or None,
        })
    return out


def ai_decompose(cfg: dict, todo: dict, completion=None) -> list[dict] | None:
    """调用当前配置的 LLM 拆解；不可用/失败返回 None（不拆）。"""
    from app.ai import gateway as ai_gateway
    if not ai_gateway.is_ready(cfg):
        return None
    if not needs_decomposition(todo):
        return None
    call = completion or ai_gateway.chat_completion
    content = call(cfg, [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(todo)},
    ])
    subs = parse_subtasks(content)
    return subs or None


def child_todo(parent: dict, sub: dict) -> dict:
    """把一条合法子任务构造成任务池条目（kind=todo、带 parent_id）。"""
    p = fields.normalize_task(parent)
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "title": sub["name"],
        "deliverable": sub["deliverable"],
        "energy_cost": sub["energy_cost"],
        "duration_min": fields.DEFAULT_DURATION_BY_COST.get(sub["energy_cost"], 30),
        "kind": "todo",
        "status": "pending",
        "category": p.get("category") or "other",
        "priority": p.get("priority") or "medium",
        "deadline": sub.get("deadline") or p.get("deadline"),
        "deadline_time": p.get("deadline_time") if not sub.get("deadline") else None,
        "location": None,
        "raw": "拆解自「{}」".format(p.get("title")),
        "parent_id": p.get("id"),
        "id": uuid.uuid4().hex[:10],
        "created_at": now,
        "plan_defer_to": None,
    }


# ---------------------------------------------------------------------------
# 拆解结果缓存（供“先预览、后采纳”的接口使用，30 分钟内有效）
# ---------------------------------------------------------------------------
_CACHE_TTL_MIN = 30
_last_decomp: dict[str, dict] = {}


def cache_subtasks(todo_id: str, subtasks: list[dict]) -> None:
    if not todo_id:
        return
    _last_decomp[str(todo_id)] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "subs": [dict(s) for s in (subtasks or [])],
    }


def cached_subtasks(todo_id: str) -> list[dict] | None:
    entry = _last_decomp.get(str(todo_id or ""))
    if not entry:
        return None
    try:
        at = datetime.fromisoformat(str(entry.get("at") or ""))
        if (datetime.now() - at).total_seconds() > _CACHE_TTL_MIN * 60:
            _last_decomp.pop(str(todo_id), None)
            return None
    except ValueError:
        return None
    return [dict(s) for s in (entry.get("subs") or [])]
