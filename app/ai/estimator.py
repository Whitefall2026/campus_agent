# -*- coding: utf-8 -*-
"""用户状态评估（从根目录 user_state/ 收编，改为 app 包内模块）。

根据 Evidence 判断用户当前 energy / task_load / external_pressure。
Phase 1 保留能力但不接入对话推理；由 Phase 2 统一在 Gateway 内调用。
"""
from __future__ import annotations

import json
from datetime import datetime

from app.ai.evidence import Evidence
from app.ai.profile import UserState

VALID_LEVELS = {"low", "medium", "high"}

SYSTEM_PROMPT = """
你是一个用户状态分析器。

你的任务不是安排任务，也不是评价用户。
你的任务是根据提供的 Evidence 判断用户当前状态。

你需要判断三个维度：

1. energy
   用户当前可用精力。
   low / medium / high

2. task_load
   用户当前需要并行处理的事务负荷。
   low / medium / high

3. external_pressure
   来自考试、比赛、截止日期、重要活动等外部硬约束的压力。
   low / medium / high

注意：

- 不要把主观焦虑直接等同于 external_pressure。
- 不要凭空创造 Evidence 中没有的信息。
- 如果证据不足，降低 confidence。
- 不要给用户进行人格分析。
- 不要安排任务。
- 不要追求让用户的时间表被完全填满。

只返回 JSON，不要返回 Markdown。
JSON 格式：

{
    "energy": "low | medium | high",
    "task_load": "low | medium | high",
    "external_pressure": "low | medium | high",
    "confidence": 0.0
}
"""


def build_prompt(evidences: list) -> str:
    lines = []
    for index, ev in enumerate(evidences, start=1):
        if isinstance(ev, Evidence):
            lines.append(f"{index}. [{ev.source}] {ev.content}")
        elif isinstance(ev, dict):
            lines.append(f"{index}. [{ev.get('source', '?')}] {ev.get('content', '')}")
    return SYSTEM_PROMPT + "\n\n当前 Evidence：\n" + "\n".join(lines)


def parse_state(result: str) -> UserState:
    data = json.loads(result)
    energy = str(data.get("energy") or "unknown").lower()
    task_load = str(data.get("task_load") or "unknown").lower()
    pressure = str(data.get("external_pressure") or "unknown").lower()
    if energy not in VALID_LEVELS:
        raise ValueError("Invalid energy level")
    if task_load not in VALID_LEVELS:
        raise ValueError("Invalid task_load level")
    if pressure not in VALID_LEVELS:
        raise ValueError("Invalid external_pressure level")
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return UserState(
        energy=energy,
        task_load=task_load,
        external_pressure=pressure,
        confidence=confidence,
        updated_at=datetime.now(),
    )


def estimate_state(evidences: list, gateway) -> UserState:
    """gateway: callable(prompt: str) -> str（返回模型文本）。"""
    if not evidences:
        raise ValueError("No evidence provided")
    result = gateway(build_prompt(evidences))
    return parse_state(result)
