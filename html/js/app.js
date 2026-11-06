/**
 * Chore Chart UI.
 *
 * Two surfaces share one data set (GET /api/calendar?from&to):
 *   Calendar (do)     Day / Week / Month views; a chore row is just a checkbox.
 *   Chores   (manage) every active chore grouped by area, with its recurrence
 *                     hint and next due date; add / reorder / edit / remove.
 *
 * A chore payload is:
 *   { id, area, text, position, freq, interval, weekdays, month_days,
 *     anchor, until, assignee, high_priority, tags, due_mode, flex_unit,
 *     flex_interval, flex_anchor, next_due }
 * plus per-day occurrences:
 *   dates: { 'YYYY-MM-DD': [{ id, done, overdue, high_priority }] }
 * and flexible occurrences:
 *   flexible: [{ id, period_key, period_start, due, done, overdue, current }]
 * Editing a single chore happens in one overlay (desktop dialog / mobile
 * bottom sheet) opened from either surface, so a chore is editable any day.
 */

const FREQS = ['once', 'daily', 'weekly', 'monthly', 'yearly'];
const FREQ_LABEL = {
  once: 'Does not repeat', daily: 'Daily', weekly: 'Weekly',
  monthly: 'Monthly', yearly: 'Yearly',
};
const FREQ_UNIT = { daily: 'days', weekly: 'weeks', monthly: 'months', yearly: 'years' };
// Weekday names indexed by the backend's Monday-based weekday (Mon 0 .. Sun 6).
const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
// Display order (Sunday first); the values are still backend weekday indices.
const DOW_ORDER = [6, 0, 1, 2, 3, 4, 5];
const CACHE_KEY = 'chore-chart-calendar-v1';
const HIDE_DONE_KEY = 'chore-chart-hide-done';
const VIEW_KEY = 'chore-chart-view';
const SURFACE_KEY = 'chore-chart-surface';
const ASSIGNEE_FILTER_KEY = 'chore-chart-assignee-filter';
const TAG_FILTER_KEY = 'chore-chart-tag-filter';
const SORT_KEY = 'chore-chart-sort';
const UNASSIGNED = '__unassigned__';
const UNTAGGED = '__untagged__';
const MAX_TAGS = 12;
const MAX_TAG_LEN = 30;
const MAX_FLEX_INTERVAL = 30;
const FLEX_UNITS = ['week', 'month', 'year'];

let surface = readPref(SURFACE_KEY, 'calendar');
let view = readPref(VIEW_KEY, 'week');
let selected = todayISO();
let today = todayISO();
let chores = [];
let dates = {};
let flexible = [];
let editorState = null; // live editor draft, or null when the overlay is closed
let assigneeFilter = readPref(ASSIGNEE_FILTER_KEY, '');
let tagFilter = readPref(TAG_FILTER_KEY, '');
let choreSort = readPref(SORT_KEY, 'manual');

function readPref(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
}
function writePref(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
}

/* ------------------------------------------------------------------ dates */

function todayISO() { return toISO(new Date()); }
function parseISO(s) {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}
function toISO(dt) {
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, '0');
  const d = String(dt.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}
function addDays(s, n) {
  const dt = parseISO(s);
  dt.setDate(dt.getDate() + n);
  return toISO(dt);
}
function daysBetween(a, b) {
  return Math.round((parseISO(b) - parseISO(a)) / 86400000);
}
// Monday-based index: Mon 0 .. Sun 6.
function dowIndex(s) { return (parseISO(s).getDay() + 6) % 7; }
// Sunday-based week start (display), returned as ISO.
function weekStartOf(s) { return addDays(s, -parseISO(s).getDay()); }
function startOfMonth(s) {
  const dt = parseISO(s); dt.setDate(1); return toISO(dt);
}
function endOfMonth(s) {
  const dt = parseISO(s); dt.setMonth(dt.getMonth() + 1, 0); return toISO(dt);
}
function daysInMonth(s) { return parseISO(endOfMonth(s)).getDate(); }

/* -------- flexible periods (week starts Sunday, matching the calendar) ---- */

function periodStartOf(unit, s) {
  if (unit === 'week') return weekStartOf(s);
  if (unit === 'month') return startOfMonth(s);
  if (unit === 'year') return s.slice(0, 4) + '-01-01';
  return null;
}

function periodEndOf(unit, s) {
  const start = periodStartOf(unit, s);
  if (unit === 'week') return addDays(start, 6);
  if (unit === 'month') return endOfMonth(start);
  if (unit === 'year') return start.slice(0, 4) + '-12-31';
  return null;
}

function addPeriods(unit, s, n) {
  if (unit === 'week') return addDays(s, 7 * n);
  if (unit === 'month') {
    const dt = parseISO(s);
    dt.setDate(1);
    dt.setMonth(dt.getMonth() + n);
    return toISO(dt);
  }
  if (unit === 'year') return String(parseISO(s).getFullYear() + n) + '-01-01';
  return s;
}

function periodIndex(unit, anchor, start) {
  if (unit === 'week') return Math.round(daysBetween(anchor, start) / 7);
  if (unit === 'month') {
    return (parseISO(start).getFullYear() - parseISO(anchor).getFullYear()) * 12 +
      (parseISO(start).getMonth() - parseISO(anchor).getMonth());
  }
  if (unit === 'year') {
    return parseISO(start).getFullYear() - parseISO(anchor).getFullYear();
  }
  return 0;
}

/** Due date of the current or next flexible occurrence (editor hint). */
function nextFlexDue(unit, interval, anchor, from) {
  if (!unit) return null;
  const start = anchor ? periodStartOf(unit, anchor) : periodStartOf(unit, from);
  if (!interval) return periodEndOf(unit, start);
  const current = periodStartOf(unit, from);
  if (current < start) return periodEndOf(unit, start);
  const index = periodIndex(unit, start, current);
  const active = Math.floor(index / interval) * interval;
  if (active === index) return periodEndOf(unit, current);
  return periodEndOf(unit, addPeriods(unit, start, active + interval));
}

function flexRepeatText(unit, interval) {
  if (!unit) return '';
  if (!interval) return 'once';
  return interval === 1 ? `every ${unit}` : `every ${interval} ${unit}s`;
}

function scopeLabel(unit) {
  return unit === 'week' ? 'This week' : unit === 'month' ? 'This month' :
    unit === 'year' ? 'This year' : 'Anytime';
}

/** Inclusive [from, to] the current view renders. */
function rangeFor(sel, which) {
  if (which === 'day') return { from: sel, to: sel };
  if (which === 'week') {
    const from = weekStartOf(sel);
    return { from, to: addDays(from, 6) };
  }
  return {
    from: weekStartOf(startOfMonth(sel)),
    to: addDays(weekStartOf(endOfMonth(sel)), 6),
  };
}

/**
 * Range to fetch: the view range, widened back to the 1st of the selected
 * month so the overdue roll-up can see earlier-in-the-month days that the
 * current view doesn't render. Stays well under CALENDAR_MAX_DAYS (62).
 */
function fetchRange(sel, which) {
  const r = rangeFor(sel, which);
  const monthStart = startOfMonth(sel);
  return { from: monthStart < r.from ? monthStart : r.from, to: r.to };
}

function formatDay(s, opts) { return parseISO(s).toLocaleDateString(undefined, opts); }

function formatRangeLabel() {
  if (view === 'day') {
    return formatDay(selected, { weekday: 'long', month: 'long', day: 'numeric' });
  }
  if (view === 'week') {
    const from = weekStartOf(selected);
    const a = formatDay(from, { month: 'short', day: 'numeric' });
    const b = formatDay(addDays(from, 6), { month: 'short', day: 'numeric', year: 'numeric' });
    return `${a} – ${b}`;
  }
  return formatDay(selected, { month: 'long', year: 'numeric' });
}

/** "Today" / "Tomorrow" / "Thursday" / "Nov 1" / "Apr 15, 2027" / "—". */
function formatNextDue(iso) {
  if (!iso) return '\u2014';
  const diff = daysBetween(today, iso);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Tomorrow';
  if (diff > 1 && diff < 7) return formatDay(iso, { weekday: 'long' });
  const sameYear = iso.slice(0, 4) === today.slice(0, 4);
  return formatDay(iso, sameYear
    ? { month: 'short', day: 'numeric' }
    : { month: 'short', day: 'numeric', year: 'numeric' });
}

