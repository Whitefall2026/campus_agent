# -*- coding: utf-8 -*-
"""AI 规划引擎：把“未排期的待办”放进用户时间轴的空档。

输入：待办（优先级/截止）、近两周已有日程与课程（占位）、用户画像状态。
输出：结构化排期建议 [{id, date, time, end_time, reason}]，由用户确认后应用。

实现策略：
- 优先调用当前配置的 AI 服务商做整体规划（理解优先级与情境）；
- 任何一条建议都会与“已有日程 + 课程表”做冲突校验，不合法即退回规则算法；
- AI 不可用/超时/输出格式错误时，整份退回规则规划，保证功能始终可用。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta

from app.ai import gateway as ai_gateway
from app.ai import profile as user_profile
from app.ai import evidence as user_evidence
from app.ai import memory as user_memory
from app.core import kinds
from app.core import courses as course_mod
from app.core.scheduler import SUGGEST_SLOTS, end_time_of
from app.core.storage import load_todos
from app.paths import DATA_DIR

PLAN_DAYS = 14
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

PLAN_SYSTEM_PROMPT = """你是「校园管家」的规划助手。用户有一批还没有排期的待办，
你要把它们安排进未来两周时间轴的空档里，像真人助理一样考虑优先级、截止时间和节奏。

输入包含：
- 待办列表（id/title/priority/deadline/deadline_time）
- 未来日期里已经被占用的时段（已有日程 + 课程）
- 用户当前状态（若有；精力低时不要把一天排太满）

规则：
1. 每条待办给一个建议日期 date（YYYY-MM-DD）与 time/end_time（HH:MM）；
2. 只能放在“占用列表”之外的空档；建议时段从 09:00-21:00 之间选择；
3. 有截止日期的必须安排在截止当天或之前；即将截止的优先安排；
4. 高优先级提前，低优先级靠后；同一天不要排得太满（精力低时尤其如此）；
5. 今天是可选日期，但不能安排已经过去的时间；
6. 不要编造待办 id，也不要漏掉任何一条。

