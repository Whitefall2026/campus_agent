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
from datetime import datetime, timedelta
from typing import Literal

from app.paths import DATA_DIR
from app.core.storage import load_todos

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


def derive_from_planner() -> dict | None:
    """用能量引擎/日程数据冷启动画像，不依赖对话条数。

    - 精力：当前时刻精力系数 + 近期轻松/吃力反馈；
    - 事务负载：未完成待办数 + 未来 7 天课程/日程密度；
    - 外部压力：未来 5 天内硬线截止/密集安排；
    - 处境：自动生成一句话概况。
    """
    now = datetime.now()
    todos = load_todos()
    try:
        from app.core import courses as course_mod
        course_events = course_mod.term_events()
    except Exception:
        course_events = []

    open_todos = [t for t in todos
                  if t.get("status") != "done"
                  and str(t.get("kind") or "") != "schedule"]
    pending_count = len(open_todos)
    today_iso = now.date().isoformat()

    # ---- 精力：当前时刻系数 + 近期反馈 ----
    hours = []
    events = []
    try:
        with open(os.path.join(DATA_DIR, "planner_profile.json"), "r",
                  encoding="utf-8") as f:
            hours = (json.load(f) or {}).get("hours") or []
    except (OSError, json.JSONDecodeError):
        pass
    try:
        with open(os.path.join(DATA_DIR, "planner_events.json"), "r",
                  encoding="utf-8") as f:
            events = json.load(f) or []
    except (OSError, json.JSONDecodeError):
        pass
    hour_coef = 0.5
    if isinstance(hours, list) and len(hours) == 24 and all(
        isinstance(v, (int, float)) for v in hours
    ):
        hour_coef = float(hours[now.hour])
    recent = [e for e in events if isinstance(e, dict)][-200:]
    tough = sum(1 for e in recent if e.get("rating") == "tough")
    easy = sum(1 for e in recent if e.get("rating") == "easy")
    if tough >= 3 and tough >= easy * 2:
        energy = "low"
    elif hour_coef < 0.4:
        energy = "low"
    elif hour_coef <= 0.9:
        energy = "medium"
    else:
        energy = "high"

    # ---- 事务负载 ----
    next7 = (now.date() + timedelta(days=7)).isoformat()
    load_events = sum(
        1 for e in course_events
        if e.get("date") and today_iso <= str(e["date"]) <= next7
    )
    if pending_count >= 7 or load_events >= 10:
        task_load = "high"
    elif pending_count >= 4 or load_events >= 6:
        task_load = "medium"
    else:
        task_load = "low"

    # ---- 外部压力：近 5 天硬线 / 密集截止 ----
    next5 = (now.date() + timedelta(days=5)).isoformat()
    hard = 0
    upcoming = 0
    for t in todos:
        dl = str(t.get("deadline") or "")
        if not dl or t.get("status") == "done":
            continue
        if today_iso <= dl <= next5:
            if str(t.get("deadline_type") or "").lower() == "hard" \
                    or not str(t.get("deadline_type") or ""):
                hard += 1
            else:
                upcoming += 1
    if hard >= 2 or (hard + upcoming) >= 4:
        external_pressure = "high"
    elif hard == 1 or upcoming >= 2:
        external_pressure = "medium"
    else:
        external_pressure = "low"

    situation_parts = []
    if pending_count:
        situation_parts.append(f"{pending_count} 项待办未完成")
    if load_events:
        situation_parts.append(f"未来一周约 {load_events} 次课程/日程")
    if hard:
        situation_parts.append(f"{hard} 项硬线截止在 5 天内")
    situation = "；".join(situation_parts) if situation_parts else "暂无突出压力"
    summary = "根据日程/课程与精力数据自动推导的初始画像。"
    profile = load_profile()
    if situation:
        profile["situation"] = situation
    profile["summary"] = summary
    profile["preferences"] = profile.get("preferences") or {}
    save_profile(profile)
    update_state(UserState(
        energy=energy,
        task_load=task_load,
        external_pressure=external_pressure,
        confidence=0.55,
        updated_at=now,
    ))
    return {
        "state": latest_state(),
        "situation": situation,
        "summary": summary,
    }


def maybe_refresh_state(minutes: float = 15.0, use_ai: bool = True) -> dict | None:
    """事件驱动的画像刷新（带最小间隔，避免频繁重算/烧 token）。

    - 超过 minutes 分钟没有刷新才执行；
    - AI 可用且证据足够时，先用证据做 AI 状态评估（estimator）；
    - 否则退回 derive_from_planner 的规则推导；
    - 顺带对近期证据做一次长期记忆沉淀（AI 或规则兜底）。
    """
    state = latest_state()
    last = None
    try:
        last_s = str(state.get("updated_at") or "")
        if last_s:
            last = datetime.fromisoformat(last_s)
    except ValueError:
        last = None
    if last is not None and (datetime.now() - last).total_seconds() < minutes * 60:
        return None

    try:
        from app.ai import evidence as _evidence_mod
        from app.ai import gateway as _gateway_mod
        from app.ai import estimator as _estimator_mod
    except Exception:
        _evidence_mod = _gateway_mod = _estimator_mod = None

    evidences = _evidence_mod.recent_evidence(limit=100) if _evidence_mod else []
    updated = False
    if use_ai and _gateway_mod is not None and len(evidences) >= 3:
        try:
            cfg = _gateway_mod.load_config()
            if _gateway_mod.is_ready(cfg):
                st = _estimator_mod.estimate_state(
                    evidences,
                    lambda prompt: _gateway_mod.chat_completion(
                        cfg, [{"role": "user", "content": prompt}]),
                )
                update_state(st)
                updated = True
        except Exception:
            updated = False
    if not updated:
        derive_from_planner()

    try:
        from app.ai import memory as _memory_mod
        cfg = _gateway_mod.load_config() if _gateway_mod is not None else None
        _memory_mod.consolidate_memories(evidences, cfg)
    except Exception:
        pass
    return {"refreshed": True, "state": latest_state()}
