"""Unit tests for chore-chart's backend (stdlib unittest, no dependencies).

Run from the repo root::

    python3 -m unittest app.test_server -v

or directly::

    python3 app/test_server.py

The date math in due_on()/next_due() and the v1 -> v2 migration are the
highest-risk parts of the app, so they get the strictest coverage here.
"""

import json
import io
import random
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

# Make `import server` work whether this is run as a script or via -m.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402  (import after the sys.path tweak)


def make_rule(freq="weekly", interval=1, weekdays=None, month_days=None,
              anchor=None, until=None):
    """A rule in the shape due_on()/next_due() expect (dates, not strings)."""
    return {"freq": freq, "interval": interval, "weekdays": weekdays,
            "month_days": month_days, "anchor": anchor, "until": until}


def d(value):
    return date.fromisoformat(value)


class DueOnTests(unittest.TestCase):
    def test_once_fires_only_on_its_anchor(self):
        rule = make_rule("once", anchor=d("2026-03-04"))
        self.assertTrue(server.due_on(rule, d("2026-03-04")))
        self.assertFalse(server.due_on(rule, d("2026-03-05")))
        self.assertFalse(server.due_on(rule, d("2026-03-03")))

    def test_daily_every_n_days(self):
        rule = make_rule("daily", interval=3, anchor=d("2026-01-01"))
        self.assertTrue(server.due_on(rule, d("2026-01-01")))
        self.assertFalse(server.due_on(rule, d("2026-01-02")))
        self.assertFalse(server.due_on(rule, d("2026-01-03")))
        self.assertTrue(server.due_on(rule, d("2026-01-04")))

    def test_weekly_on_selected_weekdays(self):
        rule = make_rule("weekly", weekdays=[0, 3], anchor=d("2026-10-05"))
        self.assertTrue(server.due_on(rule, d("2026-10-05")))   # Mon
        self.assertFalse(server.due_on(rule, d("2026-10-06")))  # Tue
        self.assertTrue(server.due_on(rule, d("2026-10-08")))   # Thu

    def test_weekly_every_two_weeks_skips_a_week(self):
        rule = make_rule("weekly", interval=2, weekdays=[0], anchor=d("2026-10-05"))
        self.assertTrue(server.due_on(rule, d("2026-10-05")))
        self.assertFalse(server.due_on(rule, d("2026-10-12")))
        self.assertTrue(server.due_on(rule, d("2026-10-19")))

    def test_monthly_day_clamps_to_a_short_month(self):
        rule = make_rule("monthly", month_days=[31], anchor=d("2026-01-31"))
        self.assertTrue(server.due_on(rule, d("2026-01-31")))
        self.assertFalse(server.due_on(rule, d("2026-02-27")))
        self.assertTrue(server.due_on(rule, d("2026-02-28")))   # clamped

    def test_monthly_last_day_token(self):
        rule = make_rule("monthly", month_days=[-1], anchor=d("2026-01-31"))
        self.assertFalse(server.due_on(rule, d("2026-02-27")))
        self.assertTrue(server.due_on(rule, d("2026-02-28")))
        self.assertTrue(server.due_on(rule, d("2026-04-30")))

    def test_monthly_every_other_month(self):
        rule = make_rule("monthly", interval=2, month_days=[1], anchor=d("2026-01-01"))
        self.assertTrue(server.due_on(rule, d("2026-01-01")))
        self.assertFalse(server.due_on(rule, d("2026-02-01")))
        self.assertTrue(server.due_on(rule, d("2026-03-01")))

    def test_yearly_feb_29_falls_back_to_the_28th(self):
        rule = make_rule("yearly", anchor=d("2024-02-29"))
        self.assertTrue(server.due_on(rule, d("2028-02-29")))   # leap
        self.assertTrue(server.due_on(rule, d("2027-02-28")))   # non-leap
        self.assertFalse(server.due_on(rule, d("2027-03-01")))

    def test_anchor_gate_blocks_earlier_days(self):
        rule = make_rule("daily", anchor=d("2026-05-10"))
        self.assertFalse(server.due_on(rule, d("2026-05-09")))
        self.assertTrue(server.due_on(rule, d("2026-05-10")))

    def test_until_gate_is_inclusive(self):
        rule = make_rule("daily", anchor=d("2026-05-01"), until=d("2026-05-03"))
        self.assertTrue(server.due_on(rule, d("2026-05-03")))
        self.assertFalse(server.due_on(rule, d("2026-05-04")))


