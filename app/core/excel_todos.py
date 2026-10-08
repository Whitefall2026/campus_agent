"""从 Excel 任务表提取待办：本地解析、预览、选取，原文件不落盘。"""
from __future__ import annotations

import io
import re
import unicodedata
from bisect import bisect_left, bisect_right
from datetime import datetime

from app.core import kinds, xlsx
from app.core.extractor import parse_text
from app.planner import fields

MAX_ITEMS = 500
ALIASES = {
    "title": ("任务", "任务名称", "任务名", "事项", "事项名称", "待办", "待办事项", "工作内容", "项目名称", "标题", "名称", "task", "title", "name"),
    "deadline": ("截止时间", "截止日期", "截止", "到期时间", "到期日期", "完成期限", "计划完成时间", "deadline", "duedate", "due"),
    "plan_date": ("日期", "计划日期", "计划完成日", "date", "planneddate"),
    "priority": ("优先级", "重要程度", "priority"),
    "duration_min": ("预计时长", "预计时长分钟", "时长", "时长分钟", "预计用时", "计划时长", "计划用时", "duration", "durationmin", "minutes"),
    "location": ("地点", "location"),
    "deliverable": ("交付物", "产出", "deliverable"),
    "status": ("状态", "完成状态", "status"),
    "checked": ("勾选", "是否完成", "checkbox", "checked"),
}


def _header(value: str) -> str:
    return re.sub(r"[\s_()/\-:]", "", unicodedata.normalize("NFKC", value).lower())


def _mapping(rows: list) -> tuple[dict, int]:
    for i, row in enumerate(rows[:20]):
        found = {}
        for col, text in row.items():
            header = _header(text)
            duration_header = re.sub(r"(?:分钟|小时|minutes?|hours?|mins?|hrs?|h)$", "", header)
            for field, names in ALIASES.items():
                if header in names or (field == "duration_min" and duration_header in names):
                    found[col] = field
                    break
        if "title" in found.values():
            if len(found.values()) != len(set(found.values())):
                raise ValueError("任务表包含重复字段，请保留一列任务名称及一列截止时间")
            return found, i + 1
    return {}, 0


def _merged_dates(sheet: dict, mapping: dict) -> tuple[dict, set]:
    """只继承日期列中明确纵向合并的值，普通空白日期不自动向下填充。"""
    date_cols = {c for c, field in mapping.items() if field in ("deadline", "plan_date")}
    by_number = dict(zip(sheet["row_numbers"], sheet["rows"]))
    missing = dict(zip(sheet["row_numbers"], sheet["uncached"]))
    numbers = sorted(by_number)
    values, invalid, assignments = {}, set(), 0
    for col, first, last in sheet["vertical_merges"]:
        if col not in date_cols:
            continue
        rows = numbers[bisect_left(numbers, first):bisect_right(numbers, last)]
        assignments += len(rows)
        if assignments > xlsx.MAX_CELLS:
            raise ValueError("Excel 合并日期范围超过限制")
        anchor = by_number.get(first, {}).get(col, "")
        for number in rows:
            values[number, col] = anchor
            if col in missing.get(first, []):
                invalid.add((number, col))
    return values, invalid


def task_key(item: dict) -> tuple:
    """相同名称与截止视为同一待办，已规划的同一事项也能识别。"""
    return (str(item.get("title") or "").strip().casefold(),
            str(item.get("deadline") or ""), str(item.get("deadline_time") or ""))


def _deadline(text: str) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    value = re.sub(r"[年/月.]", "-", text.strip()).replace("日", " ").strip()
    value = value.replace("T", " ")
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::\d{2})?)?", value)
    if match:
        y, m, d = map(int, match.group(1, 2, 3))
        hour, minute = int(match[4] or 0), int(match[5] or 0)
        try:
            dt = datetime(y, m, d, hour, minute)
        except ValueError as exc:
            raise ValueError("截止日期或时刻不合法，请检查年月日与小时分钟") from exc
        return dt.date().isoformat(), dt.strftime("%H:%M") if match[4] else None
    # 相对截止（如“下周五”）沿用产品现有自然语言规则。
    if re.search(r"今天|明天|后天|周|星期|礼拜|月|号", text):
        parsed = parse_text("提交任务，截止 " + text)
        day = parsed.get("deadline") or parsed.get("date")
        if day:
            return day, parsed.get("deadline_time") or parsed.get("time")
    raise ValueError("截止时间无法识别，请填写 YYYY-MM-DD 或 Excel 日期")


def _duration(text: str, unit: str = "minutes") -> int | None:
    if not text:
        return None
    text = unicodedata.normalize("NFKC", text).strip().lower()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if match:
        if int(match[2]) >= 60:
            raise ValueError("预计时长的分钟部分须小于 60")
        value = int(match[1]) * 60 + int(match[2])
    else:
        parts = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*(?:小时|hours?|hrs?|h))?\s*(?:(\d+(?:\.\d+)?)\s*(?:分钟|minutes?|mins?|min))?", text)
        if parts and any(parts.groups()):
            value = float(parts[1] or 0) * 60 + float(parts[2] or 0)
        else:
            value = float(text) * (60 if unit == "hours" else 1)
    if not 0 <= value <= 1440:
        raise ValueError("预计时长须为 0–1440 分钟")
    return 0 if value == 0 else max(1, round(value))


