#!/usr/bin/env python3
"""
Tiny single-file backend for the household chore chart.

- Serves the static chart at /
- GET  /api/state  -> current cycle periods + chores with completion flags
- POST /api/toggle -> check/uncheck a single chore (idempotent row write)
- POST /api/reset  -> clear one cycle's current-period checkmarks
- POST /api/chores -> add a chore {"cycle", "area", "text"}
- POST /api/chores/update  -> rename/move {"id", "area"?, "text"?, "cycle"?}
- POST /api/chores/reorder -> set a cycle's order {"cycle", "ids"}
- POST /api/chores/retire  -> soft-delete one chore {"id"} (history kept)
- POST /api/state  -> deprecated compat shim (old flat positional dict)

No dependencies beyond the Python standard library (sqlite3 is stdlib).
State lives in SQLite at /app/data/chore.db (WAL mode, one connection per
request, a transaction around every write), so it survives container
restarts and can't be torn by a crash mid-write like the old flat JSON
file could.

The first boot seeds chores from the built-in SEED_CHORES defaults (the
old app/chores.json was folded in here once the UI could edit chores),
each getting a stable integer id in seed order -- the same order the
legacy state.json import maps positional keys onto. Completions are
stored as one row per chore per cycle period (day / week-start / 1st of
month / Jan 1); "done" is computed against today's period, which makes
cycle resets automatic (a stale row from a past period simply no longer
matches) and leaves a natural completion history behind for future stats.

A legacy /app/data/state.json (positional keys like "weekly:3") is
imported once on first boot and renamed to state.json.imported.
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTML_DIR = Path(os.environ.get("HTML_DIR", "/app/html"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))

DB_FILE = DATA_DIR / "chore.db"
LEGACY_STATE = DATA_DIR / "state.json"
SCHEMA_VERSION = 1
CYCLES = ("daily", "weekly", "monthly", "yearly")
MAX_AREA_LEN = 80
MAX_TEXT_LEN = 200

# Content types for the static UI assets served out of HTML_DIR
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

# First-run defaults (formerly app/chores.json, retired once the browser
# UI could edit chores). The database is the source of truth after the
# first boot. Order matters: ids are assigned 1..n in exactly this order,
# which is what the legacy state.json import maps "weekly:3"-style keys
# onto, so keep entries stable.
SEED_CHORES = {
    "daily": [
        {"area": "Kitchen", "text": "Wipe counters and stovetop after cooking"},
        {"area": "Kitchen", "text": "Load / run / unload dishwasher"},
        {"area": "Living areas", "text": "Quick tidy — pillows, blankets, surfaces"},
        {"area": "Living areas", "text": "Wipe kitchen counters and tables"},
        {"area": "Entryway", "text": "Put shoes and bags away"},
        {"area": "Pool", "text": "Skim leaves and debris off the surface"},
        {"area": "Pool", "text": "Tidy back patio"},
        {"area": "General", "text": "Take out kitchen trash and recycling if full"},
        {"area": "General", "text": "Wipe down bathroom sink(s)"},
        {"area": "Pets", "text": "Feed cats / refresh water bowls"},
    ],
    "weekly": [
        {"area": "Whole house", "text": "Vacuum all carpets and rugs"},
        {"area": "Whole house", "text": "Mop hard floors (kitchen, bathrooms, entry)"},
        {"area": "Whole house", "text": "Dust surfaces and shelves"},
        {"area": "Bathrooms", "text": "Clean toilets, tubs, and showers"},
        {"area": "Bathrooms", "text": "Replace towels"},
        {"area": "Bedrooms", "text": "Change and wash bed linens"},
        {"area": "Laundry", "text": "Wash, dry, fold, and put away all loads"},
        {"area": "Kitchen", "text": "Clean out fridge — toss expired items"},
        {"area": "Kitchen", "text": "Wipe down counters"},
        {"area": "Upstairs", "text": "Vacuum stairs and landing"},
        {"area": "Upstairs", "text": "Sweep/vacuum stairs"},
        {"area": "Pool", "text": "Test and balance water chemistry"},
        {"area": "Pool", "text": "Brush walls and vacuum pool floor"},
        {"area": "Pool", "text": "Empty skimmer and pump baskets"},
        {"area": "Lawn", "text": "Mow the lawn"},
        {"area": "Lawn", "text": "Edge walkways and beds"},
        {"area": "General", "text": "Wipe down light switches and door handles"},
        {"area": "General", "text": "Water indoor plants"},
        {"area": "General", "text": "Water outdoor plants"},
        {"area": "Pets", "text": "Scoop out cat litter"},
    ],
    "monthly": [
        {"area": "Kitchen", "text": "Clean oven and microwave interior"},
        {"area": "Kitchen", "text": "Descale coffee maker / espresso machine"},
        {"area": "Kitchen", "text": "Wipe inside of fridge"},
        {"area": "Whole house", "text": "Wash windows (interior)"},
        {"area": "Whole house", "text": "Dust ceiling fans and light fixtures"},
        {"area": "Whole house", "text": "Vacuum under furniture and cushions"},
        {"area": "Bathrooms", "text": "Scrub grout and deep-clean shower doors"},
        {"area": "Bedrooms", "text": "Rotate/flip mattresses if applicable"},
        {"area": "Laundry room", "text": "Clean washer drum and dryer lint trap housing"},
        {"area": "Upstairs", "text": "Check and vacuum vents/air returns"},
        {"area": "Pool", "text": "Deep-clean filter (backwash or rinse cartridge)"},
        {"area": "Pool", "text": "Inspect pool cover, ladder, and safety fencing"},
        {"area": "Lawn", "text": "Fertilize / treat lawn per season"},
        {"area": "Lawn", "text": "Trim hedges and shrubs"},
        {"area": "Garage/storage", "text": "Sweep garage and tidy storage areas"},
        {"area": "General", "text": "Test smoke and CO detectors"},
        {"area": "General", "text": "Vacuum refrigerator coils"},
    ],
    "yearly": [
        {"area": "Whole house", "text": "Wash windows (exterior) and screens"},
        {"area": "Whole house", "text": "Deep-clean carpets professionally or by machine"},
        {"area": "Whole house", "text": "Repaint scuffed walls / touch up trim"},
        {"area": "Roof/gutters", "text": "Clean gutters and downspouts"},
        {"area": "Roof/gutters", "text": "Inspect roof for damage"},
        {"area": "HVAC", "text": "Service furnace and A/C units"},
        {"area": "HVAC", "text": "Replace all air filters"},
        {"area": "Safety", "text": "Replace smoke/CO detector batteries"},
        {"area": "Safety", "text": "Check fire extinguishers' charge dates"},
        {"area": "Pool", "text": "Full pool opening (spring) service"},
        {"area": "Pool", "text": "Full pool closing / winterizing (fall)"},
        {"area": "Pool", "text": "Inspect pump, heater, and filter equipment"},
        {"area": "Lawn", "text": "Aerate and overseed lawn"},
        {"area": "Lawn", "text": "Prune trees; inspect for storm damage"},
        {"area": "Lawn", "text": "Service mower and outdoor equipment"},
        {"area": "Exterior", "text": "Power-wash siding, deck, and driveway"},
        {"area": "Exterior", "text": "Reseal deck or patio if needed"},
        {"area": "Admin", "text": "Review home insurance and warranties"},
        {"area": "Admin", "text": "Declutter and donate — closets, garage, attic"},
    ],
}


def period_start(cycle, today=None):
    """First day of the cycle period containing `today`."""
    today = today or date.today()
    if cycle == "daily":
        return today
    if cycle == "weekly":
        return today - timedelta(days=today.weekday())  # Monday
    if cycle == "monthly":
        return today.replace(day=1)
    if cycle == "yearly":
        return today.replace(month=1, day=1)
    raise ValueError(f"unknown cycle: {cycle}")


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
            cycle      TEXT    NOT NULL
                       CHECK (cycle IN ('daily','weekly','monthly','yearly')),
            area       TEXT    NOT NULL,
            text       TEXT    NOT NULL,
            position   INTEGER NOT NULL,
            since      TEXT,          -- 'YYYY-MM-DD' added-date; NULL = since forever
            retired_at TEXT           -- soft-delete, reserved for UI chore editing
        );
        CREATE TABLE IF NOT EXISTS completions (
            chore_id     INTEGER NOT NULL REFERENCES chores(id) ON DELETE CASCADE,
            period_start TEXT    NOT NULL,
            PRIMARY KEY (chore_id, period_start)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def seed_chores(conn):
    """Load SEED_CHORES with deterministic ids, only when the table is empty."""
    if conn.execute("SELECT COUNT(*) FROM chores").fetchone()[0]:
        return
    rows = []
    for cycle in CYCLES:
        for position, item in enumerate(SEED_CHORES.get(cycle, [])):
            rows.append((cycle, item["area"], item["text"], position))
    if not rows:
        raise RuntimeError("SEED_CHORES contains no chores")
    conn.executemany(
        "INSERT INTO chores (id, cycle, area, text, position) VALUES (?, ?, ?, ?, ?)",
        [(i + 1, *row) for i, row in enumerate(rows)],
    )
    print(f"seeded {len(rows)} chores from SEED_CHORES")


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
    for key, value in state.items():
        cycle, sep, idx_text = key.partition(":")
        row = None
        if sep and cycle in CYCLES:
            try:
                position = int(idx_text)
            except ValueError:
                position = -1
            row = conn.execute(
                "SELECT id FROM chores WHERE cycle = ? AND position = ?",
                (cycle, position),
            ).fetchone()
        if row is None:
            if value:
                print(f"legacy import: skipping unmappable key {key!r}")
                skipped += 1
            continue
        if value:
            conn.execute(
                "INSERT OR IGNORE INTO completions (chore_id, period_start)"
                " VALUES (?, ?)",
                (row["id"], period_start(cycle).isoformat()),
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
        create_schema(conn)
        seed_chores(conn)
        import_legacy_state(conn)



def build_state():
    """Response for GET /api/state: periods, chores, plus legacy mirror keys."""
    today = date.today()
    periods = {cycle: period_start(cycle, today).isoformat() for cycle in CYCLES}
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT ch.id, ch.cycle, ch.area, ch.text, ch.position,
                   CASE WHEN comp.chore_id IS NULL THEN 0 ELSE 1 END AS done
            FROM chores ch
            LEFT JOIN completions comp
              ON comp.chore_id = ch.id
             AND comp.period_start = CASE ch.cycle
                     WHEN 'daily'   THEN ?
                     WHEN 'weekly'  THEN ?
                     WHEN 'monthly' THEN ?
                     WHEN 'yearly'  THEN ?
                 END
            WHERE ch.retired_at IS NULL
            ORDER BY CASE ch.cycle
                         WHEN 'daily' THEN 0 WHEN 'weekly' THEN 1
                         WHEN 'monthly' THEN 2 ELSE 3
                     END,
                     ch.position
            """,
            tuple(periods[cycle] for cycle in CYCLES),
        ).fetchall()
    chores = [
        {
            "id": row["id"],
            "cycle": row["cycle"],
            "area": row["area"],
            "text": row["text"],
            "position": row["position"],
            "done": bool(row["done"]),
        }
        for row in rows
    ]
    payload = {"periods": periods, "chores": chores}
    # Legacy mirror keys so old open tabs (still running the positional
    # version of the page) keep rendering checkmarks until reloaded.
    for chore in chores:
        if chore["done"]:
            payload[f"{chore['cycle']}:{chore['position']}"] = True
    return payload


