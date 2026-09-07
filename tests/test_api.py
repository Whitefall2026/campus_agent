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
from app.ai import gateway as ai_gateway
from app.web.handlers import Handler

TOUCHED = ["todos.json", "planner_profile.json", "planner_events.json",
           "ai_config.json", "user_profile.json", "user_evidence.json",
           "user_memory.json", "user_state_history.json", "chat_thread.json",
           "ai_pending.json", "ai_seen.json"]


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
        # 隔离：测试期间禁用 AI（避免真的调用用户配置的付费接口/泄露配置）
        with open(os.path.join(DATA_DIR, "ai_config.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"enabled": False}, f)

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

    def _reset_plan(self):
        """清空待办并重置今日排程缓存，保证每个用例从确定状态开始。"""
        self.req("POST", "/api/clear", {"scope": "all"})
        self.req("POST", "/api/plan/ai", {"date": date.today().isoformat()})

    def test_accept_official_push_directly_to_todo(self):
        self._reset_plan()
        item_id = "official01"
        ai_gateway.add_pending({
            "id": item_id,
            "chat_username": "gh_jobs",
            "chat_display": "人大就业",
            "sender": "人大就业",
            "seq": 9001,
            "raw": "秋招报名通知\n请于9月10日前提交报名表",
            "fields": {
                "title": "秋招报名通知",
                "kind": "todo",
                "category": "deadline",
                "priority": "medium",
                "deadline": "%04d-09-10" % date.today().year,
            },
            "method": "official-rule",
            "source": "wechat_official",
            "official_url": "https://example.com/apply",
            "status": "pending",
        })
        status, res = self.req(
            "POST", "/api/ai/pending/%s/accept" % item_id, {"kind": "todo"}
        )
        self.assertEqual(status, 200)
        todo = next(t for t in res["state"]["todos"] if t.get("wx_seq") == 9001)
        self.assertEqual(todo["kind"], "todo")
        self.assertEqual(todo["source"], "wechat_official")
        self.assertEqual(todo["title"], "秋招报名通知")

    def test_first_visit_reports_planable_candidates(self):
        # 只有未来截止的未排期待办时，首访计划页也应看到候选并触发自动规划
        self._reset_plan()
        today = date.today().isoformat()
        deadline = (date.today() + timedelta(days=2)).isoformat()
        s, res = self.req("POST", "/api/items", {
            "title": "数学作业", "kind": "todo", "category": "homework",
            "priority": "medium", "deadline": deadline, "duration_min": 60,
        })
        self.assertEqual(s, 200)
        s, res = self.req("GET", "/api/plan?date=" + today)
        self.assertEqual(s, 200)
        meta = res["plan"]["meta"]
        self.assertTrue(meta.get("view_only"))
        self.assertGreaterEqual(meta.get("candidates", 0), 1)
        self.assertEqual(len(res["plan"]["entries"] or []), 0)

    def test_future_day_can_be_planned(self):
        # 打开未来某天也应能生成排程（不再只规划真实今天）
        self._reset_plan()
        future = (date.today() + timedelta(days=3)).isoformat()
        s, res = self.req("POST", "/api/items", {
            "title": "数学作业", "kind": "todo", "category": "homework",
            "priority": "medium", "deadline": future, "duration_min": 60,
        })
        self.assertEqual(s, 200)
        tid = res["todo"]["id"]

        s, plan = self.req("POST", "/api/plan/ai", {"date": future})
        self.assertEqual(s, 200)
        self.assertEqual(plan["plan"]["date"], future)
        entries0 = [(e["task_id"], e["start"], e["end"])
                    for e in plan["plan"]["entries"]]
        self.assertIn(tid, [x[0] for x in entries0])

        s, back = self.req("GET", "/api/plan?date=" + future)
        self.assertEqual(s, 200)
        entries1 = [(e["task_id"], e["start"], e["end"])
                    for e in back["plan"]["entries"]]
        self.assertEqual(entries0, entries1)

    def test_browse_without_plan_shows_zero_usage(self):
        # 空方案的日子能量占用必须为 0（原来会偷偷算一版排程导致占用）
        self._reset_plan()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        s, res = self.req("POST", "/api/items", {
            "title": "买牛奶", "kind": "todo", "category": "other",
            "priority": "low", "duration_min": 30,
        })
        self.assertEqual(s, 200)
        s, res = self.req("GET", "/api/plan?date=" + tomorrow)
        self.assertEqual(s, 200)
        plan = res["plan"]
        self.assertTrue((plan["meta"] or {}).get("view_only"))
        self.assertEqual(len(plan["entries"] or []), 0)
        budget = plan["budget"] or {}
        self.assertEqual(budget.get("planned_points"), 0)
        self.assertEqual(budget.get("planned_ratio"), 0)

    def test_plan_survives_day_navigation(self):
        # 生成排程后翻到另一天再翻回：方案应原样保留
        self._reset_plan()
        today = date.today().isoformat()
        s, res = self.req("POST", "/api/items", {
            "title": "整理课程笔记", "kind": "todo", "category": "homework",
            "priority": "medium", "duration_min": 60,
        })
        self.assertEqual(s, 200)
        s, res = self.req("POST", "/api/plan/ai", {"date": today})
        self.assertEqual(s, 200)
        entries0 = [(e["task_id"], e["start"], e["end"])
                    for e in res["plan"]["entries"]]
        self.assertGreaterEqual(len(entries0), 1)

        other = (date.today() + timedelta(days=1)).isoformat()
        s, other_res = self.req("GET", "/api/plan?date=" + other)
        self.assertEqual(s, 200)
        self.assertEqual(len(other_res["plan"]["entries"] or []), 0)

        s, back = self.req("GET", "/api/plan?date=" + today)
        self.assertEqual(s, 200)
        entries1 = [(e["task_id"], e["start"], e["end"])
                    for e in back["plan"]["entries"]]
        self.assertEqual(entries0, entries1)

    def test_skip_binds_today_and_keeps_task_planable_later(self):
        # 跳过只作用于当天；重新规划后当天不再出现，任务进入“明日优先”，
        # 同时不再被 plan_offered_date 永久卡住，之后的日子恢复候选。
        self._reset_plan()
        today = date.today().isoformat()
        s, res_a = self.req("POST", "/api/items", {
            "title": "任务甲", "kind": "todo", "category": "homework",
            "priority": "medium", "duration_min": 60,
        })
        s, res_b = self.req("POST", "/api/items", {
            "title": "任务乙", "kind": "todo", "category": "homework",
            "priority": "medium", "duration_min": 60,
        })
        self.assertEqual((s, s), (200, 200))
        aid = res_a["todo"]["id"]
        bid = res_b["todo"]["id"]

        s, plan = self.req("POST", "/api/plan/ai", {"date": today})
        self.assertEqual(s, 200)
        placed = {e["task_id"] for e in plan["plan"]["entries"]}
        self.assertIn(aid, placed)
        self.assertIn(bid, placed)

        s, skipped = self.req("POST", "/api/plan/skip", {
            "date": today, "task_id": aid,
        })
        self.assertEqual(s, 200)

        s, replan = self.req("POST", "/api/plan/ai", {"date": today})
        self.assertEqual(s, 200)
        ids = {e["task_id"] for e in replan["plan"]["entries"]}
        self.assertNotIn(aid, ids)
        self.assertIn(bid, ids)
        tomorrow = replan["plan"]["tomorrow"] or []
        self.assertTrue(any(
            x.get("task_id") == aid and x.get("skipped") for x in tomorrow))

        s, state = self.req("GET", "/api/state")
        self.assertEqual(s, 200)
        todo = next(
            (t for t in (state.get("todo_items") or []) if t.get("id") == aid),
            None)
        self.assertIsNotNone(todo)
        self.assertEqual(todo.get("plan_skipped_dates"), [today])
        self.assertIn(todo.get("plan_offered_date"), (None, ""))

    def test_suggest_returns_cross_day_items(self):
        # 今日计划页的排程建议应覆盖未来日期，每条都带目标日期与时段
        self._reset_plan()
        today = date.today().isoformat()
        deadline = (date.today() + timedelta(days=3)).isoformat()
        s, res = self.req("POST", "/api/items", {
            "title": "数学作业", "kind": "todo", "category": "homework",
            "priority": "medium", "deadline": deadline, "duration_min": 60,
        })
        self.assertEqual(s, 200)
        tid = res["todo"]["id"]
        s, res = self.req("POST", "/api/plan/suggest", {})
        self.assertEqual(s, 200)
        items = res.get("items") or []
        it = next((x for x in items if x.get("id") == tid), None)
        self.assertIsNotNone(it)
        self.assertTrue(it.get("date") and it.get("date") >= today)
        self.assertTrue(it.get("time"))
        self.assertTrue(it.get("end_time"))
        self.assertIn("energy", res)
        self.assertIn("slot_points", it)
        # AI 不可用时走规则兜底：仍应补全概率/风险字段
        self.assertTrue(0.0 <= it.get("probability", -1) <= 1.0)
        self.assertIn("risk", it)
        self.assertIn("risk_copy", it)

    def test_far_deadline_not_suggested_by_rule_fallback(self):
        # AI 不可用时规则兜底不应把截止还很远的任务提前摆到计划页
        self._reset_plan()
        deadline = (date.today() + timedelta(days=10)).isoformat()
        s, res = self.req("POST", "/api/items", {
            "title": "结课论文", "kind": "todo", "category": "homework",
            "priority": "medium", "deadline": deadline, "duration_min": 60,
        })
        self.assertEqual(s, 200)
        tid = res["todo"]["id"]
        s, res = self.req("POST", "/api/plan/suggest", {})
        self.assertEqual(s, 200)
        self.assertEqual(res.get("method"), "rule")
        self.assertNotIn(
            tid, [x.get("id") for x in (res.get("items") or [])])
        self.assertEqual(res.get("energy", {}).get("used_points"), 0)

    def test_suggest_skip_today_then_adopt_another(self):
        # 跳过某条建议后，今日不再提示该任务；采纳其它建议会写到它的目标日期
        self._reset_plan()
        today = date.today().isoformat()
        dl_a = (date.today() + timedelta(days=1)).isoformat()
        dl_b = (date.today() + timedelta(days=2)).isoformat()
        s, res_a = self.req("POST", "/api/items", {
            "title": "任务甲", "kind": "todo", "category": "homework",
            "priority": "high", "deadline": dl_a, "duration_min": 60,
        })
        s, res_b = self.req("POST", "/api/items", {
            "title": "任务乙", "kind": "todo", "category": "homework",
            "priority": "high", "deadline": dl_b, "duration_min": 60,
        })
        self.assertEqual((s, s), (200, 200))
        aid = res_a["todo"]["id"]
        bid = res_b["todo"]["id"]

        s, sug = self.req("POST", "/api/plan/suggest", {})
        self.assertEqual(s, 200)
        it_a = next((x for x in sug["items"] if x.get("id") == aid), None)
        it_b = next((x for x in sug["items"] if x.get("id") == bid), None)
        self.assertIsNotNone(it_a)
        self.assertIsNotNone(it_b)

        s, skipped = self.req("POST", "/api/plan/skip", {
            "date": today, "task_id": aid,
        })
        self.assertEqual(s, 200)
        s, sug2 = self.req("POST", "/api/plan/suggest", {})
        self.assertEqual(s, 200)
        ids2 = {x.get("id") for x in sug2["items"]}
        self.assertNotIn(aid, ids2)
        self.assertIn(bid, ids2)

        s, patched = self.req("PATCH", "/api/todos/" + bid, {
            "kind": "schedule",
            "date": it_b["date"],
            "time": it_b["time"],
            "end_time": it_b["end_time"],
        })
        self.assertEqual(s, 200)
        s, state = self.req("GET", "/api/state")
        self.assertEqual(s, 200)
        todo = next(
            (t for t in (state.get("todos") or []) if t.get("id") == bid), None)
        self.assertIsNotNone(todo)
        self.assertEqual(todo.get("kind"), "schedule")
        self.assertEqual(todo.get("date"), it_b["date"])
        self.assertEqual(todo.get("time"), it_b["time"])

    def test_replan_include_skipped_today(self):
        # 跳过只抑制自动规划；点「重新规划」（include_skipped=true）时
        # 今天跳过的任务可以重新参与规划
        self._reset_plan()
        today = date.today().isoformat()
        dl = (date.today() + timedelta(days=1)).isoformat()
        s, res = self.req("POST", "/api/items", {
            "title": "临时加的任务", "kind": "todo", "category": "homework",
            "priority": "high", "deadline": dl, "duration_min": 60,
        })
        self.assertEqual(s, 200)
        tid = res["todo"]["id"]

        s, sug = self.req("POST", "/api/plan/suggest", {})
        self.assertIn(tid, [x.get("id") for x in sug["items"]])

        s, skipped = self.req("POST", "/api/plan/skip", {
            "date": today, "task_id": tid,
        })
        self.assertEqual(s, 200)
        s, sug2 = self.req("POST", "/api/plan/suggest", {})
        self.assertNotIn(tid, [x.get("id") for x in sug2["items"]])

        s, sug3 = self.req("POST", "/api/plan/suggest",
                           {"include_skipped": True})
        self.assertIn(tid, [x.get("id") for x in sug3["items"]])

    def test_chat_reset_keeps_evidence(self):
        # 清空会话只清对话消息；对话证据必须保留，供画像分析
        self._reset_plan()
        s, res = self.req("POST", "/api/chat", {
            "text": "你好，我习惯把作业安排在下午做。",
        })
        self.assertEqual(s, 200)
        s, prof = self.req("GET", "/api/ai/profile")
        self.assertEqual(s, 200)
        before = int(prof.get("evidence_count") or 0)
        self.assertGreater(before, 0)

        s, reset = self.req("POST", "/api/chat/reset", {})
        self.assertEqual(s, 200)
        self.assertEqual(reset.get("messages"), [])

        s, prof2 = self.req("GET", "/api/ai/profile")
        self.assertEqual(s, 200)
        self.assertEqual(int(prof2.get("evidence_count") or 0), before)

    def test_adopt_uses_displayed_future_deadline_slot(self):
        # 未来截止的任务也会被提前安排；采纳时应写入页面展示的那个时段，
        # 而不是重新计算时因引擎口径不同而落空。
        self._reset_plan()
        today = date.today().isoformat()
        deadline = (date.today() + timedelta(days=3)).isoformat()
        s, res = self.req("POST", "/api/items", {
            "title": "大创中期报告", "kind": "todo", "category": "homework",
            "priority": "high", "deadline": deadline, "energy_cost": 3,
            "duration_min": 90,
        })
        self.assertEqual(s, 200)
        tid = res["todo"]["id"]

        s, plan = self.req("POST", "/api/plan/ai", {"date": today})
        self.assertEqual(s, 200)
        entry = next(
            (e for e in plan["plan"]["entries"] if e["task_id"] == tid), None)
        self.assertIsNotNone(entry)

        s, applied = self.req("POST", "/api/plan/apply", {
            "date": today, "placements": [tid],
        })
        self.assertEqual(s, 200)
        self.assertEqual(applied.get("placed"), 1)

        s, state = self.req("GET", "/api/state")
        self.assertEqual(s, 200)
        todo = next(
            (t for t in (state.get("todos") or []) if t.get("id") == tid),
            None)
        self.assertIsNotNone(todo)
        self.assertEqual(todo.get("date"), today)
        self.assertEqual(todo.get("time"), entry["start"])
        self.assertEqual(todo.get("end_time"), entry["end"])

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

        # 加一条无截止任务作为今天的候选，保证今天有排程
        s, res = self.req("POST", "/api/items", {
            "title": "整理课程笔记", "kind": "todo", "category": "homework",
            "priority": "medium", "duration_min": 60,
        })
        self.assertEqual(s, 200)

        today = date.today().isoformat()
        s, res = self.req("POST", "/api/plan/ai", {"date": today})
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
