#!/usr/bin/env python3
"""
Tiny single-file backend for the household chore chart.

- Serves the static chart at /
- GET  /api/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD -> chores + per-date
  occurrence flags (done / overdue) for the requested range
- GET  /api/state  -> deprecated shim (derived from today's calendar)
- POST /api/toggle -> check/uncheck a single chore on a date
  {"id": 17, "done": true, "date": "2026-10-08"} (date defaults to today);
  flexible chores also accept {"period_key": "m:2026-11-01"}
- POST /api/reset  -> deprecated no-op shim (kept for old tabs)
- POST /api/chores -> add a chore {"area", "text", "anchor", "freq"?,
  "interval"?, "weekdays"?, "month_days"?, "until"?, "assignee"?,
  "high_priority"?, "tags"?, "due_mode"?, "flex_unit"?,
  "flex_interval"?, "flex_anchor"?}
- POST /api/chores/update  -> rename/reschedule {"id", "area"?, "text"?,
  "anchor"?, "freq"?, "interval"?, "weekdays"?, "month_days"?, "until"?,
  "assignee"?, "high_priority"?, "tags"?, "due_mode"?, "flex_unit"?,
  "flex_interval"?, "flex_anchor"?}
- POST /api/chores/reorder -> set display order {"ids": [...]}
- POST /api/chores/retire  -> soft-delete one chore {"id"} (history kept)
- POST /api/state  -> deprecated compat shim (old flat positional dict)

No dependencies beyond the Python standard library (sqlite3 is stdlib).
State lives in SQLite at /app/data/chore.db (WAL mode, one connection per
request, a transaction around every write), so it survives container
restarts and can't be torn by a crash mid-write like the old flat JSON
file could.

Chores carry a Tier-1 recurrence rule (once / daily / weekly / monthly /
yearly with interval, weekday or month-day selectors, an anchor first-due
date, and an optional until end date). Occurrences are computed on the fly
with pure date math -- no scheduler, no pre-created instances. Completions
are one row per chore per occurrence date; "done" means a row exists for
that date, and "overdue" means due, before today, and with no row. Past
rows are kept as history for future stats.

Schema v2 replaced the old v1 `cycle` column (daily/weekly/monthly/yearly
tabs) with the recurrence rule columns; the v1 calendar is gone and the
old tab UI has been replaced by the Day/Week/Month calendar view. A v1
database is migrated in place on boot (rules default sensibly, anchors
land on the current period so no overdue flood appears on first open).
Schema v3 adds lightweight chore metadata: a free-text assignee, a
high-priority flag (only high-priority overdue chores feed the UI's
banner), and a JSON tag list used for organizing/filtering. All three
default to "unset" for existing rows, so the v2 -> v3 migration is a
pure additive column add.
Schema v4 adds a second timing mode: flexible chores that are not tied
to a specific day. They live in a week/month/year window (or Anytime),
can repeat every N windows, and become overdue when the window's end
date passes without a completion. Their completions live in a separate
flex_completions table keyed by the window period, so dated occurrence
logic stays untouched.
A legacy /app/data/state.json (positional keys like "weekly:3") is
imported once on first boot and renamed to state.json.imported.
"""

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HTML_DIR = Path(os.environ.get("HTML_DIR", "/app/html"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))

DB_FILE = DATA_DIR / "chore.db"
LEGACY_STATE = DATA_DIR / "state.json"
SCHEMA_VERSION = 4
FREQS = ("once", "daily", "weekly", "monthly", "yearly")
FLEX_UNITS = ("week", "month", "year")
DUE_MODES = ("date", "flex")
# Keep the flexible shelf bounded even if a repeating chore goes untouched
# for a long time; the most recent missed windows are the relevant ones.
MAX_FLEX_LOOKBACK = 12
# Legacy v1 cycle names, kept only for the v1 -> v2 migration and the
# deprecated flat state.json import.
LEGACY_CYCLES = ("daily", "weekly", "monthly", "yearly")
MAX_AREA_LEN = 80
MAX_TEXT_LEN = 200
MAX_INTERVAL = 30
MAX_ASSIGNEE_LEN = 80
MAX_TAG_LEN = 30
MAX_TAGS = 12
CALENDAR_MAX_DAYS = 62

# Content types for the static UI assets served out of HTML_DIR
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def parse_day(value, name="date"):
    """Parse 'YYYY-MM-DD' to a date. Raises ValueError when invalid."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a YYYY-MM-DD string")
    try:
        year, month, day = (int(part) for part in value.split("-"))
        return date(year, month, day)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a YYYY-MM-DD string") from None


def month_length(year, month):
    """Days in a month (stdlib-only, no calendar import needed)."""
    if month == 12:
        return (date(year + 1, 1, 1) - date(year, 12, 1)).days
    return (date(year, month + 1, 1) - date(year, month, 1)).days


# ------------------------------------------------------- flexible periods
# Flexible chores are not tied to a day. Their occurrence is a whole
# week/month/year (or a single "Anytime" instance), with the period end
# acting as the derived due date. Weeks start on Sunday to match the UI.

def week_start(day):
    """Sunday that starts the week containing `day`."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def period_start(unit, day):
    """First day of the week/month/year containing `day`."""
    if unit == "week":
        return week_start(day)
    if unit == "month":
        return day.replace(day=1)
    if unit == "year":
        return day.replace(month=1, day=1)
    return None


def period_end(unit, start):
    """Last day of the period beginning on `start`."""
    if unit == "week":
        return start + timedelta(days=6)
    if unit == "month":
        return start.replace(day=month_length(start.year, start.month))
    if unit == "year":
        return date(start.year, 12, 31)
    return None


