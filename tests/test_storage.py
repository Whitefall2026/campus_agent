# -*- coding: utf-8 -*-
from datetime import date, timedelta
import json
import os
import tempfile
import unittest

from app.core import storage
from app.core.storage import rollover_unfinished_schedules


class TestScheduleRollover(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 9, 8)

    def test_previous_unfinished_schedule_becomes_todo(self):
        items = [{
            "id": "past", "title": "整理会议纪要", "kind": "schedule",
            "date": "2026-09-07", "time": "20:00", "end_time": "21:00",
            "location": "图书馆", "deadline": "2026-09-10",
            "status": "pending", "plan_defer_to": "2026-09-09",
        }]

        changed = rollover_unfinished_schedules(items, self.today)

        self.assertEqual(changed, 1)
        item = items[0]
        self.assertEqual(item["kind"], "todo")
        self.assertIsNone(item["date"])
        self.assertIsNone(item["time"])
        self.assertIsNone(item["end_time"])
        self.assertIsNone(item["plan_defer_to"])
        self.assertEqual(item["rolled_over_from"], "2026-09-07")
        self.assertEqual(item["rolled_over_from_time"], "20:00")
        self.assertEqual(item["rolled_over_from_end_time"], "21:00")
        self.assertEqual(item["rolled_over_on"], "2026-09-08")
        self.assertEqual(item["deadline"], "2026-09-10")
        self.assertEqual(item["location"], "图书馆")

    def test_rollover_is_idempotent(self):
        items = [{
            "id": "past", "title": "整理会议纪要", "kind": "schedule",
            "date": "2026-09-07", "time": "20:00", "status": "pending",
        }]
        self.assertEqual(rollover_unfinished_schedules(items, self.today), 1)
        snapshot = dict(items[0])
        self.assertEqual(rollover_unfinished_schedules(items, self.today), 0)
        self.assertEqual(items[0], snapshot)

    def test_load_todos_persists_rollover(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        items = [{
            "id": "past", "title": "整理会议纪要", "kind": "schedule",
            "date": yesterday, "time": "20:00", "status": "pending",
        }]
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "todos.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
            original = storage.DATA_FILE
            storage.DATA_FILE = path
            try:
                loaded = storage.load_todos()
            finally:
                storage.DATA_FILE = original
            with open(path, "r", encoding="utf-8") as f:
                persisted = json.load(f)

        self.assertEqual(loaded[0]["kind"], "todo")
        self.assertEqual(persisted, loaded)
        self.assertEqual(persisted[0]["rolled_over_from_time"], "20:00")

    def test_course_items_never_roll_over(self):
        items = [
            {
                "id": "course-flag", "title": "高等数学", "kind": "schedule",
                "date": "2026-09-07", "time": "08:00", "status": "pending",
                "course": True,
            },
            {
                "id": "course-source", "title": "大学英语", "kind": "schedule",
                "date": "2026-09-07", "time": "10:00", "status": "pending",
                "source": "course",
            },
        ]
        before = [dict(item) for item in items]

        changed = rollover_unfinished_schedules(items, self.today)

        self.assertEqual(changed, 0)
        self.assertEqual(items, before)

    def test_completed_and_current_or_future_schedules_stay_schedules(self):
        items = [
            {
                "id": "done", "title": "已完成", "kind": "schedule",
                "date": "2026-09-07", "time": "09:00", "status": "done",
            },
            {
                "id": "today", "title": "今天", "kind": "schedule",
                "date": "2026-09-08", "time": "09:00", "status": "pending",
            },
            {
                "id": "future", "title": "未来", "kind": "schedule",
                "date": "2026-09-09", "time": "09:00", "status": "pending",
            },
        ]

        changed = rollover_unfinished_schedules(items, self.today)

        self.assertEqual(changed, 0)
        self.assertTrue(all(item["kind"] == "schedule" for item in items))


if __name__ == "__main__":
    unittest.main()