class NextDueTests(unittest.TestCase):
    def test_returns_the_next_occurrence(self):
        self.assertEqual(
            server.next_due(make_rule("weekly", weekdays=[0], anchor=d("2026-10-05")), d("2026-10-08")),
            d("2026-10-12"))
        self.assertEqual(
            server.next_due(make_rule("daily", interval=3, anchor=d("2026-10-01")), d("2026-10-08")),
            d("2026-10-10"))

    def test_none_when_nothing_is_upcoming(self):
        self.assertIsNone(server.next_due(make_rule("once", anchor=d("2026-01-01")), d("2026-10-08")))
        self.assertIsNone(server.next_due(
            make_rule("daily", anchor=d("2026-01-01"), until=d("2026-01-05")), d("2026-10-08")))

    def test_matches_a_brute_force_due_on_scan(self):
        """next_due() must agree with due_on() on randomized rules."""
        rng = random.Random(20261008)

        def random_rule():
            freq = rng.choice(["once", "daily", "weekly", "monthly", "yearly"])
            interval = rng.randint(1, 4)
            anchor = date(2023, 1, 1) + timedelta(days=rng.randint(0, 1000))
            weekdays = (sorted(rng.sample(range(7), rng.randint(1, 3)))
                        if freq == "weekly" else None)
            month_days = (sorted(rng.sample([1, 5, 15, 28, 29, 30, 31, -1], rng.randint(1, 3)))
                          if freq == "monthly" else None)
            until = (anchor + timedelta(days=rng.randint(0, 800))
                     if rng.random() < 0.4 else None)
            return make_rule(freq, interval, weekdays, month_days, anchor, until)

        def brute_force(rule, start, horizon=1500):
            for offset in range(horizon):
                day = start + timedelta(days=offset)
                if server.due_on(rule, day):
                    return day
            return None

        for _ in range(1000):
            rule = random_rule()
            start = date(2024, 1, 1) + timedelta(days=rng.randint(0, 900))
            self.assertEqual(server.next_due(rule, start), brute_force(rule, start),
                             msg=f"rule={rule} start={start}")


class NormalizeRuleTests(unittest.TestCase):
    def test_defaults_to_weekly_anchored_today(self):
        norm = server.normalize_rule({})
        self.assertEqual(norm["freq"], "weekly")
        self.assertEqual(norm["interval"], 1)
        self.assertEqual(norm["anchor"], date.today().isoformat())
        self.assertEqual(json.loads(norm["weekdays"]), [date.today().weekday()])

    def test_drops_selectors_that_do_not_apply(self):
        norm = server.normalize_rule({"freq": "daily", "weekdays": [0, 1],
                                      "month_days": [5], "anchor": "2026-05-01"})
        self.assertIsNone(norm["weekdays"])
        self.assertIsNone(norm["month_days"])

    def test_sorts_and_deduplicates_selectors(self):
        norm = server.normalize_rule({"freq": "weekly", "weekdays": [5, 0, 0],
                                      "anchor": "2026-05-01"})
        self.assertEqual(json.loads(norm["weekdays"]), [0, 5])

    def test_rejects_bad_freq(self):
        with self.assertRaises(ValueError):
            server.normalize_rule({"freq": "hourly"})

    def test_rejects_interval_out_of_range(self):
        for bad in (0, 31, "x"):
            with self.assertRaises(ValueError):
                server.normalize_rule({"freq": "daily", "interval": bad})

    def test_rejects_empty_or_out_of_range_weekdays(self):
        for bad in ([], [7], [-1], ["1"]):
            with self.assertRaises(ValueError):
                server.normalize_rule({"freq": "weekly", "weekdays": bad})

    def test_rejects_out_of_range_month_days(self):
        for bad in ([0], [32], []):
            with self.assertRaises(ValueError):
                server.normalize_rule({"freq": "monthly", "month_days": bad})

    def test_rejects_until_before_anchor(self):
        with self.assertRaises(ValueError):
            server.normalize_rule({"freq": "daily", "anchor": "2026-05-10",
                                   "until": "2026-05-09"})