只输出严格 JSON，不要 Markdown：
{"summary":"给用户的一句话总结","plan":[{"id":"待办id","date":"YYYY-MM-DD","time":"HH:MM","end_time":"HH:MM","reason":"一句话理由"}]}
"""


def _now():
    return datetime.now()


def _unplanned_todos(todos: list) -> list:
    out = []
    for t in todos:
        if t.get("status") == "done":
            continue
        if kinds.valid_kind(t.get("kind")) != kinds.KIND_TODO:
            continue
        if t.get("date"):
            continue
        out.append(t)
    return out


def _busy_map(days: int = PLAN_DAYS) -> dict:
    """返回 {date_iso: [(start, end), ...]}，含已有日程与课程。"""
    start = _now().date()
    busy = {}
    todos = load_todos()
    for t in todos:
        if t.get("status") == "done":
            continue
        if kinds.valid_kind(t.get("kind")) != kinds.KIND_SCHEDULE:
            continue
        if not t.get("date") or not t.get("time"):
            continue
        busy.setdefault(t["date"], []).append((t["time"], end_time_of(t)))
    try:
        for ev in course_mod.term_events():
            if ev.get("date") and ev.get("time"):
                busy.setdefault(ev["date"], []).append((ev["time"], ev["end_time"]))
    except Exception:
        pass
    out = {}
    for i in range(days):
        iso = (start + timedelta(days=i)).isoformat()
        out[iso] = sorted(busy.get(iso, []))
    return out


def _clash(start: str, end: str, intervals) -> bool:
    return any(start < e and s < end for s, e in intervals)


def _deadline_key(t) -> str:
    return str(t.get("deadline") or "") or "9999-99-99"


def _priority_rank(t) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(t.get("priority"), 1)


def _rule_choice(t, busy: dict, start_date: date) -> dict | None:
    """为单条待办找规则空档。返回 {date,time,end_time,reason} 或 None。"""
    deadline = str(t.get("deadline") or "").strip()
    today = _now().date()
    first_day = start_date
    last_day = today + timedelta(days=PLAN_DAYS - 1)
    if deadline and DATE_RE.match(deadline):
        try:
            dl = date.fromisoformat(deadline)
            if dl < today:
                last_day = today
            elif dl < last_day:
                last_day = dl
        except ValueError:
            pass
    if last_day < first_day:
        first_day = last_day
    now_hm = _now().strftime("%H:%M")
    try:
        dur = int(t.get("duration_min") or 60)
        dur = max(15, dur)
    except (TypeError, ValueError):
        dur = 60
    span = (last_day - first_day).days
    for i in range(span + 1):
        d = first_day + timedelta(days=i)
        iso = d.isoformat()
        intervals = busy.get(iso, [])
        for slot in SUGGEST_SLOTS:
            if d == today and slot <= now_hm:
                continue
            end = end_time_of({"time": slot, "duration_min": dur})
            if end and not _clash(slot, end, intervals):
                if deadline and iso <= deadline:
                    reason = f"截止日当天，安排最早的可用空档" if iso == deadline \
                        else f"在截止（{deadline}）前安排，当天该时段空闲"
                else:
                    reason = "该时段空闲，先安排这条待办"
                return {"date": iso, "time": slot, "end_time": end, "reason": reason}
    return None


def _context_text(todos: list, busy: dict, state: dict) -> str:
    today = _now().date()
    lines = [f"今天：{today.isoformat()}（周{'一二三四五六日'[today.weekday()]}）"]
    state_lines = []
    for k, label in (("energy", "精力"), ("task_load", "事务负载"),
                     ("external_pressure", "外部压力")):
        v = state.get(k)
        if v and v != "unknown":
            state_lines.append(f"{label}={v}")
    if state_lines:
        lines.append("用户状态：" + "、".join(state_lines))
    lines.extend(_energy_context_lines())
    lines.append("\n待排期待办：")
    for t in todos:
        dl = t.get("deadline") or ""
        dl_t = t.get("deadline_time") or ""
        lines.append(
            f"- id={t.get('id')} title={t.get('title')} priority={t.get('priority')}"
            + (f" deadline={dl} {dl_t}" if dl else "")
        )
    lines.append("\n未来占用时段：")
    for iso in sorted(busy):
        if busy[iso]:
            lines.append(f"{iso}: " + ", ".join(f"{s}-{e}" for s, e in busy[iso]))
    return "\n".join(lines)


def _energy_context_lines() -> list:
    """读取队友规划引擎的精力曲线与反馈事件，作为 AI 排期的参考数据。

    数据文件可能尚不存在（引擎未运行过），此时返回空列表即可。
    """
    out = []
    profile_path = os.path.join(DATA_DIR, "planner_profile.json")
    events_path = os.path.join(DATA_DIR, "planner_events.json")
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = json.load(f)
        hours = profile.get("hours") if isinstance(profile, dict) else None
        if isinstance(hours, list) and len(hours) == 24 and all(
            isinstance(v, (int, float)) for v in hours
        ):
            peak = max(range(24), key=lambda i: hours[i])
            if hours[peak] > 0:
                out.append(
                    "精力曲线：{} 点前后是高峰（系数 {:.2f}），"
                    "8-10 点均值 {:.2f}，深夜不排。".format(
                        peak, hours[peak],
                        sum(hours[8:11]) / 3,
                    )
                )
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        with open(events_path, "r", encoding="utf-8") as f:
            events = json.load(f)
        if isinstance(events, list) and events:
            recent = events[-200:]
            counts = {}
            for ev in recent:
                kind = str((ev or {}).get("type") or (ev or {}).get("kind") or "?")
                counts[kind] = counts.get(kind, 0) + 1
            total = sum(counts.values())
            if total:
                summary = "、".join(f"{k}×{n}" for k, n in sorted(counts.items()))
                out.append(f"近期规划反馈事件共 {total} 条：{summary}。")
    except (OSError, ValueError, TypeError):
        pass
    return out


def _parse_ai_plan(content: str):
    text = ai_gateway._strip_json_fence(content or "")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("plan 不是 JSON 对象")
    plan = data.get("plan") if isinstance(data.get("plan"), list) else []
    return str(data.get("summary") or "").strip(), plan


def _norm_time(v):
    v = str(v or "").strip()
    return v if TIME_RE.match(v) else None


def _safe_plan_item(raw: dict, t, busy: dict, start_date: date) -> dict | None:
    """校验 AI 建议；不合法返回 None（调用方退回规则）。"""
    d = str(raw.get("date") or "").strip()
    s = _norm_time(raw.get("time"))
    e = _norm_time(raw.get("end_time"))
    if not (d and s and e and DATE_RE.match(d)):
        return None
    if e <= s:
        return None
    try:
        dd = date.fromisoformat(d)
    except ValueError:
        return None
    if not (start_date <= dd <= start_date + timedelta(days=PLAN_DAYS - 1)):
        return None
    if dd == _now().date() and s <= _now().strftime("%H:%M"):
        return None
    if _clash(s, e, busy.get(d, [])):
        return None
    deadline = str(t.get("deadline") or "")
    if deadline and DATE_RE.match(deadline) and d > deadline:
        return None
    return {
        "id": t["id"],
        "date": d,
        "time": s,
        "end_time": e,
        "reason": str(raw.get("reason") or "")[:120],
    }


def plan_open_todos() -> dict:
    """规划全部未排期待办，返回建议列表（AI 优先 + 规则兜底）。"""
    todos = _unplanned_todos(load_todos())
    if not todos:
        return {"ok": True, "method": "none", "summary": "当前没有需要排期的待办",
                "items": []}
    todos.sort(key=lambda t: (_deadline_key(t), _priority_rank(t),
                              str(t.get("title") or "")))
    busy = _busy_map()
    start_date = _now().date()
    state = user_profile.latest_state() or {}
    method = "rule"
    ai_result = None
    cfg = ai_gateway.load_config()
    if ai_gateway.is_ready(cfg):
        try:
            content = ai_gateway.chat_completion(cfg, [
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": _context_text(todos, busy, state)},
            ])
            summary, plan = _parse_ai_plan(content)
            by_id = {str(t.get("id")): t for t in todos}
            ai_result = []
            seen = set()
            for raw in plan:
                if not isinstance(raw, dict):
                    continue
                t = by_id.get(str(raw.get("id") or ""))
                if t is None or str(t["id"]) in seen:
                    continue
                item = _safe_plan_item(raw, t, busy, start_date)
                if item:
                    ai_result.append(item)
                    seen.add(str(t["id"]))
            if ai_result:
                method = "ai"
        except Exception:
            ai_result = None

    items = []
    ai_by_id = {str(x["id"]): x for x in ai_result or []}
    for t in todos:
        tid = str(t["id"])
        meta = {"title": t.get("title"), "deadline": t.get("deadline")}
        if tid in ai_by_id:
            items.append({**ai_by_id[tid], **meta})
            continue
        choice = _rule_choice(t, busy, start_date)
        if choice:
            items.append({"id": t["id"], **choice, **meta})
        else:
            items.append({
                "id": t["id"], "date": None, "time": None, "end_time": None,
                "reason": "未来两周空档不足，建议手动安排或先减负",
                **meta,
            })
    planned = sum(1 for x in items if x.get("date"))
    if method == "ai":
        summary = f"我按你的空闲时段和截止时间把待办排好了：{planned} 条可采纳。"
    else:
        summary = f"按空闲时段与截止时间自动规划：{planned} 条可采纳。"
    return {"ok": True, "method": method, "summary": summary, "items": items}


GUIDE_DAY_SYSTEM = """你是「校园管家」的智能调度决策者。

