from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


Level = Literal["low", "medium", "high"]


@dataclass
class UserState:
    """
    用户当前状态。

    energy:
        当前可用精力，而不是情绪好坏。

    task_load:
        当前需要处理的并行事务负荷。

    external_pressure:
        来自考试、比赛、DDL、重要活动等外部硬约束的压力。

    confidence:
        AI 对当前状态判断的置信度。
    """

    energy: Level
    task_load: Level
    external_pressure: Level

    confidence: float = 0.0
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "energy": self.energy,
            "task_load": self.task_load,
            "external_pressure": self.external_pressure,
            "confidence": self.confidence,
            "updated_at": self.updated_at.isoformat(),
        }
