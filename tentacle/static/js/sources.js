// Tentacle - Sources page (admins): where the library comes from.
//
// IPTV providers are cards. A provider shows what it offers (has_vod,
// has_series, has_live from its test) and a switch for each use it offers:
// "Use for Movies & Series" (the provider's `active`, the VOD sync switch) and
// "Use for Live TV" (`live_tv_enabled`). With a use on, the card holds what
// goes with it: its Categories, its Channel groups. Everything is per
// provider, so nothing here assumes there is only one.

const srcState = {
  providers: [],
  cats: {},        // provider id -> categories (as /api/providers/{id}/categories gives them)
  catFilter: {},   // provider id -> 'all' | 'movie' | 'series' | 'chosen'
  groups: {},      // provider id -> channel groups
  open: {},        // `${id}:cats` / `${id}:groups` -> panel open
  groupPoll: {},   // provider id -> timer while its groups or channels are fetched
};

// ── Tabs ─────────────────────────────────────────────────────────────────
const SOURCES_TABS = ['providers', 'live', 'youtube', 'arr', 'music'];

function showSourcesTab(tab) {
  if (!SOURCES_TABS.includes(tab)) tab = 'providers';
  if (state.currentPage !== 'sources') { showPage('sources', tab); return; }
  state.sourcesTab = tab;
  document.querySelectorAll('[data-sourcestab]').forEach(b => b.classList.toggle('active', b.dataset.sourcestab === tab));
  SOURCES_TABS.forEach(t => {
    const el = document.getElementById(`sources-tab-${t}`);
    if (el) el.style.display = t === tab ? '' : 'none';
  });
  if (tab !== 'youtube' && typeof stopYouTubePolling === 'function') stopYouTubePolling();
  if (tab === 'providers') loadProviders();
  else loadProviders({ quiet: true });   // the Live TV tab follows the switches
  if (tab === 'live') loadLiveTV();
  if (tab === 'youtube') loadYouTubePage();
  if (tab === 'arr') loadArrSources();
  if (tab === 'music') loadSettings();
  setRoute('sources', tab);
}

// The Live TV tab is there once a provider is used for Live TV (or a YouTube
// channel is a Live TV channel): it is a feature, not a provider setting.
function _showLiveTab(on) {
  const btn = document.querySelector('[data-sourcestab="live"]');
  if (btn) btn.style.display = on ? '' : 'none';
}