def extract_todos(raw: bytes) -> dict:
    result, warnings, seen, completed = [], [], set(), set()
    skipped = 0
    sheets = xlsx.read_sheets(io.BytesIO(raw))
    tables = [(sheet, *_mapping(sheet["rows"])) for sheet in sheets]
    has_tables = any(mapping for _, mapping, _ in tables)
    for sheet, mapping, start in tables:
        rows = sheet["rows"]
        if has_tables and not mapping:
            warnings.append(f"{sheet['name']}：未识别到任务表头，已跳过辅助工作表")
            continue
        merged_dates, invalid_dates = _merged_dates(sheet, mapping)
        unit = "minutes"
        for col, field in mapping.items():
            if field == "duration_min" and re.search(r"(?:小时|hours?|hrs?|h)$", _header(rows[start - 1][col])):
                unit = "hours"
        if "plan_date" in mapping.values():
            warnings.append(f"{sheet['name']}：没有明确截止值的任务使用日期列作为计划完成日，请核对；建议时段不作为截止时刻")
        if not mapping:
            warnings.append(f"{sheet['name']}：没有识别到任务表头，按每行一条自然语言待办提取，请核对预览")
        for i in range(start, len(rows)):
            row = rows[i]
            label = f"{sheet['name']} 第 {sheet['row_numbers'][i]} 行"
            values = {field: row.get(col, merged_dates.get((sheet["row_numbers"][i], col), ""))
                      for col, field in mapping.items()}
            is_completed = (values.get("status", "").strip().lower() in
                            ("done", "completed", "已完成", "完成", "已取消", "cancelled")
                            or values.get("checked", "").strip().lower() in ("☑", "✓", "✔", "true", "1", "是", "已完成"))
            try:
                if ((sheet["uncached"][i] and (not mapping or any(col in mapping for col in sheet["uncached"][i])))
                        or any((sheet["row_numbers"][i], col) in invalid_dates for col in mapping)):
                    raise ValueError("公式无缓存值或单元格报错，请在 Excel 中重算并保存")
                text = values.get("title", "") if mapping else "，".join(row[col] for col in sorted(row))
                if not text.strip() or not re.search(r"[A-Za-z\u4e00-\u9fff]", text):
                    skipped += 1
                    continue
                # 表头可能在每页重复，不将它作为待办。
                if mapping and _header(text) in ALIASES["title"]:
                    skipped += 1
                    continue
                parsed = parse_text(text)
                if not parsed.get("ok"):
                    raise ValueError("未能提取任务名称")
                item = kinds.normalize_item(parsed, kind="todo", raw=text)
                item["title"] = text[:120] if mapping else str(item.get("title") or text)[:120]
                if values.get("deadline"):
                    item["deadline"], item["deadline_time"] = _deadline(values["deadline"])
                elif values.get("plan_date"):
                    item["deadline"], _ = _deadline(values["plan_date"])
                    item["deadline_time"] = None
                if is_completed:
                    completed.add(task_key(item))
                    skipped += 1
                    continue
                duration = _duration(values["duration_min"], unit) if values.get("duration_min") else None
                if duration is not None:
                    item["duration_min"] = max(1, duration)
                if values.get("priority"):
                    priorities = {"高": "high", "高优先级": "high", "紧急": "high", "high": "high",
                                  "中": "medium", "中优先级": "medium", "普通": "medium", "medium": "medium",
                                  "低": "low", "低优先级": "low", "low": "low",
                                  "p0": "high", "p1": "medium", "p2": "low"}
                    priority = priorities.get(unicodedata.normalize("NFKC", values["priority"]).strip().lower())
                    if priority is None:
                        raise ValueError("优先级无法识别，请填写高/中/低、P0/P1/P2 或 high/medium/low")
                    item["priority"] = priority
                for field in ("location", "deliverable"):
                    if values.get(field):
                        item[field] = values[field][:120]
                item = fields.normalize_task(item)
                key = task_key(item)
                if key in seen or key in completed:
                    skipped += 1
                    continue
                if len(result) >= MAX_ITEMS:
                    raise ValueError("最多一次提取 500 条待办，请拆分表格后重试")
                seen.add(key)
                if duration == 0:
                    warnings.append(f"{label}：计划时长为 0，当前任务模型按最小 1 分钟导入，请核对")
                result.append({**{k: item.get(k) for k in (
                    "title", "category", "priority", "kind", "date", "time", "end_time",
                    "deadline", "deadline_time", "duration_min", "energy_cost", "location",
                    "deliverable", "deadline_type", "ddl_float_days")},
                    "source_row": label})
            except (ValueError, OverflowError) as exc:
                if len(result) >= MAX_ITEMS:
                    raise ValueError("最多一次提取 500 条待办，请拆分表格后重试") from exc
                warnings.append(f"{label}：{exc}，该行未导入")
                skipped += 1
    pending = [item for item in result if task_key(item) not in completed]
    skipped += len(result) - len(pending)
    return {"items": pending, "warnings": warnings[:100],
            "warning_count": len(warnings), "skipped": skipped}
