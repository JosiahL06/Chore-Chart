# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Version numbers match the `VERSION` file and the `:vX.Y.Z` image tag. Entries
before `v0.1.0` aren't listed — that's when versioning started.

## [Unreleased]

## [v0.3.0] - 2026-10-09

### Added

- Calendar view replacing the four cycle tabs: switch between **Day**, **Week**,
  and **Month**, with ‹/Today/› navigation. The month grid and week strip show a
  dot per due chore (hollow = pending, filled = done, red = overdue) plus a red
  badge counting that day's overdue chores; the selected day's chores are listed
  in a side pane grouped by area, reusing the same checkbox rows as before.
- Tier-1 recurrence rules per chore: `once` / `daily` / `weekly` / `monthly` /
  `yearly`, each with an interval ("every 2 weeks"), weekday selectors for
  weekly, month-day selectors (including "last day") for monthly, an anchor
  first-due date, and an optional until end date. Occurrences are computed on
  the fly with pure date math — no scheduler or pre-created instances.
- Overdue highlighting is derived (due + before today + no completion), shown
  in red on the calendar and flagged in the list, and never spills into a later
  day's list.
- `GET /api/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD` returning chores plus
  per-date occurrence flags (`done` / `overdue`) for the range (capped at 62
  days). `POST /api/toggle` now accepts an optional `date` to check a chore on
  a specific day.
- The chore editor's schedule section: a **Repeats** dropdown with an
  "Every [N] [unit]" interval, weekday chips for weekly rules, a day-of-month
  input (plus "last day") for monthly rules, and First due / Until date inputs.
- Each chore in the `GET /api/calendar` payload now carries `next_due` — its
  next occurrence on or after today, computed server-side with a new
  `next_due()` helper (bounded scan reusing `due_on()`), or `null` when nothing
  is upcoming. The front end never re-implements the date math.
- An optional `today=YYYY-MM-DD` on `GET /api/calendar`, letting the caller set
  the date `overdue` is derived against.
- A stdlib `unittest` suite (`app/test_server.py`) covering the recurrence date
  math, rule validation, the toggle guard, and the v1 → v2 migration, run with
  `python3 -m unittest app.test_server`. It includes a brute-force cross-check
  of `next_due()` against `due_on()` on 1,000 randomized rules.

### Changed

- The page is now **two surfaces** behind a **Calendar | Chores** switch.
  Calendar stays the read-only "what's due" view (a row is just a checkbox),
  and all add / reschedule / reorder / remove moved to the **Chores**
  management list (every active chore grouped by area with its recurrence hint
  and next-due date). Editing a single chore happens in one overlay — a
  centered dialog on desktop, a bottom sheet under 700px — with progressive
  disclosure: the interval, weekday chips, or day-of-month controls appear
  under the Repeats preset. This fixes the old model, where a chore could only
  be edited on a day it happened to be due. The header **Edit** toggle and the
  inline per-row schedule controls are gone.
- A failed toggle now rolls back and shows a message instead of leaving the
  checkbox out of sync with the server until the next poll.
- Muted text was darkened toward WCAG AA, `:focus-visible` outlines were added
  to the custom buttons, calendar cells carry a summary `aria-label`, and the
  progress bar is a proper `role="progressbar"`.
- The calendar fetch is widened back to the 1st of the selected month so the
  new overdue roll-up can see earlier-in-the-month days the current view does
  not render.
- Calendar polish: the week now starts on **Sunday** (month grid, week strip,
  and the editor's weekday chips all follow), the month grid and week strip
  render the same per-day content (done/total count, status stripe, overdue
  badge), the selected day is marked with a dark ring so it stays obvious on an
  overdue (red) day, and the day's progress bar moved into the day header beside
  the "N / M done" label. Inter-area spacing was trimmed.
- The per-chore dots in the day cells were replaced by a thin **status stripe**
  (red = overdue, sage = pending, green = all done) now that the done/total
  count makes the dots redundant; `renderDots()`/`fitDots()` and their `+N`
  clamping are gone.

- Database schema v2: the `cycle` column is replaced by recurrence rule columns
  (`freq`, `interval`, `weekdays`, `month_days`, `anchor`, `until`). A v1
  database migrates in place on boot (`PRAGMA user_version` 0/1/2 branch);
  checkmarks are preserved because a legacy period's start date (day / Monday /
  1st / Jan 1) is exactly the anchor the migration chooses, so no remapping is
  needed and no overdue flood appears on first open.
- `SEED_CHORES` keeps its legacy cycle grouping so seeded ids still line up
  with the positional `state.json` import; each chore's rule is derived from
  its old cycle and anchored to the current period on a fresh install.
- The four "Reset" buttons are gone (cycles no longer exist);
  `POST /api/reset` remains as a no-op shim for old open tabs.
- Layout is a fixed app shell on desktop/tablet: the header and calendar stay
  pinned, only the selected day's chore list scrolls, and both columns fill the
  height instead of the page growing around a long list. The day header sticks
  to the top of the list and each area header sticks just below it, so you keep
  the date and the room you're in while a long list scrolls (each group carries
  bottom padding so its header can always reach that offset instead of
  detaching early and getting clipped). A **Hide done** button in the day header
  trims finished chores and is remembered per browser, and calendar dot rows
  clamp to their column so a busy day can't wrap and deform the grid. Under
  860px it still stacks and scrolls the whole page.

