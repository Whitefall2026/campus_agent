# -*- coding: utf-8 -*-
"""Review regressions; run with python -B -m unittest discover -s tests
-p test_review_regressions.py -v. All persisted fixtures live in temporary data.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Set before app imports; setUp also redirects constants when discovery has
# already imported app modules through another test file.
os.environ.setdefault(
    "RUC_AGENT_DATA_DIR",
    os.path.join(tempfile.gettempdir(), "ruc-agent-tests", "review"),
)

import desktop
from app import paths
from app.ai import chat, evidence, gateway, memory, profile as user_profile
from app.core import courses, extractor, scheduler, storage
from app.planner import energy, planner, risk, shield, store


def flat_profile():
    # 16 waking hours, two one-point half-hours per hour: 32 points/day.
    return {"hours": [0.0] * 7 + [1.0] * 16 + [0.0], "version": 1}


def todo(item_id, **fields):
    return {
        "id": item_id, "title": item_id, "kind": "todo",
        "status": "pending", "category": "other", "priority": "medium",
        "duration_min": 30, "energy_cost": 1, **fields,
    }


def schedule(item_id, day, start, end, **fields):
    return todo(item_id, kind="schedule", date=day.isoformat(),
                time=start, end_time=end, **fields)


class IsolatedDataTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ruc-review-")
        self.addCleanup(temp.cleanup)
        self.data_dir = temp.name
        stack = ExitStack()
        self.patches = stack
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {
            "RUC_AGENT_DATA_DIR": self.data_dir,
        }))
        # Patch only filesystem boundaries, retaining real persistence and
        # algorithm calls. Restore every constant after each test.
        for module, files in (
            (paths, {}),
            (store, {}),
            (courses, {"COURSES_FILE": "courses.json"}),
            (storage, {"DATA_FILE": "todos.json", "GOALS_FILE": "goals.json"}),
            (gateway, {"AI_CONFIG_FILE": "ai_config.json",
                       "AI_PENDING_FILE": "ai_pending.json",
                       "AI_SEEN_FILE": "ai_seen.json"}),
            (chat, {"CHAT_FILE": "chat_thread.json"}),
            (evidence, {"EVIDENCE_FILE": "user_evidence.json"}),
            (memory, {"MEMORY_FILE": "user_memory.json"}),
            (user_profile, {"PROFILE_FILE": "user_profile.json",
                            "HISTORY_FILE": "user_state_history.json"}),
            (desktop, {"RUNTIME_FILE": "runtime.json"}),
        ):
            stack.enter_context(patch.object(module, "DATA_DIR", self.data_dir))
            for name, filename in files.items():
                stack.enter_context(patch.object(
                    module, name, os.path.join(self.data_dir, filename)))
        self.day = date(2026, 10, 7)


class TestEnergyRegressions(IsolatedDataTest):
    def test_daily_budget_uses_the_same_half_hour_unit_as_slots(self):
        prof = flat_profile()
        self.assertAlmostEqual(energy.block_points(9 * 60, 30, prof), 1.0)
        self.assertAlmostEqual(energy.block_points(9 * 60, 60, prof), 2.0)
        self.assertAlmostEqual(energy.block_points(9 * 60 + 15, 15, prof), 0.5)
        self.assertAlmostEqual(energy.available_total(prof), 32.0)

    def test_non_finite_or_invalid_persisted_curves_fall_back(self):
        for bad in (float("nan"), float("inf"), -float("inf"),
                    True, -0.1, 1.9, "1.0"):
            with self.subTest(coefficient=bad):
                prof = flat_profile()
                prof["hours"][9] = bad
                store.save_profile(prof, self.data_dir)
                loaded = energy.load_profile(self.data_dir)
                self.assertEqual(loaded["hours"], energy.default_profile()["hours"])
                self.assertAlmostEqual(energy.coefficient_at(9, prof), 1.2)

    def test_invalid_times_do_not_wrap_into_another_day(self):
        prof = flat_profile()
        for hour in (-1, 24, 33, float("nan"), float("inf"), "bad"):
            with self.subTest(hour=hour):
                self.assertEqual(energy.coefficient_at(hour, prof), 0.0)
        self.assertEqual(energy.block_points(23 * 60, 600, prof), 0.0)
        self.assertEqual(energy.block_points(9 * 60, -30, prof), 0.0)

    def test_feedback_is_shared_across_covered_hours_without_mutating_input(self):
        prof = flat_profile()
        before = copy.deepcopy(prof)
        # 09:30-11:30 covers 30, 60, 30 minutes respectively.
        updated = energy.record_feedback(9.5, "tough", prof, duration_min=120)
        self.assertAlmostEqual(updated["hours"][9], 0.99625)
        self.assertAlmostEqual(updated["hours"][10], 0.9925)
        self.assertAlmostEqual(updated["hours"][11], 0.99625)
        self.assertEqual(updated["hours"][8], 1.0)
        self.assertEqual(updated["hours"][12], 1.0)
        self.assertEqual(prof, before)

    def test_feedback_preserves_unavailable_hours_and_bounds(self):
        prof = flat_profile()
        prof["hours"][10] = 0.0
        prof["hours"][9] = 1.8
        easy = energy.record_feedback(9.5, "easy", prof, duration_min=90)
        self.assertEqual(easy["hours"][9], 1.8)
        self.assertEqual(easy["hours"][10], 0.0)
        prof["hours"][9] = 0.1
        tough = energy.record_feedback(9, "tough", prof, duration_min=60)
        self.assertEqual(tough["hours"][9], 0.1)
        overnight = energy.record_feedback(22.5, "easy", prof, duration_min=90)
        self.assertEqual(overnight["hours"][23], 0.0)


class TestPlannerRegressions(IsolatedDataTest):
    def test_not_before_limits_new_slots_without_inflating_existing_occupancy(self):
        options = dict(day=self.day, profile=flat_profile(), include_courses=False)
        default = planner.plan_day([todo("task")], **options)
        self.assertEqual(default["entries"][0]["start"], "07:00")
        later = planner.plan_day([todo("task")], not_before=14 * 60 + 30,
                                 ai_placements=[{"task_id": "task", "start": "09:00"}],
                                 **options)
        self.assertEqual(later["entries"][0]["start"], "14:30")
        self.assertEqual(later["budget"]["scheduled_points"], 0.0)
        self.assertEqual(later["budget"]["available_points"], 32.0)
        self.assertFalse(later["entries"][0]["ai"])
        after_close = planner.plan_day([todo("task")], not_before=23 * 60, **options)
        self.assertEqual(after_close["entries"], [])

    def test_partial_busy_cells_are_not_used_for_placements(self):
        prof = {"hours": [0.0] * 24}
        prof["hours"][9] = 1.0
        for start, end, expected in (
            ("09:10", "09:20", ["09:30"]),
            ("09:20", "09:40", []),
            ("08:45", "09:00", ["09:00"]),
            ("10:00", "10:15", ["09:00"]),
        ):
            with self.subTest(busy=(start, end)):
                items = [schedule("meeting", self.day, start, end), todo("task")]
                plan = planner.plan_day(items, day=self.day, profile=prof,
                                        include_courses=False)
                self.assertEqual([e["start"] for e in plan["entries"]], expected)

    def test_existing_and_proposed_occupancy_share_one_daily_budget(self):
        prof = {"hours": [0.0] * 24}
        prof["hours"][9] = 1.0
        plan = planner.plan_day(
            [schedule("meeting", self.day, "09:10", "09:20"), todo("task")],
            day=self.day, profile=prof, include_courses=False)
        budget = plan["budget"]
        self.assertEqual(budget["total_points"], 2.0)
        self.assertEqual(budget["available_points"], 1.0)
        self.assertEqual(budget["scheduled_points"], 1.0)
        self.assertEqual(budget["planned_points"], 1.0)
        self.assertEqual(budget["committed_ratio"], 1.0)

    def test_empty_candidate_ids_mean_no_tasks_instead_of_all_tasks(self):
        items = [todo("task")]
        options = dict(day=self.day, profile=flat_profile(), include_courses=False)
        automatic = planner.plan_day(items, **options)
        self.assertEqual([e["task_id"] for e in automatic["entries"]], ["task"])
        empty = planner.plan_day(items, candidate_ids=[], **options)
        self.assertEqual(empty["meta"]["candidates"], 0)
        self.assertEqual(empty["entries"], [])
        self.assertEqual(empty["warnings"], [])
        self.assertEqual(empty["deferrals"], [])
        self.assertEqual(empty["budget"]["planned_points"], 0.0)

    def test_completed_schedule_still_reserves_actual_time(self):
        items = [schedule("done", self.day, "09:00", "10:00", status="done")]
        plan = planner.plan_day(items, day=self.day, profile=flat_profile(),
                                include_courses=False, candidate_ids=[])
        self.assertEqual(plan["budget"]["available_points"], 30.0)
        self.assertEqual(plan["budget"]["scheduled_points"], 2.0)
        runs = planner.free_runs(items, self.day, include_courses=False,
                                 profile=flat_profile())
        self.assertFalse(any(r["start"] < 600 and 540 < r["end"] for r in runs))
        energy.save_profile(flat_profile(), self.data_dir)
        suggested = scheduler.suggest_slot(todo("task"), items, self.day,
                                           datetime(2026, 10, 7, 8))
        self.assertEqual(suggested["time"], "10:30")


class TestShieldRegressions(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        energy.save_profile(flat_profile(), self.data_dir)

    def metrics(self, items):
        plan = planner.plan_day(items, day=self.day, profile=flat_profile(),
                                include_courses=False, candidate_ids=[])
        return shield.load_metrics(items, day=self.day, plan=plan)

    def test_far_deadlines_and_future_deferrals_do_not_raise_current_load(self):
        far = (self.day + timedelta(days=30)).isoformat()
        deferred = (self.day + timedelta(days=4)).isoformat()
        items = [todo("far-%d" % i, deadline=far, energy_cost=4) for i in range(6)]
        items += [todo("deferred-%d" % i, deadline=self.day.isoformat(),
                       plan_defer_to=deferred, energy_cost=4) for i in range(6)]
        metrics = self.metrics(items)
        self.assertEqual(metrics["undone_todos"], 12)
        self.assertEqual(metrics["near_term_todos"], 0)
        self.assertEqual(metrics["near_term_energy"], 0)
        self.assertEqual(metrics["demand_ratio_3d"], 0.0)
        self.assertEqual(metrics["level"], "low")

    def test_three_day_window_includes_today_plus_two_days_only(self):
        items = [schedule("day-%d" % i, self.day + timedelta(days=i),
                          "09:00", "09:30") for i in range(4)]
        items += [schedule("past", self.day - timedelta(days=1), "09:00", "09:30"),
                  schedule("done", self.day, "10:00", "10:30", status="done"),
                  todo("near", deadline=(self.day + timedelta(days=2)).isoformat(),
                       energy_cost=4),
                  todo("outside", deadline=(self.day + timedelta(days=3)).isoformat(),
                       energy_cost=4)]
        metrics = self.metrics(items)
        self.assertEqual(metrics["density_3d"], 3)
        self.assertEqual(metrics["near_term_todos"], 1)
        self.assertEqual(metrics["near_term_energy"], 4)

    def test_three_day_demand_uses_actual_free_time_on_both_future_days(self):
        items = [schedule("full-%d" % i, self.day + timedelta(days=i),
                          "07:00", "23:00") for i in (1, 2)]
        items.append(todo("near", deadline=(self.day + timedelta(days=2)).isoformat(),
                          energy_cost=4))
        metrics = self.metrics(items)
        # Only today has capacity: four points / 32 points = 0.125.
        self.assertEqual(metrics["demand_ratio_3d"], 0.125)
        items.append(schedule("outside", self.day + timedelta(days=3),
                              "07:00", "23:00"))
        self.assertEqual(self.metrics(items)["demand_ratio_3d"], 0.125)

    def test_existing_full_day_schedule_counts_as_high_load_without_candidates(self):
        metrics = self.metrics([schedule("full", self.day, "07:00", "23:00")])
        self.assertEqual(metrics["undone_todos"], 0)
        self.assertEqual(metrics["planned_energy"], 32.0)
        self.assertEqual(metrics["planned_ratio"], 1.0)
        self.assertEqual(metrics["level"], "high")


class TestRiskRegressions(IsolatedDataTest):
    def test_latest_rating_replaces_done_but_separate_dates_and_legacy_rows_remain(self):
        events = [
            {"type": "done", "task_id": "a", "date": "2026-10-07",
             "weekday": 2, "hour": 9, "rating": "ok"},
            {"type": "rating", "task_id": "a", "date": "2026-10-07",
             "weekday": 2, "hour": 9, "rating": "tough"},
            {"type": "done", "task_id": "a", "date": "2026-10-14",
             "weekday": 2, "hour": 9, "rating": "easy"},
            {"type": "done", "weekday": 2, "hour": 9, "rating": "ok"},
            {"type": "done", "weekday": 2, "hour": 9, "rating": "tough"},
            {"type": "move", "task_id": "a", "date": "2026-10-07",
             "weekday": 2, "hour": 9, "rating": "easy"},
        ]
        for event in events:
            store.append_event(event, self.data_dir)
        # Four independent observations: 0.5, 1.0, 0.8, 0.5.
        stats = risk.load_history_stats()
        self.assertEqual(stats, {(2, "morning"): 0.7})
        self.assertEqual(risk.load_history_stats([]), {})

    def test_risk_plan_reads_persisted_history_unless_stats_are_explicit(self):
        store.append_event({
            "type": "rating", "task_id": "past", "date": self.day.isoformat(),
            "weekday": 2, "hour": 9, "rating": "tough",
        }, self.data_dir)
        plan = {"date": self.day.isoformat(), "entries": [{
            "task_id": "new", "title": "new", "start": "09:00", "end": "09:30",
            "duration_min": 30, "energy_cost": 1, "deadline_type": "hard",
        }]}
        before = copy.deepcopy(plan)
        persisted = risk.plan_with_risk(plan, n=1000, seed=7)
        explicit = risk.plan_with_risk(plan, stats={(2, "morning"): 0.5}, n=1000, seed=7)
        defaults = risk.plan_with_risk(plan, stats={}, n=1000, seed=7)
        self.assertEqual(persisted, explicit)
        self.assertTrue(persisted["entries"][0]["risk"])
        self.assertFalse(defaults["entries"][0]["risk"])
        self.assertLess(persisted["entries"][0]["probability"],
                        defaults["entries"][0]["probability"])
        self.assertEqual(plan, before)


class TestSchedulerRegressions(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        energy.save_profile(flat_profile(), self.data_dir)

    def test_cross_midnight_overlap_is_reported_by_state_and_preview(self):
        overnight = schedule("overnight", self.day, "23:30", "01:00")
        next_day = self.day + timedelta(days=1)
        overlap = schedule("overlap", next_day, "00:30", "01:30")
        touching = schedule("touching", next_day, "01:00", "02:00")
        conflicts = scheduler.find_conflicts([overlap, overnight])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(set(conflicts[0]["ids"]), {"overnight", "overlap"})
        self.assertEqual(len(scheduler.slot_conflicts(overlap, [overnight])), 1)
        self.assertEqual(scheduler.slot_conflicts(touching, [overnight]), [])
        self.assertEqual(scheduler.find_conflicts([overnight, touching]), [])

    def test_overnight_busy_time_blocks_the_next_morning_suggestion(self):
        overnight = schedule("overnight", self.day, "23:30", "10:00")
        tomorrow = self.day + timedelta(days=1)
        suggested = scheduler.suggest_slot(todo("task"), [overnight], tomorrow,
                                           datetime(2026, 10, 8, 8))
        self.assertEqual((suggested["date"], suggested["time"]),
                         ("2026-10-08", "10:30"))
        self.assertEqual(planner.day_busy([overnight], tomorrow, include_courses=False),
                         [(0, 600)])

    def test_deadline_check_uses_finish_time_including_next_day(self):
        for start, end, deadline, deadline_time, expected in (
            ("09:00", "10:00", "2026-10-07", "09:30", True),
            ("09:00", "10:00", "2026-10-07", "10:00", False),
            ("23:30", "01:00", "2026-10-07", "23:59", True),
            ("23:30", "01:00", "2026-10-08", "01:00", False),
        ):
            with self.subTest(interval=(start, end, deadline, deadline_time)):
                item = schedule("task", self.day, start, end, deadline=deadline,
                                deadline_time=deadline_time)
                conflicts = scheduler.find_conflicts([item])
                self.assertEqual(bool(conflicts), expected)
                if expected:
                    self.assertEqual(conflicts[0]["kind"], "deadline")

    def test_suggestion_finishes_by_deadline_and_respects_now(self):
        now = datetime(2026, 10, 7, 10, 31)
        item = todo("task", deadline=self.day.isoformat(), deadline_time="15:00",
                    duration_min=60, energy_cost=2)
        suggested = scheduler.suggest_slot(item, [], self.day, now)
        self.assertEqual((suggested["time"], suggested["end_time"]), ("14:00", "15:00"))
        item["deadline_time"] = "14:59"
        self.assertIsNone(scheduler.suggest_slot(item, [], self.day, now))

    def test_full_or_exhausted_today_never_forces_a_fallback_slot(self):
        full = schedule("full", self.day, "07:00", "23:00")
        for deadline in ("2026-10-06", "2026-10-07"):
            with self.subTest(deadline=deadline):
                item = todo("task", deadline=deadline)
                self.assertIsNone(scheduler.suggest_slot(
                    item, [full], self.day, datetime(2026, 10, 7, 8)))
                self.assertIsNone(scheduler.suggest_slot(
                    item, [], self.day, datetime(2026, 10, 7, 22)))

    def test_tomorrow_deadline_does_not_choose_an_elapsed_slot_today(self):
        item = todo("task", deadline="2026-10-08", deadline_time="10:00")
        suggested = scheduler.suggest_slot(item, [], self.day, datetime(2026, 10, 7, 21, 1))
        self.assertEqual((suggested["date"], suggested["time"]), ("2026-10-08", "09:00"))

    def test_suggestions_use_persisted_energy_and_reject_unavailable_days(self):
        prof = {"hours": [0.0] * 24}
        prof["hours"][14] = 1.0
        energy.save_profile(prof, self.data_dir)
        suggested = scheduler.suggest_slot(todo("task"), [], self.day,
                                           datetime(2026, 10, 7, 8))
        self.assertEqual(suggested["time"], "14:00")
        energy.save_profile({"hours": [0.0] * 24}, self.data_dir)
        self.assertIsNone(scheduler.suggest_slot(todo("task"), [], self.day,
                                                datetime(2026, 10, 7, 8)))


class TestExtractorRegressions(IsolatedDataTest):
    def test_next_week_means_the_next_calendar_week(self):
        for today, text, expected in (
            (date(2026, 10, 7), "下周五晚上7点开会", "2026-10-16"),
            (date(2026, 10, 5), "下星期一晚上7点开会", "2026-10-12"),
            (date(2026, 10, 11), "下个周日晚上7点开会", "2026-10-18"),
            (date(2026, 12, 31), "下礼拜一晚上7点开会", "2027-01-04"),
        ):
            with self.subTest(today=today, text=text):
                parsed = extractor.parse_text(text, today=today,
                                              now=datetime.combine(today, datetime.min.time()))
                self.assertEqual(parsed["date"], expected)
                self.assertEqual(parsed["time"], "19:00")


class TestPendingRegressions(IsolatedDataTest):
    def test_overnight_pending_expires_after_next_day_end(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls):
                return cls.current
        item = {"date": "2026-10-07", "time": "23:30", "end_time": "01:00"}
        with patch.object(gateway, "datetime", FixedDatetime):
            for when, expected in ((datetime(2026, 10, 7, 23, 45), False),
                                   (datetime(2026, 10, 8, 0, 45), False),
                                   (datetime(2026, 10, 8, 1, 1), True)):
                FixedDatetime.current = when
                self.assertEqual(gateway.is_pending_expired(item), expected)

    def test_wechat_same_message_same_title_different_dates_survive_deduplication(self):
        first = {"id": "first", "chat_username": "group", "seq": 7,
                 "fields": {"title": "例会", "kind": "schedule",
                            "date": "2026-10-08", "time": "09:00"}}
        second = copy.deepcopy(first)
        second["id"] = "second"
        second["fields"]["date"] = "2026-10-09"
        gateway.add_pending(first, self.data_dir)
        gateway.add_pending(second, self.data_dir)
        repeated = {**second, "id": "rescan"}
        retained = gateway.add_pending(repeated, self.data_dir)
        self.assertEqual(retained["id"], "second")
        self.assertEqual({p["id"] for p in gateway.get_pending(self.data_dir)},
                         {"first", "second"})

    def test_chat_same_title_different_dates_has_two_actionable_snapshots(self):
        # Exercise real rule extraction and persistence without an AI call.
        first = chat.chat_turn("2099年10月8日上午9点开例会")
        second = chat.chat_turn("2099年10月9日上午9点开例会")
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(len(second["items"]), 1)
        snapshots = first["items"] + second["items"]
        self.assertEqual(snapshots[0]["fields"]["title"], snapshots[1]["fields"]["title"])
        pending = gateway.get_pending()
        self.assertEqual(len(pending), 2)
        self.assertEqual({p["fields"]["date"] for p in pending},
                         {"2099-10-08", "2099-10-09"})
        self.assertEqual({s["id"] for s in snapshots}, {p["id"] for p in pending})
        self.assertTrue(chat.mark_item_outcome(second["items"][0]["id"], "accepted:schedule"))
        history_items = [it for m in chat.public_history() for it in m.get("items", [])]
        self.assertEqual([it["outcome"] for it in history_items], [None, "accepted:schedule"])


class TestHandlerRegressions(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        # Delayed import: WeChatBridge's constructor reads its configured data
        # directory. app.paths has already been redirected by the base setUp.
        from app.ai import planner as ai_planner
        from app.web import handlers
        self.handlers = handlers
        for module in (handlers, ai_planner):
            self.patches.enter_context(patch.object(module, "DATA_DIR", self.data_dir))
        for module, name in ((handlers, "_DATE_PLANS"),
                             (handlers, "_BROWSE_CACHE"),
                             (ai_planner, "_GUIDE_CACHE")):
            self.patches.enter_context(patch.object(module, name, {}))
        energy.save_profile(flat_profile(), self.data_dir)

        class QuietHandler(handlers.Handler):
            def log_message(self, fmt, *args):
                pass

        from app.web.server import LocalHTTPServer
        server = LocalHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.base_url = "http://127.0.0.1:%d" % server.server_address[1]

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop_server)

    def request(self, path, body=None):
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            result = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result["ok"])
        return result

    def fixed_clock(self, now):
        class FixedDate(date):
            @classmethod
            def today(cls):
                return now.date()

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        stack = ExitStack()
        stack.enter_context(patch.object(self.handlers, "date", FixedDate))
        stack.enter_context(patch.object(self.handlers, "datetime", FixedDatetime))
        return stack

    def test_ai_plan_rounds_current_time_up_and_future_day_keeps_default_start(self):
        storage.save_todos([todo("task")])
        for now, expected in ((datetime(2026, 10, 7, 14, 1), "14:30"),
                              (datetime(2026, 10, 7, 14, 30), "14:30"),
                              (datetime(2026, 10, 7, 14, 30, 1), "15:00"),
                              (datetime(2026, 10, 7, 14, 30, 0, 1), "15:00")):
            with self.subTest(now=now), self.fixed_clock(now):
                result = self.request("/api/plan/ai", {"date": "2026-10-07"})
                self.assertEqual(result["plan"]["entries"][0]["start"], expected)
                self.assertEqual(result["plan"]["budget"]["scheduled_points"], 0.0)
                future = self.request("/api/plan/ai", {"date": "2026-10-08"})
                self.assertEqual(future["plan"]["entries"][0]["start"], "07:00")

    def test_invalid_schedule_patch_preserves_existing_item(self):
        item = schedule("scheduled", date(2099, 10, 8), "09:00", "10:00")
        storage.save_todos([item])
        for body in ({"date": None}, {"title": ""}):
            req = urllib.request.Request(self.base_url + "/api/todos/scheduled",
                method="PATCH", data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=10)
            with caught.exception as response:
                self.assertEqual(response.code, 400)
            self.assertEqual(storage.load_todos(), [item])

    def test_feedback_api_uses_fractional_start_and_actual_end_then_is_idempotent(self):
        # Actual 09:30-11:30 beats the stale 30-minute duration field.
        storage.save_todos([schedule("done", self.day, "09:30", "11:30",
                                     duration_min=30, status="done")])
        result = self.request("/api/feedback", {"id": "done", "rating": "tough"})
        self.assertEqual(result["hour"], 9.5)
        hours = result["energy"]["hours"]
        self.assertAlmostEqual(hours[9], 0.99625)
        self.assertAlmostEqual(hours[10], 0.9925)
        self.assertAlmostEqual(hours[11], 0.99625)
        repeated = self.request("/api/feedback", {"id": "done", "rating": "tough"})
        self.assertTrue(repeated["already_recorded"])
        self.assertEqual(repeated["energy"]["hours"], hours)
        self.assertEqual(len([e for e in store.load_events() if e["type"] == "rating"]), 1)

    def test_same_title_chat_items_on_different_dates_can_both_be_accepted(self):
        first = self.request("/api/chat", {"text": "2099年10月8日上午9点开例会"})
        second = self.request("/api/chat", {"text": "2099年10月9日上午9点开例会"})
        ids = [first["items"][0]["id"], second["items"][0]["id"]]
        for item_id in ids:
            self.request("/api/ai/pending/%s/accept" % item_id, {"kind": "schedule"})
        state = self.request("/api/state")
        self.assertEqual({t["date"] for t in state["todos"]},
                         {"2099-10-08", "2099-10-09"})
        self.assertEqual({t["source_pending_id"] for t in state["todos"]}, set(ids))
        self.assertEqual(gateway.get_pending(), [])


class TestDesktopRegressions(IsolatedDataTest):
    def test_duplicate_launch_no_browser_exits_without_opening_existing_page(self):
        kernel32 = Mock()
        kernel32.CreateMutexW.return_value = 123
        # A simulated Windows mutex avoids acquiring/releasing a real system
        # handle, and permits this test to run on Linux as well as Windows.
        windows_os = SimpleNamespace(name="nt", environ=os.environ,
                                     makedirs=os.makedirs, path=os.path)
        with patch.object(desktop, "os", windows_os), \
                patch.object(desktop.ctypes, "WinDLL", return_value=kernel32, create=True), \
                patch.object(desktop.ctypes, "set_last_error", create=True), \
                patch.object(desktop.ctypes, "get_last_error", return_value=183, create=True), \
                patch.object(desktop.webbrowser, "open") as browser:
            self.assertEqual(desktop.main(["--no-browser"]), 0)
            browser.assert_not_called()
            self.assertEqual(desktop.main([]), 0)
            browser.assert_called_once_with("http://127.0.0.1:8000/")


if __name__ == "__main__":
    unittest.main()