// ── Provider cards ───────────────────────────────────────────────────────
async function loadProviders(opts = {}) {
  const grid = document.getElementById('provider-grid');
  let providers;
  try {
    providers = await api('/api/providers');
  } catch (e) {
    if (grid && !opts.quiet) grid.innerHTML = `<div class="empty-state"><p>Could not load providers: ${escHtml(e.message)}</p></div>`;
    return;
  }
  state.providers = providers;
  srcState.providers = providers;
  _showLiveTab(providers.some(p => p.live_tv_enabled) || await _youtubeOnLiveTv());
  const migrate = document.getElementById('migrate-btn');
  if (migrate) migrate.style.display = providers.length > 1 ? '' : 'none';
  if (opts.quiet || !grid) return;

  if (!providers.length) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">◉</div>
        <p style="margin-bottom:16px">No IPTV provider yet. Add one to bring in its movies and series, its live channels, or both.</p>
        <button class="btn btn-primary" onclick="showAddProvider()">Add a provider</button>
      </div>`;
    return;
  }
  grid.innerHTML = providers.map(renderProviderCard).join('');
  // What each card holds is fetched after the cards are up.
  for (const p of providers) {
    if (p.active) _loadProviderCats(p.id);
    if (p.live_tv_enabled) _loadProviderGroups(p.id);
  }
}

let _ytLiveCache = { at: 0, on: false };
async function _youtubeOnLiveTv() {
  if (Date.now() - _ytLiveCache.at < 60000) return _ytLiveCache.on;
  let on = false;
  try { on = (await api('/api/youtube/channels')).some(c => c.live_enabled); } catch (e) { on = false; }
  _ytLiveCache = { at: Date.now(), on };
  return on;
}

function _providerAddress(p) {
  if (p.provider_type && p.provider_type !== 'xtream') return (p.m3u_url || '').split('?')[0] || 'M3U playlist';
  return p.server_url || '';
}

function _switchRow(p, use, on, title, hint) {
  return `
    <div class="provider-use">
      <button type="button" class="live-toggle ${on ? 'on' : ''}" aria-pressed="${on ? 'true' : 'false'}"
        aria-label="${escapeAttr(title)} (${escapeAttr(p.name)})" onclick="setProviderUse(${p.id}, '${escapeJS(use)}', ${!on})"></button>
      <div style="flex:1;min-width:0">
        <div class="provider-use-title">${title}</div>
        <div class="provider-use-hint">${hint}</div>
      </div>
    </div>`;
}

function renderProviderCard(p) {
  const statusColor = p.status === 'ok' ? 'green' : p.status === 'error' ? 'red' : 'gray';
  const statusLabel = p.status === 'ok' ? 'Connected' : p.status === 'error' ? 'Can&rsquo;t connect' : 'Not tested yet';
  const offersVod = !!(p.has_vod || p.has_series);
  const tested = p.status === 'ok' || p.has_vod || p.has_series || p.has_live;

  const offers = [];
  if (p.has_vod) offers.push('<span class="badge badge-accent">Movies</span>');
  if (p.has_series) offers.push('<span class="badge badge-pink">Series</span>');
  if (p.has_live) offers.push('<span class="badge badge-blue">Live TV</span>');
  const offersHtml = offers.length ? offers.join(' ')
    : tested ? '<span style="color:var(--text3)">No movies, series or live channels found.</span>'
    : '<span style="color:var(--text3)">Test it to see what it offers.</span>';

  const meta = [];
  if (p.expiry) meta.push(`Expires ${new Date(p.expiry).toLocaleDateString()}`);
  if (p.max_connections) meta.push(`${p.max_connections} connection${p.max_connections === 1 ? '' : 's'} at once`);
  if (p.last_synced) meta.push(`Synced ${dashTimeAgo(p.last_synced)}`);
  else if (p.last_tested) meta.push(`Tested ${new Date(p.last_tested).toLocaleDateString()}`);

  let uses = '';
  // A switch is shown for what the provider offers (and for one that is on,
  // so it can always be switched off).
  if (offersVod || p.active) {
    uses += _switchRow(p, 'vod', !!p.active, 'Use for Movies &amp; Series',
      'Its films and series in the categories you choose join the library, refreshed every night.');
    if (p.active) uses += _providerSection(p.id, 'cats', 'Categories');
  }
  if (p.has_live || p.live_tv_enabled) {
    uses += _switchRow(p, 'live', !!p.live_tv_enabled, 'Use for Live TV',
      'The channel groups you choose become channels in Jellyfin&rsquo;s Live TV, with a TV guide.');
    if (p.live_tv_enabled) uses += _providerSection(p.id, 'groups', 'Channel groups');
  }

  return `
    <div class="provider-card ${p.status === 'ok' ? 'active-provider' : ''}" id="prov-card-${p.id}">
      <div class="provider-card-header">
        <div style="min-width:0">
          <div class="provider-card-name">${escHtml(p.name)}</div>
          <div class="provider-card-url" title="${escapeAttr(_providerAddress(p))}">${escHtml(_providerAddress(p))}</div>
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
          <div class="dot dot-${statusColor}"></div>
          <span style="font-size:12px;color:var(--text2)">${statusLabel}</span>
        </div>
      </div>
      ${meta.length ? `<div class="provider-meta">${meta.map(m => `<div class="provider-meta-item">${escHtml(m)}</div>`).join('')}</div>` : ''}
      <div class="provider-offers"><span class="provider-offers-label">Offers</span> ${offersHtml}</div>
      ${uses ? `<div class="provider-uses">${uses}</div>` : ''}
      <div class="provider-actions">
        <button class="btn btn-secondary btn-sm" onclick="refreshProvider(${p.id})">Test</button>
        <button class="btn btn-secondary btn-sm" onclick="editProvider(${p.id})">Edit</button>
        <button class="btn btn-danger btn-sm" onclick="confirmDeleteProvider(${p.id}, '${escapeJS(p.name)}')">Delete</button>
      </div>
    </div>`;
}

function _providerSection(id, kind, title) {
  const key = `${id}:${kind}`;
  const open = !!srcState.open[key];
  return `
    <div class="provider-section" id="prov-${kind}-${id}">
      <div class="provider-section-head">
        <div><strong>${title}</strong> <span class="provider-section-sum" id="prov-${kind}-sum-${id}">Loading&hellip;</span></div>
        <button class="btn btn-secondary btn-sm" id="prov-${kind}-btn-${id}" onclick="toggleProviderPanel(${id}, '${escapeJS(kind)}')">${open ? 'Close' : 'Choose'}</button>
      </div>
      <div class="provider-panel" id="prov-${kind}-panel-${id}" style="${open ? '' : 'display:none'}"></div>
    </div>`;
}

async function setProviderUse(id, use, on) {
  const p = srcState.providers.find(x => x.id === id);
  if (!p) return;
  if (!on) {
    const msg = use === 'vod'
      ? `Stop using ${p.name} for movies and series? Nightly syncs stop for it; what it already added stays in the library.`
      : `Stop using ${p.name} for Live TV? Its channels leave Jellyfin's tuner and guide. You can switch it on again later and choose the groups again.`;
    if (!confirm(msg)) return;
  }
  try {
    await api(`/api/providers/${id}`, { method: 'PUT', body: use === 'vod' ? { active: on } : { live_tv_enabled: on } });
    if (on && use === 'vod') {
      srcState.open[`${id}:cats`] = true;
      toast(`${p.name} is used for movies and series. Choose its categories.`, 'success', 5000);
    } else if (on && use === 'live') {
      srcState.open[`${id}:groups`] = true;
      toast(`${p.name} is used for Live TV. Choose its channel groups.`, 'success', 5000);
    } else {
      toast(use === 'vod' ? `${p.name}: movies and series off` : `${p.name}: Live TV off`, 'info');
    }
    await loadProviders();
  } catch (e) {
    toast(e.message, 'error', 8000);
  }
}

