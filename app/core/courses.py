# -*- coding: utf-8 -*-
"""课程表：解析教务系统导出的个人课表 Excel，并按学期周模板提供日程事件。

设计要点
--------
- 课表是“周模板”，不是一次性日程：落库存课程名/星期/周次/时间/地点，
  前端按当前周动态展开，单双周与停课都在数据层处理；
- xlsx 本质是 zip+xml，用标准库解析即可，不依赖第三方库；
- 教务导出常见“跨大节的课程在相邻行重复同一内容”，解析时按行去重，
  并把首行时间作为开始、末行时间作为结束，还原真实起止。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta

from app.paths import DATA_DIR

COURSES_FILE = os.path.join(DATA_DIR, "courses.json")

# 2026-2027 学年秋季学期第 1 周周一（用户可从 courses.json 调整）。
DEFAULT_TERM_START = "2026-09-07"

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CN_WEEKDAYS = {
    "星期一": 1, "星期二": 2, "星期三": 3, "星期四": 4, "星期五": 5,
    "星期六": 6, "星期日": 7, "星期天": 7,
}
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-~]\s*(\d{1,2}):(\d{2})")
_WEEK_RANGE_RE = re.compile(
    r"(?P<weeks>[\d,\-，～~至]+)\s*周"
    r"(?:\s*[（(]?\s*(?P<par>单双|单|双)\s*[)）]?)?"
)
_BLOCK_RE = re.compile(
    r"^(?P<loc>.*?)\s*/\s*(?P<secs>\d+(?:\s*[-~]\s*\d+)?)\s*节"
)

_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# xlsx 读取（标准库）
# ---------------------------------------------------------------------------
def _col_index(ref: str) -> int:
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _inline_text(node) -> str:
    return "".join(t.text or "" for t in node.iter(_M + "t"))


def _read_sheets(path: str) -> list:
    """返回 [{name, rows:[{col:int, value:str}, ...]}...]。"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(_M + "si"):
                shared.append(_inline_text(si))

        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rid_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        sheets = [(s.get("name"), s.get(rid_key)) for s in wb.iter(_M + "sheet")]
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.get("Id"): rel.get("Target") for rel in rels}

        out = []
        for name, rid in sheets:
            target = rel_map.get(rid, "")
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            root = ET.fromstring(z.read(target))
            rows = []
            for row in root.iter(_M + "row"):
                cells = {}
                for c in row.findall(_M + "c"):
                    ref = c.get("r")
                    t = c.get("t")
                    v = c.find(_M + "v")
                    val = ""
                    if t == "s" and v is not None and v.text is not None:
                        try:
                            val = shared[int(v.text)]
                        except (ValueError, IndexError):
                            val = v.text
                    elif t == "inlineStr":
                        isel = c.find(_M + "is")
                        if isel is not None:
                            val = _inline_text(isel)
                    elif v is not None:
                        val = v.text or ""
                    if val != "":
                        cells[_col_index(ref)] = str(val)
                if cells:
                    rows.append(cells)
            out.append({"name": name, "rows": rows})
    return out


# ---------------------------------------------------------------------------
# 单元格解析
# ---------------------------------------------------------------------------
def _row_time(row: dict) -> tuple[str, str] | None:
    for col in sorted(row):
        m = _TIME_RE.search(row[col])
        if m:
            return (
                f"{int(m.group(1)):02d}:{int(m.group(2)):02d}",
                f"{int(m.group(3)):02d}:{int(m.group(4)):02d}",
            )
    return None


def _day_map(row: dict) -> dict:
    dm = {}
    for col, val in row.items():
        key = val.strip().replace(" ", "").replace("\u3000", "")
        if key in _CN_WEEKDAYS:
            dm[col] = _CN_WEEKDAYS[key]
    return dm


