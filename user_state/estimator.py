import json
from datetime import datetime

from model import UserState
from evidence import Evidence


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


def build_prompt(evidences: list[Evidence]) -> str:
    evidence_text = []

    for index, evidence in enumerate(evidences, start=1):
        evidence_text.append(
            f"{index}. [{evidence.source}] {evidence.content}"
        )

    return (
        SYSTEM_PROMPT
        + "\n\n当前 Evidence：\n"
        + "\n".join(evidence_text)
    )


def parse_state(result: str) -> UserState:
    data = json.loads(result)

    energy = data["energy"]
    task_load = data["task_load"]
    external_pressure = data["external_pressure"]
    confidence = float(data.get("confidence", 0.0))

    if energy not in VALID_LEVELS:
        raise ValueError("Invalid energy level")

    if task_load not in VALID_LEVELS:
        raise ValueError("Invalid task_load level")

    if external_pressure not in VALID_LEVELS:
        raise ValueError("Invalid external_pressure level")

    confidence = max(0.0, min(1.0, confidence))

    return UserState(
        energy=energy,
        task_load=task_load,
        external_pressure=external_pressure,
        confidence=confidence,
        updated_at=datetime.now(),
    )


def estimate_state(
    evidences: list[Evidence],
    gateway,
) -> UserState:

    if not evidences:
        raise ValueError("No evidence provided")

    prompt = build_prompt(evidences)

    result = gateway(prompt)

    return parse_state(result)