def add_periods(unit, start, count):
    """Move a period start by `count` whole periods."""
    if unit == "week":
        return start + timedelta(days=7 * count)
    if unit == "month":
        month_index = start.year * 12 + (start.month - 1) + count
        return date(month_index // 12, month_index % 12 + 1, 1)
    if unit == "year":
        return date(start.year + count, 1, 1)
    return None


def period_index(unit, anchor, start):
    """Whole periods between two period starts."""
    if unit == "week":
        return (start - anchor).days // 7
    if unit == "month":
        return (start.year - anchor.year) * 12 + (start.month - anchor.month)
    if unit == "year":
        return start.year - anchor.year
    return 0


def flex_period_key(unit, start):
    """Stable completion key for a flexible occurrence."""
    if unit is None:
        return "once"
    return f"{unit[0]}:{start.isoformat()}"


def expand_month_days(raw, year, month):
    """Resolve stored month_days to concrete days for a month.

    Stored values are 1..31 plus -1 meaning last day. Day 31 clamps
    to the month length (Feb 28/29), so "monthly on the 31st" still
    fires every month rather than skipping short ones.
    """
    length = month_length(year, month)
    out = set()
    for day in raw:
        if day == -1:
            out.add(length)
        elif 1 <= day <= 31:
            out.add(min(day, length))
    return out


def load_rule(row):
    """Build a recurrence rule dict from a chores DB row."""
    try:
        weekdays = json.loads(row["weekdays"]) if row["weekdays"] else []
    except (json.JSONDecodeError, TypeError):
        weekdays = []
    try:
        month_days = json.loads(row["month_days"]) if row["month_days"] else []
    except (json.JSONDecodeError, TypeError):
        month_days = []
    try:
        anchor = parse_day(row["anchor"]) if row["anchor"] else None
    except ValueError:
        anchor = None
    until = None
    if row["until"]:
        try:
            until = parse_day(row["until"])
        except ValueError:
            until = None
    return {
        "freq": row["freq"],
        "interval": row["interval"] or 1,
        "weekdays": [w for w in weekdays if isinstance(w, int) and 0 <= w <= 6],
        "month_days": [m for m in month_days if isinstance(m, int) and (-1 <= m <= 31 and m != 0)],
        "anchor": anchor,
        "until": until,
    }


def load_tags(row):
    """Parse a stored tags JSON column into a clean list of strings.

    Bad/legacy data degrades to an empty list rather than failing the read.
    """
    try:
        raw = json.loads(row["tags"]) if row["tags"] else []
    except (json.JSONDecodeError, TypeError):
        raw = []
    if not isinstance(raw, list):
        return []
    tags = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            tags.append(item.strip())
    return tags[:MAX_TAGS]


def due_on(rule, day):
    """True when a recurrence rule fires on `day` (pure date math).

    Tier-1 frequencies: once / daily every N days / weekly every N weeks
    on given weekdays / monthly every N months on given month-days
    (with clamping + last-day token) / yearly every N years on the
    anchor month/day (Feb 29 -> Feb 28 in non-leap years). Everything is
    gated by anchor (nothing due before it starts) and until (optional
    end date, inclusive).
    """
    freq = rule.get("freq")
    anchor = rule.get("anchor")
    until = rule.get("until")
    interval = rule.get("interval") or 1
    if anchor is None or day < anchor:
        return False
    if until is not None and day > until:
        return False
    if freq == "once":
        return day == anchor
    if freq == "daily":
        return (day - anchor).days % max(1, interval) == 0
    if freq == "weekly":
        weekdays = rule.get("weekdays") or [anchor.weekday()]
        if day.weekday() not in weekdays:
            return False
        anchor_monday = anchor - timedelta(days=anchor.weekday())
        day_monday = day - timedelta(days=day.weekday())
        weeks = (day_monday - anchor_monday).days // 7
        return weeks % max(1, interval) == 0
    if freq == "monthly":
        month_days = rule.get("month_days") or [anchor.day]
        if day.day not in expand_month_days(month_days, day.year, day.month):
            return False
        months = (day.year - anchor.year) * 12 + (day.month - anchor.month)
        return months % max(1, interval) == 0
    if freq == "yearly":
        years = day.year - anchor.year
        if years % max(1, interval) != 0:
            return False
        if anchor.month == 2 and anchor.day == 29:
            want = 29 if (day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0)) else 28
            return day.month == 2 and day.day == want
        return day.month == anchor.month and day.day == anchor.day
    return False


def next_due(rule, start):
    """First occurrence on or after `start`, or None when nothing is upcoming.

    Bounded scan: daily is computed directly, weekly steps days for one
    interval + a week, monthly steps months for one interval + a month, and
    yearly steps years for one interval + a year. A window of one full
    interval always contains a firing, so the extra step only guards the
    anchor/selector alignment.
    """
    freq = rule.get("freq")
    anchor = rule.get("anchor")
    until = rule.get("until")
    if anchor is None:
        return None
    if freq == "once":
        return anchor if anchor >= start else None
    if until is not None and start > until:
        return None
    interval = max(1, rule.get("interval") or 1)
    day = max(start, anchor)
    if freq == "daily":
        remainder = (day - anchor).days % interval
        if remainder:
            day += timedelta(days=interval - remainder)
        return day if until is None or day <= until else None
    if freq == "weekly":
        horizon = day + timedelta(days=interval * 7 + 7)
        while day <= horizon and (until is None or day <= until):
            if due_on(rule, day):
                return day
            day += timedelta(days=1)
        return None
    if freq == "monthly":
        year, month = day.year, day.month
        for _ in range(interval + 2):
            for cand in sorted(
                expand_month_days(rule.get("month_days") or [anchor.day],
                                  year, month)
            ):
                try:
                    candidate = date(year, month, cand)
                except ValueError:
                    continue
                if candidate < day:
                    continue
                if until is not None and candidate > until:
                    continue
                if due_on(rule, candidate):
                    return candidate
            month += 1
            if month > 12:
                month, year = 1, year + 1
        return None
    if freq == "yearly":
        for step in range(interval + 2):
            year = day.year + step
            if anchor.month == 2 and anchor.day == 29:
                cand = 29 if (year % 4 == 0 and
                              (year % 100 != 0 or year % 400 == 0)) else 28
            else:
                cand = anchor.day
            try:
                candidate = date(year, anchor.month, cand)
            except ValueError:
                continue
            if candidate < day:
                continue
            if until is not None and candidate > until:
                continue
            if due_on(rule, candidate):
                return candidate
        return None
    return None


def load_flex(row):
    """Read the flexible timing columns into a normalized dict."""
    due_mode = row["due_mode"] if row["due_mode"] in DUE_MODES else "date"
    unit = row["flex_unit"] if row["flex_unit"] in FLEX_UNITS else None
    try:
        interval = int(row["flex_interval"] or 0)
    except (TypeError, ValueError):
        interval = 0
    interval = max(0, min(MAX_INTERVAL, interval))
    if due_mode != "flex":
        return {"due_mode": "date", "flex_unit": None, "flex_interval": 0,
                "flex_anchor": None, "until": None}
    if unit is None:
        interval = 0
    anchor = None
    if row["flex_anchor"]:
        try:
            anchor = period_start(unit, parse_day(row["flex_anchor"]))
        except ValueError:
            anchor = None
    if unit is not None and anchor is None:
        anchor = period_start(unit, date.today())
    until = None
    if row["until"]:
        try:
            until = parse_day(row["until"])
        except ValueError:
            until = None
    return {
        "due_mode": due_mode,
        "flex_unit": unit,
        "flex_interval": interval,
        "flex_anchor": anchor,
        "until": until,
    }


