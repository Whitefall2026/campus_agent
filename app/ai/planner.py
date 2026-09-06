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
import time
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
GUIDE_TTL = 25          # AI 引导结果缓存秒数（同一日期翻回/刷新秒出）
_GUIDE_CACHE = {}
RULE_NOW_DAYS = 3        # AI 不可用时，规则只兜底“未来几天内”值得排的任务

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

SELECTIVE_PLAN_SYSTEM = """你是「校园管家」的规划助手，负责判断“现在值不值得把某条待办排进日程”。
用户通常有一整池待办，其中很多截止日期还很远、并不紧急——不要把它们全部排出来，
只挑选目前值得规划的任务。

判断标准（结合输入里的用户状态/处境、长期记忆、精力曲线与近期反馈）：
- 已逾期 / 临近截止 / 近期必须交付的任务优先；
- 负载高或精力低时更要少排、留白，不要一次塞很多；
- 截止还很远的低优先级任务不要安排；
- 如果某条任务要做，给一条建议日期与时段：只能在占用时段之外，09:00-21:00，
  不晚于它的截止日（没有截止的也不能排到过去）；
- 安排在“今天”的时段必须晚于输入里的“当前时间”，不要安排已经过去的
  时间段；今天尽量给当前时间之后最近的合适空档；
- 同一天不要给两条任务同一个时段。

给每条选中的建议同时做“完成概率推演”：综合该任务的截止紧迫度、用户当前状态、
长期记忆里体现的偏好/性格、精力曲线对应时段系数、历史完成率基线以及当天安排
密度，估计用户按这个时段完成的把握。probability 给 0 到 1 的小数；
低于 0.6 必须 risk=true，并写一句 risk_copy：温和、可执行的建议（例如
“先只做开头一小步/交付骨架版/换到状态更好的时段/降低完成标准/拆成子任务”），
不要制造焦虑，也不要编造证据里没有的事实。

只输出严格 JSON，不要 Markdown；plan 可以只包含你选择的条目，不必覆盖全部待办：
{"summary":"给用户的一句话总结",
 "plan":[{"id":"待办id","date":"YYYY-MM-DD","time":"HH:MM","end_time":"HH:MM",
          "reason":"一句话理由","probability":0.0到1.0,
          "risk":true或false,"risk_copy":"风险/建议文案（无风险可留空）"}]}
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


def _rule_choice(t, busy: dict, start_date: date, taken: dict | None = None) -> dict | None:
    """为单条待办找规则空档。返回 {date,time,end_time,reason} 或 None。

    taken 为本次规划里已占用的 {date: [(start,end), ...]}，避免同一天
    给两条待办建议同一个时段。
    """
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
        intervals = sorted(
            list(busy.get(iso, [])) + list((taken or {}).get(iso, []))
        )
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
    now = _now()
    today = now.date()
    lines = [
        f"今天：{today.isoformat()}（周{'一二三四五六日'[today.weekday()]}）",
        f"当前时间：{now.strftime('%H:%M')}（今天只能排这个时间之后、尚未开始的时段）",
    ]
    state_lines = []
    for k, label in (("energy", "精力"), ("task_load", "事务负载"),
                     ("external_pressure", "外部压力")):
        v = state.get(k)
        if v and v != "unknown":
            state_lines.append(f"{label}={v}")
    if state_lines:
        lines.append("用户状态：" + "、".join(state_lines))
    lines.extend(_energy_context_lines())
    mem_lines = []
    for m in user_memory.list_memories()[:8]:
        prefix = "性格：" if m.get("kind") == "personality" else "偏好："
        mem_lines.append(prefix + str(m.get("content") or ""))
    if mem_lines:
        lines.append("长期记忆：" + "；".join(mem_lines))
    hours = _energy_hours()
    if hours:
        lines.append("精力系数(07-23)：" + " ".join(
            "{:.2f}".format(v) for v in hours))
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


def _energy_hours() -> list:
    """读取精力曲线的 07-23 点系数；文件缺失/异常时返回空列表。"""
    try:
        with open(os.path.join(DATA_DIR, "planner_profile.json"),
                  "r", encoding="utf-8") as f:
            hours = (json.load(f) or {}).get("hours") or []
        if isinstance(hours, list) and len(hours) >= 23:
            return [float(x) for x in hours[7:23]]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return []


def _parse_ai_plan(content: str):
    text = ai_gateway._strip_json_fence(content or "")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("plan 不是 JSON 对象")
    plan = data.get("plan") if isinstance(data.get("plan"), list) else []
    return str(data.get("summary") or "").strip(), plan


def _risk_float(v):
    """把模型给的 probability 规整到 0~1；非法返回 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(1.0, f))


