# Flexible (floating) chores — design proposal

**Status:** implemented as schema v4 + flexible UI. This file remains as the
design record; the approved change is that a flexible window's end date is its
derived due date, so unfinished windows become overdue.

The working UI label is **Flexible**. It reads more calmly than “floating” and
fits the existing calendar vocabulary. The label is easy to change.

## Product model

A chore now has one of two timing modes:

- **On a date** — the current behaviour: a real recurrence with a calendar
  anchor, day cells, next-due dates, and overdue detection.
- **Flexible** — the chore has no specific day. It lives in a small shelf
  outside the dated calendar, connected to an optional time window:
  **Anytime**, **Week**, **Month**, or **Year**. It can optionally repeat.

### Rules

- Flexible chores **never appear in calendar day cells**.
- A flexible window's **end date is its derived due date**. If the window ends
  without a completion, the chore is marked overdue in the Flexible shelf and
  the Chores list. High-priority flexible chores also feed the overdue banner.
- A repeating flexible chore gets **one completion per period**. Checking it
  off marks the current week/month/year done; it returns at the start of the
  next period. Missed periods remain visible as overdue (up to 12 per chore).
- A non-repeating flexible chore stays in the Flexible shelf until it is done.
  “Anytime” is always non-repeating and never overdue.
- Week periods start on **Sunday**, matching the calendar’s visible week.
- Area, assignee, tags, priority, reordering, editing, and deletion all still
  work exactly as they do for dated chores.
- There is no new top-level surface and no change to the Calendar | Chores
  switch.

## Mockup screens

Open `mockups/floating-chores.html` for the interactive preview.

- `screenshots/calendar-week.png` — Flexible shelf above the week grid.
- `screenshots/calendar-month.png` — same shelf, month view.
- `screenshots/chores.png` — flexible rows in the management list.
- `screenshots/editor-flex.png` — the editor’s Timing controls.
- `screenshots/mobile-week.png` — the shelf as a compact 2×2 grid on mobile.

### Calendar

A full-width **Flexible** shelf sits between the overdue banner and the
calendar/day-list layout. It only renders when flexible chores exist, so a
household that never uses the feature sees no extra furniture.

Each card is a checkbox + chore name + scope pill + repeat hint + optional
assignee. Done cards are struck through. The dashed top/bottom rules and blue
scope pills separate this shelf from the dated grid without adding a new
surface or pop-up.

### Chores management

Flexible rows keep the existing grouped-by-area list. The right-hand column
that normally shows **Next / Today** instead shows **Flexible / This month**
(or This week, This year, Anytime). The recurrence hint under the name remains
(e.g. “every month”).

### Editor

The schedule section becomes **Timing** with a two-way switch:

- **On a date** — the existing schedule controls.
- **Flexible** — Window chips (`Anytime`, `Week`, `Month`, `Year`) plus a
  Repeats select (`Doesn't repeat`, `Every week/month/year`, optionally every
  N periods). Anytime disables repeats.

A short hint shows the derived window-end due date and explains that repeating
chores reset each period.

## Database proposal (schema v4)

Keep dated chores untouched and add a parallel timing mode:

```sql
ALTER TABLE chores ADD COLUMN due_mode TEXT NOT NULL DEFAULT 'date'
  CHECK (due_mode IN ('date', 'flex'));
ALTER TABLE chores ADD COLUMN flex_unit TEXT
  CHECK (flex_unit IS NULL OR flex_unit IN ('week', 'month', 'year'));
ALTER TABLE chores ADD COLUMN flex_interval INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chores ADD COLUMN flex_anchor TEXT;  -- first active period start
```

- `due_mode = 'date'` keeps today’s `freq/interval/weekdays/month_days/anchor/until`
  behaviour unchanged.
- For flexible chores, `flex_unit` is the window. `NULL` means **Anytime**.
- `flex_interval = 0` means **doesn’t repeat**. `1` means every period, `2`
  every second period, etc.
- `flex_anchor` is the first period start for interval calculations. It
  defaults to the current period when the chore is created.
- `until` can still cap a repeating flexible chore if needed later.

### Completions

Use a separate table rather than overloading date-based completions:

```sql
CREATE TABLE flex_completions (
  chore_id   INTEGER NOT NULL REFERENCES chores(id) ON DELETE CASCADE,
  period_key TEXT    NOT NULL,
  PRIMARY KEY (chore_id, period_key)
) WITHOUT ROWID;
```

Period keys include the window so changing a chore’s window can’t accidentally
reuse an old checkmark:

- `w:2026-11-01` — week beginning Sunday
- `m:2026-11-01` — month
- `y:2026-01-01` — year
- `once` — non-repeating flexible chore

Why a separate table: date completions already drive the dated calendar and
overdue queries. Floating periods are not dates and should not leak into that
logic. History can union the two tables later.

## API sketch

- `GET /api/calendar` gains a `flexible` array computed against `today`:
  `[{id, scope, period_key, period_label, done}]`.
- `POST /api/toggle` branches on the chore’s `due_mode`. For flexible chores
  it derives the period from the supplied date (default today) and writes
  `flex_completions`; the response returns the `period_key` so the UI can
  update optimistically.
- Existing dated chores keep the same request/response shape.

## Deliberately not in the first pass

- No timing filter in the Chores toolbar; the rows are already labelled.
- No flexible items in calendar cells or week/month counts.
- No drag-and-drop between windows.
- Missed flexible periods are capped at the 12 most recent per chore rather
  than kept as an unbounded shelf.

## Open labels

- `Flexible` vs `Floating` vs `Anytime chores`.
- Whether “Anytime” should be visually grouped after the time-windowed items
  (current mockup) or pinned first.
