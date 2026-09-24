const CYCLES = ['daily', 'weekly', 'monthly', 'yearly'];

const LABELS = { daily: "Daily", weekly: "Weekly", monthly: "Monthly", yearly: "Yearly" };
const STATE_CACHE_KEY = 'chore-chart-state';

let periods = {};   // { daily: 'YYYY-MM-DD', ... } current period per cycle
let chores = [];    // [{ id, cycle, area, text, position, done }]
let editing = false; // Edit-mode toggle: inline add/rename/reorder/move/retire

function snapshot(){ return JSON.stringify({ periods, chores }); }

function adoptState(data){
  periods = data && data.periods ? data.periods : {};
  chores = data && Array.isArray(data.chores) ? data.chores : [];
}

function cacheState(){
  try{ localStorage.setItem(STATE_CACHE_KEY, snapshot()); }catch(e){ /* private mode etc. */ }
}

function restoreCachedState(){
  try{
    const raw = localStorage.getItem(STATE_CACHE_KEY);
    if(raw) adoptState(JSON.parse(raw));
  }catch(e){ /* corrupt cache -- ignore */ }
}

async function loadState(){
  try{
    const res = await fetch('/api/state', { cache: 'no-store' });
    if(res.ok){
      adoptState(await res.json());
      cacheState();
      return;
    }
  }catch(e){ console.error('load failed', e); }
  // Server unreachable -- fall back to the last snapshot so the chart still renders.
  if(!chores.length) restoreCachedState();
}

async function saveToggle(id, done){
  try{
    await fetch('/api/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, done })
    });
  }catch(e){ console.error('save failed', e); }
}

async function postReset(cycle){
  try{
    await fetch('/api/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cycle })
    });
  }catch(e){ console.error('reset failed', e); }
}

// POST to one of the chore-editing endpoints. Returns the parsed JSON on
// success (truthy, includes {ok:true} and sometimes {id}), null on failure.
async function postChoreApi(path, payload){
  try{
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if(!res.ok) return null;
    return await res.json();
  }catch(e){ console.error('chore edit failed', e); return null; }
}

// Poll for changes made on other devices (e.g. someone's phone) and
// refresh the currently-open tab so the wall tablet stays in sync.
function startPolling(){
  setInterval(async () => {
    try{
      const res = await fetch('/api/state', { cache: 'no-store' });
      if(!res.ok) return;
      const fresh = await res.json();
      // Only compare the fields we own -- the payload may carry legacy
      // mirror keys meant for old open tabs still running the old page.
      const next = {
        periods: fresh.periods || {},
        chores: Array.isArray(fresh.chores) ? fresh.chores : []
      };
      if(JSON.stringify(next) !== snapshot()){
        // Don't yank the DOM out from under an inline edit mid-typing;
        // adopt the fresh state on a later tick instead.
        const focused = document.activeElement;
        if(editing && focused && document.getElementById('panels').contains(focused)) return;
        adoptState(next);
        cacheState();
        const activeTab = document.querySelector('.tab.active');
        const activeGroup = activeTab ? activeTab.dataset.group : CYCLES[0];
        render();
        switchTab(activeGroup);
      }
    }catch(e){ /* offline or server restarting -- ignore, try again next tick */ }
  }, 20000);
}

function groupByArea(items){
  const map = {};
  items.forEach((item) => {
    if(!map[item.area]) map[item.area] = [];
    map[item.area].push(item);
  });
  return map;
}

function render(){
  const tabsEl = document.getElementById('tabs');
  const panelsEl = document.getElementById('panels');
  tabsEl.innerHTML = '';
  panelsEl.innerHTML = '';

  CYCLES.forEach((group, gi) => {
    const tab = document.createElement('button');
    tab.className = 'tab' + (gi === 0 ? ' active' : '');
    tab.textContent = LABELS[group];
    tab.onclick = () => switchTab(group);
    tab.dataset.group = group;
    tabsEl.appendChild(tab);

    const panel = document.createElement('section');
    panel.className = 'panel' + (gi === 0 ? ' active' : '');
    panel.id = 'panel-' + group;

    const byArea = groupByArea(chores.filter(c => c.cycle === group));
    Object.keys(byArea).forEach(area => {
      const gDiv = document.createElement('div');
      gDiv.className = 'group';
      const title = document.createElement('div');
      title.className = 'group-title';
      // textContent, not innerHTML: chore text/areas are user-editable now
      const label = document.createElement('span');
      label.textContent = area;
      title.appendChild(label);
      gDiv.appendChild(title);

      byArea[area].forEach(item => gDiv.appendChild(renderTask(item)));
      panel.appendChild(gDiv);
    });

    if(editing) panel.appendChild(renderAddRow(group));

    panelsEl.appendChild(panel);
  });

  updateProgress();
}

