# -*- coding: utf-8 -*-
"""AI Gateway：把微信消息交给大模型做日程提取，结果先进“待采纳”，用户确认后才入库。

设计要点
--------
- 配置存在 data/ai_config.json，支持任意 OpenAI 兼容接口（服务商/模型/API Key
  由用户在前端选择并粘贴）；
- 上传前先用规则预筛：文本需达到最短字数（默认 10 字）且含明显时间词，
  减少无效调用与费用；
- 消息内容允许发送到用户所选的 API 服务（由用户显式启用后才会上传）；
- 模型输出严格 JSON，逐字段校验清洗后再展示给用户采纳；
- AI 失败时退回规则引擎的解析结果作为“待采纳”，保证不漏事。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime

from app.core.extractor import parse_text
from app.core import kinds
from app.paths import DATA_DIR

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
AI_CONFIG_FILE = os.path.join(DATA_DIR, "ai_config.json")
AI_PENDING_FILE = os.path.join(DATA_DIR, "ai_pending.json")
AI_SEEN_FILE = os.path.join(DATA_DIR, "ai_seen.json")

PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "moonshot": {
        "label": "Moonshot (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "custom": {
        "label": "自定义",
        "base_url": "",
        "model": "",
    },
}

DEFAULT_CONFIG = {
    "enabled": False,
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "",
    "timeout": 45,
    "min_len": 10,          # 预筛：少于该字数不送 AI
    "require_time_word": True,   # 预筛：无时间词不送 AI
    "max_pending": 200,
}

ALLOWED_FIELDS = (
    "title", "category", "priority", "kind", "date", "time", "end_time",
    "duration_min", "location", "deadline", "deadline_time",
)

CATEGORY_SET = {
    "exam", "homework", "class", "deadline", "meeting",
    "activity", "social", "health", "other",
}

# 明显时间词（预筛用）：日期/星期/时刻/时段/截止类
TIME_WORD_RE = re.compile(
    r"(今天|今日|明天|明日|明晚|明早|今晚|今早|昨夜|昨晚|昨天|昨日|前天|"
    r"后天|大后天|周末|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"礼拜[一二三四五六日天]|下[周个月]|本[周月]|这个月|下月|月底|月初|"
    r"上午|早上|早晨|中午|下午|傍晚|晚上|夜里|凌晨|午夜|白天|"
    r"(\d{4}年)?\d{1,2}月\d{1,2}[日号]?|\d{1,2}[日号]|\d{1,2}[:：]\d{1,2}|"
    r"\d{1,2}[点时]半?|一刻钟|半小时|截止|ddl|deadline|提交|报名|报到|"
    r"集合时间|调整(到|为)|前交|之前|到会|开始时间|结束时间|时间|日期)"
)

# 日程线索（不一定有具体时刻，但明显与安排相关）
SCHEDULE_HINT_RE = re.compile(
    r"(后|结束后|之后|以后|随后|集合|开展|举行|进行|组织|安排|通知|开始|"
    r"报到|培训|讲座|活动|比赛|例会|开会|考试|上课|排练|彩排|值班|待办)"
)

# 明确地点线索：“到/去/在/前往 + 地点”（含 教一1101、二教401 这类）
LOCATION_RE = re.compile(
    r"(?:到|去|在|前往|至)\s*[^\s，。,.!?！？;；]{0,12}?"
    r"(?:教室|教学楼|楼|馆|室|场|地|堂|操场|礼堂|食堂|图书馆|中心|办公室|"
    r"实验室|报告厅|体育馆|游泳馆|校医院|宿舍|校区|公园|球场|医院|院|园|"
    r"教[一二三四五六七八九十]?\d*)"
)


def _has_schedule_hint(text: str) -> bool:
    return bool(SCHEDULE_HINT_RE.search(text) or LOCATION_RE.search(text))

_LOCK = threading.RLock()
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class AiGatewayError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 路径（测试可指定隔离目录）
# ---------------------------------------------------------------------------
def _paths(data_dir: str | None = None):
    if data_dir:
        return {
            "config": os.path.join(data_dir, "ai_config.json"),
            "pending": os.path.join(data_dir, "ai_pending.json"),
            "seen": os.path.join(data_dir, "ai_seen.json"),
        }
    return {
        "config": AI_CONFIG_FILE,
        "pending": AI_PENDING_FILE,
        "seen": AI_SEEN_FILE,
    }


def _seen_key(chat: str, seq) -> str:
    return "%s|%s" % (str(chat or ""), int(seq or 0))


def is_ai_seen(chat: str, seq, data_dir: str | None = None) -> bool:
    """该消息是否已成功做过一次 AI 分析（避免扫描/重连重复调用）。"""
    try:
        with _LOCK:
            path = _paths(data_dir)["seen"]
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _seen_key(chat, seq) in data
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def mark_ai_seen(chat: str, seq, data_dir: str | None = None) -> None:
    """记录“已成功分析”，最多保留 3000 条最近记录。"""
    with _LOCK:
        path = _paths(data_dir)["seen"]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        data[_seen_key(chat, seq)] = datetime.now().isoformat(timespec="seconds")
        if len(data) > 3000:
            # 删除最旧的一半，防止文件无限膨胀
            for k in list(data)[: len(data) // 2]:
                data.pop(k, None)
        _save_json(path, data)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {**default, **data}
        return default
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config(data_dir: str | None = None) -> dict:
    paths = _paths(data_dir)
    cfg = _load_json(paths["config"], DEFAULT_CONFIG)
    if cfg.get("provider") in PROVIDERS and cfg.get("provider") != "custom":
        preset = PROVIDERS[cfg["provider"]]
        if not cfg.get("base_url"):
            cfg["base_url"] = preset["base_url"]
    return cfg


def is_ready(cfg: dict) -> bool:
    return bool(
        cfg.get("enabled")
        and str(cfg.get("api_key") or "").strip()
        and str(cfg.get("base_url") or "").strip()
        and str(cfg.get("model") or "").strip()
    )


def save_config(cfg: dict, data_dir: str | None = None) -> dict:
    """清洗并保存配置；返回脱敏后的配置（不含 api_key）。"""
    clean = dict(DEFAULT_CONFIG)
    for k in clean:
        if k in cfg and cfg[k] is not None:
            clean[k] = cfg[k]
    clean["provider"] = str(clean["provider"] or "custom")
    if clean["provider"] in PROVIDERS and clean["provider"] != "custom":
        preset = PROVIDERS[clean["provider"]]
        clean["base_url"] = str(preset["base_url"])
        model = str(clean.get("model") or "").strip()
        if not model or model == str(DEFAULT_CONFIG.get("model") or ""):
            # 未填模型，或仍带着默认的 OpenAI 模型 → 用所选服务商默认模型
            clean["model"] = preset["model"]
    clean["base_url"] = str(clean.get("base_url") or "").strip().rstrip("/")
    clean["model"] = str(clean.get("model") or "").strip()
    clean["api_key"] = str(clean.get("api_key") or "").strip()
    clean["enabled"] = bool(clean.get("enabled"))
    clean["timeout"] = max(5, int(clean.get("timeout") or 45))
    clean["min_len"] = max(0, int(clean.get("min_len") or 10))
    clean["max_pending"] = max(20, int(clean.get("max_pending") or 200))
    paths = _paths(data_dir)
    with _LOCK:
        _save_json(paths["config"], clean)
    return public_config(clean)


def public_config(cfg: dict) -> dict:
    """返回可安全给前端展示的配置（API Key 用 has_key 代替）。"""
    return {
        "enabled": bool(cfg.get("enabled")),
        "provider": cfg.get("provider") or "custom",
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "has_key": bool((cfg.get("api_key") or "").strip()),
        "min_len": int(cfg.get("min_len") or 10),
        "require_time_word": bool(cfg.get("require_time_word", True)),
        "timeout": int(cfg.get("timeout") or 45),
    }


# ---------------------------------------------------------------------------
# 规则预筛（送 AI 之前）
# ---------------------------------------------------------------------------
def prefilter(text: str, min_len: int = 10, require_time_word: bool = True):
    """返回 (是否通过, 未通过原因)。通过才允许上传 AI。"""
    text = (text or "").strip()
    if not text:
        return False, "内容为空"
    if len(text) < max(0, int(min_len or 10)):
        return False, "字数不足（<%s 字），可能只是闲聊" % min_len
    if require_time_word and not (TIME_WORD_RE.search(text) or _has_schedule_hint(text)):
        return False, "未检测到时间词/地点/安排线索，按预筛规则不送 AI"
    return True, ""


# ---------------------------------------------------------------------------
# OpenAI 兼容 HTTP 调用
# ---------------------------------------------------------------------------
def _chat_completion(cfg: dict, messages: list) -> str:
    """调用 /chat/completions，返回 content 文本。"""
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise AiGatewayError("未配置接口地址 base_url")
    url = base + "/chat/completions"
    payload = {
        "model": str(cfg.get("model") or "").strip(),
        "messages": messages,
        "temperature": 0.1,
        "stream": False,
        "max_tokens": 4000,  # 长通知多日程时避免输出被截断
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + str(cfg.get("api_key") or "").strip(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=int(cfg.get("timeout") or 45)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        raise AiGatewayError("AI 接口返回 HTTP %s：%s" % (exc.code, snippet))
    except urllib.error.URLError as exc:
        raise AiGatewayError("无法连接 AI 接口：%s" % exc.reason)
    except json.JSONDecodeError:
        raise AiGatewayError("AI 接口返回的不是合法 JSON")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AiGatewayError("AI 接口响应缺少 choices[0].message.content")


def chat_completion(cfg: dict, messages: list) -> str:
    """Gateway 公开入口：把多轮对话消息发给当前配置的服务商。"""
    return _chat_completion(cfg, messages)


SYSTEM_PROMPT = """你是“校园管家”的日程提取助手。微信群里经常发“长通知”，
一条消息里可能包含多日、多时段的若干条日程，你必须逐条完整提取——宁可多列，
不可漏项；没有任何日程时才返回空数组。