/** Short recurrence hint, e.g. "every 2 weeks on Mon, Thu". */
function ruleHint(c) {
  if (c.freq === 'once') {
    return c.anchor ? `once on ${formatDay(c.anchor, { month: 'short', day: 'numeric' })}` : 'once';
  }
  const unit = FREQ_UNIT[c.freq] || '';
  let s = c.interval > 1 ? `every ${c.interval} ${unit}` : `every ${unit.replace(/s$/, '')}`;
  if (c.freq === 'weekly' && c.weekdays && c.weekdays.length) {
    s += ' on ' + c.weekdays.slice().sort((a, b) => a - b).map(i => DOW[i]).join(', ');
  }
  if (c.freq === 'monthly' && c.month_days && c.month_days.length) {
    s += ' on day ' + c.month_days.map(d => (d === -1 ? 'last' : d)).join(', ');
  }
  if (c.until) {
    s += ` until ${formatDay(c.until, { month: 'short', day: 'numeric', year: 'numeric' })}`;
  }
  return s;
}

/** Recurrence/flexible hint used by the Chores list. */
function scheduleHint(c) {
  if (c.due_mode !== 'flex') return ruleHint(c);
  if (!c.flex_unit) return 'anytime';
  const repeat = flexRepeatText(c.flex_unit, c.flex_interval);
  const overdue = flexible.some(o => o.id === c.id && o.overdue);
  if (overdue) return repeat; // the Overdue column carries the due date
  return c.next_due
    ? `${repeat} · due ${formatDay(c.next_due, { month: 'short', day: 'numeric' })}`
    : repeat;
}

/* ------------------------------------------------------------------ cache */

function snapshot() {
  return JSON.stringify({ surface, view, selected, today, chores, dates, flexible });
}
function cacheState() {
  try { localStorage.setItem(CACHE_KEY, snapshot()); } catch (e) { /* ignore */ }
}
function restoreCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return false;
    const s = JSON.parse(raw);
    if (Array.isArray(s.chores)) chores = s.chores.map(normalizeCachedChore);
    if (s.dates && typeof s.dates === 'object') dates = s.dates;
    if (Array.isArray(s.flexible)) flexible = s.flexible;
    if (s.today) today = s.today;
    if (s.selected) selected = s.selected;
    return chores.length > 0;
  } catch (e) { return false; }
}

/* -------------------------------------------------------------------- API */

async function loadCalendar() {
  const { from, to } = fetchRange(selected, view);
  // The household's own day is authoritative: send it so the server derives
  // `overdue` against the date the wall clock shows, not a UTC container's.
  const localToday = todayISO();
  try {
    const res = await fetch(
      `/api/calendar?from=${from}&to=${to}&today=${localToday}`,
      { cache: 'no-store' },
    );
    if (res.ok) {
      const data = await res.json();
      chores = (data.chores || []).map(normalizeCachedChore);
      dates = data.dates || {};
      flexible = data.flexible || [];
      today = localToday;
      cacheState();
      return true;
    }
  } catch (e) { /* offline / restarting -- keep the last snapshot */ }
  return false;
}

async function postChoreApi(path, payload) {
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let msg = `request failed (${res.status})`;
      try { msg = (await res.json()).error || msg; } catch (e) { /* not json */ }
      console.warn(path, msg);
      return null;
    }
    return await res.json();
  } catch (e) {
    console.error('request failed', e);
    return null;
  }
}

function saveToggle(id, done, day) {
  return postChoreApi('/api/toggle', { id, done, date: day });
}

function saveFlexToggle(id, done, periodKey) {
  return postChoreApi('/api/toggle', {
    id, done, period_key: periodKey, date: todayISO(),
  });
}

/* ------------------------------------------------------------ small utils */

function el(tag, className, textContent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent !== undefined) node.textContent = textContent;
  return node;
}

/** Tags are stored as a list, but an old cached payload may have none. */
function choreTags(chore) {
  return Array.isArray(chore.tags) ? chore.tags : [];
}