def _risk_bool(v, probability) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.strip().lower() in ("true", "1", "yes", "是", "有风险"):
            return True
        if v.strip().lower() in ("false", "0", "no", "否"):
            return False
    return probability is not None and probability < 0.6


def _parse_selective_plan(content: str):
    """解析选择性规划输出（带 AI 完成概率/风险字段）。

    返回 (summary, items)；每条 item 为
    {id,date,time,end_time,reason,probability,risk,risk_copy}。
    """
    text = ai_gateway._strip_json_fence(content or "")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("plan 不是 JSON 对象")
    plan = data.get("plan") if isinstance(data.get("plan"), list) else []
    summary = str(data.get("summary") or "").strip()
    out = []
    for raw in plan:
        if not isinstance(raw, dict):
            continue
        tid = str(raw.get("id") or "")
        if not tid:
            continue
        probability = _risk_float(raw.get("probability"))
        risk = _risk_bool(raw.get("risk"), probability)
        out.append({
            "id": tid,
            "date": str(raw.get("date") or ""),
            "time": str(raw.get("time") or ""),
            "end_time": str(raw.get("end_time") or ""),
            "reason": str(raw.get("reason") or "").strip()[:200],
            "probability": probability,
            "risk": risk,
            "risk_copy": str(raw.get("risk_copy") or "").strip()[:240],
        })
    return summary, out


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


def plan_open_todos(todos: list | None = None,
                    exclude_skipped: str | None = None) -> dict:
    """规划全部未排期待办，返回建议列表（AI 优先 + 规则兜底）。

    - todos：可传入待办列表（缺省从 data/todos.json 读取）；
    - exclude_skipped：某个日期（YYYY-MM-DD），该天被用户“跳过”的任务
      不参与本轮规划，改日（其他日期）再提示。
    """
    if todos is None:
        todos = load_todos()
    todos = _unplanned_todos(todos)
    today_s = _now().date().isoformat()
    if exclude_skipped:
        skip_s = str(exclude_skipped)
        todos = [
            t for t in todos
            if skip_s not in [str(x) for x in (t.get("plan_skipped_dates") or [])]
        ]
    todos = [
        t for t in todos
        if not (str(t.get("plan_defer_to") or "") > today_s)
    ]
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
    taken: dict = {}
    ai_by_id = {str(x["id"]): x for x in ai_result or []}
    ai_used = False

    def _taken_hit(item: dict) -> bool:
        d = str(item.get("date") or "")
        s = str(item.get("time") or "")
        e = str(item.get("end_time") or "")
        if not (d and s and e):
            return False
        return _clash(s, e, taken.get(d, []))

    def _take(item: dict) -> None:
        d = str(item.get("date") or "")
        s = str(item.get("time") or "")
        e = str(item.get("end_time") or "")
        if d and s and e:
            taken.setdefault(d, []).append((s, e))

    for t in todos:
        tid = str(t["id"])
        meta = {"title": t.get("title"), "deadline": t.get("deadline")}
        choice = None
        if tid in ai_by_id and not _taken_hit(ai_by_id[tid]):
            choice = {**ai_by_id[tid], **meta}
            ai_used = True
        else:
            # AI 建议撞车/缺失时退回规则重选（会避开本批已占用的时段）
            fallback = _rule_choice(t, busy, start_date, taken)
            if fallback:
                choice = {**fallback, **meta}
        if choice:
            items.append({"id": t["id"], **choice})
            _take(choice)
        else:
            items.append({
                "id": t["id"], "date": None, "time": None, "end_time": None,
                "reason": "未来两周空档不足，建议手动安排或先减负",
                **meta,
            })
    planned = sum(1 for x in items if x.get("date"))
    if method == "ai" and ai_used:
        summary = f"我按你的空闲时段和截止时间把待办排好了：{planned} 条可采纳。"
    else:
        summary = f"按空闲时段与截止时间自动规划：{planned} 条可采纳。"
    return {"ok": True, "method": "ai" if ai_used else method,
            "summary": summary, "items": items}


