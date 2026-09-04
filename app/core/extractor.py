"""自然语言信息提取器：从中文校园事务描述中提取结构化日程信息。

纯规则 + 正则实现，零第三方依赖，适合作为 demo。
支持的表达示例：
- 时间：明天/后天/周X/下周X/8月31日/8.31/下午3点/15:00/3点半
- 时长：2小时 / 90分钟 / 两节课
- 地点：在二教401 / 到行政楼 / 图书馆
- 截止：下周一9点前提交 / 截止5点 / ddl
- 优先级：重要 / 紧急 / 不急
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_PAT = r"(?:[0-9]{1,2}|[一二三四五六七八九十两]{1,3})"


def _cn_num(s: str) -> int | None:
    """中文/阿拉伯数字 -> int，支持 0~99（如 八、十二、二十、三十一、两）。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        tens, _, ones = s.partition("十")
        t = _CN_DIGITS.get(tens, 1) if tens else 1
        o = _CN_DIGITS.get(ones, 0) if ones else 0
        return t * 10 + o
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None

CATEGORY_KEYWORDS = [
    ("exam", ["考试", "测验", "quiz", "期末", "期中", "测试", "考核"]),
    ("homework", ["作业", "论文", "报告", "小组作业", "实验", "项目", "pre", "ppt", "设计"]),
    ("class", ["上课", "讲座", "实验课", "课程", "课"]),
    ("deadline", ["截止", "ddl", "deadline", "缴费", "报名截止", "选课", "交材料", "提交"]),
    ("meeting", ["会议", "例会", "开会", "研讨", "组会", "班会"]),
    ("activity", ["活动", "社团", "演出", "比赛", "报名", "招聘会", "宣讲会", "晚会", "运动会", "志愿"]),
    ("social", ["约会", "聚餐", "聚会", "生日", "吃饭", "看电影", "逛街"]),
    ("health", ["健身", "跑步", "锻炼", "体检", "打针", "吃药", "运动"]),
]

PRIORITY_HIGH = ["重要", "紧急", "加急", "务必", "必须", "尽快", "马上", "关键", "别忘了", "不要忘", "一定要"]
PRIORITY_LOW = ["不急", "有空", "随便", "低优先级", "以后再说", "小事", "不着急"]

DEADLINE_KEYWORDS = ["截止", "ddl", "deadline", "报名截止", "提交"]

CATEGORY_LABEL = {
    "exam": "考试",
    "homework": "作业",
    "class": "上课",
    "deadline": "事务/截止",
    "meeting": "会议",
    "activity": "活动",
    "social": "社交",
    "health": "健康",
    "other": "其他",
}

_PLACE_SUFFIX = (
    "图书馆", "体育馆", "教学楼", "行政楼", "实验室", "食堂", "宿舍", "操场",
    "教室", "报告厅", "会议厅", "咖啡厅", "办公室", "游泳馆", "中心", "校区",
    "公园", "球场", "院", "馆", "楼", "室", "场", "厅", "堂", "店", "站", "教",
)
_PLACE_ALT = "|".join(re.escape(s) for s in _PLACE_SUFFIX)
PLACE_RE = re.compile(
    r"(?:在|到|于|去)\s*([\u4e00-\u9fa5A-Za-z]{1,8}?(?:" + _PLACE_ALT + r")[0-9A-Za-z·\-]{0,8})"
)
PLACE_FALLBACK = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{1,8}?(?:" + _PLACE_ALT + r")[0-9A-Za-z·\-]{0,8})"
)