输入是 JSON messages 数组，每条含数字 seq、发送时间 ts、发送者 sender、content。
提取范围：真正需要到场/执行/准备/截止的安排，例如讲座、大会、选课、考试、
训练/操课、演练、彩排、集合、活动、学唱、体检、交材料、报名截止等。

硬性规则：
0. 每条日程先判断类型 kind：
   - "schedule"（日程）：有固定开始时间或地点的安排（开会、上课、比赛、活动、
     选课、演练、仪式等），进入“日程”；
   - "todo"（待办）：只有截止时间、或纯任务没有固定时间（交作业、写论文、
     填问卷、报名、买/取东西等），进入“待办”，时间写入 deadline/deadline_time。
1. 一条消息可产出一条或多条日程，items 数量不限（常见 1~6 条）。长通知中
   每个独立的“【时间】安排”段落都要单独输出，禁止只挑“最重要的那一条”；
2. 同一天多个时段要分开成多条，例如“上午操课 / 下午操课 / 晚上操课”应输出
   3 条，而不是合并成 1 条；
3. 明确给出开始时间则填 time；给出结束时间则填 end_time，例如
   “15:00-17:00” → time=“15:00”、end_time=“17:00”。只有“上午/下午/晚上”
   没有具体钟点时 time 置 null，但该日程仍要输出，不得因没有时刻而删除；