def _parse_weeks(text: str) -> list:
    """从 “1-16周(单)/1,3,5周/9周” 提取周次列表（按单双周过滤）。"""
    m = _WEEK_RANGE_RE.search(text or "")
    if not m:
        return []
    parity = m.group("par")
    weeks: list[int] = []
    for part in re.split(r"[,，]", m.group("weeks")):
        mm = re.match(r"^\s*(\d+)\s*(?:[-~至]\s*(\d+))?\s*$", part)
        if not mm:
            continue
        a = int(mm.group(1))
        b = int(mm.group(2)) if mm.group(2) else a
        for n in range(a, b + 1):
            if parity == "单" and n % 2 == 0:
                continue
            if parity == "双" and n % 2 == 1:
                continue
            weeks.append(n)
    return sorted(set(weeks))


def _first_teacher(raw: str) -> str:
    seg = re.split(r"[,，;；]", raw or "")[0].strip()
    seg = re.split(r"\s+", seg)[0].strip()
    return seg


def _parse_block(text: str) -> dict | None:
    lines = [ln.strip() for ln in str(text or "").replace("\r", "").split("\n") if ln.strip()]
    if not lines:
        return None
    name = lines[0]
    teacher_raw = lines[1] if len(lines) > 1 else ""
    detail = " ".join(lines[2:]) if len(lines) > 2 else ""
    loc = ""
    secs = None
    weeks = _parse_weeks(detail)
    m = _BLOCK_RE.search(detail)
    if m:
        loc = m.group("loc").strip()
        sec_raw = m.group("secs").replace(" ", "").replace("~", "-")
        parts = sec_raw.split("-")
        try:
            secs = (int(parts[0]), int(parts[-1])) if len(parts) > 1 else (int(parts[0]), int(parts[0]))
        except (ValueError, IndexError):
            secs = None
    elif "/" in detail:
        loc = detail.split("/", 1)[0].strip()
    if not weeks:
        weeks = list(range(1, 17))  # 解析不到周次时按整学期处理
    return {
        "name": name,
        "teacher": _first_teacher(teacher_raw),
        "teacher_raw": teacher_raw,
        "location": loc,
        "secs": secs,
        "weeks": weeks,
    }


def _parse_sheet(sheet: dict) -> tuple[dict, list[dict]]:
    rows = sheet.get("rows", [])
    header_row_idx = None
    day_map = {}
    for i, row in enumerate(rows[:30]):
        dm = _day_map(row)
        if len(dm) >= 5:
            header_row_idx = i
            day_map = dm
            break
    if header_row_idx is None:
        return {}, []
    # 表头向上几行找标题 / 学期 / 班级
    title = ""
    term = ""
    class_name = ""
    for i in range(header_row_idx - 1, -1, -1):
        text = " ".join(str(v) for v in rows[i].values())
        if not title and "课表" in text:
            title = text
        m = re.search(r"学期\s*[:：]\s*(\d{4}-\d{4}-\d)", text)
        if m:
            term = m.group(1)
        m = re.search(r"班级\s*[:：]\s*([^\s]+)", text)
        if m:
            class_name = m.group(1)
        if title and term and class_name:
            break

    # 相邻行的同一课程单元格视为跨大节的同一条课，合并时首行起/末行止
    active = {}   # weekday -> {text, first_row, last_row}
    row_times = []
    row_idx_of = {}
    class_rows = []
    for i in range(header_row_idx + 1, len(rows)):
        row = rows[i]
        tm = _row_time(row)
        has_day_content = any(row.get(col) for col in day_map)
        if not tm and not has_day_content:
            continue
        class_rows.append(row)
        row_idx_of[len(class_rows) - 1] = i
        row_times.append(tm)

    def finalize(wd):
        a = active.pop(wd, None)
        if not a:
            return None
        start_row = a["first_row"]
        end_row = a["last_row"]
        t0 = row_times[start_row]
        t1 = row_times[end_row]
        if not t0 or not t1:
            return None
        parsed = _parse_block(a["text"])
        if not parsed:
            return None
        return {
            **parsed,
            "weekday": wd,
            "time": t0[0],
            "end_time": t1[1],
        }

    courses = []
    for rno, row in enumerate(class_rows):
        for col in sorted(day_map):
            wd = day_map[col]
            text = str(row.get(col, "") or "").strip()
            prev = active.get(wd)
            if prev and prev["text"] == text and text:
                prev["last_row"] = rno
                continue
            done = finalize(wd)
            if done:
                courses.append(done)
            if text:
                active[wd] = {"text": text, "first_row": rno, "last_row": rno}
    for wd in list(active):
        done = finalize(wd)
        if done:
            courses.append(done)
    return {"title": title, "term": term, "class_name": class_name}, courses