function toggleProviderPanel(id, kind) {
  const key = `${id}:${kind}`;
  srcState.open[key] = !srcState.open[key];
  const panel = document.getElementById(`prov-${kind}-panel-${id}`);
  const btn = document.getElementById(`prov-${kind}-btn-${id}`);
  if (panel) panel.style.display = srcState.open[key] ? '' : 'none';
  if (btn) btn.textContent = srcState.open[key] ? 'Close' : 'Choose';
  if (srcState.open[key]) kind === 'cats' ? renderProvCatsPanel(id) : renderProvGroupsPanel(id);
}

// ── Categories (Movies & Series) ─────────────────────────────────────────
async function _loadProviderCats(id, fetchFirst = false) {
  const sum = document.getElementById(`prov-cats-sum-${id}`);
  try {
    let cats = fetchFirst ? [] : await api(`/api/providers/${id}/categories`);
    if (!cats.length) {
      // New provider, or asked for: read them from the provider first.
      if (sum) sum.textContent = 'Reading them from the provider…';
      await api(`/api/providers/${id}/fetch-categories`, { method: 'POST' });
      cats = await api(`/api/providers/${id}/categories`);
    }
    srcState.cats[id] = cats;
  } catch (e) {
    srcState.cats[id] = srcState.cats[id] || [];
    if (sum) sum.textContent = `Could not read them: ${e.message}`;
    return;
  }
  _updateCatsSummary(id);
  if (srcState.open[`${id}:cats`]) renderProvCatsPanel(id);
}

