// New from your provider (admin): channel groups and categories a provider
// added since the admin last looked (services/provider_new.py).
// Live TV, Sources → Live TV → New from your provider: step 1 ticks groups
// (or dismisses them), step 2 picks channels of the ticked groups into the
// household's channels. VOD: a card on IPTV providers adds new categories to
// the library or dismisses them. The sidebar's Sources badge counts both.

const nfp = {
  groups: [],          // GET /api/live/new-groups
  dismissed: 0,
  ticked: new Set(),   // group ids ticked in step 1
  looking: [],         // group ids shown in step 2
  channels: [],        // POST /api/live/new-groups/channels
  stepGroups: [],
  picked: new Set(),   // unit keys ticked in step 2
  poll: null,
  household: null,     // {countries, languages}: what "Probably for you" means
  countryNames: {},
  open: new Set(),     // place codes of the folds left open
  vod: [],
  vodTicked: new Set(),
  lastVod: null,       // the VOD count the card was last loaded for
};

// ── The badge ───────────────────────────────────────────────────────────────

async function nfpLoadCounts() {
  if (!state.currentUser?.is_admin) return;
  let c;
  try { c = await api('/api/new-from-provider/counts'); } catch (e) { return; }
  _nfpBadge('sources-badge', (c.live || 0) + (c.vod || 0));
  _nfpBadge('live-new-badge', c.live || 0);
  const note = document.getElementById('nfp-live-note');
  if (note) {
    note.style.display = c.live ? '' : 'none';
    document.getElementById('nfp-live-note-text').textContent =
      `Your provider added ${_nfpPlural(c.live || 0, 'new channel group')} since you last looked.`;
  }
  const vod = document.getElementById('nfp-vod');
  if (vod && (c.vod || 0) !== nfp.lastVod) { nfp.lastVod = c.vod || 0; loadNewCategories(); }
}

function _nfpBadge(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = n ? '' : 'none';
  el.textContent = n > 99 ? '99+' : String(n);
}

