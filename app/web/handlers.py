"""校园管家 HTTP 层：API 路由 + 静态页面。

Handler 由根目录的 server.py 启动；业务逻辑见 app.core / app.ai / app.wechat。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.core.extractor import parse_text
from app.core.scheduler import build_state, slot_conflicts, suggest_slot
from app.core.storage import load_todos, save_todos
from app.core import kinds
from app.ai import gateway as ai_gateway
from app.ai import chat as ai_chat
from app.ai import context as ai_context
from app.ai import profile as ai_profile
from app.ai import memory as ai_memory
from app.ai import evidence as ai_evidence
from app.ai import planner as ai_planner
from app.ai import pressure as ai_pressure
from app.wechat.bridge import BRIDGE as WX_BRIDGE
from app.paths import DATA_DIR, STATIC_DIR
from app.core import courses as course_mod
from app.planner import energy as plan_energy
from app.planner import fields as plan_fields
from app.planner import planner as plan_engine
from app.planner import risk as plan_risk
from app.planner import shield as plan_shield
from app.planner import decompose as plan_decompose
from app.planner import store as plan_store
from app.planner import llm_copies as plan_llm

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

SAMPLE_TEXTS = [
    "明天上午9点去图书馆写论文，重要",
    "周三下午2点在二教401上数据结构课",
    "周四晚上7点参加社团例会，在二教401",
    "数学作业下周一上午9点前提交",
    "周六下午3点到体育馆参加篮球比赛",
    "下周三上午10点去校医院体检",
    "周五下午5点前交实验报告到实验室",
    "明天晚上8点和室友聚餐，在西门食堂",
]

ALLOWED_FIELDS = {
    "title", "status", "date", "time", "end_time", "deadline",
    "deadline_time", "priority", "location", "duration_min", "kind",
    "energy_cost", "deliverable", "deadline_type", "ddl_float_days",
    "parent_id",
}

TODO_FIELDS = (
    "title", "category", "priority", "kind", "date", "time", "end_time",
    "duration_min", "location", "deadline", "deadline_time", "raw",
    "energy_cost", "deliverable", "deadline_type", "ddl_float_days",
    "parent_id",
)

VALID_ENERGY = {1, 2, 3, 4, 5}
VALID_DDL_TYPES = {"hard", "soft"}
_SKIP_FIELD = object()


def _norm_planner_field(k: str, v):
    """把规划类字段规整成合法值；不合法返回 _SKIP_FIELD 表示丢弃。"""
    if k == "energy_cost":
        if v in (None, ""):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return _SKIP_FIELD
        return n if n in VALID_ENERGY else _SKIP_FIELD
    if k == "deliverable":
        if v in (None, ""):
            return None
        s = str(v).strip()
        return s[:120] or None
    if k == "deadline_type":
        v = str(v or "").strip().lower()
        return v if v in VALID_DDL_TYPES else _SKIP_FIELD
    if k == "ddl_float_days":
        if v in (None, ""):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return _SKIP_FIELD
        return n if 0 <= n <= 7 else _SKIP_FIELD
    if k == "parent_id":
        if v in (None, ""):
            return None
        s = str(v).strip()
        return s[:40] or None
    return _SKIP_FIELD


def make_todo_from_text(text: str, kind: str | None = None) -> dict | None:
    parsed = parse_text(text)
    if not parsed.get("ok"):
        return None
    norm = kinds.normalize_item(
        {k: parsed.get(k) for k in TODO_FIELDS},
        kind=kind,
        raw=text,
    )
    if norm.get("kind") == kinds.KIND_SCHEDULE and not norm.get("date"):
        return None  # 日程必须能被放进某一天，否则交给待办
    return _finalize_todo({
        **norm,
        "id": uuid.uuid4().hex[:10],
        "status": "pending",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })


def _finalize_todo(todo: dict) -> dict:
    """入库前补全规划默认字段（耗能/时长/硬软线/浮动天数），返回新 dict。"""
    return plan_fields.normalize_task(todo)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 基础工具 ----
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _serve_static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(os.path.realpath(STATIC_DIR)):
            return self._json({"ok": False, "error": "forbidden"}, 403)
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.exists(target):
            return self._json({"ok": False, "error": "not found"}, 404)
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as f:
            body = f.read()
        self._send(200, body, MIME.get(ext, "application/octet-stream"))

    def _state(self):
        return build_state(load_todos())

    def _plan_payload(self, todos, day):
        """当日规划 + 风险评估 + 负载指标（plan/risk/shield 三合一）。"""
        state = ai_profile.latest_state()
        raw_profile = plan_energy.load_profile()
        current_day = day == date.today()
        profile = (plan_energy.profile_for_current_state(raw_profile, state)
                   if current_day else raw_profile)
        # 只给「主动推进」留有限额；硬截止任务不受此限，避免系统替用户漏事。
        cap_by_energy = {"low": 120, "medium": 180, "high": 240}
        cap = cap_by_energy.get(str(state.get("energy") or "").lower(), 180) if current_day else None
        plan0 = plan_engine.plan_day(todos, day=day, profile=profile,
                                    focus_cap_min=cap)
        plan0.setdefault("meta", {}).update({
            "state_energy": state.get("energy") or "unknown",
            "state_multiplier": profile.get("state_multiplier", 1.0),
            "focus_cap_min": cap,
        })
        seed = day.year * 10000 + day.month * 100 + day.day
        plan = plan_risk.plan_with_risk(plan0, seed=seed)
        load = plan_shield.load_metrics(todos, day=day, plan=plan0)
        return {"ok": True, "date": day.isoformat(), "plan": plan, "load": load}

    def _day_from(self, body_or_query, fallback):
        try:
            s = str(body_or_query or "")
            return date.fromisoformat(s) if s else fallback
        except ValueError:
            return fallback

    # ---- 路由 ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            return self._json(self._state())
        if path == "/api/plan":
            qs = parse_qs(urlparse(self.path).query)
            day = self._day_from(
                (qs.get("date") or [None])[0], date.today())
            return self._json(self._plan_payload(load_todos(), day))
        if path == "/api/energy":
            prof = plan_energy.load_profile()
            state = ai_profile.latest_state()
            effective = plan_energy.profile_for_current_state(prof, state)
            return self._json({
                "ok": True,
                "energy": {
                    "version": int(prof.get("version") or 1),
                    "updated_at": prof.get("updated_at"),
                    "hours": effective.get("hours") or plan_energy.DEFAULT_HOUR_COEF,
                    "available_points": round(plan_energy.available_total(effective), 2),
                    "state_level": effective.get("state_level", "unknown"),
                    "state_multiplier": effective.get("state_multiplier", 1.0),
                },
            })
        if path == "/api/wechat/status":
            return self._json(WX_BRIDGE.status())
        if path == "/api/ai/config":
            cfg = ai_gateway.load_config()
            return self._json({"ok": True, "config": ai_gateway.public_config(cfg)})
        if path == "/api/ai/pending":
            pending = ai_gateway.prune_expired_pending()
            pending.reverse()
            return self._json({"ok": True, "pending": pending})
        if path == "/api/ai/profile":
            return self._json({"ok": True, **ai_context.profile_summary()})
        if path == "/api/ai/today":
            return self._json({"ok": True, **ai_pressure.today_brief(load_todos())})
        if path == "/api/chat/history":
            return self._json({"ok": True, "messages": ai_chat.public_history()})
        if path == "/api/courses":
            return self._json(course_mod.public_summary())
        self._serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if path == "/api/parse":
            text = body.get("text", "")
            parsed = parse_text(text)
            if not parsed.get("ok"):
                return self._json({"ok": False, "error": parsed.get("error", "解析失败")}, 400)
            parsed["kind"] = kinds.derive_kind(parsed, raw=text)
            parsed["kind_label"] = "日程" if parsed["kind"] == kinds.KIND_SCHEDULE else "待办"
            todos = load_todos()
            suggestion = suggest_slot({**parsed, "status": "pending"}, todos, date.today(), datetime.now())
            clashes = slot_conflicts(parsed, todos)
            return self._json({"ok": True, "parsed": parsed, "suggestion": suggestion, "clashes": clashes})
        if path == "/api/todos":
            todos = load_todos()
            text = body.get("text", "")
            parsed0 = parse_text(text)
            todo = make_todo_from_text(text, body.get("kind")) if text else None
            if todo is None:
                if not parsed0.get("ok"):
                    return self._json({"ok": False, "error": parsed0.get("error", "无法从文本中提取事项")}, 400)
                if kinds.valid_kind(body.get("kind")) == kinds.KIND_SCHEDULE and not parsed0.get("date"):
                    return self._json({
                        "ok": False,
                        "error": "未识别到明确的日程日期，无法采纳到日程；请改选“待办”，之后可在待办页手动规划到某一天",
                    }, 400)
                return self._json({"ok": False, "error": "无法从文本中提取事项"}, 400)
            todos.append(todo)
            save_todos(todos)
            return self._json({"ok": True, "todo": todo, "state": self._state()})
        if path == "/api/items":
            todos = load_todos()
            title = str(body.get("title") or "").strip()
            if not title:
                return self._json({"ok": False, "error": "缺少标题"}, 400)
            fields = {k: body.get(k) for k in TODO_FIELDS}
            fields["title"] = title
            fields["category"] = str(body.get("category") or "other").strip()
            fields["priority"] = str(body.get("priority") or "medium").strip()
            for k in ("energy_cost", "deliverable", "deadline_type",
                      "ddl_float_days", "parent_id"):
                v = _norm_planner_field(k, fields.get(k))
                fields[k] = v if v is not _SKIP_FIELD else None
            norm = kinds.normalize_item(
                fields,
                kind=body.get("kind"),
                raw=str(body.get("raw") or title),
            )
            if norm.get("kind") == kinds.KIND_SCHEDULE and not norm.get("date"):
                return self._json({
                    "ok": False,
                    "error": "日程必须包含日期（请填写日期后再保存，或改选待办）",
                }, 400)
            todo = _finalize_todo({
                **norm,
                "id": uuid.uuid4().hex[:10],
                "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            })
            todos.append(todo)
            save_todos(todos)
            return self._json({"ok": True, "todo": todo, "state": self._state()})
        if path == "/api/seed":
            todos = load_todos()
            existing = {t.get("title") for t in todos}
            added = 0
            for text in SAMPLE_TEXTS:
                if added >= 3:
                    break
                todo = make_todo_from_text(text)
                if todo and todo["title"] not in existing:
                    todos.append(todo)
                    existing.add(todo["title"])
                    added += 1
            save_todos(todos)
            return self._json({"ok": True, "added": added, "state": self._state()})
        if path == "/api/clear":
            todos = load_todos()
            scope = str(body.get("scope") or "all").strip().lower()
            if scope == "schedule":
                todos = [
                    t for t in todos
                    if (kinds.valid_kind(t.get("kind")) or kinds.derive_kind(t))
                    != kinds.KIND_SCHEDULE
                ]
            elif scope == "todo":
                todos = [
                    t for t in todos
                    if (kinds.valid_kind(t.get("kind")) or kinds.derive_kind(t))
                    != kinds.KIND_TODO
                ]
            else:
                todos = []
            save_todos(todos)
            return self._json({"ok": True, "state": self._state()})
        if path == "/api/wechat/start":
            return self._json(WX_BRIDGE.start())
        if path == "/api/wechat/stop":
            return self._json(WX_BRIDGE.stop())
        if path == "/api/wechat/scan":
            return self._json(WX_BRIDGE.scan())
        if path == "/api/wechat/config":
            return self._json(WX_BRIDGE.update_config(body))
        if path == "/api/ai/config":
            cfg = ai_gateway.load_config()
            if "enabled" in body:
                cfg["enabled"] = bool(body["enabled"])
            if "provider" in body:
                cfg["provider"] = str(body["provider"])
            if "base_url" in body and str(body["base_url"] or "").strip():
                cfg["base_url"] = str(body["base_url"]).strip()
            if "model" in body and str(body["model"] or "").strip():
                cfg["model"] = str(body["model"]).strip()
            if body.get("api_key"):
                cfg["api_key"] = str(body["api_key"]).strip()
            if "min_len" in body:
                cfg["min_len"] = int(body["min_len"] or 10)
            if "require_time_word" in body:
                cfg["require_time_word"] = bool(body["require_time_word"])
            public = ai_gateway.save_config(cfg)
            return self._json({"ok": True, "config": public})
        if path == "/api/ai/test":
            cfg = ai_gateway.load_config()
            if "provider" in body and str(body.get("provider") or "").strip():
                cfg["provider"] = str(body["provider"]).strip()
            if "base_url" in body and str(body.get("base_url") or "").strip():
                cfg["base_url"] = str(body["base_url"]).strip()
            if "model" in body and str(body.get("model") or "").strip():
                cfg["model"] = str(body["model"]).strip()
            if body.get("api_key"):
                cfg["api_key"] = str(body["api_key"]).strip()
            try:
                reply = ai_gateway.test_connection(cfg)
                return self._json({"ok": True, "reply": reply})
            except ai_gateway.AiGatewayError as exc:
                return self._json({"ok": False, "error": str(exc)})
        if path == "/api/chat":
            result = ai_chat.chat_turn(str(body.get("text") or ""))
            return self._json(result, 400 if not result.get("ok") else 200)
        if path == "/api/chat/reset":
            return self._json(ai_chat.reset_thread())
        if path == "/api/ai/profile/reset":
            ai_profile.reset_profile()
            cleared_evidence = ai_evidence.clear_evidence()
            cleared_memories = ai_memory.clear_memories()
            return self._json({
                "ok": True,
                "cleared_evidence": cleared_evidence,
                "cleared_memories": cleared_memories,
            })
        if path == "/api/ai/profile/state":
            current = ai_profile.latest_state()
            levels = {"low", "medium", "high", "unknown"}
            energy = str(body.get("energy") or current.get("energy") or "unknown")
            if energy not in levels:
                return self._json({"ok": False, "error": "精力状态不正确"}, 400)
            current["energy"] = energy
            current["confidence"] = max(float(current.get("confidence") or 0), 0.6)
            profile = ai_profile.update_state(current)
            return self._json({"ok": True, "state": profile.get("state") or {}})
        if path == "/api/ai/plan":
            return self._json(ai_planner.plan_open_todos())
        if path == "/api/ai/plan/feedback":
            items = body.get("items")
            if not isinstance(items, list):
                items = [body]
            return self._json(ai_planner.record_plan_feedback(
                str(body.get("action") or ""), items
            ))
        if path == "/api/courses/import":
            name = str(body.get("name") or "")
            payload = str(body.get("data") or "")
            if not name.lower().endswith(".xlsx") or not payload:
                return self._json({
                    "ok": False,
                    "error": "请选择 .xlsx 格式的课表文件",
                }, 400)
            try:
                raw = base64.b64decode(payload)
            except Exception:
                return self._json({"ok": False, "error": "文件内容读取失败"}, 400)
            if not raw or len(raw) > 20 * 1024 * 1024:
                return self._json({"ok": False, "error": "文件为空或超过 20MB"}, 400)
            tmp = os.path.join(DATA_DIR, ".upload_" + uuid.uuid4().hex + ".xlsx")
            try:
                with open(tmp, "wb") as f:
                    f.write(raw)
                data = course_mod.import_xlsx(
                    tmp, term_start=str(body.get("term_start") or "") or None
                )
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc) or "课表解析失败"}, 400)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            meta = data.get("meta") or {}
            return self._json({
                "ok": True,
                "course_count": len(data.get("courses") or []),
                "term": meta.get("term", ""),
                "class_name": meta.get("class_name", ""),
                "term_start": meta.get("term_start", ""),
                "weeks_total": meta.get("weeks_total", 0),
            })
        if path == "/api/plan/apply":
            # 采纳某天的能量规划：placements=排程、deferrals=软线顺延（可只给其一）
            todos = load_todos()
            day = self._day_from(body.get("date"), date.today())
            plan0 = plan_engine.plan_day(todos, day=day)
            placements = body.get("placements")
            deferrals = body.get("deferrals")
            if placements is not None and not isinstance(placements, list):
                return self._json({"ok": False, "error": "placements 需为数组"}, 400)
            if deferrals is not None and not isinstance(deferrals, list):
                return self._json({"ok": False, "error": "deferrals 需为数组"}, 400)
            todos, applied_p = plan_engine.apply_placements(
                todos, plan0,
                task_ids=[str(x) for x in placements] if placements is not None else None)
            todos, applied_d = plan_engine.apply_deferrals(
                todos, plan0,
                task_ids=[str(x) for x in deferrals] if deferrals is not None else None)
            save_todos(todos)
            return self._json({
                "ok": True,
                "placed": len(applied_p),
                "deferred": len(applied_d),
                "state": self._state(),
            })
        if path == "/api/plan/move":
            # 用户拖拽/手动调整任务时间：落库并记录偏好
            todos = load_todos()
            item_id = str(body.get("id") or "")
            if not item_id:
                return self._json({"ok": False, "error": "缺少 id"}, 400)
            todos, rec = plan_engine.record_move(
                todos, item_id,
                str(body.get("date") or ""),
                body.get("time") or None,
                body.get("end_time") or None)
            if rec is None:
                return self._json({"ok": False, "error": "事项不存在"}, 404)
            save_todos(todos)
            return self._json({"ok": True, "record": rec, "state": self._state()})
        if path == "/api/shield":
            # 智能挡箭牌：粘贴外部任务 → 负载评估 + 建议回复（AI 可用时用 LLM 润色）
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "请粘贴要评估的外部任务描述"}, 400)
            day = self._day_from(body.get("date"), date.today())
            res = plan_shield.evaluate_intrusion(load_todos(), text, day=day)
            if res.get("ok"):
                cfg = ai_gateway.load_config()
                if ai_gateway.is_ready(cfg):
                    try:
                        enhanced = plan_llm.refusal_copy(cfg, {
                            "task": res.get("title"),
                            "load": (res.get("load") or {}).get("level"),
                            "undone": (res.get("load") or {}).get("undone_todos"),
                            "energy": (res.get("load") or {}).get("planned_energy"),
                            "reasons": (res.get("load") or {}).get("reasons"),
                            "style": str(body.get("style") or "")[:60],
                        })
                        if enhanced:
                            res["copy"] = enhanced
                            res["copy_method"] = "ai"
                    except ai_gateway.AiGatewayError:
                        pass  # 保持规则话术兜底
            return self._json(res, 400 if not res.get("ok") else 200)
        if path == "/api/plan/classify":
            # 截止类型分类：LLM 优先，规则兜底（返回与任务库一致的结果）
            todos = load_todos()
            item_id = str(body.get("id") or "")
            todo = next((t for t in todos if t.get("id") == item_id), None)
            if todo is None:
                return self._json({"ok": False, "error": "事项不存在"}, 404)
            cfg = ai_gateway.load_config()
            out = {"id": item_id}
            method = "rule"
            try:
                got = plan_llm.ddl_classify(
                    cfg, "{} {}".format(todo.get("title"), todo.get("deliverable") or ""))
            except ai_gateway.AiGatewayError:
                got = None
            if got:
                out.update(got)
                method = "ai"
            else:
                norm = plan_fields.normalize_task(todo)
                out["deadline_type"] = norm["deadline_type"]
                out["ddl_float_days"] = norm["ddl_float_days"]
            out["method"] = method
            return self._json({"ok": True, **out})
        if path == "/api/decompose":
            # LLM 任务拆解（预览）：返回候选子任务，不直接入库
            todos = load_todos()
            item_id = str(body.get("id") or "")
            todo = next((t for t in todos if t.get("id") == item_id), None)
            if todo is None:
                return self._json({"ok": False, "error": "事项不存在"}, 404)
            cfg = ai_gateway.load_config()
            reason = None
            subs = None
            if not ai_gateway.is_ready(cfg):
                reason = "AI 未启用或配置不完整（需 API Key + 模型），暂无法拆解"
            elif not plan_decompose.needs_decomposition(todo):
                reason = "该任务预计耗能不高，暂不需要拆解"
            else:
                try:
                    subs = plan_decompose.ai_decompose(cfg, todo)
                    if not subs:
                        reason = "模型未给出可用的里程碑子任务，按整块任务处理"
                except ai_gateway.AiGatewayError as exc:
                    reason = "AI 调用失败：{}".format(str(exc)[:120])
            if subs:
                plan_decompose.cache_subtasks(item_id, subs)
            return self._json({
                "ok": True,
                "id": item_id,
                "decomposed": bool(subs),
                "subtasks": subs or [],
                "reason": reason,
            })
        if path == "/api/decompose/accept":
            # 采纳拆解结果：把缓存的子任务写入待办池（parent_id 关联父任务）
            todos = load_todos()
            item_id = str(body.get("id") or "")
            todo = next((t for t in todos if t.get("id") == item_id), None)
            if todo is None:
                return self._json({"ok": False, "error": "事项不存在"}, 404)
            subs = plan_decompose.cached_subtasks(item_id)
            if not subs:
                return self._json({
                    "ok": False,
                    "error": "拆解结果已过期或不存在，请先重新拆解",
                }, 400)
            created = []
            for s in subs:
                child = plan_decompose.child_todo(todo, s)
                todos.append(child)
                created.append(child)
            save_todos(todos)
            plan_decompose.cache_subtasks(item_id, [])  # 一次性采纳，作废缓存
            return self._json({
                "ok": True,
                "created": len(created),
                "parent_id": item_id,
                "state": self._state(),
            })
        if path == "/api/feedback":
            # 任务完成后的精力反馈：校准精力曲线并记录历史
            todos = load_todos()
            item_id = str(body.get("id") or "")
            todo = next((t for t in todos if t.get("id") == item_id), None)
            if todo is None:
                return self._json({"ok": False, "error": "事项不存在"}, 404)
            rating = str(body.get("rating") or "ok").strip().lower()
            if rating not in ("easy", "ok", "tough"):
                return self._json({"ok": False, "error": "rating 需为 easy/ok/tough"}, 400)
            hm = plan_fields.hm_to_min(str(todo.get("time") or ""))
            hour = (hm // 60) if hm is not None else datetime.now().hour
            try:
                d = date.fromisoformat(str(todo.get("date") or ""))
            except ValueError:
                d = date.today()
            prof = plan_energy.load_profile()
            prof2 = plan_energy.record_feedback(hour, rating, prof)
            plan_energy.save_profile(prof2)
            plan_store.append_event({
                "type": "rating",
                "task_id": item_id,
                "title": str(todo.get("title") or ""),
                "date": d.isoformat(),
                "weekday": d.weekday(),
                "hour": hour,
                "bucket": plan_risk.bucket_of(hour),
                "rating": rating,
            })
            return self._json({
                "ok": True,
                "rating": rating,
                "hour": hour,
                "energy": prof2,
            })
        m = re.fullmatch(r"/api/ai/pending/([^/]+)/(accept|reject)", urlparse(self.path).path)
        if m:
            item_id = m.group(1)
            action = m.group(2)
            pending = ai_gateway.get_pending()
            item = next((p for p in pending if p.get("id") == item_id), None)
            if item is None:
                return self._json({"ok": False, "error": "待采纳事项不存在、已处理或已过期"}, 404)
            if action == "reject":
                ai_gateway.remove_pending(item_id)
                ai_chat.mark_item_outcome(item_id, "rejected")
                return self._json({"ok": True, "state": self._state()})
            # accept：按用户选择写入日程或待办
            todos = load_todos()
            chat = str(item.get("chat_username") or "")
            seq = int(item.get("seq") or 0)
            title = str((item.get("fields") or {}).get("title") or "").strip()
            exists = any(
                t.get("wx_chat") == chat
                and int(t.get("wx_seq") or 0) == seq
                and str(t.get("title") or "").strip() == title
                for t in todos
            )
            want = kinds.valid_kind(body.get("kind"))
            if not exists:
                todo = ai_gateway.make_ai_todo(item, source=item.get("source", "wechat_ai"))
                if want:
                    todo = kinds.normalize_item(
                        todo,
                        kind=want,
                        raw=todo.get("raw") or "",
                    )
                    if want == kinds.KIND_SCHEDULE and not todo.get("date"):
                        return self._json({
                            "ok": False,
                            "error": "该结果没有明确日期，无法采纳到日程；请采纳到待办，再到待办页手动规划到某一天",
                        }, 400)
                todo = _finalize_todo(todo)
                todos.append(todo)
                save_todos(todos)
            ai_gateway.remove_pending(item_id)
            accepted_kind = (
                want
                or kinds.valid_kind((item.get("fields") or {}).get("kind"))
                or "todo"
            )
            ai_chat.mark_item_outcome(item_id, "accepted:" + accepted_kind)
            return self._json({"ok": True, "state": self._state()})
        self._json({"ok": False, "error": "not found"}, 404)

    def do_PATCH(self):
        m = re.fullmatch(r"/api/todos/([^/]+)", urlparse(self.path).path)
        if not m:
            return self._json({"ok": False, "error": "not found"}, 404)
        body = self._read_body()
        todos = load_todos()
        todo = next((t for t in todos if t.get("id") == m.group(1)), None)
        if todo is None:
            return self._json({"ok": False, "error": "事项不存在"}, 404)
        was_done = todo.get("status") == "done"
        for k, v in body.items():
            if k not in ALLOWED_FIELDS:
                continue
            if k in ("date", "time", "end_time", "deadline", "deadline_time") and v in (None, ""):
                v = None
            if k == "status" and v not in ("pending", "done", "deferred"):
                continue
            if k == "kind":
                v = kinds.valid_kind(v) or todo.get("kind")
                if not v:
                    continue
            if k == "duration_min" and not isinstance(v, int):
                continue
            if k in ("energy_cost", "deliverable", "deadline_type",
                     "ddl_float_days", "parent_id"):
                v = _norm_planner_field(k, v)
                if v is _SKIP_FIELD:
                    continue
            todo[k] = v
            if k == "date" and not v:
                todo["time"] = None
                todo["end_time"] = None
        todo = _finalize_todo(kinds.normalize_item(
            todo,
            kind=todo.get("kind"),
            raw=todo.get("raw") or todo.get("title") or "",
        ))
        if todo.get("status") == "done" and not was_done:
            plan_risk.record_done(todo)
        save_todos(todos)
        return self._json({"ok": True, "todo": todo, "state": self._state()})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/todos/([^/]+)", urlparse(self.path).path)
        if not m:
            return self._json({"ok": False, "error": "not found"}, 404)
        todos = load_todos()
        before = len(todos)
        todos = [t for t in todos if t.get("id") != m.group(1)]
        if len(todos) == before:
            return self._json({"ok": False, "error": "事项不存在"}, 404)
        save_todos(todos)
        return self._json({"ok": True, "state": self._state()})

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))