function _updateCatsSummary(id) {
  const sum = document.getElementById(`prov-cats-sum-${id}`);
  const cats = srcState.cats[id] || [];
  if (!sum) return;
  const chosen = cats.filter(c => c.whitelisted);
  const movies = chosen.filter(c => c.type === 'movie').length;
  const series = chosen.length - movies;
  sum.textContent = !cats.length ? '— none found'
    : chosen.length ? `— ${chosen.length} of ${cats.length} chosen (${movies} movie, ${series} series)`
    : `— none of ${cats.length} chosen yet`;
  sum.style.color = chosen.length ? '' : 'var(--amber)';
}

function renderProvCatsPanel(id) {
  const panel = document.getElementById(`prov-cats-panel-${id}`);
  if (!panel) return;
  if (!panel.dataset.built) {
    const f = srcState.catFilter[id] || 'all';
    const fb = (k, label) => `<button class="filter-btn ${f === k ? 'active' : ''}" data-provcatfilter="${k}" onclick="setProvCatFilter(${id}, '${escapeJS(k)}', this)">${label}</button>`;
    panel.innerHTML = `
      <div class="provider-panel-tools">
        <input class="form-input" id="prov-cats-search-${id}" placeholder="Search categories…" aria-label="Search categories" oninput="renderProvCats(${id})">
        <div class="filter-group">${fb('all', 'All')}${fb('movie', 'Movies')}${fb('series', 'Series')}${fb('chosen', 'Chosen')}</div>
        <button class="btn btn-secondary btn-sm" onclick="provCatsSelectShown(${id}, true)">Choose all shown</button>
        <button class="btn btn-secondary btn-sm" onclick="provCatsSelectShown(${id}, false)">Clear shown</button>
      </div>
      <div class="cat-list" id="prov-cats-list-${id}"></div>
      <div class="provider-panel-foot">
        <span id="prov-cats-count-${id}" style="font-size:12px;color:var(--text3)"></span>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-secondary btn-sm" onclick="provCatsRefresh(${id})" title="Ask the provider for its current list">Refresh from provider</button>
          <button class="btn btn-primary btn-sm" onclick="provCatsSave(${id})">Save</button>
        </div>
      </div>
      <div class="form-hint" style="margin-top:8px">New choices are added at the next sync, or <a href="#" onclick="syncEverythingNow();return false" style="color:var(--accent)">sync now</a>.</div>`;
    panel.dataset.built = '1';
  }
  renderProvCats(id);
}

function setProvCatFilter(id, filter, btn) {
  srcState.catFilter[id] = filter;
  btn.closest('.filter-group').querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b === btn));
  renderProvCats(id);
}

function _provCatsShown(id) {
  const q = (document.getElementById(`prov-cats-search-${id}`)?.value || '').toLowerCase();
  const f = srcState.catFilter[id] || 'all';
  return (srcState.cats[id] || []).filter(c => {
    if (q && !String(c.name || '').toLowerCase().includes(q)) return false;
    if (f === 'movie') return c.type === 'movie';
    if (f === 'series') return c.type === 'series';
    if (f === 'chosen') return c.whitelisted;
    return true;
  });
}