def flex_occurrence(chore, start, today, done_keys, current):
    """One flexible occurrence (a whole period, or a single Anytime item)."""
    unit = chore["flex_unit"]
    key = flex_period_key(unit, start)
    due = period_end(unit, start) if unit is not None else None
    is_done = (chore["id"], key) in done_keys
    return {
        "id": chore["id"],
        "period_key": key,
        "period_start": start.isoformat() if start else None,
        "due": due.isoformat() if due else None,
        "done": is_done,
        "overdue": bool(due is not None and not is_done and due < today),
        "current": current,
        "high_priority": chore["high_priority"],
    }


def flex_occurrences(chore, today, done_keys):
    """Current and still-missed flexible occurrences for a chore.

    Repeating chores expose the current period (even when already done, so
    the checkbox can show its state) plus recent missed periods. Completed
    past periods are omitted. A non-repeating windowed chore exposes its one
    period until done, and Anytime chores expose one undated instance.
    """
    if chore["due_mode"] != "flex":
        return []
    unit = chore["flex_unit"]
    if unit is None:
        return [flex_occurrence(chore, None, today, done_keys, True)]

    anchor = chore.get("_flex_anchor", chore.get("flex_anchor"))
    if isinstance(anchor, str):
        anchor = parse_day(anchor)
    anchor = anchor or period_start(unit, today)
    interval = max(0, chore["flex_interval"] or 0)
    until = chore.get("_flex_until", chore.get("until"))
    if isinstance(until, str):
        until = parse_day(until)
    if until is not None and anchor > until:
        return []

    if interval == 0:
        current_start = period_start(unit, today)
        if anchor > current_start:
            return []
        occurrence = flex_occurrence(
            chore, anchor, today, done_keys,
            current=current_start == anchor,
        )
        return [occurrence]

    current_start = period_start(unit, today)
    if current_start < anchor:
        return []
    index = period_index(unit, anchor, current_start)
    if index < 0:
        return []
    latest_active = (index // interval) * interval
    out = []

    if latest_active == index:
        occurrence = flex_occurrence(
            chore, add_periods(unit, anchor, index), today, done_keys,
            current=True,
        )
        if until is None or occurrence["period_start"] <= until.isoformat():
            out.append(occurrence)

    scan = latest_active - interval if latest_active == index else latest_active
    scanned = 0
    while scan >= 0 and scanned < MAX_FLEX_LOOKBACK:
        start = add_periods(unit, anchor, scan)
        if until is not None and start > until:
            break
        occurrence = flex_occurrence(chore, start, today, done_keys, False)
        if not occurrence["done"]:
            out.append(occurrence)
        scan -= interval
        scanned += 1

    out.sort(key=lambda item: (item["due"] or "", item["period_key"]))
    return out


def next_flex_due(chore, today):
    """Due date of the current or next active flexible occurrence."""
    unit = chore["flex_unit"]
    if unit is None:
        return None
    anchor = chore.get("_flex_anchor", chore.get("flex_anchor"))
    if isinstance(anchor, str):
        anchor = parse_day(anchor)
    anchor = anchor or period_start(unit, today)
    interval = max(0, chore["flex_interval"] or 0)
    until = chore.get("_flex_until", chore.get("until"))
    if isinstance(until, str):
        until = parse_day(until)
    if interval == 0:
        due = period_end(unit, anchor)
    else:
        current_start = period_start(unit, today)
        if current_start < anchor:
            due = period_end(unit, anchor)
        else:
            index = period_index(unit, anchor, current_start)
            active = (index // interval) * interval
            if active == index:
                due = period_end(unit, current_start)
            else:
                due = period_end(unit, add_periods(unit, anchor,
                                                    active + interval))
    if until is not None and due > until:
        return None
    return due


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # per-connection setting
    return conn


def create_schema(conn):
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chores (
            id         INTEGER PRIMARY KEY,
            area       TEXT    NOT NULL,
            text       TEXT    NOT NULL,
            position   INTEGER NOT NULL,
            freq       TEXT    NOT NULL DEFAULT 'weekly'
                       CHECK (freq IN ('once','daily','weekly','monthly','yearly')),
            interval   INTEGER NOT NULL DEFAULT 1,
            weekdays   TEXT,            -- JSON [0..6] Mon=0, for weekly
            month_days TEXT,            -- JSON [1..31, -1=last], for monthly
            anchor     TEXT    NOT NULL DEFAULT '',  -- 'YYYY-MM-DD' first due
            until      TEXT,            -- 'YYYY-MM-DD' optional end, NULL = forever
            since      TEXT,          -- 'YYYY-MM-DD' added-date; NULL = since forever
            assignee   TEXT,          -- free-text person name; NULL = unassigned
            high_priority INTEGER NOT NULL DEFAULT 0
                       CHECK (high_priority IN (0, 1)),
            tags       TEXT,          -- JSON list of free-form tags; NULL = none
            due_mode   TEXT    NOT NULL DEFAULT 'date'
                       CHECK (due_mode IN ('date', 'flex')),
            flex_unit  TEXT    CHECK (flex_unit IS NULL OR
                       flex_unit IN ('week', 'month', 'year')),
            flex_interval INTEGER NOT NULL DEFAULT 0,
            flex_anchor TEXT,          -- first period start for flexible chores
            retired_at TEXT           -- soft-delete, reserved for UI chore editing
        );
        CREATE TABLE IF NOT EXISTS completions (
            chore_id     INTEGER NOT NULL REFERENCES chores(id) ON DELETE CASCADE,
            period_start TEXT    NOT NULL,   -- occurrence date 'YYYY-MM-DD'
            PRIMARY KEY (chore_id, period_start)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS flex_completions (
            chore_id   INTEGER NOT NULL REFERENCES chores(id) ON DELETE CASCADE,
            period_key TEXT    NOT NULL,  -- 'once' | 'w:...' | 'm:...' | 'y:...'
            PRIMARY KEY (chore_id, period_key)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def migrate_v1_to_v2(conn):
    """In-place v1 (cycle column) -> v2 (recurrence rule columns).

    Rules default sensibly and anchors land on the current period start so
    no overdue flood appears on first open after upgrade:
    - daily: anchor today.
    - weekly: anchor the Monday of this week, weekday = that Monday.
    - monthly: 1st of this month, month_days = [1].
    - yearly: Jan 1 this year.

    Completion rows need no remapping: in v1 the current period's start
    date equals the anchor chosen above (day / Monday / 1st / Jan 1), so
    checkmarks made so far keep counting as done today. Older rows are
    left untouched as history.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(chores)").fetchall()]
    if "freq" in cols:
        return  # already v2
    today = date.today()
    conn.executescript(
        """
        ALTER TABLE chores ADD COLUMN freq TEXT NOT NULL DEFAULT 'weekly'
          CHECK (freq IN ('once','daily','weekly','monthly','yearly'));
        ALTER TABLE chores ADD COLUMN interval INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE chores ADD COLUMN weekdays TEXT;
        ALTER TABLE chores ADD COLUMN month_days TEXT;
        ALTER TABLE chores ADD COLUMN anchor TEXT NOT NULL DEFAULT '';
        ALTER TABLE chores ADD COLUMN until TEXT;
        """
    )
    for row in conn.execute("SELECT id, cycle FROM chores").fetchall():
        rule = default_rule_for_cycle(row["cycle"], today)
        conn.execute(
            "UPDATE chores SET freq=?, interval=?, weekdays=?, month_days=?,"
            " anchor=? WHERE id=?",
            (rule["freq"], rule["interval"], rule["weekdays"],
             rule["month_days"], rule["anchor"], row["id"]),
        )
    # Drop the old cycle column if SQLite supports it (3.35+); harmless
    # to keep it on older builds.
    try:
        conn.execute("ALTER TABLE chores DROP COLUMN cycle")
    except sqlite3.Error:
        pass
    print("migrated database v1 (cycles) -> v2 (recurrence rules)")


def migrate_v2_to_v3(conn):
    """In-place v2 -> v3: add the chore metadata columns.

    Everything is additive with safe defaults, so existing chores simply
    become unassigned, normal priority, and untagged. The `assignee` and
    `tags` columns are nullable; the priority flag defaults to 0.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chores)").fetchall()}
    added = False
    if "assignee" not in cols:
        conn.execute("ALTER TABLE chores ADD COLUMN assignee TEXT")
        added = True
    if "high_priority" not in cols:
        conn.execute(
            "ALTER TABLE chores ADD COLUMN high_priority INTEGER NOT NULL"
            " DEFAULT 0 CHECK (high_priority IN (0, 1))"
        )
        added = True
    if "tags" not in cols:
        conn.execute("ALTER TABLE chores ADD COLUMN tags TEXT")
        added = True
    if added:
        print("migrated database v2 -> v3 (assignee, high priority, tags)")


def migrate_v3_to_v4(conn):
    """In-place v3 -> v4: flexible timing columns + flex completions table.

    Existing chores default to due_mode='date', so their dated behaviour is
    unchanged. Anytime/flexible chores start with no unit, zero interval,
    and no anchor.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chores)").fetchall()}
    added = False
    if "due_mode" not in cols:
        conn.execute(
            "ALTER TABLE chores ADD COLUMN due_mode TEXT NOT NULL DEFAULT 'date'"
            " CHECK (due_mode IN ('date', 'flex'))"
        )
        added = True
    if "flex_unit" not in cols:
        conn.execute(
            "ALTER TABLE chores ADD COLUMN flex_unit TEXT"
            " CHECK (flex_unit IS NULL OR flex_unit IN ('week', 'month', 'year'))"
        )
        added = True
    if "flex_interval" not in cols:
        conn.execute(
            "ALTER TABLE chores ADD COLUMN flex_interval INTEGER NOT NULL"
            " DEFAULT 0"
        )
        added = True
    if "flex_anchor" not in cols:
        conn.execute("ALTER TABLE chores ADD COLUMN flex_anchor TEXT")
        added = True
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flex_completions (
            chore_id   INTEGER NOT NULL REFERENCES chores(id) ON DELETE CASCADE,
            period_key TEXT    NOT NULL,
            PRIMARY KEY (chore_id, period_key)
        ) WITHOUT ROWID
        """
    )
    if added:
        print("migrated database v3 -> v4 (flexible chores)")


def default_rule_for_cycle(cycle, today):
    """DB-ready recurrence rule for a legacy v1 cycle (v1 -> v2 migration).

    Returns DB column values: weekdays/month_days as JSON strings or None,
    anchor as 'YYYY-MM-DD'. Anchors land on the current period start so a
    migrated install shows these chores due now and never overdue.
    """
    if cycle == "daily":
        anchor = today
        weekdays = month_days = None
        freq = "daily"
    elif cycle == "weekly":
        anchor = today - timedelta(days=today.weekday())  # Monday
        weekdays = json.dumps([anchor.weekday()])
        month_days = None
        freq = "weekly"
    elif cycle == "monthly":
        anchor = today.replace(day=1)
        weekdays = None
        month_days = json.dumps([1])
        freq = "monthly"
    else:  # yearly
        anchor = today.replace(month=1, day=1)
        weekdays = month_days = None
        freq = "yearly"
    return {"freq": freq, "interval": 1, "weekdays": weekdays,
            "month_days": month_days, "anchor": anchor.isoformat(),
            "until": None}


def normalize_rule(data):
    """Validate a recurrence rule from the API/seed. Returns normalized dict.

    Raises ValueError on bad input. Normalized form: freq, interval int,
    weekdays/month_days as sorted JSON strings (or None), anchor/until as
    'YYYY-MM-DD' strings (until may be None).
    """
    if not isinstance(data, dict):
        raise ValueError("recurrence rule must be an object")
    freq = data.get("freq", "weekly")
    if freq not in FREQS:
        raise ValueError(f"freq must be one of {', '.join(FREQS)}")
    try:
        interval = int(data.get("interval", 1))
    except (TypeError, ValueError):
        raise ValueError("interval must be an integer 1..30")
    if not 1 <= interval <= MAX_INTERVAL:
        raise ValueError("interval must be an integer 1..30")
    anchor_raw = data.get("anchor")
    if anchor_raw is None:
        anchor = date.today()
    else:
        anchor = parse_day(anchor_raw, name="anchor")
    until = None
    if data.get("until") not in (None, ""):
        until = parse_day(data.get("until"), name="until")
        if until < anchor:
            raise ValueError("until must not be before anchor")
    weekdays = None
    if freq == "weekly":
        raw = data.get("weekdays")
        if raw is None:
            raw = [anchor.weekday()]
        if not isinstance(raw, list) or not raw:
            raise ValueError("weekdays must be a non-empty list of 0..6")
        clean = set()
        for item in raw:
            if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 6:
                raise ValueError("weekdays must be a non-empty list of 0..6")
            clean.add(item)
        weekdays = json.dumps(sorted(clean))
    month_days = None
    if freq == "monthly":
        raw = data.get("month_days")
        if raw is None:
            raw = [anchor.day]
        if not isinstance(raw, list) or not raw:
            raise ValueError("month_days must be a non-empty list of 1..31 or -1")
        clean = set()
        for item in raw:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError("month_days must be a non-empty list of 1..31 or -1")
            if item == -1 or 1 <= item <= 31:
                clean.add(item)
            else:
                raise ValueError("month_days must be a non-empty list of 1..31 or -1")
        month_days = json.dumps(sorted(clean))
    return {
        "freq": freq,
        "interval": interval,
        "weekdays": weekdays,
        "month_days": month_days,
        "anchor": anchor.isoformat(),
        "until": until.isoformat() if until else None,
    }


def import_legacy_state(conn):
    """One-time import of the old positional state.json (kept as .imported)."""
    if conn.execute("SELECT 1 FROM meta WHERE key = 'imported'").fetchone():
        return
    if not LEGACY_STATE.exists():
        return  # check again next boot, in case a state.json shows up later
    try:
        state = json.loads(LEGACY_STATE.read_text() or "{}")
        if not isinstance(state, dict):
            raise ValueError("not a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"legacy {LEGACY_STATE.name} unreadable ({exc}); leaving it in place")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('imported', 'unreadable')"
        )
        return

    imported = skipped = 0
    today = date.today()
    # Legacy keys arrive in seed order: daily:0..9, weekly:0..19,
    # monthly:0..16, yearly:0..18. Map positionally onto the chores'
    # new recurrence rules so old checkmarks land on
    # today's occurrence date instead of a stale cycle period.
    order = [r["id"] for r in conn.execute(
        "SELECT id FROM chores ORDER BY position").fetchall()]
    legacy_counts = {"daily": 10, "weekly": 20, "monthly": 17, "yearly": 19}
    for key, value in state.items():
        cycle, sep, idx_text = key.partition(":")
        row = None
        if sep and cycle in LEGACY_CYCLES:
            try:
                position = int(idx_text)
            except ValueError:
                position = -1
            offset = sum(legacy_counts[c] for c in LEGACY_CYCLES
                         if LEGACY_CYCLES.index(c) < LEGACY_CYCLES.index(cycle))
            if 0 <= position < legacy_counts.get(cycle, 0):
                target = offset + position
                if target < len(order):
                    row = {"id": order[target]}
        if row is None:
            if value:
                print(f"legacy import: skipping unmappable key {key!r}")
                skipped += 1
            continue
        if value:
            conn.execute(
                "INSERT OR IGNORE INTO completions (chore_id, period_start)"
                " VALUES (?, ?)",
                (row["id"], today.isoformat()),
            )
            imported += 1
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('imported', ?)",
        (date.today().isoformat(),),
    )
    LEGACY_STATE.rename(LEGACY_STATE.with_name(LEGACY_STATE.name + ".imported"))
    print(f"legacy state.json imported: {imported} checked, {skipped} skipped")


def init_db():
    with closing(connect()) as conn, conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{version} is newer than this server "
                f"(v{SCHEMA_VERSION})"
            )
        if version == 0:
            # Fresh database (or a pre-versioned v1): plain schema create.
            # The migration helpers are idempotent and also patch up a
            # pre-versioned v1 table that CREATE IF NOT EXISTS left alone.
            create_schema(conn)
            migrate_v1_to_v2(conn)
            migrate_v2_to_v3(conn)
            migrate_v3_to_v4(conn)
        else:
            if version == 1:
                # v1 database in the wild: migrate in place through v4.
                migrate_v1_to_v2(conn)
                version = 2
            if version == 2:
                migrate_v2_to_v3(conn)
                version = 3
            if version == 3:
                migrate_v3_to_v4(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        import_legacy_state(conn)


def load_chores(conn):
    """All active chores as dicts with parsed recurrence/timing rules."""
    rows = conn.execute(
        "SELECT id, area, text, position, freq, interval, weekdays,"
        " month_days, anchor, until, assignee, high_priority, tags,"
        " due_mode, flex_unit, flex_interval, flex_anchor FROM chores"
        " WHERE retired_at IS NULL ORDER BY position"
    ).fetchall()
    chores = []
    today = date.today()
    for row in rows:
        rule = load_rule(row)
        timing = load_flex(row)
        if timing["due_mode"] == "flex":
            upcoming = next_flex_due(timing, today)
        else:
            upcoming = next_due(rule, today)
        chores.append({
            "id": row["id"],
            "area": row["area"],
            "text": row["text"],
            "position": row["position"],
            "freq": rule["freq"],
            "interval": rule["interval"],
            "weekdays": rule["weekdays"],
            "month_days": rule["month_days"],
            "anchor": rule["anchor"].isoformat() if rule["anchor"] else None,
            "until": rule["until"].isoformat() if rule["until"] else None,
            "assignee": row["assignee"] or None,
            "high_priority": bool(row["high_priority"]),
            "tags": load_tags(row),
            "due_mode": timing["due_mode"],
            "flex_unit": timing["flex_unit"],
            "flex_interval": timing["flex_interval"],
            "flex_anchor": (timing["flex_anchor"].isoformat()
                            if timing["flex_anchor"] else None),
            "next_due": upcoming.isoformat() if upcoming else None,
            "_rule": rule,
            "_flex_anchor": timing["flex_anchor"],
            "_flex_until": timing["until"],
        })
    return chores


def build_calendar(start, end, today=None):
    """Response for GET /api/calendar: chores + per-date occurrence flags.

    `start`/`end` are dates (inclusive, at most CALENDAR_MAX_DAYS apart).
    Each date maps to [{id, done, overdue}]; overdue is derived (due,
    before today, no completion row) so it never spills into another
    day's list -- it only tints the calendar cell. Flexible chores are
    returned separately in `flexible`, since they belong to a whole
    week/month/year instead of a specific day; their period end is the
    derived due date.

    `today` overrides the server's own date, so the household's local day
    (which can differ near midnight, or from a UTC container) drives the
    overdue boundary. Defaults to the server date when not supplied.
    """
    today = today or date.today()
    with closing(connect()) as conn:
        chores = load_chores(conn)
        done_rows = conn.execute(
            "SELECT chore_id, period_start FROM completions"
            " WHERE period_start BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        flex_done = {
            (r["chore_id"], r["period_key"])
            for r in conn.execute(
                "SELECT chore_id, period_key FROM flex_completions"
            ).fetchall()
        }
    done = {(r["chore_id"], r["period_start"]) for r in done_rows}
    dates = {}
    day = start
    while day <= end:
        key = day.isoformat()
        entries = []
        for chore in chores:
            if chore["due_mode"] != "date":
                continue
            if not due_on(chore["_rule"], day):
                continue
            is_done = (chore["id"], key) in done
            entries.append({
                "id": chore["id"],
                "done": is_done,
                "overdue": (day < today and not is_done),
                "high_priority": chore["high_priority"],
            })
        dates[key] = entries
        day += timedelta(days=1)
    flexible = []
    for chore in chores:
        if chore["due_mode"] == "flex":
            flexible.extend(flex_occurrences(chore, today, flex_done))
    flexible.sort(key=lambda item: (
        not item["overdue"],
        item["due"] or "9999-12-31",
        item["period_key"],
    ))
    payload_chores = []
    for chore in chores:
        item = {k: v for k, v in chore.items() if not k.startswith("_")}
        payload_chores.append(item)
    return {"from": start.isoformat(), "to": end.isoformat(),
            "today": today.isoformat(), "chores": payload_chores,
            "dates": dates, "flexible": flexible}


def build_state():
    """Deprecated shim for GET /api/state: today's calendar as old shape."""
    today = date.today()
    data = build_calendar(today, today)
    key = today.isoformat()
    done_ids = {e["id"] for e in data["dates"].get(key, []) if e["done"]}
    chores = []
    for chore in data["chores"]:
        chores.append({
            "id": chore["id"],
            "area": chore["area"],
            "text": chore["text"],
            "position": chore["position"],
            "freq": chore["freq"],
            "interval": chore["interval"],
            "weekdays": chore["weekdays"],
            "month_days": chore["month_days"],
            "anchor": chore["anchor"],
            "until": chore["until"],
            "assignee": chore["assignee"],
            "high_priority": chore["high_priority"],
            "tags": chore["tags"],
            "due_mode": chore["due_mode"],
            "flex_unit": chore["flex_unit"],
            "flex_interval": chore["flex_interval"],
            "flex_anchor": chore["flex_anchor"],
            "done": chore["id"] in done_ids,
        })
    return {"periods": {"daily": key}, "chores": chores, "today": key,
            "flexible": data.get("flexible", [])}


def toggle_chore(chore_id, done, day=None, period_key=None):
    """Check/uncheck a dated occurrence or a flexible period. False if unknown.

    Dated chores may only *create* a completion on a date the rule actually
    fires, so a stray POST can't leave orphan rows behind for the future
    history view. Flexible chores may only create a completion for a period
    that is currently active (current or a recent missed period). Clearing is
    always allowed and idempotent, so a checkmark can still be undone after a
    schedule change.
    """
    target = day or date.today()
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT id, freq, interval, weekdays, month_days, anchor, until,"
            " due_mode, flex_unit, flex_interval, flex_anchor, high_priority"
            " FROM chores WHERE id = ? AND retired_at IS NULL",
            (chore_id,),
        ).fetchone()
        if row is None:
            return False

        if row["due_mode"] == "flex":
            timing = load_flex(row)
            chore = {
                "id": row["id"],
                "high_priority": bool(row["high_priority"]),
                "due_mode": "flex",
                **timing,
            }
            done_keys = {
                (r["chore_id"], r["period_key"])
                for r in conn.execute(
                    "SELECT chore_id, period_key FROM flex_completions"
                    " WHERE chore_id = ?",
                    (chore_id,),
                ).fetchall()
            }
            if not period_key:
                current = next(
                    (o for o in flex_occurrences(chore, target, done_keys)
                     if o["current"]),
                    None,
                )
                if current is None:
                    raise ValueError(
                        "period_key is required for a flexible chore"
                    )
                period_key = current["period_key"]
            if not isinstance(period_key, str):
                raise ValueError("period_key must be a string")
            if done:
                allowed = {
                    o["period_key"]
                    for o in flex_occurrences(chore, target, done_keys)
                }
                if period_key not in allowed:
                    raise ValueError("flexible occurrence is no longer active")
                conn.execute(
                    "INSERT OR IGNORE INTO flex_completions"
                    " (chore_id, period_key) VALUES (?, ?)",
                    (chore_id, period_key),
                )
            else:
                conn.execute(
                    "DELETE FROM flex_completions"
                    " WHERE chore_id = ? AND period_key = ?",
                    (chore_id, period_key),
                )
            return True

        stamp = target.isoformat()
        if done:
            already = conn.execute(
                "SELECT 1 FROM completions WHERE chore_id = ? AND period_start = ?",
                (chore_id, stamp),
            ).fetchone()
            if already is None and not due_on(load_rule(row), target):
                raise ValueError("chore is not due on that date")
            conn.execute(
                "INSERT OR IGNORE INTO completions (chore_id, period_start)"
                " VALUES (?, ?)",
                (chore_id, stamp),
            )
        else:
            conn.execute(
                "DELETE FROM completions WHERE chore_id = ? AND period_start = ?",
                (chore_id, stamp),
            )
    return True


