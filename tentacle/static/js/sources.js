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
  if (tab === 'music') { loadSettings(); if (typeof loadMusicStatusLine === 'function') loadMusicStatusLine(); }
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
    if (document.getElementById(`prov-addr-${p.id}`)) _loadProviderAddresses(p.id);
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

  // Several addresses (Xtream): the one in use and its backups, tested and ranked
  const xtream = !p.provider_type || p.provider_type === 'xtream';
  const addresses = xtream && p.server_url ? _providerSection(p.id, 'addr', 'Address', 'Addresses', true) : '';

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
      'The channel groups you switch on are the channels your household can watch, with a TV guide. Everyone picks their own from them under My Channels.');
    if (p.live_tv_enabled) {
      uses += _providerSection(p.id, 'groups', 'Channel groups for everyone');
      uses += _liveLimitsRow(p);
    }
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
      ${addresses}
      ${uses ? `<div class="provider-uses">${uses}</div>` : ''}
      <div class="provider-actions">
        <button class="btn btn-secondary btn-sm" onclick="refreshProvider(${p.id})">Test</button>
        <button class="btn btn-secondary btn-sm" onclick="editProvider(${p.id})">Edit</button>
        <button class="btn btn-danger btn-sm" onclick="confirmDeleteProvider(${p.id}, '${escapeJS(p.name)}')">Delete</button>
      </div>
    </div>`;
}

function _providerSection(id, kind, title, button = 'Choose', flush = false) {
  const key = `${id}:${kind}`;
  const open = !!srcState.open[key];
  return `
    <div class="provider-section${flush ? ' flush' : ''}" id="prov-${kind}-${id}">
      <div class="provider-section-head">
        <div style="min-width:0"><strong>${title}</strong> <span class="provider-section-sum" id="prov-${kind}-sum-${id}">Loading&hellip;</span></div>
        <button class="btn btn-secondary btn-sm" id="prov-${kind}-btn-${id}" data-label="${escapeAttr(button)}" onclick="toggleProviderPanel(${id}, '${escapeJS(kind)}')">${open ? 'Close' : button}</button>
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
  if (btn) btn.textContent = srcState.open[key] ? 'Close' : (btn.dataset.label || 'Choose');
  if (srcState.open[key]) {
    if (kind === 'cats') renderProvCatsPanel(id);
    else if (kind === 'groups') renderProvGroupsPanel(id);
    else if (kind === 'addr') renderProvAddrPanel(id);
  }
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

// ── Addresses (several per provider) ─────────────────────────────────────
// The provider's own address and the backups it sent. Tested from the
// server one at a time (a one-connection account cuts a stream for a second
// one), re-tested every hour by the provider check, ranked best first.
srcState.addr = {};      // provider id -> GET /api/providers/{id}/addresses
srcState.addrPoll = {};

async function _loadProviderAddresses(id) {
  try {
    srcState.addr[id] = await api(`/api/providers/${id}/addresses`);
  } catch (e) {
    const sum = document.getElementById(`prov-addr-sum-${id}`);
    if (sum) sum.textContent = `Could not read them: ${e.message}`;
    return;
  }
  _updateAddrSummary(id);
  if (srcState.open[`${id}:addr`]) renderProvAddrPanel(id);
  if (srcState.addr[id].test && srcState.addr[id].test.running) _pollAddrTest(id);
}

