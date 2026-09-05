from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Evidence:
    """
    描述我们观察到的事实。

    Evidence 不负责判断用户状态。
    它只记录“发生了什么”。
    """

    source: str
    content: str

    created_at: datetime = field(default_factory=datetime.now)

    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }
