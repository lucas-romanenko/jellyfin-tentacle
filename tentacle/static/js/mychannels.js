// Tentacle - My Channels (everyone): my favourite Live TV channels.
//
// The household has one lineup, the admin's (channel groups switched on per
// provider, folded so one network shows once): one tuner and one guide in
// Jellyfin, the same channels and numbers for everyone. What is personal is
// the view: each person stars their favourites, which are Jellyfin's own
// favourites, so every app shows them and the guide's Favorites view is
// those. Quick picks (Sports, News…) star or unstar a type's best-known
// channels at once. A kids profile sees only the household's kids channels
// and stars among those.
//
// Channel names come from the provider: they are text, never markup (every
// row is built with escHtml, and rows carry only their key in data-key).

const mych = {
  data: null,        // GET /api/my/channels
  phoneTab: 'list',
  busy: 0,           // star changes on their way to Jellyfin
  allLimit: 150,     // rows of "All channels" drawn before "Show all"
};

const MYCH_PAGE = 150;
// Quick picks come first in this order, then any other type the channels have.
const MYCH_TYPE_ORDER = ['Sports', 'News', 'Movies', 'Kids', 'Entertainment', 'Local', 'Documentary',
  'Lifestyle', 'Music', 'International', 'Religious', 'Event/PPV'];
const MYCH_TYPE_LABEL = { 'Event/PPV': 'Events & PPV' };

function _mychByKey() {
  const m = {};
  for (const c of (mych.data?.channels || [])) m[c.key] = c;
  return m;
}

function _mychIsAdmin() {
  return !!(typeof state !== 'undefined' && state.currentUser && state.currentUser.is_admin);
}

// The channels to star: event slots with nothing on are left out, channels
// that don't work go to the bottom; otherwise the lineup's order.
function _mychChoosable() {
  const rows = (mych.data?.channels || []).filter(c => c.event !== 'no_event');
  const broken = c => (c.check && c.check.state === 'broken') ? 1 : 0;
  return rows.map((c, i) => [c, i]).sort((a, b) => broken(a[0]) - broken(b[0]) || a[1] - b[1]).map(p => p[0]);
}

// My favourites, in the lineup's order.
function _mychFavs() {
  return (mych.data?.channels || []).filter(c => c.favourite);
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
  let d;
  try {
    d = await api('/api/my/channels');
  } catch (e) {
    toast('Your channels could not be loaded: ' + e.message, 'error');
    return;
  }
  mych.data = d;
  mych.allLimit = MYCH_PAGE;
  _mychSetupNote();
  mychRender();
}

// Stars live in Jellyfin: until it has the channels, they can't be set.
function _mychSetupNote() {
  const d = mych.data || {};
  if (d.available && (!d.favourites_known || d.setting_up)) {
    _mychStatus('busy', 'Live TV is being set up in Jellyfin. Stars can be set in a minute or two.');
  } else {
    _mychStatus('', '');
  }
}

function mychRender() {
  const d = mych.data;
  const has = d && d.available;
  document.getElementById('mych-empty').style.display = has ? 'none' : '';
  document.getElementById('mych-body').style.display = has ? '' : 'none';
  if (!has) return;
  // A kids profile: the kids channels an admin picked, to star among.
  const kids = !!d.kids;
  document.getElementById('mych-sub').textContent = kids
    ? `${d.kids.chosen_by} picked these channels for you. Star the ones you watch: they come first in the guide.`
    : 'Everyone has the same channels. Star the ones you watch: your favourites come first in the guide’s Favorites view and in every Jellyfin app.'
      + (d.location && d.location.own ? ` Your local channels (CTV, Global, CBC, NBC…) are ${d.location.city}’s.` : '');
  document.getElementById('mych-picks-card').style.display = kids ? 'none' : '';
  document.getElementById('mych-all-title').textContent = kids ? 'Your channels' : 'All channels';
  document.getElementById('mych-all-tab').textContent = kids ? 'Your channels' : 'All channels';
  document.getElementById('mych-all-hint').textContent = kids
    ? `The kids channels ${d.kids.chosen_by} picked. Tap ☆ to star one.`
    : 'Everything the household has, with the guide’s numbers. Tap ☆ to star one.';
  mychPhoneTab(mych.phoneTab);
  _mychTypeOptions();
  _mychCounts();
  mychRenderPicks();
  mychRenderFavs();
  mychRenderAll();
}

