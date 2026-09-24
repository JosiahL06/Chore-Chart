# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Version numbers match the `VERSION` file and the `:vX.Y.Z` image tag. Entries
before `v0.1.0` aren't listed — that's when versioning started.

## [Unreleased]


## [v0.2.3] - 2026-09-24

### Changed

- The container image no longer runs the server as root: an entrypoint chowns
  the mounted `data/` directory (Docker creates a missing `./data` as root,
  and older images wrote root-owned files) and then drops to an unprivileged
  uid 1000 before starting `server.py`. Fresh installs and upgrades from
  root-run images now work without a manual `chown`.
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