/** People already used as assignees, alphabetically. */
function allAssignees() {
  return [...new Set(chores.map(c => c.assignee).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
}

/** Every tag in use, de-duplicated case-insensitively (first spelling wins). */
function allTags() {
  const seen = new Map();
  chores.forEach(c => choreTags(c).forEach(tag => {
    const key = tag.toLocaleLowerCase();
    if (!seen.has(key)) seen.set(key, tag);
  }));
  return [...seen.values()]
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
}

function tagKey(tag) { return tag.toLocaleLowerCase(); }

function sameTag(a, b) { return tagKey(a) === tagKey(b); }

/** Normalize a cached chore so metadata fields always have their shape. */
function normalizeCachedChore(chore) {
  return {
    ...chore,
    assignee: chore.assignee || null,
    high_priority: !!chore.high_priority,
    tags: choreTags(chore),
    due_mode: chore.due_mode === 'flex' ? 'flex' : 'date',
    flex_unit: FLEX_UNITS.includes(chore.flex_unit) ? chore.flex_unit : null,
    flex_interval: Math.max(0, Number(chore.flex_interval) || 0),
    flex_anchor: chore.flex_anchor || null,
  };
}

/* -------------------------------------------------------- header controls */

function bindHeader() {
  document.querySelectorAll('.surface-btn').forEach(btn =>
    btn.addEventListener('click', () => setSurface(btn.dataset.surface)));
  document.querySelectorAll('.view-btn').forEach(btn =>
    btn.addEventListener('click', () => setView(btn.dataset.view)));
  document.getElementById('prevBtn').addEventListener('click', () => navigate(-1));
  document.getElementById('nextBtn').addEventListener('click', () => navigate(1));
  document.getElementById('todayBtn').addEventListener('click', goToday);
}

function setSurface(next) {
  if (!['calendar', 'chores'].includes(next)) return;
  surface = next;
  writePref(SURFACE_KEY, surface);
  renderAll();
}

function setView(next) {
  if (!['day', 'week', 'month'].includes(next)) return;
  view = next;
  writePref(VIEW_KEY, view);
  renderChrome(); // reflect the new range immediately...
  refresh();      // ...then paint the panes once, when the data lands
}

function navigate(dir) {
  if (view === 'day') {
    selected = addDays(selected, dir);
  } else if (view === 'week') {
    selected = addDays(selected, 7 * dir);
  } else {
    const dt = parseISO(selected);
    const day = dt.getDate();
    dt.setDate(1);
    dt.setMonth(dt.getMonth() + dir);
    // Clamp so Jan 31 -> Feb lands on the 28th/29th, not early March.
    dt.setDate(Math.min(day, daysInMonth(toISO(dt))));
    selected = toISO(dt);
  }
  renderChrome();
  refresh();
}

function goToday() { selected = todayISO(); renderChrome(); refresh(); }
function selectDay(iso) { selected = iso; renderChrome(); refresh(); }

/* ----------------------------------------------------------------- render */

/** Cheap chrome only -- switch states, range label, surface visibility. Runs
 *  before the (slower) pane render so navigation feels instant. */
function renderChrome() {
  document.querySelectorAll('.surface-btn').forEach(b => {
    const on = b.dataset.surface === surface;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  document.querySelectorAll('.view-btn').forEach(b => {
    const on = b.dataset.view === view;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  document.getElementById('calendarSurface').hidden = surface !== 'calendar';
  document.getElementById('choresSurface').hidden = surface !== 'chores';
  document.getElementById('rangeLabel').textContent = formatRangeLabel();
  document.querySelector('.layout').classList.toggle('single', view === 'day');
}

function renderAll() {
  renderChrome();
  if (surface === 'chores') { renderChores(); return; }
  renderOverdue();
  renderFlexible();
  renderCalendar();
  renderList(); // also refreshes the progress bar in its header
}

function renderCalendar() {
  const pane = document.getElementById('calPane');
  pane.innerHTML = '';
  if (view === 'day') return; // day view is the list, full width
  pane.appendChild(view === 'week' ? renderWeek() : renderMonth());
}

/** Flexible chores: a shelf above the dated grid, omitted when empty. */
function renderFlexible() {
  const strip = document.getElementById('flexStrip');
  const wrap = document.getElementById('flexItems');
  const byId = new Map(chores.map(c => [c.id, c]));
  const items = flexible
    .map(occ => ({ occ, chore: byId.get(occ.id) }))
    .filter(item => item.chore)
    .sort((a, b) => {
      if (a.occ.overdue !== b.occ.overdue) return a.occ.overdue ? -1 : 1;
      const ad = a.occ.due || '9999-12-31';
      const bd = b.occ.due || '9999-12-31';
      return ad.localeCompare(bd) || a.chore.text.localeCompare(b.chore.text);
    });

  wrap.innerHTML = '';
  strip.hidden = !items.length;
  if (!items.length) return;
  items.forEach(({ occ, chore }) => wrap.appendChild(flexCard(chore, occ)));
}

function flexCard(chore, occ) {
  const card = el('label', 'flex-item' +
    (occ.done ? ' done' : '') + (occ.overdue ? ' overdue' : ''));
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!occ.done;
  const period = occ.current
    ? scopeLabel(chore.flex_unit)
    : (occ.due ? `due ${formatDay(occ.due, { month: 'short', day: 'numeric' })}` : '');
  cb.setAttribute('aria-label',
    `${chore.text}${period ? ', ' + period : ''}${occ.overdue ? ', overdue' : ''}`);

  const body = el('span', 'flex-item-body');
  body.appendChild(el('span', 'flex-item-text', chore.text));
  const meta = el('span', 'flex-meta');
  if (occ.overdue) meta.appendChild(el('span', 'overdue-flag', 'overdue'));
  if (occ.current) {
    const scope = el('span', 'scope-pill' + (chore.flex_unit ? '' : ' anytime'),
      scopeLabel(chore.flex_unit));
    meta.appendChild(scope);
  }
  if (occ.due) {
    meta.appendChild(el('span', 'flex-due',
      `due ${formatDay(occ.due, { month: 'short', day: 'numeric' })}`));
  }
  if (chore.flex_unit) {
    meta.appendChild(el('span', 'flex-repeat',
      flexRepeatText(chore.flex_unit, chore.flex_interval)));
  }
  if (chore.assignee) meta.appendChild(el('span', 'flex-assignee', chore.assignee));
  body.appendChild(meta);

  card.appendChild(cb);
  card.appendChild(body);
  cb.addEventListener('change', () => setFlexDone(chore, occ, card, cb));
  return card;
}

/**
 * The calendar and the day list are rendered independently, so a toggle made
 * from the list has to nudge the calendar too: its per-day tally, status stripe
 * and overdue badge (and the overdue roll-up) would otherwise go stale.
 */
function refreshCalendarSide() {
  if (surface !== 'calendar') return;
  renderOverdue();
  renderFlexible();
  renderCalendar();
}

function occFor(iso) { return dates[iso] || []; }

/** Accessible summary for a calendar cell. */
function dayAriaLabel(iso, occ) {
  const total = occ.length;
  const done = occ.filter(o => o.done).length;
  const overdue = occ.filter(o => o.overdue).length;
  const parts = [formatDay(iso, { weekday: 'long', month: 'long', day: 'numeric' })];
  parts.push(total === 0 ? 'nothing due' : `${done} of ${total} done`);
  if (overdue) parts.push(`${overdue} overdue`);
  return parts.join(', ');
}

function renderMonth() {
  const wrap = el('div', 'month-grid');
  DOW_ORDER.forEach(i => wrap.appendChild(el('div', 'dow-head', DOW[i])));
  const { from, to } = rangeFor(selected, 'month');
  const monthNum = parseISO(selected).getMonth();
  for (let iso = from; iso <= to; iso = addDays(iso, 1)) {
    const occ = occFor(iso);
    const cell = document.createElement('button');
    cell.type = 'button';
    cell.className = 'day-cell';
    if (parseISO(iso).getMonth() !== monthNum) cell.classList.add('other-month');
    if (iso === today) cell.classList.add('today');
    if (iso === selected) cell.classList.add('selected');
    if (occ.some(o => o.overdue)) cell.classList.add('has-overdue');
    cell.appendChild(el('span', 'day-num', String(parseISO(iso).getDate())));
    const count = dayCount(occ);
    if (count) cell.appendChild(count);
    const badge = overdueBadge(occ);
    if (badge) cell.appendChild(badge);
    const stripe = dayStripe(occ);
    if (stripe) cell.appendChild(stripe);
    cell.setAttribute('aria-label', dayAriaLabel(iso, occ));
    cell.addEventListener('click', () => selectDay(iso));
    wrap.appendChild(cell);
  }
  return wrap;
}

/* ---- day-cell content, shared by the month grid and the week strip ---- */

/** "3/5" for a day with chores, or null when nothing is due. */
function dayCount(occ) {
  if (!occ.length) return null;
  const done = occ.filter(o => o.done).length;
  return el('span', 'day-count', `${done}/${occ.length}`);
}

/** Red count pill for a day with overdue chores, or null. */
function overdueBadge(occ) {
  const n = occ.filter(o => o.overdue).length;
  return n ? el('span', 'od-badge', String(n)) : null;
}

/**
 * Thin edge stripe encoding the day's worst state at a glance:
 * overdue (red) > pending (green) > all done (sage). Null when nothing is due,
 * so an empty day carries no signal at all.
 */
function dayStripe(occ) {
  if (!occ.length) return null;
  const status = occ.some(o => o.overdue)
    ? 'overdue'
    : occ.every(o => o.done) ? 'done' : 'pending';
  return el('span', 'day-stripe stripe-' + status);
}

function renderWeek() {
  const wrap = el('div', 'week-grid');
  const { from } = rangeFor(selected, 'week');
  for (let i = 0; i < 7; i++) {
    const iso = addDays(from, i);
    const occ = occFor(iso);
    const overdue = occ.some(o => o.overdue);

    const col = document.createElement('button');
    col.type = 'button';
    col.className = 'day-col';
    if (iso === today) col.classList.add('today');
    if (iso === selected) col.classList.add('selected');
    if (overdue) col.classList.add('has-overdue');

    const head = el('div', 'day-col-head');
    head.appendChild(el('span', 'dow', DOW[dowIndex(iso)]));
    head.appendChild(el('span', 'day-num', String(parseISO(iso).getDate())));
    col.appendChild(head);
    const count = dayCount(occ);
    if (count) col.appendChild(count);
    const badge = overdueBadge(occ);
    if (badge) col.appendChild(badge);
    const stripe = dayStripe(occ);
    if (stripe) col.appendChild(stripe);
    col.setAttribute('aria-label', dayAriaLabel(iso, occ));
    col.addEventListener('click', () => selectDay(iso));
    wrap.appendChild(col);
  }
  return wrap;
}

/** Right-hand list: every chore due on `selected`, grouped by area. */
function renderList() {
  const list = document.getElementById('listPane');
  list.innerHTML = '';

  const occ = occFor(selected);
  const byId = new Map(chores.map(c => [c.id, c]));
  const hideDone = readPref(HIDE_DONE_KEY, '0') === '1';
  const due = occ.map(o => ({ occ: o, chore: byId.get(o.id) }))
                 .filter(x => x.chore && !(hideDone && x.occ.done));

  const head = el('div', 'list-head');
  const yearOpt = selected.slice(0, 4) !== today.slice(0, 4) ? { year: 'numeric' } : {};
  head.appendChild(el('span', 'list-date',
    formatDay(selected, { weekday: 'long', month: 'long', day: 'numeric', ...yearOpt })));

  // Progress lives in the day header: the track is the spacer that used to
  // push the count right, with the "N / M done" label beside it. Both are
  // filled by updateProgress(), so they stay correct as chores are ticked.
  const progress = el('div', 'list-progress');
  const track = el('div', 'progress-track');
  track.id = 'progressTrack';
  track.setAttribute('role', 'progressbar');
  track.setAttribute('aria-label', 'Chores done');
  track.setAttribute('aria-valuemin', '0');
  const fill = el('div', 'progress-fill');
  fill.id = 'progressFill';
  track.appendChild(fill);
  progress.appendChild(track);
  const count = el('span', 'list-count');
  count.id = 'progressLabel';
  progress.appendChild(count);
  head.appendChild(progress);

  if (occ.length) {
    const toggle = el('button', 'hide-done-btn' + (hideDone ? ' active' : ''),
      hideDone ? 'Show done' : 'Hide done');
    toggle.type = 'button';
    toggle.setAttribute('aria-pressed', hideDone ? 'true' : 'false');
    toggle.addEventListener('click', () => {
      writePref(HIDE_DONE_KEY, hideDone ? '0' : '1');
      renderList();
    });
    head.appendChild(toggle);
  }
  list.appendChild(head);

  if (!due.length) {
    list.appendChild(el('div', 'empty',
      occ.length ? 'All done \u2014 nothing left to do.' : 'Nothing due on this day.'));
  }

  const groups = new Map();
  due.forEach(x => {
    if (!groups.has(x.chore.area)) groups.set(x.chore.area, []);
    groups.get(x.chore.area).push(x);
  });
  groups.forEach((items, area) => {
    const g = el('div', 'group');
    g.appendChild(el('div', 'group-title', area));
    items.forEach(x => g.appendChild(calendarRow(x.chore, x.occ)));
    list.appendChild(g);
  });
  updateProgress(); // the bar lives in this header
  applyStickyOffsets();
}

/** A calendar row: checkbox + text + recurrence hint (+ metadata flags). */
function calendarRow(chore, occ) {
  const day = selected; // the date these rows were rendered for
  const row = el('div', 'task' + (occ.done ? ' done' : '') + (occ.overdue ? ' overdue' : ''));
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!occ.done;
  cb.setAttribute('aria-label', chore.text);

  const body = el('div', 'task-body');
  body.appendChild(el('div', 'task-text', chore.text));
  body.appendChild(el('div', 'task-when', ruleHint(chore)));
  const meta = choreMeta(chore, occ);
  if (meta) body.appendChild(meta);

  row.appendChild(cb);
  row.appendChild(body);
  cb.addEventListener('change', () => setChoreDone(chore, occ, row, cb, day));
  row.addEventListener('click', e => {
    if (e.target !== cb) {
      cb.checked = !cb.checked;
      setChoreDone(chore, occ, row, cb, day);
    }
  });
  return row;
}

/**
 * Assignee / tags / priority / overdue badges. Returns null when a chore has
 * no metadata, so plain rows stay uncluttered.
 */
function choreMeta(chore, occ) {
  const tags = choreTags(chore);
  const overdue = !!(occ && occ.overdue);
  if (!chore.high_priority && !chore.assignee && !tags.length && !overdue) return null;
  const meta = el('div', 'task-meta');
  if (chore.high_priority) {
    const flag = el('span', 'prio-flag', 'High priority');
    flag.title = 'High priority';
    meta.appendChild(flag);
  }
  if (chore.assignee) {
    const person = el('span', 'assignee-pill', chore.assignee);
    person.title = `Assigned to ${chore.assignee}`;
    meta.appendChild(person);
  }
  tags.forEach(tag => {
    const pill = el('span', 'tag-pill', '#' + tag);
    pill.title = `Tag: ${tag}`;
    meta.appendChild(pill);
  });
  if (overdue) meta.appendChild(el('span', 'overdue-flag', 'overdue'));
  return meta;
}

/**
 * Publish the sticky day header's height as --list-head-h so area headers can
 * stick just beneath it instead of underneath it.
 */
function applyStickyOffsets() {
  const head = document.querySelector('.list-head');
  if (head) document.documentElement.style.setProperty('--list-head-h', head.offsetHeight + 'px');
}

/* ------------------------------------------------------- chores (manage) */

function renderChores() {
  const wrap = document.getElementById('choreGroups');
  wrap.innerHTML = '';
  const toolbar = document.getElementById('choreToolbar');
  toolbar.hidden = !chores.length;
  syncChoreFilters();

  const shown = chores.filter(matchesChoreFilters);
  const groups = new Map();
  shown.forEach(c => {
    if (!groups.has(c.area)) groups.set(c.area, []);
    groups.get(c.area).push(c);
  });

  const areas = groups.size;
  const filtering = !!(assigneeFilter || tagFilter);
  document.getElementById('choreCount').textContent = chores.length
    ? (filtering
        ? `${shown.length} of ${chores.length} chore${chores.length === 1 ? '' : 's'}`
        : `${chores.length} chore${chores.length === 1 ? '' : 's'} \u00b7 ${areas} area${areas === 1 ? '' : 's'}`)
    : '';

  if (!chores.length) {
    const empty = el('div', 'empty-chores');
    empty.appendChild(el('div', 'empty-chores-title', 'No chores yet'));
    empty.appendChild(el('div', 'empty-chores-text',
      'Add your first chore \u2014 give it a name, an area, and a schedule.'));
    const btn = el('button', 'primary-btn', '+ Add your first chore');
    btn.type = 'button';
    btn.addEventListener('click', () => openEditor(null));
    empty.appendChild(btn);
    wrap.appendChild(empty);
    return;
  }

  if (!shown.length) {
    const empty = el('div', 'empty-chores');
    empty.appendChild(el('div', 'empty-chores-title', 'No matching chores'));
    empty.appendChild(el('div', 'empty-chores-text',
      'Nothing matches that assignee and tag filter.'));
    const clear = el('button', 'primary-btn', 'Clear filters');
    clear.type = 'button';
    clear.addEventListener('click', clearChoreFilters);
    empty.appendChild(clear);
    wrap.appendChild(empty);
    return;
  }

  const visibleIds = shown.map(c => c.id);
  groups.forEach((items, area) => {
    const g = el('div', 'group');
    g.appendChild(el('div', 'group-title', area));
    sortChoreItems(items).forEach(c => g.appendChild(choreRow(c, visibleIds)));
    wrap.appendChild(g);
  });
}

/** True when a chore passes the current assignee/tag filters. */
function matchesChoreFilters(chore) {
  if (assigneeFilter === UNASSIGNED) {
    if (chore.assignee) return false;
  } else if (assigneeFilter) {
    if ((chore.assignee || '').toLocaleLowerCase() !==
        assigneeFilter.toLocaleLowerCase()) return false;
  }
  const tags = choreTags(chore);
  if (tagFilter === UNTAGGED) {
    if (tags.length) return false;
  } else if (tagFilter) {
    if (!tags.some(tag => sameTag(tag, tagFilter))) return false;
  }
  return true;
}

/** Sort a single area's rows while keeping the selected grouping intact. */
function sortChoreItems(items) {
  const textKey = c => c.text.toLocaleLowerCase();
  if (choreSort === 'tag') {
    items.sort((a, b) => {
      const at = choreTags(a).slice().sort((x, y) => x.localeCompare(y))[0] || '\uffff';
      const bt = choreTags(b).slice().sort((x, y) => x.localeCompare(y))[0] || '\uffff';
      return at.localeCompare(bt, undefined, { sensitivity: 'base' }) ||
        textKey(a).localeCompare(textKey(b));
    });
  } else if (choreSort === 'assignee') {
    items.sort((a, b) => {
      const aa = a.assignee || '\uffff';
      const bb = b.assignee || '\uffff';
      return aa.localeCompare(bb, undefined, { sensitivity: 'base' }) ||
        textKey(a).localeCompare(textKey(b));
    });
  }
  return items;
}

/** Keep the toolbar selects in step with the chores and current filter state. */
function syncChoreFilters() {
  const assignees = allAssignees();
  const tags = allTags();
  if (assigneeFilter && assigneeFilter !== UNASSIGNED &&
      !assignees.some(a => a.toLocaleLowerCase() === assigneeFilter.toLocaleLowerCase())) {
    assigneeFilter = '';
    writePref(ASSIGNEE_FILTER_KEY, '');
  }
  if (tagFilter && tagFilter !== UNTAGGED &&
      !tags.some(t => sameTag(t, tagFilter))) {
    tagFilter = '';
    writePref(TAG_FILTER_KEY, '');
  }
  if (!['manual', 'tag', 'assignee'].includes(choreSort)) choreSort = 'manual';

  fillFilterSelect('assigneeFilter',
    [['', 'Everyone'], [UNASSIGNED, 'Unassigned'],
     ...assignees.map(a => [a, a])], assigneeFilter);
  fillFilterSelect('tagFilter',
    [['', 'All tags'], ...tags.map(t => [t, '#' + t]), [UNTAGGED, 'Untagged']],
    tagFilter);
  document.getElementById('sortSelect').value = choreSort;
}

function fillFilterSelect(id, options, value) {
  const select = document.getElementById(id);
  select.innerHTML = '';
  options.forEach(([optionValue, label]) => {
    const opt = document.createElement('option');
    opt.value = optionValue;
    opt.textContent = label;
    select.appendChild(opt);
  });
  select.value = value;
}

function clearChoreFilters() {
  assigneeFilter = '';
  tagFilter = '';
  writePref(ASSIGNEE_FILTER_KEY, '');
  writePref(TAG_FILTER_KEY, '');
  renderChores();
}

/** A management row: text + recurrence hint, next due, and row actions. */
function choreRow(chore, visibleIds) {
  const row = el('div', 'task chore-row');
  row.appendChild(choreBody(chore));

  const meta = el('div', 'next-due');
  if (chore.due_mode === 'flex') {
    const occurrences = flexible.filter(o => o.id === chore.id);
    const overdue = occurrences
      .filter(o => o.overdue)
      .sort((a, b) => (a.due || '').localeCompare(b.due || ''));
    const current = occurrences.find(o => o.current);
    meta.classList.add('flex-next');
    if (overdue.length) {
      meta.classList.add('overdue-next');
      meta.appendChild(el('span', 'next-due-label', 'Overdue'));
      let due = `Due ${formatDay(overdue[0].due, { month: 'short', day: 'numeric' })}`;
      if (overdue.length > 1) due += ` +${overdue.length - 1}`;
      meta.appendChild(el('span', 'flex-scope', due));
    } else {
      meta.appendChild(el('span', 'next-due-label', 'Flexible'));
      if (current) {
        const scope = el('span', 'flex-scope' +
          (current.done ? ' done-scope' : ''), scopeLabel(chore.flex_unit));
        meta.appendChild(scope);
      } else {
        meta.appendChild(el('span', 'flex-scope',
          chore.next_due ? formatNextDue(chore.next_due) : '\u2014'));
      }
    }
  } else {
    meta.appendChild(el('span', 'next-due-label', 'Next'));
    meta.appendChild(el('span', 'next-due-date', formatNextDue(chore.next_due)));
  }
  row.appendChild(meta);

  const index = visibleIds.indexOf(chore.id);
  const manual = choreSort === 'manual';
  const actions = el('div', 'row-actions');
  const up = el('button', '', '\u2191');
  up.type = 'button';
  up.title = manual ? 'Move up' : 'Switch to manual order to reorder';
  up.disabled = !manual || index <= 0;
  up.addEventListener('click', e => { e.stopPropagation(); moveItem(chore, -1, visibleIds); });
  const down = el('button', '', '\u2193');
  down.type = 'button';
  down.title = manual ? 'Move down' : 'Switch to manual order to reorder';
  down.disabled = !manual || index < 0 || index >= visibleIds.length - 1;
  down.addEventListener('click', e => { e.stopPropagation(); moveItem(chore, 1, visibleIds); });
  const edit = el('button', 'edit-row-btn', 'Edit');
  edit.type = 'button';
  edit.addEventListener('click', e => { e.stopPropagation(); openEditor(chore); });
  actions.appendChild(up);
  actions.appendChild(down);
  actions.appendChild(edit);
  row.appendChild(actions);

  row.addEventListener('click', () => openEditor(chore));
  return row;
}

function choreBody(chore) {
  const body = el('div', 'task-body');
  body.appendChild(el('div', 'task-text', chore.text));
  body.appendChild(el('div', 'task-when', scheduleHint(chore)));
  const meta = choreMeta(chore, null);
  if (meta) body.appendChild(meta);
  return body;
}

/* ---------------------------------------------------------------- overdue */

/**
 * Roll-up of *high-priority* overdue occurrences in the loaded range.
 * Normal-priority overdue chores still tint the calendar and are flagged in
 * the day list, but they never feed this banner.
 */
function renderOverdue() {
  const banner = document.getElementById('overdueBanner');
  const byId = new Map(chores.map(c => [c.id, c]));
  let count = 0;
  let earliest = null;
  Object.keys(dates).forEach(iso => {
    if (iso >= today) return;
    const n = dates[iso].filter(o => {
      if (!o.overdue) return false;
      if (o.high_priority !== undefined) return o.high_priority;
      return !!(byId.get(o.id) || {}).high_priority;
    }).length;
    if (!n) return;
    count += n;
    if (!earliest || iso < earliest) earliest = iso;
  });
  const flexOverdue = flexible.filter(o => {
    if (!o.overdue) return false;
    if (o.high_priority !== undefined) return o.high_priority;
    return !!(byId.get(o.id) || {}).high_priority;
  });
  count += flexOverdue.length;

  banner.innerHTML = '';
  if (!count) { banner.hidden = true; return; }
  banner.hidden = false;
  banner.appendChild(el('span', 'overdue-banner-text',
    `\u26a0 ${count} high-priority chore${count === 1 ? '' : 's'} overdue from earlier`));
  const review = el('button', '', 'Review \u2192');
  review.type = 'button';
  review.addEventListener('click', () => {
    if (earliest) { selectDay(earliest); return; }
    // Flexible-only overdue: bring the shelf into view and focus its first
    // overdue checkbox.
    setSurface('calendar');
    document.getElementById('flexStrip').scrollIntoView(
      { behavior: 'smooth', block: 'nearest' });
    const first = document.querySelector('#flexItems .flex-item.overdue input');
    if (first) first.focus();
  });
  banner.appendChild(review);
}

/* ----------------------------------------------------------------- editor */

function openEditor(chore) {
  const isFlex = chore && chore.due_mode === 'flex';
  const flexUnit = isFlex ? (chore.flex_unit || null) : 'month';
  editorState = chore
    ? {
        id: chore.id, text: chore.text, area: chore.area, freq: chore.freq,
        interval: chore.interval, weekdays: [...(chore.weekdays || [])],
        month_days: [...(chore.month_days || [])],
        anchor: chore.anchor || selected, until: chore.until || '',
        assignee: chore.assignee || '', tags: [...choreTags(chore)],
        high_priority: !!chore.high_priority,
        due_mode: isFlex ? 'flex' : 'date',
        flex_unit: flexUnit,
        flex_interval: isFlex ? (chore.flex_interval || 0) : 0,
        flex_anchor: chore.flex_anchor ||
          (flexUnit ? periodStartOf(flexUnit, todayISO()) : null),
      }
    : {
        id: null, text: '', area: '', freq: 'weekly', interval: 1,
        weekdays: [dowIndex(selected)], month_days: [parseISO(selected).getDate()],
        anchor: selected, until: '',
        assignee: '', tags: [], high_priority: false,
        due_mode: 'date', flex_unit: 'month', flex_interval: 0,
        flex_anchor: periodStartOf('month', todayISO()),
      };

  document.getElementById('sheetTitle').textContent = chore ? 'Edit chore' : 'New chore';
  document.getElementById('f-name').value = editorState.text;
  document.getElementById('f-area').value = editorState.area;
  document.getElementById('f-assignee').value = editorState.assignee;
  document.getElementById('f-priority').checked = editorState.high_priority;
  document.getElementById('f-tag').value = '';
  document.getElementById('f-freq').value = editorState.freq;
  document.getElementById('delBtn').style.display = chore ? '' : 'none';
  hideEditorError();
  closeCombo('f-area', 'areaSuggestions');
  closeCombo('f-assignee', 'assigneeSuggestions');
  closeCombo('f-tag', 'tagSuggestions');
  renderTagChips();
  renderTiming();
  renderCustomFields();

  document.getElementById('scrim').hidden = false;
  document.getElementById('sheet').hidden = false;
  document.getElementById('f-name').focus();
}

function closeEditor() {
  editorState = null;
  document.getElementById('scrim').hidden = true;
  document.getElementById('sheet').hidden = true;
}

/** Areas already in use, alphabetically. */
function savedAreas() {
  return [...new Set(chores.map(c => c.area).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
}

/** Areas used elsewhere, offered as a filtered autocomplete list. */
function renderAreaSuggestions(filter) {
  const input = document.getElementById('f-area');
  const values = filterSuggestions(savedAreas(), input.value, filter);
  renderComboOptions('areaSuggestions', values, input.value.trim(), area => {
    input.value = area;
    input.focus();
    closeCombo('f-area', 'areaSuggestions');
  });
}

/** Existing people (plus the current draft's assignee), alphabetically. */
function savedPeople() {
  const people = new Set(allAssignees());
  if (editorState && editorState.assignee) people.add(editorState.assignee);
  return [...people].sort((a, b) => a.localeCompare(b));
}

/** People used elsewhere, offered as a filtered autocomplete list. */
function renderAssigneeSuggestions(filter) {
  const input = document.getElementById('f-assignee');
  const values = filterSuggestions(savedPeople(), input.value, filter);
  renderComboOptions('assigneeSuggestions', values, input.value.trim(), person => {
    input.value = person;
    input.focus();
    closeCombo('f-assignee', 'assigneeSuggestions');
  });
}

/** Existing tags not already on this chore, offered as a filtered list. */
function renderTagSuggestions(filter) {
  if (!editorState) return;
  const input = document.getElementById('f-tag');
  const available = allTags()
    .filter(tag => !editorState.tags.some(selected => sameTag(selected, tag)));
  const values = filterSuggestions(available, input.value, filter);
  renderComboOptions('tagSuggestions', values, '', tag => {
    input.value = '';
    addEditorTag(tag);
    renderTagSuggestions(true);
    openCombo('f-tag', 'tagSuggestions');
    input.focus();
  });
}

/** Case-insensitive substring filter used while typing in a combo. */
function filterSuggestions(values, query, filter) {
  if (!filter) return values;
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return values;
  return values.filter(value => value.toLocaleLowerCase().includes(needle));
}

/** Fill a combo menu with option buttons, marking an exact current match. */
function renderComboOptions(menuId, values, activeValue, onPick) {
  const menu = document.getElementById(menuId);
  menu.innerHTML = '';
  const wanted = (activeValue || '').toLocaleLowerCase();
  values.forEach(value => {
    const active = wanted && value.toLocaleLowerCase() === wanted;
    const option = el('button', 'combo-option' + (active ? ' active' : ''), value);
    option.type = 'button';
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', active ? 'true' : 'false');
    option.addEventListener('click', () => onPick(value));
    menu.appendChild(option);
  });
}

/** Show a combo only when it actually has recommendations. */
function openCombo(inputId, menuId) {
  const input = document.getElementById(inputId);
  const menu = document.getElementById(menuId);
  const hasOptions = menu.children.length > 0;
  menu.hidden = !hasOptions;
  input.setAttribute('aria-expanded', hasOptions ? 'true' : 'false');
}

function closeCombo(inputId, menuId) {
  document.getElementById(menuId).hidden = true;
  document.getElementById(inputId).setAttribute('aria-expanded', 'false');
}

/**
 * Wire a text input to a suggestion menu. The menu opens on focus, filters as
 * the user types, closes on blur/Escape, and supports arrow-key navigation.
 */
function bindCombo(inputId, menuId, render) {
  const input = document.getElementById(inputId);
  const menu = document.getElementById(menuId);
  const combo = input.closest('.combo');
  const show = filter => {
    render(filter);
    openCombo(inputId, menuId);
  };

  input.addEventListener('focus', () => show(false));
  input.addEventListener('input', () => show(true));
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !menu.hidden) {
      e.stopPropagation(); // first Escape closes the menu, not the editor
      closeCombo(inputId, menuId);
    } else if (e.key === 'ArrowDown' && !menu.hidden) {
      e.preventDefault();
      const first = menu.querySelector('.combo-option');
      if (first) first.focus();
    }
  });

  // Keep mouse selection from blurring the input before its click lands.
  menu.addEventListener('mousedown', e => e.preventDefault());
  menu.addEventListener('keydown', e => {
    const options = [...menu.querySelectorAll('.combo-option')];
    const index = options.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      (options[index + 1] || options[0])?.focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (index <= 0) input.focus();
      else options[index - 1].focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closeCombo(inputId, menuId);
      input.focus();
    }
  });

  if (combo) {
    combo.addEventListener('focusout', e => {
      if (!combo.contains(e.relatedTarget)) closeCombo(inputId, menuId);
    });
  }
}

/** Re-render every tag surface after the draft's tag list changes. */
function refreshTagUi() {
  renderTagChips();
  const menu = document.getElementById('tagSuggestions');
  if (menu && !menu.hidden) {
    renderTagSuggestions(true);
    openCombo('f-tag', 'tagSuggestions');
  }
}

/* ---- editor tag chips ---- */

function renderTagChips() {
  const wrap = document.getElementById('tagChips');
  wrap.innerHTML = '';
  if (!editorState || !editorState.tags.length) {
    wrap.appendChild(el('span', 'tag-empty', 'No tags yet'));
    return;
  }
  editorState.tags.forEach((tag, index) => {
    const chip = el('span', 'tag-chip', tag);
    const remove = el('button', 'tag-remove', '\u00d7');
    remove.type = 'button';
    remove.setAttribute('aria-label', `Remove tag ${tag}`);
    remove.title = `Remove ${tag}`;
    remove.addEventListener('click', () => {
      editorState.tags.splice(index, 1);
      refreshTagUi();
    });
    chip.appendChild(remove);
    wrap.appendChild(chip);
  });
}

/** Add one tag to the draft. Returns false when it would exceed a limit. */
function addEditorTag(raw) {
  if (!editorState) return false;
  const tag = raw.trim().replace(/\s+/g, ' ');
  if (!tag) return true;
  if (editorState.tags.some(t => sameTag(t, tag))) return true;
  if (tag.length > MAX_TAG_LEN) {
    showEditorError(`Tags can be at most ${MAX_TAG_LEN} characters.`);
    return false;
  }
  if (editorState.tags.length >= MAX_TAGS) {
    showEditorError(`A chore can have at most ${MAX_TAGS} tags.`);
    return false;
  }
  editorState.tags.push(tag);
  hideEditorError();
  refreshTagUi();
  return true;
}

/** Pull whatever is typed in the tag box into the draft (comma-separated ok). */
function commitTagInput() {
  const input = document.getElementById('f-tag');
  const menu = document.getElementById('tagSuggestions');
  if (!input.value.trim()) { input.value = ''; return true; }
  let ok = true;
  input.value.split(',').forEach(part => {
    if (ok) ok = addEditorTag(part);
  });
  if (ok) input.value = '';
  if (!menu.hidden) {
    renderTagSuggestions(true);
    openCombo('f-tag', 'tagSuggestions');
  }
  return ok;
}

/* ---- editor timing: dated schedule vs flexible window ---- */

function renderTiming() {
  if (!editorState) return;
  const isFlex = editorState.due_mode === 'flex';
  document.querySelectorAll('.timing-btn').forEach(btn => {
    const active = btn.dataset.timing === editorState.due_mode;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  document.getElementById('dateTiming').hidden = isFlex;
  document.getElementById('flexTiming').hidden = !isFlex;
  if (isFlex) {
    renderFlexWindowChips();
    renderFlexRepeatOptions();
    renderFlexHint();
  }
}

function setTiming(mode) {
  if (!editorState || !['date', 'flex'].includes(mode)) return;
  if (editorState.due_mode === mode) return;
  editorState.due_mode = mode;
  if (mode === 'flex' && editorState.flex_unit && !editorState.flex_anchor) {
    editorState.flex_anchor = periodStartOf(editorState.flex_unit, todayISO());
  }
  renderTiming();
}

function renderFlexWindowChips() {
  const wrap = document.getElementById('flexWindowChips');
  wrap.innerHTML = '';
  [['Anytime', null], ['Week', 'week'], ['Month', 'month'], ['Year', 'year']]
    .forEach(([label, value]) => {
      const active = editorState.flex_unit === value;
      const chip = el('button', 'chip' + (active ? ' active' : ''), label);
      chip.type = 'button';
      chip.setAttribute('aria-pressed', active ? 'true' : 'false');
      chip.addEventListener('click', () => {
        if (editorState.flex_unit === value) return;
        editorState.flex_unit = value;
        editorState.flex_anchor = value
          ? periodStartOf(value, todayISO())
          : null;
        if (!value) editorState.flex_interval = 0;
        renderTiming();
      });
      wrap.appendChild(chip);
    });
}

function renderFlexRepeatOptions() {
  const select = document.getElementById('f-flex-repeat');
  select.innerHTML = '';
  const unit = editorState.flex_unit;
  if (!unit) {
    const opt = document.createElement('option');
    opt.value = '0';
    opt.textContent = "Doesn't repeat";
    select.appendChild(opt);
    select.value = '0';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (let n = 0; n <= MAX_FLEX_INTERVAL; n++) {
    const opt = document.createElement('option');
    opt.value = String(n);
    opt.textContent = n === 0
      ? "Doesn't repeat"
      : n === 1 ? `Every ${unit}` : `Every ${n} ${unit}s`;
    select.appendChild(opt);
  }
  if (![...select.options].some(o => Number(o.value) === editorState.flex_interval)) {
    editorState.flex_interval = 0;
  }
  select.value = String(editorState.flex_interval);
}

function renderFlexHint() {
  const hint = document.getElementById('flexTimingHint');
  if (!editorState) { hint.textContent = ''; return; }
  const unit = editorState.flex_unit;
  if (!unit) {
    hint.textContent = 'Anytime chores stay in the Flexible shelf until done and never become overdue.';
    return;
  }
  const due = nextFlexDue(unit, editorState.flex_interval,
    editorState.flex_anchor, todayISO());
  const dueText = due
    ? `Due ${formatDay(due, { weekday: 'short', month: 'short', day: 'numeric' })}.`
    : '';
  const reset = editorState.flex_interval
    ? (editorState.flex_interval === 1
        ? `Resets every ${unit}.`
        : `Resets every ${editorState.flex_interval} ${unit}s.`)
    : 'Stays until done.';
  hint.textContent = `${dueText} ${reset}`.trim();
}

/** Normalize the draft into the exact keys /api/chores(+/update) accept. */
function buildTimingPayload(s) {
  if (s.due_mode === 'flex') {
    return {
      due_mode: 'flex',
      flex_unit: s.flex_unit,
      flex_interval: s.flex_unit ? s.flex_interval : 0,
      flex_anchor: s.flex_unit ? s.flex_anchor : null,
    };
  }
  return {
    due_mode: 'date',
    flex_unit: null,
    flex_interval: 0,
    flex_anchor: null,
  };
}

/** Rebuilds the schedule controls for the current freq (progressive disclosure). */
function renderCustomFields() {
  const box = document.getElementById('custom');
  box.innerHTML = '';
  const s = editorState;
  if (!s) return;

  if (s.freq === 'once') {
    box.appendChild(dateFields([{ label: 'First due', key: 'anchor' }]));
    return;
  }

  const every = el('div', 'inline');
  every.appendChild(el('span', '', 'Every'));
  const interval = document.createElement('input');
  interval.type = 'number';
  interval.min = '1'; interval.max = '30';
  interval.value = String(s.interval);
  interval.className = 'interval';
  interval.setAttribute('aria-label', 'Interval');
  interval.addEventListener('change', () => {
    const n = parseInt(interval.value, 10);
    s.interval = (isNaN(n) || n < 1) ? 1 : Math.min(30, n);
    interval.value = String(s.interval);
  });
  every.appendChild(interval);
  every.appendChild(el('span', '', ' ' + (FREQ_UNIT[s.freq] || 'units')));
  box.appendChild(every);

  if (s.freq === 'weekly') {
    box.appendChild(el('div', 'divider-label', 'On these days'));
    box.appendChild(weekdayChips());
  }
  if (s.freq === 'monthly') {
    box.appendChild(el('div', 'divider-label', 'On day'));
    box.appendChild(monthDayRow());
  }
  box.appendChild(dateFields([
    { label: 'First due', key: 'anchor' },
    { label: 'Until (optional)', key: 'until' },
  ]));
}

function dateFields(fields) {
  const wrap = el('div', 'dates');
  fields.forEach(f => {
    const label = el('label', 'field');
    label.appendChild(document.createTextNode(f.label));
    const input = document.createElement('input');
    input.type = 'date';
    input.value = editorState[f.key] || '';
    input.setAttribute('aria-label', f.label);
    input.addEventListener('change', () => { editorState[f.key] = input.value; });
    label.appendChild(input);
    wrap.appendChild(label);
  });
  return wrap;
}

function weekdayChips() {
  const row = el('div', 'chips');
  DOW_ORDER.forEach(i => {
    const name = DOW[i];
    const active = editorState.weekdays.includes(i);
    const chip = el('button', 'chip' + (active ? ' active' : ''), name);
    chip.type = 'button';
    chip.setAttribute('aria-pressed', active ? 'true' : 'false');
    chip.addEventListener('click', () => {
      if (editorState.weekdays.includes(i)) {
        // keep at least one weekday selected
        if (editorState.weekdays.length > 1) {
          editorState.weekdays = editorState.weekdays.filter(x => x !== i);
        }
      } else {
        editorState.weekdays = [...editorState.weekdays, i].sort((a, b) => a - b);
      }
      renderCustomFields();
    });
    row.appendChild(chip);
  });
  return row;
}

function monthDayRow() {
  const row = el('div', 'inline');
  const input = document.createElement('input');
  input.type = 'number';
  input.min = '1'; input.max = '31';
  input.value = String(editorState.month_days[0] > 0 ? editorState.month_days[0] : 1);
  input.className = 'monthday';
  input.setAttribute('aria-label', 'Day of month');
  input.addEventListener('change', () => {
    let n = parseInt(input.value, 10);
    if (isNaN(n) || n < 1) n = 1;
    if (n > 31) n = 31;
    editorState.month_days = [n];
    input.value = String(n);
  });
  row.appendChild(input);
  const last = el('button', 'chip' + (editorState.month_days.includes(-1) ? ' active' : ''), 'last day');
  last.type = 'button';
  last.addEventListener('click', () => {
    editorState.month_days = editorState.month_days.includes(-1) ? [1] : [-1];
    renderCustomFields();
  });
  row.appendChild(last);
  return row;
}

function onFreqChange(value) {
  if (!editorState) return;
  editorState.freq = value;
  const base = editorState.anchor || selected;
  if (value === 'weekly' && !editorState.weekdays.length) {
    editorState.weekdays = [dowIndex(base)];
  }
  if (value === 'monthly' && !editorState.month_days.length) {
    editorState.month_days = [parseISO(base).getDate()];
  }
  renderCustomFields();
}

/** Normalize the draft into the exact keys /api/chores(+/update) accept. */
function buildRulePayload(s) {
  const payload = { freq: s.freq, interval: s.interval, anchor: s.anchor || today, until: s.until || null };
  if (s.freq === 'weekly') {
    payload.weekdays = s.weekdays.length ? [...s.weekdays].sort((a, b) => a - b) : [0];
  }
  if (s.freq === 'monthly') {
    payload.month_days = s.month_days.length ? [...s.month_days] : [1];
  }
  return payload;
}

function showEditorError(msg) {
  const box = document.getElementById('editorError');
  box.textContent = msg;
  box.hidden = false;
}
function hideEditorError() { document.getElementById('editorError').hidden = true; }

async function saveEditor() {
  if (!editorState) return;
  const name = document.getElementById('f-name').value.trim();
  const area = document.getElementById('f-area').value.trim();
  if (!name || !area) {
    showEditorError('A name and an area are both required.');
    (!name ? document.getElementById('f-name') : document.getElementById('f-area')).focus();
    return;
  }
  if (!commitTagInput()) {
    document.getElementById('f-tag').focus();
    return;
  }
  const rule = buildRulePayload(editorState);
  const timing = buildTimingPayload(editorState);
  if (timing.due_mode === 'flex') rule.until = null;
  const metadata = {
    assignee: document.getElementById('f-assignee').value.trim() || null,
    high_priority: document.getElementById('f-priority').checked,
    tags: [...editorState.tags],
  };
  const payload = { area, text: name, ...metadata, ...rule, ...timing };
  const isNew = !editorState.id;
  const data = isNew
    ? await postChoreApi('/api/chores', payload)
    : await postChoreApi('/api/chores/update', { id: editorState.id, ...payload });
  if (!data) {
    showEditorError('Could not save \u2014 check your connection and try again.');
    return;
  }
  closeEditor();
  toast(isNew ? 'Chore added' : 'Chore saved');
  await refresh();
}

async function deleteEditor() {
  if (!editorState || !editorState.id) return;
  if (!confirm(`Remove "${editorState.text}"? Past checkmarks are kept.`)) return;
  const data = await postChoreApi('/api/chores/retire', { id: editorState.id });
  if (!data) {
    showEditorError('Could not remove \u2014 try again.');
    return;
  }
  closeEditor();
  toast('Chore removed');
  await refresh();
}

/* ---------------------------------------------------------------- actions */

/**
 * Toggle one occurrence from the day list. `day` is the date the row was
 * rendered for — captured at render time rather than read from `selected`, so
 * a click that lands while the list is briefly still showing a previous
 * selection writes to the day the row actually represents.
 */
async function setChoreDone(chore, occ, row, checkbox, day) {
  const done = checkbox.checked;
  const previous = !!occ.done;
  if (done === previous) return;

  // optimistic: paint immediately, then reconcile with the server
  occ.done = done;
  row.classList.toggle('done', done);
  cacheState();

  const isCurrent = day === selected;
  const hideDone = readPref(HIDE_DONE_KEY, '0') === '1';
  if (isCurrent) {
    updateProgress();
    refreshCalendarSide(); // keep the day cells' tally/stripe/badge in step
  }
  // With "Hide done" on, a chore that's just been ticked should leave the list
  // straight away instead of lingering until the next refresh.
  if (isCurrent && hideDone && done) renderList();

  const result = await saveToggle(chore.id, done, day);
  if (!result) {
    // roll back so the screen never silently disagrees with the server
    occ.done = previous;
    checkbox.checked = previous;
    row.classList.toggle('done', previous);
    cacheState();
    if (isCurrent) {
      updateProgress();
      refreshCalendarSide();
      if (hideDone) renderList(); // bring the row back now that it's pending
    }
    toast('Could not save \u2014 tap to try again');
  } else if (!isCurrent) {
    refresh(); // the visible day moved on while the write was in flight
  }
}

async function setFlexDone(chore, occ, card, checkbox) {
  const done = checkbox.checked;
  const previousDone = !!occ.done;
  const previousOverdue = !!occ.overdue;
  if (done === previousDone) return;

  occ.done = done;
  occ.overdue = done ? false : !!(occ.due && occ.due < todayISO());
  card.classList.toggle('done', done);
  card.classList.toggle('overdue', occ.overdue);
  cacheState();
  renderOverdue();

  const result = await saveFlexToggle(chore.id, done, occ.period_key);
  if (!result) {
    occ.done = previousDone;
    occ.overdue = previousOverdue;
    checkbox.checked = previousDone;
    card.classList.toggle('done', previousDone);
    card.classList.toggle('overdue', previousOverdue);
    cacheState();
    renderOverdue();
    toast('Could not save \u2014 tap to try again');
  }
}

/** Move a chore one step within the currently visible management list. */
async function moveItem(item, delta, visibleIds) {
  const ids = chores.map(c => c.id);
  const visible = visibleIds && visibleIds.length ? visibleIds : ids;
  const visibleIndex = visible.indexOf(item.id);
  const targetId = visible[visibleIndex + delta];
  const from = ids.indexOf(item.id);
  const to = ids.indexOf(targetId);
  if (visibleIndex < 0 || targetId === undefined || from < 0 || to < 0) return;
  ids.splice(from, 1);
  ids.splice(to, 0, item.id);
  const data = await postChoreApi('/api/chores/reorder', { ids });
  if (!data) { toast('Could not reorder \u2014 try again'); return; }
  await refresh();
}

function updateProgress() {
  const occ = occFor(selected);
  const total = occ.length;
  const done = occ.filter(o => o.done).length;
  const fill = document.getElementById('progressFill');
  if (fill) fill.style.width = (total ? (done / total * 100) : 0) + '%';
  const label = document.getElementById('progressLabel');
  if (label) label.textContent = total ? `${done} / ${total} done` : 'nothing due';
  const track = document.getElementById('progressTrack');
  if (track) {
    track.setAttribute('aria-valuenow', String(done));
    track.setAttribute('aria-valuemax', String(Math.max(total, 1)));
  }
}

/* ------------------------------------------------------------------ toast */

let toastTimer = null;
function toast(msg) {
  let node = document.getElementById('toast');
  if (!node) {
    node = el('div', 'toast');
    node.id = 'toast';
    node.setAttribute('role', 'status');
    document.body.appendChild(node);
  }
  node.textContent = msg;
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 2200);
}

/* ---------------------------------------------------------------- refresh */

/** Refresh data from the server then redraw (keeps surface, view, selection). */
async function refresh() {
  await loadCalendar();
  renderAll();
}

/* ---------------------------------------------------------------- polling */

// Poll so the wall tablet follows changes made on someone's phone.
function startPolling() {
  setInterval(async () => {
    if (editorState) return; // never yank the DOM out from under the editor
    const before = snapshot();
    await loadCalendar();
    if (snapshot() !== before) renderAll();
  }, 20000);
}

/* -------------------------------------------------------------------- init */

function bindChoresControls() {
  const assigneeSelect = document.getElementById('assigneeFilter');
  assigneeSelect.addEventListener('change', () => {
    assigneeFilter = assigneeSelect.value;
    writePref(ASSIGNEE_FILTER_KEY, assigneeFilter);
    renderChores();
  });
  const tagSelect = document.getElementById('tagFilter');
  tagSelect.addEventListener('change', () => {
    tagFilter = tagSelect.value;
    writePref(TAG_FILTER_KEY, tagFilter);
    renderChores();
  });
  const sortSelect = document.getElementById('sortSelect');
  sortSelect.addEventListener('change', () => {
    choreSort = sortSelect.value;
    writePref(SORT_KEY, choreSort);
    renderChores();
  });
}

function bindEditor() {
  document.getElementById('scrim').addEventListener('click', closeEditor);
  document.getElementById('closeBtn').addEventListener('click', closeEditor);
  document.getElementById('cancelBtn').addEventListener('click', closeEditor);
  document.getElementById('saveBtn').addEventListener('click', saveEditor);
  document.getElementById('delBtn').addEventListener('click', deleteEditor);
  document.getElementById('f-freq').addEventListener('change', e => onFreqChange(e.target.value));
  document.querySelectorAll('.timing-btn').forEach(btn =>
    btn.addEventListener('click', () => setTiming(btn.dataset.timing)));
  document.getElementById('f-flex-repeat').addEventListener('change', e => {
    if (!editorState) return;
    editorState.flex_interval = parseInt(e.target.value, 10) || 0;
    renderFlexHint();
  });
  bindCombo('f-area', 'areaSuggestions', renderAreaSuggestions);
  bindCombo('f-assignee', 'assigneeSuggestions', renderAssigneeSuggestions);
  bindCombo('f-tag', 'tagSuggestions', renderTagSuggestions);
  document.getElementById('addBtn').addEventListener('click', () => openEditor(null));
  document.getElementById('addTagBtn').addEventListener('click', commitTagInput);
  document.getElementById('f-tag').addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      commitTagInput();
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && editorState) closeEditor();
  });
}

(async function init() {
  restoreCache();
  bindHeader();
  bindChoresControls();
  bindEditor();
  renderAll();          // paint immediately from cache (or the empty state)
  await loadCalendar(); // then reconcile with the server
  renderAll();
  startPolling();

  // The day header's height drives the sticky area-header offset, so it needs
  // a nudge when the layout reflows (e.g. the date wraps on a narrow window).
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(applyStickyOffsets, 150);
  });
})();
