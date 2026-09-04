"""校园管家 demo 服务器：仅用 Python 标准库，零依赖。


启动：python server.py [端口]
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from extractor import parse_text
from scheduler import build_state, slot_conflicts, suggest_slot
from storage import load_todos, save_todos
from wechat_bridge import BRIDGE as WX_BRIDGE
import ai_gateway
import kinds

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT, "static")

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
}

TODO_FIELDS = (
    "title", "category", "priority", "kind", "date", "time", "end_time",
    "duration_min", "location", "deadline", "deadline_time", "raw",
)


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
    return {
        **norm,
        "id": uuid.uuid4().hex[:10],
        "status": "pending",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


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

    # ---- 路由 ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            return self._json(self._state())
        if path == "/api/wechat/status":
            return self._json(WX_BRIDGE.status())
        if path == "/api/ai/config":
            cfg = ai_gateway.load_config()
            return self._json({"ok": True, "config": ai_gateway.public_config(cfg)})
        if path == "/api/ai/pending":
            pending = ai_gateway.prune_expired_pending()
            pending.reverse()
            return self._json({"ok": True, "pending": pending})
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
            todo = {
                **norm,
                "id": uuid.uuid4().hex[:10],
                "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
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
            if not exists:
                todo = ai_gateway.make_ai_todo(item)
                want = kinds.valid_kind(body.get("kind"))
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
                todos.append(todo)
                save_todos(todos)
            ai_gateway.remove_pending(item_id)
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
        for k, v in body.items():
            if k not in ALLOWED_FIELDS:
                continue
            if k in ("date", "time", "end_time", "deadline", "deadline_time") and v in (None, ""):
                v = None
            if k == "status" and v not in ("pending", "done"):
                continue
            if k == "kind":
                v = kinds.valid_kind(v) or todo.get("kind")
                if not v:
                    continue
            if k == "duration_min" and not isinstance(v, int):
                continue
            todo[k] = v
            if k == "date" and not v:
                todo["time"] = None
                todo["end_time"] = None
        todo = kinds.normalize_item(
            todo,
            kind=todo.get("kind"),
            raw=todo.get("raw") or todo.get("title") or "",
        )
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


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"校园管家 demo 已启动: http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止服务")
    if WX_BRIDGE.config().get("auto_start"):
        WX_BRIDGE.start()
        print("微信监听：已开启，等待微信登录后自动连接（状态可查看 /api/wechat/status）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        WX_BRIDGE.stop()


if __name__ == "__main__":
    main()
