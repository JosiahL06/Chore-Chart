# Technical notes

Developer reference for chore-chart: architecture, the JSON API, CI, backups, and migration. For the plain-language intro, see the [README](../README.md).

## How it works

- **`html/`** — the UI in vanilla HTML/CSS/JS with no build step: `index.html` (markup), `css/style.css` (styling), `js/app.js` (rendering and API calls), and `icons/` (favicon + apple-touch-icon — drop in your own files with the same names to rebrand). The chore list itself is served by the API.
- **`app/server.py`** — stdlib-only Python server (no dependencies beyond Python's built-in `sqlite3`). Serves the page plus a tiny JSON API (below). Environment: `HTML_DIR`, `DATA_DIR`, `PORT` (default `8080`).
- **`data/chore.db`** — SQLite database (WAL mode) on the mounted volume. Every write is a transaction, so state can't be torn or half-written.

Chores have stable integer ids, so reordering or adding chores never shifts checkmarks. Each completion is one row per chore per cycle period (day / week starting Monday / 1st of the month / Jan 1). "Done" is computed against today's period, so **cycles reset automatically** when the period rolls over — the reset buttons just clear a cycle early. Old rows are kept as history (~4,000 rows/year, well under a megabyte) for future stats/streaks.

The first boot against an empty `data/chore.db` seeds the built-in default list (`SEED_CHORES` in `app/server.py`; the old `app/chores.json` was retired once the UI could edit chores). After that the database is the source of truth — wiping `data/chore.db` reseeds the defaults but **loses checkmarks/history** (back up first, see Backups).

Pages poll `/api/state` every 20 seconds, so checking something off on your phone shows up on the wall tablet shortly after. The last snapshot is also cached in `localStorage`, so the chart still renders if the server is briefly unreachable.

## API

- `GET /api/state` — current cycle periods plus the chore list with completion flags
- `POST /api/toggle` — check/uncheck a single chore: `{"id": 17, "done": true}`
- `POST /api/reset` — clear one cycle's current checkmarks: `{"cycle": "weekly"}`
- `POST /api/chores` — add a chore: `{"cycle": "weekly", "area": "Kitchen", "text": "…"}`
- `POST /api/chores/update` — rename or move a chore: `{"id": 17, "text": "…", "cycle": "monthly"}`
- `POST /api/chores/reorder` — set one cycle's order: `{"cycle": "weekly", "ids": [11, 12, 13]}`
- `POST /api/chores/retire` — soft-delete a chore (history kept): `{"id": 17}`
- `POST /api/state` — deprecated: accepts the old flat positional dict (compat for old open tabs)

Chore edits from the UI (`Edit` in the page header) write straight to these endpoints as single transactions, just like toggling. Removal is a soft delete (`chores.retired_at`), so past checkmarks and history survive.

## CI and image tags

CI (`.github/workflows/build.yaml`) builds the image and publishes it to `ghcr.io/<owner>/<repo>` — the registry `docker compose` pulls from — with three tags on pushes to `main` that touch `Dockerfile`, `app/`, `html/`, or `VERSION`:

- `:latest` — newest successful build.
- `:vX.Y.Z` — read from the `VERSION` file (semver: X = breaking, Y = new features, Z = bugfixes/security). Bump it when you cut a release; CI fails fast if the file isn't exactly `vMAJOR.MINOR.PATCH`. Rebuilding without bumping overwrites this tag — every build is still unique via its SHA tag.
- `:<git sha>` — every build, uniquely identified.

Tag pushes don't rebuild the image; they run the separate `release-notes` workflow instead, which publishes the matching `CHANGELOG.md` section as the GitHub Release body — a `docker pull` line for that version's image included (the tag in the pull command comes from `VERSION`, so it stays correct even if a tag and `VERSION` disagree).

The compose file publishes port `8080` and mounts `./data` (the SQLite database lives there); a commented block in the file shows the alternative shared-network setup (`external: frontend`) if you'd rather have a reverse proxy join the app's network directly instead of the published port. The image ships with a `HEALTHCHECK` against `GET /api/state` (defined in the Dockerfile, so compose stays clean) — `docker ps` reports healthy/unhealthy.

For local development the paths are overridable:

```bash
HTML_DIR=$PWD/html DATA_DIR=/tmp/chore-data python3 app/server.py
```

## Migration from state.json

On first boot next to an existing `data/state.json`, the server imports the old positional checkmarks (mapping `weekly:3` to the seeded chore's id) and renames the file to `state.json.imported` as a backup. Unmappable keys are skipped with a warning.

## Backups

Don't `cp` a live SQLite database — the WAL sidecar won't come along. Take a consistent copy with `VACUUM INTO` instead:

```bash
python3 -c "import sqlite3; sqlite3.connect('data/chore.db').execute(\"VACUUM INTO 'chore-backup.db'\")"
```

(or stop the container first and then copy the file).

## Notes

- No authentication on the API — fine for a trusted home network; add auth via your reverse proxy if that changes. Write protection is still on the TODO.
- Toggles are single-row writes, so two people checking different chores at the same moment merge instead of clobbering each other.
- History/streaks and assignees aren't built yet, but the schema is already accruing the data for them.
