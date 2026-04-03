// Tentacle - Pages JS
// History, Library, Collections, Duplicates, Lists page logic

// ── Shared state ──────────────────────────────────────────────────────────
const pages = {
  lib: { type: 'all', src: 'all', sourceTag: null, listId: null, listStatus: null, sort: 'date_desc', offset: 0, limit: 48, items: [], total: 0 },
  dup: { filter: 'pending', items: [] },
  history: { runs: [], snapshots: {} },
  chart: null,
};

// ── Utility ───────────────────────────────────────────────────────────────
function escapeAttr(str) {
  if (!str) return '';
  return String(str).replace(/&/g,'&amp;').replace(/'/g,'&#39;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function escapeJS(str) {
  if (!str) return '';
  return String(str).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"');
}

// ── HISTORY PAGE ──────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const data = await api('/api/sync/history?limit=50');
    pages.history.runs = data.runs;
    renderHistorySummary(data.runs);
    renderHistoryRuns(data.runs);
    populateChartCategories(data.runs);
  } catch (e) {
    document.getElementById('history-runs').innerHTML =
      '<div class="empty-state"><p>Failed to load history</p></div>';
  }
}

function renderHistorySummary(runs) {
  const el = document.getElementById('history-summary');
  if (!runs.length) { el.innerHTML = ''; return; }

  const totalMovies = runs.reduce((s, r) => s + (r.movies_new || 0), 0);
  const totalSeries = runs.reduce((s, r) => s + (r.series_new || 0), 0);
  const avgDuration = runs.filter(r => r.duration_seconds)
    .reduce((s, r, _, a) => s + r.duration_seconds / a.length, 0);

  el.innerHTML = `
    <div class="stat-card">
      <div class="stat-accent" style="background:var(--accent)"></div>
      <div class="stat-label">Total Runs</div>
      <div class="stat-value">${runs.length}</div>
      <div class="stat-sub">${runs.filter(r=>r.status==='completed').length} successful</div>
    </div>
    <div class="stat-card">
      <div class="stat-accent" style="background:var(--green)"></div>
      <div class="stat-label">Movies Added</div>
      <div class="stat-value">${totalMovies.toLocaleString()}</div>
      <div class="stat-sub">across all runs</div>
    </div>
    <div class="stat-card">
      <div class="stat-accent" style="background:var(--pink)"></div>
      <div class="stat-label">Series Added</div>
      <div class="stat-value">${totalSeries.toLocaleString()}</div>
      <div class="stat-sub">avg ${Math.round(avgDuration/60)}m per sync</div>
    </div>`;
}

function renderHistoryRuns(runs) {
  const el = document.getElementById('history-runs');
  if (!runs.length) {
    el.innerHTML = '<div class="empty-state" style="padding:40px"><p>No sync runs yet. Run your first sync to see history here.</p></div>';
    return;
  }

  el.innerHTML = runs.map(run => {
    const date = run.started_at ? new Date(run.started_at) : null;
    const dateStr = date ? date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '—';
    const duration = run.duration_seconds ? `${Math.round(run.duration_seconds/60)}m ${run.duration_seconds%60}s` : '—';
    const statusColor = run.status === 'completed' ? 'green' : run.status === 'running' ? 'amber' : 'red';

    const catStats = run.category_stats || {};
    const catPills = Object.entries(catStats)
      .sort((a,b) => (b[1].new||0) - (a[1].new||0))
      .slice(0, 8)
      .map(([name, stats]) => `
        <span class="run-cat-pill ${stats.new > 0 ? 'has-new' : ''}">
          ${name.split(' - ').pop()} ${stats.new > 0 ? `+${stats.new}` : stats.total || ''}
        </span>`).join('');

    return `
      <div class="run-row">
        <div class="run-header">
          <div class="dot dot-${statusColor}"></div>
          <span style="font-size:13px;font-weight:500">${run.provider_name}</span>
          <span class="badge badge-gray">${run.sync_type}</span>
          <span style="font-size:12px;color:var(--text3);font-family:'DM Mono',monospace">${dateStr}</span>
          <span style="font-size:11px;color:var(--text3);margin-left:auto">⏱ ${duration}</span>
        </div>
        <div class="run-numbers">
          <div class="run-number"><span>+${run.movies_new||0}</span>movies new</div>
          <div class="run-number"><span>${run.movies_existing||0}</span>existing</div>
          <div class="run-number"><span>${run.movies_skipped||0}</span>skipped</div>
          <div class="run-number"><span>+${run.series_new||0}</span>series new</div>
          ${run.movies_failed > 0 ? `<div class="run-number" style="color:var(--red)"><span>${run.movies_failed}</span>failed</div>` : ''}
        </div>
        ${catPills ? `<div class="run-cats">${catPills}</div>` : ''}
        ${run.error_message ? `<div style="margin-top:8px;font-size:12px;color:var(--red);font-family:'DM Mono',monospace">${run.error_message}</div>` : ''}
      </div>`;
  }).join('');
}

function populateChartCategories(runs) {
  const cats = new Set();
  runs.forEach(run => {
    Object.keys(run.category_stats || {}).forEach(c => cats.add(c));
  });

  const sel = document.getElementById('chart-category');
  const current = sel.value;
  sel.innerHTML = '<option value="">Select a category...</option>' +
    [...cats].sort().map(c => `<option value="${c}" ${c===current?'selected':''}>${c}</option>`).join('');
}