def _rule_now_candidates(todos: list, today: date) -> list:
    """AI 不可用时规则兜底的候选：逾期、未来 RULE_NOW_DAYS 天内截止，
    或顺延日已到的任务；无截止且不紧急的任务不自动建议。"""
    horizon = today + timedelta(days=RULE_NOW_DAYS)
    today_s = today.isoformat()
    out = []
    for t in todos:
        defer_to = str(t.get("plan_defer_to") or "")
        dl = str(t.get("deadline") or "")
        if defer_to:
            if defer_to <= today_s:
                out.append(t)
            continue
        if not dl:
            continue
        try:
            d = date.fromisoformat(dl)
        except ValueError:
            continue
        if d <= horizon:
            out.append(t)
    return out


def plan_recommend_now(todos: list | None = None,
                       exclude_skipped: str | None = None) -> dict:
    """今日计划页的“选择性”排程建议。

    不把整池待办都排出来，而是让 AI 判断哪些目前值得规划：
    - AI 可用：模型从全部未排期待办中挑选并给出时段，同时对每条建议做
      完成概率推演（probability/risk/risk_copy），综合画像、记忆与历史；
    - AI 不可用/失败：规则只兜底逾期、未来几天内截止或顺延到期的任务，
      概率字段由调用方用规则模拟补全。
    """
    if todos is None:
        todos = load_todos()
    todos = _unplanned_todos(todos)
    today = _now().date()
    today_s = today.isoformat()
    if exclude_skipped:
        skip_s = str(exclude_skipped)
        todos = [
            t for t in todos
            if skip_s not in [str(x) for x in (t.get("plan_skipped_dates") or [])]
        ]
    todos = [
        t for t in todos
        if not (str(t.get("plan_defer_to") or "") > today_s)
    ]
    if not todos:
        return {"ok": True, "method": "none", "summary": "当前没有需要排期的待办",
                "items": []}

    ordered = sorted(todos, key=lambda t: (
        _deadline_key(t), _priority_rank(t), str(t.get("title") or "")))
    busy = _busy_map()
    state = user_profile.latest_state() or {}
    taken: dict = {}
    items: list = []
    summary = ""
    method = "rule"
    cfg = ai_gateway.load_config()
    risk_missing = False

    if ai_gateway.is_ready(cfg):
        try:
            content = ai_gateway.chat_completion(cfg, [
                {"role": "system", "content": SELECTIVE_PLAN_SYSTEM},
                {"role": "user", "content": _context_text(ordered, busy, state)},
            ])
            ai_summary, plan = _parse_selective_plan(content)
            by_id = {str(t.get("id")): t for t in ordered}
            seen = set()
            for raw in plan:
                if not isinstance(raw, dict):
                    continue
                t = by_id.get(str(raw.get("id") or ""))
                if t is None or str(t["id"]) in seen:
                    continue
                item = _safe_plan_item(raw, t, busy, today)
                if item is None:
                    continue
                d = str(item.get("date") or "")
                s = str(item.get("time") or "")
                e = str(item.get("end_time") or "")
                if d and s and e and _clash(s, e, taken.get(d, [])):
                    continue
                meta = {"title": t.get("title"), "deadline": t.get("deadline")}
                item = {
                    "id": t["id"],
                    **item,
                    **meta,
                    "probability": raw.get("probability"),
                    "risk": raw.get("risk"),
                    "risk_copy": str(raw.get("risk_copy") or "").strip()[:240],
                }
                if item.get("probability") is None:
                    risk_missing = True
                items.append(item)
                taken.setdefault(d, []).append((s, e))
                seen.add(str(t["id"]))
            method = "ai"
            summary = ai_summary
        except Exception:
            items = []
            taken = {}
            risk_missing = False

    if method == "rule" or risk_missing:
        # AI 不可用/失败 → 规则兜底；只排近期值得推进的任务
        method = "rule"
        summary = ""
        taken = {}
        items = []
        for t in _rule_now_candidates(ordered, today):
            choice = _rule_choice(t, busy, today, taken)
            if choice is None:
                continue
            meta = {"title": t.get("title"), "deadline": t.get("deadline")}
            items.append({"id": t["id"], **choice, **meta})
            taken.setdefault(str(choice.get("date") or ""), []).append(
                (str(choice.get("time") or ""), str(choice.get("end_time") or "")))

    # 防御：今天的建议不能落在“当前时间之前（已过去）”
    now_hm = _now().strftime("%H:%M")
    today_s2 = today.isoformat()
    items = [
        x for x in items
        if not (str(x.get("date") or "") == today_s2
                and str(x.get("time") or "")
                and str(x.get("time")) <= now_hm)
    ]

    if method == "rule" and not summary:
        planned = sum(1 for x in items if x.get("date"))
        if items:
            summary = "AI 当前不可用，已按“临近截止”规则给出 {} 条可采纳建议。".format(
                planned)
        else:
            summary = "目前没有临近截止、值得马上排期的新任务。"
    return {"ok": True, "method": method, "summary": summary, "items": items}