function renderTask(item){
  const row = document.createElement('div');
  row.className = 'task' + (item.done ? ' done' : '');

  if(editing){
    row.classList.add('editing');
    row.appendChild(renderEditControls(item));
    return row;
  }

  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!item.done;
  cb.dataset.id = String(item.id);
  cb.addEventListener('change', () => setChoreDone(item, row, cb.checked));

  const text = document.createElement('div');
  text.className = 'task-text';
  text.textContent = item.text;

  row.appendChild(cb);
  row.appendChild(text);
  row.addEventListener('click', (e) => {
    if(e.target.tagName.toLowerCase() !== 'input'){
      cb.checked = !cb.checked;
      setChoreDone(item, row, cb.checked);
    }
  });
  return row;
}

function renderEditControls(item){
  const wrap = document.createElement('div');
  wrap.className = 'edit-fields';

  const textInput = document.createElement('input');
  textInput.type = 'text';
  textInput.className = 'edit-text';
  textInput.value = item.text;
  textInput.maxLength = 200;
  textInput.setAttribute('aria-label', 'Chore text');
  textInput.addEventListener('change', () => editChoreField(item, 'text', textInput));
  wrap.appendChild(textInput);

  const controls = document.createElement('div');
  controls.className = 'edit-controls';

  const areaInput = document.createElement('input');
  areaInput.type = 'text';
  areaInput.className = 'edit-area';
  areaInput.value = item.area;
  areaInput.maxLength = 80;
  areaInput.setAttribute('aria-label', 'Area');
  areaInput.addEventListener('change', () => editChoreField(item, 'area', areaInput));
  controls.appendChild(areaInput);

  const cycleSelect = document.createElement('select');
  cycleSelect.className = 'edit-cycle';
  CYCLES.forEach((cycle) => {
    const option = document.createElement('option');
    option.value = cycle;
    option.textContent = LABELS[cycle];
    option.selected = cycle === item.cycle;
    cycleSelect.appendChild(option);
  });
  cycleSelect.addEventListener('change', () => moveChore(item, cycleSelect));
  controls.appendChild(cycleSelect);

  const ids = chores.filter(c => c.cycle === item.cycle).map(c => c.id);
  const index = ids.indexOf(item.id);

  const up = document.createElement('button');
  up.type = 'button';
  up.textContent = '↑';
  up.title = 'Move up';
  up.disabled = index <= 0;
  up.addEventListener('click', () => moveItem(item, -1));
  controls.appendChild(up);

  const down = document.createElement('button');
  down.type = 'button';
  down.textContent = '↓';
  down.title = 'Move down';
  down.disabled = index < 0 || index >= ids.length - 1;
  down.addEventListener('click', () => moveItem(item, 1));
  controls.appendChild(down);

  const retire = document.createElement('button');
  retire.type = 'button';
  retire.className = 'retire-btn';
  retire.textContent = 'Remove';
  retire.addEventListener('click', () => removeChore(item));
  controls.appendChild(retire);

  wrap.appendChild(controls);
  return wrap;
}

function renderAddRow(cycle){
  const wrap = document.createElement('div');
  wrap.className = 'add-row';

  const textInput = document.createElement('input');
  textInput.type = 'text';
  textInput.placeholder = 'New chore…';
  textInput.maxLength = 200;
  textInput.setAttribute('aria-label', 'New chore text');

  const areaInput = document.createElement('input');
  areaInput.type = 'text';
  areaInput.placeholder = 'Area';
  areaInput.maxLength = 80;
  areaInput.setAttribute('aria-label', 'New chore area');

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'add-btn';
  btn.textContent = '+ Add';

  const submit = async () => {
    const text = textInput.value.trim();
    const area = areaInput.value.trim();
    if(!text || !area){
      (text ? areaInput : textInput).focus();
      return;
    }
    const data = await postChoreApi('/api/chores', { cycle, area, text });
    if(!data || typeof data.id !== 'number') return;
    chores.push({ id: data.id, cycle, area, text, position: nextPosition(cycle), done: false });
    sortChores();
    await syncAndRender();
    const next = document.querySelector('#panel-' + cycle + ' .add-row input');
    if(next) next.focus();
  };

  btn.addEventListener('click', submit);
  [textInput, areaInput].forEach((input) => {
    input.addEventListener('keydown', (e) => { if(e.key === 'Enter') submit(); });
  });

  wrap.appendChild(textInput);
  wrap.appendChild(areaInput);
  wrap.appendChild(btn);
  return wrap;
}

