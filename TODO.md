# TODO

## Short term
- [x] **Move state to SQLite** — replace the flat `data/state.json` with a real SQLite database (via Python's stdlib `sqlite3`, so the server stays dependency-free). Safer writes, room for history/assignees later. *(Done: `data/chore.db` in WAL mode, one connection per request, transactional writes; legacy `state.json` auto-imported once and kept as `.imported`.)*
- [x] **Edit chores from the UI** — add/rename/reorder chores in the browser instead of editing `app/chores.json`. *(Done: Edit toggle in the page header — add, rename text/area, reorder with ↑/↓, move between cycles, soft-delete via new `POST /api/chores*` endpoints; `retired_at` soft-delete keeps history, and rows now render via `textContent` so user-typed text can't inject markup.)*
- [x] **Retire `app/chores.json`** — clutter once edits live in the UI; folded the first-run seed into `server.py` as `SEED_CHORES` and dropped `CHORES_FILE` + the Dockerfile `COPY`. *(Done: DB is the source of truth after first boot; seed order unchanged, so fresh-seed ids and the legacy `state.json` mapping still line up.)*
- [x] **Stable chore IDs** — state is currently keyed by array position (`weekly:3`), so reordering or inserting chores shifts checkmarks. Give each chore a fixed `id` and key state by that instead. *(Done: integer primary keys; completions keyed by `chore_id` + period.)*
- [x] **Automatic cycle resets** — reset Daily/Weekly/Monthly/Yearly on a schedule (compare a stored "last reset" date against the cycle) instead of relying on someone tapping the button. *(Done, simpler than planned: completions store the period they belong to, so stale rows stop matching when the period rolls over — no scheduler or stored reset date needed.)*
- [ ] **Basic write protection** — a shared token or reverse-proxy auth so only the household can `POST` to the API.

## Later
- [x] **Merge instead of overwrite** — send only changed keys (or a version number) so two people checking things off at once don't clobber each other. *(Done: `POST /api/toggle` writes exactly one chore's row; the client never sends whole-state blobs anymore.)*
- [ ] **Assignees** — tag a chore with a person and show who completed it. *(Schema-ready: add a `completed_by` column to `completions`, a people list, and UI.)*
- [ ] **History / streaks** — show past weeks and current streaks. *(Data already accruing since the migration: one row per chore per completed period, no timestamps — period granularity is enough. Needs a summary/charts view only.)*

## Housekeeping
- [ ] Small-screen / wall-tablet layout pass.
- [x] Add a healthcheck (`GET /api/state`) — done as a `HEALTHCHECK` in the Dockerfile so compose stays clean.
- [x] Automate the release notes — tag pushes publish the matching `CHANGELOG.md` section as the release body via `.ci/publish-release.py` (re-runs detect an already-existing release and do nothing).