def reset_cycle(cycle):
    """Deprecated no-op shim: cycles no longer exist; always True."""
    return True


def clean_field(value, name, max_len):
    """Trimmed, bounded non-empty string. Raises ValueError when invalid."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be empty")
    if len(cleaned) > max_len:
        raise ValueError(f"{name} must be at most {max_len} characters")
    return cleaned


def normalize_assignee(value):
    """Trim an optional free-text assignee. None/blank -> None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("assignee must be a string")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_ASSIGNEE_LEN:
        raise ValueError(f"assignee must be at most {MAX_ASSIGNEE_LEN} characters")
    return cleaned


def normalize_high_priority(value):
    """Normalize the high-priority flag to SQLite's 0/1."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError("high_priority must be a boolean")


def normalize_tags(value):
    """Validate a tag list, returning a JSON string (or None when empty).

    Tags are trimmed, de-duplicated case-insensitively (first spelling
    wins), and capped in both length and count.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("tags must be a list of strings")
    clean = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("tags must be a list of strings")
        tag = item.strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LEN:
            raise ValueError(f"tags must be at most {MAX_TAG_LEN} characters")
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        clean.append(tag)
    if len(clean) > MAX_TAGS:
        raise ValueError(f"a chore can have at most {MAX_TAGS} tags")
    return json.dumps(clean) if clean else None