function _addrHost(url) { return String(url || '').replace(/^https?:\/\//, ''); }

function _addrWord(row) {
  if (!row.tested_at) return { text: 'Not tested yet', cls: 'gray' };
  if (row.word === 'works') return { text: 'Works', cls: 'green' };
  if (row.ok) return { text: 'Login works', cls: 'amber' };
  const w = row.word || 'fails';
  return { text: w.charAt(0).toUpperCase() + w.slice(1), cls: 'red' };
}

function _updateAddrSummary(id) {
  const sum = document.getElementById(`prov-addr-sum-${id}`);
  const data = srcState.addr[id];
  if (!sum || !data) return;
  const rows = data.addresses || [];
  const cur = rows.find(r => r.in_use);
  const backups = rows.length - (cur ? 1 : 0);
  const word = cur ? _addrWord(cur) : null;
  // The address itself is under the provider's name: here, how it does and the backups
  sum.innerHTML = (word && cur.tested_at ? `— <span class="addr-word ${word.cls}">${escHtml(word.text.toLowerCase())}</span> · ` : '— ')
    + `<span style="color:var(--text3)">${backups ? `${backups} backup${backups === 1 ? '' : 's'}` : 'no backups yet'}</span>`;
  sum.style.color = '';
}

function _addrDetails(r) {
  const bits = [];
  if (r.connections && r.connections.max != null) bits.push(`${r.connections.active ?? 0}/${r.connections.max} in use`);
  if (r.live_seconds != null) bits.push(`live ${r.live_seconds} s`);
  else if (r.live_word) bits.push(`live: ${r.live_word}`);
  if (r.vod_seconds != null) bits.push(`film ${r.vod_seconds} s`);
  else if (r.vod_word) bits.push(`film: ${r.vod_word}`);
  if (r.streams_skipped) bits.push(`streams not tried: ${r.streams_skipped}`);
  if (r.cloudflare) bits.push('Cloudflare');
  return bits.join(' · ');
}

function _addrDay(r) {
  const n = (r.day_good || 0) + (r.day_bad || 0);
  if (!n) return '';
  return `<span class="addr-day" title="Tests and hourly checks in the last 24 hours">${r.day_good}/${n} today</span>`;
}

function renderProvAddrPanel(id) {
  const panel = document.getElementById(`prov-addr-panel-${id}`);
  const data = srcState.addr[id];
  if (!panel) return;
  if (!data) { panel.innerHTML = '<div class="form-hint">Loading&hellip;</div>'; return; }
  const running = data.test && data.test.running;
  const rows = (data.addresses || []).map(r => {
    const w = _addrWord(r);
    const details = _addrDetails(r);
    const actions = r.in_use
      ? '<span class="addr-inuse">In use</span>'
      : `<button class="btn btn-secondary btn-sm" onclick="useProviderAddress(${id}, ${r.id})">Use this one</button>
         <button class="addr-remove" title="Remove" aria-label="Remove ${escapeAttr(_addrHost(r.url))}" onclick="removeProviderAddress(${id}, ${r.id})">&times;</button>`;
    return `
      <div class="addr-row${r.in_use ? ' in-use' : ''}">
        <div class="dot dot-${w.cls}"></div>
        <div class="addr-main">
          <div class="addr-url mono" title="${escapeAttr(r.url)}">${escHtml(_addrHost(r.url))}${r.preferred && !r.in_use ? ' <span class="addr-pref">your choice</span>' : ''}</div>
          <div class="addr-result"><span class="addr-word ${w.cls}">${escHtml(w.text)}</span>${details ? ` <span class="addr-detail">· ${escHtml(details)}</span>` : ''} ${_addrDay(r)}</div>
        </div>
        <div class="addr-actions">${actions}</div>
      </div>`;
  }).join('');
  const prog = running
    ? `Testing ${escHtml(_addrHost(data.test.current || ''))} (${(data.test.done || 0) + 1} of ${data.test.total || '?'})&hellip;`
    : 'One address at a time; streams are skipped while anyone is watching.';
  const mode = data.auto_switch || 'switch';
  panel.innerHTML = `
    <div class="addr-list">${rows}</div>
    <div class="provider-panel-foot">
      <span class="form-hint" id="prov-addr-prog-${id}" style="margin:0">${prog}</span>
      <button class="btn btn-secondary btn-sm" id="prov-addr-test-${id}" onclick="testProviderAddresses(${id})" ${running ? 'disabled' : ''}>${running ? 'Testing&hellip;' : 'Test all'}</button>
    </div>
    <div class="addr-add">
      <textarea class="form-input" id="prov-addr-text-${id}" rows="2" placeholder="Paste the provider&rsquo;s message with its other addresses" aria-label="Addresses to add"></textarea>
      <button class="btn btn-secondary btn-sm" onclick="addProviderAddresses(${id})">Add</button>
    </div>
    <label class="addr-mode">When the address in use fails
      <select class="form-input" onchange="setAddressAutoSwitch(${id}, this.value)" aria-label="When the address in use fails">
        <option value="switch" ${mode === 'switch' ? 'selected' : ''}>switch to the best backup</option>
        <option value="switch_back" ${mode === 'switch_back' ? 'selected' : ''}>switch, and come back when it works again</option>
        <option value="off" ${mode === 'off' ? 'selected' : ''}>only tell me</option>
      </select>
    </label>`;
}

async function addProviderAddresses(id) {
  const el = document.getElementById(`prov-addr-text-${id}`);
  const text = (el?.value || '').trim();
  if (!text) { toast('Paste the addresses first', 'error'); return; }
  try {
    const res = await api(`/api/providers/${id}/addresses`, { method: 'POST', body: { text } });
    srcState.addr[id] = res;
    const n = res.added.length;
    toast(n ? `${n} address${n === 1 ? '' : 'es'} added. Test all to see which work.`
      : 'Those addresses are already in the list', n ? 'success' : 'info', 5000);
    renderProvAddrPanel(id);
    _updateAddrSummary(id);
  } catch (e) {
    toast(e.message, 'error', 6000);
  }
}

async function removeProviderAddress(id, addressId) {
  try {
    srcState.addr[id] = await api(`/api/providers/${id}/addresses/${addressId}`, { method: 'DELETE' });
    renderProvAddrPanel(id);
    _updateAddrSummary(id);
  } catch (e) {
    toast(e.message, 'error', 6000);
  }
}

async function useProviderAddress(id, addressId) {
  const data = srcState.addr[id] || {};
  const row = (data.addresses || []).find(r => r.id === addressId);
  const p = srcState.providers.find(x => x.id === id);
  if (!row || !p) return;
  if (!confirm(`Use ${_addrHost(row.url)} for ${p.name}?\n\nNothing is imported again or deleted: the library, channels, guide and recordings stay as they are. Streams already playing finish on the old address.`)) return;
  try {
    const res = await api(`/api/providers/${id}/addresses/${addressId}/use`, { method: 'POST' });
    srcState.addr[id] = res;
    toast(res.switched ? `${p.name} now uses ${_addrHost(row.url)}` : 'That one is already in use', 'success', 5000);
    await loadProviders();
  } catch (e) {
    toast(e.message, 'error', 8000);
  }
}

async function testProviderAddresses(id) {
  try {
    srcState.addr[id] = await api(`/api/providers/${id}/addresses/test`, { method: 'POST' });
    renderProvAddrPanel(id);
    _pollAddrTest(id);
  } catch (e) {
    toast(e.message, 'error', 6000);
  }
}

function _pollAddrTest(id) {
  clearInterval(srcState.addrPoll[id]);
  srcState.addrPoll[id] = setInterval(async () => {
    let data;
    try { data = await api(`/api/providers/${id}/addresses`); } catch (e) { return; }
    srcState.addr[id] = data;
    if (srcState.open[`${id}:addr`]) renderProvAddrPanel(id);
    _updateAddrSummary(id);
    if (!data.test || !data.test.running) {
      clearInterval(srcState.addrPoll[id]);
      delete srcState.addrPoll[id];
      const works = (data.addresses || []).filter(r => r.word === 'works').length;
      toast(`Tested: ${works} of ${(data.addresses || []).length} work`, works ? 'success' : 'error', 5000);
    }
  }, 2000);
}

async function setAddressAutoSwitch(id, mode) {
  try {
    srcState.addr[id] = await api(`/api/providers/${id}/addresses/settings`, { method: 'PUT', body: { auto_switch: mode } });
    toast(mode === 'off' ? 'Saved: you are told, nothing switches' : 'Saved', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Live TV per provider: streams at once, channel numbers ───────────────
function _liveLimitsRow(p) {
  const account = p.max_connections ? `The account says ${p.max_connections}.` : '';
  const off = p.channel_number_offset;
  return `
    <div class="provider-live-limits">
      <label>Streams at once
        <input type="number" min="0" max="50" class="form-input" value="${p.live_max_streams ?? ''}" placeholder="6"
          aria-label="Streams at once (${escapeAttr(p.name)})" onchange="saveProviderLiveNumber(${p.id}, 'live_max_streams', this)">
      </label>
      <span class="form-hint" style="margin:0">${account} Live TV, recordings and films share them.</span>
      <label>Channel numbers +
        <input type="number" min="0" step="1000" class="form-input wide" value="${off ?? ''}" placeholder="automatic"
          aria-label="Added to the channel numbers (${escapeAttr(p.name)})" onchange="saveProviderLiveNumber(${p.id}, 'channel_number_offset', this)">
      </label>
    </div>`;
}

async function saveProviderLiveNumber(id, field, el) {
  const raw = (el.value || '').trim();
  const n = raw === '' ? 0 : parseInt(raw, 10);
  if (isNaN(n) || n < 0) { toast('Enter a whole number', 'error'); return; }
  if (field === 'channel_number_offset' && !confirm('Jellyfin sees this provider\u2019s channels under new numbers at its next guide refresh. Recordings set up on the old numbers need setting up again. Change them?')) {
    loadProviders();
    return;
  }
  try {
    await api(`/api/providers/${id}`, { method: 'PUT', body: { [field]: n } });
    toast(field === 'live_max_streams'
      ? (n ? `Saved: ${n} at once` : 'Saved: the default (6)')
      : 'Saved: Jellyfin picks the numbers up at its next guide refresh', 'success', 5000);
    const p = srcState.providers.find(x => x.id === id);
    if (p) p[field] = n || null;
  } catch (e) {
    toast(e.message, 'error', 6000);
  }
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
  if (!ok) return false;
  const base = (document.getElementById(inputId)?.value || '').trim();
  try {
    const u = new URL(base);
    const port = u.port || (u.protocol === 'https:' ? '443' : '80');
    await api('/api/settings', { method: 'POST', body: { settings: { live_setup_host: u.hostname, live_setup_port: port } } });
  } catch (e) { /* the address itself is saved; Live TV reads it from there */ }
  fillArrWebhookUrls();
  if (typeof loadTentacleAddress === 'function') loadTentacleAddress();
  return true;
}

// ── Live TV: which provider's channels are shown ────────────────────────
function onLiveProviderChange(value) {
  liveState.providerId = parseInt(value, 10) || null;
  liveState.chPage = 1;
  liveState.dirtyChannels = {};
  loadLiveTV();
}

// Old words in messages the server writes ("EPG sync: …") read as the page does.
function plainWords(text) {
  return String(text || '')
    .replace(/\bEPG\b/g, 'TV guide')
    .replace(/\bSmart ?[Ll]ists?\b/g, m => /s$/.test(m) ? 'Playlists' : 'Playlist');
}
