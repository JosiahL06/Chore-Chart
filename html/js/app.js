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
 *     anchor, until, next_due }
 * plus per-day occurrences:
 *   dates: { 'YYYY-MM-DD': [{ id, done, overdue }] }
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

let surface = readPref(SURFACE_KEY, 'calendar');
let view = readPref(VIEW_KEY, 'week');
let selected = todayISO();
let today = todayISO();
let chores = [];
let dates = {};
let editorState = null; // live editor draft, or null when the overlay is closed

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

/* ------------------------------------------------------------------ cache */

function snapshot() {
  return JSON.stringify({ surface, view, selected, today, chores, dates });
}
function cacheState() {
  try { localStorage.setItem(CACHE_KEY, snapshot()); } catch (e) { /* ignore */ }
}
function restoreCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return false;
    const s = JSON.parse(raw);
    if (Array.isArray(s.chores)) chores = s.chores;
    if (s.dates && typeof s.dates === 'object') dates = s.dates;
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
      chores = data.chores || [];
      dates = data.dates || {};
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

/* ------------------------------------------------------------ small utils */

function el(tag, className, textContent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent !== undefined) node.textContent = textContent;
  return node;
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
  renderCalendar();
  renderList(); // also refreshes the progress bar in its header
}

function renderCalendar() {
  const pane = document.getElementById('calPane');
  pane.innerHTML = '';
  if (view === 'day') return; // day view is the list, full width
  pane.appendChild(view === 'week' ? renderWeek() : renderMonth());
}

/**
 * The calendar and the day list are rendered independently, so a toggle made
 * from the list has to nudge the calendar too: its per-day tally, status stripe
 * and overdue badge (and the overdue roll-up) would otherwise go stale.
 */
function refreshCalendarSide() {
  if (surface !== 'calendar') return;
  renderOverdue();
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

/** A calendar row: checkbox + text + recurrence hint (+ overdue flag). */
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
  if (occ.overdue) body.appendChild(el('span', 'overdue-flag', 'overdue'));

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
  const groups = new Map();
  chores.forEach(c => {
    if (!groups.has(c.area)) groups.set(c.area, []);
    groups.get(c.area).push(c);
  });
  document.getElementById('choreCount').textContent = chores.length
    ? `${chores.length} chore${chores.length === 1 ? '' : 's'} \u00b7 ${groups.size} area${groups.size === 1 ? '' : 's'}`
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

  groups.forEach((items, area) => {
    const g = el('div', 'group');
    g.appendChild(el('div', 'group-title', area));
    items.forEach(c => g.appendChild(choreRow(c)));
    wrap.appendChild(g);
  });
}

/** A management row: text + recurrence hint, next due, and row actions. */
function choreRow(chore) {
  const row = el('div', 'task chore-row');
  row.appendChild(choreBody(chore));

  const meta = el('div', 'next-due');
  meta.appendChild(el('span', 'next-due-label', 'Next'));
  meta.appendChild(el('span', 'next-due-date', formatNextDue(chore.next_due)));
  row.appendChild(meta);

  const ids = chores.map(c => c.id);
  const index = ids.indexOf(chore.id);
  const actions = el('div', 'row-actions');
  const up = el('button', '', '\u2191');
  up.type = 'button'; up.title = 'Move up'; up.disabled = index <= 0;
  up.addEventListener('click', e => { e.stopPropagation(); moveItem(chore, -1); });
  const down = el('button', '', '\u2193');
  down.type = 'button'; down.title = 'Move down';
  down.disabled = index < 0 || index >= ids.length - 1;
  down.addEventListener('click', e => { e.stopPropagation(); moveItem(chore, 1); });
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
  body.appendChild(el('div', 'task-when', ruleHint(chore)));
  return body;
}

/* ---------------------------------------------------------------- overdue */

/** Roll-up of overdue occurrences in the loaded range (earlier than today). */
function renderOverdue() {
  const banner = document.getElementById('overdueBanner');
  let count = 0;
  let earliest = null;
  Object.keys(dates).forEach(iso => {
    if (iso >= today) return;
    const n = dates[iso].filter(o => o.overdue).length;
    if (!n) return;
    count += n;
    if (!earliest || iso < earliest) earliest = iso;
  });

  banner.innerHTML = '';
  if (!count) { banner.hidden = true; return; }
  banner.hidden = false;
  banner.appendChild(el('span', 'overdue-banner-text',
    `\u26a0 ${count} chore${count === 1 ? '' : 's'} overdue from earlier`));
  const review = el('button', '', 'Review \u2192');
  review.type = 'button';
  review.addEventListener('click', () => selectDay(earliest));
  banner.appendChild(review);
}

/* ----------------------------------------------------------------- editor */

function openEditor(chore) {
  editorState = chore
    ? {
        id: chore.id, text: chore.text, area: chore.area, freq: chore.freq,
        interval: chore.interval, weekdays: [...(chore.weekdays || [])],
        month_days: [...(chore.month_days || [])],
        anchor: chore.anchor || selected, until: chore.until || '',
      }
    : {
        id: null, text: '', area: '', freq: 'weekly', interval: 1,
        weekdays: [dowIndex(selected)], month_days: [parseISO(selected).getDate()],
        anchor: selected, until: '',
      };

  document.getElementById('sheetTitle').textContent = chore ? 'Edit chore' : 'New chore';
  document.getElementById('f-name').value = editorState.text;
  document.getElementById('f-area').value = editorState.area;
  document.getElementById('f-freq').value = editorState.freq;
  document.getElementById('delBtn').style.display = chore ? '' : 'none';
  hideEditorError();
  fillAreaOptions();
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

/** Offer the areas already in use so typos don't spawn duplicate groups. */
function fillAreaOptions() {
  const datalist = document.getElementById('areaOptions');
  datalist.innerHTML = '';
  [...new Set(chores.map(c => c.area))].sort().forEach(area => {
    const opt = document.createElement('option');
    opt.value = area;
    datalist.appendChild(opt);
  });
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
  const rule = buildRulePayload(editorState);
  const isNew = !editorState.id;
  const data = isNew
    ? await postChoreApi('/api/chores', { area, text: name, ...rule })
    : await postChoreApi('/api/chores/update', { id: editorState.id, area, text: name, ...rule });
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

async function moveItem(item, delta) {
  const ids = chores.map(c => c.id);
  const index = ids.indexOf(item.id);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= ids.length) return;
  ids.splice(index, 1);
  ids.splice(target, 0, item.id);
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

function bindEditor() {
  document.getElementById('scrim').addEventListener('click', closeEditor);
  document.getElementById('closeBtn').addEventListener('click', closeEditor);
  document.getElementById('cancelBtn').addEventListener('click', closeEditor);
  document.getElementById('saveBtn').addEventListener('click', saveEditor);
  document.getElementById('delBtn').addEventListener('click', deleteEditor);
  document.getElementById('f-freq').addEventListener('change', e => onFreqChange(e.target.value));
  document.getElementById('addBtn').addEventListener('click', () => openEditor(null));
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && editorState) closeEditor();
  });
}

(async function init() {
  restoreCache();
  bindHeader();
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