GUIDE_DAY_SYSTEM = """你是「校园管家」的智能调度决策者。

能量引擎会严格按你返回的任务顺序，把任务放进当天的空闲时段；
你的职责是根据用户的画像（精力/负载/压力）、精力曲线、长期记忆和处境，
决定今天值得推进哪些任务、先后顺序如何——负载高时主动留白，
把低价值软线任务往后放，而不是把日程塞满。

输入包含候选任务、当天空闲时段与用户背景。只输出严格 JSON：
{"order":["任务id", ...],
 "placements":[{"id":"任务id","start":"HH:MM","reason":"为什么放在这个时段"}],
 "note":"一句话给用户看的说明",
 "advice":["一条可执行的建议", ...]}

规则：
1. 今天截止、已逾期、硬线任务必须排在前面且不得省略；
2. placements 里 start 必须落在当天空闲时段内，且匹配任务时长；
3. 高精力任务优先放精力高峰（上午/曲线显示的高系数时段），事务性任务
   可以放下午/晚上；用户偏好时段优先；
4. 软线/无截止/低优先级任务可以排在后面或省略（引擎会自然放到明天优先）；
5. 用户精力低或负载高时，控制“今天推进”的数量，宁可少排也别硬塞；
6. 只能使用输入里出现的任务 id，不要编造；
7. advice 给 0~3 条真正可执行的建议（减负/调整优先级/拆解/时段取舍），
   语气符合用户的性格偏好，不要空话。
"""