def toggle_chore(chore_id, done):
    """Check/unchecked one chore in its current period. False if unknown id."""
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT cycle FROM chores WHERE id = ? AND retired_at IS NULL",
            (chore_id,),
        ).fetchone()
        if row is None:
            return False
        period = period_start(row["cycle"]).isoformat()
        if done:
            conn.execute(
                "INSERT OR IGNORE INTO completions (chore_id, period_start)"
                " VALUES (?, ?)",
                (chore_id, period),
            )
        else:
            conn.execute(
                "DELETE FROM completions WHERE chore_id = ? AND period_start = ?",
                (chore_id, period),
            )
    return True


def reset_cycle(cycle):
    """Clear one cycle's current-period rows. False for an unknown cycle."""
    if cycle not in CYCLES:
        return False
    period = period_start(cycle).isoformat()
    with closing(connect()) as conn, conn:
        conn.execute(
            "DELETE FROM completions WHERE period_start = ?"
            " AND chore_id IN (SELECT id FROM chores WHERE cycle = ?)",
            (period, cycle),
        )
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


def create_chore(cycle, area, text):
    """Append a chore to a cycle. Returns the new id; ValueError if invalid."""
    if cycle not in CYCLES:
        raise ValueError("unknown cycle")
    area = clean_field(area, "area", MAX_AREA_LEN)
    text = clean_field(text, "text", MAX_TEXT_LEN)
    with closing(connect()) as conn, conn:
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM chores WHERE cycle = ?",
            (cycle,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO chores (cycle, area, text, position) VALUES (?, ?, ?, ?)",
            (cycle, area, text, position),
        )
        return cur.lastrowid


