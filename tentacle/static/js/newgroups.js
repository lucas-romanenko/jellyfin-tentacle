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

async function loadNewGroups() {
  const el = document.getElementById('nfp-groups');
  if (!el) return;
  const dismissed = document.getElementById('nfp-show-dismissed')?.checked;
  let d;
  try { d = await api(`/api/live/new-groups${dismissed ? '?dismissed=true' : ''}`); } catch (e) {
    el.innerHTML = `<div class="mych-none">${escHtml(e.message)}</div>`;
    return;
  }
  nfp.groups = d.groups || [];
  nfp.dismissed = d.dismissed || 0;
  const ids = new Set(nfp.groups.map(g => g.id));
  for (const id of [...nfp.ticked]) if (!ids.has(id)) nfp.ticked.delete(id);
  _nfpBadge('live-new-badge', d.new || 0);
  nfpRenderGroups();
  _nfpReading(d.loading || {});
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

function _nfpGroupMeta(g) {
  const parts = [`<span>${escHtml(g.provider)}</span>`];
  if (g.loaded && g.channels !== undefined) {
    parts.push(`<span>${_nfpPlural(g.channels, 'channel')}</span>`);
    const checked = g.checked || 0;
    if (checked) parts.push(`<span class="ok">${g.works} work</span>`
      + (checked < g.channels ? `<span>${g.channels - checked} not checked yet</span>` : ''));
    else if (g.channels) parts.push('<span>Not checked yet</span>');
    if (g.channels) parts.push(g.with_guide ? `<span class="ok">${g.with_guide} with a guide</span>` : '<span>No guide</span>');
    if (g.events) parts.push(`<span class="ev">${_nfpPlural(g.events, 'event channel')}${g.no_event ? `, ${g.no_event} with nothing on now` : ''}</span>`);
  } else {
    parts.push(`<span>${_nfpPlural(g.provider_count || 0, 'channel')} (not read yet)</span>`);
  }
  if (g.review === 'dismissed') parts.push(`<span>Dismissed${g.reviewed_at ? ' ' + escHtml(_nfpDate(g.reviewed_at)) : ''}</span>`);
  else if (g.first_seen) parts.push(`<span>First seen ${escHtml(_nfpDate(g.first_seen))}</span>`);
  return _mychMeta(parts);
}

function nfpRenderGroups() {
  const el = document.getElementById('nfp-groups');
  if (!el) return;
  const q = (document.getElementById('nfp-search')?.value || '').trim().toLowerCase();
  const newOnes = nfp.groups.filter(g => g.review === 'new');
  document.getElementById('nfp-count').textContent = newOnes.length
    ? `${_nfpPlural(newOnes.length, 'group')} your provider added since you last looked`
    : 'Nothing new since you last looked.';
  const lbl = document.getElementById('nfp-dismissed-label');
  if (lbl) lbl.textContent = nfp.dismissed ? `Show dismissed (${nfp.dismissed})` : 'Show dismissed';
  const rows = nfp.groups.filter(g => !q || g.name.toLowerCase().includes(q));
  if (!rows.length) {
    el.innerHTML = `<div class="mych-none">${nfp.groups.length ? 'No group matches.'
      : 'No new groups. When your provider adds some, they show up here (Tentacle looks every night).'}</div>`;
  } else {
    el.innerHTML = rows.map(g => `
      <label class="pool-row nfp-group${g.review === 'dismissed' ? ' out' : ''}" data-id="${g.id}">
        <input type="checkbox" data-act="tick"${nfp.ticked.has(g.id) ? ' checked' : ''} aria-label="Tick ${escHtml(g.name)}">
        <span class="mych-text"><span class="mych-name"><span class="pool-name">${escHtml(g.name)}</span></span>${_nfpGroupMeta(g)}</span>
        ${g.review === 'dismissed' ? '<span class="pool-actions"><button class="btn btn-secondary btn-sm" data-act="restore">Bring back</button></span>' : ''}
      </label>`).join('');
  }
  _nfpStep1Buttons();
}

function _nfpStep1Buttons() {
  const n = nfp.ticked.size;
  const look = document.getElementById('nfp-look-btn');
  const dis = document.getElementById('nfp-dismiss-btn');
  if (look) { look.disabled = !n; look.textContent = n ? `Show their channels (${n})` : 'Show their channels'; }
  if (dis) {
    const newTicked = nfp.groups.filter(g => nfp.ticked.has(g.id) && g.review === 'new').length;
    dis.disabled = !newTicked;
    dis.textContent = newTicked ? `Dismiss (${newTicked})` : 'Dismiss';
  }
}

function _nfpGroupsClick(ev) {
  const row = ev.target.closest('.nfp-group');
  if (!row) return;
  const id = parseInt(row.dataset.id, 10);
  const act = ev.target.dataset?.act;
  if (act === 'restore') {
    ev.preventDefault();
    _nfpPost('/api/live/new-groups/restore', { group_ids: [id] }, 'Back on the new list.').then(loadNewGroups);
    return;
  }
  if (act === 'tick') {
    if (ev.target.checked) nfp.ticked.add(id); else nfp.ticked.delete(id);
    _nfpStep1Buttons();
  }
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

async function nfpDismiss() {
  const ids = nfp.groups.filter(g => nfp.ticked.has(g.id) && g.review === 'new').map(g => g.id);
  if (!ids.length) return;
  const r = await _nfpPost('/api/live/new-groups/dismiss', { group_ids: ids },
    `${_nfpPlural(ids.length, 'group')} dismissed. They won’t show up as new again.`);
  if (r) { ids.forEach(id => nfp.ticked.delete(id)); loadNewGroups(); }
}

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
  const names = nfp.stepGroups.map(g => g.name);
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
        <span class="mych-text"><span class="pool-name">${escHtml(g.name)}</span>${_nfpGroupMeta(g)}</span>
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
    : `Nothing ticked. You can still mark the ${groups} as looked at.`;
  const btn = document.getElementById('nfp-add-btn');
  btn.textContent = n ? `Add ${_nfpPlural(n, 'channel')} to the household’s channels` : `Add none, done with the ${groups}`;
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
  document.getElementById('nfp-channels')?.addEventListener('click', _nfpChannelsClick);
  document.getElementById('nfp-vod-list')?.addEventListener('click', _nfpVodClick);
});