能量引擎会严格按你返回的任务顺序，把任务放进当天的空闲时段；
你的职责是根据用户的画像（精力/负载/压力）、精力曲线、长期记忆和处境，
决定今天值得推进哪些任务、先后顺序如何——负载高时主动留白，
把低价值软线任务往后放，而不是把日程塞满。

输入包含候选任务与用户背景。只输出严格 JSON：
{"order":["任务id", ...], "note":"一句话给用户看的说明",
 "advice":["一条可执行的建议", ...]}

规则：
1. 今天截止、已逾期、硬线任务必须排在前面且不得省略；
2. 软线/无截止/低优先级任务可以排在后面或省略（引擎会自然放到明天优先）；
3. 高优先级且需要大块精力的任务，优先放到上午等精力高峰时段靠前的位置；
4. 用户精力低或负载高时，控制“今天推进”的数量，宁可少排也别硬塞；
5. 只能使用输入里出现的任务 id，不要编造；
6. advice 给 0~3 条真正可执行的建议（减负/调整优先级/拆解/时段取舍），
   语气符合用户的性格偏好，不要空话。
"""


def guide_day_order(todos: list, day_iso: str) -> dict:
    """让 AI 决定某天的任务推进顺序；AI 不可用时返回空引导。"""
    from app.planner import planner as engine_mod

    try:
        day = date.fromisoformat(str(day_iso or "")[:10])
    except ValueError:
        return {"order": [], "note": "", "advice": []}
    cands = engine_mod.candidate_tasks(todos, day)
    if not cands:
        return {"order": [], "note": "", "advice": []}
    cands.sort(key=engine_mod._task_key)
    iso = day.isoformat()
    cfg = ai_gateway.load_config()
    if not ai_gateway.is_ready(cfg):
        return {"order": None, "note": "", "advice": []}

    forced = [
        c for c in cands
        if str(c.get("deadline") or "") == iso
        or (str(c.get("deadline") or "") and str(c["deadline"]) < iso)
    ]
    forced_ids = [str(c["id"]) for c in forced]
    cand_ids = {str(c["id"]) for c in cands}

    lines = []
    for c in cands:
        lines.append(
            "- id={id} title={title} priority={prio} deadline={dl} "
            "deadline_type={dt} energy_cost={ec} duration={dur}{ov}".format(
                id=c.get("id"),
                title=c.get("title"),
                prio=c.get("priority"),
                dl=c.get("deadline") or "",
                dt=c.get("deadline_type") or "",
                ec=c.get("energy_cost") or 1,
                dur=c.get("duration_min") or 60,
                ov="（已逾期）" if str(c.get("deadline") or "") and str(c["deadline"]) < iso else "",
            )
        )

    state = user_profile.latest_state() or {}
    state_lines = []
    for k, label in (("energy", "精力"), ("task_load", "事务负载"),
                     ("external_pressure", "外部压力")):
        v = state.get(k)
        if v and v != "unknown":
            state_lines.append(f"{label}={v}")
    mem_lines = []
    for m in user_memory.list_memories()[:8]:
        prefix = "性格：" if m.get("kind") == "personality" else "偏好："
        mem_lines.append(prefix + str(m.get("content") or ""))
    context = ["日期：" + iso]
    if state_lines:
        context.append("用户状态：" + "、".join(state_lines))
    context.extend(_energy_context_lines())
    if mem_lines:
        context.append("长期记忆：" + "；".join(str(x) for x in mem_lines))
    user = (
        "候选任务：\n{lines}\n\n{ctx}\n\n"
        "请按系统规则输出 order 与 note。"
    ).format(lines="\n".join(lines), ctx="\n".join(context))
    try:
        content = ai_gateway.chat_completion(cfg, [
            {"role": "system", "content": GUIDE_DAY_SYSTEM},
            {"role": "user", "content": user},
        ])
        text = ai_gateway._strip_json_fence(content or "")
        data = json.loads(text)
        ai_order = [str(x) for x in (data.get("order") or [])]
        note = str(data.get("note") or "").strip()[:120]
        raw_advice = data.get("advice") if isinstance(data.get("advice"), list) else []
        advice = [str(x).strip()[:120] for x in raw_advice if str(x).strip()][:3]
    except Exception:
        return {"order": None, "note": "", "advice": []}

    rest = [x for x in ai_order if x in cand_ids and x not in forced_ids]
    seen = set(forced_ids)
    final = list(forced_ids)
    for x in rest:
        if x not in seen:
            final.append(x)
            seen.add(x)
    for c in cands:
        cid = str(c["id"])
        if cid not in seen:
            final.append(cid)
            seen.add(cid)
    return {"order": final, "note": note, "advice": advice}


def _hour_bucket(time_s: str) -> str:
    try:
        h = int(str(time_s or "")[:2])
    except (TypeError, ValueError):
        return "unknown"
    if 6 <= h < 12:
        return "上午"
    if 12 <= h < 18:
        return "下午"
    if 18 <= h < 24:
        return "晚上"
    return "unknown"


def record_plan_feedback(action: str, items: list) -> dict:
    """用户采纳/拒绝 AI 排期建议 → 写入画像偏好与长期记忆。

    action: accept | reject
    items: [{id, title, date, time, end_time}]
    """
    action = str(action or "").strip().lower()
    if action not in ("accept", "reject"):
        return {"ok": False, "error": "action 需为 accept 或 reject"}
    items = [it for it in (items or []) if isinstance(it, dict)]
    if not items:
        return {"ok": False, "error": "缺少建议条目"}

    profile = user_profile.load_profile()
    prefs = profile.get("preferences") or {}
    prefs = dict(prefs)
    plan_pref = dict(prefs.get("planning") or {})
    buckets = dict(plan_pref.get("buckets") or {})
    plan_pref.setdefault("accepted", 0)
    plan_pref.setdefault("rejected", 0)

    lines = []
    for it in items:
        title = str(it.get("title") or "未命名待办")[:60]
        when = " ".join(str(x) for x in (
            it.get("date") or "", it.get("time") or ""
        ) if x)
        bucket = _hour_bucket(it.get("time"))
        if action == "accept":
            plan_pref["accepted"] = int(plan_pref["accepted"]) + 1
            if bucket != "unknown":
                buckets[bucket] = int(buckets.get(bucket, 0)) + 1
            lines.append(f"采纳 AI 排期：{title} → {when}".strip())
        else:
            plan_pref["rejected"] = int(plan_pref["rejected"]) + 1
            lines.append(f"拒绝 AI 排期建议：{title}（{when}）".strip())
    plan_pref["buckets"] = buckets
    prefs["planning"] = plan_pref
    profile["preferences"] = prefs
    user_profile.save_profile(profile)
    for line in lines:
        user_evidence.add_evidence(
            "plan_" + action,
            line,
            {"kind": "plan_feedback"},
        )

    # 偏好收敛：采纳 ≥3 次且某时段明显占优时，写入长期记忆
    accepted = int(plan_pref.get("accepted") or 0)
    if accepted >= 3 and buckets:
        top = max(buckets, key=lambda k: buckets[k])
        if int(buckets[top]) >= 2 and buckets[top] == max(buckets.values()):
            user_memory.add_memory(
                "用户更倾向把任务安排在" + top,
                importance=min(0.9, 0.45 + 0.08 * int(buckets[top])),
                category="planning_preference",
            )
    return {"ok": True, "recorded": len(lines), "action": action}