def normalize_due_mode(value):
    """'date' or 'flex'; missing defaults to the existing dated mode."""
    if value in (None, ""):
        return "date"
    if value not in DUE_MODES:
        raise ValueError("due_mode must be 'date' or 'flex'")
    return value


def normalize_flex_unit(value):
    """Week/month/year window, or None for Anytime."""
    if value in (None, "", "anytime"):
        return None
    if value not in FLEX_UNITS:
        raise ValueError("flex_unit must be week, month, year, or null")
    return value


def normalize_flex_interval(value):
    """0 = doesn't repeat; 1..30 = every N windows."""
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise ValueError("flex_interval must be an integer 0..30")
    try:
        interval = int(value)
    except (TypeError, ValueError):
        raise ValueError("flex_interval must be an integer 0..30") from None
    if not 0 <= interval <= MAX_INTERVAL:
        raise ValueError("flex_interval must be an integer 0..30")
    return interval


def normalize_timing(due_mode="date", flex_unit=None, flex_interval=0,
                     flex_anchor=None):
    """Normalize a chore's timing mode into DB-ready values."""
    mode = normalize_due_mode(due_mode)
    if mode == "date":
        return {
            "due_mode": "date",
            "flex_unit": None,
            "flex_interval": 0,
            "flex_anchor": None,
        }
    unit = normalize_flex_unit(flex_unit)
    interval = normalize_flex_interval(flex_interval)
    if unit is None:
        interval = 0
        anchor = None
    else:
        if flex_anchor in (None, ""):
            anchor = period_start(unit, date.today())
        else:
            anchor = period_start(unit, parse_day(flex_anchor, "flex_anchor"))
    return {
        "due_mode": "flex",
        "flex_unit": unit,
        "flex_interval": interval,
        "flex_anchor": anchor.isoformat() if anchor else None,
    }