4. “今天/明天/下周一/2号/下午3点”等相对时间一律以该消息的发送日期为基准
   （北京时间）换算；消息正文写明的日期（如“9月3日”）优先于消息发送日期；
5. 同日同时段、不同适用对象的活动（如“九连…看陕北公学”“二连…看陕北公学”）
   可合并为一条，在 reason 中注明适用对象；不同时段即使名称相同也要分开；
6. 纯闲聊、问候、天气播报、保密提醒、防暑建议等不要提取；
7. 所有内容必须来自输入文本，禁止编造；无法确定的时间/地点置 null，
   把握不足时降低 confidence 并在 reason 里说明。
8. 消息里的日期/时间已经过去（早于该消息发送当天，或当天对应时段已结束）
   视为过期通知，不要提取。

示例（帮助你理解“一条消息多条日程”）：
消息内容：“明日9月3日：【上午】本科新生选课；【15:00-17:00，训练场地】
全要素演练、闭营仪式彩排；【18:30-20:30，训练场地】全要素演练、闭营仪式彩排。”
应输出 3 个 items：
- 本科新生选课：date=9月3日，time=null；
- 全要素演练、闭营仪式彩排（下午）：date=9月3日，time=“15:00”，
  end_time=“17:00”，location=“训练场地”；
- 全要素演练、闭营仪式彩排（晚上）：date=9月3日，time=“18:30”，
  end_time=“20:30”，location=“训练场地”。

