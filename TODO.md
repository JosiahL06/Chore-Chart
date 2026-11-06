# TODO

## Short term
- [x] **Calendar view** — replace the four cycle tabs with a Day/Week/Month calendar showing recurring chores and overdue in red. *(Done: month grid + week strip with per-chore dots and an overdue badge, selected day's list grouped by area, ‹/Today/› navigation, two-pane desktop / stacked mobile layout.)*
- [x] **Per-chore recurrence** — let each chore pick its own schedule (daily/weekly/monthly/yearly, intervals, weekdays, month days) instead of a fixed cycle. *(Done: schema v2 `freq/interval/weekdays/month_days/anchor/until`, pure `due_on()` date math, in-place v1 migration that preserves checkmarks; edited via the Repeats / Every [N] [unit] / on [chips] controls.)*
- [x] **Assignees** — assign a chore to a person and view chores by who they belong to. *(Done: schema v3 `assignee` free-text column + editor field with suggestions from people already in use; the Chores view shows an assignee badge and filters/sorts by assignee. No accounts — assignees are labels, and "completed by" tracking is still deferred below.)*
- [x] **Priority** — a high-priority checkbox when adding/editing a chore. Only high-priority overdue chores feed the overdue banner; every overdue chore still shows as overdue in the calendar view and day list. *(Done: `high_priority` flag, editor checkbox, priority badges, banner filtered.)*
- [x] **Tags** — free-form organizers added to chores that do nothing on their own, but can be used to sort and filter the Chores view. Kept separate from assignees. *(Done: schema v3 `tags` JSON list, editor chip input, tag badges, assignee- and tag-aware filter/sort toolbar.)*
- [x] **Flexible chores** — chores with no specific day that can belong to an Anytime/Week/Month/Year window and repeat. *(Done: schema v4 `due_mode` + `flex_unit`/`flex_interval`/`flex_anchor` and `flex_completions`; the Calendar surface gained a Flexible shelf, the editor a Timing switch, and the Chores rows Flexible/Overdue timing columns. The window end is the derived due date, so unfinished windows become overdue; repeating windows reset each period.)*
- [ ] **Basic write protection** — a shared token or reverse-proxy auth so only the household can `POST` to the API.

## Later
- [x] **Merge instead of overwrite** — send only changed keys (or a version number) so two people checking things off at once don't clobber each other. *(Done: `POST /api/toggle` writes exactly one chore's row; the client never sends whole-state blobs anymore.)*
- [ ] **Completed-by tracking** — record who checked each chore off and show it in history. *(Requires an identity/"who are you" concept; assignees alone don't tell you who actually did the work. Would add a `completed_by` column to `completions`.)*
- [ ] **History / streaks** — show past weeks and current streaks. *(Data already accruing since the migration: one row per chore per completed period, no timestamps — period granularity is enough. Needs a summary/charts view only.)*

## Housekeeping
- [x] Small-screen / wall-tablet layout pass — app shell on desktop: sticky header + calendar with only the chore list scrolling, sticky day header with area headers pinned below it (groups carry bottom padding so a header never detaches early and clips), "Hide done" toggle in the day header, dot rows clamped to their column; single stacked column under 860px, tighter calendar cells under 560px. *(Verified in Chromium at 1280×800, 1280×560, 861px, 860px, 390px and 320px.)*
- [x] Run the container as a non-root user — entrypoint (`app/docker-entrypoint.sh`) chowns `./data` then drops to uid 1000 via `setpriv`; startup now prints an ownership fix hint instead of a traceback if the DB is unwritable.
- [x] Add a healthcheck (`GET /api/state`) — done as a `HEALTHCHECK` in the Dockerfile so compose stays clean.
- [x] Automate the release notes — tag pushes publish the matching `CHANGELOG.md` section as the release body via `.ci/publish-release.py` (re-runs detect an already-existing release and do nothing).