def create_chore(area, text, rule=None, assignee=None, high_priority=False,
                 tags=None, due_mode="date", flex_unit=None,
                 flex_interval=0, flex_anchor=None):
    """Append a chore with a recurrence rule. Returns the new id."""
    area = clean_field(area, "area", MAX_AREA_LEN)
    text = clean_field(text, "text", MAX_TEXT_LEN)
    norm = normalize_rule(rule or {})
    assignee = normalize_assignee(assignee)
    priority = normalize_high_priority(high_priority)
    tags_json = normalize_tags(tags)
    timing = normalize_timing(due_mode, flex_unit, flex_interval, flex_anchor)
    with closing(connect()) as conn, conn:
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM chores",
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO chores (area, text, position, freq, interval,"
            " weekdays, month_days, anchor, until, assignee, high_priority,"
            " tags, due_mode, flex_unit, flex_interval, flex_anchor)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (area, text, position, norm["freq"], norm["interval"],
             norm["weekdays"], norm["month_days"], norm["anchor"],
             norm["until"], assignee, priority, tags_json, timing["due_mode"],
             timing["flex_unit"], timing["flex_interval"],
             timing["flex_anchor"]),
        )
        return cur.lastrowid


def update_chore(chore_id, fields):
    """Apply area/text/recurrence/metadata changes to one active chore.

    False for an unknown (or retired) id; ValueError on invalid input.
    Recurrence keys (freq, interval, weekdays, month_days, anchor, until)
    are validated together so partial updates can't leave a broken rule.
    Metadata keys (assignee, high_priority, tags) are optional and may be
    cleared with null / false / []. Timing keys switch between dated and
    flexible modes; switching to date clears the flexible fields.
    """
    updates = {}
    if "area" in fields:
        updates["area"] = clean_field(fields["area"], "area", MAX_AREA_LEN)
    if "text" in fields:
        updates["text"] = clean_field(fields["text"], "text", MAX_TEXT_LEN)
    if "assignee" in fields:
        updates["assignee"] = normalize_assignee(fields["assignee"])
    if "high_priority" in fields:
        updates["high_priority"] = normalize_high_priority(fields["high_priority"])
    if "tags" in fields:
        updates["tags"] = normalize_tags(fields["tags"])
    rule_keys = ("freq", "interval", "weekdays", "month_days", "anchor",
                 "until")
    rule_update = {k: fields[k] for k in rule_keys if k in fields}
    timing_keys = ("due_mode", "flex_unit", "flex_interval", "flex_anchor")
    timing_update = {k: fields[k] for k in timing_keys if k in fields}
    if not updates and not rule_update and not timing_update:
        raise ValueError(
            "nothing to update (need area, text, schedule, timing, assignee,"
            " priority, or tags)"
        )
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT freq, interval, weekdays, month_days, anchor, until,"
            " due_mode, flex_unit, flex_interval, flex_anchor"
            " FROM chores WHERE id = ? AND retired_at IS NULL",
            (chore_id,),
        ).fetchone()
        if row is None:
            return False
        if rule_update:
            current = {
                "freq": row["freq"],
                "interval": row["interval"],
                "weekdays": json.loads(row["weekdays"]) if row["weekdays"] else None,
                "month_days": json.loads(row["month_days"]) if row["month_days"] else None,
                "anchor": row["anchor"],
                "until": row["until"],
            }
            current.update(rule_update)
            norm = normalize_rule(current)
            updates.update({
                "freq": norm["freq"],
                "interval": norm["interval"],
                "weekdays": norm["weekdays"],
                "month_days": norm["month_days"],
                "anchor": norm["anchor"],
                "until": norm["until"],
            })
        if timing_update:
            due_mode = fields.get("due_mode", row["due_mode"])
            flex_unit = fields.get("flex_unit", row["flex_unit"])
            flex_interval = fields.get("flex_interval", row["flex_interval"])
            flex_anchor = fields.get("flex_anchor", row["flex_anchor"])
            if ("flex_unit" in fields and "flex_anchor" not in fields
                    and fields["flex_unit"] != row["flex_unit"]):
                flex_anchor = None  # new window: start it in the current period
            timing = normalize_timing(
                due_mode, flex_unit, flex_interval, flex_anchor
            )
            updates.update(timing)
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE chores SET {assignments} WHERE id = ?",
            (*updates.values(), chore_id),
        )
    return True


