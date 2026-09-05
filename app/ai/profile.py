# -*- coding: utf-8 -*-
"""用户画像与状态存储：AI Gateway 的“对人的记忆”层。

分层：
- UserState：短期状态（精力/事务负载/外部压力/置信度），会随时间衰减；
- UserProfile：长期画像（当前处境、偏好、概况摘要）+ 最新状态快照；
- 历史：data/user_state_history.json 保存每次状态更新的快照，供后续画曲线。

Phase 1 只负责采集与展示，画像暂不注入推理 Prompt。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

from app.paths import DATA_DIR

Level = Literal["low", "medium", "high", "unknown"]

PROFILE_FILE = os.path.join(DATA_DIR, "user_profile.json")
HISTORY_FILE = os.path.join(DATA_DIR, "user_state_history.json")

EMPTY_PROFILE = {
    "version": 1,
    "state": {
        "energy": "unknown",
        "task_load": "unknown",
        "external_pressure": "unknown",
        "confidence": 0.0,
        "updated_at": "",
    },
    "situation": "",
    "preferences": {},
    "summary": "",
    "updated_at": "",
}

_LOCK = threading.RLock()


@dataclass
class UserState:
    """用户当前状态快照（可随时间更新）。"""

    energy: Level = "unknown"
    task_load: Level = "unknown"
    external_pressure: Level = "unknown"
    confidence: float = 0.0
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "energy": self.energy,
            "task_load": self.task_load,
            "external_pressure": self.external_pressure,
            "confidence": round(max(0.0, min(1.0, float(self.confidence))), 3),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
        }


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 画像读写
# ---------------------------------------------------------------------------
def load_profile() -> dict:
    data = _read_json(PROFILE_FILE, None)
    if not isinstance(data, dict):
        data = {}
    merged = json.loads(json.dumps(EMPTY_PROFILE))
    merged.update({k: v for k, v in data.items() if k in merged})
    if isinstance(data.get("state"), dict):
        merged["state"].update({
            k: v for k, v in data["state"].items() if k in merged["state"]
        })
    return merged


def save_profile(profile: dict) -> dict:
    clean = json.loads(json.dumps(EMPTY_PROFILE))
    for k in clean:
        if k in profile and profile[k] is not None:
            clean[k] = profile[k]
    if isinstance(profile.get("state"), dict):
        for k in clean["state"]:
            if k in profile["state"] and profile["state"][k] is not None:
                clean["state"][k] = profile["state"][k]
    clean["updated_at"] = _now_iso()
    with _LOCK:
        _write_json(PROFILE_FILE, clean)
    return clean


def update_state(state: UserState | dict) -> dict:
    """更新最新状态，并追加一条历史快照。"""
    if isinstance(state, UserState):
        snapshot = state.to_dict()
    else:
        snapshot = dict(state or {})
        snapshot["updated_at"] = str(snapshot.get("updated_at") or _now_iso())
    profile = load_profile()
    for k in profile["state"]:
        if k in snapshot and snapshot[k] is not None:
            profile["state"][k] = snapshot[k]
    profile["state"]["updated_at"] = str(snapshot.get("updated_at") or _now_iso())
    with _LOCK:
        history = _read_json(HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        history.append(dict(profile["state"]))
        if len(history) > 2000:
            history = history[-2000:]
        _write_json(HISTORY_FILE, history)
        save_profile(profile)
    return profile


def reset_profile() -> dict:
    """清空画像与历史快照（保留长期记忆/证据由调用方决定）。"""
    with _LOCK:
        clean = json.loads(json.dumps(EMPTY_PROFILE))
        clean["updated_at"] = _now_iso()
        _write_json(PROFILE_FILE, clean)
        if os.path.exists(HISTORY_FILE):
            os.remove(HISTORY_FILE)
    return clean


def state_history(limit: int = 500) -> list:
    history = _read_json(HISTORY_FILE, [])
    return (history if isinstance(history, list) else [])[-limit:]


def latest_state() -> dict:
    return dict(load_profile().get("state") or {})