function _mychCounts() {
  const n = _mychFavs().length;
  const all = _mychChoosable().length;
  document.getElementById('mych-total').textContent = `· ${n} favourite${n === 1 ? '' : 's'} of ${all} channels`;
  document.getElementById('mych-fav-count').textContent = n;
  const tab = document.getElementById('mych-tab-count');
  if (tab) tab.textContent = n;
  const allTab = document.getElementById('mych-all-count');
  if (allTab) allTab.textContent = all;
}

function _mychTypeOptions() {
  const sel = document.getElementById('mych-all-type');
  if (!sel) return;
  const have = new Set();
  for (const c of _mychChoosable()) for (const t of (c.types || [])) have.add(t);
  const types = MYCH_TYPE_ORDER.filter(t => have.has(t)).concat([...have].filter(t => !MYCH_TYPE_ORDER.includes(t)).sort());
  const fresh = _mychChoosable().filter(c => c.new).length;
  sel.style.display = types.length || fresh ? '' : 'none';
  const cur = sel.value;
  sel.innerHTML = '<option value="">All types</option>' +
    (fresh ? `<option value="${MYCH_NEW}">New (${fresh})</option>` : '') +
    types.map(t => `<option value="${escHtml(t)}">${escHtml(MYCH_TYPE_LABEL[t] || t)}</option>`).join('');
  sel.value = types.includes(cur) || (fresh && cur === MYCH_NEW) ? cur : '';
  const line = document.getElementById('mych-new-line');
  if (line) {
    line.style.display = fresh ? '' : 'none';
    line.innerHTML = fresh ? `<span class="mych-new" style="margin:0 6px 0 0">New</span>${fresh === 1 ? '1 channel was' : `${fresh} channels were`} added lately. <a href="#" onclick="mychShowNew();return false" style="color:var(--accent-text)">Show ${fresh === 1 ? 'it' : 'them'}</a>` : '';
  }
}

// The channels added to the household's channels lately (marked New).
const MYCH_NEW = '__new';

