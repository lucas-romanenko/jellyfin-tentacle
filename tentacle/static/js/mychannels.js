// Tentacle - My Channels (everyone): which Live TV channels you see.
//
// The household's channels are the admin's (channel groups switched on per
// provider). Each person sees either all of them or a list of their own, in
// their own order: that order is their channel numbers, 1, 2, 3... Stars are
// Jellyfin's favourites on their own channels, so every app shows them.
// A change saves by itself a moment later; Tentacle then sets it up in
// Jellyfin (a minute or so before the TV shows it).
//
// Channel names come from the provider: they are text, never markup (every
// row is built with escHtml, and rows carry only their key in data-key).

const mych = {
  data: null,        // GET /api/my/channels
  ownList: false,
  list: [],          // keys of my own list, in order
  saveTimer: null,
  saving: false,
  dirty: false,
  phoneTab: 'list',
  dragKey: null,
};

function _mychByKey() {
  const m = {};
  for (const c of (mych.data?.channels || [])) m[c.key] = c;
  return m;
}

// The sidebar link is there once the household has any Live TV channel.
async function mychCheckNav() {
  const nav = document.getElementById('nav-mychannels');
  if (!nav) return;
  try {
    const d = await api('/api/my/channels');
    nav.style.display = d.available ? '' : 'none';
  } catch (e) {
    nav.style.display = 'none';
  }
}

async function loadMyChannels() {
  try {
    mych.data = await api('/api/my/channels');
  } catch (e) {
    toast('Your channels could not be loaded: ' + e.message, 'error');
    return;
  }
  mych.ownList = !!mych.data.own_list;
  mych.list = (mych.data.my_list || []).slice();
  mych.dirty = false;
  _mychStatus(mych.data.setting_up ? 'busy' : '', mych.data.setting_up ? 'Setting up your channels in Jellyfin…' : '');
  mychRender();
}

function mychRender() {
  const d = mych.data;
  const has = d && d.available;
  document.getElementById('mych-empty').style.display = has ? 'none' : '';
  document.querySelector('#page-mychannels .mych-head').style.display = has ? '' : 'none';
  if (!has) {
    document.getElementById('mych-all').style.display = 'none';
    document.getElementById('mych-own').style.display = 'none';
    return;
  }
  const all = document.getElementById('mych-mode-all');
  const own = document.getElementById('mych-mode-own');
  all.classList.toggle('active', !mych.ownList);
  own.classList.toggle('active', mych.ownList);
  all.setAttribute('aria-checked', String(!mych.ownList));
  own.setAttribute('aria-checked', String(mych.ownList));
  document.getElementById('mych-count-all').textContent = d.total;
  document.getElementById('mych-count-own').textContent = mych.ownList || mych.list.length ? mych.list.length : '';
  document.getElementById('mych-all').style.display = mych.ownList ? 'none' : '';
  const ownEl = document.getElementById('mych-own');
  ownEl.style.display = mych.ownList ? '' : 'none';
  ownEl.classList.toggle('tab-list', mych.phoneTab === 'list');
  ownEl.classList.toggle('tab-add', mych.phoneTab === 'add');
  if (mych.ownList) { mychRenderOwn(); mychRenderAdd(); } else { mychRenderAll(); }
}

function _mychLogo(c) {
  return c.logo_url
    ? `<img class="mych-logo" src="${escHtml(c.logo_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.className='mych-logo none';this.removeAttribute('src')">`
    : '<span class="mych-logo none"></span>';
}

function _mychStar(c) {
  const on = !!c.favourite;
  const title = on ? 'Remove from favourites' : 'Add to favourites';
  return `<button class="mych-icon mych-star${on ? ' on' : ''}" data-act="star" title="${title}" aria-label="${title}" aria-pressed="${on}">${on ? '★' : '☆'}</button>`;
}

function _mychMatches(c, q) {
  return !q || String(c.name || '').toLowerCase().includes(q);
}

function mychRenderAll() {
  const el = document.getElementById('mych-all-list');
  const q = (document.getElementById('mych-all-search').value || '').trim().toLowerCase();
  const rows = (mych.data.channels || []).filter(c => _mychMatches(c, q));
  if (!rows.length) { el.innerHTML = `<div class="mych-none">${q ? 'No channel matches.' : 'No channels yet.'}</div>`; return; }
  const num = {};
  (mych.data.channels || []).forEach((c, i) => { num[c.key] = i + 1; });
  el.innerHTML = rows.map(c => `
    <div class="mych-row" data-key="${escHtml(c.key)}">
      <span class="mych-num">${num[c.key]}</span>${_mychLogo(c)}
      <span class="mych-name" title="${escHtml(c.name)}">${escHtml(c.name)}</span>${_mychStar(c)}
    </div>`).join('');
}

