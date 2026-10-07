"""有界的 XLSX 读取器：仅解析工作表值，不执行公式、宏或外部链接。"""
from __future__ import annotations

import math
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_ROWS = 10000
MAX_CELLS = 100000


def _col_index(ref: str) -> int:
    match = re.fullmatch(r"([A-Z]+)\d+", ref or "")
    if not match:
        raise ValueError("Excel 单元格坐标不正确")
    result = 0
    for char in match[1]:
        result = result * 26 + ord(char) - 64
    if result > 16384:
        raise ValueError("Excel 列数超过限制")
    return result - 1


def _text(node) -> str:
    return "".join(t.text or "" for t in node.iter(M + "t"))


def _date_value(value: str, fmt: str, epoch1904: bool) -> str:
    code = re.sub(r'"[^"\n]*"|\\.|\[[^\]]*\]', "", fmt.lower())
    is_date = bool(re.search(r"[yd]", code))
    is_time = bool(re.search(r"[hs]", code))
    if not is_date and not is_time:
        return value
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Excel 日期值不正确")
    if epoch1904:
        epoch = datetime(1904, 1, 1)
    else:
        epoch = datetime(1899, 12, 30)
        if 0 < number < 60:
            number += 1  # Excel 的虚构 1900-02-29 之前需补一天。
    parsed = epoch + timedelta(days=number)
    if is_date:
        return parsed.isoformat(sep=" ", timespec="minutes") if is_time else parsed.date().isoformat()
    return parsed.strftime("%H:%M")


def read_sheets(source) -> list[dict]:
    """path 或二进制文件对象 → 可见工作表及稀疏行、原始行号、公式缺缓存位置。"""
    try:
        return _read_sheets(source)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError, OverflowError, RuntimeError) as exc:
        raise ValueError("Excel 文件损坏、加密或格式不支持，请用 Excel 另存为 .xlsx 后重试") from exc


def _read_sheets(source) -> list[dict]:
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if (len(infos) > 5000 or len({i.filename for i in infos}) != len(infos)
                or sum(i.file_size for i in infos) > MAX_XML_BYTES):
            raise ValueError("Excel 解压后过大或结构异常（上限 64MB）")

        def xml(name):
            data = archive.read(name)
            declarations = data.replace(b"\x00", b"").upper()
            if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
                raise ValueError("Excel XML 包含不支持的实体声明")
            return ET.fromstring(data)

        names = set(archive.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = [_text(n) for n in xml("xl/sharedStrings.xml").findall(M + "si")]
        formats = {14: "yyyy-mm-dd", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
                   18: "h:mm", 19: "h:mm:ss", 20: "h:mm", 21: "h:mm:ss",
                   22: "yyyy-mm-dd h:mm", 45: "mm:ss", 46: "[h]:mm:ss", 47: "mm:ss"}
        formats.update({n: "yyyy-mm-dd" for n in list(range(27, 37)) + list(range(50, 59))})
        styles = []
        if "xl/styles.xml" in names:
            root = xml("xl/styles.xml")
            for fmt in root.iter(M + "numFmt"):
                formats[int(fmt.get("numFmtId"))] = fmt.get("formatCode") or ""
            xfs = root.find(M + "cellXfs")
            if xfs is not None:
                styles = [formats.get(int(xf.get("numFmtId") or 0), "") for xf in xfs]
        wb = xml("xl/workbook.xml")
        prop = wb.find(M + "workbookPr")
        epoch1904 = prop is not None and prop.get("date1904") in ("1", "true")
        rels = {r.get("Id"): r for r in xml("xl/_rels/workbook.xml.rels")}
        out, row_count, cell_count = [], 0, 0
        for sheet in wb.iter(M + "sheet"):
            if sheet.get("state") in ("hidden", "veryHidden"):
                continue
            rel = rels[sheet.get(RID)]
            if rel.get("TargetMode") == "External":
                raise ValueError("Excel 不支持外部工作表")
            target = rel.get("Target") or ""
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            if not target.startswith("xl/"):
                raise ValueError("Excel 工作表路径不正确")
            rows, row_numbers, uncached = [], [], []
            for row in xml(target).iter(M + "row"):
                row_count += 1
                if row_count > MAX_ROWS:
                    raise ValueError("Excel 超过 10000 行或 100000 单元格限制")
                cells, missing = {}, []
                for cell in row.findall(M + "c"):
                    cell_count += 1
                    if cell_count > MAX_CELLS:
                        raise ValueError("Excel 超过 10000 行或 100000 单元格限制")
                    col = _col_index(cell.get("r"))
                    val = cell.find(M + "v")
                    text = val.text or "" if val is not None else ""
                    kind = cell.get("t")
                    if cell.find(M + "f") is not None and not text:
                        missing.append(col)
                    if kind == "s":
                        index = int(text)
                        if index < 0:
                            raise ValueError("Excel 共享字符串索引不正确")
                        text = shared[index]
                    elif kind == "inlineStr":
                        text = _text(cell)
                    elif kind == "e":
                        missing.append(col)
                        text = ""
                    elif kind not in ("str", "b", "d") and text and styles:
                        style = int(cell.get("s") or 0)
                        if style < 0:
                            raise ValueError("Excel 样式索引不正确")
                        text = _date_value(text, styles[style], epoch1904)
                    if len(text) > 10000:
                        raise ValueError("Excel 单元格内容超过 10000 字符限制")
                    if text.strip():
                        cells[col] = text.strip()
                if cells or missing:
                    rows.append(cells)
                    row_numbers.append(int(row.get("r") or row_count))
                    uncached.append(missing)
            out.append({"name": sheet.get("name") or "工作表", "rows": rows,
                        "row_numbers": row_numbers, "uncached": uncached})
        return out
