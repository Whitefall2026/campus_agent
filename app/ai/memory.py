# -*- coding: utf-8 -*-
"""长期记忆：把跨会话值得记住的用户信息按重要度保存。

与 Evidence（原始观察，可删）区分：memory 是经过整理/确认的结论，
例如“用户处在考试季”“用户喜欢简短直接的回答”，供后续画像与规划引用。
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime

from app.paths import DATA_DIR

MEMORY_FILE = os.path.join(DATA_DIR, "user_memory.json")
MAX_MEMORY = 200

_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("memories"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "memories": []}


def _save(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEMORY_FILE)


def list_memories() -> list:
    return list(_load().get("memories") or [])


def add_memory(content: str, importance: float = 0.5,
               category: str = "preference",
               kind: str | None = None) -> dict:
    """写入一条长期记忆；同类别同内容时更新而不是重复堆积。"""
    content = str(content or "").strip()[:300]
    if not content:
        return {}
    if kind not in ("preference", "personality"):
        kind = "personality" if "personality" in str(category) else "preference"
    importance = max(0.0, min(1.0, float(importance or 0.5)))
    item = {
        "id": uuid.uuid4().hex[:10],
        "category": category,
        "kind": kind,
        "content": content,
        "importance": importance,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    with _LOCK:
        data = _load()
        memories = data["memories"]
        for m in memories:
            if m.get("category") == category and m.get("content") == content:
                m.update({
                    "importance": importance,
                    "updated_at": _now_iso(),
                })
                _save(data)
                return m
        memories.append(item)
        memories.sort(key=lambda m: m.get("importance", 0), reverse=True)
        if len(memories) > MAX_MEMORY:
            data["memories"] = memories[:MAX_MEMORY]
        _save(data)
    return item


def remember_preference(content: str, importance: float = 0.5,
                        category: str = "preference") -> dict:
    return add_memory(content, importance=importance,
                      category=category, kind="preference")


def remember_personality(content: str, importance: float = 0.5,
                         category: str = "personality") -> dict:
    return add_memory(content, importance=importance,
                      category=category, kind="personality")


def clear_memories() -> int:
    with _LOCK:
        n = len(_load().get("memories") or [])
        _save({"version": 1, "memories": []})
    return n


# ---------------------------------------------------------------------------
# 记忆沉淀：从对话/反馈证据里提炼 preference / personality
# ---------------------------------------------------------------------------
_PREF_RE = re.compile(
    r"(?:我(?:平时|一般|通常)?(?:更|比较)?(?:喜欢|习惯|倾向(?:于)?|希望|"
    r"愿意|不想|不喜欢|讨厌|抗拒)\s*[，,：: ]*([^，。！？!?；;]{2,40}))"
)
_PERSONALITY_RE = re.compile(
    r"(?:我是(?:一个|个)?|我这个人|我属于|我性格|我比较|我偏)\s*"
    r"([^，。！？!?；;]{2,40})"
)


def extract_memory_candidates(evidences: list) -> list:
    """规则兜底：从证据文本里找稳定的偏好/性格陈述，返回候选 dict 列表。"""
    out = []
    seen = set()
    for ev in evidences:
        content = str((ev if isinstance(ev, str) else ev.get("content") or "") or "")
        if not content or content.startswith(("帮我", "请帮我", "记得")):
            continue
        for pattern, kind in ((_PREF_RE, "preference"),
                              (_PERSONALITY_RE, "personality")):
            for m in pattern.finditer(content):
                cand = m.group(1).strip()
                if len(cand) < 2:
                    continue
                key = (kind, cand)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "content": cand,
                    "kind": kind,
                    "category": kind,
                    "importance": 0.35,
                })
    return out


def parse_ai_memories(content: str) -> list:
    """解析 AI 输出的候选记忆 JSON。"""
    from app.ai import gateway as _gw
    text = _gw._strip_json_fence(content or "")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("memories 不是 JSON 对象")
    raw = data.get("memories")
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        cand = str(it.get("content") or "").strip()[:300]
        kind = str(it.get("kind") or "preference").strip().lower()
        if kind not in ("preference", "personality"):
            kind = "preference"
        if len(cand) < 2:
            continue
        try:
            importance = max(0.0, min(1.0, float(it.get("importance") or 0.5)))
        except (TypeError, ValueError):
            importance = 0.5
        out.append({
            "content": cand,
            "kind": kind,
            "category": kind,
            "importance": importance,
        })
    return out


_CONSOLIDATE_SYSTEM = """你是长期记忆整理助手。下面是用户最近说过的原话/行为证据。
只提炼“稳定的偏好或性格特征”（例如：喜欢简短直接的回答、倾向上午深度工作、
不喜欢被临时塞事、性格直接），不要提炼一次性情绪或具体日程。
宁缺毋滥；没有就返回空数组。只输出严格 JSON：
{"memories":[{"content":"一句稳定特征","kind":"preference|personality","importance":0到1}]}"""


def consolidate_memories(evidences: list | None = None,
                         cfg: dict | None = None) -> dict:
    """把近期证据沉淀为长期记忆；AI 可用时让模型判断，否则规则兜底。"""
    if evidences is None:
        try:
            from app.ai import evidence as _ev_mod
            evidences = _ev_mod.recent_evidence(limit=100)
        except Exception:
            evidences = []
    evidences = list(evidences or [])
    if not evidences:
        return {"added": 0, "candidates": 0, "method": "none"}

    candidates = []
    method = "rule"
    try:
        from app.ai import gateway as _gw
        if cfg is not None and _gw.is_ready(cfg):
            lines = []
            for i, ev in enumerate(evidences[:80], start=1):
                content = str(ev.get("content") or "")[:240] \
                    if isinstance(ev, dict) else str(ev or "")[:240]
                src = ev.get("source", "?") if isinstance(ev, dict) else "?"
                lines.append(f"{i}. [{src}] {content}")
            text = _gw.chat_completion(cfg, [
                {"role": "system", "content": _CONSOLIDATE_SYSTEM},
                {"role": "user", "content": "证据：\n" + "\n".join(lines)},
            ])
            candidates = parse_ai_memories(text)
            method = "ai"
    except Exception:
        candidates = []
    if not candidates:
        candidates = extract_memory_candidates(evidences)
        method = "rule" if method != "ai" else "rule-fallback"
    added = 0
    for cand in candidates:
        if add_memory(cand["content"], importance=cand["importance"],
                      category=cand["category"], kind=cand["kind"]):
            added += 1
    return {"added": added, "candidates": len(candidates), "method": method}