function mychRenderOwn() {
  const el = document.getElementById('mych-own-list');
  const by = _mychByKey();
  const keys = mych.list.filter(k => by[k]);
  document.getElementById('mych-list-count').textContent = keys.length;
  if (!keys.length) {
    el.innerHTML = '<div class="mych-none">Your list is empty. Add the channels you watch.</div>';
    return;
  }
  el.innerHTML = keys.map((k, i) => {
    const c = by[k];
    return `
    <div class="mych-row" data-key="${escHtml(k)}" draggable="true">
      <span class="mych-grip" aria-hidden="true">⋮⋮</span>
      <span class="mych-num">${i + 1}</span>${_mychLogo(c)}
      <span class="mych-name" title="${escHtml(c.name)}">${escHtml(c.name)}</span>${_mychStar(c)}
      <span class="mych-arrows">
        <button class="mych-icon" data-act="up" title="Move up" aria-label="Move ${escHtml(c.name)} up"${i === 0 ? ' disabled' : ''}>↑</button>
        <button class="mych-icon" data-act="down" title="Move down" aria-label="Move ${escHtml(c.name)} down"${i === keys.length - 1 ? ' disabled' : ''}>↓</button>
      </span>
      <button class="mych-icon" data-act="remove" title="Remove from my list" aria-label="Remove ${escHtml(c.name)}">✕</button>
    </div>`;
  }).join('');
}

function mychRenderAdd() {
  const el = document.getElementById('mych-add-list');
  const q = (document.getElementById('mych-add-search').value || '').trim().toLowerCase();
  const inList = new Set(mych.list);
  const rows = (mych.data.channels || []).filter(c => _mychMatches(c, q));
  if (!rows.length) { el.innerHTML = `<div class="mych-none">${q ? 'No channel matches.' : 'No channels yet.'}</div>`; return; }
  el.innerHTML = rows.map(c => `
    <div class="mych-row" data-key="${escHtml(c.key)}">
      ${_mychLogo(c)}<span class="mych-name" title="${escHtml(c.name)}">${escHtml(c.name)}</span>
      ${inList.has(c.key)
        ? '<span class="mych-added">✓ In my list</span>'
        : `<button class="btn btn-secondary btn-sm" data-act="add" aria-label="Add ${escHtml(c.name)}">+ Add</button>`}
    </div>`).join('');
}

function mychPhoneTab(tab) {
  mych.phoneTab = tab;
  document.querySelectorAll('[data-mychtab]').forEach(b => b.classList.toggle('active', b.dataset.mychtab === tab));
  const ownEl = document.getElementById('mych-own');
  ownEl.classList.toggle('tab-list', tab === 'list');
  ownEl.classList.toggle('tab-add', tab === 'add');
}

// Switching to your own list the first time starts it with your favourites.
function mychSetMode(own) {
  if (own === mych.ownList) return;
  mych.ownList = own;
  if (own && !mych.list.length) {
    mych.list = (mych.data.channels || []).filter(c => c.favourite).map(c => c.key);
    if (!mych.list.length) mychPhoneTab('add');
  }
  mychRender();
  _mychQueueSave(0);
}

function _mychChanged() {
  mychRenderOwn();
  mychRenderAdd();
  document.getElementById('mych-count-own').textContent = mych.list.length;
  _mychQueueSave(900);
}

function _mychStatus(kind, text) {
  const el = document.getElementById('mych-status');
  if (!el) return;
  el.className = 'mych-status' + (kind ? ' ' + kind : '');
  el.textContent = text || '';
}

function _mychQueueSave(delay) {
  mych.dirty = true;
  if (mych.saveTimer) clearTimeout(mych.saveTimer);
  _mychStatus('busy', 'Saving…');
  mych.saveTimer = setTimeout(_mychSave, delay);
}

async function _mychSave() {
  mych.saveTimer = null;
  if (mych.saving) { _mychQueueSave(400); return; }
  mych.saving = true;
  mych.dirty = false;
  try {
    const d = await api('/api/my/channels', { method: 'PUT', body: { own_list: mych.ownList, keys: mych.list } });
    // Keep what was changed while this save was on its way.
    if (!mych.dirty) { mych.data = d; mych.list = (d.my_list || []).slice(); mychRender(); }
    _mychStatus('ok', 'Saved. Your TV and apps show it in about a minute.');
  } catch (e) {
    if (!e.sessionExpired) _mychStatus('', 'Not saved: ' + e.message);
  } finally {
    mych.saving = false;
  }
}

async function _mychStarToggle(key) {
  const c = _mychByKey()[key];
  if (!c) return;
  const want = !c.favourite;
  c.favourite = want;
  mychRender();
  try {
    await api('/api/my/channels/favourite', { method: 'PUT', body: { key, favourite: want } });
  } catch (e) {
    c.favourite = !want;
    mychRender();
    if (!e.sessionExpired) toast(e.message, 'warning', 5000);
  }
}

function _mychMove(key, delta) {
  const i = mych.list.indexOf(key);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= mych.list.length) return;
  [mych.list[i], mych.list[j]] = [mych.list[j], mych.list[i]];
  _mychChanged();
}

