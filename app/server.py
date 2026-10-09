#!/usr/bin/env python3
"""
Tiny single-file backend for the household chore chart.

- Serves the static chart at /
- GET  /api/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD -> chores + per-date
  occurrence flags (done / overdue) for the requested range
- GET  /api/state  -> deprecated shim (derived from today's calendar)
- POST /api/toggle -> check/uncheck a single chore on a date
  {"id": 17, "done": true, "date": "2026-10-08"} (date defaults to today)
- POST /api/reset  -> deprecated no-op shim (kept for old tabs)
- POST /api/chores -> add a chore {"area", "text", "anchor", "freq"?,
  "interval"?, "weekdays"?, "month_days"?, "until"?}
- POST /api/chores/update  -> rename/reschedule {"id", "area"?, "text"?,
  "anchor"?, "freq"?, "interval"?, "weekdays"?, "month_days"?, "until"?}
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
SCHEMA_VERSION = 2
FREQS = ("once", "daily", "weekly", "monthly", "yearly")
# Legacy v1 cycle names, kept only for the v1 -> v2 migration and the
# deprecated flat state.json import.
LEGACY_CYCLES = ("daily", "weekly", "monthly", "yearly")
MAX_AREA_LEN = 80
MAX_TEXT_LEN = 200
MAX_INTERVAL = 30
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
            retired_at TEXT           -- soft-delete, reserved for UI chore editing
        );
        CREATE TABLE IF NOT EXISTS completions (
            chore_id     INTEGER NOT NULL REFERENCES chores(id) ON DELETE CASCADE,
            period_start TEXT    NOT NULL,   -- occurrence date 'YYYY-MM-DD'
            PRIMARY KEY (chore_id, period_start)
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
            create_schema(conn)
        elif version == 1:
            # v1 database in the wild: migrate in place, then mark v2.
            migrate_v1_to_v2(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        import_legacy_state(conn)


def load_chores(conn):
    """All active chores as dicts with parsed recurrence rules."""
    rows = conn.execute(
        "SELECT id, area, text, position, freq, interval, weekdays,"
        " month_days, anchor, until FROM chores"
        " WHERE retired_at IS NULL ORDER BY position"
    ).fetchall()
    chores = []
    for row in rows:
        rule = load_rule(row)
        upcoming = next_due(rule, date.today())
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
            "next_due": upcoming.isoformat() if upcoming else None,
            "_rule": rule,
        })
    return chores


def build_calendar(start, end, today=None):
    """Response for GET /api/calendar: chores + per-date occurrence flags.

    `start`/`end` are dates (inclusive, at most CALENDAR_MAX_DAYS apart).
    Each date maps to [{id, done, overdue}]; overdue is derived (due,
    before today, no completion row) so it never spills into another
    day's list -- it only tints the calendar cell.

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
    done = {(r["chore_id"], r["period_start"]) for r in done_rows}
    dates = {}
    day = start
    while day <= end:
        key = day.isoformat()
        entries = []
        for chore in chores:
            if not due_on(chore["_rule"], day):
                continue
            is_done = (chore["id"], key) in done
            entries.append({
                "id": chore["id"],
                "done": is_done,
                "overdue": (day < today and not is_done),
            })
        dates[key] = entries
        day += timedelta(days=1)
    payload_chores = []
    for chore in chores:
        item = {k: v for k, v in chore.items() if not k.startswith("_")}
        payload_chores.append(item)
    return {"from": start.isoformat(), "to": end.isoformat(),
            "today": today.isoformat(), "chores": payload_chores,
            "dates": dates}


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
            "done": chore["id"] in done_ids,
        })
    return {"periods": {"daily": key}, "chores": chores, "today": key}


def toggle_chore(chore_id, done, day=None):
    """Check/uncheck one chore on one occurrence date. False if unknown id.

    A completion may only be *created* on a date the rule actually fires, so a
    stray POST can't leave orphan rows behind for the future history view.
    Clearing is always allowed (and idempotent), so an existing checkmark can
    still be undone after the chore's schedule is changed.
    """
    target = day or date.today()
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT freq, interval, weekdays, month_days, anchor, until"
            " FROM chores WHERE id = ? AND retired_at IS NULL",
            (chore_id,),
        ).fetchone()
        if row is None:
            return False
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


def create_chore(area, text, rule=None):
    """Append a chore with a recurrence rule. Returns the new id."""
    area = clean_field(area, "area", MAX_AREA_LEN)
    text = clean_field(text, "text", MAX_TEXT_LEN)
    norm = normalize_rule(rule or {})
    with closing(connect()) as conn, conn:
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM chores",
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO chores (area, text, position, freq, interval,"
            " weekdays, month_days, anchor, until)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (area, text, position, norm["freq"], norm["interval"],
             norm["weekdays"], norm["month_days"], norm["anchor"],
             norm["until"]),
        )
        return cur.lastrowid


def update_chore(chore_id, fields):
    """Apply area/text/recurrence changes to one active chore.

    False for an unknown (or retired) id; ValueError on invalid input.
    Recurrence keys (freq, interval, weekdays, month_days, anchor, until)
    are validated together so partial updates can't leave a broken rule.
    """
    updates = {}
    if "area" in fields:
        updates["area"] = clean_field(fields["area"], "area", MAX_AREA_LEN)
    if "text" in fields:
        updates["text"] = clean_field(fields["text"], "text", MAX_TEXT_LEN)
    rule_keys = ("freq", "interval", "weekdays", "month_days", "anchor",
                 "until")
    rule_update = {k: fields[k] for k in rule_keys if k in fields}
    if not updates and not rule_update:
        raise ValueError("nothing to update (need area, text, or schedule)")
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT freq, interval, weekdays, month_days, anchor, until"
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
                        {"error": "expected {id, done, date?}"}, status=400
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
                if not toggle_chore(chore_id, bool(body.get("done", True)), day):
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

