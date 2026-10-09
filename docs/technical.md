# Technical notes

Developer reference for chore-chart: architecture, the JSON API, CI, backups, and migration. For the plain-language intro, see the [README](../README.md).

## How it works

- **`html/`** — the UI in vanilla HTML/CSS/JS with no build step: `index.html` (markup), `css/style.css` (styling), `js/app.js` (rendering and API calls), and `icons/` (favicon + apple-touch-icon — drop in your own files with the same names to rebrand). The chore list itself is served by the API.
- The UI is two surfaces behind a **Calendar | Chores** switch. **Calendar** is a read-only "what's due" view (Day/Week/Month; a row is just a checkbox), and **Chores** is the management list (every active chore grouped by area with its recurrence hint and next-due, plus add/reorder/edit/remove). Editing a single chore opens one overlay — a centered dialog on desktop, a bottom sheet under 700px — with progressive disclosure, so a chore is editable on any day rather than only when it's due.
- **`app/server.py`** — stdlib-only Python server (no dependencies beyond Python's built-in `sqlite3`). Serves the page plus a tiny JSON API (below). Environment: `HTML_DIR`, `DATA_DIR`, `PORT` (default `8080`).
- **`data/chore.db`** — SQLite database (WAL mode) on the mounted volume. Every write is a transaction, so state can't be torn or half-written.

Chores have stable integer ids, so reordering or adding chores never shifts checkmarks. Each completion is one row per chore per **occurrence date**, and each chore carries a recurrence rule (`freq`, `interval`, `weekdays`, `month_days`, `anchor`, `until`). Occurrences are computed on the fly with pure date math (`due_on()` in `app/server.py`) — there's no scheduler and no pre-created instance rows. "Done" is a completion row for that date; **overdue** is derived (due + before today + no row), never stored, so it can't spill into a later day's list. Past rows are kept as history (~4,000 rows/year, well under a megabyte) for future stats/streaks.

The first boot against an empty `data/chore.db` creates the schema and starts with **no chores** — new installations are clean and empty so you can add your own from the UI. The database is the source of truth after the first boot, so wiping `data/chore.db` resets to an empty chart but **loses checkmarks/history** (back up first, see Backups).

Pages poll `/api/calendar` every 20 seconds, so checking something off on your phone shows up on the wall tablet shortly after. The last snapshot is also cached in `localStorage`, so the chart still renders if the server is briefly unreachable.

## API

- `GET /api/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD` — the core read: the chore list plus a `dates` map keyed by day, each entry a list of `{id, done, overdue}` occurrences for that day. Each chore also carries `next_due` — its next occurrence on or after today (computed with `next_due()`, so the front end never re-implements the date math), or `null` when nothing is upcoming. Range defaults to today..+6 days, is capped at 62 days (`CALENDAR_MAX_DAYS`), and rejects malformed or inverted ranges with 400. An optional `today=YYYY-MM-DD` sets the date `overdue` is derived against — the UI sends the browser's local day, so a UTC container can't shift the boundary near midnight.
- `GET /api/state` — deprecated shim (today's calendar shaped like the old payload), kept for old open tabs
- `POST /api/toggle` — check/uncheck a single chore on one date: `{"id": 17, "done": true, "date": "2026-10-08"}` (`date` optional, defaults to today). A completion is only *created* on a date the rule actually fires (400 otherwise), so a stray POST can't leave orphan rows; clearing an existing checkmark is always allowed, so a checkmark can still be undone after the schedule changes.
- `POST /api/reset` — deprecated no-op shim (cycles no longer exist)
- `POST /api/chores` — add a chore: `{"area": "Kitchen", "text": "…", "freq": "weekly", "interval": 2, "weekdays": [0, 3], "anchor": "2026-10-05", "until": null}`
- `POST /api/chores/update` — rename or reschedule a chore: `{"id": 17, "text": "…", "freq": "monthly", "month_days": [1, 15]}`. Recurrence fields are validated together, so a partial update can't leave a broken rule; irrelevant selectors (e.g. `weekdays` on a monthly rule) are dropped, out-of-range values for the active selector return 400.
- `POST /api/chores/reorder` — set the global order: `{"ids": [11, 12, 13]}` (must match the active chores exactly; a stale list is rejected rather than scrambling the order)
- `POST /api/chores/retire` — soft-delete a chore (history kept): `{"id": 17}`
- `POST /api/state` — deprecated: accepts the old flat positional dict (compat for old open tabs)

Chore edits from the UI (the **Chores** surface and its editor overlay) write straight to these endpoints as single transactions, just like toggling. Removal is a soft delete (`chores.retired_at`), so past checkmarks and history survive.

## CI and image tags

CI (`.github/workflows/build.yaml`) builds the image and publishes it to `ghcr.io/<owner>/<repo>` — the registry `docker compose` pulls from — with three tags on pushes to `main` that touch `Dockerfile`, `app/`, `html/`, or `VERSION`:

- `:latest` — newest successful build.
- `:vX.Y.Z` — read from the `VERSION` file (semver: X = breaking, Y = new features, Z = bugfixes/security). Bump it when you cut a release; CI fails fast if the file isn't exactly `vMAJOR.MINOR.PATCH`. Rebuilding without bumping overwrites this tag — every build is still unique via its SHA tag.
- `:<git sha>` — every build, uniquely identified.

Tag pushes don't rebuild the image; they run the separate `release-notes` workflow instead, which publishes the matching `CHANGELOG.md` section as the GitHub Release body — a `docker pull` line for that version's image included (the tag in the pull command comes from `VERSION`, so it stays correct even if a tag and `VERSION` disagree).

The compose file publishes port `8080` and mounts `./data` (the SQLite database lives there); a commented block in the file shows the alternative shared-network setup (`external: frontend`) if you'd rather have a reverse proxy join the app's network directly instead of the published port. It also sets `TZ` (default `Etc/UTC`) so the server's own date falls back to your local day; set it via the environment or a `.env` file. The image ships with a `HEALTHCHECK` against `GET /api/state` (defined in the Dockerfile, so compose stays clean) — `docker ps` reports healthy/unhealthy.

The image doesn't run the server as root. Its entrypoint (`app/docker-entrypoint.sh`) starts as root only to `chown` the mounted `./data` — Docker creates a missing bind-mount source as `root:root`, and older images wrote root-owned files — then drops to uid 1000 via `setpriv` before exec'ing `server.py`. The server process is always unprivileged; `docker exec` still lands as root (the container's configured user), so pass `-u chore-chart` for a non-root shell. Since chowning happens at start, upgrades from root-run images and fresh installs both work without manual fixes. On a host where your uid isn't 1000, create `./data` yourself and start the container with `user: "UID:GID"` (the entrypoint skips its chown when already non-root).

For local development the paths are overridable:

```bash
HTML_DIR=$PWD/html DATA_DIR=/tmp/chore-data python3 app/server.py
```

## Migration from state.json

On first boot next to an existing `data/state.json`, the server imports the old positional checkmarks (mapping `weekly:3` to the chore id at that position, when such a chore exists) and renames the file to `state.json.imported` as a backup. Unmappable keys are skipped with a warning.

## Schema migration (v1 → v2)

`PRAGMA user_version` decides what boot does:

- `0` — fresh database (or a pre-versioned v1): create the schema at v2.
- `1` — a v1 database (the `cycle` column): migrate in place, then mark v2.

`migrate_v1_to_v2()` adds the recurrence columns, derives a rule for every row from its old cycle (daily → anchor today; weekly → weekdays `[Mon]` anchored this Monday; monthly → day `[1]` anchored the 1st; yearly → anchored Jan 1), and drops the `cycle` column when SQLite supports it (3.35+). **Completion rows are left untouched**: in v1 the current period's start date (day / Monday / 1st / Jan 1) is exactly the anchor the migration picks, so checkmarks made so far keep counting as done, nothing before the anchor is due (no overdue flood on first open), and older rows remain as history. A database newer than the server refuses to start rather than risk corruption.

## Backups

Don't `cp` a live SQLite database — the WAL sidecar won't come along. Take a consistent copy with `VACUUM INTO` instead:

```bash
python3 -c "import sqlite3; sqlite3.connect('data/chore.db').execute(\"VACUUM INTO 'chore-backup.db'\")"
```

(or stop the container first and then copy the file).

## Tests

The backend ships a stdlib `unittest` suite (no dependencies) covering the recurrence date math, rule validation, the toggle endpoint's due-date guard, and the v1 → v2 migration — including that existing checkmarks survive it:

```bash
python3 -m unittest app.test_server -v
```

`NextDueTests.test_matches_a_brute_force_due_on_scan` cross-checks `next_due()` against a brute-force `due_on()` scan over 1,000 randomized rules, so the two can't quietly drift apart.

## Notes

- No authentication on the API — fine for a trusted home network; add auth via your reverse proxy if that changes. Write protection is still on the TODO.
- Toggles are single-row writes, so two people checking different chores at the same moment merge instead of clobbering each other.
- History/streaks and assignees aren't built yet, but the schema is already accruing the data for them.
