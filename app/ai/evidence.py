# -*- coding: utf-8 -*-
"""证据层：记录“观察到的用户事实”，不负责判断。

证据来源包括：对话、微信消息、日程行为、用户反馈（采纳/忽略/纠正）。
Phase 1 先采集与展示；状态评估（estimator）在 Phase 2 消费这些证据。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from app.paths import DATA_DIR

EVIDENCE_FILE = os.path.join(DATA_DIR, "user_evidence.json")
MAX_EVIDENCE = 800

_LOCK = threading.RLock()


@dataclass
class Evidence:
    """一条可审计的观察记录。"""

    source: str
    content: str
    created_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "content": self.content,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "metadata": self.metadata,
        }


def _read() -> list:
    try:
        with open(EVIDENCE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write(items: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = EVIDENCE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, EVIDENCE_FILE)


def add_evidence(source: str, content: str, metadata: dict | None = None) -> dict:
    ev = Evidence(
        source=source,
        content=str(content or "")[:500],
        metadata=dict(metadata or {}),
    )
    item = ev.to_dict()
    with _LOCK:
        items = _read()
        items.append(item)
        if len(items) > MAX_EVIDENCE:
            items = items[-MAX_EVIDENCE:]
        _write(items)
    return item


def recent_evidence(limit: int = 200) -> list:
    items = _read()
    return list(reversed(items[-limit:]))


def clear_evidence() -> int:
    with _LOCK:
        n = len(_read())
        _write([])
    return n


def evidence_count() -> int:
    return len(_read())