function _mychOnClick(ev) {
  const btn = ev.target.closest('button[data-act]');
  const row = ev.target.closest('.mych-row');
  if (!btn || !row) return;
  const key = row.dataset.key;
  const act = btn.dataset.act;
  if (act === 'star') _mychStarToggle(key);
  else if (act === 'add') { if (!mych.list.includes(key)) mych.list.push(key); _mychChanged(); }
  else if (act === 'remove') { mych.list = mych.list.filter(k => k !== key); _mychChanged(); }
  else if (act === 'up') _mychMove(key, -1);
  else if (act === 'down') _mychMove(key, 1);
}

// Drag a row of your list to another place.
function _mychDragSetup(listEl) {
  listEl.addEventListener('dragstart', ev => {
    const row = ev.target.closest('.mych-row');
    if (!row) return;
    mych.dragKey = row.dataset.key;
    row.classList.add('dragging');
    ev.dataTransfer.effectAllowed = 'move';
    try { ev.dataTransfer.setData('text/plain', mych.dragKey); } catch (e) {}
  });
  listEl.addEventListener('dragover', ev => {
    if (!mych.dragKey) return;
    ev.preventDefault();
    listEl.querySelectorAll('.drop-before').forEach(r => r.classList.remove('drop-before'));
    const row = ev.target.closest('.mych-row');
    if (row && row.dataset.key !== mych.dragKey) row.classList.add('drop-before');
  });
  listEl.addEventListener('drop', ev => {
    ev.preventDefault();
    const row = ev.target.closest('.mych-row');
    const from = mych.dragKey;
    mych.dragKey = null;
    if (!from) return;
    const rest = mych.list.filter(k => k !== from);
    const at = row ? rest.indexOf(row.dataset.key) : rest.length;
    rest.splice(at < 0 ? rest.length : at, 0, from);
    mych.list = rest;
    _mychChanged();
  });
  listEl.addEventListener('dragend', () => {
    mych.dragKey = null;
    listEl.querySelectorAll('.dragging, .drop-before').forEach(r => r.classList.remove('dragging', 'drop-before'));
  });
}

document.addEventListener('DOMContentLoaded', () => {
  for (const id of ['mych-all-list', 'mych-own-list', 'mych-add-list']) {
    document.getElementById(id)?.addEventListener('click', _mychOnClick);
  }
  const own = document.getElementById('mych-own-list');
  if (own) _mychDragSetup(own);
});

// ── Admins: Sources → Live TV → People and In Jellyfin ─────────────────────

function _mychWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

async function loadLivePeople() {
  const el = document.getElementById('live-people');
  if (!el) return;
  let d;
  try { d = await api('/api/live/people'); } catch (e) { el.innerHTML = `<div class="mych-none">${escHtml(e.message)}</div>`; return; }
  if (!d.people.length) { el.innerHTML = '<div class="mych-none">Nobody has signed in yet.</div>'; return; }
  el.innerHTML = `<table class="mych-people"><thead><tr><th>Person</th><th>Sees</th><th>Last change</th></tr></thead><tbody>${
    d.people.map(p => `<tr><td>${escHtml(p.name)}</td><td>${p.own_list
      ? `Own list <span class="muted">· ${p.count} channel${p.count === 1 ? '' : 's'}</span>`
      : `All channels <span class="muted">· ${p.count}</span>`}</td><td class="muted">${escHtml(_mychWhen(p.changed)) || '—'}</td></tr>`).join('')
  }</tbody></table>`;
}

async function loadLiveSetup() {
  const el = document.getElementById('live-setup-status');
  if (!el) return;
  let d;
  try { d = await api('/api/live/people'); } catch (e) { el.textContent = e.message; return; }
  const s = d.setup || {};
  const own = d.people.filter(p => p.own_list).length;
  const line = s.running ? '<span style="color:var(--amber)">Setting up now…</span>'
    : s.ok === false ? `<span style="color:var(--red)">The last try didn&rsquo;t work: ${escHtml(s.message)}</span>`
    : s.message ? `<span style="color:var(--amber)">${escHtml(s.message)}</span>`
    : s.last_at ? `<span style="color:var(--green)">✓ Set up</span> <span style="color:var(--text3)">· checked ${escHtml(_mychWhen(s.last_at))}</span>`
    : '<span style="color:var(--text3)">Not checked yet since Tentacle started (it checks within a few minutes, then every hour).</span>';
  el.innerHTML = `${line}<br><span style="color:var(--text2)">${d.people.length - own} on All channels (${d.total} channels), ${own} with a list of their own.</span>`;
}

async function liveSetupNow(btn) {
  if (btn) btn.disabled = true;
  try {
    await api('/api/live/setup', { method: 'POST' });
    toast('Setting up Live TV in Jellyfin. This takes a minute or two.', 'info', 5000);
    setTimeout(loadLiveSetup, 1500);
  } catch (e) {
    if (!e.sessionExpired) toast(e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}