def _build_meta(base: dict, path: str, courses: list) -> dict:
    return {
        "term": str(base.get("term") or ""),
        "class_name": str(base.get("class_name") or ""),
        "title": str(base.get("title") or ""),
        "term_start": DEFAULT_TERM_START,
        "weeks_total": max((max(c["weeks"]) for c in courses), default=0),
        "source_file": os.path.basename(path),
        "imported_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# 对外：导入与读取
# ---------------------------------------------------------------------------
def import_xlsx(path: str, term_start: str | None = None) -> dict:
    """解析教务个人课表 xlsx 并写入 courses.json。"""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    sheets = _read_sheets(path)
    picked = None
    for sheet in sheets:
        meta, courses = _parse_sheet(sheet)
        if courses:
            picked = (sheet, meta, courses)
            break
    if picked is None:
        raise ValueError("Excel 中没有识别到“星期 × 大节”课表")
    _sheet, meta, courses = picked
    if not str(term_start or "").strip():
        term_start = (load_courses().get("meta") or {}).get("term_start") or None
    meta = _build_meta(meta, path, courses)
    if term_start:
        try:
            date.fromisoformat(str(term_start))
            meta["term_start"] = str(term_start)
        except ValueError:
            pass
    norm = []
    for c in courses:
        key = f"{c['name']}|{c['weekday']}|{c['location']}|{','.join(map(str, c['weeks']))}"
        norm.append({
            "id": "c" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12],
            "name": c["name"],
            "teacher": c.get("teacher", ""),
            "location": c.get("location", ""),
            "weekday": c["weekday"],
            "weeks": c["weeks"],
            "time": c["time"],
            "end_time": c["end_time"],
            "secs": c.get("secs"),
            "category": "class",
        })
    data = {"meta": meta, "courses": norm}
    with _LOCK:
        _save(data)
    return data


def _save(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = COURSES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, COURSES_FILE)


def load_courses() -> dict:
    try:
        with open(COURSES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("courses"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"meta": {}, "courses": []}


def public_summary() -> dict:
    data = load_courses()
    meta = data.get("meta") or {}
    return {
        "ok": True,
        "meta": meta,
        "course_count": len(data.get("courses") or []),
        "weeks_total": int(meta.get("weeks_total") or 0),
        "term_start": str(meta.get("term_start") or ""),
    }


def term_events() -> list:
    """把课程周模板展开为带具体日期的日程事件。"""
    data = load_courses()
    meta = data.get("meta") or {}
    start_s = str(meta.get("term_start") or "").strip()
    if not start_s:
        return []
    try:
        start = date.fromisoformat(start_s)
    except ValueError:
        return []
    events = []
    for c in data.get("courses") or []:
        try:
            weekday = int(c.get("weekday") or 0)
            if weekday < 1 or weekday > 7:
                continue
        except (ValueError, TypeError):
            continue
        for w in c.get("weeks") or []:
            d = start + timedelta(days=(int(w) - 1) * 7 + weekday - 1)
            events.append({
                "id": f"{c['id']}-w{w}",
                "course_id": c["id"],
                "title": c.get("name") or "未命名课程",
                "teacher": c.get("teacher", ""),
                "location": c.get("location", ""),
                "weekday": weekday,
                "date": d.isoformat(),
                "time": c.get("time"),
                "end_time": c.get("end_time"),
                "kind": "schedule",
                "category": "class",
                "source": "course",
                "week_no": int(w),
                "course": True,
                "status": "pending",
                "created_at": meta.get("imported_at", ""),
            })
    events.sort(key=lambda e: (e["date"], e["time"] or "99:99", e["title"]))
    return events