class DefaultRuleTests(unittest.TestCase):
    def test_anchors_land_on_the_current_period_start(self):
        today = date(2026, 10, 8)  # a Thursday
        self.assertEqual(server.default_rule_for_cycle("daily", today)["anchor"],
                         today.isoformat())
        weekly = server.default_rule_for_cycle("weekly", today)
        self.assertEqual(weekly["anchor"], "2026-10-05")  # Monday
        self.assertEqual(json.loads(weekly["weekdays"]), [0])
        monthly = server.default_rule_for_cycle("monthly", today)
        self.assertEqual(monthly["anchor"], "2026-10-01")
        self.assertEqual(json.loads(monthly["month_days"]), [1])
        self.assertEqual(server.default_rule_for_cycle("yearly", today)["anchor"],
                         "2026-01-01")


class DbTestCase(unittest.TestCase):
    """Points the module's DB globals at a throwaway sqlite file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (server.DATA_DIR, server.DB_FILE, server.LEGACY_STATE)
        server.DATA_DIR = Path(self._tmp.name)
        server.DB_FILE = server.DATA_DIR / "chore.db"
        server.LEGACY_STATE = server.DATA_DIR / "state.json"
        server.init_db()

    def tearDown(self):
        server.DATA_DIR, server.DB_FILE, server.LEGACY_STATE = self._saved
        self._tmp.cleanup()


class ChoreApiTests(DbTestCase):
    def test_add_update_reorder_retire(self):
        first = server.create_chore("Kitchen", "Wipe counters",
                                    {"freq": "daily", "anchor": "2026-10-08"})
        second = server.create_chore("Pool", "Skim",
                                     {"freq": "weekly", "weekdays": [0],
                                      "anchor": "2026-10-05"})
        self.assertIsInstance(first, int)
        self.assertTrue(server.update_chore(second, {"text": "Skim the pool"}))
        self.assertTrue(server.reorder_chores([second, first]))
        with closing(server.connect()) as conn:
            order = [r["id"] for r in conn.execute(
                "SELECT id FROM chores WHERE retired_at IS NULL ORDER BY position")]
        self.assertEqual(order, [second, first])
        self.assertTrue(server.retire_chore(first))
        self.assertFalse(server.retire_chore(first))  # already retired

    def test_update_rejects_invalid_and_reports_unknown(self):
        cid = server.create_chore("A", "a", {"freq": "daily", "anchor": "2026-10-08"})
        self.assertFalse(server.update_chore(999, {"text": "nope"}))
        with self.assertRaises(ValueError):
            server.update_chore(cid, {"text": "   "})
        with self.assertRaises(ValueError):
            server.update_chore(cid, {})  # nothing to change

    def test_reorder_rejects_a_stale_or_duplicated_list(self):
        a = server.create_chore("A", "a", {"freq": "daily", "anchor": "2026-10-08"})
        server.create_chore("B", "b", {"freq": "daily", "anchor": "2026-10-08"})
        with self.assertRaises(ValueError):
            server.reorder_chores([a])
        with self.assertRaises(ValueError):
            server.reorder_chores([a, a])

    def test_toggle_only_creates_completions_on_due_dates(self):
        monday = date(2026, 10, 5)
        cid = server.create_chore("Pool", "Skim",
                                  {"freq": "weekly", "weekdays": [0],
                                   "anchor": monday.isoformat()})
        self.assertTrue(server.toggle_chore(cid, True, monday))    # due
        tuesday = monday + timedelta(days=1)
        with self.assertRaises(ValueError):                        # not due
            server.toggle_chore(cid, True, tuesday)
        with closing(server.connect()) as conn:
            rows = conn.execute("SELECT period_start FROM completions").fetchall()
        self.assertEqual([r["period_start"] for r in rows], [monday.isoformat()])
        self.assertTrue(server.toggle_chore(cid, False, tuesday))  # clearing ok

    def test_toggle_unknown_chore_returns_false(self):
        self.assertFalse(server.toggle_chore(999, True, d("2026-10-08")))

    def test_calendar_today_override_drives_overdue(self):
        today = date.today()
        server.create_chore("Kitchen", "Wipe",
                            {"freq": "daily", "anchor": today.isoformat()})
        on_time = server.build_calendar(today, today, today)
        self.assertFalse(on_time["dates"][today.isoformat()][0]["overdue"])
        ahead = server.build_calendar(today, today, today + timedelta(days=1))
        self.assertTrue(ahead["dates"][today.isoformat()][0]["overdue"])


V1_SCHEMA = """
CREATE TABLE chores (
    id INTEGER PRIMARY KEY, area TEXT NOT NULL, text TEXT NOT NULL,
    position INTEGER NOT NULL, cycle TEXT NOT NULL,
    since TEXT, retired_at TEXT
);
CREATE TABLE completions (
    chore_id INTEGER NOT NULL, period_start TEXT NOT NULL,
    PRIMARY KEY (chore_id, period_start)
) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (server.DATA_DIR, server.DB_FILE, server.LEGACY_STATE)
        server.DATA_DIR = Path(self._tmp.name)
        server.DB_FILE = server.DATA_DIR / "chore.db"
        server.LEGACY_STATE = server.DATA_DIR / "state.json"
        today = date.today()
        self.monday = today - timedelta(days=today.weekday())
        conn = sqlite3.connect(server.DB_FILE)
        conn.executescript(V1_SCHEMA)
        conn.executemany(
            "INSERT INTO chores (area, text, position, cycle) VALUES (?, ?, ?, ?)",
            [("Kitchen", "Wipe counters", 0, "daily"),
             ("Pool", "Skim", 1, "weekly"),
             ("Lawn", "Mow", 2, "monthly"),
             ("Roof", "Clean gutters", 3, "yearly")],
        )
        conn.execute("INSERT INTO completions (chore_id, period_start) VALUES (2, ?)",
                     (self.monday.isoformat(),))
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        with redirect_stdout(io.StringIO()):  # the migration is chatty by design
            server.init_db()

    def tearDown(self):
        server.DATA_DIR, server.DB_FILE, server.LEGACY_STATE = self._saved
        self._tmp.cleanup()

    def test_schema_is_bumped_to_v2(self):
        with closing(server.connect()) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            cols = [r[1] for r in conn.execute("PRAGMA table_info(chores)")]
        self.assertEqual(version, 2)
        self.assertIn("freq", cols)
        self.assertIn("anchor", cols)
        # The old column is dropped on SQLite >= 3.35 and kept harmlessly below.

    def test_rules_are_derived_from_the_old_cycle(self):
        with closing(server.connect()) as conn:
            chores = {c["area"]: c for c in server.load_chores(conn)}
        today = date.today()
        self.assertEqual(chores["Kitchen"]["freq"], "daily")
        self.assertEqual(chores["Kitchen"]["anchor"], today.isoformat())
        self.assertEqual(chores["Pool"]["freq"], "weekly")
        self.assertEqual(chores["Pool"]["weekdays"], [0])
        self.assertEqual(chores["Pool"]["anchor"], self.monday.isoformat())
        self.assertEqual(chores["Lawn"]["freq"], "monthly")
        self.assertEqual(chores["Lawn"]["month_days"], [1])
        self.assertEqual(chores["Roof"]["freq"], "yearly")
        self.assertEqual(chores["Roof"]["anchor"],
                         today.replace(month=1, day=1).isoformat())

    def test_completions_are_preserved(self):
        with closing(server.connect()) as conn:
            rows = conn.execute(
                "SELECT chore_id, period_start FROM completions").fetchall()
        self.assertEqual([(r["chore_id"], r["period_start"]) for r in rows],
                         [(2, self.monday.isoformat())])


if __name__ == "__main__":
    unittest.main(verbosity=2)
