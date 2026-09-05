# -*- coding: utf-8 -*-
"""app.planner 的拆解 / 风险 / 挡箭牌模块单元测试（纯标准库 unittest）。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import os
import shutil
import uuid
import unittest
from datetime import date, timedelta

from app.planner import decompose, planner, risk, shield, store
from app.paths import DATA_DIR


@contextlib.contextmanager
def _tmp_dir():
    path = os.path.join(DATA_DIR, "unittest_tmp", uuid.uuid4().hex)
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def mk_todo(id_s, title, **kw):
    t = {
        "id": id_s, "title": title, "kind": "todo", "status": "pending",
        "category": "other", "priority": "medium", "date": None, "time": None,
        "end_time": None, "deadline": None, "deadline_time": None,
        "duration_min": None, "raw": title, "created_at": "2026-01-01T08:00:00",
    }
    for k, v in kw.items():
        t[k] = v
    return t


_READY_CFG = {"enabled": True, "api_key": "k", "base_url": "http://x",
              "model": "m", "timeout": 30}


class TestDecompose(unittest.TestCase):
    def test_needs_decomposition_rules(self):
        big = mk_todo("a", "写论文", energy_cost=4)
        self.assertTrue(decompose.needs_decomposition(big))
        long = mk_todo("b", "整理资料", duration_min=150, energy_cost=3)
        self.assertTrue(decompose.needs_decomposition(long))
        small = mk_todo("c", "买牛奶", energy_cost=1, duration_min=30)
        self.assertFalse(decompose.needs_decomposition(small))
        child = mk_todo("d", "写论文", energy_cost=5, parent_id="x")
        self.assertFalse(decompose.needs_decomposition(child))

    def test_parse_subtasks_valid_and_drops(self):
        content = """{"subtasks":[
          {"name":"搭实验环境","deliverable":"可运行的代码环境说明文档",
           "energy_cost":2,"deadline":"2026-07-01"},
          {"name":"跑通基线实验","deliverable":"基线对比表",
           "energy_cost":3,"deadline":"2026-07-05"},
          {"name":"读文献","deliverable":"写200字摘要","energy_cost":1,"deadline":null}
        ]}"""
        subs = decompose.parse_subtasks(content)
        self.assertEqual(len(subs), 2)  # 第三条是过程性描述（写200字）→ 丢弃
        self.assertEqual(subs[0]["name"], "搭实验环境")
        self.assertEqual(subs[0]["deadline"], "2026-07-01")
        self.assertEqual(subs[1]["name"], "跑通基线实验")
        self.assertEqual(subs[1]["deadline"], "2026-07-05")

    def test_parse_subtasks_rejects_bad_json(self):
        self.assertEqual(decompose.parse_subtasks("not json"), [])
        self.assertEqual(decompose.parse_subtasks('{"items": []}'), [])

    def test_ai_decompose_not_ready(self):
        self.assertIsNone(decompose.ai_decompose(
            {"enabled": False}, mk_todo("a", "写论文", energy_cost=5)))

    def test_ai_decompose_with_stub(self):
        calls = []

        def stub(cfg, messages):
            calls.append(messages)
            return '{"subtasks":[{"name":"大纲","deliverable":"一页大纲","energy_cost":1,"deadline":null}]}'

        todo = mk_todo("a", "写毕业论文开题", deadline="2026-08-01",
                       energy_cost=5, duration_min=180)
        subs = decompose.ai_decompose(_READY_CFG, todo, completion=stub)
        self.assertEqual(len(subs), 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("写毕业论文开题", calls[0][1]["content"])

    def test_child_todo_links_parent(self):
        parent = mk_todo("p1", "完成课程论文", deadline="2026-07-10",
                         category="homework", priority="high", energy_cost=5)
        child = decompose.child_todo(parent, {
            "name": "写引言", "deliverable": "引言初稿段落",
            "energy_cost": 1, "deadline": "2026-07-02",
        })
        self.assertEqual(child["parent_id"], "p1")
        self.assertEqual(child["kind"], "todo")
        self.assertEqual(child["deadline"], "2026-07-02")
        self.assertNotEqual(child["id"], "p1")


class TestRisk(unittest.TestCase):
    def test_bucket_of(self):
        self.assertEqual(risk.bucket_of(9), "morning")
        self.assertEqual(risk.bucket_of(15), "afternoon")
        self.assertEqual(risk.bucket_of(21), "evening")
        self.assertEqual(risk.bucket_of(None), "evening")

    def test_stats_aggregation(self):
        events = [
            {"type": "done", "weekday": 3, "hour": 15, "rating": "easy"},
            {"type": "done", "weekday": 3, "hour": 16, "rating": "tough"},
            {"type": "done", "weekday": 3, "hour": 9, "rating": "ok"},
            {"type": "move", "weekday": 3, "hour": 15, "rating": "easy"},  # 忽略
        ]
        stats = risk.load_history_stats(events)
        self.assertAlmostEqual(stats[(3, "afternoon")], 0.75)  # (1.0+0.5)/2
        self.assertAlmostEqual(stats[(3, "morning")], 0.8)

    def test_base_rate_fallback_and_override(self):
        self.assertAlmostEqual(risk.base_rate(0, 9, {}), risk.DEFAULT_RATE["morning"])
        stats = {(3, "afternoon"): 0.4}
        self.assertAlmostEqual(risk.base_rate(3, 14, stats), 0.4)

    def test_simulate_deterministic_and_risk(self):
        day = date(2026, 6, 4)  # 周四
        todos = [
            mk_todo("t1", "冲刺答辩 PPT", deadline=day.isoformat(),
                    duration_min=120, energy_cost=4),
        ]
        plan = planner.plan_day(todos, day=day, include_courses=False)
        # 自定义历史：周四下午完成率极低 → 应标记为风险
        stats = {(3, risk.bucket_of(10)): 0.25}
        # 调整所在时段为上午，但给一个覆盖性统计：一律用低历史
        stats[(3, "morning")] = 0.25
        r1 = risk.simulate(plan, stats=stats, n=200, seed=7)
        r2 = risk.simulate(plan, stats=stats, n=200, seed=7)
        self.assertEqual([x["probability"] for x in r1],
                         [x["probability"] for x in r2])
        self.assertTrue(all(0.0 <= x["probability"] <= 1.0 for x in r1))
        self.assertTrue(all(x["risk"] for x in r1))

    def test_risk_copy_mentions_split_and_weekday(self):
        e = {"title": "写实验报告", "start": "14:00", "deliverable": "报告全文",
             "date": "2026-06-04"}
        copy = risk.risk_copy(e, weekday=3, rate=0.4)
        self.assertIn("周四下午", copy)
        self.assertIn("子任务", copy)
        self.assertIn("40%", copy)

    def test_record_done(self):
        with _tmp_dir() as tmp:
            todo = mk_todo("t1", "交作业", kind="schedule", date="2026-06-04",
                           time="09:30")
            risk.record_done(todo, rating="tough", hour=9, data_dir=tmp)
            events = store.load_events(tmp)
            self.assertEqual(len(events), 1)
            ev = events[0]
            self.assertEqual(ev["type"], "done")
            self.assertEqual(ev["weekday"], 3)
            self.assertEqual(ev["rating"], "tough")


class TestShield(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 6, 4)

    def test_light_load_when_empty(self):
        m = shield.load_metrics([], day=self.day,
                                plan=planner.plan_day([], day=self.day,
                                                      include_courses=False))
        self.assertEqual(m["level"], "low")
        self.assertEqual(m["undone_todos"], 0)

    def test_high_load_with_many_undone(self):
        todos = [
            mk_todo("t%d" % i, "任务%d" % i)
            for i in range(8)
        ]
        plan = planner.plan_day(todos, day=self.day, include_courses=False)
        m = shield.load_metrics(todos, day=self.day, plan=plan)
        self.assertEqual(m["level"], "high")
        self.assertGreaterEqual(m["undone_todos"], 6)

    def test_intrusion_light_verdict_and_copy(self):
        res = shield.evaluate_intrusion([], "明天晚上7点参加社团团建聚餐",
                                        day=self.day)
        self.assertTrue(res["ok"])
        self.assertEqual(res["verdict"], "light")
        self.assertIn("宽松", res["copy"])

    def test_intrusion_heavy_copy(self):
        todos = [mk_todo("t%d" % i, "任务%d" % i) for i in range(8)]
        res = shield.evaluate_intrusion(todos, "明天下午去郊外团建一整天",
                                        day=self.day)
        self.assertEqual(res["verdict"], "heavy")
        self.assertIn("负载比较满", res["copy"])
        self.assertIn("待定", res["copy"])

    def test_reply_copy_shapes(self):
        self.assertIn("宽松", shield.reply_copy("light", {"undone_todos": 0}))
        self.assertIn("待定", shield.reply_copy("heavy",
                                                {"undone_todos": 8, "planned_energy": 15.0}))


if __name__ == "__main__":
    unittest.main()