- The UI now sends its own local date to the calendar, so `overdue` follows the
  household's wall clock instead of a UTC container's; `docker-compose.yml`
  sets `TZ` to keep the server-side fallbacks in step.
- Navigating the calendar paints its panes once (the chrome updates instantly
  and the data follows) instead of rendering twice, and the Day/Week/Month and
  Calendar/Chores switches now expose `aria-pressed`.

### Fixed

- `POST /api/toggle` no longer creates a completion on a date the rule doesn't
  fire (400 instead), so a stray request can't leave orphan rows for the future
  history view; clearing an existing checkmark is still always allowed.
- With **Hide done** on, checking a chore off now removes its row immediately
  instead of leaving it on screen until the next poll; if the save fails the row
  reappears as the toggle rolls back.

### Removed

- The header **Edit** toggle and the inline per-row schedule controls
  (replaced by the Chores surface and the editor overlay).
- The Daily/Weekly/Monthly/Yearly tab strip and its panels.
- The built-in starter chore list (`SEED_CHORES`) is no longer seeded on first
  boot. New installations start clean and empty, so the built-in default chores
  are gone and you add your own from the UI.


## [v0.2.3] - 2026-09-24

### Changed

- The container image no longer runs the server as root: an entrypoint chowns
  the mounted `data/` directory (Docker creates a missing `./data` as root,
  and older images wrote root-owned files) and then drops to an unprivileged
  uid 1000 before starting `server.py`. Fresh installs and upgrades from
  root-run images now work without a manual `chown`.
- Added `.dockerignore` to filter out git history, documents, local databases,
  and virtualenvs from the Docker build context.
- Hardened the container image: application code and static assets are copied
  as `root:root` read-only to uid 1000, and `PYTHONDONTWRITEBYTECODE=1` prevents
  attempting to write `.pyc` caches into the container.
- Starting the server against an unwritable database prints the path and the
  ownership fix instead of a raw traceback.

## [v0.2.2] - 2026-09-24

### Added

- Public repository on GitHub: [`JosiahL06/Chore-Chart`](https://github.com/JosiahL06/Chore-Chart).
  Releases there publish themselves from this file's matching section — each
  with a `docker pull` line for the version's image — and CI builds
  `ghcr.io/josiahl06/chore-chart` tagged `:latest`, `:vX.Y.Z`, and `:<git sha>`.

### Changed

- Docker Compose now defaults to the `ghcr.io/josiahl06/chore-chart` image;
  set `CHORE_CHART_IMAGE` to pull from a different registry.
- Documentation rewritten for the public repository.

## [v0.2.1] - 2026-09-24

### Added

- Favicon set (`favicon.svg`, `favicon.ico`, `apple-touch-icon.png`) and page
  metadata for browsers (viewport, description, color-scheme, theme-color).

### Changed

- Container image now ships the license text (`COPY LICENSE`), so a redistributed
  image is self-contained.
- Split the UI's inline CSS and JS into `html/css/style.css` and
  `html/js/app.js`; the server serves static assets from `HTML_DIR` with
  path-traversal protection and a wider set of content types.
- Generalized the Docker Compose template (swap-able registry prefix,
  commented shared-network alternative).
- Tag pushes now publish the release notes from `CHANGELOG.md` (a separate
  `release-notes` workflow) instead of rebuilding the image, followed by a
  `docker pull` line for the version's image.

## [v0.2.0] - 2026-09-23

### Added

- Edit chores in the browser: add, rename text or area, reorder with ↑/↓, move
  between cycles, and remove. Backed by new `POST /api/chores`,
  `/api/chores/update`, `/api/chores/reorder`, and `/api/chores/retire`
  endpoints.
- Removal is a soft delete (`chores.retired_at`), so past checkmarks and
  history survive.

### Changed

- First-boot seed folded into `app/server.py` as `SEED_CHORES`, in the same
  order, so freshly seeded ids still line up with the legacy `state.json`
  mapping.
- Chore text and area render via `textContent`, so user-typed text can't inject
  markup.

### Removed

- `app/chores.json` — the database is the source of truth once chores can be
  edited in the UI.

## [v0.1.0] - 2026-09-23

### Added

- SQLite-backed state (`data/chore.db`, WAL mode, one connection per request, a
  transaction around every write), replacing the flat `data/state.json` file.
- Stable integer chore ids, with completions keyed per chore per period, so
  reordering or adding chores never shifts checkmarks.
- Automatic cycle resets: completions store the period they belong to (day /
  week starting Monday / 1st of the month / Jan 1), so a stale row simply stops
  matching once the period rolls over — no reset scheduler required.
- One-time import of a legacy `data/state.json`, renamed to
  `state.json.imported` as a backup.
- `VERSION` file consumed by CI, which publishes `:latest`, `:vX.Y.Z`, and
  `:<git sha>` image tags, plus a `docker-compose.yml` for running the image.
- Backup guidance using `VACUUM INTO` (a plain `cp` would miss the WAL sidecar).

### Changed

- Checking a chore off writes exactly one row (`POST /api/toggle`), so two
  people checking different chores at the same moment merge instead of
  clobbering each other.
- `POST /api/state` kept working as a deprecated shim for old open tabs.