只输出严格 JSON，不要 Markdown 代码块，结构为：
{"items":[{"seq":<来源消息数字 seq>,"title":"简短标题",
"category":"exam|homework|class|deadline|meeting|activity|social|health|other",
"kind":"schedule|todo",
"priority":"high|medium|low","date":"YYYY-MM-DD 或 null",
"time":"HH:MM 或 null","end_time":"HH:MM 或 null","duration_min":数字或 null,
"location":"地点或 null","deadline":"YYYY-MM-DD 或 null",
"deadline_time":"HH:MM 或 null","confidence":0到1,"reason":"一句话依据"}]}
注意：kind=todo 时，只填 deadline/deadline_time（如“周五前交”→ deadline=周五），
不要填 date/time；kind=schedule 时填 date/time/location，deadline 一般不填。
没有日程时返回 {"items": []}。
"""


def _msg_entry(msg: dict) -> dict:
    ts = msg.get("ts") or msg.get("create_time") or 0
    try:
        ts_iso = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        ts_iso = ""
    return {
        "seq": int(msg.get("seq") or msg.get("sort_seq") or 0),
        "ts": ts_iso,
        "sender": str(msg.get("sender") or ""),
        "content": str(msg.get("content") or ""),
    }


def analyze(
    cfg: dict,
    messages: list,
    focus_seq: int | None = None,
    data_dir: str | None = None,
) -> list:
    """messages: 按时间升序的 [{seq/sort_seq, ts/create_time, sender, content}]。

    focus_seq 非空表示只分析该条（其余为上下文）；为空则分析整个窗口。
    返回规范化后的 items 列表（含 seq / title / 字段 / confidence / reason）。
    """
    if not is_ready(cfg):
        raise AiGatewayError("AI 未启用或配置不完整（需 enabled + API Key + base_url + model）")
    if not messages:
        return []
    entries = [_msg_entry(m) for m in messages]
    user_prompt = json.dumps({"messages": entries}, ensure_ascii=False)
    if focus_seq is not None:
        user_prompt += (
            "\n\n请只分析 seq=%s 这一条消息是否构成日程；其它消息仅作上下文参考。"
            % int(focus_seq)
        )
    content = _chat_completion(
        cfg,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return _parse_items(content)


def test_connection(cfg: dict) -> str:
    """轻量连通性测试。"""
    if not str(cfg.get("base_url") or "").strip():
        raise AiGatewayError("未配置接口地址")
    if not str(cfg.get("model") or "").strip():
        raise AiGatewayError("未配置模型")
    if not str(cfg.get("api_key") or "").strip():
        raise AiGatewayError("未填写 API Key")
    content = _chat_completion(
        cfg,
        [
            {"role": "user", "content": "只回复两个字：正常"},
        ],
    )
    return str(content or "").strip()[:50]


def _strip_json_fence(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _parse_items(content: str) -> list:
    text = _strip_json_fence(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AiGatewayError("AI 输出不是合法 JSON：%s" % exc)
    items = data.get("items") if isinstance(data, dict) else None
    if items is None and isinstance(data, list):
        items = data
    if not isinstance(items, list):
        raise AiGatewayError("AI 输出缺少 items 数组")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        fields = normalize_item(it)
        if not fields.get("title"):
            continue
        out.append({
            "seq": int(it.get("seq") or 0),
            "fields": fields,
            "confidence": _to_float(it.get("confidence")),
            "reason": str(it.get("reason") or "").strip()[:200],
        })
    return out


def _to_float(v):
    try:
        f = float(v)
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return None


def normalize_item(it: dict) -> dict:
    """把模型字段清洗成校园管家 todo 字段。"""
    fields = {k: None for k in ALLOWED_FIELDS}
    title = str(it.get("title") or "").strip()
    if len(title) > 80:
        title = title[:80] + "…"
    fields["title"] = title or None
    fields["kind"] = kinds.valid_kind(it.get("kind"))
    cat = str(it.get("category") or "other").strip().lower()
    fields["category"] = cat if cat in CATEGORY_SET else "other"
    prio = str(it.get("priority") or "medium").strip().lower()
    fields["priority"] = prio if prio in ("high", "medium", "low") else "medium"
    for key in ("date", "deadline"):
        v = str(it.get(key) or "").strip()
        fields[key] = v if _valid_date(v) else None
    for key in ("time", "end_time", "deadline_time"):
        v = str(it.get(key) or "").strip()
        fields[key] = _norm_time(v)
    try:
        dur = int(float(it.get("duration_min")))
        fields["duration_min"] = dur if dur >= 0 else None
    except (TypeError, ValueError):
        fields["duration_min"] = None
    loc = str(it.get("location") or "").strip()
    fields["location"] = loc[:80] or None
    return fields


def _valid_date(v: str) -> bool:
    if not _DATE_RE.match(v or ""):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _norm_time(v: str) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    if _TIME_RE.match(v):
        hh, mm = v.split(":")
        return "%02d:%02d" % (int(hh), int(mm))
    # 容忍 “15点”“下午3点” 之类简单写法
    m = re.search(r"(\d{1,2})[点时]", v)
    if m:
        hh = int(m.group(1))
        if v.startswith(("下午", "晚上")) and hh < 12:
            hh += 12
        return "%02d:00" % hh if 0 <= hh <= 23 else None
    return None


# ---------------------------------------------------------------------------
# 规则兜底（AI 失败时仍然给出可采纳的解析结果）
# ---------------------------------------------------------------------------
def rule_fields(text: str, msg_ts=None) -> dict:
    today = None
    now = None
    try:
        ts = float(msg_ts or 0)
        if ts > 0:
            now = datetime.fromtimestamp(ts)
            today = now.date()
    except (TypeError, ValueError, OSError):
        pass
    try:
        parsed = parse_text(text, today=today, now=now) if today else parse_text(text)
    except Exception:
        parsed = {"ok": False}
    if not parsed.get("ok"):
        return {}
    return {k: parsed.get(k) for k in ALLOWED_FIELDS}


# ---------------------------------------------------------------------------
# 待采纳队列（本地 JSON，简单持久化）
# ---------------------------------------------------------------------------
def get_pending(data_dir: str | None = None) -> list:
    with _LOCK:
        path = _paths(data_dir)["pending"]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []


def _save_pending(items: list, data_dir: str | None = None):
    with _LOCK:
        _save_json(_paths(data_dir)["pending"], items)


def add_pending(entry: dict, data_dir: str | None = None) -> dict:
    with _LOCK:
        items = get_pending(data_dir)
        # 同一条消息可能产出多条日程（选课、彩排…），所以去重键必须包含标题，
        # 不能只按“会话 + 消息序号”去重，否则后面的日程会被当成重复丢掉。
        def key(p):
            return (
                str(p.get("chat_username") or ""),
                int(p.get("seq") or 0),
                str((p.get("fields") or {}).get("title") or "").strip(),
            )

        k = key(entry)
        for p in items:
            if key(p) == k:
                return p
        items.append(entry)
        cfg = load_config(data_dir)
        cap = int(cfg.get("max_pending") or 200)
        if len(items) > cap:
            items = items[-cap:]
        _save_pending(items, data_dir)
        return entry


def remove_pending(item_id: str, data_dir: str | None = None) -> bool:
    with _LOCK:
        items = get_pending(data_dir)
        new_items = [p for p in items if str(p.get("id") or "") != item_id]
        if len(new_items) == len(items):
            return False
        _save_pending(new_items, data_dir)
        return True


def has_pending_item(chat: str, seq, method: str | None = None,
                     data_dir: str | None = None) -> bool:
    """待采纳列表里是否已有同一消息（会话+seq）的条目，可按 method 过滤。"""
    with _LOCK:
        items = get_pending(data_dir)
    chat = str(chat or "")
    seq = int(seq or 0)
    for p in items:
        if str(p.get("chat_username") or "") != chat:
            continue
        try:
            if int(p.get("seq") or 0) != seq:
                continue
        except (TypeError, ValueError):
            continue
        if method is None or p.get("method") == method:
            return True
    return False


def remove_pending_for_seq(chat: str, seq, methods: tuple | list | None = None,
                           data_dir: str | None = None) -> int:
    """删除同一消息（会话+seq）下指定 method 的待采纳条目，返回删除数量。"""
    with _LOCK:
        items = get_pending(data_dir)
        chat = str(chat or "")
        seq = int(seq or 0)
        keep = []
        removed = 0
        for p in items:
            same = (
                str(p.get("chat_username") or "") == chat
                and int(p.get("seq") or 0) == seq
            )
            if same and (methods is None or p.get("method") in methods):
                removed += 1
                continue
            keep.append(p)
        if removed:
            _save_pending(keep, data_dir)
        return removed


def _anchor_passed(date_s, time_s) -> bool:
    """判断某个“日期(+时间)”锚点是否已经过去。"""
    if not date_s:
        return False
    try:
        d = datetime.strptime(str(date_s), "%Y-%m-%d").date()
    except ValueError:
        return False
    now = datetime.now()
    t = str(time_s or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if m:
        end = datetime.combine(
            d,
            datetime.strptime("%s:%s" % (m.group(1), m.group(2)), "%H:%M").time(),
        )
        return end < now
    return d < now.date()


def is_pending_expired(fields: dict) -> bool:
    """日程是否已全部过期（过去的不再显示在待采纳里）。

    锚点优先级：事件 date(+end_time/time)、截止 deadline(+deadline_time)。
    只要还有任意一个锚点没过去（例如截止在明天），就继续显示。
    """
    f = fields or {}
    anchors = [
        (f.get("date"), f.get("end_time") or f.get("time")),
        (f.get("deadline"), f.get("deadline_time")),
    ]
    anchors = [(d, t) for d, t in anchors if d]
    if not anchors:
        return False  # 没有时间信息（如待办池），不判断过期
    return all(_anchor_passed(d, t) for d, t in anchors)


def prune_expired_pending(data_dir: str | None = None) -> list:
    """移除所有已过期的待采纳条目，返回剩余列表。"""
    with _LOCK:
        items = get_pending(data_dir)
        visible = [p for p in items if not is_pending_expired(p.get("fields") or {})]
        if len(visible) != len(items):
            _save_pending(visible, data_dir)
        return visible


def make_ai_todo(entry: dict, source: str = "wechat_ai") -> dict:
    """把待采纳条目转成 todos.json 里的完整 todo。"""
    fields = {k: entry.get("fields", {}).get(k) for k in ALLOWED_FIELDS}
    raw = str(entry.get("raw") or "")
    if not fields.get("title"):
        parsed = rule_fields(raw, entry.get("msg_ts"))
        fields.update({k: parsed.get(k) for k in ALLOWED_FIELDS})
    fields = kinds.normalize_item(fields, raw=raw)
    todo = {
        **fields,
        "raw": raw,
        "id": uuid.uuid4().hex[:10],
        "status": "pending",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "wx_chat": str(entry.get("chat_username") or ""),
        "wx_chat_name": str(entry.get("chat_display") or entry.get("chat_username") or ""),
        "wx_sender": str(entry.get("sender") or ""),
        "wx_seq": int(entry.get("seq") or 0),
        "wx_local_id": entry.get("local_id"),
        "wx_ts": entry.get("msg_ts"),
    }
    if fields.get("title") is None:
        todo["title"] = (raw or "未命名事项")[:80]
    return todo


def new_pending_id() -> str:
    return uuid.uuid4().hex[:10]


if __name__ == "__main__":
    # 简单自检
    for t, ok, why in [
        ("军训结束后，信院同学到教一1101开展《陕北公学校歌》集中学唱活动", True, "有“后”和地点"),
        ("这个不错，大家随便聊聊", False, ""),
        ("周六下午3点到体育馆参加篮球比赛", True, ""),
        ("今晚7点社团例会在二教401", True, ""),
    ]:
        passed, reason = prefilter(t, min_len=10)
        print(t[:20], "->", passed, "|", reason)
