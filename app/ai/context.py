# -*- coding: utf-8 -*-
"""统一上下文构建：把画像、记忆、日程等信息组装成 Gateway 可用的上下文。

Phase 1 先提供规划模式判断与画像摘要；Phase 2 起由 chat/planner
统一调用本模块，避免每个功能自己拼 Prompt。
"""
from __future__ import annotations

from app.ai import memory as user_memory
from app.ai import profile as user_profile
from app.ai import evidence as user_evidence


def build_planning_context(state) -> dict:
    """根据用户状态选择规划模式（收编自 user_state/context.py）。"""
    s = state.to_dict() if hasattr(state, "to_dict") else (state or {})
    context = {
        "energy": s.get("energy", "unknown"),
        "task_load": s.get("task_load", "unknown"),
        "external_pressure": s.get("external_pressure", "unknown"),
        "confidence": float(s.get("confidence") or 0.0),
    }
    if context["energy"] == "low":
        context["planning_mode"] = "protect_energy"
    elif context["task_load"] == "high":
        context["planning_mode"] = "reduce_load"
    elif context["external_pressure"] == "high":
        context["planning_mode"] = "protect_deadline"
    else:
        context["planning_mode"] = "normal"
    return context


def profile_summary() -> dict:
    """给前端展示的画像摘要（Phase 1：只展示，不注入推理）。"""
    profile = user_profile.load_profile()
    state = profile.get("state") or {}
    if not profile.get("updated_at") or not state.get("updated_at") \
            or all(v in (None, "", "unknown") for v in (
                state.get("energy"), state.get("task_load"),
                state.get("external_pressure"))):
        derived = user_profile.derive_from_planner()
        if derived:
            user_evidence.add_evidence(
                "planner",
                "根据日程/课程/精力数据自动生成初始画像：" + derived.get("situation", ""),
                {"kind": "cold_start"},
            )
            profile = user_profile.load_profile()
            state = profile.get("state") or {}
    memories = user_memory.list_memories()
    return {
        "state": state,
        "situation": str(profile.get("situation") or ""),
        "preferences": profile.get("preferences") or {},
        "summary": str(profile.get("summary") or ""),
        "memory_count": len(memories),
        "evidence_count": user_evidence.evidence_count(),
        "updated_at": str(profile.get("updated_at") or ""),
    }
