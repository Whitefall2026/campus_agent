# -*- coding: utf-8 -*-
"""标准库合成 XLSX 与真实 HTTP 导入测试；所有服务数据均在独立临时目录。

运行：python -B -m unittest discover -s tests -p test_excel_todos.py -v
"""
from __future__ import annotations

import atexit
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch

# 必须先于任何 app.* 导入；保留整套测试已经设置的隔离目录。
if "RUC_AGENT_DATA_DIR" not in os.environ:
    _IMPORT_DATA = tempfile.TemporaryDirectory(prefix="ruc-excel-import-tests-")
    atexit.register(_IMPORT_DATA.cleanup)
    os.environ.setdefault("RUC_AGENT_DATA_DIR", _IMPORT_DATA.name)

from app.core import excel_todos, xlsx

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
HEADERS = ["Task", "Deadline", "Priority", "Duration", "Location", "Deliverable", "Status"]


def make_xlsx(sheets, *, shared=(), epoch1904=False):
    """sheets=[(名称, 行列表, 可选隐藏状态)]；dict 单元格可指定 t/v/s/f。"""
    workbook = ET.Element("workbook", xmlns=MAIN_NS, attrib={"xmlns:r": REL_NS})
    ET.SubElement(workbook, "workbookPr", date1904="1" if epoch1904 else "0")
    sheet_list = ET.SubElement(workbook, "sheets")
    relationships = ET.Element("Relationships", xmlns=PACKAGE_NS)
    files = {}
    for index, spec in enumerate(sheets, 1):
        name, rows, *state = spec
        attrs = {"name": name, "sheetId": str(index), "r:id": f"rId{index}"}
        if state:
            attrs["state"] = state[0]
        ET.SubElement(sheet_list, "sheet", attrs)
        target = f"worksheets/sheet{index}.xml"
        ET.SubElement(relationships, "Relationship", Id=f"rId{index}",
                      Type=REL_NS + "/worksheet",
                      Target="/xl/" + target if index % 2 == 0 else target)
        root = ET.Element("worksheet", xmlns=MAIN_NS)
        data = ET.SubElement(root, "sheetData")
        for row_number, values in enumerate(rows, 1):
            row = ET.SubElement(data, "row", r=str(row_number))
            for col, value in enumerate(values):
                if value is None:
                    continue
                letters, number = "", col + 1
                while number:
                    number, digit = divmod(number - 1, 26)
                    letters = chr(65 + digit) + letters
                spec = value if isinstance(value, dict) else {"v": value}
                kind = spec.get("t", "inlineStr" if isinstance(spec.get("v"), str) else None)
                attrs = {"r": spec.get("ref", f"{letters}{row_number}")}
                if kind is not None:
                    attrs["t"] = kind
                if "s" in spec:
                    attrs["s"] = str(spec["s"])
                cell = ET.SubElement(row, "c", attrs)
                if "f" in spec:
                    ET.SubElement(cell, "f").text = spec["f"]
                if kind == "inlineStr":
                    inline = ET.SubElement(cell, "is")
                    for fragment in spec.get("rich", [str(spec.get("v", ""))]):
                        run = ET.SubElement(inline, "r")
                        ET.SubElement(run, "t").text = fragment
                elif "v" in spec:
                    ET.SubElement(cell, "v").text = str(spec["v"])
        files["xl/" + target] = ET.tostring(root, encoding="utf-8")
    files["xl/workbook.xml"] = ET.tostring(workbook, encoding="utf-8")
    files["xl/_rels/workbook.xml.rels"] = ET.tostring(relationships, encoding="utf-8")
    strings = ET.Element("sst", xmlns=MAIN_NS)
    for value in shared:
        entry = ET.SubElement(strings, "si")
        for fragment in value if isinstance(value, tuple) else (value,):
            run = ET.SubElement(entry, "r")
            ET.SubElement(run, "t").text = fragment
    files["xl/sharedStrings.xml"] = ET.tostring(strings, encoding="utf-8")
    styles = ET.Element("styleSheet", xmlns=MAIN_NS)
    formats = ET.SubElement(styles, "numFmts", count="1")
    ET.SubElement(formats, "numFmt", numFmtId="164", formatCode="yyyy-mm-dd hh:mm")
    xfs = ET.SubElement(styles, "cellXfs", count="4")
    for fmt in (0, 14, 164, 20):
        ET.SubElement(xfs, "xf", numFmtId=str(fmt))
    files["xl/styles.xml"] = ET.tostring(styles, encoding="utf-8")
    return make_zip(files)


