# -*- coding: utf-8 -*-
"""微信读取与监听桥接：自动把聊天消息中的日程提取进校园管家。

设计目标
--------
- 复用 wechatauto-replica 的「读取」(WeChatDB) 与「监听」(Listener) 能力；
- 微信未登录/未运行时，桥接保持“未连接”并自动重试（以 Weixin.exe
  进程存活为门槛，避免本地缓存密钥造成“假已连接”），不影响主服务；
- 每个会话记录处理到哪一条消息（sort_seq 水位线），重启后不会漏消息，
  也不会把已加入的日程重复加入；
- 只对「像日程」的文本消息做自动提取（含日期/时间/地点/截止/时长），
  其余消息记入活动日志但不生成待办，避免污染日程。

运行形态：由 server.py 在后台以守护线程启动；也可单独 import 调试。
"""
from __future__ import annotations

import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

from app.ai import gateway as ai_gateway
from app.core.extractor import parse_text
from app.core import kinds
from app.core.storage import rollover_unfinished_schedules

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.paths import DATA_DIR

# 只处理文本消息（微信 db 里 local_type=1 显示名为“文本”）
TEXT_TYPE = "文本"

# 具备任一字段即视为“像日程”
SIGNAL_FIELDS = (
    "date",
    "time",
    "end_time",
    "deadline",
    "deadline_time",
    "location",
    "duration_min",
)

TODO_FIELDS = (
    "title",
    "category",
    "priority",
    "date",
    "time",
    "end_time",
    "duration_min",
    "location",
    "deadline",
    "deadline_time",
    "raw",
)

ACTIVITY_CAP = 300


def _text_of_message(msg: dict) -> str | None:
    """取消息里的纯文本；非文本/占位返回 None。"""
    if str(msg.get("type") or "") != TEXT_TYPE:
        return None
    content = msg.get("content")
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or (text.startswith("[") and text.endswith("]")):
        return None
    return text

DEFAULT_CONFIG = {
    "auto_start": True,          # server 启动后自动尝试连接微信
    "watch": ["文件传输助手"],   # 监听哪些会话（昵称/备注/username 均可，逗号分隔）
    "watch_all": False,          # True = 监听全部可见会话（首次会扫描每个会话最近消息）
    "interval": 1.5,             # 轮询新消息间隔（秒）
    "backfill": 30,              # 首次连接/手动扫描时，每个会话回读最近 N 条消息
    "min_len": 4,                # 文本少于该字数直接忽略
    "ignore_self": False,        # True = 忽略自己发出的消息（注意转发到文件传输助手会变成自己发）
}

