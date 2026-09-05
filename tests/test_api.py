# -*- coding: utf-8 -*-
"""端到端 API 集成测试：真实起 HTTP 服务（随机端口）打全链路。

- 新增任务（含规划字段）→ 能量规划+风险 → 采纳排程/顺延 → 挡箭牌 →
  完成反馈（EMA）→ 拖拽记录 → 状态回归；
- 测试前后备份/恢复 data/ 下被触碰的文件，绝不污染本地数据；
- 运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

from app.paths import DATA_DIR
from app.web.handlers import Handler

TOUCHED = ["todos.json", "planner_profile.json", "planner_events.json"]


class TestApiIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # 备份本地数据
        cls._bak = os.path.join(DATA_DIR, "unittest_tmp", "api_bak")
        os.makedirs(cls._bak, exist_ok=True)
        for f in TOUCHED:
            p = os.path.join(DATA_DIR, f)
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(cls._bak, f))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        # 恢复本地数据
        for f in TOUCHED:
            src = os.path.join(cls._bak, f)
            dst = os.path.join(DATA_DIR, f)
            if os.path.exists(src):
                shutil.copy2(src, dst)
            elif os.path.exists(dst):
                os.remove(dst)
        shutil.rmtree(os.path.join(DATA_DIR, "unittest_tmp"), ignore_errors=True)

    def req(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        r = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    def test_full_flow(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        s, res = self.req("GET", "/api/energy")
        self.assertEqual(s, 200)
        self.assertEqual(len(res["energy"]["hours"]), 24)

        s, res = self.req("POST", "/api/items", {
            "title": "冲刺答辩 PPT", "kind": "todo", "category": "homework",
            "priority": "high", "deadline": tomorrow, "energy_cost": 4,
            "deliverable": "答辩用 15 页 PPT",
        })
        self.assertEqual(s, 200)
        big_id = res["todo"]["id"]
        self.assertEqual(res["todo"]["energy_cost"], 4)

        s, res = self.req("POST", "/api/items", {
            "title": "推文初稿", "kind": "todo", "category": "activity",
            "priority": "low", "deadline": tomorrow, "duration_min": 60,
        })
        self.assertEqual(s, 200)
        soft_id = res["todo"]["id"]
        self.assertEqual(res["todo"]["deadline_type"], "soft")
        self.assertEqual(res["todo"]["ddl_float_days"], 2)

        s, res = self.req("GET", "/api/plan?date=" + tomorrow)
        self.assertEqual(s, 200)
        plan = res["plan"]
        self.assertGreaterEqual(len(plan["entries"]), 1)
        for e in plan["entries"]:
            self.assertIn("probability", e)
            self.assertIn("risk", e)

        s, res = self.req("POST", "/api/shield",
                          {"text": "这周末去郊外团建一整天"})
        self.assertEqual(s, 200)
        self.assertIn(res["verdict"], ("light", "moderate", "heavy"))
        self.assertTrue(res["copy"])

        # 不启用 AI 时拆解应优雅降级
        s, res = self.req("POST", "/api/decompose", {"id": big_id})
        self.assertEqual(s, 200)
        self.assertFalse(res["decomposed"])
        self.assertTrue(res["reason"])

        s, res = self.req("PATCH", "/api/todos/" + big_id, {"status": "done"})
        self.assertEqual(s, 200)

        s, res = self.req("POST", "/api/feedback",
                          {"id": big_id, "rating": "tough"})
        self.assertEqual(s, 200)
        self.assertEqual(len(res["energy"]["hours"]), 24)

        s, res = self.req("POST", "/api/plan/move",
                          {"id": soft_id, "date": tomorrow,
                           "time": "20:00", "end_time": "21:00"})
        self.assertEqual(s, 200)
        self.assertEqual(res["record"]["type"], "move")

        s, res = self.req("GET", "/api/state")
        self.assertEqual(s, 200)
        self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main()