def update_chore(chore_id, fields):
    """Apply area/text/cycle changes to one active chore.

    False for an unknown (or retired) id; ValueError on invalid input.
    """
    updates = {}
    if "area" in fields:
        updates["area"] = clean_field(fields["area"], "area", MAX_AREA_LEN)
    if "text" in fields:
        updates["text"] = clean_field(fields["text"], "text", MAX_TEXT_LEN)
    if "cycle" in fields:
        if fields["cycle"] not in CYCLES:
            raise ValueError("unknown cycle")
        updates["cycle"] = fields["cycle"]
    if not updates:
        raise ValueError("nothing to update (need area, text, or cycle)")
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT cycle FROM chores WHERE id = ? AND retired_at IS NULL",
            (chore_id,),
        ).fetchone()
        if row is None:
            return False
        if updates.get("cycle") not in (None, row["cycle"]):
            # Moving cycles: land at the end of the target list.
            updates["position"] = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM chores WHERE cycle = ?",
                (updates["cycle"],),
            ).fetchone()[0]
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE chores SET {assignments} WHERE id = ?",
            (*updates.values(), chore_id),
        )
    return True


def reorder_chores(cycle, ids):
    """Rewrite one cycle's positions from an exact, current id list.

    A stale list (another device edited first) raises ValueError instead of
    scrambling the order.
    """
    if cycle not in CYCLES:
        raise ValueError("unknown cycle")
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
                "SELECT id FROM chores WHERE cycle = ? AND retired_at IS NULL"
                " ORDER BY position",
                (cycle,),
            )
        ]
        if sorted(ordered) != sorted(current):
            raise ValueError("ids do not match this cycle's chores")
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
    """Deprecated POST /api/state: translate an old positional dict to rows."""
    applied = 0
    with closing(connect()) as conn, conn:
        for key, value in flat.items():
            cycle, sep, idx_text = key.partition(":")
            if not sep or cycle not in CYCLES:
                continue  # not a positional key (e.g. 'periods' echoed back)
            try:
                position = int(idx_text)
            except ValueError:
                continue
            row = conn.execute(
                "SELECT id FROM chores WHERE cycle = ? AND position = ?",
                (cycle, position),
            ).fetchone()
            if row is None:
                continue
            period = period_start(cycle).isoformat()
            if bool(value):
                conn.execute(
                    "INSERT OR IGNORE INTO completions (chore_id, period_start)"
                    " VALUES (?, ?)",
                    (row["id"], period),
                )
            else:
                conn.execute(
                    "DELETE FROM completions WHERE chore_id = ? AND period_start = ?",
                    (row["id"], period),
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
                    self._send_json({"error": "expected {id, done}"}, status=400)
                    return
                try:
                    chore_id = int(body["id"])
                except (TypeError, ValueError):
                    self._send_json({"error": "id must be an integer"}, status=400)
                    return
                if not toggle_chore(chore_id, bool(body.get("done", True))):
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
                        {"error": "expected {cycle, area, text}"}, status=400
                    )
                    return
                chore_id = create_chore(
                    body.get("cycle"), body.get("area"), body.get("text")
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
                cycle = body.get("cycle") if isinstance(body, dict) else None
                ids = body.get("ids") if isinstance(body, dict) else None
                reorder_chores(cycle, ids)
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
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"chore-chart server listening on :{port} (db: {DB_FILE})")
    server.serve_forever()


if __name__ == "__main__":
    main()