function renderCategoryChart() {
  const category = document.getElementById('chart-category').value;
  const canvas = document.getElementById('category-chart');
  const empty = document.getElementById('chart-empty');

  if (!category) {
    canvas.style.display = 'none';
    empty.style.display = 'block';
    return;
  }

  const runs = pages.history.runs.filter(r =>
    r.category_stats && r.category_stats[category] && r.started_at
  ).reverse();

  if (!runs.length) {
    canvas.style.display = 'none';
    empty.style.display = 'block';
    empty.textContent = 'No data for this category yet';
    return;
  }

  canvas.style.display = 'block';
  empty.style.display = 'none';

  const labels = runs.map(r => new Date(r.started_at).toLocaleDateString());
  const totals = runs.map(r => r.category_stats[category].total || 0);
  const news = runs.map(r => r.category_stats[category].new || 0);

  if (pages.chart) pages.chart.destroy();

  pages.chart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'Total',
          data: totals,
          backgroundColor: 'rgba(108,99,255,0.2)',
          borderColor: 'rgba(108,99,255,0.6)',
          borderWidth: 1,
          type: 'line',
          tension: 0.3,
          fill: true,
          yAxisID: 'y',
        },
        {
          label: 'New',
          data: news,
          backgroundColor: 'rgba(74,222,128,0.5)',
          borderColor: 'rgba(74,222,128,0.8)',
          borderWidth: 1,
          yAxisID: 'y1',
        }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { color: '#9090a8', font: { family: 'DM Mono', size: 11 } } } },
      scales: {
        x: { ticks: { color: '#5a5a72', font: { family: 'DM Mono', size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' } },
        y: { ticks: { color: '#5a5a72', font: { family: 'DM Mono', size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' }, position: 'left' },
        y1: { ticks: { color: '#4ade80', font: { family: 'DM Mono', size: 10 } }, grid: { drawOnChartArea: false }, position: 'right' },
      }
    }
  });
}

// ── LIBRARY PAGE ──────────────────────────────────────────────────────────
let _libEventSource = null;
let _libRefreshTimer = null;

function _debouncedLibRefresh() {
  clearTimeout(_libRefreshTimer);
  _libRefreshTimer = setTimeout(async () => {
    const grid = document.getElementById('lib-grid');
    if (!grid) return;
    grid.style.transition = 'opacity 0.3s ease';
    grid.style.opacity = '0.4';
    pages.lib.offset = 0;
    pages.lib.items = [];
    await fetchLibraryPage();
    grid.style.opacity = '1';
  }, 2000);
}

function connectLibraryStream() {
  if (_libEventSource) return;
  _libEventSource = new EventSource('/api/library/stream');

  _libEventSource.addEventListener('movie_added', () => _debouncedLibRefresh());
  _libEventSource.addEventListener('series_added', () => _debouncedLibRefresh());

  _libEventSource.addEventListener('movie_removed', (e) => {
    const data = JSON.parse(e.data);
    const grid = document.getElementById('lib-grid');
    if (!grid) return;
    const card = grid.querySelector(`[data-tmdb-id="${data.tmdb_id}"]`);
    if (card) {
      card.classList.add('lib-card-exit');
      card.addEventListener('animationend', () => card.remove());
      pages.lib.items = pages.lib.items.filter(i => i.tmdb_id !== data.tmdb_id);
      pages.lib.total = Math.max(0, pages.lib.total - 1);
    }
  });

  _libEventSource.addEventListener('series_removed', (e) => {
    const data = JSON.parse(e.data);
    const grid = document.getElementById('lib-grid');
    if (!grid) return;
    const card = grid.querySelector(`[data-tmdb-id="${data.tmdb_id}"]`);
    if (card) {
      card.classList.add('lib-card-exit');
      card.addEventListener('animationend', () => card.remove());
      pages.lib.items = pages.lib.items.filter(i => i.tmdb_id !== data.tmdb_id);
      pages.lib.total = Math.max(0, pages.lib.total - 1);
    }
  });

  _libEventSource.onerror = () => {
    _libEventSource.close();
    _libEventSource = null;
    // Reconnect after 5s if still on library page
    setTimeout(() => {
      if (state.currentPage === 'library') connectLibraryStream();
    }, 5000);
  };
}

function disconnectLibraryStream() {
  if (_libEventSource) {
    _libEventSource.close();
    _libEventSource = null;
  }
}

async function loadLibrary() {
  connectLibraryStream();
  pages.lib.offset = 0;
  pages.lib.items = [];
  loadLibListPills();
  loadLibStats();
  loadLibDownloads();
  loadLibSyncSummary();
  checkStaleFiles();
  await fetchLibraryPage();
  // Update duplicates tab badge
  _updateDupBadges();
  // Update following tab badge
  _updateFollowBadge();
}

// ── Library Stats Bar ──
async function loadLibStats() {
  try {
    const d = await api('/api/sync/dashboard');
    const lib = d.library || {};
    const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
    el('lib-stat-movies', (lib.total_movies ?? 0).toLocaleString());
    el('lib-stat-series', (lib.total_series ?? 0).toLocaleString());
    el('lib-stat-downloaded', ((lib.radarr_movies || 0) + (lib.sonarr_series || 0)).toLocaleString());
    el('lib-stat-vod', ((lib.vod_movies || 0) + (lib.vod_series || 0)).toLocaleString());
    // Duplicate badge
    const dup = document.getElementById('dup-badge');
    if (dup) {
      const pending = lib.pending_duplicates || 0;
      if (pending > 0) { dup.style.display = 'inline'; dup.textContent = pending; }
      else { dup.style.display = 'none'; }
    }
    // Jellyfin connection banner
    const cfg = d.config || {};
    const jfBanner = document.getElementById('jellyfin-error-banner');
    if (jfBanner) {
      if (!cfg.jellyfin_url_configured) {
        document.getElementById('jellyfin-banner-title').textContent = 'Jellyfin Not Configured';
        document.getElementById('jellyfin-banner-msg').textContent = 'Set up your Jellyfin connection in Settings → Connections';
        jfBanner.style.display = '';
      } else if (cfg.jellyfin_connected === false) {
        document.getElementById('jellyfin-banner-title').textContent = 'Jellyfin Connection Failed';
        document.getElementById('jellyfin-banner-msg').textContent = 'Check your API key in Settings → Connections';
        jfBanner.style.display = '';
      } else {
        jfBanner.style.display = 'none';
      }
    }
    // Update sidebar last sync
    const ts = d.status?.vod_sync?.timestamp;
    if (ts) {
      const sidebarSync = document.getElementById('sidebar-last-sync');
      if (sidebarSync) sidebarSync.textContent = dashTimeAgo(ts) || '—';
    }
  } catch (e) { /* stats are non-critical */ }
}

// ── Library Downloads Section ──
let _dlPollTimer = null;
let _dlPollActive = false;

async function loadLibDownloads() {
  try {
    const data = await api('/api/activity');
    renderLibDownloads(data);
    // Start polling only if there are active downloads
    const hasActive = data.downloads && data.downloads.length > 0;
    if (hasActive && !_dlPollTimer) {
      _dlPollActive = true;
      _dlPollTimer = setInterval(pollLibDownloads, 5000);
    } else if (!hasActive) {
      stopDownloadPolling();
    }
  } catch (e) {
    // If endpoint not available, hide section silently
    const el = document.getElementById('lib-downloads');
    if (el) el.style.display = 'none';
  }
}

async function pollLibDownloads() {
  if (state.currentPage !== 'library') { stopDownloadPolling(); return; }
  try {
    const data = await api('/api/activity');
    renderLibDownloads(data);
    if (!data.downloads || data.downloads.length === 0) {
      stopDownloadPolling();
    }
  } catch (e) { stopDownloadPolling(); }
}

function stopDownloadPolling() {
  if (_dlPollTimer) { clearInterval(_dlPollTimer); _dlPollTimer = null; }
  _dlPollActive = false;
}

function renderLibDownloads(data) {
  const container = document.getElementById('lib-downloads');
  const body = document.getElementById('lib-dl-body');
  const countEl = document.getElementById('lib-dl-count');
  const badge = document.getElementById('dl-badge');
  if (!container || !body) return;

  const dls = data.downloads || [];
  if (badge) {
    badge.style.display = dls.length > 0 ? '' : 'none';
    badge.textContent = dls.length;
  }
  if (dls.length === 0) {
    container.style.display = 'none';
    return;
  }
  container.style.display = '';
  countEl.textContent = `(${dls.length})`;
  body.innerHTML = dls.map(d => {
    const pct = d.progress != null ? Math.round(d.progress) : 0;
    const status = (d.status || 'downloading').toLowerCase();
    const statusClass = status.includes('import') ? 'importing' : status.includes('queue') ? 'queued' : 'downloading';
    const eta = d.eta ? ` · ${d.eta}` : '';
    const qual = d.quality ? ` · ${d.quality}` : '';
    return `<div class="dl-item">
      <div class="dl-item-title">${d.title || 'Unknown'}${d.episode ? ' — ' + d.episode : ''}</div>
      <div class="dl-item-status ${statusClass}">${status}${qual}${eta}</div>
      <div class="dl-item-bar"><div class="dl-item-bar-fill" style="width:${pct}%"></div></div>
      <div class="dl-item-pct">${pct}%</div>
    </div>`;
  }).join('');
}

// ── Library Sync Summary ──
async function loadLibSyncSummary() {
  const container = document.getElementById('lib-sync-summary');
  const body = document.getElementById('lib-sync-body');
  const timeEl = document.getElementById('lib-sync-time');
  if (!container || !body) return;
  try {
    const data = await api('/api/sync/summary');
    if (!data || !data.completed_at) {
      container.style.display = 'none';
      return;
    }
    container.style.display = '';
    timeEl.textContent = dashTimeAgo(data.completed_at) || data.completed_at || '';
    let html = '';
    // VOD providers
    if (data.providers && data.providers.length > 0) {
      data.providers.forEach(p => {
        let parts = [];
        if (p.new_movies > 0 || p.new_series > 0) {
          if (p.new_movies > 0) parts.push(`${p.new_movies} new movie${p.new_movies !== 1 ? 's' : ''}`);
          if (p.new_series > 0) parts.push(`${p.new_series} new series`);
        }
        if (p.new_categories && p.new_categories.length > 0) {
          parts.push(`<span style="color:var(--amber)">${p.new_categories.length} new categor${p.new_categories.length !== 1 ? 'ies' : 'y'}: ${p.new_categories.join(', ')}</span> <a href="#" onclick="showPage('vod');return false" style="color:var(--accent);font-size:11px">Enable →</a>`);
        }
        if (parts.length > 0) {
          html += `<div style="margin-bottom:6px"><strong>${p.name}:</strong> ${parts.join(' · ')}</div>`;
        }
      });
    }
    // Radarr/Sonarr
    if (data.radarr_new > 0 || data.sonarr_new > 0) {
      let arrParts = [];
      if (data.radarr_new > 0) arrParts.push(`${data.radarr_new} new Radarr movie${data.radarr_new !== 1 ? 's' : ''}`);
      if (data.sonarr_new > 0) arrParts.push(`${data.sonarr_new} new Sonarr series`);
      html += `<div style="margin-bottom:6px"><strong>Downloads:</strong> ${arrParts.join(', ')}</div>`;
    }
    // Lists
    if (data.lists_updated && data.lists_updated.length > 0) {
      html += `<div style="margin-bottom:6px"><strong>Lists:</strong> ${data.lists_updated.join(', ')} updated</div>`;
    }
    if (!html) {
      html = '<div style="color:var(--text3)">No new content since last sync</div>';
    } else {
      html += `<div style="margin-top:8px"><a href="#" onclick="setLibSort('date_desc');document.getElementById('lib-sort').value='date_desc';fetchLibraryPage();return false" style="color:var(--accent);font-size:12px">View recently added →</a></div>`;
    }
    body.innerHTML = html;
  } catch (e) {
    container.style.display = 'none';
  }
}

// ── Stale files check (moved from Dashboard) ──
let _staleChecked = false;
async function checkStaleFiles() {
  if (_staleChecked) return;
  _staleChecked = true;
  if (!state.currentUser?.is_admin) return;
  try {
    const data = await api('/api/settings/stale-files');
    if (data.show || data.has_stale) {
      _staleData = data;
      document.getElementById('stale-files-banner').style.display = '';
      document.getElementById('stale-strm-count').textContent = data.strm_count || 0;
    }
  } catch (e) { /* ignore */ }
}

async function loadLibListPills() {
  const container = document.getElementById('lib-list-pills');
  const row = document.getElementById('lib-list-row');
  if (!container) return;
  try {
    const lists = await api('/api/lists');
    if (!lists.length) {
      container.innerHTML = '';
      if (row) row.style.display = 'none';
      return;
    }
    if (row) row.style.display = 'flex';
    const active = pages.lib.listId;
    container.innerHTML = lists.map(l =>
      `<button class="lib-list-pill${active === l.id ? ' active' : ''}" data-list-id="${l.id}" onclick="setLibList(${l.id},this)">${escapeAttr(l.name)}${l.last_item_count ? ` <span style="opacity:0.5;font-size:11px">${l.last_item_count}</span>` : ''}</button>`
    ).join('');
    // Check scroll arrows after render
    setTimeout(() => updateListScrollArrows(), 50);
  } catch (e) {
    container.innerHTML = '';
    if (row) row.style.display = 'none';
  }
}

function updateListScrollArrows() {
  const inner = document.getElementById('lib-list-pills');
  const leftBtn = document.getElementById('lib-list-scroll-left');
  const rightBtn = document.getElementById('lib-list-scroll-right');
  if (!inner || !leftBtn || !rightBtn) return;
  const hasOverflow = inner.scrollWidth > inner.clientWidth + 2;
  leftBtn.classList.toggle('hidden', !hasOverflow || inner.scrollLeft <= 0);
  rightBtn.classList.toggle('hidden', !hasOverflow || inner.scrollLeft >= inner.scrollWidth - inner.clientWidth - 2);
}

function scrollListPills(dir) {
  const inner = document.getElementById('lib-list-pills');
  if (!inner) return;
  inner.scrollBy({ left: dir * 200, behavior: 'smooth' });
  setTimeout(() => updateListScrollArrows(), 350);
}

function setLibList(listId, btn) {
  const isDeselect = pages.lib.listId === listId;
  if (isDeselect) {
    // Exit list mode
    pages.lib.listId = null;
    pages.lib.listStatus = null;
    btn.classList.remove('active');
    _exitListMode();
  } else {
    // Enter list mode
    pages.lib.listId = listId;
    pages.lib.listStatus = null;
    document.querySelectorAll('#lib-list-pills .lib-list-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _enterListMode();
  }
  pages.lib.offset = 0;
  pages.lib.items = [];
  fetchLibraryPage();
}

function _enterListMode() {
  // Deselect type + source filters
  pages.lib.type = 'all';
  pages.lib.src = 'all';
  pages.lib.sourceTag = null;
  pages.lib.listStatus = 'in_library';  // Library is browse-only — no missing items
  document.querySelectorAll('[data-libtype]').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('[data-libsrc]').forEach(b => b.classList.remove('active'));
  // Dim the type/source groups
  const typeGrp = document.getElementById('lib-type-group');
  const srcGrp = document.getElementById('lib-src-group');
  if (typeGrp) typeGrp.style.opacity = '0.35';
  if (srcGrp) srcGrp.style.opacity = '0.35';
  // Hide list status sub-filter (Library is browse-only, no Missing filter)
  const statusEl = document.getElementById('lib-list-status');
  if (statusEl) statusEl.classList.add('hidden');
}

function _exitListMode() {
  // Restore type/source filters
  document.querySelector('[data-libtype="all"]')?.classList.add('active');
  document.querySelector('[data-libsrc="all"]')?.classList.add('active');
  const typeGrp = document.getElementById('lib-type-group');
  const srcGrp = document.getElementById('lib-src-group');
  if (typeGrp) typeGrp.style.opacity = '1';
  if (srcGrp) srcGrp.style.opacity = '1';
  // Hide list status sub-filter
  const statusEl = document.getElementById('lib-list-status');
  if (statusEl) statusEl.classList.add('hidden');
}

function setLibListStatus(status, btn) {
  pages.lib.listStatus = status === 'all' ? null : status;
  document.querySelectorAll('[data-liststatus]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  pages.lib.offset = 0;
  pages.lib.items = [];
  fetchLibraryPage();
}

function setLibSort(value) {
  pages.lib.sort = value;
  pages.lib.offset = 0;
  pages.lib.items = [];
  fetchLibraryPage();
}

async function fetchLibraryPage() {
  const { type, src, sourceTag, listId, listStatus, sort, offset, limit } = pages.lib;
  const el = document.getElementById('lib-grid');

  if (offset === 0) {
    el.innerHTML = '<div class="loading-state" style="grid-column:1/-1"><div class="spinner"></div></div>';
  }

  try {
    const params = new URLSearchParams({ limit, offset });
    if (listId) {
      params.set('list_id', listId);
      if (listStatus) params.set('list_status', listStatus);
    } else {
      if (type !== 'all') params.set('media_type', type);
      if (src !== 'all') params.set('source', src);
      if (sourceTag) params.set('source_tag', sourceTag);
    }
    if (sort && sort !== 'date_desc') params.set('sort', sort);
    const search = document.getElementById('lib-search')?.value.trim();
    if (search) params.set('search', search);

    const data = await api(`/api/library/items?${params}`);
    pages.lib.total = data.total;

    if (offset === 0) {
      pages.lib.items = data.items;
      el.innerHTML = '';
      if (!listId) renderSourcePills(data.source_breakdown || {});
      else document.getElementById('lib-source-pills').innerHTML = '';
      // Update stats bar
      const statsEl = document.getElementById('lib-stats');
      if (statsEl) statsEl.textContent = `${data.total.toLocaleString()} item${data.total !== 1 ? 's' : ''}`;
    } else {
      pages.lib.items = [...pages.lib.items, ...data.items];
    }

    data.items.forEach(item => {
      el.insertAdjacentHTML('beforeend', renderLibCard(item));
    });

    const loadMore = document.getElementById('lib-load-more');
    loadMore.style.display = pages.lib.items.length < pages.lib.total ? 'block' : 'none';

    if (!data.items.length && offset === 0) {
      el.innerHTML = `<div class="empty-state" style="grid-column:1/-1">
        <div class="empty-icon">◫</div>
        <p>${listId ? 'No items found. Fetch the list first.' : 'No content yet. Run a sync to populate your library.'}</p>
      </div>`;
    }
  } catch (e) {
    if (offset === 0) el.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><p>Failed to load library</p></div>';
  }
}

function renderLibCard(item) {
  // Library is browse-only — skip missing items entirely
  if (item.in_library === false) return '';

  // Normal in-library card
  const poster = item.poster_path
    ? `<img src="https://image.tmdb.org/t/p/w185${item.poster_path}" loading="lazy" onerror="this.parentElement.innerHTML='<div class=\\'lib-card-poster-placeholder\\'>◫</div>'">`
    : `<div class="lib-card-poster-placeholder">◫</div>`;

  let badges = '';
  if (item.source === 'radarr' || item.source === 'sonarr') badges += `<span class="badge badge-green" style="font-size:9px;padding:1px 5px">DL</span>`;
  if (item.source_tag) badges += `<span class="badge badge-accent" style="font-size:9px;padding:1px 5px">${item.source_tag}</span>`;
  const sourceBadge = badges ? `<div class="lib-card-source" style="display:flex;flex-direction:column;gap:3px;align-items:flex-end">${badges}</div>` : '';

  return `
    <div class="lib-card" data-tmdb-id="${item.tmdb_id}" onclick="showMediaDetail(${item.tmdb_id}, '${escapeAttr(item.media_type)}')">
      <div class="lib-card-poster">
        ${poster}
        ${sourceBadge}
      </div>
      <div class="lib-card-info">
        <div class="lib-card-title" title="${escapeAttr(item.title)}">${item.title}</div>
        <div class="lib-card-meta">${item.year || '—'} · ${item.media_type === 'movie' ? '🎬' : '📺'}</div>
      </div>
    </div>`;
}

let _addArrTmdbId = null;
let _addArrMediaType = 'movie';
let _epPickerSeasons = [];  // cached season list for current series
let _epPickerLoaded = {};   // season_number -> episodes array

async function showAddToRadarrModal(tmdbId, title, year, posterPath) {
  showAddToArrModal(tmdbId, title, year, posterPath, 'movie');
}

async function showAddToArrModal(tmdbId, title, year, posterPath, mediaType) {
  _resetManageMode(); // ensure modal is in add mode
  _addArrTmdbId = tmdbId;
  _addArrMediaType = mediaType || 'movie';
  const isSeries = _addArrMediaType === 'series';
  const arrName = isSeries ? 'Sonarr' : 'Radarr';

  document.getElementById('add-arr-modal-title').textContent = `Add to ${arrName}`;

  const modalBox = document.getElementById('add-arr-modal-box');
  modalBox.className = 'modal modal-arr';

  const info = document.getElementById('add-radarr-movie-info');
  const posterImg = posterPath
    ? `<img src="https://image.tmdb.org/t/p/w185${posterPath}" style="border-radius:8px;width:80px;height:120px;object-fit:cover">`
    : `<div style="width:80px;height:120px;background:var(--bg3);border-radius:8px;display:flex;align-items:center;justify-content:center;color:var(--text3)">&#9707;</div>`;
  info.innerHTML = `${posterImg}<div style="display:flex;flex-direction:column;justify-content:center"><div style="font-weight:600;font-size:17px">${title || 'Unknown'}</div><div style="color:var(--text2);font-size:14px">${year || ''}</div></div>`;

  const select = document.getElementById('add-radarr-quality');
  select.innerHTML = '<option value="">Loading...</option>';
  try {
    const endpoint = isSeries ? '/api/lists/sonarr-profiles' : '/api/lists/radarr-profiles';
    const profiles = await api(endpoint);
    select.innerHTML = profiles.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
  } catch (e) {
    select.innerHTML = '<option value="">Failed to load profiles</option>';
  }

  document.getElementById('sonarr-extra-fields').style.display = isSeries ? 'block' : 'none';
  const monitorWrap = document.getElementById('dl-more-monitor-wrap');
  if (monitorWrap) monitorWrap.style.display = 'none';

  // Reset episode picker state
  _epPickerSeasons = [];
  _epPickerLoaded = {};
  document.getElementById('episode-picker').style.display = 'none';
  document.getElementById('episode-picker-seasons').innerHTML = '';
  document.getElementById('add-sonarr-monitor').value = 'all';

  const btn = document.getElementById('add-radarr-confirm-btn');
  btn.disabled = false;
  btn.textContent = `Add to ${arrName}`;
  showModal('modal-add-to-radarr');
}

async function confirmAddToArr() {
  if (!_addArrTmdbId) return;
  const isSeries = _addArrMediaType === 'series';
  const arrName = isSeries ? 'Sonarr' : 'Radarr';
  const btn = document.getElementById('add-radarr-confirm-btn');
  btn.disabled = true;
  btn.textContent = 'Adding...';
  const qualityId = document.getElementById('add-radarr-quality').value;
  const body = { tmdb_ids: [_addArrTmdbId] };
  if (qualityId) body.quality_profile_id = parseInt(qualityId);
  if (isSeries) {
    const monitorVal = document.getElementById('add-sonarr-monitor').value;
    body.season_folder = document.getElementById('add-sonarr-season-folder').checked;
    if (monitorVal === 'custom') {
      const selected = _getSelectedEpisodes();
      if (selected.length === 0) {
        toast('Select at least one episode', 'error');
        btn.disabled = false;
        btn.textContent = `Add to ${arrName}`;
        return;
      }
      body.monitor = 'none';
      body.selected_episodes = selected;
    } else {
      body.monitor = monitorVal;
    }
  }
  const endpoint = isSeries ? '/api/lists/add-to-sonarr' : '/api/lists/add-to-radarr';
  try {
    const r = await api(endpoint, { method: 'POST', body });
    if (r.added > 0) {
      if (r.release_date && new Date(r.release_date) > new Date()) {
        const rd = new Date(r.release_date);
        const label = rd.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });
        toast(`Added to ${arrName} — releases ${label}, will download when available`, 'info', 6000);
      } else {
        toast(`Added to ${arrName}`);
      }
      closeModal('modal-add-to-radarr');
    } else if (r.already_exists > 0) {
      toast(`Already in ${arrName}`, 'info');
      closeModal('modal-add-to-radarr');
    } else {
      toast('Failed to add', 'error');
      btn.disabled = false;
      btn.textContent = `Add to ${arrName}`;
    }
  } catch (e) {
    toast(e.message, 'error');
    btn.disabled = false;
    btn.textContent = `Add to ${arrName}`;
  }
}

function confirmAddToRadarr() { confirmAddToArr(); }

// ── Episode Picker ──────────────────────────────────
async function onMonitorPresetChange(val) {
  const picker = document.getElementById('episode-picker');
  const modalBox = document.getElementById('add-arr-modal-box');
  if (val !== 'custom') {
    picker.style.display = 'none';
    modalBox.classList.remove('ep-expanded');
    return;
  }
  picker.style.display = 'block';
  modalBox.classList.add('ep-expanded');
  if (_epPickerSeasons.length > 0) return; // already loaded
  const loading = document.getElementById('episode-picker-loading');
  loading.style.display = 'block';
  try {
    const data = await api(`/api/discover/seasons/${_addArrTmdbId}`);
    _epPickerSeasons = (data.seasons || []).filter(s => s.season_number > 0);
    _renderSeasons();
  } catch (e) {
    document.getElementById('episode-picker-seasons').innerHTML = '<div style="padding:12px;color:var(--text3)">Failed to load seasons</div>';
  }
  loading.style.display = 'none';
}

function _renderSeasons() {
  const container = document.getElementById('episode-picker-seasons');
  container.innerHTML = _epPickerSeasons.map(s => {
    const airYear = s.air_date ? ` (${s.air_date.substring(0, 4)})` : '';
    return `<div class="ep-picker-season" data-season="${s.season_number}">
      <div class="ep-picker-season-header" onclick="toggleSeasonAccordion(${s.season_number})">
        <span class="ep-arrow" id="ep-arrow-${s.season_number}">▶</span>
        <input type="checkbox" class="ep-picker-season-check" onclick="event.stopPropagation();toggleSeasonAll(${s.season_number}, this.checked)">
        <span>${s.name || 'Season ' + s.season_number}${airYear}</span>
        <span class="ep-count">${s.episode_count} ep</span>
      </div>
      <div class="ep-picker-episodes" id="ep-list-${s.season_number}"></div>
    </div>`;
  }).join('');
}

async function toggleSeasonAccordion(seasonNum) {
  const arrow = document.getElementById(`ep-arrow-${seasonNum}`);
  const list = document.getElementById(`ep-list-${seasonNum}`);
  const isOpen = list.classList.contains('open');
  if (isOpen) {
    list.classList.remove('open');
    arrow.classList.remove('open');
    return;
  }
  list.classList.add('open');
  arrow.classList.add('open');
  if (!_epPickerLoaded[seasonNum]) {
    const tmdbId = _downloadMoreTmdbId || _addArrTmdbId;
    list.innerHTML = '<div style="padding:8px 28px;color:var(--text3);font-size:12px">Loading...</div>';
    try {
      const data = await api(`/api/discover/season/${tmdbId}/${seasonNum}`);
      _epPickerLoaded[seasonNum] = data.episodes || [];
      _renderEpisodes(seasonNum);
    } catch (e) {
      list.innerHTML = '<div style="padding:8px 28px;color:var(--text3);font-size:12px">Failed to load</div>';
    }
  }
}

function _renderEpisodes(seasonNum) {
  const eps = _epPickerLoaded[seasonNum] || [];
  const list = document.getElementById(`ep-list-${seasonNum}`);
  const vodEps = _vodEpisodes[String(seasonNum)] || _vodEpisodes[seasonNum] || [];
  const dlEps = _dlEpisodes[String(seasonNum)] || _dlEpisodes[seasonNum] || [];
  list.innerHTML = eps.map(ep => {
    const airDate = ep.air_date ? ep.air_date.substring(0, 10) : '';
    const epNum = `S${String(seasonNum).padStart(2, '0')}E${String(ep.episode_number).padStart(2, '0')}`;
    const title = ep.name || '';
    const isVod = vodEps.includes(ep.episode_number);
    const isDl = dlEps.includes(ep.episode_number);
    if (isVod || isDl) {
      const badgeText = isVod ? 'VOD' : 'DL';
      const badgeClass = isVod ? 'ep-badge-vod' : 'ep-badge-dl';
      return `<label class="ep-picker-ep ep-have">
        <input type="checkbox" checked disabled data-season="${seasonNum}" data-episode="${ep.episode_number}">
        <span class="ep-num">${epNum}</span>
        <span class="ep-title" title="${escapeAttr(title)}">${title}</span>
        <span class="ep-dl-badge ${badgeClass}">${badgeText}</span>
      </label>`;
    }
    return `<label class="ep-picker-ep">
      <input type="checkbox" data-season="${seasonNum}" data-episode="${ep.episode_number}" onchange="updateSeasonCheckbox(${seasonNum})">
      <span class="ep-num">${epNum}</span>
      <span class="ep-title" title="${escapeAttr(title)}">${title}</span>
      <span class="ep-date">${airDate}</span>
    </label>`;
  }).join('');
  _updateSeasonStatus(seasonNum, eps.length);
}

async function toggleSeasonAll(seasonNum, checked) {
  const list = document.getElementById(`ep-list-${seasonNum}`);
  if (checked && !list.classList.contains('open')) {
    await toggleSeasonAccordion(seasonNum);
  }
  list.querySelectorAll('input[type="checkbox"]:not(:disabled)').forEach(cb => cb.checked = checked);
}

function updateSeasonCheckbox(seasonNum) {
  const list = document.getElementById(`ep-list-${seasonNum}`);
  if (!list) return;
  const cbs = list.querySelectorAll('input[type="checkbox"]');
  const allChecked = cbs.length > 0 && [...cbs].every(cb => cb.checked);
  const seasonCb = document.querySelector(`.ep-picker-season[data-season="${seasonNum}"] .ep-picker-season-check`);
  if (seasonCb) seasonCb.checked = allChecked;
}

function _updateSeasonStatus(seasonNum, totalEps) {
  const vodEps = _vodEpisodes[String(seasonNum)] || _vodEpisodes[seasonNum] || [];
  const dlEps = _dlEpisodes[String(seasonNum)] || _dlEpisodes[seasonNum] || [];
  const haveCount = new Set([...vodEps, ...dlEps]).size;
  const seasonEl = document.querySelector(`.ep-picker-season[data-season="${seasonNum}"]`);
  if (!seasonEl) return;

  // Update season checkbox — disable if full season already owned
  const seasonCb = seasonEl.querySelector('.ep-picker-season-check');
  if (seasonCb) {
    const full = haveCount >= totalEps && totalEps > 0;
    seasonCb.checked = full;
    seasonCb.disabled = full;
  }

  // Update coverage indicator
  const countEl = seasonEl.querySelector('.ep-count');
  if (!countEl) return;
  if (haveCount === 0) {
    countEl.textContent = `${totalEps} ep`;
    countEl.className = 'ep-count';
  } else if (haveCount >= totalEps) {
    countEl.textContent = `${totalEps}/${totalEps}`;
    countEl.className = 'ep-count ep-count-full';
  } else {
    countEl.textContent = `${haveCount}/${totalEps}`;
    countEl.className = 'ep-count ep-count-partial';
  }
}

async function epPickerSelectAll() {
  // Expand all seasons so lazy-loaded episodes are rendered
  const seasons = document.querySelectorAll('#episode-picker-seasons .ep-picker-season');
  for (const s of seasons) {
    const sn = parseInt(s.dataset.season);
    const list = document.getElementById(`ep-list-${sn}`);
    if (list && !list.classList.contains('open')) {
      await toggleSeasonAccordion(sn);
    }
  }
  document.querySelectorAll('#episode-picker-seasons input[type="checkbox"]:not(:disabled)').forEach(cb => cb.checked = true);
}

async function epPickerSelectNone() {
  const seasons = document.querySelectorAll('#episode-picker-seasons .ep-picker-season');
  for (const s of seasons) {
    const sn = parseInt(s.dataset.season);
    const list = document.getElementById(`ep-list-${sn}`);
    if (list && !list.classList.contains('open')) {
      await toggleSeasonAccordion(sn);
    }
  }
  document.querySelectorAll('#episode-picker-seasons input[type="checkbox"]:not(:disabled)').forEach(cb => cb.checked = false);
}

function _getSelectedEpisodes() {
  const selected = [];
  document.querySelectorAll('#episode-picker-seasons .ep-picker-ep input[type="checkbox"]:checked:not(:disabled)').forEach(cb => {
    selected.push({ season: parseInt(cb.dataset.season), episode: parseInt(cb.dataset.episode) });
  });
  return selected;
}

// ── Manage Episodes (existing Sonarr series) ────────
let _manageTmdbId = null;

async function showManageEpisodesModal(tmdbId, title, year, posterPath) {
  _manageTmdbId = tmdbId;
  _epPickerSeasons = [];
  _epPickerLoaded = {};

  const modalBox = document.getElementById('add-arr-modal-box');
  modalBox.className = 'modal modal-arr ep-expanded';

  document.getElementById('add-arr-modal-title').textContent = 'Manage Episodes';

  const info = document.getElementById('add-radarr-movie-info');
  const posterImg = posterPath
    ? `<img src="https://image.tmdb.org/t/p/w185${posterPath}" style="border-radius:8px;width:80px;height:120px;object-fit:cover">`
    : `<div style="width:80px;height:120px;background:var(--bg3);border-radius:8px;display:flex;align-items:center;justify-content:center;color:var(--text3)">&#9707;</div>`;
  info.innerHTML = `${posterImg}<div style="display:flex;flex-direction:column;justify-content:center"><div style="font-weight:600;font-size:17px">${title || 'Unknown'}</div><div style="color:var(--text2);font-size:14px">${year || ''}</div></div>`;

  // Hide add-specific fields
  document.getElementById('add-radarr-quality').parentElement.style.display = 'none';
  document.getElementById('sonarr-extra-fields').style.display = 'none';

  // Show episode picker directly
  const picker = document.getElementById('episode-picker');
  picker.style.display = 'block';
  const container = document.getElementById('episode-picker-seasons');
  container.innerHTML = '';
  const loading = document.getElementById('episode-picker-loading');
  loading.style.display = 'block';

  const btn = document.getElementById('add-radarr-confirm-btn');
  btn.disabled = false;
  btn.textContent = 'Save Changes';
  btn.setAttribute('onclick', 'confirmManageEpisodes()');

  showModal('modal-add-to-radarr');

  // Load episodes from Sonarr
  try {
    const data = await api(`/api/discover/sonarr-episodes/${tmdbId}`);
    if (!data.in_sonarr) {
      container.innerHTML = '<div style="padding:12px;color:var(--text3)">Series not found in Sonarr</div>';
      loading.style.display = 'none';
      return;
    }
    // Group episodes by season
    const seasonMap = {};
    for (const ep of data.episodes) {
      const sn = ep.seasonNumber;
      if (sn === 0) continue; // skip specials
      if (!seasonMap[sn]) seasonMap[sn] = [];
      seasonMap[sn].push(ep);
    }
    const seasonNums = Object.keys(seasonMap).map(Number).sort((a, b) => a - b);

    container.innerHTML = seasonNums.map(sn => {
      const eps = seasonMap[sn];
      const allMonitored = eps.every(e => e.monitored);
      return `<div class="ep-picker-season" data-season="${sn}">
        <div class="ep-picker-season-header" onclick="toggleSeasonAccordion(${sn})">
          <span class="ep-arrow" id="ep-arrow-${sn}">▶</span>
          <input type="checkbox" class="ep-picker-season-check" onclick="event.stopPropagation();toggleSeasonAll(${sn}, this.checked)" ${allMonitored ? 'checked' : ''}>
          <span>Season ${sn}</span>
          <span class="ep-count">${eps.length} ep</span>
        </div>
        <div class="ep-picker-episodes" id="ep-list-${sn}"></div>
      </div>`;
    }).join('');

    // Pre-render episodes (we already have the data from Sonarr)
    for (const sn of seasonNums) {
      _epPickerLoaded[sn] = true; // mark as loaded so toggleSeasonAccordion doesn't re-fetch
      const list = document.getElementById(`ep-list-${sn}`);
      list.innerHTML = seasonMap[sn].map(ep => {
        const epNum = `S${String(sn).padStart(2, '0')}E${String(ep.episodeNumber).padStart(2, '0')}`;
        const dlBadge = ep.hasFile ? '<span class="ep-dl-badge">✓</span>' : '';
        return `<label class="ep-picker-ep">
          <input type="checkbox" ${ep.monitored ? 'checked' : ''} data-season="${sn}" data-episode="${ep.episodeNumber}" onchange="updateSeasonCheckbox(${sn})">
          <span class="ep-num">${epNum}</span>
          <span class="ep-title" title="${escapeAttr(ep.title || '')}">${ep.title || ''}</span>
          ${dlBadge}
        </label>`;
      }).join('');
    }
  } catch (e) {
    container.innerHTML = '<div style="padding:12px;color:var(--text3)">Failed to load episodes</div>';
  }
  loading.style.display = 'none';
}

async function confirmManageEpisodes() {
  if (!_manageTmdbId) return;
  const btn = document.getElementById('add-radarr-confirm-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  const selected = _getSelectedEpisodes();
  try {
    const r = await api('/api/discover/manage-episodes', {
      method: 'POST',
      body: { tmdb_id: _manageTmdbId, selected_episodes: selected },
    });
    if (r.success) {
      toast(`Updated — monitoring ${r.monitored} episode${r.monitored !== 1 ? 's' : ''}${r.searching ? `, searching ${r.searching} new` : ''}`);
      closeModal('modal-add-to-radarr');
    } else {
      toast('Failed to update', 'error');
      btn.disabled = false;
      btn.textContent = 'Save Changes';
    }
  } catch (e) {
    toast(e.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Save Changes';
  }
}

// ── Download More Episodes (VOD series → Sonarr) ────────
let _downloadMoreTmdbId = null;
let _vodEpisodes = {};  // {season: [ep1, ep2, ...]} from VOD scan
let _dlEpisodes = {};   // {season: [ep1, ep2, ...]} from Sonarr (hasFile=true)

async function showDownloadMoreModal(tmdbId, title, year, posterPath) {
  _downloadMoreTmdbId = tmdbId;
  _vodEpisodes = {};
  _dlEpisodes = {};
  _epPickerSeasons = [];
  _epPickerLoaded = {};

  const modalBox = document.getElementById('add-arr-modal-box');
  modalBox.className = 'modal modal-arr ep-expanded';

  document.getElementById('add-arr-modal-title').textContent = 'Download More Episodes';

  const info = document.getElementById('add-radarr-movie-info');
  const posterImg = posterPath
    ? `<img src="https://image.tmdb.org/t/p/w185${posterPath}" style="border-radius:8px;width:80px;height:120px;object-fit:cover">`
    : `<div style="width:80px;height:120px;background:var(--bg3);border-radius:8px;display:flex;align-items:center;justify-content:center;color:var(--text3)">&#9707;</div>`;
  info.innerHTML = `${posterImg}<div style="display:flex;flex-direction:column;justify-content:center"><div style="font-weight:600;font-size:17px">${title || 'Unknown'}</div><div style="color:var(--text2);font-size:14px">${year || ''}</div></div>`;

  // Show quality profile selector (needed for Sonarr add)
  document.getElementById('add-radarr-quality').parentElement.style.display = '';
  document.getElementById('sonarr-extra-fields').style.display = 'none';

  // Load Sonarr quality profiles
  const select = document.getElementById('add-radarr-quality');
  select.innerHTML = '<option value="">Loading...</option>';
  try {
    const profiles = await api('/api/lists/sonarr-profiles');
    select.innerHTML = profiles.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
  } catch (e) {
    select.innerHTML = '<option value="">Failed to load profiles</option>';
  }

  // Show monitor new episodes toggle
  let monitorToggle = document.getElementById('dl-more-monitor-new');
  if (!monitorToggle) {
    const wrap = document.createElement('div');
    wrap.id = 'dl-more-monitor-wrap';
    wrap.style.cssText = 'margin-bottom:16px;display:flex;align-items:center;gap:8px';
    wrap.innerHTML = '<input type="checkbox" id="dl-more-monitor-new" checked><label for="dl-more-monitor-new" style="font-size:13px;color:var(--text2);cursor:pointer">Auto-download new episodes</label>';
    document.getElementById('add-radarr-quality').parentElement.after(wrap);
    monitorToggle = document.getElementById('dl-more-monitor-new');
  }
  document.getElementById('dl-more-monitor-wrap').style.display = 'flex';
  monitorToggle.checked = true;

  // Show episode picker
  const picker = document.getElementById('episode-picker');
  picker.style.display = 'block';
  const container = document.getElementById('episode-picker-seasons');
  container.innerHTML = '';
  const loading = document.getElementById('episode-picker-loading');
  loading.style.display = 'block';

  const btn = document.getElementById('add-radarr-confirm-btn');
  btn.disabled = false;
  btn.textContent = 'Add to Sonarr';
  btn.setAttribute('onclick', 'confirmDownloadMore()');

  showModal('modal-add-to-radarr');

  // Fetch TMDB seasons + VOD episodes + Sonarr episodes in parallel
  try {
    const [seasonsData, vodData, sonarrData] = await Promise.all([
      api(`/api/discover/seasons/${tmdbId}`),
      api(`/api/discover/vod-episodes/${tmdbId}`),
      api(`/api/discover/sonarr-episodes/${tmdbId}`).catch(() => ({ in_sonarr: false })),
    ]);

    if (vodData.has_episodes) {
      _vodEpisodes = vodData.episodes;
    }

    // Build downloaded episodes map from Sonarr (skip VOD — Sonarr sees .strm as hasFile)
    if (sonarrData.in_sonarr && sonarrData.episodes) {
      for (const ep of sonarrData.episodes) {
        if (ep.hasFile && ep.seasonNumber > 0) {
          const vodList = _vodEpisodes[String(ep.seasonNumber)] || _vodEpisodes[ep.seasonNumber] || [];
          if (vodList.includes(ep.episodeNumber)) continue;
          if (!_dlEpisodes[ep.seasonNumber]) _dlEpisodes[ep.seasonNumber] = [];
          _dlEpisodes[ep.seasonNumber].push(ep.episodeNumber);
        }
      }
    }

    _epPickerSeasons = (seasonsData.seasons || []).filter(s => s.season_number > 0);
    _renderSeasonsWithVod();
  } catch (e) {
    container.innerHTML = '<div style="padding:12px;color:var(--text3)">Failed to load seasons</div>';
  }
  loading.style.display = 'none';
}

function _renderSeasonsWithVod() {
  const container = document.getElementById('episode-picker-seasons');
  container.innerHTML = _epPickerSeasons.map(s => {
    const sn = s.season_number;
    const airYear = s.air_date ? ` (${s.air_date.substring(0, 4)})` : '';
    const total = s.episode_count || 0;
    const vodEps = _vodEpisodes[String(sn)] || _vodEpisodes[sn] || [];
    const dlEps = _dlEpisodes[String(sn)] || _dlEpisodes[sn] || [];
    const haveCount = new Set([...vodEps, ...dlEps]).size;
    let countText, countClass, checked;
    if (haveCount === 0) {
      countText = `${total} ep`;
      countClass = 'ep-count';
      checked = false;
    } else if (haveCount >= total) {
      countText = `${total}/${total}`;
      countClass = 'ep-count ep-count-full';
      checked = true;
    } else {
      countText = `${haveCount}/${total}`;
      countClass = 'ep-count ep-count-partial';
      checked = false;
    }
    return `<div class="ep-picker-season" data-season="${sn}">
      <div class="ep-picker-season-header" onclick="toggleSeasonAccordion(${sn})">
        <span class="ep-arrow" id="ep-arrow-${sn}">▶</span>
        <input type="checkbox" class="ep-picker-season-check" ${checked ? 'checked' : ''} ${haveCount >= total && total > 0 ? 'disabled' : ''} onclick="event.stopPropagation();toggleSeasonAll(${sn}, this.checked)">
        <span>Season ${sn}${airYear}</span>
        <span class="${countClass}">${countText}</span>
      </div>
      <div class="ep-picker-episodes" id="ep-list-${sn}"></div>
    </div>`;
  }).join('');
}

async function confirmDownloadMore() {
  if (!_downloadMoreTmdbId) return;
  const selected = _getSelectedEpisodes();
  if (selected.length === 0) {
    toast('Select at least one episode', 'error');
    return;
  }

  const btn = document.getElementById('add-radarr-confirm-btn');
  btn.disabled = true;
  btn.textContent = 'Adding...';

  const qualityId = document.getElementById('add-radarr-quality').value;
  try {
    const r = await api('/api/lists/add-to-sonarr', {
      method: 'POST',
      body: {
        tmdb_ids: [_downloadMoreTmdbId],
        quality_profile_id: qualityId ? parseInt(qualityId) : undefined,
        selected_episodes: selected,
        monitor_new: document.getElementById('dl-more-monitor-new')?.checked || false,
      },
    });
    if (r.added > 0) {
      const monitorMsg = document.getElementById('dl-more-monitor-new')?.checked ? ' + monitoring new episodes' : '';
      toast(`Added to Sonarr — downloading ${selected.length} episode${selected.length !== 1 ? 's' : ''}${monitorMsg}`);
      closeModal('modal-add-to-radarr');
    } else if (r.already_exists > 0) {
      toast('Already in Sonarr', 'error');
      btn.disabled = false;
      btn.textContent = 'Add to Sonarr';
    } else {
      toast(r.detail || 'Failed to add', 'error');
      btn.disabled = false;
      btn.textContent = 'Add to Sonarr';
    }
  } catch (e) {
    toast(e.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Add to Sonarr';
  }
}

async function toggleFollow(tmdbId, follow) {
  try {
    await api(`/api/library/follow/${tmdbId}`, {
      method: 'POST',
      body: { follow },
    });
    toast(follow ? 'Following — new episodes will download automatically' : 'Unfollowed — no longer tracking new episodes');
  } catch (e) {
    toast(e.message || 'Failed to update', 'error');
    // Revert checkbox
    const cb = document.querySelector('.detail-follow-toggle input[type="checkbox"]');
    if (cb) cb.checked = !follow;
  }
}

function _resetManageMode() {
  // Restore add modal to normal state when closing
  document.getElementById('add-radarr-quality').parentElement.style.display = '';
  document.getElementById('add-radarr-confirm-btn').setAttribute('onclick', 'confirmAddToArr()');
  _manageTmdbId = null;
  _downloadMoreTmdbId = null;
  _vodEpisodes = {};
}

function loadMoreLibrary() {
  pages.lib.offset += pages.lib.limit;
  fetchLibraryPage();
}

function setLibType(type, btn) {
  // Exit list mode if active
  if (pages.lib.listId) {
    pages.lib.listId = null;
    pages.lib.listStatus = null;
    document.querySelectorAll('#lib-list-pills .lib-list-pill').forEach(b => b.classList.remove('active'));
    _exitListMode();
  }
  pages.lib.type = type;
  pages.lib.sourceTag = null;
  document.querySelectorAll('[data-libtype]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadLibrary();
}

function setLibSrc(src, btn) {
  // Exit list mode if active
  if (pages.lib.listId) {
    pages.lib.listId = null;
    pages.lib.listStatus = null;
    document.querySelectorAll('#lib-list-pills .lib-list-pill').forEach(b => b.classList.remove('active'));
    _exitListMode();
  }
  pages.lib.src = src;
  pages.lib.sourceTag = null;
  document.querySelectorAll('[data-libsrc]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadLibrary();
}

let libSearchTimer;
function searchLibrary() {
  clearTimeout(libSearchTimer);
  pages.lib.sourceTag = null;
  libSearchTimer = setTimeout(loadLibrary, 350);
}

function renderSourcePills(breakdown) {
  const el = document.getElementById('lib-source-pills');
  if (!Object.keys(breakdown).length) { el.innerHTML = ''; return; }
  const active = pages.lib.sourceTag;
  const allBtn = `<button class="badge ${!active ? 'badge-accent' : 'badge-gray'}" style="cursor:pointer;font-size:12px;padding:4px 10px"
    onclick="filterByTag(null)">All</button>`;
  const pills = Object.entries(breakdown)
    .sort((a,b) => b[1]-a[1])
    .map(([tag, count]) => `
      <button class="badge ${active === tag ? 'badge-accent' : 'badge-gray'}" style="cursor:pointer;font-size:12px;padding:4px 10px"
        onclick="filterByTag('${escapeAttr(tag)}')">
        ${tag} <span style="opacity:0.6;margin-left:4px">${count}</span>
      </button>`).join('');
  el.innerHTML = allBtn + pills;
}

function filterByTag(tag) {
  const search = document.getElementById('lib-search');
  if (search) search.value = '';
  if (pages.lib.listId) {
    pages.lib.listId = null;
    pages.lib.listStatus = null;
    document.querySelectorAll('#lib-list-pills .lib-list-pill').forEach(b => b.classList.remove('active'));
    _exitListMode();
  }
  pages.lib.sourceTag = tag || null;
  loadLibrary();
}

async function showMediaDetail(tmdbId, mediaType) {
  showModal('modal-media-detail');
  document.getElementById('detail-title').textContent = 'Loading...';
  document.getElementById('detail-body').innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';

  try {
    const data = await api(`/api/library/item/${mediaType}/${tmdbId}`);
    document.getElementById('detail-title').textContent = data.title;
    const isSeries = mediaType === 'series';
    document.getElementById('detail-body').innerHTML = `
      <div style="display:flex;gap:20px">
        ${data.poster_path ? `<img src="https://image.tmdb.org/t/p/w185${data.poster_path}" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
        <div style="flex:1">
          <div style="font-size:13px;color:var(--text2);margin-bottom:12px">${data.year || '—'} · ${data.runtime ? data.runtime+'m' : ''} · ★ ${data.rating || '—'}</div>
          <p style="font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:16px">${data.overview || 'No overview available.'}</p>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
            ${(data.genres||[]).map(g => `<span class="badge badge-gray">${g}</span>`).join('')}
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            ${(data.tags||[]).map(t => `<span class="badge badge-accent">${t}</span>`).join('')}
          </div>
          <div style="margin-top:16px;font-size:11px;color:var(--text3);font-family:'DM Mono',monospace">
            Source: ${data.source} · Added: ${data.date_added ? new Date(data.date_added).toLocaleDateString() : '—'}
            ${data.strm_path ? `<br>Path: ${data.strm_path}` : ''}
          </div>
        </div>
      </div>
      ${isSeries ? '<div id="detail-episodes" style="margin-top:20px"><div class="loading-state" style="padding:16px 0"><div class="spinner"></div></div></div>' : ''}`;

    // For series, load episode breakdown
    if (isSeries) _loadSeriesEpisodes(tmdbId, data);
  } catch (e) {
    document.getElementById('detail-body').innerHTML = '<div class="empty-state"><p>Failed to load details</p></div>';
  }
}

let _detailEpState = {}; // { vodEps, dlEps, tmdbId, loaded: {sn: true} }

async function _loadSeriesEpisodes(tmdbId, seriesData) {
  const container = document.getElementById('detail-episodes');
  if (!container) return;

  try {
    const [seasonsData, vodData, sonarrData] = await Promise.all([
      api(`/api/discover/seasons/${tmdbId}`),
      api(`/api/discover/vod-episodes/${tmdbId}`),
      api(`/api/discover/sonarr-episodes/${tmdbId}`).catch(() => ({ in_sonarr: false })),
    ]);

    const seasons = (seasonsData.seasons || []).filter(s => s.season_number > 0);
    const vodEps = vodData.episodes || {};
    const dlEps = {};
    const sonarrEpMap = {};
    if (sonarrData.in_sonarr && sonarrData.episodes) {
      for (const ep of sonarrData.episodes) {
        if (ep.seasonNumber > 0) {
          sonarrEpMap[`${ep.seasonNumber}-${ep.episodeNumber}`] = ep;
          if (ep.hasFile) {
            // Skip if this episode is also VOD — Sonarr sees .strm as hasFile
            const vodList = vodEps[String(ep.seasonNumber)] || vodEps[ep.seasonNumber] || [];
            if (vodList.includes(ep.episodeNumber)) continue;
            if (!dlEps[ep.seasonNumber]) dlEps[ep.seasonNumber] = [];
            dlEps[ep.seasonNumber].push(ep.episodeNumber);
          }
        }
      }
    }

    _detailEpState = { vodEps, dlEps, sonarrEpMap, tmdbId, loaded: {} };

    // Compute totals
    let totalOwned = 0, totalAvailable = 0;
    for (const s of seasons) {
      const sn = s.season_number;
      const total = s.episode_count || 0;
      const vod = vodEps[String(sn)] || vodEps[sn] || [];
      const dl = dlEps[String(sn)] || dlEps[sn] || [];
      const have = new Set([...vod, ...dl]).size;
      totalOwned += have;
      totalAvailable += total;
    }

    const isComplete = totalOwned >= totalAvailable && totalAvailable > 0;
    const countClass = isComplete ? 'ep-count-full' : (totalOwned > 0 ? 'ep-count-partial' : '');
    const downloadMoreBtn = !isComplete ? `<button class="btn btn-primary btn-sm" onclick="closeModal('modal-media-detail');showDownloadMoreModal(${tmdbId},'${escapeJS(seriesData.title||'')}','${escapeJS(seriesData.year||'')}','${escapeJS(seriesData.poster_path||'')}')">Download More</button>` : '';

    // Following toggle — show if series is in Sonarr (has sonarr_path or sonarr source)
    const inSonarr = sonarrData.in_sonarr;
    const isFollowing = seriesData.following || false;
    const followToggle = inSonarr ? `<label class="detail-follow-toggle" title="Auto-download new episodes">
      <input type="checkbox" ${isFollowing ? 'checked' : ''} onchange="toggleFollow(${tmdbId}, this.checked)">
      <span class="detail-follow-label">Following</span>
    </label>` : '';

    let html = `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <div style="font-size:14px;font-weight:600;color:var(--text)">Episodes <span class="${countClass}" style="font-weight:400;font-size:13px">${totalOwned}/${totalAvailable}</span></div>
      <div style="display:flex;align-items:center;gap:8px">${followToggle}${downloadMoreBtn}</div>
    </div>`;

    html += '<div class="detail-ep-seasons">';
    for (const s of seasons) {
      const sn = s.season_number;
      const total = s.episode_count || 0;
      const vod = vodEps[String(sn)] || vodEps[sn] || [];
      const dl = dlEps[String(sn)] || dlEps[sn] || [];
      const haveSet = new Set([...vod, ...dl]);
      const haveCount = haveSet.size;
      if (haveCount === 0) continue; // Only show seasons we have episodes for

      const seasonFull = haveCount >= total && total > 0;
      const cClass = seasonFull ? 'ep-count-full' : 'ep-count-partial';
      const airYear = s.air_date ? ` (${s.air_date.substring(0, 4)})` : '';

      html += `<div class="detail-ep-season" data-season="${sn}">
        <div class="detail-ep-season-hdr" onclick="detailToggleSeason(${sn})">
          <span class="ep-arrow">▶</span>
          <span>Season ${sn}${airYear}</span>
          <span class="ep-count ${cClass}">${haveCount}/${total}</span>
        </div>
        <div class="detail-ep-list" id="detail-ep-list-${sn}"></div>
      </div>`;
    }
    html += '</div>';

    if (totalOwned === 0) {
      html = `<div style="font-size:13px;color:var(--text3);padding:12px 0">No episodes in library yet. ${downloadMoreBtn}</div>`;
    }

    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = '<div style="font-size:13px;color:var(--text3);padding:8px 0">Could not load episode data</div>';
  }
}

async function detailToggleSeason(sn) {
  const seasonEl = document.querySelector(`.detail-ep-season[data-season="${sn}"]`);
  if (!seasonEl) return;
  const isOpen = seasonEl.classList.contains('open');
  if (isOpen) { seasonEl.classList.remove('open'); return; }
  seasonEl.classList.add('open');

  const list = document.getElementById(`detail-ep-list-${sn}`);
  if (_detailEpState.loaded[sn]) return; // already loaded

  list.innerHTML = '<div style="padding:8px 12px;color:var(--text3);font-size:12px">Loading...</div>';
  try {
    const data = await api(`/api/discover/season/${_detailEpState.tmdbId}/${sn}`);
    _detailEpState.loaded[sn] = true;
    const tmdbEps = data.episodes || [];
    const vodEps = _detailEpState.vodEps;
    const dlEps = _detailEpState.dlEps;
    const vod = vodEps[String(sn)] || vodEps[sn] || [];
    const dl = dlEps[String(sn)] || dlEps[sn] || [];
    const haveSet = new Set([...vod, ...dl]);
    const ownedNums = [...haveSet].sort((a, b) => a - b);

    // Build name lookup from TMDB
    const nameMap = {};
    for (const ep of tmdbEps) nameMap[ep.episode_number] = ep.name || '';
    // Fallback to Sonarr names
    for (const epNum of ownedNums) {
      if (!nameMap[epNum]) {
        const key = `${sn}-${epNum}`;
        if (_detailEpState.sonarrEpMap[key]) nameMap[epNum] = _detailEpState.sonarrEpMap[key].title || '';
      }
    }

    let rows = '';
    for (const epNum of ownedNums) {
      const isVod = vod.includes(epNum);
      const isDl = dl.includes(epNum);
      const badges = [];
      if (isVod) badges.push('<span class="ep-dl-badge ep-badge-vod">VOD</span>');
      if (isDl) badges.push('<span class="ep-dl-badge ep-badge-dl">DL</span>');

      rows += `<div class="detail-ep-row">
        <span class="ep-num">${epNum}</span>
        <span class="detail-ep-name">${nameMap[epNum] || ''}</span>
        ${badges.join('')}
      </div>`;
    }
    list.innerHTML = rows;
  } catch {
    list.innerHTML = '<div style="padding:8px 12px;color:var(--text3);font-size:12px">Failed to load</div>';
  }
}

async function showCoverageDetail(tmdbId, mediaType, title, year, posterPath) {
  showModal('modal-media-detail');
  document.getElementById('detail-title').textContent = 'Loading...';
  document.getElementById('detail-body').innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';

  try {
    const data = await api(`/api/library/item/${mediaType}/${tmdbId}`);
    document.getElementById('detail-title').textContent = data.title;
    document.getElementById('detail-body').innerHTML = `
      <div style="display:flex;gap:20px">
        ${data.poster_path ? `<img src="https://image.tmdb.org/t/p/w185${data.poster_path}" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
        <div style="flex:1">
          <div style="font-size:13px;color:var(--text2);margin-bottom:12px">${data.year || '—'} · ${data.runtime ? data.runtime+'m' : ''} · ★ ${data.rating || '—'}</div>
          <p style="font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:16px">${data.overview || 'No overview available.'}</p>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
            ${(data.genres||[]).map(g => `<span class="badge badge-gray">${g}</span>`).join('')}
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            ${(data.tags||[]).map(t => `<span class="badge badge-accent">${t}</span>`).join('')}
          </div>
          <div style="margin-top:16px;font-size:11px;color:var(--text3);font-family:'DM Mono',monospace">
            Source: ${data.source} · Added: ${data.date_added ? new Date(data.date_added).toLocaleDateString() : '—'}
            ${data.strm_path ? `<br>Path: ${data.strm_path}` : ''}
          </div>
        </div>
      </div>`;
  } catch {
    // Item not in library — fetch from TMDB for overview
    try {
      const data = await api(`/api/library/tmdb/${mediaType}/${tmdbId}`);
      const isSeries = mediaType === 'series';
      const arrLabel = isSeries ? 'Sonarr' : 'Radarr';
      document.getElementById('detail-title').textContent = data.title || title || 'Unknown';
      document.getElementById('detail-body').innerHTML = `
        <div style="display:flex;gap:20px">
          ${data.poster_path ? `<img src="https://image.tmdb.org/t/p/w185${data.poster_path}" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
          <div style="flex:1">
            <div style="font-size:13px;color:var(--text2);margin-bottom:12px">${data.year || '—'} · ${data.runtime ? data.runtime+'m · ' : ''}★ ${data.rating || '—'}</div>
            <p style="font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:16px">${data.overview || 'No overview available.'}</p>
            <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
              ${(data.genres||[]).map(g => `<span class="badge badge-gray">${g}</span>`).join('')}
            </div>
            <div style="margin-top:8px;padding-top:12px;border-top:1px solid var(--border)">
              <span class="badge" style="background:var(--red-dim);color:var(--red);margin-bottom:8px">Not in library</span>
              <div style="margin-top:8px">
                <button class="btn btn-primary btn-sm" onclick="closeModal('modal-media-detail');showAddToArrModal(${tmdbId},'${escapeJS(data.title||'')}','${escapeJS(data.year||'')}','${escapeJS(data.poster_path||'')}','${mediaType}')">Add to ${arrLabel}</button>
              </div>
            </div>
          </div>
        </div>`;
    } catch {
      document.getElementById('detail-title').textContent = title || 'Unknown';
      document.getElementById('detail-body').innerHTML = `
        <div style="display:flex;gap:20px">
          ${posterPath ? `<img src="https://image.tmdb.org/t/p/w185${posterPath}" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
          <div style="flex:1">
            <div style="font-size:13px;color:var(--text2);margin-bottom:12px">${year || '—'}</div>
            <p style="font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:16px">Not in library yet.</p>
          </div>
        </div>`;
    }
  }
}

// ── LISTS PAGE ───────────────────────────────────────────────────────────
async function loadLists() {
  try {
    await loadListCards();
  } catch (e) {
    const el = document.getElementById('lists-active');
    if (el) el.innerHTML = '<div class="empty-state" style="padding:24px"><p>Failed to load lists</p></div>';
  }
}

async function loadListCards() {
  const el = document.getElementById('lists-active');
  if (!el) return;

  try {
    const lists = await api('/api/lists');
    if (!lists.length) {
      el.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text3)">No lists added yet. Add your first list below.</div>';
      return;
    }

    const typeLabels = { trakt: 'Trakt List', letterboxd: 'Letterboxd List', imdb_rss: 'IMDb List' };
    el.innerHTML = lists.map(list => {
      const icon = list.type === 'trakt' ? '🎬' : list.type === 'letterboxd' ? '📋' : '⭐';
      const typeLabel = typeLabels[list.type] || list.type;
      const lastFetched = list.last_fetched ? timeAgo(new Date(list.last_fetched)) : 'never';
      return `
      <div class="card list-card" style="margin-bottom:12px" id="list-card-${list.id}">
        <div style="padding:16px">
          <div style="display:flex;align-items:flex-start;gap:12px">
            <div style="width:36px;height:36px;background:var(--bg3);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0">${icon}</div>
            <div style="flex:1;min-width:0">
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                <span style="font-size:15px;font-weight:600;color:var(--text)">${list.name}</span>
                ${list.auto_add_radarr ? '<span class="badge badge-green">Auto-grab</span>' : ''}
                <div style="margin-left:auto;display:flex;gap:6px">
                  <button class="btn btn-secondary btn-sm" onclick="fetchList(${list.id})">Fetch</button>
                  <button class="btn btn-danger btn-sm" onclick="deleteList(${list.id})">Delete</button>
                </div>
              </div>
              <div style="font-size:12px;color:var(--text3);margin-top:4px">
                ${typeLabel} · Last fetched: ${lastFetched}${list.last_item_count ? ` · ${list.last_item_count} items` : ''}
              </div>
              <div style="font-size:12px;color:var(--text3);margin-top:2px">Tag: ${list.tag}</div>
            </div>
          </div>
          ${list.last_item_count ? `
          <div id="list-coverage-bar-${list.id}" style="margin-top:12px">
            <button class="btn btn-secondary btn-sm" onclick="loadListCoverageInline(${list.id})">View Coverage</button>
          </div>` : ''}
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div class="empty-state" style="padding:24px"><p>Failed to load lists</p></div>';
  }
}

function timeAgo(date) {
  const s = Math.floor((Date.now() - date.getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

async function loadListCoverageInline(listId) {
  const el = document.getElementById(`list-coverage-bar-${listId}`);
  if (!el) return;
  el.innerHTML = '<div style="display:flex;align-items:center;gap:8px"><div class="spinner" style="width:16px;height:16px"></div><span style="font-size:12px;color:var(--text3)">Loading coverage...</span></div>';

  try {
    const data = await api(`/api/lists/${listId}/coverage`);
    const total = data.total || 0;
    const vodPct = total ? Math.round(data.vod_count / total * 100) : 0;
    const radarrPct = total ? Math.round(data.radarr_count / total * 100) : 0;
    const sonarrPct = total ? Math.round((data.sonarr_count || 0) / total * 100) : 0;

    // Build downloaded stats line
    let dlStats = '';
    if (data.radarr_count) dlStats += `<span style="color:var(--blue)">⬇️ ${data.radarr_count} in Radarr</span>`;
    if (data.sonarr_count) dlStats += `<span style="color:var(--blue)">⬇️ ${data.sonarr_count} in Sonarr</span>`;
    if (!dlStats) dlStats = `<span style="color:var(--blue)">⬇️ 0 downloaded</span>`;

    // Build add-all buttons based on missing media types
    let addBtns = '';
    if (data.missing_movies > 0) {
      addBtns += `<button class="btn btn-primary btn-sm" onclick="addAllMissingFromCard(${listId}, 'radarr')">Add ${data.missing_movies} Missing to Radarr</button>`;
    }
    if (data.missing_series > 0) {
      addBtns += `<button class="btn btn-primary btn-sm" onclick="addAllMissingFromCard(${listId}, 'sonarr')">Add ${data.missing_series} Missing to Sonarr</button>`;
    }

    el.innerHTML = `
      <div style="margin-bottom:8px">
        <div style="display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--bg3)">
          <div style="width:${vodPct}%;background:var(--green);transition:width 0.3s"></div>
          <div style="width:${radarrPct}%;background:var(--blue);transition:width 0.3s"></div>
          <div style="width:${sonarrPct}%;background:var(--blue);transition:width 0.3s"></div>
        </div>
        <div style="display:flex;gap:14px;margin-top:6px;font-size:12px">
          <span style="color:var(--green)">✅ ${data.vod_count} in VOD</span>
          ${dlStats}
          <span style="color:var(--red)">❌ ${data.missing_count} missing</span>
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn btn-secondary btn-sm" onclick="showListCoverage(${listId}, '${escapeJS(data.name)}')">View Coverage</button>
        ${addBtns}
      </div>`;
  } catch (e) {
    el.innerHTML = `<div style="font-size:12px;color:var(--red)">${e.message}</div>`;
  }
}

async function addAllMissingFromCard(listId, target = 'radarr') {
  const label = target === 'sonarr' ? 'Sonarr' : 'Radarr';
  try {
    const r = await api(`/api/lists/${listId}/add-missing-to-${target}`, { method: 'POST', body: {} });
    toast(`Added ${r.added} to ${label}${r.already_exists ? `, ${r.already_exists} already existed` : ''}${r.failed ? `, ${r.failed} failed` : ''}`);
    loadListCoverageInline(listId);
  } catch (e) {
    toast(e.message, 'error');
  }
}

function onQuickListNameInput() {
  const name = document.getElementById('quick-list-name').value;
  const tagEl = document.getElementById('quick-list-tag');
  if (!tagEl.dataset.manualEdit) tagEl.value = name;
}

function onQuickListUrlInput() {
  const url = document.getElementById('quick-list-url').value;
  const urlLower = url.toLowerCase();
  const typeEl = document.getElementById('quick-list-type');
  const nameEl = document.getElementById('quick-list-name');
  const tagEl = document.getElementById('quick-list-tag');
  const hintEl = document.getElementById('quick-list-hint');

  // Auto-detect type
  let detectedName = '';
  if (urlLower.includes('imdb.com/list/') || urlLower.includes('imdb.com/chart/') || urlLower.includes('imdb.com/user/')) {
    typeEl.value = 'imdb_rss';
  } else if (urlLower.includes('trakt.tv')) {
    typeEl.value = 'trakt';
  } else if (urlLower.includes('letterboxd.com')) {
    typeEl.value = 'letterboxd';
  }

  // Auto-fill name from URL pattern
  if (urlLower.includes('imdb.com/chart/top')) {
    detectedName = 'IMDB TOP 250';
    hintEl.textContent = 'IMDb Top 250 chart';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.includes('imdb.com/chart/moviemeter')) {
    detectedName = 'IMDb Most Popular';
    hintEl.textContent = 'IMDb Most Popular Movies';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.includes('imdb.com/chart/bottom')) {
    detectedName = 'IMDb Bottom 100';
    hintEl.textContent = 'IMDb Bottom 100 chart';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.match(/imdb\.com\/user\/ur\d+/)) {
    detectedName = 'My IMDB Watchlist';
    hintEl.textContent = 'IMDb user watchlist detected';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.match(/imdb\.com\/list\/ls\d+/)) {
    detectedName = 'IMDB List';
    hintEl.textContent = 'IMDb custom list detected';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.includes('trakt.tv')) {
    const m = url.match(/\/lists\/([^/?]+)/);
    detectedName = m ? m[1].replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) : 'Trakt List';
    hintEl.textContent = 'Trakt list detected';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.includes('letterboxd.com')) {
    const m = url.match(/\/list\/([^/?]+)/);
    detectedName = m ? m[1].replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) : 'Letterboxd List';
    hintEl.textContent = 'Letterboxd list detected';
    hintEl.style.color = 'var(--accent)';
  } else if (url.trim()) {
    hintEl.textContent = 'Paste an IMDb, Trakt, or Letterboxd URL';
    hintEl.style.color = 'var(--text3)';
  } else {
    hintEl.textContent = 'Supports IMDb lists, charts, watchlists, Trakt lists, and Letterboxd lists';
    hintEl.style.color = 'var(--text3)';
  }

  // Auto-fill name and tag if user hasn't manually edited them
  if (detectedName && !nameEl.dataset.manualEdit) {
    nameEl.value = detectedName;
    if (!tagEl.dataset.manualEdit) tagEl.value = detectedName;
  }
}

async function saveQuickList() {
  const body = {
    name: document.getElementById('quick-list-name').value.trim(),
    type: document.getElementById('quick-list-type').value,
    url: document.getElementById('quick-list-url').value.trim(),
    tag: document.getElementById('quick-list-tag').value.trim(),
    auto_add_radarr: document.getElementById('quick-list-radarr').checked,
  };

  if (!body.name || !body.url || !body.tag) {
    toast('Name, URL and tag are required', 'error');
    return;
  }

  try {
    await api('/api/lists', { method: 'POST', body });
    toast('List added');
    document.getElementById('quick-list-name').value = '';
    document.getElementById('quick-list-url').value = '';
    document.getElementById('quick-list-tag').value = '';
    document.getElementById('quick-list-radarr').checked = false;
    loadListCards();
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── PLAYLISTS PAGE ────────────────────────────────────────────────────────
let _smartlistSortCache = {};

async function loadJellyfinPage() {
  // Fetch sort info for all playlists
  try {
    const data = await api('/api/smartlists');
    _smartlistSortCache = {};
    for (const sl of (data.smartlists || [])) {
      _smartlistSortCache[sl.name] = { sort_by: sl.sort_by, sort_order: sl.sort_order };
    }
  } catch {}

  // Load the currently active tab's content
  const activeTab = document.querySelector('[data-jftab].active');
  const tab = activeTab ? activeTab.getAttribute('data-jftab') : 'home';
  if (tab === 'home') loadHomeScreen();
  if (tab === 'playlists') {
    const banner = document.getElementById('auto-playlist-banner');
    if (banner && !localStorage.getItem('tentacle_dismiss_auto_banner')) banner.style.display = '';
    loadAutoPlaylists();
    loadTagRules();
  }
}

async function loadDiscoverPage() {
  // Load the active discover tab's content
  const activeTab = document.querySelector('[data-discovertab].active');
  const tab = activeTab ? activeTab.getAttribute('data-discovertab') : 'browse';
  if (tab === 'browse') loadDiscover();
  if (tab === 'lists') loadLists();
  if (tab === 'activity') { startActivityPolling(); renderActivity(); }
  else { stopActivityPolling(); loadActivity(); } // Fetch badge count even when not on activity tab
}

async function saveDiscoverInJellyfin() {
  const cb = document.getElementById('discover_in_jellyfin');
  if (!cb) return;
  try {
    await api('/api/settings', { method: 'POST', body: { settings: { discover_in_jellyfin: cb.checked ? 'true' : 'false' } } });
    toast(cb.checked ? 'Discover tab enabled in Jellyfin' : 'Discover tab disabled in Jellyfin');
  } catch (e) {
    toast(e.message, 'error');
  }
}

function dismissAutoPlaylistBanner() {
  localStorage.setItem('tentacle_dismiss_auto_banner', '1');
  const el = document.getElementById('auto-playlist-banner');
  if (el) el.style.display = 'none';
}

// toggleHomeScreenSection removed — Home Screen is now a full tab

// ── Auto Playlists ──────────────────────────────────────────────────────

const _autoCategoryLabels = { source: 'Sources', list: 'Lists', builtin: 'Built-in' };
const _autoCategoryOrder = ['source', 'list', 'builtin'];

async function loadAutoPlaylists() {
  const el = document.getElementById('auto-playlists-list');
  try {
    const data = await api('/api/smartlists/auto-playlists');
    const playlists = data.auto_playlists || [];

    if (!playlists.length) {
      el.innerHTML = `<div style="padding:24px;text-align:center;color:var(--text3);font-size:13px">
        No auto playlists yet. Sync a provider, import a list, or scan Radarr to see playlists here.
      </div>`;
      return;
    }

    // Group by category
    const groups = {};
    for (const p of playlists) {
      const cat = p.category || 'other';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(p);
    }

    let html = '';
    for (const cat of _autoCategoryOrder) {
      const items = groups[cat];
      if (!items || !items.length) continue;
      html += `<div style="padding:8px 16px 4px;font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">${_autoCategoryLabels[cat] || cat}</div>`;
      for (const p of items) {
        const checked = p.enabled ? 'checked' : '';
        const toggleBg = p.enabled ? 'var(--accent)' : 'var(--bg3)';
        const togglePos = p.enabled ? '18px' : '2px';
        const countBadge = p.item_count ? `<span style="font-size:11px;color:var(--text3);font-family:'DM Mono',monospace">${p.item_count}</span>` : '';
        const sortDrop = p.enabled ? _sortDropdown(p.name) : '';
        html += `
          <div style="display:flex;align-items:center;gap:12px;padding:8px 16px;border-bottom:1px solid var(--border)">
            <label style="position:relative;display:inline-block;width:36px;height:20px;flex-shrink:0;cursor:pointer">
              <input type="checkbox" ${checked} onchange="toggleAutoPlaylist('${escapeAttr(p.key)}')"
                style="opacity:0;width:0;height:0;position:absolute">
              <span style="position:absolute;top:0;left:0;right:0;bottom:0;background:${toggleBg};border-radius:10px;transition:0.2s"></span>
              <span style="position:absolute;top:2px;left:${togglePos};width:16px;height:16px;background:white;border-radius:50%;transition:0.2s"></span>
            </label>
            <div style="flex:1;min-width:0">
              <div style="font-size:13px;font-weight:500">${p.name}</div>
              <div style="font-size:11px;color:var(--text3)">${p.origin}</div>
            </div>
            ${sortDrop}
            ${countBadge}
          </div>`;
      }
    }
    el.innerHTML = html;

    // Hide banner if user already has enabled playlists
    if (playlists.some(p => p.enabled)) {
      dismissAutoPlaylistBanner();
    }
  } catch (e) {
    el.innerHTML = `<div style="padding:16px;color:var(--text3);font-size:13px">Failed to load auto playlists</div>`;
  }
}

async function toggleAutoPlaylist(key) {
  // Find current state from the checkbox (it already toggled)
  const el = document.getElementById('auto-playlists-list');
  const checkbox = el.querySelector(`input[onchange*="${CSS.escape(key)}"]`);
  const enabled = checkbox ? checkbox.checked : true;

  try {
    const r = await api('/api/smartlists/auto-playlists/toggle', {
      method: 'POST',
      body: { key, enabled },
    });
    if (r.jellyfin_error) {
      toast(`Playlist ${enabled ? 'enabled' : 'disabled'} but Jellyfin sync failed \u2014 ${r.jellyfin_error}`, 'warning');
    } else {
      toast(r.message || (enabled ? 'Playlist enabled' : 'Playlist disabled'));
    }
    // Reload to reflect updated state
    setTimeout(() => loadAutoPlaylists(), 500);
  } catch (e) {
    toast(e.message, 'error');
    loadAutoPlaylists();
  }
}

async function setPlaylistSort(name, combined) {
  const [sortBy, dir] = combined.split('_');
  const sortOrder = dir === 'asc' ? 'Ascending' : 'Descending';
  try {
    const r = await api('/api/smartlists/sort', {
      method: 'POST',
      body: { name, sort_by: sortBy, sort_order: sortOrder },
    });
    if (r.success) {
      toast('Sort updated');
      _smartlistSortCache[name] = { sort_by: sortBy, sort_order: sortOrder };
    } else {
      toast(r.message || 'Failed to update sort', 'error');
    }
  } catch (e) {
    toast('Failed: ' + e.message, 'error');
  }
}

async function syncPlaylistsToJellyfin() {
  toast('Syncing playlists to Jellyfin...', 'info');
  try {
    const r = await api('/api/smartlists/sync', { method: 'POST' });
    const created = r.created || 0;
    const updated = r.updated || 0;
    const removed = r.removed || 0;
    const artUpdated = (r.artwork || {}).updated || 0;
    const parts = [];
    if (created) parts.push(`${created} created`);
    if (updated) parts.push(`${updated} updated`);
    if (removed) parts.push(`${removed} removed`);
    if (artUpdated) parts.push(`${artUpdated} artwork uploaded`);
    toast(parts.length ? `Synced: ${parts.join(', ')}` : 'Playlists up to date');
  } catch (e) {
    toast('Sync failed: ' + e.message, 'error');
  }
}

async function loadListSubscriptions() {
  loadListCards();
}

function showAddList() {
  document.getElementById('list-modal-title').textContent = 'Add List';
  document.getElementById('l-name').value = '';
  document.getElementById('l-url').value = '';
  document.getElementById('l-tag').value = '';
  document.getElementById('l-tag').dataset.manualEdit = '';
  document.getElementById('l-type').value = 'imdb_rss';
  document.getElementById('l-auto-fetch').checked = true;
  document.getElementById('l-url-hint').textContent = 'Supports IMDb lists, charts, watchlists, Trakt, and Letterboxd';
  document.getElementById('l-url-hint').style.color = '';
  showModal('modal-add-list');
}

function onModalUrlInput() {
  const url = document.getElementById('l-url').value;
  const urlLower = url.toLowerCase();
  const typeEl = document.getElementById('l-type');
  const hintEl = document.getElementById('l-url-hint');

  // Auto-detect type
  if (urlLower.includes('imdb.com/list/') || urlLower.includes('imdb.com/chart/') || urlLower.includes('imdb.com/user/')) {
    typeEl.value = 'imdb_rss';
  } else if (urlLower.includes('trakt.tv')) {
    typeEl.value = 'trakt';
  } else if (urlLower.includes('letterboxd.com')) {
    typeEl.value = 'letterboxd';
  }

  // Update hint to confirm detection
  if (urlLower.includes('imdb.com/chart/top')) {
    hintEl.textContent = 'IMDb Top 250 chart';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.includes('imdb.com/chart/moviemeter')) {
    hintEl.textContent = 'IMDb Most Popular Movies';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.match(/imdb\.com\/user\/ur\d+/)) {
    hintEl.textContent = 'IMDb user watchlist';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.match(/imdb\.com\/list\/ls\d+/)) {
    hintEl.textContent = 'IMDb custom list';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.includes('trakt.tv')) {
    hintEl.textContent = 'Trakt list';
    hintEl.style.color = 'var(--accent)';
  } else if (urlLower.includes('letterboxd.com')) {
    hintEl.textContent = 'Letterboxd list';
    hintEl.style.color = 'var(--accent)';
  } else if (url.trim()) {
    hintEl.textContent = 'Paste an IMDb, Trakt, or Letterboxd URL';
    hintEl.style.color = '';
  } else {
    hintEl.textContent = 'Supports IMDb lists, charts, watchlists, Trakt, and Letterboxd';
    hintEl.style.color = '';
  }
}

function onModalNameInput() {
  const name = document.getElementById('l-name').value;
  const tagEl = document.getElementById('l-tag');
  // Auto-fill tag from name unless user manually edited tag
  if (!tagEl.dataset.manualEdit) {
    tagEl.value = name;
  }
}

async function saveList() {
  const autoFetch = document.getElementById('l-auto-fetch').checked;
  const body = {
    name: document.getElementById('l-name').value.trim(),
    type: document.getElementById('l-type').value,
    url: document.getElementById('l-url').value.trim(),
    tag: document.getElementById('l-tag').value.trim() || document.getElementById('l-name').value.trim(),
  };

  if (!body.name || !body.url) {
    toast('Name and URL are required', 'error');
    return;
  }
  if (!body.tag) body.tag = body.name;

  try {
    const created = await api('/api/lists', { method: 'POST', body });
    closeModal('modal-add-list');
    loadListCards();
    if (autoFetch && created.id) {
      fetchList(created.id);
    } else {
      toast('List subscription added');
    }
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function fetchList(id) {
  const loading = toast('Fetching list — this may take a moment...', 'loading', 0);
  try {
    const r = await api(`/api/lists/${id}/fetch`, { method: 'POST' });
    loading.remove();
    let msg = `Fetched ${r.fetched} items — ${r.stored} stored`;
    if (r.skipped_no_tmdb) msg += `, ${r.skipped_no_tmdb} skipped (no TMDB match)`;
    if (r.skipped_duplicate) msg += `, ${r.skipped_duplicate} skipped (duplicate)`;
    if (r.tagged) msg += `, ${r.tagged} tagged in library`;
    toast(msg, 'success', 8000);
    loadListCards();
  } catch (e) {
    loading.remove();
    toast(e.message, 'error');
  }
}

async function deleteList(id) {
  if (!confirm('Delete this list subscription?')) return;
  try {
    await api(`/api/lists/${id}`, { method: 'DELETE' });
    toast('List deleted');
    loadListCards();
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── LIST COVERAGE ────────────────────────────────────────────────────────

let _coverageListId = null;

async function showListCoverage(listId, listName) {
  _coverageListId = listId;
  document.getElementById('coverage-modal-title').textContent = listName;
  document.getElementById('coverage-loading').style.display = 'block';
  document.getElementById('coverage-content').style.display = 'none';
  showModal('modal-list-coverage');

  try {
    const data = await api(`/api/lists/${listId}/coverage`);
    renderCoverage(data);
  } catch (e) {
    document.getElementById('coverage-loading').innerHTML =
      `<div style="color:var(--red);font-size:13px">${e.message}</div>`;
  }
}

let _coverageData = null;
let _coverageFilter = 'all';

function renderCoverage(data) {
  document.getElementById('coverage-loading').style.display = 'none';
  document.getElementById('coverage-content').style.display = 'block';

  _coverageData = data;
  _coverageData._allDownloaded = [...data.radarr, ...(data.sonarr || [])];
  _coverageFilter = 'all';

  const total = data.total;
  const vodCount = data.vod_count ?? data.vod.length;
  const downloadedCount = _coverageData._allDownloaded.length;
  const missingCount = data.missing_count ?? data.missing.length;
  const vodPct = total ? Math.round(vodCount / total * 100) : 0;
  const downloadedPct = total ? Math.round(downloadedCount / total * 100) : 0;
  const missingPct = total ? Math.round(missingCount / total * 100) : 0;

  // Downloaded subtitle
  const radarrCount = data.radarr_count ?? data.radarr.length;
  const sonarrCount = data.sonarr_count ?? (data.sonarr || []).length;
  let dlSub = '';
  if (radarrCount && sonarrCount) dlSub = `${radarrCount} Radarr, ${sonarrCount} Sonarr`;
  else if (radarrCount) dlSub = 'In Radarr';
  else if (sonarrCount) dlSub = 'In Sonarr';
  else dlSub = 'Downloaded';

  document.getElementById('coverage-summary').innerHTML = `
    <div class="coverage-filter-btn active" onclick="setCoverageFilter('all')" data-filter="all" style="flex:1;min-width:120px;background:var(--bg2);border:2px solid var(--accent);border-radius:var(--radius-sm);padding:12px;cursor:pointer;transition:border-color 0.15s,opacity 0.15s">
      <div style="font-size:22px;font-weight:700;color:var(--text)">${total}</div>
      <div style="font-size:11px;color:var(--text3)">All Items</div>
    </div>
    <div class="coverage-filter-btn" onclick="setCoverageFilter('vod')" data-filter="vod" style="flex:1;min-width:120px;background:var(--green-dim);border:2px solid transparent;border-radius:var(--radius-sm);padding:12px;cursor:pointer;transition:border-color 0.15s,opacity 0.15s">
      <div style="font-size:22px;font-weight:700;color:var(--green)">${vodCount}<span style="font-size:12px;font-weight:400;margin-left:4px">${vodPct}%</span></div>
      <div style="font-size:11px;color:var(--text3)">In VOD</div>
    </div>
    <div class="coverage-filter-btn" onclick="setCoverageFilter('downloaded')" data-filter="downloaded" style="flex:1;min-width:120px;background:var(--blue-dim);border:2px solid transparent;border-radius:var(--radius-sm);padding:12px;cursor:pointer;transition:border-color 0.15s,opacity 0.15s">
      <div style="font-size:22px;font-weight:700;color:var(--blue)">${downloadedCount}<span style="font-size:12px;font-weight:400;margin-left:4px">${downloadedPct}%</span></div>
      <div style="font-size:11px;color:var(--text3)">${dlSub}</div>
    </div>
    <div class="coverage-filter-btn" onclick="setCoverageFilter('missing')" data-filter="missing" style="flex:1;min-width:120px;background:var(--red-dim);border:2px solid transparent;border-radius:var(--radius-sm);padding:12px;cursor:pointer;transition:border-color 0.15s,opacity 0.15s">
      <div style="font-size:22px;font-weight:700;color:var(--red)">${missingCount}<span style="font-size:12px;font-weight:400;margin-left:4px">${missingPct}%</span></div>
      <div style="font-size:11px;color:var(--text3)">Missing</div>
    </div>`;

  _applyCoverageFilter();
}

function setCoverageFilter(filter) {
  _coverageFilter = filter;
  // Update button styles
  document.querySelectorAll('.coverage-filter-btn').forEach(btn => {
    const isActive = btn.dataset.filter === filter;
    btn.classList.toggle('active', isActive);
    btn.style.borderColor = isActive ? 'var(--accent)' : 'transparent';
    btn.style.opacity = isActive ? '1' : '0.7';
  });
  _applyCoverageFilter();
}

function _applyCoverageFilter() {
  const data = _coverageData;
  if (!data) return;

  const grid = document.getElementById('coverage-grid');
  const actions = document.getElementById('coverage-actions');
  const oldBtn = document.getElementById('coverage-add-all-btn');

  let items = [];
  let showAdd = false;

  switch (_coverageFilter) {
    case 'vod':
      items = data.vod.map(m => coverageCard(m));
      break;
    case 'downloaded':
      items = data._allDownloaded.map(m => coverageCard(m));
      break;
    case 'missing':
      items = data.missing.map(m => coverageCard(m, true));
      showAdd = true;
      break;
    default: // 'all'
      items = [
        ...data.vod.map(m => coverageCard(m)),
        ...data._allDownloaded.map(m => coverageCard(m)),
        ...data.missing.map(m => coverageCard(m, true)),
      ];
      break;
  }

  grid.innerHTML = items.join('');

  // Add-all buttons
  oldBtn.style.display = 'none';
  let wrapper = document.getElementById('coverage-add-all-wrapper');
  if (!wrapper) {
    wrapper = document.createElement('span');
    wrapper.id = 'coverage-add-all-wrapper';
    wrapper.style.display = 'flex';
    wrapper.style.gap = '8px';
    actions.appendChild(wrapper);
  }

  if (showAdd) {
    const missingMovies = data.missing_movies || data.missing.filter(m => m.media_type !== 'series').length;
    const missingSeries = data.missing_series || data.missing.filter(m => m.media_type === 'series').length;
    let btnsHtml = '';
    if (missingMovies > 0) {
      btnsHtml += `<button class="btn btn-primary btn-sm coverage-add-all-action" data-target="radarr" onclick="addAllMissingToArr('radarr')">Add ${missingMovies} to Radarr</button>`;
    }
    if (missingSeries > 0) {
      btnsHtml += `<button class="btn btn-primary btn-sm coverage-add-all-action" data-target="sonarr" onclick="addAllMissingToArr('sonarr')">Add ${missingSeries} to Sonarr</button>`;
    }
    wrapper.innerHTML = btnsHtml;
    actions.style.display = btnsHtml ? 'flex' : 'none';
  } else {
    wrapper.innerHTML = '';
    actions.style.display = 'none';
  }
}

function coverageCard(item, showAdd = false) {
  const poster = item.poster_path
    ? `<img src="https://image.tmdb.org/t/p/w200${item.poster_path}" alt="" loading="lazy">`
    : `<div class="no-poster">🎬</div>`;
  const year = item.year ? ` (${item.year})` : '';
  const isSeries = (item.media_type || 'movie') === 'series';
  const addBtn = showAdd
    ? `<button class="card-add-btn" title="Add to ${isSeries ? 'Sonarr' : 'Radarr'}" data-tmdb="${item.tmdb_id}" onclick="event.stopPropagation();showAddToArrModal(${item.tmdb_id},'${escapeJS(item.title)}','${escapeJS(item.year||'')}','${escapeJS(item.poster_path||'')}','${isSeries ? 'series' : 'movie'}')">+</button>`
    : '';
  const mt = item.media_type || 'movie';
  const clickAttr = item.tmdb_id
    ? `onclick="showCoverageDetail(${item.tmdb_id},'${mt}','${escapeJS(item.title||'')}','${escapeJS(item.year||'')}','${escapeJS(item.poster_path||'')}')" style="cursor:pointer"`
    : '';
  return `<div class="coverage-card" ${clickAttr}>
    ${poster}${addBtn}
    <div class="card-title" title="${escapeAttr((item.title || '') + year)}">${item.title || 'Unknown'}${year}</div>
  </div>`;
}

async function addAllMissingToArr(target = 'radarr') {
  const label = target === 'sonarr' ? 'Sonarr' : 'Radarr';
  const btn = document.querySelector(`.coverage-add-all-action[data-target="${target}"]`);
  if (btn) { btn.disabled = true; btn.textContent = 'Adding...'; }
  try {
    const r = await api(`/api/lists/${_coverageListId}/add-missing-to-${target}`, { method: 'POST', body: {} });
    toast(`Added ${r.added} to ${label}${r.already_exists ? `, ${r.already_exists} already existed` : ''}${r.failed ? `, ${r.failed} failed` : ''}`);
    if (btn) btn.textContent = `Done (${r.added} added)`;
  } catch (err) {
    toast(err.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = `Add all to ${label}`; }
  }
}

// Keep old function name for backwards compat
function addAllMissingToRadarr() { addAllMissingToArr('radarr'); }

// ── SMARTLISTS ───────────────────────────────────────────────────────────

async function loadSmartLists() {
  const el = document.getElementById('smartlists-table');
  const statusEl = document.getElementById('smartlists-path-status');
  try {
    const data = await api('/api/smartlists');
    const lists = data.smartlists || [];

    statusEl.innerHTML = data.path_accessible
      ? `<span style="color:var(--green)">${data.path}</span>`
      : `<span style="color:var(--red)">${data.path} (not accessible)</span>`;

    if (!lists.length) {
      el.innerHTML = '<div class="empty-state" style="padding:24px"><p>No SmartLists to manage yet. Run a sync first.</p></div>';
      return;
    }

    let html = `<table style="width:100%;font-size:12px;border-collapse:collapse">
      <thead><tr style="text-align:left;color:var(--text3);border-bottom:1px solid var(--border)">
        <th style="padding:8px 12px;font-weight:500">Name</th>
        <th style="padding:8px 12px;font-weight:500">Tag</th>
        <th style="padding:8px 12px;font-weight:500">Media</th>
        <th style="padding:8px 12px;font-weight:500;text-align:center">Status</th>
      </tr></thead><tbody>`;

    for (const sl of lists) {
      const mediaLabel = sl.media_type.join(', ');
      const statusBadge = sl.exists_on_disk
        ? '<span class="badge badge-green">On disk</span>'
        : '<span class="badge" style="background:var(--amber-dim);color:var(--amber)">Missing</span>';

      html += `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:8px 12px;color:var(--text)">${sl.name}</td>
        <td style="padding:8px 12px"><span class="badge badge-accent">${sl.tag}</span></td>
        <td style="padding:8px 12px;color:var(--text2)">${mediaLabel}</td>
        <td style="padding:8px 12px;text-align:center">${statusBadge}</td>
      </tr>`;
    }

    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div class="empty-state" style="padding:24px"><p>Failed to load SmartLists</p></div>';
  }
}

async function syncSmartLists() {
  toast('Syncing playlists to Jellyfin...', 'info');
  try {
    const r = await api('/api/smartlists/sync', { method: 'POST' });
    toast(`Playlists synced: ${r.created} created, ${r.updated} updated, ${r.total} total`);
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function refreshTags() {
  toast('Refreshing tags...', 'info');
  try {
    const r = await api('/api/sync/refresh-tags', { method: 'POST' });
    toast(`Updated ${r.updated_movies} movies, ${r.updated_series} series`);
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── HOME SCREEN ──────────────────────────────────────────────────────────

let homeRows = [];

async function loadHomeScreen() {
  const listEl = document.getElementById('home-rows-list');
  const heroSelect = document.getElementById('home-hero-select');

  try {
    const data = await api('/api/smartlists/home-config');
    const config = data.exists ? data.config : {};
    homeRows = (config.rows || []).sort((a, b) => a.order - b.order);

    // Populate hero dropdown from ALL available playlists, not just home rows
    try {
      const allPlaylists = await api('/api/smartlists/all-playlists');
      const playlists = (allPlaylists.playlists || []).sort((a, b) => a.name.localeCompare(b.name));
      heroSelect.innerHTML = '<option value="">-- disabled --</option>';
      for (const p of playlists) {
        const selected = config.hero && config.hero.playlist_id === p.playlist_id ? ' selected' : '';
        heroSelect.innerHTML += `<option value="${p.playlist_id}"${selected}>${p.name}</option>`;
      }
    } catch (_) {
      // Fallback to home rows if endpoint fails
      heroSelect.innerHTML = '<option value="">-- disabled --</option>';
      for (const r of homeRows) {
        const selected = config.hero && config.hero.playlist_id === r.playlist_id ? ' selected' : '';
        heroSelect.innerHTML += `<option value="${r.playlist_id}"${selected}>${r.display_name}</option>`;
      }
    }

    // Set hero sort dropdown and require_logo checkbox
    const heroSortEl = document.getElementById('home-hero-sort');
    if (heroSortEl && config.hero) {
      const sortBy = config.hero.sort_by || 'random';
      const sortOrder = config.hero.sort_order === 'Ascending' ? 'asc' : 'desc';
      heroSortEl.value = sortBy + '_' + sortOrder;
    }
    const logoCheckbox = document.getElementById('home-hero-require-logo');
    if (logoCheckbox && config.hero) {
      logoCheckbox.checked = config.hero.require_logo !== false;
    }

    if (!homeRows.length) {
      listEl.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text3);font-size:13px">No rows configured — the default Jellyfin home screen will be used.</div>';
    } else {
      renderHomeRows();
    }
  } catch (e) {
    listEl.innerHTML = '<div class="empty-state" style="padding:24px"><p>Failed to load home config</p></div>';
  }
}

function rowKey(row) {
  if (row.type === 'builtin') return `builtin:${row.section_id}`;
  return `playlist:${row.playlist_id}`;
}

function renderHomeRows() {
  const listEl = document.getElementById('home-rows-list');
  if (!homeRows.length) {
    listEl.innerHTML = '<div class="empty-state" style="padding:24px"><p>No rows</p></div>';
    return;
  }

  listEl.innerHTML = homeRows.map((row, i) => {
    const key = rowKey(row);
    const isBuiltin = row.type === 'builtin';
    const badge = isBuiltin
      ? '<span style="font-size:9px;padding:2px 6px;border-radius:3px;background:var(--blue);color:#fff;white-space:nowrap">Jellyfin</span>'
      : '<span style="font-size:9px;padding:2px 6px;border-radius:3px;background:var(--purple);color:#fff;white-space:nowrap">Tentacle</span>';
    const maxItemsInput = isBuiltin ? '' : `
      <input type="number" min="5" max="100" value="${row.max_items || 20}"
        onclick="event.stopPropagation()" onmousedown="event.stopPropagation()"
        onchange="saveRowMaxItemsByKey('${key}', this.value)"
        style="width:52px;padding:3px 4px;font-size:11px;text-align:center;background:var(--bg1);border:1px solid var(--border);border-radius:4px;color:var(--text);cursor:text"
        title="Max items in this row" draggable="false">`;
    return `
    <div class="home-row-item" draggable="true" data-idx="${i}"
      ondragstart="homeRowDragStart(event)" ondragover="homeRowDragOver(event)" ondrop="homeRowDrop(event)"
      style="display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;background:var(--bg2);cursor:grab">
      <span style="color:var(--text3);font-size:11px;width:24px;text-align:center">${i + 1}</span>
      <span style="color:var(--text3);font-size:16px;cursor:grab">&#x2630;</span>
      <span style="flex:1;font-size:13px;color:var(--text)">${row.display_name}</span>
      ${maxItemsInput}
      ${badge}
      <button onclick="event.stopPropagation();removeHomeRowByKey('${key}')"
        style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:14px;padding:4px 6px;border-radius:4px"
        onmouseover="this.style.color='var(--red)'" onmouseout="this.style.color='var(--text3)'"
        title="Remove row">&#10005;</button>
    </div>`;
  }).join('');
}

let homeRowDragIdx = null;
function homeRowDragStart(e) {
  homeRowDragIdx = parseInt(e.currentTarget.getAttribute('data-idx'));
  e.dataTransfer.effectAllowed = 'move';
}
function homeRowDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
}
async function homeRowDrop(e) {
  e.preventDefault();
  const dropIdx = parseInt(e.currentTarget.getAttribute('data-idx'));
  if (homeRowDragIdx === null || homeRowDragIdx === dropIdx) return;
  const moved = homeRows.splice(homeRowDragIdx, 1)[0];
  homeRows.splice(dropIdx, 0, moved);
  for (let i = 0; i < homeRows.length; i++) homeRows[i].order = i + 1;
  homeRowDragIdx = null;
  renderHomeRows();
  try {
    await api('/api/smartlists/reorder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order: homeRows.map(r => rowKey(r)) }),
    });
    pushHomeConfig();
  } catch (err) {
    toast('Failed to save row order: ' + err.message, 'error');
  }
}

async function updateHeroPick() {
  const heroSelect = document.getElementById('home-hero-select');
  try {
    const r = await api('/api/smartlists/hero', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ playlist_id: heroSelect.value }),
    });
    if (r.success) pushHomeConfig();
    else toast(r.message || 'Failed to update hero', 'error');
  } catch (e) {
    toast('Failed to save hero: ' + e.message, 'error');
  }
}

async function updateHeroSort() {
  const el = document.getElementById('home-hero-sort');
  const [sortBy, dir] = el.value.split('_');
  const sortOrder = dir === 'asc' ? 'Ascending' : 'Descending';
  const requireLogo = document.getElementById('home-hero-require-logo')?.checked ?? true;
  try {
    const r = await api('/api/smartlists/hero-sort', {
      method: 'POST',
      body: { sort_by: sortBy, sort_order: sortOrder, require_logo: requireLogo },
    });
    if (r.success) pushHomeConfig();
    else toast(r.message || 'Failed', 'error');
  } catch (e) {
    toast('Failed: ' + e.message, 'error');
  }
}

async function pushHomeConfig() {
  try {
    const r = await api('/api/smartlists/notify', { method: 'POST' });
    if (r.notified) {
      toast('Jellyfin notified — home screen will update shortly');
    } else {
      toast('Saved but Jellyfin plugin didn\'t respond — ' + (r.error || 'check plugin is installed'), 'warning');
    }
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function saveRowMaxItems(playlistId, val) {
  const v = Math.max(5, Math.min(100, parseInt(val) || 20));
  try {
    await api('/api/smartlists/row-max-items', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ playlist_id: playlistId, max_items: v }),
    });
    const row = homeRows.find(r => r.playlist_id === playlistId);
    if (row) row.max_items = v;
    toast(`Row limit set to ${v}`);
  } catch (e) {
    toast('Failed to save: ' + e.message, 'error');
  }
}

async function saveRowMaxItemsByKey(key, val) {
  const v = Math.max(5, Math.min(100, parseInt(val) || 20));
  try {
    await api('/api/smartlists/row-max-items', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row_key: key, max_items: v }),
    });
    const row = homeRows.find(r => rowKey(r) === key);
    if (row) row.max_items = v;
    toast(`Row limit set to ${v}`);
  } catch (e) {
    toast('Failed to save: ' + e.message, 'error');
  }
}

// ── HOME ROW MANAGEMENT ──────────────────────────────────────────────────

async function showAddHomeRow() {
  const wrapper = document.getElementById('add-row-inline');
  const select = document.getElementById('add-row-select');
  wrapper.style.display = '';

  try {
    const [playlistData, builtinData] = await Promise.all([
      api('/api/smartlists/available-playlists'),
      api('/api/smartlists/builtin-sections'),
    ]);
    const playlists = playlistData.playlists || [];
    const builtins = builtinData.sections || [];

    select.innerHTML = '<option value="">Select a row to add...</option>';
    if (builtins.length) {
      select.innerHTML += '<optgroup label="Jellyfin Sections">';
      for (const s of builtins) {
        select.innerHTML += `<option value="builtin:${s.section_id}">${s.display_name}</option>`;
      }
      select.innerHTML += '</optgroup>';
    }
    if (playlists.length) {
      select.innerHTML += '<optgroup label="Tentacle Playlists">';
      for (const p of playlists) {
        select.innerHTML += `<option value="playlist:${p.playlist_id}">${p.name}</option>`;
      }
      select.innerHTML += '</optgroup>';
    }
    if (!playlists.length && !builtins.length) {
      select.innerHTML = '<option value="">All rows already added</option>';
    }
  } catch (e) {
    select.innerHTML = '<option value="">Failed to load options</option>';
  }
}

function hideAddHomeRow() {
  document.getElementById('add-row-inline').style.display = 'none';
}

async function confirmAddHomeRow() {
  const select = document.getElementById('add-row-select');
  const val = select.value;
  if (!val) { toast('Select a row first', 'error'); return; }

  const body = {};
  if (val.startsWith('builtin:')) {
    body.section_id = val.replace('builtin:', '');
  } else if (val.startsWith('playlist:')) {
    body.playlist_id = val.replace('playlist:', '');
  } else {
    body.playlist_id = val;  // backwards compat
  }

  try {
    const r = await api('/api/smartlists/add-row', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.success) {
      toast('Row added');
      hideAddHomeRow();
      loadHomeScreen();
      pushHomeConfig();
    } else {
      toast(r.message || 'Failed to add row', 'error');
    }
  } catch (e) {
    toast('Failed to add row: ' + e.message, 'error');
  }
}

async function removeHomeRow(playlistId) {
  try {
    const r = await api('/api/smartlists/remove-row', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ playlist_id: playlistId }),
    });
    if (r.success) {
      toast('Row removed');
      loadHomeScreen();
      pushHomeConfig();
    } else {
      toast(r.message || 'Failed to remove row', 'error');
    }
  } catch (e) {
    toast('Failed to remove row: ' + e.message, 'error');
  }
}

async function removeHomeRowByKey(key) {
  try {
    const r = await api('/api/smartlists/remove-row', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row_key: key }),
    });
    if (r.success) {
      toast('Row removed');
      loadHomeScreen();
      pushHomeConfig();
    } else {
      toast(r.message || 'Failed to remove row', 'error');
    }
  } catch (e) {
    toast('Failed to remove row: ' + e.message, 'error');
  }
}

// ── TAG RULES ─────────────────────────────────────────────────────────────
const RULE_FIELDS = [
  { value: 'genre', label: 'Genre', operators: ['contains'] },
  { value: 'rating', label: 'Rating', operators: ['greater_than', 'less_than'] },
  { value: 'year', label: 'Year', operators: ['equals', 'greater_than', 'less_than'] },
  { value: 'source', label: 'Provider', operators: ['equals'] },
  { value: 'list', label: 'Imported List', operators: ['equals'] },
  { value: 'runtime', label: 'Runtime (min)', operators: ['greater_than', 'less_than'] },
  { value: 'downloaded', label: 'Downloaded', operators: ['equals'] },
];

const OP_LABELS = { contains: 'contains', equals: 'equals', greater_than: '>', less_than: '<' };

const _LOCKED_SORT_PLAYLISTS = ['Recently Added Movies', 'Recently Added TV'];

function _sortDropdown(name) {
  // Built-in playlists with forced sort show a locked label instead of a dropdown
  if (_LOCKED_SORT_PLAYLISTS.includes(name)) {
    return `<span style="font-size:11px;padding:3px 8px;background:var(--bg2);color:var(--text3);border:1px solid var(--border);border-radius:4px;white-space:nowrap"
      title="Sort is locked for this playlist">Recently Added</span>`;
  }
  const info = _smartlistSortCache[name] || { sort_by: 'releasedate', sort_order: 'Descending' };
  const val = info.sort_by + '_' + (info.sort_order === 'Ascending' ? 'asc' : 'desc');
  const opts = [
    ['releasedate_desc', 'Newest First'],
    ['releasedate_asc', 'Oldest First'],
    ['name_asc', 'A \u2192 Z'],
    ['name_desc', 'Z \u2192 A'],
    ['communityrating_desc', 'Top Rated'],
    ['datecreated_desc', 'Recently Added'],
    ['random_asc', 'Random'],
  ];
  const escaped = name.replace(/'/g, "\\'");
  return `<select onchange="setPlaylistSort('${escaped}', this.value)"
    style="font-size:11px;padding:3px 8px;background:var(--bg2);color:var(--text2);border:1px solid var(--border);border-radius:4px;cursor:pointer"
    title="Sort order">${opts.map(([v, l]) => `<option value="${v}"${v === val ? ' selected' : ''}>${l}</option>`).join('')}</select>`;
}

async function loadTagRules() {
  const el = document.getElementById('tag-rules-list');
  try {
    const rules = await api('/api/tags/rules');
    _tagRulesCache = rules;
    if (!rules.length) {
      el.innerHTML = `<div class="empty-state" style="padding:32px">
        <div class="empty-icon" style="font-size:24px">&#9881;</div>
        <p style="margin-bottom:12px">No custom playlists yet</p>
        <button class="btn btn-primary btn-sm" onclick="showAddTagRule()">Create Playlist</button>
      </div>`;
      return;
    }

    const typeLabels = { both: 'Movies & Series', movies: 'Movies', series: 'Series' };

    el.innerHTML = rules.map(rule => {
      const condStr = (rule.conditions || []).map(c => {
        const fld = RULE_FIELDS.find(f => f.value === c.field);
        let display = `${fld ? fld.label : c.field} ${OP_LABELS[c.operator] || c.operator} ${c.value}`;
        if (c.field === 'downloaded') display = c.value === 'yes' ? 'Downloaded (Radarr)' : 'VOD only';
        else if (c.field === 'list') display = `List: ${c.value}`;
        else if (c.field === 'source') display = `Provider: ${c.value}`;
        return `<span class="badge badge-gray">${display}</span>`;
      }).join(' ');

      return `
        <div class="list-item">
          <div class="list-info" style="flex:1">
            <div class="list-name">${rule.name}</div>
            <div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px">
              <span class="badge badge-accent">${typeLabels[rule.apply_to] || rule.apply_to}</span>
              ${condStr}
            </div>
            <div class="list-meta" style="margin-top:4px">${rule.active ? '<span style="color:var(--green)">Active</span>' : '<span style="color:var(--text3)">Inactive</span>'}</div>
          </div>
          <div style="display:flex;gap:6px;align-items:center">
            ${_sortDropdown(rule.output_tag)}
            <div class="dot ${rule.active ? 'dot-green' : 'dot-gray'}"></div>
            <button class="btn btn-secondary btn-sm" onclick="editTagRule(${rule.id})">Edit</button>
            <button class="btn btn-danger btn-sm" onclick="deleteTagRule(${rule.id})">&#10005;</button>
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div class="empty-state" style="padding:24px"><p>Failed to load custom playlists</p></div>';
  }
}

let _tagRulesCache = [];
let _conditionOptions = null;

async function _ensureConditionOptions() {
  if (!_conditionOptions) {
    try { _conditionOptions = await api('/api/tags/condition-options'); }
    catch (_) { _conditionOptions = { sources: [], lists: [] }; }
  }
  return _conditionOptions;
}

async function showAddTagRule() {
  document.getElementById('tag-rule-modal-title').textContent = 'Create Playlist';
  document.getElementById('edit-rule-id').value = '';
  document.getElementById('tr-name').value = '';
  document.getElementById('tr-tag').value = '';
  document.getElementById('tr-tag').dataset.manualEdit = '';
  document.getElementById('tr-apply-to').value = 'both';
  document.getElementById('tr-active').checked = true;
  document.getElementById('tr-conditions').innerHTML = '';

  // Reset friendly builder
  const radios = document.querySelectorAll('input[name="tr-content-source"]');
  for (const r of radios) r.checked = r.value === 'all';
  document.getElementById('tr-source-pick').style.display = 'none';
  document.getElementById('tr-list-pick').style.display = 'none';
  document.getElementById('tr-filter-genre').value = '';
  document.getElementById('tr-filter-rating').value = '';
  document.getElementById('tr-filter-year').value = '';
  document.getElementById('tr-source-section').style.display = '';
  document.getElementById('tr-simple-filters').style.display = '';
  document.getElementById('tr-advanced-section').style.display = 'none';
  document.getElementById('tr-advanced-label').textContent = 'Show advanced filters';

  // Populate source/list dropdowns
  await _ensureConditionOptions();
  const srcPick = document.getElementById('tr-source-pick');
  srcPick.innerHTML = '<option value="">Select...</option>' +
    (_conditionOptions?.sources || []).map(s => `<option value="${escapeAttr(s)}">${s}</option>`).join('');
  const listPick = document.getElementById('tr-list-pick');
  listPick.innerHTML = '<option value="">Select...</option>' +
    (_conditionOptions?.lists || []).map(l => `<option value="${escapeAttr(l.tag)}">${l.name}</option>`).join('');

  showModal('modal-tag-rule');
}

function onContentSourceChange() {
  const selected = document.querySelector('input[name="tr-content-source"]:checked')?.value || 'all';
  document.getElementById('tr-source-pick').style.display = selected === 'source' ? '' : 'none';
  document.getElementById('tr-list-pick').style.display = selected === 'list' ? '' : 'none';
}

function toggleAdvancedFilters() {
  const section = document.getElementById('tr-advanced-section');
  const label = document.getElementById('tr-advanced-label');
  const sourceSection = document.getElementById('tr-source-section');
  const simpleFilters = document.getElementById('tr-simple-filters');
  if (section.style.display === 'none') {
    section.style.display = '';
    sourceSection.style.display = 'none';
    simpleFilters.style.display = 'none';
    label.textContent = 'Use simple builder';
    if (!document.getElementById('tr-conditions').children.length) {
      addRuleCondition();
    }
  } else {
    section.style.display = 'none';
    sourceSection.style.display = '';
    simpleFilters.style.display = '';
    label.textContent = 'Show advanced filters';
  }
}

function onCollectionNameInput() {
  const name = document.getElementById('tr-name').value;
  const tagEl = document.getElementById('tr-tag');
  if (!tagEl.dataset.manualEdit) {
    tagEl.value = name.trim();
  }
}

async function editTagRule(id) {
  if (!_tagRulesCache.length) _tagRulesCache = await api('/api/tags/rules');
  const rule = _tagRulesCache.find(r => r.id === id);
  if (!rule) { toast('Rule not found', 'error'); return; }

  document.getElementById('tag-rule-modal-title').textContent = 'Edit Playlist';
  document.getElementById('edit-rule-id').value = id;
  document.getElementById('tr-name').value = rule.name;
  document.getElementById('tr-tag').value = rule.output_tag;
  document.getElementById('tr-tag').dataset.manualEdit = rule.output_tag !== rule.name ? 'true' : '';
  document.getElementById('tr-apply-to').value = rule.apply_to;
  document.getElementById('tr-active').checked = rule.active;

  const condEl = document.getElementById('tr-conditions');
  condEl.innerHTML = '';
  for (const cond of (rule.conditions || [])) addRuleCondition(cond.field, cond.operator, cond.value);

  // Editing always uses advanced mode (existing rules have raw conditions)
  document.getElementById('tr-source-section').style.display = 'none';
  document.getElementById('tr-simple-filters').style.display = 'none';
  document.getElementById('tr-advanced-section').style.display = '';
  document.getElementById('tr-advanced-label').textContent = 'Use simple builder';

  showModal('modal-tag-rule');
}

async function addRuleCondition(field = 'genre', operator = 'contains', value = '') {
  await _ensureConditionOptions();
  const el = document.getElementById('tr-conditions');
  const fieldOpts = RULE_FIELDS.map(f =>
    `<option value="${f.value}" ${f.value === field ? 'selected' : ''}>${f.label}</option>`
  ).join('');

  const row = document.createElement('div');
  row.className = 'form-row';
  row.style.marginBottom = '8px';
  row.style.alignItems = 'center';
  row.innerHTML = `
    <select class="form-input" style="flex:1" onchange="updateCondOps(this)" data-cond-field>${fieldOpts}</select>
    <select class="form-input" style="flex:1" data-cond-op></select>
    <div data-cond-val-wrap style="flex:1"></div>
    <button class="btn btn-danger btn-sm" onclick="this.parentElement.remove()" style="flex-shrink:0;padding:4px 8px">&#10005;</button>`;
  el.appendChild(row);

  const fieldDef = RULE_FIELDS.find(f => f.value === field);
  const opSel = row.querySelector('[data-cond-op]');
  opSel.innerHTML = (fieldDef ? fieldDef.operators : ['equals']).map(op =>
    `<option value="${op}" ${op === operator ? 'selected' : ''}>${OP_LABELS[op] || op}</option>`
  ).join('');

  _renderCondValueInput(row, field, value);
}

function _renderCondValueInput(row, field, value) {
  const wrap = row.querySelector('[data-cond-val-wrap]');
  if (field === 'source') {
    const opts = (_conditionOptions?.sources || []).map(s =>
      `<option value="${escapeAttr(s)}" ${s === value ? 'selected' : ''}>${s}</option>`
    ).join('');
    wrap.innerHTML = `<select class="form-input" data-cond-val><option value="">Select source...</option>${opts}</select>`;
  } else if (field === 'list') {
    const opts = (_conditionOptions?.lists || []).map(l =>
      `<option value="${escapeAttr(l.tag)}" ${l.tag === value ? 'selected' : ''}>${l.name}</option>`
    ).join('');
    wrap.innerHTML = `<select class="form-input" data-cond-val><option value="">Select list...</option>${opts}</select>`;
  } else if (field === 'downloaded') {
    wrap.innerHTML = `<select class="form-input" data-cond-val>
      <option value="yes" ${value === 'yes' || !value ? 'selected' : ''}>Yes (Radarr)</option>
      <option value="no" ${value === 'no' ? 'selected' : ''}>No (VOD only)</option>
    </select>`;
  } else {
    wrap.innerHTML = `<input class="form-input" placeholder="Value" value="${escapeAttr(value)}" data-cond-val>`;
  }
}

function updateCondOps(fieldSelect) {
  const row = fieldSelect.parentElement;
  const opSel = row.querySelector('[data-cond-op]');
  const field = fieldSelect.value;
  const fieldDef = RULE_FIELDS.find(f => f.value === field);
  opSel.innerHTML = (fieldDef ? fieldDef.operators : ['equals']).map(op =>
    `<option value="${op}">${OP_LABELS[op] || op}</option>`
  ).join('');
  _renderCondValueInput(row, field, '');
}

async function saveTagRule() {
  const ruleId = document.getElementById('edit-rule-id').value;
  const name = document.getElementById('tr-name').value.trim();
  let output_tag = document.getElementById('tr-tag').value.trim();
  const apply_to = document.getElementById('tr-apply-to').value;
  const active = document.getElementById('tr-active').checked;

  if (!output_tag && name) output_tag = name;
  if (!name || !output_tag) { toast('Playlist name is required', 'error'); return; }

  const isAdvanced = document.getElementById('tr-advanced-section').style.display !== 'none';
  const conditions = [];

  if (isAdvanced) {
    // Read from raw condition builder
    const condRows = document.getElementById('tr-conditions').children;
    for (const row of condRows) {
      const field = row.querySelector('[data-cond-field]').value;
      const operator = row.querySelector('[data-cond-op]').value;
      const value = row.querySelector('[data-cond-val]').value.trim();
      if (!value) { toast('All condition values are required', 'error'); return; }
      conditions.push({ field, operator, value });
    }
  } else {
    // Build from friendly builder
    const contentSource = document.querySelector('input[name="tr-content-source"]:checked')?.value || 'all';
    if (contentSource === 'source') {
      const src = document.getElementById('tr-source-pick').value;
      if (!src) { toast('Select a provider source', 'error'); return; }
      conditions.push({ field: 'source', operator: 'equals', value: src });
    } else if (contentSource === 'list') {
      const lst = document.getElementById('tr-list-pick').value;
      if (!lst) { toast('Select a list', 'error'); return; }
      conditions.push({ field: 'list', operator: 'equals', value: lst });
    } else if (contentSource === 'downloaded') {
      conditions.push({ field: 'downloaded', operator: 'equals', value: 'yes' });
    }

    const genre = document.getElementById('tr-filter-genre').value.trim();
    if (genre) conditions.push({ field: 'genre', operator: 'contains', value: genre });

    const rating = document.getElementById('tr-filter-rating').value.trim();
    if (rating) conditions.push({ field: 'rating', operator: 'greater_than', value: rating });

    const year = document.getElementById('tr-filter-year').value.trim();
    if (year) conditions.push({ field: 'year', operator: 'greater_than', value: year });
  }

  if (!conditions.length) { toast('At least one filter is required', 'error'); return; }

  try {
    if (ruleId) {
      await api(`/api/tags/rules/${ruleId}`, { method: 'PUT', body: { name, output_tag, apply_to, active, conditions } });
      toast('Playlist updated');
    } else {
      await api('/api/tags/rules', { method: 'POST', body: { name, output_tag, apply_to, active, conditions } });
      toast('Playlist created');
    }
    _tagRulesCache = [];
    closeModal('modal-tag-rule');
    loadTagRules();
    // Auto-sync to Jellyfin (configs + artwork)
    syncPlaylistsToJellyfin();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function deleteTagRule(id) {
  if (!confirm('Delete this playlist?')) return;
  try {
    await api(`/api/tags/rules/${id}`, { method: 'DELETE' });
    toast('Playlist deleted');
    _tagRulesCache = [];
    loadTagRules();
    // Auto-sync to Jellyfin (configs + cleanup + artwork)
    syncPlaylistsToJellyfin();
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── FOLLOWING TAB ─────────────────────────────────────────────────────────
async function loadFollowing() {
  const grid = document.getElementById('following-grid');
  const empty = document.getElementById('following-empty');
  grid.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  empty.style.display = 'none';
  try {
    const items = await api('/api/library/following');
    if (!items.length) {
      grid.innerHTML = '';
      empty.style.display = '';
      return;
    }
    grid.innerHTML = items.map(item => renderLibCard(item)).join('');
    // Update badge
    const badge = document.getElementById('lib-follow-badge');
    if (badge) { badge.textContent = items.length; badge.style.display = ''; }
  } catch (e) {
    grid.innerHTML = '<div class="empty-state"><p>Failed to load following list</p></div>';
  }
}

// ── DUPLICATES PAGE ───────────────────────────────────────────────────────
async function loadDuplicates() {
  try {
    const data = await api('/api/duplicates');
    pages.dup.items = data.duplicates;
    renderDupStats(data);
    renderDupList();
  } catch (e) {
    document.getElementById('dup-list').innerHTML = '<div class="empty-state"><p>Failed to load duplicates</p></div>';
  }
}

function renderDupStats(data) {
  const el = document.getElementById('dup-stats');
  el.innerHTML = `
    <div class="stat-card">
      <div class="stat-accent" style="background:var(--amber)"></div>
      <div class="stat-label">Pending</div>
      <div class="stat-value">${data.pending || 0}</div>
      <div class="stat-sub">need resolution</div>
    </div>
    <div class="stat-card">
      <div class="stat-accent" style="background:var(--green)"></div>
      <div class="stat-label">Resolved</div>
      <div class="stat-value">${data.resolved || 0}</div>
      <div class="stat-sub">cleaned up</div>
    </div>
    <div class="stat-card">
      <div class="stat-accent" style="background:var(--blue)"></div>
      <div class="stat-label">Total</div>
      <div class="stat-value">${data.total || 0}</div>
      <div class="stat-sub">detected</div>
    </div>`;
}

function setDupFilter(filter, btn) {
  pages.dup.filter = filter;
  document.querySelectorAll('[data-dupfilter]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderDupList();
}

function renderDupList() {
  const el = document.getElementById('dup-list');
  const filter = pages.dup.filter;
  let items = pages.dup.items;

  if (filter === 'pending') items = items.filter(d => d.resolution === 'pending');
  else if (filter === 'resolved') items = items.filter(d => d.resolution !== 'pending');

  if (!items.length) {
    el.innerHTML = `<div class="empty-state" style="padding:40px">
      <div class="empty-icon" style="font-size:24px">⊕</div>
      <p>${filter === 'pending' ? 'No pending duplicates — your library is clean!' : 'No resolved duplicates yet'}</p>
    </div>`;
    return;
  }

  el.innerHTML = items.map(dup => {
    const sources = dup.sources || [];
    const hasRadarr = sources.some(s => s.source === 'radarr');
    const hasVod = sources.some(s => s.source.startsWith('provider_'));
    const sourceCards = sources.map(s => {
      const isRadarr = s.source === 'radarr';
      const sourceLabel = isRadarr ? 'Downloaded (Radarr)' :
        s.source.startsWith('provider_') ? 'VOD (Streamed)' : s.source;
      const icon = isRadarr ? '&#11015;' : '&#128225;';
      const color = isRadarr ? 'var(--green)' : 'var(--amber)';
      return `
        <div class="dup-source-card" style="border-left:3px solid ${color}">
          <div style="font-size:12px;font-weight:500;margin-bottom:4px">${icon} ${sourceLabel}</div>
          <div style="font-size:11px;color:var(--text3);font-family:'DM Mono',monospace;word-break:break-all">${s.path || '(path not recorded)'}</div>
        </div>`;
    }).join('');

    const title = dup.title || `TMDB #${dup.tmdb_id}`;
    const poster = dup.poster_path ? `https://image.tmdb.org/t/p/w92${dup.poster_path}` : '';
    const resLabel = { keep_radarr: 'Kept Downloaded', keep_vod: 'Kept VOD', keep_both: 'Kept Both' };

    return `
      <div class="dup-row" style="display:flex;gap:16px;align-items:flex-start">
        ${poster ? `<img src="${poster}" style="width:48px;border-radius:6px;flex-shrink:0" alt="">` : ''}
        <div style="flex:1;min-width:0">
          <div class="dup-header">
            <span class="badge ${dup.resolution === 'pending' ? 'badge-amber' : 'badge-green'}">${dup.resolution === 'pending' ? 'Pending' : resLabel[dup.resolution] || dup.resolution}</span>
            <span class="badge badge-gray">${dup.media_type}</span>
            <span style="font-size:13px;font-weight:500">${title}</span>
          </div>
          <div class="dup-sources">${sourceCards}</div>
          ${dup.resolution === 'pending' ? `
            <div class="dup-actions">
              ${hasRadarr ? `<button class="btn btn-success btn-sm" onclick="resolveDup(${dup.id}, 'keep_radarr')">Keep Downloaded</button>` : ''}
              ${hasVod ? `<button class="btn btn-secondary btn-sm" onclick="resolveDup(${dup.id}, 'keep_vod')">Keep VOD</button>` : ''}
              <button class="btn btn-secondary btn-sm" onclick="resolveDup(${dup.id}, 'keep_both')">Keep Both</button>
            </div>` : ''}
        </div>
      </div>`;
  }).join('');
}

function resolveDup(id, resolution) {
  if (resolution === 'keep_both') {
    _executeResolveDup(id, resolution);
    return;
  }
  const titleEl = document.getElementById('resolve-dup-title');
  const msgEl = document.getElementById('resolve-dup-message');
  const warnEl = document.getElementById('resolve-dup-warning');
  const confirmBtn = document.getElementById('resolve-dup-confirm');

  if (resolution === 'keep_vod') {
    titleEl.textContent = 'Keep VOD Stream';
    msgEl.textContent = 'This will keep the streamed VOD version and remove the downloaded copy.';
    warnEl.textContent = 'The movie will be deleted from Radarr and the downloaded file will be permanently removed.';
    confirmBtn.textContent = 'Delete Downloaded';
  } else {
    titleEl.textContent = 'Keep Downloaded';
    msgEl.textContent = 'This will keep the Radarr download and remove the VOD stream.';
    warnEl.textContent = 'The VOD stream files (.strm and .nfo) will be permanently deleted.';
    confirmBtn.textContent = 'Delete VOD';
  }
  confirmBtn.onclick = () => { closeModal('modal-resolve-dup'); _executeResolveDup(id, resolution); };
  showModal('modal-resolve-dup');
}

async function _executeResolveDup(id, resolution) {
  try {
    await api(`/api/duplicates/${id}/resolve`, { method: 'POST', body: { resolution } });
    const labels = { keep_radarr: 'Kept downloaded', keep_vod: 'Kept VOD', keep_both: 'Kept both' };
    toast(labels[resolution] || resolution);
    loadDuplicates();
    _updateDupBadges();
  } catch (e) {
    toast(e.message, 'error');
  }
}

function resolveAllKeepRadarr() {
  const titleEl = document.getElementById('resolve-dup-title');
  const msgEl = document.getElementById('resolve-dup-message');
  const warnEl = document.getElementById('resolve-dup-warning');
  const confirmBtn = document.getElementById('resolve-dup-confirm');

  titleEl.textContent = 'Resolve All Duplicates';
  msgEl.textContent = 'This will resolve all pending duplicates by keeping the downloaded versions.';
  warnEl.textContent = 'All VOD stream files for duplicated content will be permanently deleted.';
  confirmBtn.textContent = 'Keep All Downloaded';
  confirmBtn.onclick = () => { closeModal('modal-resolve-dup'); _executeResolveAll(); };
  showModal('modal-resolve-dup');
}

async function _executeResolveAll() {
  try {
    const r = await api('/api/duplicates/resolve-all', { method: 'POST', body: { resolution: 'keep_radarr' } });
    toast(`Resolved ${r.count} duplicates`);
    loadDuplicates();
    _updateDupBadges();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function _updateFollowBadge() {
  try {
    const items = await api('/api/library/following');
    const badge = document.getElementById('lib-follow-badge');
    if (badge) {
      if (items.length > 0) { badge.style.display = 'inline'; badge.textContent = items.length; }
      else badge.style.display = 'none';
    }
  } catch (e) { /* silent */ }
}

async function _updateDupBadges() {
  try {
    const data = await api('/api/duplicates');
    const pending = (data.duplicates || []).filter(d => d.resolution === 'pending').length;
    for (const id of ['dup-badge', 'lib-dup-badge']) {
      const el = document.getElementById(id);
      if (el) {
        if (pending > 0) { el.style.display = 'inline'; el.textContent = pending; }
        else el.style.display = 'none';
      }
    }
    // Auto-refresh duplicates list if the tab is currently visible
    const dupTab = document.getElementById('lib-tab-duplicates');
    if (dupTab && dupTab.style.display !== 'none') {
      pages.dup.items = data.duplicates;
      renderDupStats(data);
      renderDupList();
    }
  } catch {}
}

// ── LOG VIEWER ────────────────────────────────────────────────────────────
let _logAutoScroll = true;
let _logEventSource = null;
let _logLineCount = 0;

function initLogViewer() {
  if (_logEventSource) _logEventSource.close();

  const body = document.getElementById('log-body');
  const dot = document.getElementById('log-conn-dot');

  _logEventSource = new EventSource('/api/radarr/logs/stream');
  _logEventSource.onopen = () => { if (dot) dot.className = 'dot dot-green'; };
  _logEventSource.onmessage = (e) => {
    try { appendLogLine(JSON.parse(e.data)); } catch (_) {}
  };
  _logEventSource.onerror = () => {
    if (dot) dot.className = 'dot dot-red';
    setTimeout(initLogViewer, 3000);
  };
}

function appendLogLine(entry) {
  const body = document.getElementById('log-body');
  if (!body) return;

  const placeholder = body.querySelector('[style*="Connecting"]');
  if (placeholder) placeholder.remove();

  const isHighlight = entry.msg.includes('+') && (entry.msg.includes('new') || entry.msg.includes('Series'));
  const isError = entry.color === 'error';
  const ts = entry.ts ? entry.ts.split('T')[1]?.split('.')[0] || '' : '';

  const line = document.createElement('div');
  line.className = 'log-line';
  line.innerHTML = `
    <span class="log-ts">${ts}</span>
    <span class="log-level ${entry.color}">${entry.level.substring(0,4)}</span>
    <span class="log-msg ${isHighlight ? 'highlight' : isError ? 'error' : ''}">${escapeHtml(entry.msg)}</span>`;

  body.appendChild(line);
  _logLineCount++;

  while (body.children.length > 300) body.removeChild(body.firstChild);
  if (_logAutoScroll) body.scrollTop = body.scrollHeight;
}

function escapeHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function clearLogPanel() {
  const body = document.getElementById('log-body');
  if (body) body.innerHTML = '';
  _logLineCount = 0;
}

function toggleLogScroll() {
  _logAutoScroll = !_logAutoScroll;
  const btn = document.getElementById('log-scroll-btn');
  if (btn) btn.textContent = _logAutoScroll ? '↓ Auto-scroll' : '○ Auto-scroll';
}

document.addEventListener('DOMContentLoaded', () => { setTimeout(initLogViewer, 500); });

// ── RADARR SCAN ───────────────────────────────────────────────────────────
async function scanRadarr() {
  const btn = document.getElementById('radarr-scan-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⟳ Scanning...'; }
  try {
    await api('/api/radarr/scan', { method: 'POST' });
    toast('Radarr scan started — watch logs for progress', 'info');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = '⬇ Scan Radarr'; } }, 3000);
  }
}

async function scanSonarr() {
  const btn = document.getElementById('sonarr-scan-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⟳ Scanning...'; }
  try {
    await api('/api/sonarr/scan', { method: 'POST' });
    toast('Sonarr scan started — watch logs for progress', 'info');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = '⬇ Scan Sonarr'; } }, 3000);
  }
}

async function writeNfos(btn) {
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⟳ Writing...';
  try {
    const r = await api('/api/radarr/write-nfos', { method: 'POST' });
    toast(`NFOs written: ${r.written}, skipped: ${r.skipped}`);
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

// ── PROVIDER MIGRATION ────────────────────────────────────────────────────
async function showMigrate() {
  const providers = state.providers || await api('/api/providers').catch(() => []);
  const opts = providers.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
  document.getElementById('migrate-from').innerHTML = opts;
  document.getElementById('migrate-to').innerHTML = opts;
  document.getElementById('migrate-preview').style.display = 'none';
  showModal('modal-migrate');
}

async function previewMigration() {
  const fromId = document.getElementById('migrate-from').value;
  const toId = document.getElementById('migrate-to').value;
  if (fromId === toId) { toast('Select different providers', 'error'); return; }
  try {
    const r = await api(`/api/radarr/migration/preview?from_id=${fromId}&to_id=${toId}`);
    const el = document.getElementById('migrate-preview');
    el.style.display = 'block';
    el.innerHTML = `From: ${r.from_provider} (${r.current_movies} movies)<br>To: ${r.to_provider}<br>${r.note ? `<span style="color:var(--amber)">${r.note}</span>` : ''}${r.error ? `<span style="color:var(--red)">Error: ${r.error}</span>` : ''}`;
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function runMigration(dryRun) {
  const fromId = parseInt(document.getElementById('migrate-from').value);
  const toId = parseInt(document.getElementById('migrate-to').value);
  if (fromId === toId) { toast('Select different providers', 'error'); return; }
  if (!dryRun && !confirm('This will rewrite .strm files. Continue?')) return;
  try {
    const r = await api('/api/radarr/migration/run', { method: 'POST', body: { from_provider_id: fromId, to_provider_id: toId, dry_run: dryRun } });
    toast(`Migration complete: ${r.movies_rewritten} movies rewritten, ${r.movies_not_found} not found`);
    closeModal('modal-migrate');
    loadProviders();
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── ACTIVITY (Downloads + Upcoming) ───────────────────────────────────────
let _activityPollTimer = null;
let _activityData = null;

function startActivityPolling() {
  stopActivityPolling();
  loadActivity();
  _activityPollTimer = setInterval(loadActivity, 3000);
}

function stopActivityPolling() {
  if (_activityPollTimer) {
    clearInterval(_activityPollTimer);
    _activityPollTimer = null;
  }
}

async function loadActivity() {
  try {
    const data = await api('/api/activity');
    _activityData = data;
    // Always update badge count
    const count = (data.downloads || []).length + (data.unreleased || []).length;
    const badge = document.getElementById('activity-tab-badge');
    if (badge) {
      badge.textContent = count;
      badge.classList.toggle('has-activity', count > 0);
    }
    // Only re-render if activity tab is visible
    const panel = document.getElementById('discover-tab-activity');
    if (panel && panel.style.display !== 'none') {
      renderActivity(data);
    }
  } catch (_) {}
}

function renderActivity(data) {
  const content = document.getElementById('activity-content');
  if (!content) return;
  if (!data) data = _activityData;
  if (!data) { content.innerHTML = '<div class="activity-empty">Loading…</div>'; return; }

  const downloads = data.downloads || [];
  const unreleased = data.unreleased || [];
  let html = '';

  if (downloads.length > 0) {
    html += '<div class="activity-section-title">Downloading</div><div class="activity-grid">';
    html += downloads.map(dl => {
      const poster = dl.poster_path
        ? `<img src="https://image.tmdb.org/t/p/w185${dl.poster_path}" loading="lazy" onerror="this.style.display='none'">`
        : '<div class="activity-poster-placeholder">◫</div>';
      const statusClass = 'dl-' + (dl.status || 'downloading');
      const statusLabel = dl.status === 'importing' ? 'Importing' :
        dl.status === 'queued' ? 'Queued' :
        dl.status === 'warning' ? 'Warning' : 'Downloading';
      const pct = Math.min(Math.max(dl.progress || 0, 0), 100);
      const epLabel = dl.episode ? ' · ' + escapeAttr(dl.episode) : '';
      const etaLabel = dl.eta ? ' · ' + escapeAttr(dl.eta) : '';
      const sizeLabel = dl.size_remaining ? escapeAttr(dl.size_remaining) + ' left' : '';
      const qualityLabel = dl.quality ? escapeAttr(dl.quality) : '';
      const metaParts = [qualityLabel, sizeLabel].filter(Boolean).join(' · ');
      return `<div class="activity-card">
        <div class="activity-poster">${poster}</div>
        <div class="activity-info">
          <div class="activity-title">${escapeAttr(dl.title)}${epLabel}</div>
          <div class="activity-meta">${dl.year || ''}${metaParts ? ' · ' + metaParts : ''}</div>
          <div class="activity-progress">
            <div class="activity-progress-bar"><div class="activity-progress-fill ${statusClass}" style="width:${pct}%"></div></div>
            <span class="activity-progress-label">${statusLabel} · ${pct.toFixed(1)}%${etaLabel}</span>
          </div>
        </div>
      </div>`;
    }).join('');
    html += '</div>';
  }

  if (unreleased.length > 0) {
    html += '<div class="activity-section-title">Upcoming Releases</div><div class="activity-grid">';
    html += unreleased.map(item => {
      const poster = item.poster_path
        ? `<img src="https://image.tmdb.org/t/p/w185${item.poster_path}" loading="lazy" onerror="this.style.display='none'">`
        : '<div class="activity-poster-placeholder">◫</div>';
      let daysUntil = '';
      if (item.release_date) {
        const rd = new Date(item.release_date + 'T00:00:00');
        const now = new Date();
        const diff = Math.ceil((rd - now) / 86400000);
        daysUntil = diff <= 0 ? 'Releasing soon' : diff === 1 ? 'Tomorrow' : diff + ' days';
      }
      return `<div class="activity-card">
        <div class="activity-poster">${poster}</div>
        <div class="activity-info">
          <div class="activity-title">${escapeAttr(item.title)}</div>
          <div class="activity-meta">${item.year || ''} · ${escapeAttr(item.release_date || '')}</div>
          ${daysUntil ? `<div class="activity-countdown">${escapeAttr(daysUntil)}</div>` : ''}
        </div>
      </div>`;
    }).join('');
    html += '</div>';
  }

  if (!html) {
    html = '<div class="activity-empty">No active downloads or upcoming releases</div>';
  }

  content.innerHTML = html;
}

// ── DISCOVER PAGE ────────────────────────────────────────────────────────
let _discoverType = 'movies';
let _discoverSections = [];
let _discoverActiveSection = null;

const DISCOVER_SECTION_LABELS = {
  popular: 'Popular',
  now_playing: 'Now Playing',
  upcoming: 'Upcoming',
  on_the_air: 'On the Air',
  top_rated: 'Top Rated',
  missing: 'From My Lists',
};

async function loadDiscover() {
  const grid = document.getElementById('discover-grid');
  const tabsEl = document.getElementById('discover-section-tabs');
  grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text3)"><span class="toast-spinner"></span> Loading…</div>';
  try {
    const data = await api(`/api/discover?type=${_discoverType}`);
    _discoverSections = data.sections || [];
    if (!_discoverSections.length) {
      tabsEl.innerHTML = '';
      grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>No content found. Check your TMDB bearer token in Settings.</p></div>';
      return;
    }
    // Render section tabs
    tabsEl.innerHTML = _discoverSections.map(sec => {
      const label = DISCOVER_SECTION_LABELS[sec.id] || sec.title;
      return `<button class="discover-sec-tab" data-section="${sec.id}" onclick="switchDiscoverSection('${sec.id}')" style="padding:10px 20px;font-size:13px;font-weight:500;border:none;background:transparent;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;font-family:'DM Sans',sans-serif;display:flex;align-items:center;gap:6px">${label}<span style="font-size:11px;background:var(--bg3);color:var(--text3);padding:1px 7px;border-radius:10px">${sec.items.length}</span></button>`;
    }).join('');
    // Activate first or previously active section
    const targetId = _discoverActiveSection && _discoverSections.find(s => s.id === _discoverActiveSection) ? _discoverActiveSection : _discoverSections[0].id;
    switchDiscoverSection(targetId);
  } catch (e) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>Failed to load discover: ${e.message}</p></div>`;
  }
}

function switchDiscoverSection(sectionId) {
  _discoverActiveSection = sectionId;
  document.querySelectorAll('.discover-sec-tab').forEach(btn => {
    const active = btn.getAttribute('data-section') === sectionId;
    btn.style.color = active ? 'var(--text)' : 'var(--text3)';
    btn.style.borderBottomColor = active ? 'var(--accent)' : 'transparent';
  });
  const section = _discoverSections.find(s => s.id === sectionId);
  if (section) renderDiscoverGrid(section.items);
}

function renderDiscoverGrid(items) {
  const grid = document.getElementById('discover-grid');
  if (!items.length) {
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>No content in this section</p></div>';
    return;
  }
  grid.innerHTML = items.map(item => {
    const poster = item.poster_path
      ? `<img src="https://image.tmdb.org/t/p/w185${item.poster_path}" loading="lazy" onerror="this.parentElement.innerHTML='<div class=\\'lib-card-poster-placeholder\\'>◫</div>'">`
      : `<div class="lib-card-poster-placeholder">◫</div>`;
    const badge = item.in_library
      ? `<span class="badge badge-green" style="font-size:9px;padding:1px 5px">In Library</span>`
      : `<span class="badge" style="font-size:9px;padding:1px 5px;background:var(--bg3);color:var(--text3)">${item.media_type === 'movie' ? 'Movie' : 'Show'}</span>`;
    const addBtn = item.in_library ? '' : `<button onclick="event.stopPropagation();showAddToArrModal(${item.tmdb_id},'${escapeJS(item.title)}','${escapeJS(item.year||'')}','${escapeJS(item.poster_path||'')}','${item.media_type}')" class="lib-card-add-btn" title="Add to ${item.media_type === 'series' ? 'Sonarr' : 'Radarr'}">+</button>`;
    const clickHandler = `onclick="showDiscoverDetail(${item.tmdb_id},'${escapeAttr(item.media_type)}','${escapeJS(item.title)}','${escapeJS(item.year||'')}','${escapeJS(item.poster_path||'')}',${!!item.in_library})"`;
    const listTag = item.list_name ? `<div style="font-size:10px;color:var(--accent);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeAttr(item.list_name)}</div>` : '';
    return `
      <div class="lib-card" ${clickHandler}>
        <div class="lib-card-poster">
          ${poster}
          <div class="lib-card-source">${badge}</div>
          ${addBtn}
        </div>
        <div class="lib-card-info">
          <div class="lib-card-title" title="${escapeAttr(item.title)}">${item.title}</div>
          <div class="lib-card-meta">${item.year || '—'} · ★ ${item.rating || '—'}</div>
          ${listTag}
        </div>
      </div>`;
  }).join('');
}

async function showDiscoverDetail(tmdbId, mediaType, title, year, posterPath, inLibrary) {
  showModal('modal-media-detail');
  document.getElementById('detail-title').textContent = 'Loading...';
  document.getElementById('detail-body').innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';

  try {
    const data = await api(`/api/discover/detail/${mediaType}/${tmdbId}`);
    const isSeries = mediaType === 'series';
    const arrLabel = isSeries ? 'Sonarr' : 'Radarr';
    // Use detail response in_library (authoritative) over card-level flag
    const isInLibrary = data.in_library !== undefined ? data.in_library : inLibrary;
    let actionBtn;
    if (isInLibrary && isSeries && data.library_source === 'sonarr') {
      actionBtn = `<span class="badge badge-green" style="font-size:12px;padding:4px 10px">In Library</span> <button class="btn btn-secondary btn-sm" style="margin-left:6px" onclick="closeModal('modal-media-detail');showManageEpisodesModal(${tmdbId},'${escapeJS(data.title||title||'')}','${escapeJS(data.year||year||'')}','${escapeJS(data.poster_path||posterPath||'')}')">Manage Episodes</button>`;
    } else if (isInLibrary && isSeries && data.library_source && data.library_source.startsWith('provider_')) {
      actionBtn = `<span class="badge badge-green" style="font-size:12px;padding:4px 10px">In Library</span> <button class="btn btn-primary btn-sm" style="margin-left:6px" onclick="closeModal('modal-media-detail');showDownloadMoreModal(${tmdbId},'${escapeJS(data.title||title||'')}','${escapeJS(data.year||year||'')}','${escapeJS(data.poster_path||posterPath||'')}')">Download More Episodes</button>`;
    } else if (isInLibrary) {
      actionBtn = `<span class="badge badge-green" style="font-size:12px;padding:4px 10px">In Library</span>`;
    } else {
      actionBtn = `<button class="btn btn-primary btn-sm" onclick="closeModal('modal-media-detail');showAddToArrModal(${tmdbId},'${escapeJS(data.title||title||'')}','${escapeJS(data.year||year||'')}','${escapeJS(data.poster_path||posterPath||'')}','${mediaType}')">Add to ${arrLabel}</button>`;
    }
    document.getElementById('detail-title').textContent = data.title || title || 'Unknown';
    document.getElementById('detail-body').innerHTML = `
      <div style="display:flex;gap:20px">
        ${data.poster_path ? `<img src="https://image.tmdb.org/t/p/w185${data.poster_path}" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
        <div style="flex:1">
          <div style="font-size:13px;color:var(--text2);margin-bottom:12px">${data.year || year || '—'} · ${data.runtime ? data.runtime+'m · ' : ''}★ ${data.rating || '—'}</div>
          <p style="font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:16px">${data.overview || 'No overview available.'}</p>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
            ${(data.genres||[]).map(g => `<span class="badge badge-gray">${g}</span>`).join('')}
          </div>
          <div style="margin-top:8px">
            ${actionBtn}
          </div>
        </div>
      </div>`;
  } catch {
    document.getElementById('detail-title').textContent = title || 'Unknown';
    document.getElementById('detail-body').innerHTML = `
      <div style="display:flex;gap:20px">
        ${posterPath ? `<img src="https://image.tmdb.org/t/p/w185${posterPath}" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
        <div style="flex:1">
          <div style="font-size:13px;color:var(--text2);margin-bottom:12px">${year || '—'}</div>
          <p style="font-size:13px;color:var(--text2)">Could not load details.</p>
        </div>
      </div>`;
  }
}

function setDiscoverType(type, btn) {
  _discoverType = type;
  _discoverActiveSection = null; // Reset — tabs change between Movies/TV
  document.querySelectorAll('#discover-tab-browse .filter-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (_discoverSearchQuery) {
    doDiscoverSearch(_discoverSearchQuery);
  } else {
    loadDiscover();
  }
}

let _discoverSearchQuery = '';
let _discoverSearchTimeout = null;

function onDiscoverSearchInput(input) {
  const q = input.value.trim();
  const clearBtn = document.getElementById('discover-search-clear');
  if (clearBtn) clearBtn.style.display = q ? 'block' : 'none';
  if (_discoverSearchTimeout) clearTimeout(_discoverSearchTimeout);
  if (!q) {
    _discoverSearchQuery = '';
    loadDiscover();
    return;
  }
  _discoverSearchTimeout = setTimeout(() => {
    _discoverSearchQuery = q;
    doDiscoverSearch(q);
  }, 400);
}

function clearDiscoverSearch() {
  const input = document.getElementById('discover-search-input');
  if (input) input.value = '';
  const clearBtn = document.getElementById('discover-search-clear');
  if (clearBtn) clearBtn.style.display = 'none';
  _discoverSearchQuery = '';
  loadDiscover();
}

async function doDiscoverSearch(query) {
  const grid = document.getElementById('discover-grid');
  const tabsEl = document.getElementById('discover-section-tabs');
  if (tabsEl) tabsEl.innerHTML = '';
  grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text3)"><span class="toast-spinner"></span> Searching…</div>';
  try {
    const data = await api(`/api/discover/search?q=${encodeURIComponent(query)}&type=${_discoverType}`);
    const items = data.items || [];
    if (!items.length) {
      grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>No results for "${query}"</p></div>`;
      return;
    }
    renderDiscoverGrid(items);
  } catch (e) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>Search failed: ${e.message}</p></div>`;
  }
}

// ══════════════════════════════════════════════════════════════════════════
// ── LIVE TV PAGE ─────────────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

const liveState = {
  providerId: null,
  groups: [],
  chPage: 1,
  chPerPage: 100,
  chSearch: '',
  chGroupFilter: '',
  chEpgFilter: '',
  syncPollTimer: null,
};

async function loadLiveTV() {
  const noProvEl = document.getElementById('live-no-provider');
  const statsEl = document.getElementById('live-stats');
  const tabsEl = document.querySelector('#page-live-tv .live-tabs');
  const panels = document.querySelectorAll('#page-live-tv .live-tab-panel');

  try {
    const provData = await api('/api/live/provider');
    if (provData.provider) {
      liveState.providerId = provData.provider.id;
      if (noProvEl) noProvEl.style.display = 'none';
      if (statsEl) statsEl.style.display = 'grid';
      if (tabsEl) tabsEl.style.display = 'flex';
      panels.forEach(p => p.style.display = '');

      const data = await api('/api/live/status');
      renderLiveStats(data);
      await loadLiveGroups();
      await loadLiveChannels();
    } else {
      liveState.providerId = null;
      if (noProvEl) noProvEl.style.display = '';
      if (statsEl) statsEl.style.display = 'none';
      if (tabsEl) tabsEl.style.display = 'none';
      panels.forEach(p => p.style.display = 'none');
    }
  } catch (e) {
    if (statsEl) statsEl.innerHTML =
      `<div style="grid-column:1/-1;color:var(--red);font-size:13px">Failed to load: ${e.message}</div>`;
  }
}

// ── Tab switching ─────────────────────────────────────────────────────────

function showLiveTab(name) {
  const page = document.getElementById('page-live-tv');
  page.querySelectorAll('[data-livetab]').forEach(t => t.classList.remove('active'));
  page.querySelectorAll('.live-tab-panel').forEach(p => p.classList.remove('active'));
  page.querySelector(`[data-livetab="${name}"]`)?.classList.add('active');
  document.getElementById(`live-panel-${name}`)?.classList.add('active');

  if (name === 'groups') loadLiveGroups();
  if (name === 'channels') loadLiveChannels();
  if (name === 'setup') fillSetupUrls();
}

async function fillSetupUrls() {
  try {
    const settings = await api('/api/settings/raw');
    const savedHost = settings['live_setup_host'];
    const savedPort = settings['live_setup_port'];
    if (savedHost) {
      document.getElementById('live-setup-host').value = savedHost;
      document.getElementById('live-setup-port').value = savedPort || '8888';
      showSetupLocked();
    }
  } catch (e) {}
  updateSetupUrls();
}

async function saveSetupAddress() {
  const host = document.getElementById('live-setup-host').value.trim();
  const port = document.getElementById('live-setup-port').value.trim() || '8888';
  if (!host) { toast('Enter the server IP address', 'error'); return; }
  try {
    await api('/api/settings', { method: 'POST', body: { settings: { live_setup_host: host, live_setup_port: port } } });
    showSetupLocked();
    toast('Server address saved');
  } catch (e) { toast(e.message, 'error'); }
}

function editSetupAddress() {
  document.getElementById('live-setup-edit').style.display = 'flex';
  document.getElementById('live-setup-locked').style.display = 'none';
}

function showSetupLocked() {
  const base = getSetupBase();
  document.getElementById('live-setup-address-display').textContent = base;
  document.getElementById('live-setup-edit').style.display = 'none';
  document.getElementById('live-setup-locked').style.display = 'flex';
  updateSetupUrls();
}

function getSetupBase() {
  const host = document.getElementById('live-setup-host').value || 'localhost';
  const port = document.getElementById('live-setup-port').value || '8888';
  return `http://${host}:${port}`;
}

function updateSetupUrls() {
  const base = getSetupBase();
  document.getElementById('live-setup-tuner-preview').textContent = base;
  document.getElementById('live-setup-xmltv-preview').textContent = `${base}/hdhr/xmltv.xml`;
}

function copyLiveSetup(type, btn) {
  const base = getSetupBase();
  const url = type === 'tuner' ? base : `${base}/hdhr/xmltv.xml`;
  navigator.clipboard.writeText(url).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = orig, 1500);
  });
}

// ── Provider form ─────────────────────────────────────────────────────────

function fillProviderForm(p) {
  document.getElementById('live-provider-type').value = p.provider_type || 'xtream';
  document.getElementById('live-server-url').value = p.server_url || '';
  document.getElementById('live-username').value = p.username || '';
  document.getElementById('live-password').value = p.password || '';
  document.getElementById('live-m3u-url').value = p.m3u_url || '';
  document.getElementById('live-epg-url').value = p.epg_url || '';
  document.getElementById('live-user-agent').value = p.user_agent || '';
  onLiveTypeChange();
}

function onLiveTypeChange() {
  const type = document.getElementById('live-provider-type').value;
  document.getElementById('live-xtream-fields').style.display = type === 'xtream' ? '' : 'none';
  document.getElementById('live-m3u-fields').style.display = type !== 'xtream' ? '' : 'none';
}

async function saveLiveProvider() {
  const type = document.getElementById('live-provider-type').value;
  const body = {
    provider_type: type,
    server_url: document.getElementById('live-server-url').value,
    username: document.getElementById('live-username').value,
    password: document.getElementById('live-password').value,
    m3u_url: document.getElementById('live-m3u-url').value,
    epg_url: document.getElementById('live-epg-url').value,
    user_agent: document.getElementById('live-user-agent').value,
    live_tv_enabled: true,
  };

  try {
    const res = await api('/api/live/provider', { method: 'POST', body });
    liveState.providerId = res.provider_id;
    toast('Provider saved', 'success');
  } catch (e) {
    toast(`Save failed: ${e.message}`, 'error');
  }
}

async function testLiveProvider() {
  const el = document.getElementById('live-test-result');
  el.innerHTML = '<span style="color:var(--amber)">Testing...</span>';
  try {
    const res = await api('/api/live/provider/test', { method: 'POST' });
    if (res.success) {
      let info = '';
      if (res.info) {
        info = ` — ${res.info.status || ''}, max ${res.info.max_connections || '?'} connections`;
      }
      el.innerHTML = `<span style="color:var(--green)">Connected${info}</span>`;
    } else {
      el.innerHTML = `<span style="color:var(--red)">${res.message}</span>`;
    }
  } catch (e) {
    el.innerHTML = `<span style="color:var(--red)">${e.message}</span>`;
  }
}

function renderLiveStats(data) {
  const el = document.getElementById('live-stats');
  el.innerHTML = `
    <div class="stat-card" style="border-top:3px solid var(--accent)">
      <div style="font-size:11px;color:var(--text3);text-transform:uppercase;margin-bottom:4px">Groups</div>
      <div style="font-size:24px;font-weight:600">${data.enabled_groups || 0} / ${data.total_groups || 0}</div>
    </div>
    <div class="stat-card" style="border-top:3px solid var(--blue)">
      <div style="font-size:11px;color:var(--text3);text-transform:uppercase;margin-bottom:4px">Channels</div>
      <div style="font-size:24px;font-weight:600">${data.total_channels || 0}</div>
    </div>
    <div class="stat-card" style="border-top:3px solid var(--green)">
      <div style="font-size:11px;color:var(--text3);text-transform:uppercase;margin-bottom:4px">Enabled</div>
      <div style="font-size:24px;font-weight:600">${data.enabled_channels || 0}</div>
    </div>
    <div class="stat-card" style="border-top:3px solid var(--amber)">
      <div style="font-size:11px;color:var(--text3);text-transform:uppercase;margin-bottom:4px">EPG Programs</div>
      <div style="font-size:24px;font-weight:600">${data.epg_programs || 0}</div>
    </div>`;
}

async function refreshLiveStats() {
  try {
    const data = await api('/api/live/status');
    renderLiveStats(data);
  } catch (e) {}
}

// ── Groups ────────────────────────────────────────────────────────────────

async function loadLiveGroups() {
  if (!liveState.providerId) return;
  try {
    const data = await api(`/api/live/groups?provider_id=${liveState.providerId}`);
    liveState.groups = data.groups || [];
    renderLiveGroups(liveState.groups);
    populateGroupFilter(liveState.groups);
  } catch (e) {
    renderLiveGroups([]);
  }
}

function renderLiveGroups(groups) {
  const emptyEl = document.getElementById('live-groups-empty');
  const loadedEl = document.getElementById('live-groups-loaded');
  const el = document.getElementById('live-groups');

  if (!groups.length) {
    if (emptyEl) emptyEl.style.display = '';
    if (loadedEl) loadedEl.style.display = 'none';
    return;
  }

  if (emptyEl) emptyEl.style.display = 'none';
  if (loadedEl) loadedEl.style.display = '';

  el.innerHTML = groups.map((g, i) => `
    <div class="live-group-row" data-group-id="${g.id}" data-group-idx="${i}" data-group-name="${(g.name || '').toLowerCase()}">
      <span class="group-name">${g.name}</span>
      <span class="group-count">${g.channel_count || 0} ch</span>
      <button class="live-toggle ${g.enabled ? 'on' : ''}" onclick="toggleLiveGroup(${g.id}, this, event)"></button>
    </div>`).join('');
  liveState.lastToggledIdx = null;
  updateGroupsSummary(groups);
}

function updateGroupsSummary(groups) {
  const el = document.getElementById('live-groups-summary');
  if (!el) return;
  const enabled = groups.filter(g => g.enabled).length;
  el.textContent = `— ${enabled} of ${groups.length} enabled`;
}

function filterLiveGroups() {
  const q = (document.getElementById('live-group-search')?.value || '').toLowerCase();
  document.querySelectorAll('#live-groups .live-group-row').forEach(row => {
    const name = row.dataset.groupName || '';
    row.style.display = name.includes(q) ? '' : 'none';
  });
}

function toggleLiveGroup(groupId, btn, evt) {
  const row = btn.closest('.live-group-row');
  const idx = parseInt(row.dataset.groupIdx);
  const isOn = btn.classList.contains('on');
  const enabled = !isOn;

  // Shift-click: range toggle
  if (evt && evt.shiftKey && liveState.lastToggledIdx != null) {
    const from = Math.min(liveState.lastToggledIdx, idx);
    const to = Math.max(liveState.lastToggledIdx, idx);
    liveState.groups.slice(from, to + 1).forEach(g => g.enabled = enabled);
    renderLiveGroups(liveState.groups);
    filterLiveGroups();
    liveState.lastToggledIdx = idx;
    return;
  }

  liveState.lastToggledIdx = idx;
  const g = liveState.groups.find(g => g.id === groupId);
  if (g) g.enabled = enabled;
  if (enabled) btn.classList.add('on'); else btn.classList.remove('on');
  updateGroupsSummary(liveState.groups);
}

function toggleAllGroups(enabled) {
  const q = (document.getElementById('live-group-search')?.value || '').toLowerCase();
  const visible = liveState.groups.filter(g => (g.name || '').toLowerCase().includes(q));
  visible.forEach(g => g.enabled = enabled);
  renderLiveGroups(liveState.groups);
  filterLiveGroups();
}

async function saveLiveGroups() {
  if (!liveState.providerId) return;
  const enabledIds = liveState.groups.filter(g => g.enabled).map(g => g.id);
  const disabledIds = liveState.groups.filter(g => !g.enabled).map(g => g.id);
  toast('Saving groups...', 'info');
  try {
    if (enabledIds.length) await api('/api/live/groups/bulk', { method: 'PUT', body: { group_ids: enabledIds, enabled: true } });
    if (disabledIds.length) await api('/api/live/groups/bulk', { method: 'PUT', body: { group_ids: disabledIds, enabled: false } });
    toast('Groups saved — syncing channels...', 'info');
    liveSyncChannels();
  } catch (e) {
    toast(`Failed: ${e.message}`, 'error');
  }
}

function populateGroupFilter(groups) {
  const sel = document.getElementById('live-ch-group-filter');
  if (!sel) return;
  const enabledGroups = groups.filter(g => g.enabled).sort((a, b) => a.name.localeCompare(b.name));
  sel.innerHTML = '<option value="">All Groups</option>' +
    enabledGroups.map(g => `<option value="${g.name}">${g.name} (${g.channel_count || 0})</option>`).join('');
}

// ── Channels ──────────────────────────────────────────────────────────────

async function loadLiveChannels() {
  if (!liveState.providerId) return;
  const params = new URLSearchParams({
    provider_id: liveState.providerId,
    page: liveState.chPage,
    per_page: liveState.chPerPage,
  });
  if (liveState.chSearch) params.set('search', liveState.chSearch);
  if (liveState.chGroupFilter) params.set('group', liveState.chGroupFilter);
  if (liveState.chEpgFilter) params.set('has_epg', liveState.chEpgFilter === 'has_epg' ? 'true' : 'false');

  try {
    const data = await api(`/api/live/channels?${params}`);
    renderLiveChannels(data.channels, data.total);
    renderChPagination(data.total, data.page, data.per_page);
    // Show EPG status
    const status = await api('/api/live/status');
    const epgEl = document.getElementById('live-epg-status');
    if (epgEl && !epgEl.innerHTML.includes('Downloading')) {
      epgEl.textContent = status.epg_programs
        ? `${status.epg_programs} guide programs loaded`
        : 'No guide data — click Sync to download';
    }
  } catch (e) {
    document.getElementById('live-channels').innerHTML =
      `<div style="padding:20px;color:var(--text3);font-size:13px">No channels. Enable groups and click "Sync Channels".</div>`;
  }
}

function renderLiveChannels(channels, total) {
  const el = document.getElementById('live-channels');
  document.getElementById('live-ch-count').textContent = total ? `(${total})` : '';

  if (!channels || !channels.length) {
    el.innerHTML = `<div style="padding:20px;color:var(--text3);font-size:13px">No channels. Enable groups and click "Sync Channels".</div>`;
    return;
  }

  liveState.pageChannels = channels;
  liveState.lastToggledChIdx = null;
  liveState.dirtyChannels = {};

  el.innerHTML = channels.map((ch, i) => `
    <div class="live-ch-row" data-ch-idx="${i}">
      ${ch.logo_url ? `<img class="live-ch-logo" src="${ch.logo_url}" loading="lazy" onerror="this.style.display='none'">` : `<div class="live-ch-logo"></div>`}
      <span class="live-ch-name">${ch.name}</span>
      <span class="live-ch-group">${ch.group_title || ''}</span>
      <span class="live-ch-epg-badge ${ch.has_epg_data ? 'has-epg' : 'no-epg'}">${ch.has_epg_data ? 'Has EPG' : 'No EPG'}</span>
      <button class="live-toggle ${ch.enabled ? 'on' : ''}" onclick="toggleLiveChannel(${i}, this, event)" style="flex-shrink:0"></button>
    </div>`).join('');
}

function renderChPagination(total, page, perPage) {
  const el = document.getElementById('live-ch-pagination');
  const totalPages = Math.ceil(total / perPage);
  if (totalPages <= 1) { el.innerHTML = ''; return; }

  let html = '';
  if (page > 1) html += `<button class="btn btn-secondary btn-sm" onclick="liveChPage(${page - 1})">← Prev</button>`;
  html += `<span style="font-size:12px;color:var(--text3);align-self:center">Page ${page} of ${totalPages}</span>`;
  if (page < totalPages) html += `<button class="btn btn-secondary btn-sm" onclick="liveChPage(${page + 1})">Next →</button>`;
  el.innerHTML = html;
}

function liveChPage(page) {
  liveState.chPage = page;
  loadLiveChannels();
}

function searchLiveChannels() {
  liveState.chSearch = document.getElementById('live-ch-search')?.value || '';
  liveState.chPage = 1;
  loadLiveChannels();
}

function filterLiveChannels() {
  liveState.chGroupFilter = document.getElementById('live-ch-group-filter')?.value || '';
  liveState.chPage = 1;
  loadLiveChannels();
}

function filterLiveChannelsByEpg() {
  liveState.chEpgFilter = document.getElementById('live-ch-epg-filter')?.value || '';
  liveState.chPage = 1;
  loadLiveChannels();
}

function toggleLiveChannel(idx, btn, evt) {
  const ch = liveState.pageChannels[idx];
  if (!ch) return;
  const isOn = btn.classList.contains('on');
  const enabled = !isOn;

  // Shift-click: range toggle
  if (evt && evt.shiftKey && liveState.lastToggledChIdx != null) {
    const from = Math.min(liveState.lastToggledChIdx, idx);
    const to = Math.max(liveState.lastToggledChIdx, idx);
    for (let i = from; i <= to; i++) {
      const c = liveState.pageChannels[i];
      c.enabled = enabled;
      liveState.dirtyChannels[c.id] = enabled;
      const row = document.querySelector(`.live-ch-row[data-ch-idx="${i}"] .live-toggle`);
      if (row) { if (enabled) row.classList.add('on'); else row.classList.remove('on'); }
    }
    liveState.lastToggledChIdx = idx;
    return;
  }

  liveState.lastToggledChIdx = idx;
  ch.enabled = enabled;
  liveState.dirtyChannels[ch.id] = enabled;
  if (enabled) btn.classList.add('on'); else btn.classList.remove('on');
}

function toggleAllChannels(enabled) {
  if (!liveState.pageChannels || !liveState.pageChannels.length) return;
  liveState.pageChannels.forEach((ch, i) => {
    ch.enabled = enabled;
    liveState.dirtyChannels[ch.id] = enabled;
    const row = document.querySelector(`.live-ch-row[data-ch-idx="${i}"] .live-toggle`);
    if (row) { if (enabled) row.classList.add('on'); else row.classList.remove('on'); }
  });
}

async function saveLiveChannels() {
  const dirty = liveState.dirtyChannels || {};
  const enableIds = Object.entries(dirty).filter(([, v]) => v).map(([k]) => parseInt(k));
  const disableIds = Object.entries(dirty).filter(([, v]) => !v).map(([k]) => parseInt(k));
  if (!enableIds.length && !disableIds.length) { toast('No changes to save'); return; }
  toast('Saving channels...', 'info');
  try {
    if (enableIds.length) await api('/api/live/channels/bulk', { method: 'POST', body: { channel_ids: enableIds, enabled: true } });
    if (disableIds.length) await api('/api/live/channels/bulk', { method: 'POST', body: { channel_ids: disableIds, enabled: false } });
    liveState.dirtyChannels = {};
    toast(`Saved ${enableIds.length + disableIds.length} channel changes`);
  } catch (e) {
    toast(`Failed: ${e.message}`, 'error');
  }
}

// ── Sync actions ──────────────────────────────────────────────────────────

async function liveSyncGroups() {
  if (!liveState.providerId) { toast('No provider configured', 'error'); return; }
  try {
    const res = await api(`/api/live/sync/${liveState.providerId}`, { method: 'POST' });
    toast(res.message || 'Sync started', 'info');
    startSyncPoll();
  } catch (e) {
    toast(`Sync failed: ${e.message}`, 'error');
  }
}

async function liveSyncChannels() {
  if (!liveState.providerId) { toast('No provider configured', 'error'); return; }
  try {
    const res = await api(`/api/live/sync-channels/${liveState.providerId}`, { method: 'POST' });
    if (!res.success) { toast(res.message, 'error'); return; }
    toast(res.message || 'Channel sync started', 'info');
    startSyncPoll();
  } catch (e) {
    toast(`Sync failed: ${e.message}`, 'error');
  }
}

async function liveSyncEpg() {
  if (!liveState.providerId) { toast('No provider configured', 'error'); return; }
  try {
    const res = await api(`/api/live/sync-epg/${liveState.providerId}`, { method: 'POST' });
    toast(res.message || 'EPG sync started', 'info');
    startEpgPoll();
  } catch (e) {
    toast(`EPG sync failed: ${e.message}`, 'error');
  }
}

function startEpgPoll() {
  if (liveState.epgPollTimer) clearInterval(liveState.epgPollTimer);
  const statusEl = document.getElementById('live-epg-status');

  liveState.epgPollTimer = setInterval(async () => {
    try {
      const s = await api(`/api/live/sync-status?provider_id=${liveState.providerId}`);
      if (s.phase !== 'epg') return; // different sync running
      statusEl.innerHTML = `<span style="color:var(--amber)">${s.message || 'Syncing...'}</span>` +
        (s.progress != null ? ` <div class="live-progress" style="width:120px;display:inline-block;vertical-align:middle;margin-left:6px"><div class="live-progress-bar" style="width:${s.progress}%"></div></div>` : '');

      if (s.status === 'complete') {
        clearInterval(liveState.epgPollTimer);
        statusEl.innerHTML = `<span style="color:var(--green)">${s.message}</span>`;
        toast('EPG sync complete', 'success');
        refreshLiveStats();
        // Auto-refresh Jellyfin guide if server address is configured
        try {
          const settings = await api('/api/settings/raw');
          if (settings['live_setup_host']) {
            api('/api/live/refresh-guide', { method: 'POST' }).then(() => {
              toast('Jellyfin guide refresh triggered', 'info');
            }).catch(() => {});
          }
        } catch (e) {}
      } else if (s.status === 'error') {
        clearInterval(liveState.epgPollTimer);
        statusEl.innerHTML = `<span style="color:var(--red)">${s.message}</span>`;
        toast('EPG sync failed', 'error');
      }
    } catch (e) {
      clearInterval(liveState.epgPollTimer);
    }
  }, 2000);
}

function startSyncPoll() {
  if (liveState.syncPollTimer) clearInterval(liveState.syncPollTimer);
  const statusEl = document.getElementById('live-sync-status');

  liveState.syncPollTimer = setInterval(async () => {
    try {
      const s = await api(`/api/live/sync-status?provider_id=${liveState.providerId}`);
      statusEl.innerHTML = `${s.message || ''} ${s.progress != null ? `<div class="live-progress" style="width:120px;display:inline-block;vertical-align:middle;margin-left:6px"><div class="live-progress-bar" style="width:${s.progress}%"></div></div>` : ''}`;

      // Channel sync chains into EPG sync automatically — handle EPG completion here too
      if (s.phase === 'epg' && s.status === 'complete') {
        clearInterval(liveState.syncPollTimer);
        liveState.syncPollTimer = null;
        toast(s.message, 'success');
        setTimeout(() => { statusEl.textContent = ''; }, 3000);
        refreshLiveStats();
        loadLiveTV();
        // Auto-refresh Jellyfin guide if server address is configured
        try {
          const settings = await api('/api/settings/raw');
          if (settings['live_setup_host']) {
            api('/api/live/refresh-guide', { method: 'POST' }).then(() => {
              toast('Jellyfin guide refresh triggered', 'info');
            }).catch(() => {});
          }
        } catch (e) {}
      } else if (s.phase === 'epg' && s.status === 'error') {
        clearInterval(liveState.syncPollTimer);
        liveState.syncPollTimer = null;
        toast('EPG sync failed: ' + s.message, 'error');
        setTimeout(() => { statusEl.textContent = ''; }, 5000);
        loadLiveTV();
      } else if (s.phase === 'complete') {
        clearInterval(liveState.syncPollTimer);
        liveState.syncPollTimer = null;
        toast(s.message, 'success');
        setTimeout(() => { statusEl.textContent = ''; }, 3000);
        loadLiveTV();
      } else if (s.phase === 'error') {
        clearInterval(liveState.syncPollTimer);
        liveState.syncPollTimer = null;
        toast(s.message, 'error');
        setTimeout(() => { statusEl.textContent = ''; }, 5000);
      }
    } catch (e) {
      clearInterval(liveState.syncPollTimer);
      liveState.syncPollTimer = null;
      statusEl.textContent = '';
    }
  }, 1500);
}

// ══════════════════════════════════════════════════════════════════════════
// ── VOD PAGE ─────────────────────────────────────────────────────────────

const vodState = {
  providerId: null,
  categories: [],
  catFilter: 'all',
};

async function loadVodPage() {
  const select = document.getElementById('vod-provider-select');
  const noProvEl = document.getElementById('vod-no-provider');
  const provBar = document.getElementById('vod-provider-bar');
  const tabsBar = document.getElementById('vod-tabs-bar');

  try {
    const providers = await api('/api/providers');
    const vodProviders = providers.filter(p => p.has_vod || p.has_series || p.active);

    const catTab = document.getElementById('vod-tab-categories');
    const syncTab = document.getElementById('vod-tab-sync');

    if (!vodProviders.length) {
      if (noProvEl) noProvEl.style.display = '';
      if (provBar) provBar.style.display = 'none';
      if (tabsBar) tabsBar.style.display = 'none';
      if (catTab) catTab.style.display = 'none';
      if (syncTab) syncTab.style.display = 'none';
      return;
    }

    if (noProvEl) noProvEl.style.display = 'none';
    if (provBar) provBar.style.display = 'flex';
    if (tabsBar) tabsBar.style.display = 'flex';
    if (catTab) catTab.style.display = '';

    select.innerHTML = vodProviders.map(p =>
      `<option value="${p.id}">${escapeAttr(p.name)} ${p.active ? '(Active)' : ''}</option>`
    ).join('');

    // Select first active, or first available
    const active = vodProviders.find(p => p.active) || vodProviders[0];
    select.value = active.id;
    vodState.providerId = active.id;

    const statusEl = document.getElementById('vod-provider-status');
    if (statusEl) {
      const badges = [];
      if (active.has_vod) badges.push('Movies');
      if (active.has_series) badges.push('Series');
      statusEl.textContent = badges.length ? badges.join(' + ') : '';
    }

    await loadVodCategories();
  } catch (e) {
    if (noProvEl) noProvEl.style.display = '';
  }
}

function onVodProviderChange() {
  const select = document.getElementById('vod-provider-select');
  vodState.providerId = parseInt(select.value);
  loadVodCategories();
}

function showVodTab(tab) {
  const page = document.getElementById('page-vod');
  page.querySelectorAll('[data-vodtab]').forEach(t => t.classList.remove('active'));
  page.querySelector(`[data-vodtab="${tab}"]`)?.classList.add('active');
  document.getElementById('vod-tab-categories').style.display = tab === 'categories' ? '' : 'none';
  document.getElementById('vod-tab-sync').style.display = tab === 'sync' ? '' : 'none';
}

async function loadVodCategories() {
  if (!vodState.providerId) return;
  const listEl = document.getElementById('vod-cat-list');
  listEl.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';

  try {
    let cats = await api(`/api/providers/${vodState.providerId}/categories`);
    // If no categories yet (newly added provider), auto-fetch from provider
    if (!cats || !cats.length) {
      listEl.innerHTML = '<div class="loading-state"><div class="spinner"></div><p style="margin-top:8px;color:var(--text-muted)">Fetching categories from provider...</p></div>';
      await api(`/api/providers/${vodState.providerId}/fetch-categories`, { method: 'POST' });
      cats = await api(`/api/providers/${vodState.providerId}/categories`);
    }
    vodState.categories = cats;
    renderVodCats();
  } catch (e) {
    listEl.innerHTML = '<div class="empty-state"><p>No categories yet. Click "Refresh from Provider" to fetch.</p></div>';
  }
}

async function vodFetchCategories() {
  if (!vodState.providerId) return;
  toast('Fetching categories from provider...', 'info');
  const listEl = document.getElementById('vod-cat-list');
  listEl.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';

  try {
    await api(`/api/providers/${vodState.providerId}/fetch-categories`, { method: 'POST' });
    const cats = await api(`/api/providers/${vodState.providerId}/categories`);
    vodState.categories = cats;
    renderVodCats();
    toast(`Loaded ${cats.length} categories`);
  } catch (e) {
    toast(e.message, 'error');
    listEl.innerHTML = '<div class="empty-state"><p>Failed to fetch categories</p></div>';
  }
}

function renderVodCats() {
  const search = (document.getElementById('vod-cat-search')?.value || '').toLowerCase();
  const filter = vodState.catFilter;

  let cats = vodState.categories.filter(c => {
    if (search && !c.name.toLowerCase().includes(search)) return false;
    if (filter === 'movie') return c.type === 'movie';
    if (filter === 'series') return c.type === 'series';
    if (filter === 'active') return c.whitelisted;
    if (filter === 'english') return c.is_likely_english && !c.is_foreign;
    return true;
  });

  const countEl = document.getElementById('vod-cat-count');
  if (countEl) countEl.textContent = `— ${cats.length} of ${vodState.categories.length} shown, ${vodState.categories.filter(c=>c.whitelisted).length} active`;

  const listEl = document.getElementById('vod-cat-list');
  if (!cats.length) {
    listEl.innerHTML = '<div class="empty-state"><p>No categories match filter</p></div>';
    return;
  }

  listEl.innerHTML = cats.map(c => {
    let syncInfo = '';
    if (c.last_sync_matched != null || c.last_sync_skipped != null) {
      const matched = c.last_sync_matched || 0;
      const skipped = c.last_sync_skipped || 0;
      if (skipped && !matched) {
        syncInfo = `<span style="font-size:10px;color:var(--red);font-family:'DM Mono',monospace" title="${skipped} items had no TMDB match">0 matched</span>`;
      } else if (matched) {
        syncInfo = `<span style="font-size:10px;color:var(--green);font-family:'DM Mono',monospace" title="${matched} matched, ${skipped} skipped">${matched} matched</span>`;
      }
    }
    return `
    <div class="cat-row ${c.whitelisted ? 'whitelisted' : ''}" data-id="${c.id}">
      <input type="checkbox" class="cat-checkbox" id="vod-cat-${c.id}"
        ${c.whitelisted ? 'checked' : ''}
        onchange="vodToggleCat(${c.id}, this.checked)">
      <label for="vod-cat-${c.id}" class="cat-name" title="${escapeAttr(c.name)}">${escapeAttr(c.name)}</label>
      ${syncInfo}
      <span class="badge ${c.type === 'movie' ? 'badge-accent' : 'badge-pink'}">${c.type}</span>
      ${c.title_count ? `<span style="font-size:11px;color:var(--text3);font-family:'DM Mono',monospace">${c.title_count}</span>` : ''}
    </div>`;
  }).join('');
}

function vodToggleCat(id, checked) {
  const cat = vodState.categories.find(c => c.id === id);
  if (cat) cat.whitelisted = checked;
  const row = document.querySelector(`.cat-row[data-id="${id}"]`);
  if (row) row.classList.toggle('whitelisted', checked);
}

function filterVodCats() { renderVodCats(); }

function setVodCatFilter(filter, btn) {
  vodState.catFilter = filter;
  const page = document.getElementById('page-vod');
  page.querySelectorAll('[data-vodcatfilter]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderVodCats();
}

function vodSelectAllVisible(checked) {
  document.querySelectorAll('#vod-cat-list .cat-checkbox').forEach(cb => {
    cb.checked = checked;
    const id = parseInt(cb.id.replace('vod-cat-', ''));
    vodToggleCat(id, checked);
  });
}

async function vodSaveCategories() {
  if (!vodState.providerId) return;
  const toEnable = vodState.categories.filter(c => c.whitelisted).map(c => String(c.id));
  const toDisable = vodState.categories.filter(c => !c.whitelisted).map(c => String(c.id));

  try {
    if (toEnable.length) {
      await api(`/api/providers/${vodState.providerId}/categories/update`, {
        method: 'POST', body: { category_ids: toEnable, whitelisted: true }
      });
    }
    if (toDisable.length) {
      await api(`/api/providers/${vodState.providerId}/categories/update`, {
        method: 'POST', body: { category_ids: toDisable, whitelisted: false }
      });
    }
    toast(`Saved — ${toEnable.length} categories active`);
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function vodRunSync(type) {
  if (!vodState.providerId) { toast('No provider selected', 'error'); return; }
  try {
    await api('/api/sync/trigger', {
      method: 'POST',
      body: { provider_id: vodState.providerId, sync_type: type }
    });
    toast(`VOD ${type} sync started`, 'info');
    const info = document.getElementById('vod-sync-info');
    if (info) info.innerHTML = '<p style="color:var(--accent)">Sync running... check Dashboard for progress.</p>';
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function vodPreviewSync() {
  if (!vodState.providerId) return;
  toast('Fetching preview...', 'info');
  try {
    const r = await api(`/api/providers/${vodState.providerId}/preview`);
    const info = document.getElementById('vod-sync-info');
    if (info) info.innerHTML = `<p>~<strong>${r.estimated_movies?.toLocaleString() || 0}</strong> movies from <strong>${r.movie_categories || 0}</strong> categories</p>`;
    toast(`Preview: ~${r.estimated_movies?.toLocaleString() || 0} movies from ${r.movie_categories || 0} categories`, 'info', 6000);
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── GLOBAL EXPORTS ────────────────────────────────────────────────────────
// All functions referenced via onclick="" in HTML must be on window.
// ══════════════════════════════════════════════════════════════════════════
(function exposeGlobals() {
  const fns = [
    // Lists page
    loadLists, loadListCards,
    saveQuickList, onQuickListUrlInput, onQuickListNameInput, onModalUrlInput, onModalNameInput,
    loadListCoverageInline, addAllMissingFromCard,
    fetchList, deleteList, showListCoverage, showAddList, saveList,
    // Coverage modal
    addAllMissingToRadarr, addAllMissingToArr, setCoverageFilter,
    // Jellyfin page (home screen + playlists + discover tabs)
    loadJellyfinPage, loadAutoPlaylists, toggleAutoPlaylist, dismissAutoPlaylistBanner, saveDiscoverInJellyfin,
    showAddTagRule, editTagRule, deleteTagRule, saveTagRule, onContentSourceChange, toggleAdvancedFilters,
    addRuleCondition, updateCondOps, onCollectionNameInput, syncSmartLists, refreshTags, syncPlaylistsToJellyfin, setPlaylistSort,
    pushHomeConfig, updateHeroPick, updateHeroSort, saveRowMaxItems, saveRowMaxItemsByKey,
    showAddHomeRow, hideAddHomeRow, confirmAddHomeRow, removeHomeRow, removeHomeRowByKey,
    homeRowDragStart, homeRowDragOver, homeRowDrop, rowKey,
    // Library
    loadLibListPills, setLibList, setLibListStatus, setLibSort, scrollListPills,
    showMediaDetail, showCoverageDetail, filterByTag, searchLibrary, setLibType, setLibSrc,
    loadMoreLibrary, showAddToRadarrModal, showAddToArrModal, confirmAddToRadarr, confirmAddToArr,
    onMonitorPresetChange, toggleSeasonAccordion, toggleSeasonAll, updateSeasonCheckbox, epPickerSelectAll, epPickerSelectNone,
    showManageEpisodesModal, confirmManageEpisodes,
    showDownloadMoreModal, confirmDownloadMore, detailToggleSeason, toggleFollow,
    // Following
    loadFollowing,
    // Duplicates
    setDupFilter, resolveDup, resolveAllKeepRadarr,
    // Log viewer
    clearLogPanel, toggleLogScroll,
    // Radarr / Sonarr
    scanRadarr, scanSonarr, writeNfos,
    // Migration
    showMigrate, previewMigration, runMigration,
    // VOD
    loadVodPage, onVodProviderChange, showVodTab, loadVodCategories, vodFetchCategories,
    filterVodCats, setVodCatFilter, vodSelectAllVisible, vodToggleCat, vodSaveCategories,
    vodRunSync, vodPreviewSync,
    // Activity
    loadActivity, startActivityPolling, stopActivityPolling,
    // Discover
    loadDiscoverPage, loadDiscover, setDiscoverType, switchDiscoverSection, showDiscoverDetail,
    onDiscoverSearchInput, clearDiscoverSearch,
    // Live TV
    loadLiveTV, showLiveTab, onLiveTypeChange, saveLiveProvider, testLiveProvider,
    liveSyncGroups, liveSyncChannels, liveSyncEpg, fillSetupUrls, updateSetupUrls, copyLiveSetup, saveSetupAddress, editSetupAddress,
    toggleLiveGroup, toggleAllGroups, saveLiveGroups, filterLiveGroups,
    loadLiveChannels, toggleLiveChannel, toggleAllChannels, saveLiveChannels, searchLiveChannels, filterLiveChannels, filterLiveChannelsByEpg, liveChPage,
  ];
  for (const fn of fns) {
    if (typeof fn === 'function') window[fn.name] = fn;
  }
})();