def reorder_chores(ids):
    """Rewrite the global position order from an exact, current id list.

    Positions are global across all non-retired chores (one list, grouped
    for display by area on the client). A stale list (another device
    edited first) raises ValueError instead of scrambling the order.
    """
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids must be a non-empty list")
    try:
        ordered = [int(i) for i in ids]
    except (TypeError, ValueError):
        raise ValueError("ids must be integers") from None
    if len(set(ordered)) != len(ordered):
        raise ValueError("ids contains duplicates")
    with closing(connect()) as conn, conn:
        current = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM chores WHERE retired_at IS NULL"
                " ORDER BY position"
            )
        ]
        if sorted(ordered) != sorted(current):
            raise ValueError("ids do not match the active chores")
        conn.executemany(
            "UPDATE chores SET position = ? WHERE id = ?",
            list(enumerate(ordered)),
        )
    return True


def retire_chore(chore_id):
    """Soft-delete one chore; completions (history) are kept. False if unknown."""
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE chores SET retired_at = ? WHERE id = ? AND retired_at IS NULL",
            (date.today().isoformat(), chore_id),
        )
        return cur.rowcount > 0


def apply_flat_state(flat):
    """Deprecated POST /api/state: translate an old positional dict to rows.

    Keys like 'daily:3' map onto chore ids by position; values
    toggle today's occurrence (v1 could only ever represent 'this period').
    """
    applied = 0
    today = date.today().isoformat()
    legacy_counts = {"daily": 10, "weekly": 20, "monthly": 17, "yearly": 19}
    with closing(connect()) as conn, conn:
        order = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM chores ORDER BY position"
            ).fetchall()
        ]
        for key, value in flat.items():
            cycle, sep, idx_text = key.partition(":")
            if not sep or cycle not in LEGACY_CYCLES:
                continue  # not a positional key (e.g. 'periods' echoed back)
            try:
                position = int(idx_text)
            except ValueError:
                continue
            offset = sum(
                legacy_counts[c]
                for c in LEGACY_CYCLES
                if LEGACY_CYCLES.index(c) < LEGACY_CYCLES.index(cycle)
            )
            target = offset + position
            if not (0 <= position < legacy_counts.get(cycle, 0)
                    and target < len(order)):
                continue
            chore_id = order[target]
            if bool(value):
                conn.execute(
                    "INSERT OR IGNORE INTO completions (chore_id, period_start)"
                    " VALUES (?, ?)",
                    (chore_id, today),
                )
            else:
                conn.execute(
                    "DELETE FROM completions WHERE chore_id = ? AND period_start = ?",
                    (chore_id, today),
                )
            applied += 1
    return applied


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quieter logs -- default BaseHTTPRequestHandler logs every request to stderr
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b"{}"

    @staticmethod
    def _parse_json(raw):
        try:
            return json.loads(raw or b"{}"), None
        except json.JSONDecodeError:
            return None, "invalid JSON"

    def do_GET(self):
        if self.path.startswith("/api/calendar"):
            query = parse_qs(self.path.partition("?")[2])
            try:
                start = parse_day((query.get("from") or [None])[0] or
                                  date.today().isoformat(), "from")
                end = parse_day((query.get("to") or [None])[0] or
                                (start + timedelta(days=6)).isoformat(), "to")
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            if end < start:
                self._send_json({"error": "'to' must not precede 'from'"},
                                status=400)
                return
            if (end - start).days + 1 > CALENDAR_MAX_DAYS:
                self._send_json(
                    {"error": f"range exceeds {CALENDAR_MAX_DAYS} days"},
                    status=400,
                )
                return
            today = None
            if query.get("today"):
                try:
                    today = parse_day(query["today"][0], "today")
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
            try:
                payload = build_calendar(start, end, today)
            except sqlite3.Error as exc:
                print(f"GET /api/calendar failed: {exc}")
                self._send_json({"error": "database error"}, status=500)
                return
            self._send_json(payload)
            return

        if self.path == "/api/state":
            try:
                payload = build_state()
            except sqlite3.Error as exc:
                print(f"GET /api/state failed: {exc}")
                self._send_json({"error": "database error"}, status=500)
                return
            self._send_json(payload)
            return

        # Static files -- / and /index.html plus the bundled css/ + js/ assets
        raw_path = self.path.partition("?")[0]
        if raw_path in ("/", "/index.html"):
            rel = "index.html"
        else:
            rel = raw_path.lstrip("/")
        root = HTML_DIR.resolve()
        file_path = (root / rel).resolve()
        try:
            file_path.relative_to(root)
        except ValueError:
            self.send_error(404)  # traversal attempt -- stay inside HTML_DIR
            return
        ctype = (
            CONTENT_TYPES.get(file_path.suffix.lower())
            if file_path.is_file()
            else None
        )
        if ctype is None:
            self.send_error(404)
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        routes = (
            "/api/state",
            "/api/toggle",
            "/api/reset",
            "/api/chores",
            "/api/chores/update",
            "/api/chores/reorder",
            "/api/chores/retire",
        )
        if self.path not in routes:
            self.send_error(404)
            return

        body, error = self._parse_json(self._read_body())
        if error:
            self._send_json({"error": error}, status=400)
            return

        try:
            if self.path == "/api/toggle":
                if not isinstance(body, dict) or "id" not in body:
                    self._send_json(
                        {"error": "expected {id, done, date?, period_key?}"},
                        status=400,
                    )
                    return
                try:
                    chore_id = int(body["id"])
                except (TypeError, ValueError):
                    self._send_json({"error": "id must be an integer"}, status=400)
                    return
                day = None
                if body.get("date") is not None:
                    try:
                        day = parse_day(body["date"], "date")
                    except ValueError as exc:
                        self._send_json({"error": str(exc)}, status=400)
                        return
                if not toggle_chore(
                    chore_id, bool(body.get("done", True)), day,
                    body.get("period_key"),
                ):
                    self._send_json({"error": "unknown chore"}, status=404)
                    return
                self._send_json({"ok": True})
                return

            if self.path == "/api/reset":
                cycle = body.get("cycle") if isinstance(body, dict) else None
                if not reset_cycle(cycle):
                    self._send_json({"error": "unknown cycle"}, status=400)
                    return
                self._send_json({"ok": True})
                return

            if self.path == "/api/chores":
                if not isinstance(body, dict):
                    self._send_json(
                        {"error": "expected {area, text, ...schedule}"}, status=400
                    )
                    return
                chore_id = create_chore(
                    body.get("area"), body.get("text"),
                    {k: body[k] for k in
                     ("freq", "interval", "weekdays", "month_days", "anchor",
                      "until") if k in body},
                    assignee=body.get("assignee"),
                    high_priority=body.get("high_priority", False),
                    tags=body.get("tags"),
                    due_mode=body.get("due_mode", "date"),
                    flex_unit=body.get("flex_unit"),
                    flex_interval=body.get("flex_interval", 0),
                    flex_anchor=body.get("flex_anchor"),
                )
                self._send_json({"ok": True, "id": chore_id})
                return

            if self.path in ("/api/chores/update", "/api/chores/retire"):
                if not isinstance(body, dict) or "id" not in body:
                    self._send_json(
                        {"error": "expected an object with id"}, status=400
                    )
                    return
                try:
                    chore_id = int(body["id"])
                except (TypeError, ValueError):
                    self._send_json({"error": "id must be an integer"}, status=400)
                    return
                if self.path == "/api/chores/retire":
                    found = retire_chore(chore_id)
                else:
                    found = update_chore(chore_id, body)
                if not found:
                    self._send_json({"error": "unknown chore"}, status=404)
                    return
                self._send_json({"ok": True})
                return

            if self.path == "/api/chores/reorder":
                ids = body.get("ids") if isinstance(body, dict) else None
                reorder_chores(ids)
                self._send_json({"ok": True})
                return

            # /api/state -- deprecated compatibility endpoint
            if not isinstance(body, dict):
                self._send_json({"error": "state must be a JSON object"}, status=400)
                return
            apply_flat_state(body)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except sqlite3.Error as exc:
            print(f"POST {self.path} failed: {exc}")
            self._send_json({"error": "database error"}, status=500)


def main():
    try:
        init_db()
    except sqlite3.Error as exc:
        print(
            f"chore-chart cannot open its database: {exc}\n"
            f"  db: {DB_FILE}\n"
            "If the file was created by an older root-run Docker container,\n"
            'fix its ownership once with:  sudo chown -R "$USER" data',
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"chore-chart server listening on :{port} (db: {DB_FILE})")
    server.serve_forever()


if __name__ == "__main__":
    main()