function renderProvCats(id) {
  const list = document.getElementById(`prov-cats-list-${id}`);
  if (!list) return;
  const all = srcState.cats[id] || [];
  const cats = _provCatsShown(id);
  const count = document.getElementById(`prov-cats-count-${id}`);
  if (count) count.textContent = `${cats.length} shown · ${all.filter(c => c.whitelisted).length} of ${all.length} chosen`;
  if (!cats.length) {
    list.innerHTML = `<div class="empty-state"><p>${all.length ? 'No category matches.' : 'No categories yet. Refresh from provider.'}</p></div>`;
    return;
  }
  list.innerHTML = cats.map(c => {
    let found = '';
    if (c.last_sync_matched != null || c.last_sync_skipped != null) {
      const matched = c.last_sync_matched || 0;
      const skipped = c.last_sync_skipped || 0;
      found = skipped && !matched
        ? `<span class="cat-found" style="color:var(--red)" title="${skipped} titles had no TMDB match">none matched</span>`
        : matched ? `<span class="cat-found" style="color:var(--green)" title="${matched} matched, ${skipped} skipped">${matched} added</span>` : '';
    }
    return `
    <div class="cat-row ${c.whitelisted ? 'whitelisted' : ''}" data-id="${c.id}">
      <input type="checkbox" class="cat-checkbox" id="prov-cat-${id}-${c.id}" ${c.whitelisted ? 'checked' : ''}
        onchange="provCatToggle(${id}, ${c.id}, this.checked)">
      <label for="prov-cat-${id}-${c.id}" class="cat-name" title="${escapeAttr(c.name)}">${escHtml(c.name)}</label>
      ${found}
      <span class="badge ${c.type === 'movie' ? 'badge-accent' : 'badge-pink'}">${c.type === 'movie' ? 'Movies' : 'Series'}</span>
      ${c.title_count ? `<span style="font-size:11px;color:var(--text3);font-family:'DM Mono',monospace" title="Titles in this category">${c.title_count}</span>` : ''}
    </div>`;
  }).join('');
}

function provCatToggle(id, catId, checked) {
  const c = (srcState.cats[id] || []).find(x => x.id === catId);
  if (c) c.whitelisted = checked;
  document.querySelector(`#prov-cats-list-${id} .cat-row[data-id="${catId}"]`)?.classList.toggle('whitelisted', checked);
  _updateCatsSummary(id);
  const count = document.getElementById(`prov-cats-count-${id}`);
  const all = srcState.cats[id] || [];
  if (count) count.textContent = `${_provCatsShown(id).length} shown · ${all.filter(c => c.whitelisted).length} of ${all.length} chosen`;
}

function provCatsSelectShown(id, on) {
  _provCatsShown(id).forEach(c => { c.whitelisted = on; });
  renderProvCats(id);
  _updateCatsSummary(id);
}

async function provCatsRefresh(id) {
  toast('Reading the categories from the provider…', 'info');
  const keep = new Set((srcState.cats[id] || []).filter(c => c.whitelisted).map(c => c.id));
  await _loadProviderCats(id, true);
  // Unsaved choices survive the refresh
  (srcState.cats[id] || []).forEach(c => { if (keep.has(c.id)) c.whitelisted = true; });
  renderProvCats(id);
  _updateCatsSummary(id);
}