function switchTab(group){
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.group === group));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + group));
  updateProgress(group);
}

async function setChoreDone(chore, row, checked){
  chore.done = checked;
  row.classList.toggle('done', checked);
  updateProgress();
  cacheState();
  await saveToggle(chore.id, checked);
}

function updateProgress(group){
  if(!group){
    const activeTab = document.querySelector('.tab.active');
    group = activeTab ? activeTab.dataset.group : CYCLES[0];
  }
  const items = chores.filter(c => c.cycle === group);
  const total = items.length;
  const done = items.filter(c => c.done).length;
  document.getElementById('progressFill').style.width = (total ? (done/total*100) : 0) + '%';
  document.getElementById('progressLabel').textContent = `${LABELS[group]}: ${done} / ${total} done`;
}

async function resetGroup(group){
  chores.forEach(c => { if(c.cycle === group) c.done = false; });
  await postReset(group);
  await syncAndRender(group);
}

// Re-render while keeping the visible tab (and, elsewhere, edit mode).
function rerender(group){
  const activeTab = document.querySelector('.tab.active');
  const target = group || (activeTab ? activeTab.dataset.group : CYCLES[0]);
  render();
  switchTab(target);
}

// The caller has already applied its optimistic local change; refresh from
// the server (source of truth) and redraw. Keeps the local view if the
// server is unreachable.
async function syncAndRender(group){
  cacheState();
  await loadState();
  rerender(group);
}

function toggleEdit(){
  editing = !editing;
  const btn = document.getElementById('editBtn');
  btn.classList.toggle('active', editing);
  btn.textContent = editing ? 'Done' : 'Edit';
  rerender();
}

function sortChores(){
  chores.sort((a, b) =>
    CYCLES.indexOf(a.cycle) - CYCLES.indexOf(b.cycle) ||
    a.position - b.position
  );
}

function nextPosition(cycle){
  const positions = chores.filter(c => c.cycle === cycle).map(c => c.position);
  return positions.length ? Math.max.apply(null, positions) + 1 : 0;
}

async function editChoreField(item, field, input){
  const value = input.value.trim();
  if(!value || value === item[field]){
    input.value = item[field];
    return;
  }
  const data = await postChoreApi('/api/chores/update', { id: item.id, [field]: value });
  if(!data){ input.value = item[field]; return; } // server rejected -- revert
  item[field] = value;
  await syncAndRender(); // area changes regroup the list
}

async function moveChore(item, select){
  const cycle = select.value;
  if(cycle === item.cycle) return;
  const data = await postChoreApi('/api/chores/update', { id: item.id, cycle });
  if(!data){ select.value = item.cycle; return; }
  item.cycle = cycle;
  item.position = nextPosition(cycle);
  sortChores();
  await syncAndRender(cycle); // follow the chore to its new tab
}

async function moveItem(item, delta){
  const ids = chores.filter(c => c.cycle === item.cycle).map(c => c.id);
  const index = ids.indexOf(item.id);
  const target = index + delta;
  if(index < 0 || target < 0 || target >= ids.length) return;
  ids.splice(index, 1);
  ids.splice(target, 0, item.id);
  const data = await postChoreApi('/api/chores/reorder', { cycle: item.cycle, ids });
  if(!data) return;
  ids.forEach((id, position) => {
    const chore = chores.find(c => c.id === id);
    if(chore) chore.position = position;
  });
  sortChores();
  await syncAndRender();
}

async function removeChore(item){
  if(!confirm(`Remove "${item.text}"? Past checkmarks are kept.`)) return;
  const data = await postChoreApi('/api/chores/retire', { id: item.id });
  if(!data) return;
  chores = chores.filter(c => c.id !== item.id);
  await syncAndRender();
}

(async function init(){
  await loadState();
  render();
  startPolling();
})();
