# -*- coding: utf-8 -*-
"""app.planner 单元测试（纯标准库 unittest）。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import os
import shutil
import uuid
import unittest
from datetime import date, timedelta

from app.planner import energy as energy_mod
from app.planner import fields, planner, store
from app.paths import DATA_DIR


@contextlib.contextmanager
def _tmp_dir():
    """沙箱下系统 Temp 与 tempfile.mkdtemp 目录都不可写，
    统一在 data/（gitignore）下用 os.makedirs 建目录；清理尽力而为。"""
    path = os.path.join(DATA_DIR, "unittest_tmp", uuid.uuid4().hex)
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def mk_todo(id_s, title, **kw):
    t = {
        "id": id_s,
        "title": title,
        "kind": "todo",
        "status": "pending",
        "category": "other",
        "priority": "medium",
        "date": None,
        "time": None,
        "end_time": None,
        "deadline": None,
        "deadline_time": None,
        "duration_min": None,
        "raw": title,
        "created_at": "2026-01-01T08:00:00",
    }
    for k, v in kw.items():
        t[k] = v
    return t


def mk_schedule(id_s, title, day, start, end=None):
    return mk_todo(id_s, title, kind="schedule", date=day.isoformat(),
                   time=start, end_time=end)


class TestFields(unittest.TestCase):
    def test_energy_cost_from_duration(self):
        self.assertEqual(fields.normalize_task(mk_todo("a", "x", duration_min=60))["energy_cost"], 2)
        self.assertEqual(fields.normalize_task(mk_todo("b", "x", duration_min=45))["energy_cost"], 2)
        self.assertEqual(fields.normalize_task(mk_todo("c", "x", duration_min=150))["energy_cost"], 5)

    def test_energy_cost_default_by_priority(self):
        low = fields.normalize_task(mk_todo("a", "x", category="meeting", priority="low"))
        self.assertEqual(low["energy_cost"], 1)
        high = fields.normalize_task(mk_todo("b", "x", category="meeting", priority="high"))
        self.assertEqual(high["energy_cost"], 3)
        exam = fields.normalize_task(mk_todo("c", "x", category="exam"))
        self.assertEqual(exam["energy_cost"], 4)

    def test_duration_defaulted_from_cost(self):
        n = fields.normalize_task(mk_todo("a", "x", energy_cost=3))
        self.assertEqual(n["duration_min"], 90)

    def test_deadline_type_infer(self):
        hard = fields.normalize_task(mk_todo("a", "期末考试复习", deadline="2026-06-01"))
        self.assertEqual(hard["deadline_type"], "hard")
        hard2 = fields.normalize_task(mk_todo("b", "签证申请材料截止", deadline="2026-06-01"))
        self.assertEqual(hard2["deadline_type"], "hard")
        soft = fields.normalize_task(mk_todo("c", "社团推文初稿", deadline="2026-06-01"))
        self.assertEqual(soft["deadline_type"], "soft")
        soft2 = fields.normalize_task(mk_todo("d", "内部周报", deadline="2026-06-01"))
        self.assertEqual(soft2["deadline_type"], "soft")

    def test_float_days(self):
        soft = fields.normalize_task(mk_todo("a", "周报", deadline="2026-06-01"))
        self.assertEqual(soft["ddl_float_days"], 2)
        hard = fields.normalize_task(mk_todo("b", "期末考试", deadline="2026-06-01"))
        self.assertEqual(hard["ddl_float_days"], 0)

    def test_normalize_keeps_original_intact(self):
        t = mk_todo("a", "期末考试")
        before = dict(t)
        fields.normalize_task(t)
        self.assertEqual(t, before)


class TestEnergy(unittest.TestCase):
    def test_default_curve(self):
        prof = energy_mod.default_profile()
        self.assertEqual(len(prof["hours"]), 24)
        self.assertAlmostEqual(energy_mod.coefficient_at(9, prof), 1.2)
        self.assertAlmostEqual(energy_mod.coefficient_at(21, prof), 0.4)
        self.assertAlmostEqual(energy_mod.coefficient_at(3, prof), 0.0)

    def test_block_points(self):
        prof = energy_mod.default_profile()
        # 08:00 起 60 分钟：两个半小时格都落在系数 1.2 的小时里
        self.assertAlmostEqual(energy_mod.block_points(8 * 60, 60, prof), 2.4)
        # 07:00 起 30 分钟：系数 0.5 → 0.5 点
        self.assertAlmostEqual(energy_mod.block_points(7 * 60, 30, prof), 0.5)

    def test_ema_update_clamp(self):
        prof = energy_mod.default_profile()
        prof2 = energy_mod.record_feedback(9.5, "tough", prof)
        self.assertLess(prof2["hours"][9], prof["hours"][9])
        self.assertGreaterEqual(prof2["hours"][9], energy_mod.COEF_MIN)
        prof3 = energy_mod.record_feedback(9.5, "easy", prof2)
        self.assertGreater(prof3["hours"][9], prof2["hours"][9])
        self.assertLessEqual(prof3["hours"][9], energy_mod.COEF_MAX)
        # 未知评级不改变曲线
        prof4 = energy_mod.record_feedback(9.5, "???", prof)
        self.assertEqual(prof4["hours"], prof["hours"])

    def test_available_total_positive(self):
        self.assertGreater(energy_mod.available_total(), 10)


class TestStore(unittest.TestCase):
    def test_event_roundtrip(self):
        with _tmp_dir() as tmp:
            store.clear_events(tmp)
            store.append_event({"type": "defer", "task_id": "a"}, tmp)
            store.append_event({"type": "move", "task_id": "b"}, tmp)
            events = store.load_events(tmp)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["type"], "defer")
            self.assertIn("created_at", events[0])

    def test_profile_roundtrip(self):
        with _tmp_dir() as tmp:
            prof = energy_mod.default_profile()
            prof["hours"][9] = 1.4
            store.save_profile(prof, tmp)
            loaded = energy_mod.load_profile(tmp)
            self.assertAlmostEqual(loaded["hours"][9], 1.4)


class TestPlanner(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 6, 4)  # 周四

    def test_free_runs_excludes_busy(self):
        todos = [
            mk_schedule("c1", "上课", self.day, "08:00", "10:00"),
            mk_schedule("c2", "例会", self.day, "14:00", "15:00"),
        ]
        runs = planner.free_runs(todos, self.day, include_courses=False)
        for r in runs:
            start, end = r["start"], r["end"]
            # 与 08:00-10:00 不重叠
            self.assertFalse(start < 10 * 60 and end > 8 * 60)
            # 与 14:00-15:00 不重叠
            self.assertFalse(start < 15 * 60 and end > 14 * 60)
        # 且确实保留了空闲（长度 > 0）
        self.assertTrue(runs)
        self.assertGreater(sum(r["end"] - r["start"] for r in runs), 10 * 60)

    def test_places_earliest_free(self):
        # 上午 10 点前有课，10:00 之后空闲 → 任务应排到 10:00
        todos = [
            mk_schedule("c1", "早课", self.day, "08:00", "10:00"),
            mk_todo("t1", "交实验报告", deadline=self.day.isoformat(),
                    category="homework", priority="high", duration_min=60),
        ]
        plan = planner.plan_day(todos, day=self.day, include_courses=False)
        self.assertEqual(plan["meta"]["planned"], 1)
        entry = plan["entries"][0]
        self.assertEqual(entry["start"], "10:00")
        self.assertEqual(entry["end"], "11:00")

    def test_deadline_limit_respected(self):
        # 截止 15:00，时长 90 分钟 → 必须 13:30 前开始；白天只有 13:00 后空闲
        todos = [
            mk_schedule("c1", "上午课", self.day, "08:00", "13:00"),
            mk_todo("t1", "下午截止的作业", deadline=self.day.isoformat(),
                    deadline_time="15:00", category="homework",
                    duration_min=90, energy_cost=2),
        ]
        plan = planner.plan_day(todos, day=self.day, include_courses=False)
        self.assertEqual(plan["meta"]["planned"], 1)
        entry = plan["entries"][0]
        self.assertLessEqual(entry["end"], "15:00")

    def test_tolerance_flag_and_warning(self):
        # 16:00 前全忙；16:00-17:00 系数 0.9 → 60 分钟任务提供 1.8 点，
        # 任务耗能 2 → 2 ≤ 1.8×1.15 属容差适配（略超 1.0 倍）
        todos = [
            mk_schedule("c1", "全天课", self.day, "07:00", "16:00"),
            mk_todo("t1", "较费劲的短任务", deadline=self.day.isoformat(),
                    duration_min=60, energy_cost=2),
        ]
        plan = planner.plan_day(todos, day=self.day, include_courses=False)
        entry = plan["entries"][0]
        self.assertEqual(entry["start"], "16:00")
        self.assertTrue(entry["tolerance"])
        self.assertTrue(any(
            w["level"] == "tolerance" and w["task_id"] == "t1"
            for w in plan["warnings"]
        ))

    def test_blocked_warning_and_tomorrow(self):
        # 一整天都被占用 → 任务进「明日优先」并收到阻塞文案
        todos = [
            mk_schedule("c1", "全天满课", self.day, "07:00", "23:00"),
            mk_todo("t1", "冲刺答辩 PPT", deadline=self.day.isoformat(),
                    category="homework", priority="high",
                    duration_min=120, energy_cost=4),
        ]
        plan = planner.plan_day(todos, day=self.day, include_courses=False)
        self.assertEqual(plan["meta"]["planned"], 0)
        blocked = [w for w in plan["warnings"] if w["level"] == "blocked"]
        self.assertTrue(blocked)
        self.assertIn("今日未启动", blocked[0]["copy"])
        self.assertIn("t1", [t["task_id"] for t in plan["tomorrow"]])
        self.assertTrue(plan["meta"]["load_high"])

    def test_soft_deferral_on_overload(self):
        # 一天全忙：硬线任务阻塞触发负载过高 → 软线任务被建议顺延
        todos = [
            mk_schedule("c1", "全天满课", self.day, "07:00", "23:00"),
            mk_todo("t1", "期末考试复习", deadline=self.day.isoformat(),
                    priority="high", duration_min=120, energy_cost=4),
            mk_todo("t2", "推文初稿", deadline=self.day.isoformat(),
                    priority="low", duration_min=60, energy_cost=1),
        ]
        plan = planner.plan_day(todos, day=self.day, include_courses=False)
        self.assertEqual(len(plan["deferrals"]), 1)
        d = plan["deferrals"][0]
        self.assertEqual(d["task_id"], "t2")
        self.assertEqual(d["to_date"], (self.day + timedelta(days=1)).isoformat())
        self.assertIn("自动延后", d["copy"])
        # 软线任务不再同时出现在「明日优先」里（它已经有了去处）
        self.assertNotIn("t2", [t["task_id"] for t in plan["tomorrow"]])

    def test_apply_placements(self):
        todos = [
            mk_todo("t1", "交实验报告", deadline=self.day.isoformat(),
                    category="homework", priority="high", duration_min=60),
        ]
        plan = planner.plan_day(todos, day=self.day, include_courses=False)
        with _tmp_dir() as tmp:
            new_todos, applied = planner.apply_placements(
                todos, plan, data_dir=tmp)
            self.assertEqual(len(applied), 1)
            t = new_todos[0]
            self.assertEqual(t["kind"], "schedule")
            self.assertEqual(t["date"], self.day.isoformat())
            self.assertIsNotNone(t["time"])
            events = store.load_events(tmp)
            self.assertEqual(events[0]["type"], "plan_apply")

    def test_apply_deferrals(self):
        todos = [
            mk_schedule("c1", "全天满课", self.day, "07:00", "23:00"),
            mk_todo("t1", "期末考试复习", deadline=self.day.isoformat(),
                    priority="high", duration_min=120, energy_cost=4),
            mk_todo("t2", "周报初稿", deadline=self.day.isoformat(),
                    priority="low", duration_min=60, energy_cost=1),
        ]
        plan = planner.plan_day(todos, day=self.day, include_courses=False)
        self.assertEqual(len(plan["deferrals"]), 1)
        with _tmp_dir() as tmp:
            new_todos, applied = planner.apply_deferrals(
                todos, plan, data_dir=tmp)
            self.assertEqual(len(applied), 1)
            t2 = next(t for t in new_todos if t["id"] == "t2")
            self.assertEqual(t2["status"], "deferred")
            self.assertEqual(t2["deadline"], plan["deferrals"][0]["to_date"])
            self.assertEqual(t2["plan_defer_to"], plan["deferrals"][0]["to_date"])

    def test_record_move(self):
        todos = [mk_schedule("c1", "被拖拽的日程", self.day, "10:00", "11:00")]
        with _tmp_dir() as tmp:
            new_todos, rec = planner.record_move(
                todos, "c1", self.day.isoformat(), "15:00", "16:00",
                data_dir=tmp)
            self.assertIsNotNone(rec)
            self.assertEqual(new_todos[0]["time"], "15:00")
            events = store.load_events(tmp)
            self.assertEqual(events[0]["type"], "move")
            self.assertEqual(events[0]["from_time"], "10:00")

    def test_deferred_task_waits_for_target_day(self):
        today = self.day
        tomorrow = today + timedelta(days=1)
        t = mk_todo("t1", "周报初稿", deadline=(today - timedelta(days=1)).isoformat(),
                    status="deferred", plan_defer_to=tomorrow.isoformat(),
                    duration_min=30, energy_cost=1)
        # 顺延日还没到 → 今天不参与排程
        cands_today = planner.candidate_tasks([t], today)
        self.assertEqual(len(cands_today), 0)
        # 到了顺延日 → 重新参与
        cands_tomorrow = planner.candidate_tasks([t], tomorrow)
        self.assertEqual(len(cands_tomorrow), 1)

    def test_include_courses_no_crash(self):
        todos = [mk_todo("t1", "普通任务", duration_min=30, energy_cost=1)]
        plan = planner.plan_day(todos, day=self.day)  # 默认 include_courses=True
        self.assertTrue(plan["ok"])


if __name__ == "__main__":
    unittest.main()