function mychShowNew() {
  const sel = document.getElementById('mych-all-type');
  if (!sel) return;
  sel.value = MYCH_NEW;
  mychPhoneTab('all');   // phone: the All channels tab
  mychRenderAll();
  document.getElementById('mych-all-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── Quick picks ─────────────────────────────────────────────────────────────

// A quick pick stars a type's short list, not every channel of the type: the
// server marks each type's best channels (main_types, at most 25 a type:
// known networks, with a guide, that work, in the admin's country). Every
// channel of the type is under All channels (the type filter).
function _mychPicks() {
  const by = {};
  for (const c of _mychChoosable()) {
    for (const t of (c.main_types || [])) (by[t] = by[t] || []).push(c);
  }
  const types = MYCH_TYPE_ORDER.filter(t => by[t]).concat(Object.keys(by).filter(t => !MYCH_TYPE_ORDER.includes(t)).sort());
  return types.map(t => {
    const starred = by[t].filter(c => c.favourite).length;
    // On while at least half of its channels are starred (unstarring a few
    // keeps it on); tapping it then unstars them.
    return { type: t, channels: by[t], starred, on: starred > 0 && starred * 2 >= by[t].length };
  });
}

function mychRenderPicks() {
  const el = document.getElementById('mych-picks');
  if (!el) return;
  const picks = _mychPicks();
  el.innerHTML = picks.map((p, i) => {
    const label = MYCH_TYPE_LABEL[p.type] || p.type;
    const n = p.channels.length;
    const title = p.on ? `Unstar the ${p.starred} ${label} channel${p.starred === 1 ? '' : 's'}`
      : `Star ${n} popular ${label} channel${n === 1 ? '' : 's'}`;
    // On a phone the first six show; "More types" shows the rest.
    const extra = i >= 6 && !p.on ? ' extra' : '';
    return `<button class="mych-pick${p.on ? ' on' : ''}${extra}" data-type="${escHtml(p.type)}" aria-pressed="${p.on}" title="${escHtml(title)}">`
      + `<span class="star" aria-hidden="true">${p.on ? '★' : '☆'}</span> ${escHtml(label)} `
      + `<span class="n">${p.on ? `${p.starred} starred` : n}</span>`
      + '</button>';
  }).join('') + (picks.length > 6 && !el.classList.contains('all')
    ? '<button class="mych-pick more" data-more="1">More types…</button>' : '');
}

// The keys a tap on a quick pick changes, and whether it stars or unstars them.
function _mychPickChange(type) {
  const picks = _mychPicks();
  const pick = picks.find(p => p.type === type);
  if (!pick) return null;
  if (pick.on) {
    // Unstar what this pick starred, but not what another pick that is on has too.
    const keep = new Set();
    for (const p of picks) if (p.on && p.type !== type) for (const c of p.channels) keep.add(c.key);
    return { favourite: false, keys: pick.channels.filter(c => c.favourite && !keep.has(c.key)).map(c => c.key) };
  }
  return { favourite: true, keys: pick.channels.filter(c => !c.favourite).map(c => c.key) };
}

async function mychPick(type) {
  const change = _mychPickChange(type);
  if (!change) return;
  const label = MYCH_TYPE_LABEL[type] || type;
  const n = change.keys.length;
  if (!n) return;
  const by = _mychByKey();
  for (const k of change.keys) if (by[k]) by[k].favourite = change.favourite;
  _mychChanged();
  const ok = change.favourite ? `Starred ${n} ${label} channel${n === 1 ? '' : 's'}. More under All channels.`
    : `Unstarred ${n} ${label} channel${n === 1 ? '' : 's'}`;
  await _mychSend('/api/my/channels/favourites', { keys: change.keys, favourite: change.favourite }, () => {
    for (const k of change.keys) if (by[k]) by[k].favourite = !change.favourite;
  }, ok);
}

// ── Rows ────────────────────────────────────────────────────────────────────

function _mychLogo(c) {
  return c.logo_url
    ? `<img class="mych-logo" src="${escHtml(c.logo_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.className='mych-logo none';this.removeAttribute('src')">`
    : '<span class="mych-logo none"></span>';
}

function _mychStar(c) {
  const on = !!c.favourite;
  const title = on ? `Unstar ${c.name}` : `Star ${c.name}`;
  return `<button class="mych-icon mych-star${on ? ' on' : ''}" data-act="star" title="${on ? 'Unstar' : 'Star'}" aria-label="${escHtml(title)}" aria-pressed="${on}">${on ? '★' : '☆'}</button>`;
}

function _mychMatches(c, q) {
  if (!q) return true;
  return String(c.name || '').toLowerCase().includes(q) || (c.number != null && String(c.number) === q);
}

// "2 h ago", for when a channel was last checked.
function _mychAgo(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '';
  const min = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (min < 1) return 'just now';
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

// An event's start: "Today 19:00", "Tomorrow 20:00", "Fri 23 Oct 19:00".
function _mychWhenEvent(iso) {
  const d = new Date(iso || '');
  if (isNaN(d)) return '';
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((day - today) / 86400000);
  if (diff === 0) return `Today ${time}`;
  if (diff === 1) return `Tomorrow ${time}`;
  return `${d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })} ${time}`;
}

// Where a folded network streams from (admins only): "from NBC 2 Buffalo · 16 versions".
function _mychVia(c) {
  if (!_mychIsAdmin() || !(c.variants > 1)) return '';
  return ` <span class="via">from ${escHtml(c.variant_name || '')} · ${c.variants} versions</span>`;
}

function _mychWorks(c) {
  const s = c.check && c.check.state;
  const when = c.check && c.check.at ? ` <span class="when">(checked ${escHtml(_mychAgo(c.check.at))})</span>` : '';
  if (s === 'works') return `<span><span class="ok">✓ Works</span>${when}</span>`;
  if (s === 'broken') return `<span><span class="bad">✗ Not working</span>${when}</span>`;
  if (s === 'unchecked') return '<span>Not checked yet</span>';
  return '';
}

function _mychGuide(c) {
  const ev = c.event && typeof c.event === 'object' ? c.event : null;
  if (ev) return `<span class="ev">▶ ${escHtml(ev.title || '')}${ev.start ? ' · ' + escHtml(_mychWhenEvent(ev.start)) : ''}</span>`;
  if (c.event === 'no_event') return '<span>No event</span>';
  if (c.has_guide === undefined) return '';
  return c.has_guide ? '<span class="ok">Guide ✓</span>' : '<span>No guide</span>';
}

function _mychMeta(parts) {
  const items = parts.filter(Boolean);
  return items.length ? `<span class="mych-meta">${items.join('')}</span>` : '';
}

function _mychRow(c, meta, via = true) {
  const broken = c.check && c.check.state === 'broken';
  return `
    <div class="mych-row${c.favourite ? ' starred' : ''}${broken ? ' broken' : ''}" data-key="${escHtml(c.key)}">
      <span class="mych-num">${c.number != null ? escHtml(String(c.number)) : ''}</span>${_mychLogo(c)}
      <span class="mych-text"><span class="mych-name" title="${escHtml(c.name)}">${escHtml(c.name)}${c.new ? '<span class="mych-new" title="Added to the household’s channels in the last two weeks">New</span>' : ''}${via ? _mychVia(c) : ''}</span>${meta}</span>${_mychStar(c)}
    </div>`;
}

function mychRenderFavs() {
  const el = document.getElementById('mych-fav-list');
  const favs = _mychFavs();
  if (!favs.length) {
    el.innerHTML = `<div class="mych-none">${mych.data?.kids
      ? 'No favourites yet. Star the channels you watch.'
      : 'No favourites yet. Tap a quick pick above, or star channels under All channels.'}</div>`;
    return;
  }
  el.innerHTML = favs.map(c => {
    const ev = c.event && typeof c.event === 'object' ? _mychGuide(c) : '';
    const broken = c.check && c.check.state === 'broken' ? '<span class="bad">✗ Not working</span>' : '';
    return _mychRow(c, _mychMeta([ev, broken]), false);
  }).join('');
}

function mychRenderAll() {
  const el = document.getElementById('mych-all-list');
  const q = (document.getElementById('mych-all-search').value || '').trim().toLowerCase();
  const type = document.getElementById('mych-all-type')?.value || '';
  const rows = _mychChoosable().filter(c => _mychMatches(c, q)
    && (!type || (type === MYCH_NEW ? c.new : (c.types || []).includes(type))));
  if (!rows.length) { el.innerHTML = `<div class="mych-none">${q || type ? 'No channel matches.' : 'No channels yet.'}</div>`; return; }
  const shown = rows.slice(0, mych.allLimit);
  el.innerHTML = shown.map(c => _mychRow(c, _mychMeta([_mychGuide(c), _mychWorks(c)]))).join('')
    + (rows.length > shown.length ? `<button class="mych-more" data-act="more">Show all ${rows.length} channels</button>` : '');
}

function mychPhoneTab(tab) {
  mych.phoneTab = tab;
  document.querySelectorAll('[data-mychtab]').forEach(b => b.classList.toggle('active', b.dataset.mychtab === tab));
  const el = document.getElementById('mych-own');
  el.classList.toggle('tab-list', tab === 'list');
  el.classList.toggle('tab-all', tab === 'all');
}

function _mychChanged() {
  _mychCounts();
  mychRenderPicks();
  mychRenderFavs();
  mychRenderAll();
}

function _mychStatus(kind, text) {
  const el = document.getElementById('mych-status');
  if (!el) return;
  el.className = 'mych-status' + (kind ? ' ' + kind : '');
  el.textContent = text || '';
}

// Send a star change to Jellyfin; undo() puts the stars back if it fails.
async function _mychSend(path, body, undo, ok) {
  mych.busy++;
  _mychStatus('busy', 'Saving…');
  try {
    const d = await api(path, { method: 'PUT', body });
    mych.busy--;
    // A quick pick answers with the whole page: take it once nothing else is on its way.
    if (d && d.channels && !mych.busy) { mych.data = d; _mychChanged(); }
    if (!mych.busy) _mychStatus('ok', 'Saved. Every Jellyfin app shows your favourites.');
    if (ok) toast(ok, 'success', 2500);
  } catch (e) {
    mych.busy--;
    undo();
    _mychChanged();
    _mychStatus('', '');
    if (!e.sessionExpired) toast(e.message, 'warning', 5000);
  }
}

function _mychStarToggle(key) {
  const c = _mychByKey()[key];
  if (!c) return;
  const want = !c.favourite;
  c.favourite = want;
  _mychChanged();
  return _mychSend('/api/my/channels/favourite', { key, favourite: want }, () => { c.favourite = !want; });
}

function _mychOnClick(ev) {
  const pick = ev.target.closest('.mych-pick');
  if (pick && pick.dataset.more) { document.getElementById('mych-picks').classList.add('all'); mychRenderPicks(); return; }
  if (pick) { mychPick(pick.dataset.type); return; }
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  if (btn.dataset.act === 'more') { mych.allLimit = Infinity; mychRenderAll(); return; }
  const row = ev.target.closest('.mych-row');
  if (row && btn.dataset.act === 'star') _mychStarToggle(row.dataset.key);
}

document.addEventListener('DOMContentLoaded', () => {
  for (const id of ['mych-picks', 'mych-fav-list', 'mych-all-list']) {
    document.getElementById(id)?.addEventListener('click', _mychOnClick);
  }
  document.getElementById('mych-all-search')?.addEventListener('input', () => { mych.allLimit = MYCH_PAGE; });
  document.getElementById('pool-list')?.addEventListener('click', _poolOnClick);
  document.getElementById('pool-list')?.addEventListener('change', _poolOnChange);
});

// ── Admins: Sources → Live TV → Channels (the household's lineup) ──────────
//
// Folded networks with the variant that feeds them (changeable), splitting a
// wrong fold and merging a missed one, taking single channels or networks out,
// correcting a channel's types, and the guide / works marks.

const pool = {
  entries: [],       // GET /api/live/catalog/pool
  nets: {},          // network id -> GET /api/live/catalog/networks entry
  types: [],
  open: null,        // key of the row whose details are open
  selected: new Set(),
  limit: 200,
};

async function loadLivePool() {
  const el = document.getElementById('pool-list');
  if (!el) return;
  loadPoolChecker();
  try {
    const [p, n, t] = await Promise.all([
      api('/api/live/catalog/pool'), api('/api/live/catalog/networks'), api('/api/live/catalog/types')]);
    pool.entries = p.channels || [];
    pool.nets = {};
    for (const net of (n.networks || [])) pool.nets[net.id] = net;
    pool.types = t.types || [];
  } catch (e) {
    el.innerHTML = `<div class="mych-none">${escHtml(e.message)}</div>`;
    return;
  }
  const sel = document.getElementById('pool-type');
  if (sel && sel.options.length <= 1) {
    sel.innerHTML = '<option value="">All types</option>' +
      pool.types.map(t => `<option value="${escHtml(t)}">${escHtml(MYCH_TYPE_LABEL[t] || t)}</option>`).join('');
  }
  poolRender();
  _poolLoadRegion();
}

async function loadPoolChecker() {
  const el = document.getElementById('pool-checker');
  if (!el) return;
  let s;
  try { s = await api('/api/live/checker/status'); } catch (e) { el.textContent = ''; el.style.display = 'none'; return; }
  el.style.display = '';
  const doing = s.state === 'checking' ? `checking ${escHtml(s.current?.name || 'a channel')}`
    : s.state === 'paused' ? `paused: ${escHtml(s.paused_reason || 'someone is watching')}`
    : s.state === 'backoff' ? 'waiting: the provider asked to slow down'
    : s.state === 'stopped' ? 'stopped'
    : 'waiting for the next check';
  const dot = s.state === 'checking' ? 'on' : s.state === 'paused' || s.state === 'backoff' ? 'paused' : '';
  const t = s.pool || {};
  el.innerHTML = `<span><span class="dot ${dot}"></span><strong>Stream checker</strong>: ${doing}</span>`
    + `<span><span style="color:var(--green)">${t.works || 0} work</span> · <span style="color:var(--red)">${t.broken || 0} not working</span> · ${t.unchecked || 0} not checked yet</span>`
    + (s.last_checked_at ? `<span>last check ${escHtml(_mychAgo(s.last_checked_at))}</span>` : '')
    + '<span class="note" style="color:var(--text3)">One channel at a time, only while nobody watches or records.</span>';
}

function _poolNet(e) {
  return e.key[0] === 'n' ? pool.nets[parseInt(e.key.slice(1), 10)] : null;
}

function _poolFiltered() {
  const q = (document.getElementById('pool-search')?.value || '').trim().toLowerCase();
  const type = document.getElementById('pool-type')?.value || '';
  const show = document.getElementById('pool-show')?.value || '';
  if (show === 'out') {
    return Object.values(pool.nets).filter(n => n.excluded)
      .map(n => ({ key: n.key, name: n.name, out: true, variants: n.count, types: n.types || [] }))
      .filter(e => !q || e.name.toLowerCase().includes(q));
  }
  const broken = e => (e.check && e.check.state === 'broken') ? 1 : 0;
  return pool.entries.filter(e => {
    if (q && !(`${e.name} ${e.variant_name || ''}`.toLowerCase().includes(q))) return false;
    if (type && !(e.types || []).includes(type)) return false;
    if (show === 'networks') return e.key[0] === 'n';
    if (show === 'broken') return broken(e);
    if (show === 'noguide') return !e.has_guide && !(e.event && typeof e.event === 'object');
    if (show === 'noevent') return e.event === 'no_event';
    return true;
  }).map((e, i) => [e, i]).sort((a, b) => broken(a[0]) - broken(b[0]) || a[1] - b[1]).map(p => p[0]);
}

function _poolVariantLabel(v) {
  const works = v.check?.state === 'works' ? '✓ Works' : v.check?.state === 'broken' ? '✗ Not working' : 'Not checked';
  return `${v.name} · ${v.has_guide ? 'Guide ✓' : 'No guide'} · ${works}${v.enabled ? '' : ' · off'}`;
}

function _poolVariantSelect(net) {
  const rank = v => (v.check?.state === 'works' ? 0 : v.check?.state === 'broken' ? 2 : 1) + (v.enabled ? 0 : 3);
  const vars = net.variants.slice().sort((a, b) => rank(a) - rank(b));
  return `<select class="form-input pool-variant" data-act="variant" aria-label="Which ${escHtml(net.name)} feeds the channel">`
    + vars.map(v => `<option value="${v.channel_id}"${v.chosen ? ' selected' : ''}>${escHtml(_poolVariantLabel(v))}</option>`).join('')
    + `<option value="">Let Tentacle choose</option></select>`;
}

function _poolTypes(e) {
  return `<span class="pool-types">${(e.types || []).map(t =>
    `<button class="pool-type" data-act="details" title="Change the types">${escHtml(MYCH_TYPE_LABEL[t] || t)}</button>`).join('')}</span>`;
}

function _poolRow(e) {
  const net = _poolNet(e);
  const isNet = !!net;
  const via = isNet && net.count > 1
    ? `<span class="via">from ${escHtml(e.variant_name || '')} · ${net.count} versions</span>` : '';
  const meta = e.out ? _mychMeta(['<span>Taken out: people don’t see it</span>'])
    : _mychMeta([via, _mychGuide(e), _mychWorks(e), _poolTypes(e),
      (e.main_types || []).length ? `<span class="ev" title="A quick pick adds it">In quick pick: ${escHtml(e.main_types.map(t => MYCH_TYPE_LABEL[t] || t).join(', '))}</span>` : '']);
  const selectable = e.key[0] === 'n' || e.key[0] === 'c';
  const actions = e.out
    ? `<button class="btn btn-secondary btn-sm" data-act="putback">Put back</button>`
    : `${isNet && net.variants.length > 1 ? _poolVariantSelect(net) : ''}
       ${selectable ? `<button class="btn btn-secondary btn-sm" data-act="details" aria-expanded="${pool.open === e.key}">${pool.open === e.key ? 'Close' : 'Edit'}</button>` : ''}
       ${selectable ? `<button type="button" class="live-toggle on" data-act="takeout" aria-label="${escHtml(e.name)} is in the household’s channels" aria-pressed="true" title="Take out of the household’s channels"></button>` : ''}`;
  return `
    <div class="pool-row${e.out ? ' out' : ''}" data-key="${escHtml(e.key)}">
      ${selectable && !e.out ? `<input type="checkbox" data-act="select" aria-label="Select ${escHtml(e.name)} to merge"${pool.selected.has(e.key) ? ' checked' : ''}>` : ''}
      ${_mychLogo(e)}
      <span class="mych-text"><span class="mych-name" title="${escHtml(e.provider_name || e.name)}"><span class="pool-name">${escHtml(e.name)}</span>${e.provider_name && e.provider_name !== e.name && e.key[0] === 'c' ? ` <span class="via">${escHtml(e.provider_name)}</span>` : ''}</span>${meta}</span>
      <span class="pool-actions">${actions}</span>
    </div>${pool.open === e.key ? _poolDetail(e, net) : ''}`;
}

function _poolDetail(e, net) {
  const types = new Set(e.types || []);
  let html = `<div class="pool-detail" data-key="${escHtml(e.key)}">
    <h4>Types (people&rsquo;s quick picks)</h4>
    <div class="pool-typepick">${pool.types.map(t =>
      `<label><input type="checkbox" value="${escHtml(t)}"${types.has(t) ? ' checked' : ''}> ${escHtml(MYCH_TYPE_LABEL[t] || t)}</label>`).join('')}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      <button class="btn btn-primary btn-sm" data-act="types">Save types</button>
      <button class="btn btn-secondary btn-sm" data-act="types-auto">Automatic types</button>
    </div>`;
  if (net) {
    html += `<h4>${net.count} version${net.count === 1 ? '' : 's'} folded into ${escHtml(net.name)}</h4>
    <div class="pool-var-list">${net.variants.slice().sort((x, y) => (y.chosen ? 1 : 0) - (x.chosen ? 1 : 0)).map(v => `
      <label class="pool-var"><input type="checkbox" value="${v.channel_id}"> <span class="n" title="${escHtml(v.provider_name || v.name)}">${v.chosen ? '<span class="feeds">Feeds it</span>' : ''}${escHtml(v.name)}</span>
        <span class="mych-meta" style="margin:0">${v.has_guide ? '<span class="ok">Guide ✓</span>' : '<span>No guide</span>'}${_mychWorks(v)}${v.enabled ? '' : '<span>off</span>'}</span></label>`).join('')}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <button class="btn btn-secondary btn-sm" data-act="split">Split ticked ones off</button>
      <button class="btn btn-secondary btn-sm" data-act="dissolve">Undo this fold</button>
      <button class="btn btn-secondary btn-sm" data-act="rename">Rename</button>
    </div>`;
  }
  return html + '</div>';
}

function poolRender() {
  const el = document.getElementById('pool-list');
  if (!el) return;
  const rows = _poolFiltered();
  const nets = pool.entries.filter(e => e.key[0] === 'n').length;
  const hidden = pool.entries.filter(e => e.event === 'no_event').length;
  document.getElementById('pool-count').textContent =
    `${pool.entries.length} channels, ${nets} of them networks folded from several versions`
    + (hidden ? ` · ${hidden} event slots with nothing on, hidden from people` : '');
  _poolMergeBtn();
  if (!rows.length) {
    el.innerHTML = `<div class="mych-none">${pool.entries.length ? 'Nothing matches.' : 'No channels yet. Switch on channel groups on the provider’s card (IPTV providers).'}</div>`;
    return;
  }
  const shown = rows.slice(0, pool.limit);
  el.innerHTML = shown.map(_poolRow).join('') + (rows.length > shown.length
    ? `<button class="mych-more" data-act="more">Show all ${rows.length}</button>` : '');
}

function _poolMergeBtn() {
  const b = document.getElementById('pool-merge-btn');
  if (!b) return;
  b.disabled = pool.selected.size < 2;
  b.textContent = pool.selected.size ? `Merge selected (${pool.selected.size})` : 'Merge selected';
}

async function _poolDo(fn, ok) {
  try {
    await fn();
    if (ok) toast(ok, 'success', 3000);
  } catch (e) {
    if (!e.sessionExpired) toast(e.message, 'error');
  }
  await loadLivePool();
}

function _poolOnChange(ev) {
  const t = ev.target;
  const row = t.closest('.pool-row');
  if (t.dataset.act === 'select' && row) {
    if (t.checked) pool.selected.add(row.dataset.key); else pool.selected.delete(row.dataset.key);
    _poolMergeBtn();
  } else if (t.dataset.act === 'variant' && row) {
    const nid = row.dataset.key.slice(1);
    const id = t.value ? parseInt(t.value, 10) : null;
    _poolDo(() => api(`/api/live/catalog/networks/${nid}/variant`, { method: 'PUT', body: { channel_id: id } }),
      id ? 'Changed. People keep the channel, favourites and recordings.' : 'Tentacle chooses again');
  }
}

function _poolOnClick(ev) {
  const btn = ev.target.closest('[data-act]');
  if (!btn || btn.tagName === 'SELECT' || btn.type === 'checkbox') return;
  const act = btn.dataset.act;
  if (act === 'more') { pool.limit = Infinity; poolRender(); return; }
  const holder = btn.closest('.pool-row, .pool-detail');
  if (!holder) return;
  const key = holder.dataset.key;
  const id = key.slice(1);
  const e = pool.entries.find(x => x.key === key) || { key, name: key };
  if (act === 'details') { pool.open = pool.open === key ? null : key; poolRender(); return; }
  if (act === 'takeout') {
    if (key[0] === 'n') {
      _poolDo(() => api(`/api/live/catalog/networks/${id}`, { method: 'PUT', body: { excluded: true } }),
        `${e.name} taken out. Show “Taken out” to put it back.`);
    } else {
      _poolDo(() => api(`/api/live/catalog/channels/${id}/enabled`, { method: 'PUT', body: { enabled: false } }),
        `${e.name} switched off. Switch it on again in the Provider list.`);
    }
  } else if (act === 'putback') {
    _poolDo(() => api(`/api/live/catalog/networks/${id}`, { method: 'PUT', body: { excluded: false } }), 'Put back');
  } else if (act === 'types' || act === 'types-auto') {
    const types = act === 'types' ? [...holder.querySelectorAll('.pool-typepick input:checked')].map(i => i.value) : [];
    _poolDo(() => api('/api/live/catalog/types', { method: 'PUT', body: { key, types } }),
      types.length ? 'Types saved' : 'Automatic types again');
  } else if (act === 'split') {
    const ids = [...holder.querySelectorAll('.pool-var input:checked')].map(i => parseInt(i.value, 10));
    if (!ids.length) { toast('Tick the versions that are a different channel first', 'warning'); return; }
    const name = prompt('They become one channel of their own. Its name (blank: each stays on its own):', '');
    if (name === null) return;
    _poolDo(() => api(`/api/live/catalog/networks/${id}/split`, { method: 'POST', body: { channel_ids: ids, name: name.trim() || null } }),
      'Split off');
  } else if (act === 'dissolve') {
    if (!confirm(`Show every version of ${e.name} as a channel of its own again?`)) return;
    _poolDo(() => api(`/api/live/catalog/networks/${id}/split`, { method: 'POST', body: {} }), 'Fold undone');
  } else if (act === 'rename') {
    const name = prompt(`Name people see for ${e.name} (blank: the automatic name):`, e.name);
    if (name === null) return;
    _poolDo(() => api(`/api/live/catalog/networks/${id}`, { method: 'PUT', body: { name: name.trim() } }), 'Renamed');
  }
}

async function poolMerge() {
  const keys = [...pool.selected];
  if (keys.length < 2) return;
  const first = pool.entries.find(e => e.key === keys[0]);
  const name = prompt(`Merge ${keys.length} channels into one. Its name:`, first ? first.name : '');
  if (name === null) return;
  pool.selected.clear();
  await _poolDo(() => api('/api/live/catalog/merge', { method: 'POST', body: { keys, name: name.trim() || null } }),
    'Merged into one channel');
}

async function _poolLoadRegion() {
  const input = document.getElementById('pool-region');
  if (!input || input.dataset.loaded) return;
  try {
    const s = await api('/api/settings/raw');
    input.value = s.livetv_region || '';
    input.dataset.loaded = '1';
  } catch (e) { /* stays empty */ }
}

async function poolSaveRegion() {
  const input = document.getElementById('pool-region');
  const note = document.getElementById('pool-region-note');
  try {
    await api('/api/settings', { method: 'POST', body: { settings: { livetv_region: input.value.trim() } } });
    await api('/api/live/catalog/refresh', { method: 'POST' });
    if (note) note.textContent = 'Saved. Networks you didn’t choose a version for follow it.';
    await loadLivePool();
  } catch (e) {
    if (!e.sessionExpired) toast(e.message, 'error');
  }
}

// ── Admins: Sources → Live TV → People and In Jellyfin ─────────────────────

function _mychWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

// Each person's favourites; a kids profile sees the kids channels only.
async function loadLivePeople() {
  const el = document.getElementById('live-people');
  if (!el) return;
  let d;
  try { d = await api('/api/live/people'); } catch (e) { el.innerHTML = `<div class="mych-none">${escHtml(e.message)}</div>`; return; }
  if (!d.people.length) { el.innerHTML = '<div class="mych-none">Nobody has signed in yet.</div>'; return; }
  const favs = p => p.favourites == null ? '<span class="muted">Not known yet</span>'
    : `${p.favourites} favourite${p.favourites === 1 ? '' : 's'}`;
  const places = new Set(d.people.map(p => p.location && p.location.slug));
  const where = p => places.size > 1 && p.location ? ` · local channels from ${escHtml(p.location.city)}` : '';
  el.innerHTML = `<table class="mych-people"><thead><tr><th>Person</th><th>Favourites</th><th>Channels</th></tr></thead><tbody>${
    d.people.map(p => `<tr><td>${escHtml(p.name)}${p.kids ? ' <span class="badge badge-blue">Kids</span>' : ''}</td><td>${favs(p)}</td>`
      + `<td class="muted">${p.kids ? `Kids channels only (${p.channels})` : `All ${p.channels}`}${where(p)}</td></tr>`).join('')
  }</tbody></table>`;
}

async function loadLiveSetup() {
  const el = document.getElementById('live-setup-status');
  if (!el) return;
  let d;
  try { d = await api('/api/live/people'); } catch (e) { el.textContent = e.message; return; }
  const s = d.setup || {};
  const line = s.running ? '<span style="color:var(--amber)">Setting up now…</span>'
    : s.ok === false ? `<span style="color:var(--red)">The last try didn&rsquo;t work: ${escHtml(s.message)}</span>`
    : s.message ? `<span style="color:var(--amber)">${escHtml(s.message)}</span>`
    : s.last_at ? `<span style="color:var(--green)">✓ Set up</span> <span style="color:var(--text3)">· checked ${escHtml(_mychWhen(s.last_at))}</span>`
    : '<span style="color:var(--text3)">Not checked yet since Tentacle started (it checks within a few minutes, then every hour).</span>';
  el.innerHTML = `${line}<br><span style="color:var(--text2)">One lineup of ${d.total} channels for ${d.people.length} ${d.people.length === 1 ? 'person' : 'people'}; each stars their own favourites.</span>`;
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