function _nfpDate(iso) {
  const d = new Date(iso || '');
  if (isNaN(d)) return '';
  const opts = { day: 'numeric', month: 'short' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString([], opts);
}

function _nfpPlural(n, one, many) {
  return `${n} ${n === 1 ? one : (many || one + 's')}`;
}

function nfpOpenLive() {
  showSourcesTab('live');
  showLiveTab('new');
}

// ── Step 1: the new groups ──────────────────────────────────────────────────
// "Probably for you" (the household's countries and languages) on top; the
// rest folded by country or language, each fold with Tick all / Dismiss all.

function _nfpLocale() {
  const loc = (navigator.language || '').split('-');
  return `lang=${encodeURIComponent(loc[0] || '')}&country=${encodeURIComponent(loc[1] || '')}`;
}

async function loadNewGroups() {
  const el = document.getElementById('nfp-groups');
  if (!el) return;
  let d;
  try { d = await api(`/api/live/new-groups?${_nfpLocale()}`); } catch (e) {
    el.innerHTML = `<div class="mych-none">${escHtml(e.message)}</div>`;
    return;
  }
  nfp.groups = (d.groups || []).filter(g => g.review === 'new');
  nfp.dismissed = d.dismissed || 0;
  nfp.household = d.household || { countries: [], languages: [] };
  const ids = new Set(nfp.groups.map(g => g.id));
  for (const id of [...nfp.ticked]) if (!ids.has(id)) nfp.ticked.delete(id);
  _nfpBadge('live-new-badge', d.new || 0);
  nfpRenderGroups();
  _nfpReading(d.loading || {});
  const fold = document.getElementById('nfp-dismissed');
  if (fold) {
    fold.style.display = nfp.dismissed ? '' : 'none';
    document.getElementById('nfp-dismissed-label').textContent = `Dismissed · ${_nfpPlural(nfp.dismissed, 'group')}`;
    if (fold.open) loadDismissedGroups();
  }
}

function _nfpReading(l) {
  const el = document.getElementById('nfp-reading');
  if (!el) return;
  clearTimeout(nfp.poll);
  if (!l.running) { el.style.display = 'none'; return; }
  el.style.display = '';
  el.innerHTML = `<span class="spinner" style="width:12px;height:12px"></span> Reading the new groups&rsquo; channels from your provider`
    + (l.total ? ` (${l.done || 0} of ${l.total})` : '') + '. The counts fill in as it goes.';
  nfp.poll = setTimeout(() => {
    if (document.getElementById('live-panel-new')?.classList.contains('active')
        && document.getElementById('nfp-step2').style.display === 'none') loadNewGroups();
  }, 4000);
}

// A country's flag from its ISO code; regions and languages get a globe.
function _nfpFlag(place) {
  if (!place) return '<span class="nfp-flag" aria-hidden="true">&middot;</span>';
  if (!place.iso) return '<span class="nfp-flag" aria-hidden="true">🌐</span>';
  const f = String.fromCodePoint(...[...place.iso.toUpperCase()].map(c => 0x1F1E6 + c.charCodeAt(0) - 65));
  return `<span class="nfp-flag" aria-hidden="true">${f}</span>`;
}

function _nfpPlaceName(place) { return place ? place.name : 'Other'; }

function _nfpEmpty(g) { return g.loaded && g.channels === 0; }

function _nfpGroupMeta(g) {
  const parts = [];
  if (g.loaded && g.channels !== undefined) {
    if (g.channels === 0) parts.push('<span>Your provider lists no channels in it right now</span>');
    else if (g.events && g.events === g.channels) {
      const on = g.events - (g.no_event || 0);
      parts.push(`<span>${_nfpPlural(g.events, 'event slot')}</span>`);
      parts.push(on ? `<span class="ev">${on} on now</span>` : '<span>Nothing on now</span>');
    } else {
      parts.push(`<span>${_nfpPlural(g.channels, 'channel')}</span>`);
      const checked = g.checked || 0;
      if (checked) parts.push(`<span class="ok">${g.works} work</span>`);
      parts.push(g.with_guide ? `<span class="ok">${g.with_guide} with a guide</span>` : '<span>No guide</span>');
      const on = (g.events || 0) - (g.no_event || 0);
      if (g.events) parts.push(`<span class="ev">${_nfpPlural(g.events, 'event slot')}, ${on ? `${on} on now` : 'nothing on now'}</span>`);
    }
  } else {
    parts.push(`<span>${_nfpPlural(g.provider_count || 0, 'channel')} (reading&hellip;)</span>`);
  }
  if (g.review === 'dismissed') parts.push(`<span>Dismissed${g.reviewed_at ? ' ' + escHtml(_nfpDate(g.reviewed_at)) : ''}</span>`);
  else if (g.first_seen) parts.push(`<span>New ${escHtml(_nfpDate(g.first_seen))}</span>`);
  return _mychMeta(parts);
}

function _nfpTitle(g, withPlace) {
  const tags = (g.tags || []).map(t => `<span class="nfp-tag">${escHtml(t)}</span>`).join('');
  const ev = g.event_group && !_nfpEmpty(g) ? '<span class="nfp-tag ev">Event channels</span>' : '';
  const empty = _nfpEmpty(g) ? '<span class="nfp-tag empty">Empty</span>' : '';
  const place = withPlace ? `<span class="nfp-tag place">${_nfpFlag(g.place).replace('nfp-flag', 'nfp-flag sm')} ${escHtml(_nfpPlaceName(g.place))}</span>` : '';
  return `<span class="nfp-g-title" title="Your provider calls it: ${escHtml(g.name)}"><span>${escHtml(g.title || g.name)}</span>${place}${ev}${empty}${tags}</span>`;
}

function _nfpSamples(g) {
  const s = g.samples || [];
  if (!s.length) return '';
  const urls = [...new Set(s.map(x => x.logo_url).filter(Boolean))];   // one placeholder logo for all: shown once
  const logos = urls.slice(0, 5).map(u =>
    `<img src="${escHtml(u)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`).join('');
  const more = (g.channels || 0) > s.length ? ', &hellip;' : '';
  const label = s.some(x => x.event) ? 'On now: ' : '';
  return `<span class="nfp-samples">${logos}<span class="names">${label}${s.map(x => escHtml(x.name)).join(', ')}${more}</span></span>`;
}

function _nfpRow(g, withPlace) {
  const out = g.review === 'dismissed';
  const ticked = nfp.ticked.has(g.id);
  return `
    <label class="nfp-g${ticked ? ' ticked' : ''}${out ? ' out' : ''}" data-id="${g.id}">
      ${out ? '' : `<input type="checkbox" data-act="tick"${ticked ? ' checked' : ''} aria-label="Tick ${escHtml(g.title || g.name)}">`}
      <span class="nfp-g-main">${_nfpTitle(g, withPlace)}${_nfpGroupMeta(g)}${_nfpSamples(g)}</span>
      ${out ? '<button class="btn btn-secondary btn-sm" data-act="restore">Bring back</button>'
        : '<button class="nfp-x" data-act="dismiss" title="Dismiss this group">Dismiss</button>'}
    </label>`;
}

// For you first: the household's own countries, then its languages; within,
// groups with channels before empty ones, biggest first.
function _nfpOrder(a, b) {
  const rank = g => (g.for_you === 'country' ? 0 : 1);
  return rank(a) - rank(b) || (_nfpEmpty(a) - _nfpEmpty(b))
    || ((b.channels ?? b.provider_count ?? 0) - (a.channels ?? a.provider_count ?? 0))
    || (a.title || a.name).localeCompare(b.title || b.name);
}

function _nfpMatches(g, q) {
  return !q || `${g.title} ${g.name} ${_nfpPlaceName(g.place)}`.toLowerCase().includes(q);
}

function _nfpYouLine() {
  const h = nfp.household || {};
  const names = (h.countries || []).map(c => nfp.countryNames[c] || c);
  const langs = (h.languages || []).map(l => ({ en: 'English', fr: 'French', es: 'Spanish', de: 'German', it: 'Italian',
    pt: 'Portuguese', nl: 'Dutch', ar: 'Arabic' }[l] || l));
  const list = a => a.length <= 1 ? (a[0] || '') : `${a.slice(0, -1).join(', ')} and ${a[a.length - 1]}`;
  if (!names.length && !langs.length) return 'Groups like the channels you already have.';
  return `From ${list(names) || 'your country'}${langs.length ? `, or in ${list(langs)}` : ''}: like the channels you already have.`;
}

function _nfpGroupAll(rows, label) {
  const ids = rows.map(g => g.id).join(',');
  const all = rows.length && rows.every(g => nfp.ticked.has(g.id));
  return `<span class="nfp-fold-acts">
      <button class="btn btn-secondary btn-sm" data-act="tick-all" data-ids="${ids}">${all ? 'Untick all' : 'Tick all'}</button>
      <button class="btn btn-secondary btn-sm" data-act="dismiss-all" data-ids="${ids}" data-label="${escHtml(label)}">Dismiss all</button>
    </span>`;
}

function nfpRenderGroups() {
  const el = document.getElementById('nfp-groups');
  if (!el) return;
  const q = (document.getElementById('nfp-search')?.value || '').trim().toLowerCase();
  nfp.countryNames = {};
  for (const g of nfp.groups) if (g.place?.iso) nfp.countryNames[g.place.iso] = g.place.name;
  const names = { US: 'the US', GB: 'the UK', CA: 'Canada', NL: 'the Netherlands' };
  for (const c of nfp.household?.countries || []) nfp.countryNames[c] = names[c] || nfp.countryNames[c] || c;
  const mine = nfp.groups.filter(g => g.for_you).sort(_nfpOrder);
  const rest = nfp.groups.filter(g => !g.for_you);
  document.getElementById('nfp-count').textContent = nfp.groups.length
    ? `${_nfpPlural(nfp.groups.length, 'new group')} since you last looked: ${mine.length ? `${mine.length} probably for you` : 'none look like they’re for you'}.`
    : 'Nothing new since you last looked.';
  const restBtn = document.getElementById('nfp-dismiss-rest-btn');
  if (restBtn) {
    restBtn.style.display = rest.length ? '' : 'none';
    restBtn.textContent = mine.length ? `Dismiss everything not for you (${rest.length})` : `Dismiss all ${rest.length}`;
  }
  if (!nfp.groups.length) {
    el.innerHTML = '<div class="mych-none">No new groups. When your provider adds some, they show up here (Tentacle looks every night).</div>';
    _nfpStep1Buttons();
    return;
  }
  let html = '';
  const mineShown = mine.filter(g => _nfpMatches(g, q));
  if (mineShown.length || !q) {
    html += `<section class="nfp-mine"><div class="nfp-sec-head"><span class="grow"><span class="nfp-sec-title">Probably for you</span>
        <span class="nfp-sec-sub">${escHtml(_nfpYouLine())}</span></span>${mineShown.length ? _nfpGroupAll(mineShown, 'probably for you') : ''}</div>
      ${mineShown.length ? mineShown.map(g => _nfpRow(g, true)).join('')
        : '<div class="mych-none">Nothing new looks like it&rsquo;s for you. The groups below are from other countries.</div>'}</section>`;
  }
  const byPlace = new Map();
  for (const g of rest) {
    const k = g.place ? g.place.code : '';
    if (!byPlace.has(k)) byPlace.set(k, { place: g.place, rows: [] });
    byPlace.get(k).rows.push(g);
  }
  const folds = [...byPlace.entries()].sort((a, b) =>
    (!a[1].place - !b[1].place) || _nfpPlaceName(a[1].place).localeCompare(_nfpPlaceName(b[1].place)));
  let foldHtml = '';
  for (const [k, f] of folds) {
    const placeHit = q && _nfpPlaceName(f.place).toLowerCase().includes(q);
    const rows = f.rows.filter(g => placeHit || _nfpMatches(g, q)).sort(_nfpOrder);
    if (!rows.length) continue;
    const chans = rows.reduce((n, g) => n + (g.loaded ? (g.channels || 0) : (g.provider_count || 0)), 0);
    const open = nfp.open.has(k) || (q && rows.length);
    const name = _nfpPlaceName(f.place);
    foldHtml += `<details class="nfp-fold" data-place="${escHtml(k)}"${open ? ' open' : ''}>
        <summary>${_nfpFlag(f.place)}<span class="nfp-fold-name">${escHtml(name)}</span>
          <span class="nfp-fold-sub">${_nfpPlural(rows.length, 'group')} &middot; ${_nfpPlural(chans, 'channel')}</span>
          ${_nfpGroupAll(rows, name)}<span class="nfp-chev" aria-hidden="true"></span></summary>
        ${rows.map(g => _nfpRow(g, false)).join('')}
      </details>`;
  }
  if (foldHtml) html += `<div class="nfp-label">${mine.length ? 'Other countries and languages' : 'By country and language'}</div>${foldHtml}`;
  el.innerHTML = html || '<div class="mych-none">No group matches.</div>';
  _nfpStep1Buttons();
}

function _nfpStep1Buttons() {
  const n = nfp.ticked.size;
  const foot = document.getElementById('nfp-step1-foot');
  if (!foot) return;
  foot.style.display = n ? '' : 'none';
  const all = n && n === nfp.groups.length;
  document.getElementById('nfp-ticked').textContent = `${_nfpPlural(n, 'group')} ticked`;
  document.getElementById('nfp-look-btn').textContent = `Show their channels (${n})`;
  document.getElementById('nfp-dismiss-btn').textContent = all ? `Dismiss all ${n}` : `Dismiss ${n}`;
}

function nfpSelectAll(on) {
  if (on) nfp.groups.forEach(g => nfp.ticked.add(g.id)); else nfp.ticked.clear();
  nfpRenderGroups();
}

function _nfpGroupsClick(ev) {
  const btn = ev.target.closest('button[data-act]');
  const act = btn?.dataset.act || ev.target.dataset?.act;
  if (act === 'tick-all' || act === 'dismiss-all') {
    ev.preventDefault();
    ev.stopPropagation();
    const ids = btn.dataset.ids.split(',').filter(Boolean).map(Number);
    if (act === 'tick-all') {
      const all = ids.every(id => nfp.ticked.has(id));
      ids.forEach(id => all ? nfp.ticked.delete(id) : nfp.ticked.add(id));
      nfpRenderGroups();
    } else {
      _nfpDismissIds(ids, btn.dataset.label);
    }
    return;
  }
  const row = ev.target.closest('.nfp-g');
  if (!row) return;
  const id = parseInt(row.dataset.id, 10);
  if (act === 'restore') {
    ev.preventDefault();
    _nfpPost('/api/live/new-groups/restore', { group_ids: [id] }, 'Back on the new list.').then(loadNewGroups);
    return;
  }
  if (act === 'dismiss') {
    ev.preventDefault();
    const g = nfp.groups.find(x => x.id === id);
    _nfpDismissIds([id], g ? (g.title || g.name) : '');
    return;
  }
  if (act === 'tick') {
    if (ev.target.checked) nfp.ticked.add(id); else nfp.ticked.delete(id);
    row.classList.toggle('ticked', ev.target.checked);
    _nfpStep1Buttons();
  }
}

function _nfpFoldToggle(ev) {
  const d = ev.target;
  if (!d.classList?.contains('nfp-fold') || d.dataset.place === undefined) return;
  if (d.open) nfp.open.add(d.dataset.place); else nfp.open.delete(d.dataset.place);
}

async function _nfpPost(path, body, ok) {
  try {
    const r = await api(path, { method: 'POST', body });
    if (ok) toast(ok, 'success');
    nfpLoadCounts();
    return r;
  } catch (e) {
    toast(e.message, 'error');
    return null;
  }
}

// Dismiss at once, with Undo in the toast (Show dismissed brings them back later too).
async function _nfpDismissIds(ids, label) {
  if (!ids.length) return;
  let r;
  try { r = await api('/api/live/new-groups/dismiss', { method: 'POST', body: { group_ids: ids } }); } catch (e) {
    toast(e.message, 'error');
    return;
  }
  ids.forEach(id => nfp.ticked.delete(id));
  const what = ids.length === 1 && label ? `“${label}” dismissed.` : `${_nfpPlural(ids.length, 'group')} dismissed.`;
  const t = toast(what, 'success', 9000);
  const undo = document.createElement('button');
  undo.className = 'nfp-undo';
  undo.textContent = 'Undo';
  undo.onclick = async () => {
    t.remove();
    await _nfpPost('/api/live/new-groups/restore', { group_ids: ids }, `${_nfpPlural(ids.length, 'group')} back on the new list.`);
    loadNewGroups();
  };
  t.appendChild(undo);
  nfpLoadCounts();
  loadNewGroups();
  return r;
}

function nfpDismiss() {
  const ids = nfp.groups.filter(g => nfp.ticked.has(g.id)).map(g => g.id);
  _nfpDismissIds(ids);
}

function nfpDismissRest() {
  _nfpDismissIds(nfp.groups.filter(g => !g.for_you).map(g => g.id));
}

async function loadDismissedGroups() {
  const el = document.getElementById('nfp-dismissed-list');
  if (!el) return;
  el.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  let d;
  try { d = await api(`/api/live/new-groups?dismissed=true&${_nfpLocale()}`); } catch (e) {
    el.innerHTML = `<div class="mych-none">${escHtml(e.message)}</div>`;
    return;
  }
  const rows = (d.groups || []).filter(g => g.review === 'dismissed');
  el.innerHTML = rows.length ? rows.map(g => _nfpRow(g, true)).join('') : '<div class="mych-none">No dismissed groups.</div>';
}

function nfpDismissedToggle(d) { if (d.open) loadDismissedGroups(); }

// ── Step 2: the channels of the ticked groups ───────────────────────────────

async function nfpLook() {
  nfp.looking = nfp.groups.filter(g => nfp.ticked.has(g.id)).map(g => g.id);
  if (!nfp.looking.length) return;
  clearTimeout(nfp.poll);
  document.getElementById('nfp-step1').style.display = 'none';
  document.getElementById('nfp-reading').style.display = 'none';
  document.getElementById('nfp-step2').style.display = '';
  document.getElementById('nfp-step2-title').textContent = 'Channels';
  document.getElementById('nfp-channels').innerHTML =
    '<div class="loading-state"><div class="spinner"></div> Reading the channels from your provider…</div>';
  document.getElementById('nfp-ch-search').value = '';
  window.scrollTo({ top: 0 });
  let d;
  try { d = await api('/api/live/new-groups/channels', { method: 'POST', body: { group_ids: nfp.looking } }); } catch (e) {
    document.getElementById('nfp-channels').innerHTML = `<div class="mych-none">${escHtml(e.message)}</div>`;
    return;
  }
  nfp.channels = d.channels || [];
  nfp.stepGroups = d.groups || [];
  nfp.picked = new Set();
  const names = nfp.stepGroups.map(g => g.title || g.name);
  document.getElementById('nfp-step2-title').textContent = names.length === 1
    ? `Channels in ${names[0]}` : `Channels in ${names.length} groups`;
  nfpRenderChannels();
}

function nfpBack() {
  document.getElementById('nfp-step2').style.display = 'none';
  document.getElementById('nfp-step1').style.display = '';
  loadNewGroups();
}

function _nfpRank(c) {
  return (c.in_pool ? 10 : 0) + (c.check?.state === 'works' ? 0 : c.check?.state === 'broken' ? 2 : 1);
}

function _nfpShown() {
  const q = (document.getElementById('nfp-ch-search')?.value || '').trim().toLowerCase();
  const show = document.getElementById('nfp-ch-show')?.value || '';
  const noEvent = document.getElementById('nfp-show-noevent')?.checked;
  return nfp.channels.filter(c => {
    if (c.event === 'no_event' && !noEvent) return false;
    if (q && !`${c.name} ${c.provider_name}`.toLowerCase().includes(q)) return false;
    if (show === 'works') return c.check?.state === 'works';
    if (show === 'guide') return c.has_guide || (c.event && typeof c.event === 'object');
    if (show === 'notin') return !c.in_pool;
    return true;
  });
}

function _nfpChannelRow(c) {
  const meta = _mychMeta([
    c.provider_name && c.provider_name !== c.name ? `<span class="via">${escHtml(c.provider_name)}</span>` : '',
    _mychWorks(c), _mychGuide(c),
    c.feeds > 1 ? `<span>${c.feeds} versions, shown once</span>` : '',
    c.in_pool ? '<span class="ev">Already in your channels: adds another version</span>' : '',
    `<span class="pool-types">${(c.types || []).map(t => `<span class="pool-type">${escHtml(MYCH_TYPE_LABEL[t] || t)}</span>`).join('')}</span>`,
  ]);
  const broken = c.check?.state === 'broken';
  return `
    <label class="pool-row nfp-ch${broken ? ' broken' : ''}" data-key="${escHtml(c.key)}">
      <input type="checkbox" data-act="pick"${nfp.picked.has(c.key) ? ' checked' : ''} aria-label="Add ${escHtml(c.name)}">
      ${_mychLogo(c)}
      <span class="mych-text"><span class="mych-name" title="${escHtml(c.provider_name || c.name)}"><span class="pool-name">${escHtml(c.name)}</span></span>${meta}</span>
    </label>`;
}

function _nfpNoneShown(g) {
  const mine = nfp.channels.filter(c => c.group_id === g.id);
  if (!mine.length) return 'Your provider lists no channels in this group right now.';
  const empty = mine.filter(c => c.event === 'no_event').length;
  if (empty === mine.length && !document.getElementById('nfp-show-noevent')?.checked)
    return `Nothing on now: ${empty === 1 ? 'its event channel has' : `all ${empty} of its event channels have`} no event at the moment. Tick <strong>Show event slots with no event</strong> to see them.`;
  return 'No channel matches.';
}

function nfpRenderChannels() {
  const el = document.getElementById('nfp-channels');
  if (!el) return;
  const shown = _nfpShown();
  const hidden = nfp.channels.filter(c => c.event === 'no_event').length;
  const lbl = document.getElementById('nfp-noevent-label');
  if (lbl) lbl.textContent = `Show event slots with no event${hidden ? ` (${hidden})` : ''}`;
  lbl?.parentElement && (lbl.parentElement.style.display = hidden ? '' : 'none');
  const byGroup = {};
  for (const c of shown) (byGroup[c.group_id] = byGroup[c.group_id] || []).push(c);
  let html = '';
  for (const g of nfp.stepGroups) {
    const rows = (byGroup[g.id] || []).slice().sort((a, b) => _nfpRank(a) - _nfpRank(b));
    const all = rows.length && rows.every(c => nfp.picked.has(c.key));
    html += `<div class="nfp-ghead" data-group="${g.id}">
        ${_nfpFlag(g.place)}<span class="mych-text">${_nfpTitle(g, false)}${_nfpGroupMeta(g)}</span>
        ${rows.length ? `<button class="btn btn-secondary btn-sm" data-act="all">${all ? 'Untick all' : 'Tick all'}${rows.length > 1 ? ` ${rows.length}` : ''}</button>` : ''}
      </div>`;
    html += rows.length ? rows.map(_nfpChannelRow).join('')
      : `<div class="mych-none">${_nfpNoneShown(g)}</div>`;
  }
  el.innerHTML = html;
  _nfpPickedLine();
}

function _nfpPickedLine() {
  const n = nfp.picked.size;
  const groups = _nfpPlural(nfp.looking.length, 'group');
  document.getElementById('nfp-picked').textContent = n
    ? `${_nfpPlural(n, 'channel')} ticked. The rest stay out.`
    : 'Nothing ticked yet.';
  const btn = document.getElementById('nfp-add-btn');
  btn.textContent = n ? `Add ${_nfpPlural(n, 'channel')} to the household’s channels` : `Done, add none`;
  btn.title = n ? '' : `Marks the ${groups} as looked at; none of their channels are added`;
  btn.classList.toggle('btn-primary', n > 0);
  btn.classList.toggle('btn-secondary', n === 0);
}

function _nfpChannelsClick(ev) {
  const act = ev.target.dataset?.act;
  if (act === 'pick') {
    const key = ev.target.closest('.nfp-ch')?.dataset.key;
    if (!key) return;
    if (ev.target.checked) nfp.picked.add(key); else nfp.picked.delete(key);
    _nfpPickedLine();
    return;
  }
  if (act === 'all') {
    const gid = parseInt(ev.target.closest('.nfp-ghead').dataset.group, 10);
    const rows = _nfpShown().filter(c => c.group_id === gid);
    const all = rows.every(c => nfp.picked.has(c.key));
    rows.forEach(c => all ? nfp.picked.delete(c.key) : nfp.picked.add(c.key));
    nfpRenderChannels();
  }
}

// Step 2 in bulk: tick every shown channel, only the working ones, only the
// ones with a guide (or an event on now), or none.
function nfpPickBulk(how) {
  nfp.picked.clear();
  if (how !== 'none') {
    const fits = _nfpShown().filter(c => how === 'all'
      || (how === 'works' && c.check?.state === 'works')
      || (how === 'guide' && (c.has_guide || (c.event && typeof c.event === 'object'))));
    fits.filter(c => !c.in_pool).forEach(c => nfp.picked.add(c.key));   // channels the household has stay unticked
    if (!nfp.picked.size) toast(fits.length ? 'You have these channels already.'
      : how === 'works' ? 'None of these channels has been found working yet.' : 'None of these channels has a guide.', 'info');
  }
  nfpRenderChannels();
}

async function nfpAdd() {
  const ids = nfp.channels.filter(c => nfp.picked.has(c.key)).flatMap(c => c.channel_ids);
  const btn = document.getElementById('nfp-add-btn');
  btn.disabled = true;
  const n = nfp.picked.size;
  const r = await _nfpPost('/api/live/new-groups/add', { group_ids: nfp.looking, channel_ids: ids },
    n ? `${_nfpPlural(n, 'channel')} added. Everyone gets them in Live TV in a minute or two.`
      : `Done with the ${_nfpPlural(nfp.looking.length, 'group')}. Nothing was added.`);
  btn.disabled = false;
  if (!r) return;
  nfp.looking.forEach(id => nfp.ticked.delete(id));
  nfp.looking = [];
  nfpBack();
}

// ── VOD: new categories ─────────────────────────────────────────────────────

async function loadNewCategories() {
  const card = document.getElementById('nfp-vod');
  if (!card || !state.currentUser?.is_admin) return;
  const dismissed = document.getElementById('nfp-vod-dismissed')?.checked;
  let d;
  try { d = await api(`/api/new-from-provider/categories${dismissed ? '?dismissed=true' : ''}`); } catch (e) { return; }
  nfp.vod = d.categories || [];
  const ids = new Set(nfp.vod.map(c => c.id));
  for (const id of [...nfp.vodTicked]) if (!ids.has(id)) nfp.vodTicked.delete(id);
  card.style.display = (d.new || dismissed) ? '' : 'none';
  document.getElementById('nfp-vod-count').textContent = d.new
    ? `${_nfpPlural(d.new, 'category', 'categories')} your provider added since you last looked` : 'Nothing new since you last looked.';
  const lbl = document.getElementById('nfp-vod-dismissed-label');
  if (lbl) lbl.textContent = d.dismissed ? `Show dismissed (${d.dismissed})` : 'Show dismissed';
  const kind = { movie: 'Movies', series: 'Series' };
  document.getElementById('nfp-vod-list').innerHTML = nfp.vod.length ? nfp.vod.map(c => `
    <label class="pool-row nfp-cat${c.review === 'dismissed' ? ' out' : ''}" data-id="${c.id}">
      <input type="checkbox" data-act="tick"${nfp.vodTicked.has(c.id) ? ' checked' : ''} aria-label="Tick ${escHtml(c.name)}">
      <span class="mych-text"><span class="mych-name"><span class="pool-name">${escHtml(c.name)}</span></span>${_mychMeta([
        `<span>${kind[c.type] || escHtml(c.type)}</span>`, `<span>${escHtml(c.provider)}</span>`,
        `<span>${_nfpPlural(c.titles || 0, 'title')}</span>`,
        c.review === 'dismissed' ? '<span>Dismissed</span>' : (c.first_seen ? `<span>First seen ${escHtml(_nfpDate(c.first_seen))}</span>` : '')])}</span>
      ${c.review === 'dismissed' ? '<span class="pool-actions"><button class="btn btn-secondary btn-sm" data-act="restore">Bring back</button></span>' : ''}
    </label>`).join('') : '<div class="mych-none">No dismissed categories.</div>';
  _nfpVodButtons();
}

function _nfpVodButtons() {
  const n = nfp.vod.filter(c => nfp.vodTicked.has(c.id) && c.review === 'new').length;
  const add = document.getElementById('nfp-vod-add-btn');
  const dis = document.getElementById('nfp-vod-dismiss-btn');
  if (add) { add.disabled = !n; add.textContent = n ? `Add ${n} to the library` : 'Add to the library'; }
  if (dis) { dis.disabled = !n; dis.textContent = n ? `Dismiss (${n})` : 'Dismiss'; }
}

function _nfpVodClick(ev) {
  const row = ev.target.closest('.nfp-cat');
  if (!row) return;
  const id = parseInt(row.dataset.id, 10);
  const act = ev.target.dataset?.act;
  if (act === 'restore') {
    ev.preventDefault();
    _nfpPost('/api/new-from-provider/categories/restore', { category_ids: [id] }, 'Back on the new list.').then(loadNewCategories);
    return;
  }
  if (act === 'tick') {
    if (ev.target.checked) nfp.vodTicked.add(id); else nfp.vodTicked.delete(id);
    _nfpVodButtons();
  }
}

async function nfpVodDecide(add) {
  const ids = nfp.vod.filter(c => nfp.vodTicked.has(c.id) && c.review === 'new').map(c => c.id);
  if (!ids.length) return;
  const n = _nfpPlural(ids.length, 'category', 'categories');
  const r = await _nfpPost(`/api/new-from-provider/categories/${add ? 'add' : 'dismiss'}`, { category_ids: ids },
    add ? `${n} added. Their movies and series come in with tonight’s sync.` : `${n} dismissed.`);
  if (!r) return;
  ids.forEach(id => nfp.vodTicked.delete(id));
  loadNewCategories();
  if (add && typeof loadProviders === 'function') loadProviders({ quiet: true });
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('nfp-groups')?.addEventListener('click', _nfpGroupsClick);
  document.getElementById('nfp-groups')?.addEventListener('toggle', _nfpFoldToggle, true);
  document.getElementById('nfp-dismissed-list')?.addEventListener('click', _nfpGroupsClick);
  document.getElementById('nfp-channels')?.addEventListener('click', _nfpChannelsClick);
  document.getElementById('nfp-vod-list')?.addEventListener('click', _nfpVodClick);
});