async function provCatsSave(id) {
  const cats = srcState.cats[id] || [];
  const on = cats.filter(c => c.whitelisted).map(c => String(c.id));
  const off = cats.filter(c => !c.whitelisted).map(c => String(c.id));
  try {
    if (on.length) await api(`/api/providers/${id}/categories/update`, { method: 'POST', body: { category_ids: on, whitelisted: true } });
    if (off.length) await api(`/api/providers/${id}/categories/update`, { method: 'POST', body: { category_ids: off, whitelisted: false } });
    toast(`Saved: ${on.length} categor${on.length === 1 ? 'y' : 'ies'} chosen`);
    _updateCatsSummary(id);
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Channel groups (Live TV) ─────────────────────────────────────────────
async function _loadProviderGroups(id) {
  const sum = document.getElementById(`prov-groups-sum-${id}`);
  try {
    const data = await api(`/api/live/groups?provider_id=${id}`);
    srcState.groups[id] = data.groups || [];
  } catch (e) {
    srcState.groups[id] = srcState.groups[id] || [];
    if (sum) sum.textContent = `Could not read them: ${e.message}`;
    return;
  }
  if (!srcState.groups[id].length && !srcState.groupPoll[id]) {
    // Just switched on: read the groups from the provider (groups only, fast).
    provGroupsRefresh(id, true);
  }
  _updateGroupsSummary(id);
  if (srcState.open[`${id}:groups`]) renderProvGroupsPanel(id);
}

function _updateGroupsSummary(id) {
  const sum = document.getElementById(`prov-groups-sum-${id}`);
  if (!sum) return;
  if (srcState.groupPoll[id]) return;   // the sync's own message is showing
  const groups = srcState.groups[id] || [];
  const on = groups.filter(g => g.enabled);
  const channels = on.reduce((n, g) => n + (g.channel_count || 0), 0);
  sum.textContent = !groups.length ? '— none read yet'
    : on.length ? `— ${on.length} of ${groups.length} on (${channels.toLocaleString()} channels)`
    : `— none of ${groups.length} on yet`;
  sum.style.color = on.length ? '' : 'var(--amber)';
}

function renderProvGroupsPanel(id) {
  const panel = document.getElementById(`prov-groups-panel-${id}`);
  if (!panel) return;
  if (!panel.dataset.built) {
    panel.innerHTML = `
      <div class="provider-panel-tools">
        <input class="form-input" id="prov-groups-search-${id}" placeholder="Search groups…" aria-label="Search channel groups" oninput="renderProvGroups(${id})">
        <button class="btn btn-secondary btn-sm" onclick="provGroupsSelectShown(${id}, true)">All shown on</button>
        <button class="btn btn-secondary btn-sm" onclick="provGroupsSelectShown(${id}, false)">All shown off</button>
      </div>
      <div class="cat-list" id="prov-groups-list-${id}"></div>
      <div class="provider-panel-foot">
        <span id="prov-groups-status-${id}" style="font-size:12px;color:var(--text3)"></span>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-secondary btn-sm" onclick="provGroupsRefresh(${id})" title="Ask the provider for its current groups">Refresh from provider</button>
          <button class="btn btn-primary btn-sm" onclick="provGroupsSave(${id})">Save &amp; update channels</button>
        </div>
      </div>
      <div class="form-hint" style="margin-top:8px">Channels, their order and names are set on the <a href="#" onclick="showSourcesTab('live');return false" style="color:var(--accent)">Live TV</a> tab.</div>`;
    panel.dataset.built = '1';
  }
  renderProvGroups(id);
}

function _provGroupsShown(id) {
  const q = (document.getElementById(`prov-groups-search-${id}`)?.value || '').toLowerCase();
  return (srcState.groups[id] || []).filter(g => !q || String(g.name || '').toLowerCase().includes(q));
}

function renderProvGroups(id) {
  const list = document.getElementById(`prov-groups-list-${id}`);
  if (!list) return;
  const groups = _provGroupsShown(id);
  if (!groups.length) {
    list.innerHTML = `<div class="empty-state"><p>${(srcState.groups[id] || []).length ? 'No group matches.' : 'No groups yet. Refresh from provider.'}</p></div>`;
    return;
  }
  list.innerHTML = groups.map(g => `
    <div class="live-group-row" data-group-id="${g.id}">
      <span class="group-name">${escHtml(g.name || '')}</span>
      <span class="group-count">${g.channel_count || 0} ch</span>
      <button type="button" class="live-toggle ${g.enabled ? 'on' : ''}" aria-label="${escapeAttr(g.name || '')} in Live TV"
        aria-pressed="${g.enabled ? 'true' : 'false'}" onclick="provGroupToggle(${id}, ${g.id}, this)"></button>
    </div>`).join('');
}

function provGroupToggle(id, groupId, btn) {
  const g = (srcState.groups[id] || []).find(x => x.id === groupId);
  if (!g) return;
  g.enabled = !g.enabled;
  btn.classList.toggle('on', g.enabled);
  btn.setAttribute('aria-pressed', g.enabled ? 'true' : 'false');
  _updateGroupsSummary(id);
}

function provGroupsSelectShown(id, on) {
  _provGroupsShown(id).forEach(g => { g.enabled = on; });
  renderProvGroups(id);
  _updateGroupsSummary(id);
}

async function provGroupsRefresh(id, quiet = false) {
  try {
    const res = await api(`/api/live/sync/${id}`, { method: 'POST' });
    if (res && res.success === false) { if (!quiet) toast(res.message, 'error'); return; }
    if (!quiet) toast('Reading the channel groups from the provider…', 'info');
    _pollProviderLive(id);
  } catch (e) {
    if (!quiet) toast(e.message, 'error', 8000);
  }
}

async function provGroupsSave(id) {
  const groups = srcState.groups[id] || [];
  const on = groups.filter(g => g.enabled).map(g => g.id);
  const off = groups.filter(g => !g.enabled).map(g => g.id);
  try {
    if (on.length) await api('/api/live/groups/bulk', { method: 'PUT', body: { group_ids: on, enabled: true } });
    if (off.length) await api('/api/live/groups/bulk', { method: 'PUT', body: { group_ids: off, enabled: false } });
    _updateGroupsSummary(id);
    if (!on.length) { toast('Saved: no channel group is on'); return; }
    const res = await api(`/api/live/sync-channels/${id}`, { method: 'POST' });
    if (res && res.success === false) { toast(res.message, 'error'); return; }
    toast('Saved. Fetching the channels and the TV guide…', 'info', 5000);
    _pollProviderLive(id);
  } catch (e) {
    toast(e.message, 'error', 8000);
  }
}

// The group and channel syncs run in the background; the card says how far
// they are, then shows the result.
function _pollProviderLive(id) {
  clearInterval(srcState.groupPoll[id]);
  const say = (msg, color) => {
    for (const elId of [`prov-groups-sum-${id}`, `prov-groups-status-${id}`]) {
      const el = document.getElementById(elId);
      if (el) { el.textContent = msg; el.style.color = color || 'var(--amber)'; }
    }
  };
  srcState.groupPoll[id] = setInterval(async () => {
    let s;
    try { s = await api(`/api/live/sync-status?provider_id=${id}`); } catch (e) { s = { phase: 'error', message: e.message }; }
    const done = s.phase === 'complete' || (s.phase === 'epg' && (s.status === 'complete' || s.status === 'error'));
    if (s.phase === 'error' || done || s.phase === 'idle' || !s.phase) {
      clearInterval(srcState.groupPoll[id]);
      delete srcState.groupPoll[id];
      if (s.phase === 'error') { say(s.message || 'Failed', 'var(--red)'); toast(s.message || 'Failed', 'error', 8000); }
      else if (done) toast(plainWords(s.message || 'Done'), 'success');
      const panel = document.getElementById(`prov-groups-panel-${id}`);
      if (panel) delete panel.dataset.built;
      await _loadProviderGroups(id);
      if (state.sourcesTab === 'live') loadLiveTV();
      return;
    }
    say(plainWords(s.message || 'Working…') + (s.progress != null ? ` (${s.progress}%)` : ''));
  }, 1500);
}

// ── Radarr & Sonarr ──────────────────────────────────────────────────────
async function loadArrSources() {
  const list = document.getElementById('arr-status-list');
  if (list) {
    try {
      const status = await api('/api/settings/connection-status');
      list.innerHTML = ['radarr', 'sonarr'].map(app => {
        const info = status[app] || {};
        const name = app === 'radarr' ? 'Radarr' : 'Sonarr';
        const what = app === 'radarr' ? 'movies' : 'series';
        let dot = 'gray', text = 'Not set up: add its address and key under Settings &rarr; Services.';
        if (info.configured && info.ok) {
          dot = info.vod_root_missing ? 'amber' : 'green';
          text = info.vod_root_missing
            ? 'Connected. It has no root folder for your IPTV shows folder, so &ldquo;Download more episodes&rdquo; of an IPTV series would make a second copy of the show.'
            : `Connected: downloaded ${what} join the library.`;
        } else if (info.configured) {
          dot = 'red';
          text = `Can&rsquo;t connect: ${escHtml(info.error || 'no answer')}`;
        }
        return `<div class="arr-status-row"><div class="dot dot-${dot}"></div><strong>${name}</strong><span>${text}</span></div>`;
      }).join('');
    } catch (e) {
      list.innerHTML = `<div style="color:var(--red);font-size:12px">Could not check: ${escHtml(e.message)}</div>`;
    }
  }
  fillArrWebhookUrls();
}

// Tentacle's own address, as Jellyfin and the Arr apps reach it: the one
// saved in Settings → Services, else the one worked out (not saved yet).
async function tentacleBaseUrl() {
  // The saved one is in the settings; the status call (which also works one
  // out) checks addresses over the network and can take a while.
  try {
    const saved = ((await api('/api/settings/raw')).youtube_base_url || '').trim();
    if (saved) return { base: saved.replace(/\/+$/, ''), saved: true };
  } catch (e) { /* ask the status below */ }
  try {
    const st = await api('/api/youtube/status');
    const base = st.base_url || (st.detected && st.detected.url) || '';
    return { base: base.replace(/\/+$/, ''), saved: !!st.base_url };
  } catch (e) {
    return { base: '', saved: false };
  }
}

async function fillArrWebhookUrls() {
  const { base, saved } = await tentacleBaseUrl();
  const r = document.getElementById('radarr-webhook-url');
  const s = document.getElementById('sonarr-webhook-url');
  if (r) r.value = base ? `${base}/api/radarr/webhook` : '';
  if (s) s.value = base ? `${base}/api/sonarr/webhook` : '';
  const hint = document.getElementById('arr-webhook-hint');
  if (hint) {
    hint.innerHTML = !base
      ? '<span style="color:var(--red)">Tentacle&rsquo;s address is not known yet: set it in <a href="#" onclick="showPage(\'settings\',\'services\');return false" style="color:var(--accent)">Settings &rarr; Services</a>.</span>'
      : saved ? 'Built from Tentacle&rsquo;s address (Settings &rarr; Services).'
      : 'Built from the address Tentacle worked out for itself. Save it in <a href="#" onclick="showPage(\'settings\',\'services\');return false" style="color:var(--accent)">Settings &rarr; Services</a> to be sure it stays.';
  }
}

// Settings → Services: the one Tentacle address. Saved through the YouTube
// address endpoint (it checks the address answers as Tentacle and repoints
// every existing video); the Live TV setup's stored host and port follow it.
async function saveTentacleAddress(inputId) {
  const ok = await ytSaveAddress(inputId);
  if (!ok) return;
  const base = (document.getElementById(inputId)?.value || '').trim();
  try {
    const u = new URL(base);
    const port = u.port || (u.protocol === 'https:' ? '443' : '80');
    await api('/api/settings', { method: 'POST', body: { settings: { live_setup_host: u.hostname, live_setup_port: port } } });
  } catch (e) { /* the address itself is saved; Live TV reads it from there */ }
  fillArrWebhookUrls();
}

// ── Live TV: which provider, and the connection limit ────────────────────
function onLiveProviderChange(value) {
  liveState.providerId = parseInt(value, 10) || null;
  liveState.chPage = 1;
  liveState.dirtyChannels = {};
  loadLiveTV();
}

async function saveLiveStreamLimit() {
  const el = document.getElementById('livetv_max_concurrent_streams');
  const raw = (el?.value || '').trim();
  const n = raw === '' ? '' : parseInt(raw, 10);
  if (raw !== '' && (isNaN(n) || n < 0)) { toast('Enter a whole number, or 0 for no limit', 'error'); return; }
  try {
    await api('/api/settings', { method: 'POST', body: { settings: { livetv_max_concurrent_streams: raw === '' ? '' : String(n) } } });
    toast(n === 0 ? 'Saved: no limit' : `Saved: ${raw === '' ? 'the default' : n} at once. Jellyfin sees the new tuner count at its next guide refresh.`, 'success', 6000);
  } catch (e) {
    toast(e.message, 'error');
  }
}

// Old words in messages the server writes ("EPG sync: …") read as the page does.
function plainWords(text) {
  return String(text || '')
    .replace(/\bEPG\b/g, 'TV guide')
    .replace(/\bSmart ?[Ll]ists?\b/g, m => /s$/.test(m) ? 'Playlists' : 'Playlist');
}