def _add_months(d: date, n: int) -> date:
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _extract_dates(text: str, today: date) -> list[dict]:
    found = []
    # 1) 2026年8月31日 / 2026-08-31 / 2026/8/31
    pat = re.compile(r"(?<![\d.])(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})(?:[日号])?")
    for m in pat.finditer(text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            found.append({"date": d.isoformat(), "raw": m.group(0), "pos": m.start()})
        except ValueError:
            pass
    # 2) 8月31日 / 八月三十一日 / 8.31 / 8/31
    pat = re.compile(r"(?<![\d./\-年])(" + CN_PAT + r")[月./-](" + CN_PAT + r")(?:[日号])?")
    for m in pat.finditer(text):
        seg = m.group(0)
        if "." in seg and m.group(2).isdigit() and len(m.group(2)) < 2:
            continue  # 避免把 1.5小时 误判成日期
        mon, day = _cn_num(m.group(1)), _cn_num(m.group(2))
        if mon is None or day is None:
            continue
        try:
            d = date(today.year, mon, day)
            found.append({"date": d.isoformat(), "raw": seg, "pos": m.start()})
        except ValueError:
            pass
    # 3) 大后天/后天/明天/明晚/今天/今晚/前天
    for token, delta in [
        ("大后天", 3), ("后天", 2), ("明天", 1), ("明日", 1), ("明晚", 1),
        ("今天", 0), ("今日", 0), ("今晚", 0), ("前天", -1),
    ]:
        for m in re.finditer(token, text):
            found.append({"date": (today + timedelta(days=delta)).isoformat(), "raw": token, "pos": m.start()})
    # 4) 下周一 / 下个周X / 下星期X / 下礼拜X
    pat = re.compile(r"下(?:个)?(?:周|星期|礼拜)([一二三四五六日天])")
    for m in pat.finditer(text):
        w = WEEKDAY_CN[m.group(1)]
        days = (w - today.weekday()) % 7
        days = days if days > 0 else 7
        found.append({"date": (today + timedelta(days=days)).isoformat(), "raw": m.group(0), "pos": m.start()})
    # 5) 本周X / 周X / 星期X / 礼拜X（不含“下周”）
    pat = re.compile(r"(?<!下)(?:本)?(?:周|星期|礼拜)([一二三四五六日天])")
    for m in pat.finditer(text):
        w = WEEKDAY_CN[m.group(1)]
        days = (w - today.weekday()) % 7
        found.append({"date": (today + timedelta(days=days)).isoformat(), "raw": m.group(0), "pos": m.start()})
    # 6) 下个月X号 / 这个月X号
    for prefix, offset in [("下个月", 1), ("这个月", 0)]:
        pat = re.compile(prefix + r"(" + CN_PAT + r")[日号]?")
        for m in pat.finditer(text):
            try:
                day = _cn_num(m.group(1))
                if day is None:
                    continue
                d = _add_months(date(today.year, today.month, 1), offset)
                d = date(d.year, d.month, day)
                found.append({"date": d.isoformat(), "raw": m.group(0), "pos": m.start()})
            except ValueError:
                pass
    # 7) 裸 X号 / X日（本月的），如 31号 / 三十一号
    pat = re.compile(r"(?<![\d./\-月年])(" + CN_PAT + r")[日号]")
    for m in pat.finditer(text):
        try:
            day = _cn_num(m.group(1))
            if day is None:
                continue
            d = date(today.year, today.month, day)
            found.append({"date": d.isoformat(), "raw": m.group(0), "pos": m.start()})
        except ValueError:
            pass
    # 排序，且同一位置重叠时取更长匹配
    out = []
    covered = []
    for item in sorted(found, key=lambda x: (x["pos"], -len(x["raw"]))):
        if any(item["pos"] >= s and item["pos"] + len(item["raw"]) <= e for s, e in covered):
            continue
        covered.append((item["pos"], item["pos"] + len(item["raw"])))
        out.append(item)
    return out


def _extract_times(text: str) -> list[dict]:
    found = []
    occupied = []
    # 0) HH:MM - HH:MM 范围
    pat = re.compile(r"(?<!\d)(\d{1,2})[:：](\d{2})\s*(?:-|—|~|至|到)\s*(\d{1,2})[:：](\d{2})")
    for m in pat.finditer(text):
        h1, mi1, h2, mi2 = (int(x) for x in m.groups())
        if 0 <= h1 <= 23 and 0 <= mi1 <= 59 and 0 <= h2 <= 23 and 0 <= mi2 <= 59:
            found.append({"hh": h1, "mm": mi1, "raw": m.group(0), "pos": m.start(), "pair": (h2, mi2)})
            occupied.append((m.start(), m.end()))
    # 1) HH:MM
    pat = re.compile(r"(?<!\d)(\d{1,2})[:：](\d{2})")
    for m in pat.finditer(text):
        if any(s <= m.start() and m.end() <= e for s, e in occupied):
            continue
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            found.append({"hh": h, "mm": mi, "raw": m.group(0), "pos": m.start()})
            occupied.append((m.start(), m.end()))
    # 2) 中文时间：下午3点 / 八点半 / 十一点四十 / 两点一刻 / 晚上7点
    pat = re.compile(
        r"(凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|夜里|晚间)?\s*(" + CN_PAT + r")[点时]"
        r"(半|一刻|三刻|" + CN_PAT + r"(?:分)?)?"
    )
    for m in pat.finditer(text):
        if any(s <= m.start() and m.end() <= e for s, e in occupied):
            continue
        period, h_raw, min_raw = m.group(1), m.group(2), m.group(3)
        h = _cn_num(h_raw)
        if h is None:
            continue
        mi = 0
        if min_raw:
            if min_raw == "半":
                mi = 30
            elif min_raw == "一刻":
                mi = 15
            elif min_raw == "三刻":
                mi = 45
            else:
                mi = _cn_num(re.sub(r"分$", "", min_raw)) or 0
        if period in ("下午", "傍晚") and h < 12:
            h += 12
        elif period in ("晚上", "夜里", "晚间") and h < 12:
            h += 12
        elif period == "中午" and h < 11:
            h += 12
        elif period in ("早上", "清晨", "凌晨") and h == 12:
            h = 0
        elif not period and 1 <= h <= 6:
            h += 12  # 口语里裸“5点”通常指下午5点
        if 0 <= h <= 23 and 0 <= mi <= 59:
            found.append({"hh": h, "mm": mi, "raw": m.group(0), "pos": m.start()})
            occupied.append((m.start(), m.end()))
    found.sort(key=lambda x: (x["pos"], x["raw"]))
    return found


def _find_duration(text: str) -> tuple[int | None, str | None]:
    """识别时长，返回 (分钟数, 命中的原文片段)，如 两个小时 -> (120, '两个小时')。"""

    def conv(g: str | None) -> int | None:
        if g is None:
            return None
        if "." in g:
            return int(round(float(g)))
        return _cn_num(g)

    checks = [
        (re.compile(r"([一二两三四五六七八九十\d]+)\s*(?:个)?半小时"), lambda m: (conv(m.group(1)) or 0) * 60 + 30),
        (re.compile(r"半小时"), 30),
        (re.compile(r"(\d+(?:\.\d+)?|" + CN_PAT + r")\s*(?:个)?小时"), lambda m: (conv(m.group(1)) or 0) * 60),
        (re.compile(r"(\d+|[一二三四五六七八九十两]{1,3})\s*(?:分钟|min(?:s)?)"), lambda m: conv(m.group(1)) or 0),
        (re.compile(r"(\d+(?:\.\d+)?|" + CN_PAT + r")\s*节课"), lambda m: (conv(m.group(1)) or 0) * 45),
    ]
    for pat, val in checks:
        m = pat.search(text)
        if m:
            minutes = val(m) if callable(val) else val
            if minutes:
                return minutes, m.group(0)
    return None, None


def _find_location(text: str) -> str | None:
    m = PLACE_RE.search(text)
    if m:
        loc = m.group(1).strip().lstrip("在去到于")
        if loc and not re.search(r"[点时午日月周上下前]", loc):
            return loc
    for m in PLACE_FALLBACK.finditer(text):
        loc = m.group(1).strip().lstrip("在去到于")
        if loc and not re.search(r"[点时午日月周上下前]", loc):
            return loc
    return None


def _detect_category(text: str) -> str:
    low = text.lower()
    for cat, kws in CATEGORY_KEYWORDS:
        for kw in kws:
            if kw.lower() in low:
                return cat
    return "other"


def _detect_priority(text: str) -> str:
    low = text.lower()
    if any(kw.lower() in low for kw in PRIORITY_HIGH):
        return "high"
    if any(kw.lower() in low for kw in PRIORITY_LOW):
        return "low"
    return "medium"


def _make_title(text: str, dates: list, times: list, location: str | None, duration_raw: str | None = None) -> str:
    cleaned = text
    for d in dates:
        cleaned = cleaned.replace(d["raw"], " ", 1)
    for t in times:
        cleaned = cleaned.replace(t["raw"], " ", 1)
    if location:
        cleaned = cleaned.replace(location, " ", 1)
    if duration_raw:
        cleaned = cleaned.replace(duration_raw, " ", 1)
    cleaned = re.sub(r"\s*[在去到于]\s*", " ", cleaned)
    cleaned = re.sub(r"\s*前\s*", "", cleaned)
    cleaned = re.sub(
        r"^(记得|别忘了|帮我|请|需要|要|去|准备|打算|提交|今天|明天|后天|上午|下午|晚上)",
        "", cleaned.strip(),
    )
    cleaned = re.sub(r"[，。；、,.!！？?…·\s]+$", "", cleaned.strip())
    cleaned = re.sub(r"[，。；、,，\s]*(重要|紧急|加急|务必|尽快|不着急|不急|有空|随便)?$", "", cleaned.strip())
    cleaned = re.sub(r"(提交|截止|完成|记得)$", "", cleaned.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，。；、,.!！？?")
    if not cleaned:
        # 内容全被识别掉时（如“八点半去图书馆”只剩动词+地点），退回轻量清理
        cleaned = text
        for d in dates:
            cleaned = cleaned.replace(d["raw"], " ", 1)
        for t in times:
            cleaned = cleaned.replace(t["raw"], " ", 1)
        if duration_raw:
            cleaned = cleaned.replace(duration_raw, " ", 1)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，。；、,.!！？?")
    if len(cleaned) > 24:
        cleaned = cleaned[:24] + "…"
    return cleaned


def parse_text(text: str, today: date | None = None, now: datetime | None = None) -> dict:
    """把一段自然语言解析成结构化 todo 字段（JSON 友好）。"""
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "error": "输入内容为空"}
    today = today or date.today()
    now = now or datetime.now()

    dates = _extract_dates(raw, today)
    times = _extract_times(raw)
    duration_min, duration_raw = _find_duration(raw)
    location = _find_location(raw)
    category = _detect_category(raw)
    priority = _detect_priority(raw)

    # 截止关键词（取最靠左的一个）
    dl_pos = None
    for kw in DEADLINE_KEYWORDS:
        m = re.search(re.escape(kw), raw, re.IGNORECASE)
        if m and (dl_pos is None or m.start() < dl_pos):
            dl_pos = m.start()

    # “X点前”也视为截止
    before_time = None
    for t in times:
        after = raw[t["pos"] + len(t["raw"]):].lstrip()
        if after.startswith("前"):
            before_time = t
            break

    event_date = None
    event_time = None
    deadline = None
    deadline_time = None

    if before_time is not None and dl_pos is None:
        deadline = dates[0]["date"] if dates else today.isoformat()
        deadline_time = before_time
    elif dl_pos is not None:
        dates_after = [d for d in dates if d["pos"] >= dl_pos]
        dates_before = [d for d in dates if d["pos"] < dl_pos]
        times_after = [t for t in times if t["pos"] >= dl_pos]
        times_before = [t for t in times if t["pos"] < dl_pos]
        if dates_after or times_after:
            # 截止信息在关键词之后：前后的时间可同时存在（如“下午2点交材料，截止5点”）
            deadline = dates_after[-1]["date"] if dates_after else (dates[-1]["date"] if dates else None)
            deadline_time = times_after[-1] if times_after else (times[-1] if times else None)
            event_date = dates_before[0]["date"] if dates_before else None
            event_time = times_before[0] if times_before else None
        else:
            # 截止信息在关键词之前（如“下周一9点前提交”），整体视为截止
            deadline = dates[-1]["date"] if dates else today.isoformat()
            deadline_time = times[-1] if times else None
    else:
        event_date = dates[0]["date"] if dates else None
        event_time = times[0] if times else None

    # 有时间但没有日期：今天若还没到点，否则顺延到明天
    if event_time and not event_date:
        guess = today
        if event_time["hh"] * 60 + event_time["mm"] <= now.hour * 60 + now.minute:
            guess = today + timedelta(days=1)
        event_date = guess.isoformat()

    # 结束时间：范围 > 时长 > 截止时间兜底
    end_time = None
    if event_time and event_time.get("pair"):
        end_time = f"{event_time['pair'][0]:02d}:{event_time['pair'][1]:02d}"
    if duration_min and not end_time and event_time:
        total = event_time["hh"] * 60 + event_time["mm"] + duration_min
        end_time = f"{(total // 60) % 24:02d}:{total % 60:02d}"
    if not end_time and event_time and deadline_time and event_date and deadline and event_date == deadline:
        et = event_time["hh"] * 60 + event_time["mm"]
        dt = deadline_time["hh"] * 60 + deadline_time["mm"]
        if dt > et:
            end_time = f"{deadline_time['hh']:02d}:{deadline_time['mm']:02d}"

    tokens = [d["raw"] for d in dates] + [t["raw"] for t in times]
    if location:
        tokens.append(location)
    if duration_raw:
        tokens.append(duration_raw)
    for kw in DEADLINE_KEYWORDS:
        if kw.lower() in raw.lower() and kw not in tokens:
            tokens.append(kw)

    deadline_time_str = (
        f"{deadline_time['hh']:02d}:{deadline_time['mm']:02d}" if deadline_time else None
    )
    notes = []
    if category != "other":
        notes.append(f"识别为「{CATEGORY_LABEL[category]}」类事项")
    notes.append("优先级：" + {"high": "高", "medium": "中", "low": "低"}[priority])
    if location:
        notes.append(f"地点：{location}")
    if duration_min:
        notes.append(f"预计时长：{duration_min} 分钟")
    if deadline:
        notes.append(f"截止时间：{deadline}" + (f" {deadline_time_str}" if deadline_time_str else ""))

    return {
        "ok": True,
        "raw": raw,
        "title": _make_title(raw, dates, times, location, duration_raw),
        "category": category,
        "category_label": CATEGORY_LABEL[category],
        "priority": priority,
        "date": event_date,
        "time": f"{event_time['hh']:02d}:{event_time['mm']:02d}" if event_time else None,
        "end_time": end_time,
        "duration_min": duration_min,
        "location": location,
        "deadline": deadline,
        "deadline_time": deadline_time_str,
        "tokens": tokens,
        "notes": notes,
    }