def guide_day_order(todos: list, day_iso: str,
                    rejections: list | None = None,
                    candidate_ids: list | None = None) -> dict:
    """让 AI 决定某天的任务推进顺序；AI 不可用时返回空引导。"""
    from app.planner import planner as engine_mod

    try:
        day = date.fromisoformat(str(day_iso or "")[:10])
    except ValueError:
        return {"order": [], "note": "", "advice": [], "placements": []}
    iso = day.isoformat()
    if not rejections:
        try:
            todos_mtime = os.path.getmtime(os.path.join(DATA_DIR, "todos.json"))
        except OSError:
            todos_mtime = 0
        hit = _GUIDE_CACHE.get((iso, todos_mtime))
        if hit and time.time() - hit[0] < GUIDE_TTL:
            return {k: list(v) if isinstance(v, list) else v
                    for k, v in hit[1].items()}
    if candidate_ids:
        idset = {str(x) for x in candidate_ids}
        cands = [
            t for t in todos
            if str(t.get("id")) in idset
            and t.get("status") != "done"
            and not t.get("date") and not t.get("time")
        ]
    else:
        cands = engine_mod.candidate_tasks(todos, day)
    if not cands:
        return {"order": [], "note": "", "advice": [], "placements": []}
    cands.sort(key=engine_mod._task_key)
    cfg = ai_gateway.load_config()
    if not ai_gateway.is_ready(cfg):
        return {"order": None, "note": "", "advice": [], "placements": []}
    cfg = dict(cfg)
    cfg["timeout"] = min(max(int(cfg.get("timeout") or 15), 5), 15)

    forced = [
        c for c in cands
        if str(c.get("deadline") or "") == iso
        or (str(c.get("deadline") or "") and str(c["deadline"]) < iso)
    ]
    forced_ids = [str(c["id"]) for c in forced]
    cand_ids = {str(c["id"]) for c in cands}
    runs = engine_mod.free_runs(todos, day)
    window_lines = [
        "{}–{}".format(
            engine_mod.fields.min_to_hm(r["start"]),
            engine_mod.fields.min_to_hm(r["end"]),
        )
        for r in runs
    ]

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
    context.append("当天空闲时段：" + ("、".join(window_lines) or "无"))
    user = (
        "候选任务：\n{lines}\n\n{ctx}\n\n"
        "{rej}"
        "请按系统规则输出 order 与 note。"
    ).format(
        lines="\n".join(lines),
        ctx="\n".join(context),
        rej="".join(
            "被引擎拒绝的上一轮提议：{id}：{why}。请换一个可行时段，"
            "或考虑降低完成标准/拆解后再排。\n".format(
                id=str(r.get("task_id") or ""),
                why=str(r.get("reason") or ""),
            )
            for r in (rejections or [])
        ),
    )
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
        raw_placements = data.get("placements") if isinstance(data.get("placements"), list) else []
        placements = []
        for pitem in raw_placements:
            if not isinstance(pitem, dict):
                continue
            pid = str(pitem.get("id") or "")
            start_s = str(pitem.get("start") or "").strip()
            if pid in cand_ids and TIME_RE.match(start_s):
                placements.append({
                    "task_id": pid,
                    "start": start_s,
                    "reason": str(pitem.get("reason") or "")[:120],
                })
    except Exception:
        return {"order": None, "note": "", "advice": [], "placements": []}

    rest = [x for x in ai_order if x in cand_ids and x not in forced_ids]
    seen = set(forced_ids)
    final = list(forced_ids)
    for x in rest:
        if x not in seen:
            final.append(x)
            seen.add(x)
    if not candidate_ids:
        # 规则/引擎直连模式：确保所有候选都参与贪心；
        # AI 模式则尊重 AI 的取舍——它没选的任务今天不排。
        for c in cands:
            cid = str(c["id"])
            if cid not in seen:
                final.append(cid)
                seen.add(cid)
    result = {"order": final, "note": note, "advice": advice,
              "placements": placements}
    if not rejections:
        try:
            todos_mtime = os.path.getmtime(os.path.join(DATA_DIR, "todos.json"))
        except OSError:
            todos_mtime = 0
        _GUIDE_CACHE[(iso, todos_mtime)] = (time.time(), result)
        if len(_GUIDE_CACHE) > 40:
            old = sorted(_GUIDE_CACHE.items(), key=lambda kv: kv[1][0])[:20]
            for k, _v in old:
                _GUIDE_CACHE.pop(k, None)
    return result


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