CONFIG_KEYS = {
    "auto_start": bool,
    "watch": list,
    "watch_all": bool,
    "interval": (int, float),
    "backfill": int,
    "min_len": int,
    "ignore_self": bool,
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _wechat_process_running() -> bool:
    """微信桌面端（Weixin.exe）进程是否在运行。

    wechatauto 只要本地有缓存的解密密钥，就能离线打开微信数据库；
    若桥接仅以“数据库可读”判断连接，微信未登录时会误报“已连接”。
    因此连接前与连接期间都以进程存在性作为“客户端在线”的门槛：
    进程不在 = 一定未登录，保持未连接并自动重试。
    """
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # tasklist 在本地位码（GBK/UTF-8 等）可能不同，按字节匹配 ASCII 进程名最稳
        return b"Weixin.exe" in (r.stdout or b"")
    except Exception:
        return False


class WeChatBridge:
    """微信桥接：连接管理 + 历史读取(backfill) + 实时监听 + 日程入库。"""

    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir or DATA_DIR
        # todos 默认与 campus_agent 共用 data/todos.json；
        # 传入自定义 data_dir（如测试）时隔离到该目录，避免污染正式数据。
        self.todo_file = (
            os.path.join(self.data_dir, "todos.json")
            if data_dir
            else os.path.join(DATA_DIR, "todos.json")
        )
        os.makedirs(self.data_dir, exist_ok=True)
        self.config_path = os.path.join(self.data_dir, "wechat_config.json")
        self.state_path = os.path.join(self.data_dir, "wechat_state.json")
        self.activity_path = os.path.join(self.data_dir, "wechat_activity.json")

        self._lock = threading.RLock()      # 保护 config/state/activity 与文件写入
        self._life = threading.RLock()      # 保护运行状态（线程/连接句柄）

        self._config = self._load_json(self.config_path, DEFAULT_CONFIG)
        self._state = self._load_json(self.state_path, {
            "account": None,
            "connected_at": None,
            "targets": [],
            "watermarks": {},
            "added_total": 0,
            "ignored_total": 0,
            "last_error": None,
            "last_scan_at": None,
        })
        self._activity = self._load_json(self.activity_path, [])

        self._running = False
        self._connecting = False
        self._connected = False
        self._reconfig = False
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._ai_thread: threading.Thread | None = None
        self._ai_queue: queue.Queue = queue.Queue()
        self._ai_attempted: set = set()   # 本进程内已尝试过 AI 的 (chat, seq)
        self._db = None                     # WeChatDB（惰性导入，不在此 import）
        self._listener = None               # Listener
        self._chat_names: dict = {}         # username -> 显示名
        self._sender_cache: dict = {}       # 发送者 username -> 显示名（回扫时避免反复查库）
        self._last_tick_persist = time.time()
        self._last_discover = 0.0

    # ------------------------------------------------------------------
    # todos 读写（与 storage.py 保持一致的原子写）
    # ------------------------------------------------------------------
    def _load_todos(self) -> list:
        try:
            with open(self.todo_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_todos(self, todos: list) -> None:
        # 微信监听线程也可能是跨日后的第一个写入方；写入前执行同一套顺延规则，
        # 避免它把尚未转换的旧日程重新覆盖回 todos.json。
        rollover_unfinished_schedules(todos)
        os.makedirs(os.path.dirname(self.todo_file), exist_ok=True)
        tmp = self.todo_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.todo_file)

    # ------------------------------------------------------------------
    # 文件读写（全部在锁内调用或本身加锁）
    # ------------------------------------------------------------------
    def _load_json(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {**default, **data}
            return default
        except (OSError, json.JSONDecodeError):
            return default

    def _save_json(self, path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _persist_state(self):
        with self._lock:
            self._state["targets"] = [
                {"username": t.get("username"), "display": t.get("display"),
                 "requested": t.get("requested")}
                for t in self._state.get("targets", [])
            ]
            self._save_json(self.state_path, self._state)

    # ------------------------------------------------------------------
    # 对外控制 API
    # ------------------------------------------------------------------
    def config(self) -> dict:
        with self._lock:
            return dict(self._config)

    def start(self) -> dict:
        """启动后台线程。线程内部会自动等待微信登录并连接。"""
        with self._life:
            if self._thread and self._thread.is_alive():
                return {"ok": True, "running": True}
            self._running = True
            self._reconfig = False
            self._thread = threading.Thread(
                target=self._loop, name="wx-bridge", daemon=True
            )
            self._thread.start()
            if not (self._ai_thread and self._ai_thread.is_alive()):
                self._ai_thread = threading.Thread(
                    target=self._ai_worker_loop, name="wx-ai-worker", daemon=True
                )
                self._ai_thread.start()
        return {"ok": True, "running": True}

    def stop(self) -> dict:
        with self._life:
            self._running = False
            t = self._thread
        self._wake.set()
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=8)
        if self._ai_thread and self._ai_thread.is_alive():
            self._ai_thread.join(timeout=5)
        return {"ok": True, "running": False}

    def request_reconnect(self):
        with self._life:
            self._reconfig = True
        self._wake.set()

    def update_config(self, patch: dict) -> dict:
        changed = False
        with self._lock:
            cfg = dict(self._config)
            for key, typ in CONFIG_KEYS.items():
                if key not in patch or patch[key] is None:
                    continue
                val = patch[key]
                if typ is list:
                    if not isinstance(val, list):
                        continue
                    val = [str(x).strip() for x in val if str(x).strip()]
                elif typ is bool:
                    val = bool(val)
                elif typ is int:
                    try:
                        val = max(0, int(val))
                    except (TypeError, ValueError):
                        continue
                elif typ in ((int, float),):
                    try:
                        val = max(0.5, float(val))
                    except (TypeError, ValueError):
                        continue
                if cfg.get(key) != val:
                    cfg[key] = val
                    changed = True
            self._config = cfg
            self._save_json(self.config_path, self._config)
        if changed:
            with self._life:
                if self._connected or self._thread and self._thread.is_alive():
                    self.request_reconnect()
        return self.status()

    def status(self) -> dict:
        with self._life:
            running = self._running
            connected = self._connected
            connecting = self._connecting
        with self._lock:
            st = {
                "ok": True,
                "running": running,
                "connected": connected,
                "connecting": connecting,
                "auto_start": bool(self._config.get("auto_start")),
                "watch": list(self._config.get("watch", [])),
                "watch_all": bool(self._config.get("watch_all")),
                "ignore_self": bool(self._config.get("ignore_self")),
                "backfill": int(self._config.get("backfill", 0)),
                "interval": float(self._config.get("interval", 1.5)),
                "account": self._state.get("account"),
                "targets": list(self._state.get("targets", [])),
                "last_error": self._state.get("last_error"),
                "added_total": int(self._state.get("added_total", 0)),
                "ignored_total": int(self._state.get("ignored_total", 0)),
                "last_scan_at": self._state.get("last_scan_at"),
                "activity": list(reversed(self._activity[-12:])),
            }
        return st

    def scan(self) -> dict:
        """立即回读所有监听会话的最近消息（重新跑一次提取逻辑，带去重）。"""
        with self._life:
            if not self._connected or self._db is None:
                return {
                    "ok": False,
                    "error": "尚未连接微信，无法扫描（请先登录微信并等待状态变为已连接）",
                    "status": self.status(),
                }
            db = self._db
        with self._lock:
            targets = list(self._state.get("targets", []))
            limit = int(self._config.get("backfill", 30)) or 30
            self._state["last_scan_at"] = _now_iso()
        added = ignored = ai_queued = 0
        for t in targets:
            r = self._backfill_chat(db, t, limit)
            added += r.get("added", 0)
            ignored += r.get("ignored", 0)
            ai_queued += r.get("ai_queued", 0)
        self._persist_state()
        self._info(f"手动扫描完成：新增 {added} 条，忽略 {ignored} 条"
                   + (f"，送 AI 分析 {ai_queued} 条" if ai_queued else ""))
        return {
            "ok": True,
            "added": added,
            "ignored": ignored,
            "ai_queued": ai_queued,
            "status": self.status(),
        }

    # ------------------------------------------------------------------
    # 后台主循环
    # ------------------------------------------------------------------
    def _loop(self):
        tick = 0
        while True:
            with self._life:
                if not self._running:
                    break
                if self._reconfig:
                    self._reconfig = False
                    reconnect = True
                else:
                    reconnect = False
            if reconnect:
                self._disconnect("监听配置已更新，正在重新连接…")
            try:
                with self._life:
                    connected = self._connected
                if not connected:
                    self._try_connect()
                else:
                    self._housekeeping(tick)
            except Exception as exc:
                msg = str(exc) or repr(exc)
                self._set_error(msg)
                self._disconnect(f"微信未就绪：{msg[:80]}，自动重试中")
            # 未连接时每 10 秒重试一次，避免频繁扫微信进程内存
            self._wake.clear()
            self._wake.wait(1 if connected else 10)
            tick += 1
        self._disconnect("桥接已停止")

    def _housekeeping(self, tick: int):
        with self._life:
            db = self._db
            lst = self._listener
        if lst is None or not lst._thread or not lst._thread.is_alive():
            raise RuntimeError("监听线程意外退出")
        # 微信退出/注销后数据库不再更新：断开并进入自动重试，
        # 避免进程已消失仍显示“已连接”的假象。
        if tick % 5 == 0 and not _wechat_process_running():
            raise RuntimeError("微信进程已退出，请重新登录微信桌面版，将自动重连")
        now = time.time()
        # 每 ~10s 落盘一次水位线；监听过程中宕机最多重放 10s 消息（去重可兜底）
        if now - self._last_tick_persist >= 10:
            self._last_tick_persist = now
            with self._lock:
                if lst is not None:
                    for user, seq in lst.watermark.items():
                        cur = int(self._state["watermarks"].get(user, 0) or 0)
                        if int(seq or 0) > cur:
                            self._state["watermarks"][user] = int(seq or 0)
            self._persist_state()
        # watch_all 模式下，每隔一段时间发现新会话
        with self._lock:
            watch_all = bool(self._config.get("watch_all"))
        if watch_all and db is not None and now - self._last_discover >= 20:
            self._last_discover = now
            self._discover_new_chats(db, lst)

    def _try_connect(self):
        with self._life:
            if self._connecting or self._connected:
                return
            self._connecting = True
        try:
            self._do_connect()
        finally:
            with self._life:
                self._connecting = False

    def _do_connect(self):
        if importlib.util.find_spec("wechatauto") is None:
            raise RuntimeError(
                "未安装 wechatauto-replica（请先 pip install -e wechatauto-replica-main 或安装该包）"
            )
        if not _wechat_process_running():
            raise RuntimeError(
                "未检测到微信进程（Weixin.exe）：请登录微信 4.x 桌面版并保持运行，"
                "桥接会每 10 秒自动重试"
            )
        from wechatauto.db import Listener, WeChatDB

        db = WeChatDB()  # 打开本地数据库；密钥缺失/无法解密时抛错，由外层进入等待重试
        info = db.get_self_info()
        account_user = (info.get("username") or "").strip()
        if account_user:
            with self._lock:
                old_account = (self._state.get("account") or {}).get("username") or ""
                if old_account and old_account != account_user:
                    # 换了微信号：旧水位线/会话不再适用，清空重新回读，避免漏消息
                    self._state["watermarks"] = {}
                    self._state["targets"] = []
                    self._state["added_total"] = 0
                    self._state["ignored_total"] = 0
                    self._persist_state()
        sessions = db.get_sessions(limit=300)
        with self._lock:
            watch_all = bool(self._config.get("watch_all"))
            backfill = int(self._config.get("backfill", 30) or 0)
            interval = max(0.5, float(self._config.get("interval", 1.5)))

        targets = self._resolve_targets(db, sessions, watch_all)
        if not targets:
            raise RuntimeError(
                "没有找到要监听的会话：请检查 data/wechat_config.json 的 watch 列表，"
                "或勾选“监听所有会话”"
            )

        # 1) 回读最近消息（“读取”）
        self._initial_catchup(db, targets, backfill)

        # 2) 水位线之后的增量交给 Listener（“监听”）
        with self._lock:
            watermark = {
                t["username"]: int(self._state["watermarks"].get(t["username"], 0) or 0)
                for t in targets
            }
        lst = Listener(db, interval=interval, watermark=watermark)
        for t in targets:
            lst.add_listener(t["username"], self._on_msg)
        lst.start()

        with self._life:
            self._db = db
            self._listener = lst
            self._connected = True
        self._chat_names = {t["username"]: t["display"] for t in targets}
        with self._lock:
            self._state["account"] = {
                "username": info.get("username"),
                "nick_name": info.get("nick_name") or info.get("remark") or info.get("username"),
            }
            self._state["targets"] = list(targets)
            self._state["connected_at"] = _now_iso()
            self._state["last_error"] = None
        self._persist_state()
        self._log_activity("", "", "info", "", "info",
                           f"已连接微信，开始监听 {len(targets)} 个会话")
        names = "、".join(t["display"] for t in targets[:8])
        if len(targets) > 8:
            names += f" 等 {len(targets)} 个会话"
        self._info(f"已连接微信（{info.get('nick_name') or info.get('username')}），监听：{names}")

    def _initial_catchup(self, db, targets: list, backfill: int) -> None:
        """连接/切换监听目标时的历史回读。

        - 显式 watch 模式：每次连接都对每个监听会话回读最近 backfill 条。
          否则“之前被 watch_all 推过水位线、或加入监听前积压”的消息永远
          不会被重新评估（已入库的按 wx_seq 去重，不会重复添加）；
        - watch_all 模式：只回读“新出现”的会话，避免每次连接全量扫描。
        """
        with self._lock:
            watch_all = bool(self._config.get("watch_all"))
        for t in targets:
            with self._lock:
                is_new = t["username"] not in self._state["watermarks"]
            if not watch_all or is_new:
                self._backfill_chat(db, t, backfill)

    def _resolve_targets(self, db, sessions, watch_all: bool):
        session_by_username = {s["username"]: s for s in sessions}

        def display_of(username: str) -> str:
            try:
                name = db.get_nickname(username)
                return name or username
            except Exception:
                return username

        targets = []
        seen = set()

        def add_target(username: str, requested: str):
            if username in seen:
                return
            seen.add(username)
            targets.append({
                "username": username,
                "display": display_of(username),
                "requested": requested,
            })

        if watch_all:
            for s in sessions:
                add_target(s["username"], s["username"])
            return targets

        alias = {"文件传输助手": "filehelper", "filehelper": "filehelper"}
        with self._lock:
            raw_names = [str(n).strip() for n in self._config.get("watch", []) if str(n).strip()]
        for name in raw_names:
            if name in session_by_username:
                add_target(name, name)
                continue
            if alias.get(name) and alias[name] in session_by_username:
                add_target(alias[name], name)
                continue
            try:
                found = db.search_contact(name) or []
            except Exception:
                found = []
            if not found:
                self._log_activity("", "", "error", name, "error",
                                   f"找不到会话「{name}」（尚未聊过天或名称不匹配）")
                continue
            exact = next(
                (r for r in found
                 if r.get("nick_name") == name or r.get("remark") == name),
                found[0],
            )
            add_target(exact["username"], name)
        return targets

    def _discover_new_chats(self, db, lst):
        try:
            sessions = db.get_sessions(limit=300)
        except Exception:
            return
        with self._lock:
            backfill = int(self._config.get("backfill", 30) or 0)
        for s in sessions:
            username = s["username"]
            with self._lock:
                known = username in self._state["watermarks"]
            if known:
                continue
            try:
                display = db.get_nickname(username) or username
            except Exception:
                display = username
            target = {"username": username, "display": display, "requested": username}
            lst.add_listener(username, self._on_msg)  # 内部水位线先设到最新
            self._chat_names[username] = display
            self._backfill_chat(db, target, backfill)

    # ------------------------------------------------------------------
    # 回读最近消息（历史扫描）
    # ------------------------------------------------------------------
    def _backfill_chat(self, db, target: dict, limit: int) -> dict:
        if not target or not target.get("username"):
            return {"added": 0, "ignored": 0, "ai_queued": 0}
        username = target["username"]
        added = ignored = 0
        try:
            msgs = db.get_messages(username, limit=limit) or []
        except Exception as exc:
            self._log_activity(target.get("display") or username, "", "error",
                               "", "error", f"读取最近消息失败：{exc}")
            return {"added": 0, "ignored": 0, "ai_queued": 0}
        max_seq = 0
        with self._lock:
            max_seq = int(self._state["watermarks"].get(username, 0) or 0)
        # get_messages 最新在前，这里转成旧→新
        ordered = list(reversed(msgs))
        ai_enabled = self._ai_enabled()
        ai_kept = []
        ai_queued = 0
        for msg in ordered:
            if ai_enabled:
                text = _text_of_message(msg)
                if text is None:
                    pass
                elif self._config_ignore_self() and int(msg.get("sender_id") or 0) == 2:
                    pass
                else:
                    with self._lock:
                        ai_cfg = self._ai_cfg_snapshot()
                    passed, why = ai_gateway.prefilter(
                        text,
                        min_len=ai_cfg.get("min_len", 10),
                        require_time_word=ai_cfg.get("require_time_word", True),
                    )
                    if passed:
                        try:
                            mseq = int(msg.get("sort_seq") or 0)
                        except (TypeError, ValueError):
                            mseq = 0
                        # 已成功分析过、或本进程已入队 → 不再重复调 AI
                        if not self._claim_ai(username, mseq):
                            continue
                        ai_kept.append(msg)
                    else:
                        self._log_activity(
                            target.get("display") or username,
                            self._sender_label(db, msg), "文本", text, "ignored",
                            "AI预筛未通过：" + why,
                        )
                        ignored += 1
            else:
                action = self._process_message(db, target, msg)
                if action == "added":
                    added += 1
                elif action == "ignored":
                    ignored += 1
            try:
                seq = int(msg.get("sort_seq") or 0)
                if seq > max_seq:
                    max_seq = seq
            except (TypeError, ValueError):
                pass
        if ai_kept:
            self._ai_queue.put({
                "kind": "batch",
                "target": target,
                "msgs": ai_kept,
            })
            ai_queued = len(ai_kept)
        with self._lock:
            self._state["watermarks"][username] = max_seq
        return {"added": added, "ignored": ignored, "ai_queued": ai_queued}

    # ------------------------------------------------------------------
    # 消息处理：提取日程 → 写入 todos.json
    # ------------------------------------------------------------------
    def _on_msg(self, msg: dict, lst) -> None:
        user = msg.get("username") or ""
        target = {
            "username": user,
            "display": self._chat_names.get(user, user),
            "requested": user,
        }
        self._process_message(self._db, target, msg)

    def _process_message(self, db, target: dict, msg: dict) -> str:
        mtype = str(msg.get("type") or "")
        if mtype != TEXT_TYPE:
            return "skip"
        content = msg.get("content")
        if not isinstance(content, str):
            return "skip"
        text = content.strip()
        if not text or (text.startswith("[") and text.endswith("]")):
            return "skip"

        username = target.get("username") or ""
        chat_display = target.get("display") or username or "未知会话"
        sender = self._sender_label(db, msg)
        if username == "filehelper":
            # 文件传输助手本质是“发给自己的便签”。微信 4.x 的数据库里
            # 这些消息的 sender 记录不可靠（实测会落到某个好友 id 上），
            # 统一按“我”处理，避免张冠李戴。
            sender = "我"
        seq = 0
        try:
            seq = int(msg.get("sort_seq") or 0)
        except (TypeError, ValueError):
            pass

        with self._lock:
            min_len = int(self._config.get("min_len", 4) or 0)
            ignore_self = bool(self._config.get("ignore_self"))
        if ignore_self and int(msg.get("sender_id") or 0) == 2:
            return "skip"
        # ---- AI 模式：预筛通过后送 AI（结果进“待采纳”，不直接入库） ----
        ai_cfg = self._ai_cfg_snapshot()
        if ai_gateway.is_ready(ai_cfg):
            passed, why = ai_gateway.prefilter(
                text,
                min_len=ai_cfg.get("min_len", 10),
                require_time_word=ai_cfg.get("require_time_word", True),
            )
            if not passed:
                self._log_activity(chat_display, sender, mtype, text, "ignored",
                                   "AI预筛未通过：" + why)
                self._bump("ignored_total")
                return "ignored"
            if not self._claim_ai(username, seq):
                # 回读扫描已经送过 AI（或正在送），实时回调不再重复排队
                return "duplicate"
            self._ai_queue.put({
                "kind": "live",
                "target": {"username": username, "display": chat_display, "requested": username},
                "msg": dict(msg),
            })
            return "ai_queued"
        # ---- 规则模式（AI 未启用时保留原自动入库逻辑） ----
        if len(text) < min_len:
            self._log_activity(chat_display, sender, mtype, text, "ignored",
                               f"内容过短（{len(text)}字），不像日程")
            self._bump("ignored_total")
            return "ignored"

        # 历史消息里的“明天/下周三”应以消息发出当天为基准解析
        mdate = mnow = None
        try:
            ts = float(msg.get("create_time") or 0)
            if ts > 0:
                mnow = datetime.fromtimestamp(ts)
                mdate = mnow.date()
        except (TypeError, ValueError, OSError):
            pass
        try:
            parsed = parse_text(text, today=mdate, now=mnow) if mdate else parse_text(text)
        except Exception as exc:
            self._log_activity(chat_display, sender, mtype, text, "error",
                               f"解析失败：{exc}")
            return "error"
        if not parsed.get("ok"):
            self._log_activity(chat_display, sender, mtype, text, "ignored",
                               parsed.get("error") or "无法解析")
            self._bump("ignored_total")
            return "ignored"
        if not any(parsed.get(k) for k in SIGNAL_FIELDS):
            self._log_activity(chat_display, sender, mtype, text, "ignored",
                               "未检测到时间/日期/地点/截止等日程要素")
            self._bump("ignored_total")
            return "ignored"

        # 区分日程/待办并做字段归一化（“X日前交”归为带截止的待办）
        norm = kinds.normalize_item(
            {k: parsed.get(k) for k in TODO_FIELDS},
            raw=text,
        )
        # 入库（带去重：同一会话同一条 sort_seq 只入一次）
        todo = {
            **norm,
            "id": uuid.uuid4().hex[:10],
            "status": "pending",
            "created_at": _now_iso(),
            "source": "wechat",
            "wx_chat": username,
            "wx_chat_name": chat_display,
            "wx_sender": sender,
            "wx_seq": seq,
            "wx_local_id": msg.get("local_id"),
            "wx_ts": msg.get("create_time"),
        }
        with self._lock:
            todos = self._load_todos()
            if any(
                t.get("wx_chat") == username and t.get("wx_seq") == seq
                for t in todos
            ):
                return "duplicate"
            todos.append(todo)
            self._save_todos(todos)
            self._state["added_total"] = int(self._state.get("added_total", 0)) + 1
        when = f"{norm.get('date') or ''} {norm.get('time') or ''}".strip()
        self._log_activity(
            chat_display, sender, mtype, text, "added",
            f"已加入「{norm.get('title') or parsed.get('title')}」"
            + (f"（{when}）" if when else "")
            + (f" 类型：{'日程' if norm.get('kind') == kinds.KIND_SCHEDULE else '待办'}"),
        )
        self._info(f"[{chat_display}] {sender} → 已提取「{norm.get('title') or parsed.get('title')}」{when}")
        return "added"

    # ------------------------------------------------------------------
    # AI Gateway 后台任务
    # ------------------------------------------------------------------
    def _ai_cfg_snapshot(self) -> dict:
        try:
            return ai_gateway.load_config(self.data_dir)
        except Exception:
            return {}

    def _ai_enabled(self) -> bool:
        try:
            return ai_gateway.is_ready(self._ai_cfg_snapshot())
        except Exception:
            return False

    def _config_ignore_self(self) -> bool:
        with self._lock:
            return bool(self._config.get("ignore_self"))

    def _claim_ai(self, username: str, seq) -> bool:
        """原子地认领一条消息：本进程已送过 AI / 已成功分析过则不再重复入队。

        回读扫描与实时监听可能并发碰到同一条消息，若不加锁认领，
        同一 seq 会被排队两次，出现“AI 结果 + 规则兜底”两条重复待采纳。
        """
        try:
            seq = int(seq or 0)
        except (TypeError, ValueError):
            seq = 0
        if not username or not seq:
            return False
        with self._lock:
            key = (username, seq)
            if key in self._ai_attempted:
                return False
            if ai_gateway.is_ai_seen(username, seq, self.data_dir):
                return False
            self._ai_attempted.add(key)
            return True

    def _ai_worker_loop(self):
        while True:
            with self._life:
                if not self._running:
                    break
            try:
                job = self._ai_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_ai_job(job)
            except Exception as exc:
                self._info("AI 任务异常：" + repr(exc))

    def _fallback_worth(self, fields: dict) -> bool:
        """规则兜底是否值得生成：要有标题、有明确日程/截止要素、没过期、标题正常。"""
        f = fields or {}
        title = str(f.get("title") or "").strip()
        if len(title) < 2 or title.startswith("@"):
            return False
        if not any(f.get(k) for k in SIGNAL_FIELDS):
            return False
        if ai_gateway.is_pending_expired(f):
            return False
        return True

    def _run_ai_job(self, job: dict):
        cfg = self._ai_cfg_snapshot()
        if not ai_gateway.is_ready(cfg):
            return
        with self._life:
            db = self._db
        if db is None:
            return
        target = job.get("target") or {}
        chat = str(target.get("username") or "")
        display = target.get("display") or chat or "未知会话"
        kind = job.get("kind")
        msg = job.get("msg")
        msgs = list(job.get("msgs") or [])

        try:
            if kind == "batch":
                # 一批塞太多长通知容易让输出被截断 → 拆成每批 ≤ 8 条
                if len(msgs) > 8:
                    for i in range(8, len(msgs), 8):
                        self._ai_queue.put({
                            "kind": "batch",
                            "target": target,
                            "msgs": msgs[i:i + 8],
                        })
                    msgs = msgs[:8]
                context = list(msgs)
                focus_seq = None
                candidates = list(msgs)
            else:
                recent = list(reversed(db.get_messages(chat, limit=8) or []))
                merged = {}
                for m in recent + ([msg] if msg else []):
                    try:
                        merged[int(m.get("sort_seq") or 0)] = m
                    except (TypeError, ValueError):
                        continue
                context = [merged[k] for k in sorted(merged)]
                focus_seq = 0
                try:
                    focus_seq = int((msg or {}).get("sort_seq") or 0)
                except (TypeError, ValueError):
                    pass
                candidates = [msg] if msg else []
        except Exception as exc:
            self._info("AI 上下文读取失败：" + repr(exc))
            return

        entries = []
        by_seq = {}
        for m in context:
            try:
                seq = int(m.get("sort_seq") or 0)
            except (TypeError, ValueError):
                continue
            by_seq[seq] = m
            entries.append({
                "seq": seq,
                "ts": m.get("create_time") or 0,
                "sender": self._sender_label(db, m),
                "content": str(m.get("content") or ""),
            })
        if not entries:
            return

        def mark_candidates_done():
            for src in candidates:
                try:
                    mseq = int(src.get("sort_seq") or 0)
                except (TypeError, ValueError):
                    continue
                try:
                    ai_gateway.mark_ai_seen(chat, mseq, self.data_dir)
                except Exception:
                    pass

        try:
            items = ai_gateway.analyze(cfg, entries, focus_seq=focus_seq or None)
            mark_candidates_done()
            # 模型若仍返回已过期安排，直接过滤：过期内容不进入待采纳
            items = [it for it in items
                     if not ai_gateway.is_pending_expired(it.get("fields") or {})]
            if not items:
                # AI 明确判定“没有日程/待办”时绝不兜底；
                # 若同一条消息此前因偶发失败残留了规则兜底，也一并清掉，
                # 避免“过期通知 AI 已 pass，规则兜底却蹦出来”的假象。
                for src in candidates:
                    try:
                        mseq = int(src.get("sort_seq") or 0)
                    except (TypeError, ValueError):
                        continue
                    ai_gateway.remove_pending_for_seq(
                        chat, mseq, methods=("rule-fallback",), data_dir=self.data_dir
                    )
                self._log_activity(display, "", "文本", "", "ignored",
                                   "AI：未识别到日程/待办，或内容已过期（不启用规则兜底）")
                self._bump("ignored_total")
                return
            for it in items:
                try:
                    seq = int(it.get("seq") or 0)
                except (TypeError, ValueError):
                    seq = 0
                src = by_seq.get(seq)
                if src is None and candidates:
                    src = candidates[-1]
                if src is None:
                    continue
                self._append_ai_pending(db, target, src, it.get("fields", {}),
                                        "ai", it.get("confidence"),
                                        it.get("reason") or "", None)
        except ai_gateway.AiGatewayError as exc:
            self._info(f"[AI] {display} 分析失败，退回规则解析：{exc}")
            for src in candidates:
                fields = ai_gateway.rule_fields(
                    str(src.get("content") or ""), src.get("create_time")
                )
                if not self._fallback_worth(fields):
                    # AI 确实失败了，但规则结果没抓住有效要素 / 已过期 /
                    # 标题是“@所有人…”之类的噪声 → 不值得兜底，标记后不再重扫
                    try:
                        mseq = int(src.get("sort_seq") or 0)
                    except (TypeError, ValueError):
                        mseq = 0
                    self._info(f"[AI] {display} 分析失败且规则兜底无有效内容，跳过")
                    ai_gateway.mark_ai_seen(chat, mseq, self.data_dir)
                    continue
                self._append_ai_pending(db, target, src, fields, "rule-fallback",
                                        None, None, str(exc))

    def _append_ai_pending(self, db, target: dict, src: dict, fields: dict,
                           method: str, confidence, reason: str, error):
        chat = str(target.get("username") or "")
        display = target.get("display") or chat or "未知会话"
        sender = self._sender_label(db, src)
        if chat == "filehelper":
            sender = "我"
        seq_val = 0
        try:
            seq_val = int(src.get("sort_seq") or 0)
        except (TypeError, ValueError):
            pass
        if method == "rule-fallback" and ai_gateway.has_pending_item(
            chat, seq_val, method="ai", data_dir=self.data_dir
        ):
            # 同一消息已有 AI 成功结果，规则兜底不再叠加
            return None
        if method == "ai":
            # AI 结果比规则兜底更准：同一条消息之前的兜底结果让位
            ai_gateway.remove_pending_for_seq(
                chat, seq_val, methods=("rule-fallback",), data_dir=self.data_dir
            )
        fields = dict(fields or {})
        raw = str(src.get("content") or "")
        fields = kinds.normalize_item(fields, raw=raw)
        entry = {
            "id": ai_gateway.new_pending_id(),
            "chat_username": chat,
            "chat_display": display,
            "sender": sender,
            "seq": seq_val,
            "local_id": src.get("local_id"),
            "msg_ts": src.get("create_time"),
            "raw": raw,
            "fields": fields,
            "method": method,
            "confidence": confidence,
            "reason": reason or "",
            "error": error,
            "created_at": _now_iso(),
            "status": "pending",
        }
        ai_gateway.add_pending(entry, self.data_dir)
        title = (fields or {}).get("title") or "未命名事项"
        self._info(f"[AI] {display} → 已生成待采纳「{title}」（{method}）")

    def _sender_label(self, db, msg: dict) -> str:
        try:
            sid = int(msg.get("sender_id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid == 2:
            return "我"
        su = msg.get("sender_username") or ""
        if su and db is not None:
            if su in self._sender_cache:
                return self._sender_cache[su]
            try:
                name = db.get_nickname(su)
                if name:
                    self._sender_cache[su] = name
                    if len(self._sender_cache) > 2000:
                        self._sender_cache.clear()
                    return name
            except Exception:
                pass
        return f"用户{sid}" if sid else "未知"

    def _bump(self, key: str, n: int = 1):
        with self._lock:
            self._state[key] = int(self._state.get(key, 0)) + n

    # ------------------------------------------------------------------
    # 活动日志 / 状态错误
    # ------------------------------------------------------------------
    def _log_activity(self, chat, sender, mtype, content, action, detail):
        snippet = " ".join(str(content or "").split())[:80]
        entry = {
            "ts": _now_iso(),
            "chat": str(chat or ""),
            "sender": str(sender or ""),
            "type": str(mtype or ""),
            "content": snippet,
            "action": str(action or "info"),
            "detail": str(detail or ""),
        }
        with self._lock:
            self._activity.append(entry)
            if len(self._activity) > ACTIVITY_CAP:
                self._activity = self._activity[-ACTIVITY_CAP:]
            self._save_json(self.activity_path, self._activity)

    def _set_error(self, msg: str):
        with self._lock:
            self._state["last_error"] = msg
            self._save_json(self.state_path, self._state)
        self._info("等待重试：" + msg)

    def _info(self, msg: str):
        if self._config.get("verbose", True):
            try:
                print("[微信] " + msg, flush=True)
            except Exception:
                pass

    def _disconnect(self, reason: str = ""):
        with self._life:
            self._connected = False
            db, lst = self._db, self._listener
            self._db = None
            self._listener = None
        if lst is not None:
            try:
                lst.stop()
            except Exception:
                pass
        # 落盘最新水位线，下次启动从断点续读
        self._persist_state()
        if reason and self._running:
            self._log_activity("", "", "info", "", "info", reason)


# 供 server.py 直接使用的单例
BRIDGE = WeChatBridge()


if __name__ == "__main__":
    # 便于单独调试：python wechat_bridge.py
    bridge = WeChatBridge()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("微信桥接（调试模式）：连接微信后自动监听")
    print("按 Ctrl+C 退出")
    bridge.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