def make_zip(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return stream.getvalue()


def tasks_xlsx(rows):
    return make_xlsx([("Tasks", [HEADERS, *rows])])


class TestExcelTodos(unittest.TestCase):
    def test_multiple_sheets_shared_inline_and_source_rows(self):
        raw = make_xlsx([
            ("First", [["说明"], [], HEADERS,
                       [{"t": "s", "v": 0}, "2030-10-09 09:30", "高", "1.5小时", "图书馆", "报告"]]),
            ("Second", [HEADERS, [{"v": "", "rich": ["整理", "课程笔记"]}]]),
            ("Hidden", [HEADERS, ["隐藏任务"]], "hidden"),
            ("VeryHidden", [HEADERS, ["另一隐藏任务"]], "veryHidden"),
        ], shared=[("提交", "实验报告")])
        result = excel_todos.extract_todos(raw)
        self.assertEqual([t["title"] for t in result["items"]], ["提交实验报告", "整理课程笔记"])
        first = result["items"][0]
        self.assertEqual(first["source_row"], "First 第 4 行")
        self.assertEqual((first["deadline"], first["deadline_time"]), ("2030-10-09", "09:30"))
        self.assertEqual((first["priority"], first["duration_min"]), ("high", 90))
        self.assertEqual((first["location"], first["deliverable"]), ("图书馆", "报告"))
        self.assertTrue(all(t["kind"] == "todo" and t["date"] is None and t["time"] is None
                            for t in result["items"]))
        self.assertEqual(result["warnings"], [])

    def test_1900_and_1904_dates_and_time_formats(self):
        cases = [(False, 1, 1, "1900-01-01", None),
                 (False, 59, 1, "1900-02-28", None),
                 (False, 61, 1, "1900-03-01", None),
                 (False, 46272.5, 2, "2026-09-07", "12:00"),
                 (True, 0, 1, "1904-01-01", None),
                 (True, 1.5, 2, "1904-01-02", "12:00")]
        for epoch, serial, style, deadline, deadline_time in cases:
            with self.subTest(epoch1904=epoch, serial=serial):
                raw = make_xlsx([("Dates", [HEADERS,
                    ["提交报告", {"v": serial, "s": style}, None, {"v": 0.0625, "s": 3}]])],
                    epoch1904=epoch)
                item = excel_todos.extract_todos(raw)["items"][0]
                self.assertEqual((item["deadline"], item["deadline_time"]), (deadline, deadline_time))
                self.assertEqual(item["duration_min"], 90)

    def test_formula_cache_missing_cache_and_error_cells(self):
        raw = tasks_xlsx([
            [{"t": "str", "f": '"提交报告"', "v": "提交报告"},
             {"f": "DATE(2026,9,7)", "v": 46272, "s": 1}],
            ["缺缓存任务", {"f": "TODAY()", "s": 1}],
            [{"t": "str", "f": '"缺缓存标题"'}],
            ["错误日期任务", {"t": "e", "v": "#VALUE!"}],
            ["正常任务"],
        ])
        result = excel_todos.extract_todos(raw)
        self.assertEqual([t["title"] for t in result["items"]], ["提交报告", "正常任务"])
        self.assertEqual(result["items"][0]["deadline"], "2026-09-07")
        self.assertEqual((result["skipped"], result["warning_count"]), (3, 3))
        self.assertTrue(all("公式无缓存值或单元格报错" in w for w in result["warnings"]))

    def test_numeric_formula_cache_without_style_remains_numeric(self):
        raw = tasks_xlsx([["整理资料", None, None, {"f": "15*3", "v": 45}]])
        sheet = xlsx.read_sheets(io.BytesIO(raw))[0]
        self.assertEqual(sheet["rows"][1][3], "45")
        self.assertEqual(sheet["uncached"][1], [])
        result = excel_todos.extract_todos(raw)
        self.assertEqual(result["items"][0]["duration_min"], 45)
        self.assertEqual(result["warnings"], [])

    def test_negative_style_index_rejected(self):
        raw = tasks_xlsx([["提交报告", {"v": 46272, "s": -1}]])
        with self.assertRaisesRegex(ValueError, "样式索引"):
            xlsx.read_sheets(io.BytesIO(raw))

    def test_completed_cancelled_duplicates_and_repeated_headers(self):
        statuses = ("done", "completed", "已完成", "完成", "已取消", "cancelled")
        rows = [["忽略" + status, None, None, None, None, None, status] for status in statuses]
        rows.extend([["提交报告", "2030-10-09"], ["提交报告", "2030-10-09"], HEADERS])
        raw = make_xlsx([("First", [HEADERS, *rows]),
                         ("Second", [HEADERS, ["提交报告", "2030-10-09"],
                                     ["提交报告", "2030-10-10"]])])
        result = excel_todos.extract_todos(raw)
        self.assertEqual([t["deadline"] for t in result["items"]], ["2030-10-09", "2030-10-10"])
        self.assertEqual(result["skipped"], len(statuses) + 3)
        self.assertEqual(result["warnings"], [])

    def test_bad_dates_warn_and_keep_valid_rows(self):
        invalid = ("2030-02-30", "2030-13-01", "2030-10-09 25:00", "not a date")
        result = excel_todos.extract_todos(tasks_xlsx(
            [["坏日期任务" + str(i), value] for i, value in enumerate(invalid)] +
            [["正常任务", "2030/10/09 09:30"]]))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["deadline"], "2030-10-09")
        self.assertEqual(result["skipped"], len(invalid))
        self.assertEqual(result["warning_count"], len(invalid))
        self.assertTrue(all("该行未导入" in w for w in result["warnings"]))

    def test_no_header_uses_natural_language_and_warns(self):
        result = excel_todos.extract_todos(make_xlsx([
            ("Notes", [["整理课程笔记"], [], ["提交数学作业", "2030年10月9日前"]])]))
        self.assertEqual(len(result["items"]), 2)
        self.assertIn("没有识别到任务表头", result["warnings"][0])
        self.assertEqual(result["items"][1]["source_row"], "Notes 第 3 行")
        self.assertEqual(result["items"][1]["deadline"], "2030-10-09")

    def test_duplicate_headers_rejected(self):
        with self.assertRaisesRegex(ValueError, "重复字段"):
            excel_todos.extract_todos(make_xlsx([("Tasks", [["Task", "Title"], ["报告", "报告"]])]))

    def test_invalid_numeric_dates_rejected(self):
        for value in ("nan", "inf", "-inf", "1e308"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                xlsx.read_sheets(io.BytesIO(tasks_xlsx([["提交报告", {"t": "n", "v": value, "s": 1}]])))

    def test_corrupt_zip_missing_parts_and_bad_xml(self):
        cases = [b"not a zip", tasks_xlsx([["任务"]])[:30], make_zip({}),
                 make_zip({"xl/workbook.xml": b"<broken"})]
        for raw in cases:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                excel_todos.extract_todos(raw)

    def test_entity_declaration_rejected(self):
        raw = make_zip({"xl/workbook.xml": b'<!DOCTYPE workbook [<!ENTITY test "unsafe">]><workbook/>'})
        with self.assertRaisesRegex(ValueError, "实体声明"):
            xlsx.read_sheets(io.BytesIO(raw))

    def test_archive_size_limit_and_duplicate_members(self):
        raw = tasks_xlsx([["整理资料"]])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            total = sum(info.file_size for info in archive.infolist())
        with patch.object(xlsx, "MAX_XML_BYTES", total):
            self.assertEqual(len(excel_todos.extract_todos(raw)["items"]), 1)
        with patch.object(xlsx, "MAX_XML_BYTES", total - 1), self.assertRaisesRegex(ValueError, "过大"):
            xlsx.read_sheets(io.BytesIO(raw))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("same.xml", "first")
            with self.assertWarns(UserWarning):
                archive.writestr("same.xml", "second")
        with self.assertRaisesRegex(ValueError, "结构异常"):
            xlsx.read_sheets(io.BytesIO(stream.getvalue()))

    def test_archive_member_limit(self):
        raw = make_zip({f"part{i}.xml": b"" for i in range(5001)})
        with self.assertRaisesRegex(ValueError, "结构异常"):
            xlsx.read_sheets(io.BytesIO(raw))

    def test_row_and_cell_limits_across_sheets(self):
        raw = make_xlsx([("First", [["任务"]]), ("Second", [["另一任务"]])])
        with patch.object(xlsx, "MAX_ROWS", 2), patch.object(xlsx, "MAX_CELLS", 2):
            self.assertEqual(len(xlsx.read_sheets(io.BytesIO(raw))), 2)
        for field in ("MAX_ROWS", "MAX_CELLS"):
            with self.subTest(limit=field), patch.object(xlsx, field, 1), self.assertRaisesRegex(ValueError, "限制"):
                xlsx.read_sheets(io.BytesIO(raw))

    def test_empty_rows_still_obey_row_limit(self):
        raw = make_xlsx([("EmptyRows", [["任务"], [], []])])
        with patch.object(xlsx, "MAX_ROWS", 2), self.assertRaisesRegex(ValueError, "限制"):
            xlsx.read_sheets(io.BytesIO(raw))

    def test_cell_text_and_column_limits(self):
        raw = make_xlsx([("Text", [["A" * 10000]])])
        self.assertEqual(len(xlsx.read_sheets(io.BytesIO(raw))[0]["rows"][0][0]), 10000)
        cases = (["A" * 10001], [{"v": "任务", "ref": "XFE1"}], [{"v": "任务", "ref": "bad"}])
        for row in cases:
            with self.subTest(cell=row[0]), self.assertRaises(ValueError):
                xlsx.read_sheets(io.BytesIO(make_xlsx([("BadCell", [row])])))

    def test_item_limit_and_warning_limit(self):
        with patch.object(excel_todos, "MAX_ITEMS", 2):
            self.assertEqual(len(excel_todos.extract_todos(tasks_xlsx([["Task A"], ["Task B"]]))["items"]), 2)
            with self.assertRaisesRegex(ValueError, "500"):
                excel_todos.extract_todos(tasks_xlsx([["Task A"], ["Task B"], ["Task C"]]))
        result = excel_todos.extract_todos(tasks_xlsx([[f"Task {i}", "bad date"] for i in range(105)]))
        self.assertEqual((len(result["warnings"]), result["warning_count"], result["skipped"]), (100, 105, 105))


HTTP_SERVER = """
import json, sys
from pathlib import Path
from app.web.server import LocalHTTPServer
from app.web.handlers import Handler
server = LocalHTTPServer(('127.0.0.1', 0), Handler)
Path(sys.argv[1]).write_text(json.dumps({'port': server.server_port}), encoding='utf-8')
server.serve_forever()
"""


class TestExcelTodoImportHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ruc-excel-todos-tests-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.data_dir = Path(cls.temp.name) / "data"
        cls.data_dir.mkdir()
        (cls.data_dir / "ai_config.json").write_text('{"enabled": false}', encoding="utf-8")
        ready = Path(cls.temp.name) / "ready.json"
        log_path = Path(cls.temp.name) / "server.log"
        log = log_path.open("w", encoding="utf-8")
        cls.addClassCleanup(log.close)
        env = dict(os.environ, RUC_AGENT_DATA_DIR=str(cls.data_dir), PYTHONIOENCODING="utf-8")
        cls.process = subprocess.Popen(
            [sys.executable, "-B", "-u", "-c", HTTP_SERVER, str(ready)],
            cwd=Path(__file__).resolve().parents[1], env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 15
        while not ready.exists():
            if cls.process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("隔离 HTTP 服务启动失败：" + log_path.read_text(encoding="utf-8"))
            time.sleep(0.05)
        cls.base = "http://127.0.0.1:" + str(json.loads(ready.read_text(encoding="utf-8"))["port"])
        # 禁用环境代理：所有请求只访问测试子进程的本机随机端口。
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def stop_server(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    def setUp(self):
        status, result = self.req("POST", "/api/clear", {"scope": "all"})
        self.assertEqual(status, 200, result)

    def req(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def body(self, rows, **extra):
        return {"name": "tasks.xlsx", "data": base64.b64encode(tasks_xlsx(rows)).decode("ascii"), **extra}

    def persisted(self):
        return json.loads((self.data_dir / "todos.json").read_text(encoding="utf-8"))

    def test_preview_does_not_save_tasks_or_upload(self):
        before = (self.data_dir / "todos.json").read_bytes()
        files_before = set(self.data_dir.iterdir())
        status, result = self.req("POST", "/api/todos/import", self.body([["Task A"], ["Task B"]]))
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual([item["title"] for item in result["items"]], ["Task A", "Task B"])
        self.assertEqual((self.data_dir / "todos.json").read_bytes(), before)
        self.assertEqual(set(self.data_dir.iterdir()), files_before)

    def test_selected_index_imports_only_that_task(self):
        status, result = self.req("POST", "/api/todos/import",
                                  self.body([["Task A"], ["Task B", "2030-10-09 09:30"]], selected=[1]))
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        saved = self.persisted()
        self.assertEqual([item["title"] for item in saved], ["Task B"])
        self.assertEqual((saved[0]["deadline"], saved[0]["deadline_time"]), ("2030-10-09", "09:30"))
        self.assertEqual((saved[0]["kind"], saved[0]["status"]), ("todo", "pending"))
        self.assertTrue(saved[0]["id"])

    def test_empty_selected_imports_nothing(self):
        before = (self.data_dir / "todos.json").read_bytes()
        status, result = self.req("POST", "/api/todos/import", self.body([["Task A"]], selected=[]))
        self.assertEqual(status, 200, result)
        self.assertEqual((self.data_dir / "todos.json").read_bytes(), before)

    def test_existing_tasks_are_deduplicated_including_scheduled_tasks(self):
        status, result = self.req("POST", "/api/items", {
            "title": "Task A", "kind": "schedule", "date": "2030-10-08", "time": "10:00",
            "deadline": "2030-10-09", "deadline_time": "09:30"})
        self.assertEqual(status, 200, result)
        original_id = result["todo"]["id"]
        body = self.body([["Task A", "2030-10-09 09:30"], ["Task B"]], selected=[0, 1])
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                status, result = self.req("POST", "/api/todos/import", body)
                self.assertEqual(status, 200, result)
                saved = self.persisted()
                self.assertEqual([item["title"] for item in saved], ["Task A", "Task B"])
                self.assertEqual(saved[0]["id"], original_id)

    def test_invalid_base64_and_invalid_file_return_400(self):
        polluted = self.body([["Task A"]])
        polluted["data"] += "%!"
        bodies = [{"name": "tasks.xlsx", "data": "%%%invalid%%%"},
                  {"name": "tasks.xlsx", "data": base64.b64encode(b"not a ZIP").decode("ascii")},
                  {"name": "tasks.xlsx", "data": ""},
                  self.body([["Task A"]], name="tasks.xls"), polluted]
        before = (self.data_dir / "todos.json").read_bytes()
        for body in bodies:
            with self.subTest(name=body["name"], data=body["data"][:20]):
                status, result = self.req("POST", "/api/todos/import", body)
                self.assertEqual(status, 400, result)
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
                self.assertEqual((self.data_dir / "todos.json").read_bytes(), before)

    def test_invalid_selection_is_rejected_without_partial_import(self):
        for selected in ([0, 99], [-1], [True], ["0"], "all"):
            with self.subTest(selected=selected):
                status, result = self.req("POST", "/api/todos/import",
                                          self.body([["Task A"]], selected=selected))
                self.assertEqual(status, 400, result)
                self.assertEqual(self.persisted(), [])


if __name__ == "__main__":
    unittest.main()
