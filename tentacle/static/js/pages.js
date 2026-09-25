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
  return String(str).replace(/\\/g,'\\\\').replace(/'/g,'\\x27').replace(/"/g,'\\x22').replace(/`/g,'\\x60').replace(/\u2018/g,'\\x27').replace(/\u2019/g,'\\x27').replace(/\u201C/g,'\\x22').replace(/\u201D/g,'\\x22');
}
function _trailerBtn(url) {
  if (!url) return '';
  return ` <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();_playTrailerInModal('${escapeJS(url)}')" style="margin-left:6px"><svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14" style="vertical-align:-2px;margin-right:4px"><path d="M8 5v14l11-7z"/></svg>Trailer</button>`;
}
function _playTrailerInModal(url) {
  const match = url.match(/[?&]v=([^&]+)/);
  if (!match) return window.open(url, '_blank');
  const id = match[1];
  const overlay = document.createElement('div');
  overlay.id = 'trailer-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,0.85);display:flex;align-items:center;justify-content:center;cursor:pointer';
  overlay.innerHTML = `<iframe src="https://www.youtube-nocookie.com/embed/${id}?autoplay=1&rel=0" style="width:80vw;max-width:960px;aspect-ratio:16/9;border:none;border-radius:8px" allow="autoplay;encrypted-media" allowfullscreen></iframe>`;
  overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
  var escHandler = function(e) { if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', escHandler); } };
  document.addEventListener('keydown', escHandler);
  document.body.appendChild(overlay);
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
  if (state.currentUser?.is_admin) {
    loadLibStats();
    loadLibSyncSummary();
    checkStaleFiles();
    checkNewContentNotice();
    loadMatchSuspects();
  }
  loadLibDownloads();
  await fetchLibraryPage();
  // Update duplicates tab badge
  if (state.currentUser?.is_admin) _updateDupBadges();
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

// A poll still out after this long is given up on: api() has no timeout, and
// a request that never answers must not stop the polling for good.
const POLL_STALE_MS = 90000;
let _dlPollSince = 0;   // when the outstanding poll started, 0 = none
let _dlPollToken = 0;
async function pollLibDownloads() {
  if (state.currentPage !== 'library') { stopDownloadPolling(); return; }
  // A slow Radarr/Sonarr can make one answer take longer than the interval;
  // don't stack another request behind it.
  if (_dlPollSince && Date.now() - _dlPollSince < POLL_STALE_MS) return;
  const tok = ++_dlPollToken;
  _dlPollSince = Date.now();
  try {
    const data = await api('/api/activity');
    if (tok !== _dlPollToken) return;   // given up on; a newer poll owns the panel
    renderLibDownloads(data);
    if (!data.downloads || data.downloads.length === 0) {
      stopDownloadPolling();
    }
  } catch (e) { if (tok === _dlPollToken) stopDownloadPolling(); }
  finally { if (tok === _dlPollToken) _dlPollSince = 0; }
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
    const rawStatus = (d.status || 'downloading').toLowerCase();
    const status = rawStatus === 'import_blocked' ? 'import blocked' : rawStatus;
    const statusClass = rawStatus === 'stuck' ? 'stuck' :
      rawStatus === 'import_blocked' ? 'blocked' :
      rawStatus.includes('import') ? 'importing' : rawStatus.includes('queue') ? 'queued' : 'downloading';
    const eta = d.eta ? ` · ${d.eta}` : '';
    const qual = d.quality ? ` · ${d.quality}` : '';
    const reqBy = d.requested_by ? `<span class="dl-requested-by">${d.requested_by}</span>` : '';
    return `<div class="dl-item">
      <div class="dl-item-title">${d.title || 'Unknown'}${d.episode ? ' — ' + d.episode : ''}</div>
      ${reqBy}
      <div class="dl-item-status ${statusClass}">${status}${qual}${eta}</div>
      <div class="dl-item-bar"><div class="dl-item-bar-fill" style="width:${pct}%"></div></div>
      <div class="dl-item-pct">${pct}%</div>
    </div>`;
  }).join('');
}

// ── Library Sync Summary ──
let _lastSyncData = null;

async function loadLibSyncSummary() {
  const container = document.getElementById('lib-sync-summary');
  const timeEl = document.getElementById('lib-sync-time');
  const briefEl = document.getElementById('lib-sync-brief');
  if (!container) return;
  try {
    const data = await api('/api/sync/summary');
    if (!data || !data.completed_at) {
      container.style.display = 'none';
      return;
    }
    _lastSyncData = data;
    container.style.display = '';
    timeEl.textContent = dashTimeAgo(data.completed_at) || data.completed_at || '';
    // Build brief inline summary
    let brief = [];
    const totalNew = (data.providers || []).reduce((s, p) => s + (p.new_movies || 0) + (p.new_series || 0), 0);
    if (totalNew > 0) brief.push(`${totalNew} new VOD`);
    if (data.radarr_new > 0) brief.push(`${data.radarr_new} Radarr`);
    if (data.sonarr_new > 0) brief.push(`${data.sonarr_new} Sonarr`);
    // Check for any failures
    const hasFailure = (data.providers_detail || []).some(p => p.status === 'failed')
      || data.radarr_status === 'failed' || data.sonarr_status === 'failed'
      || data.jellyfin_status === 'failed';
    if (hasFailure) brief.push('<span style="color:var(--red)">errors</span>');
    if (brief.length === 0) brief.push('no changes');
    briefEl.innerHTML = '· ' + brief.join(' · ');
  } catch (e) {
    container.style.display = 'none';
  }
}

function openSyncDetailModal() {
  if (!_lastSyncData) return;
  const overlay = document.getElementById('modal-sync-detail');
  const body = document.getElementById('sync-detail-body');
  if (!overlay || !body) return;
  body.innerHTML = _buildSyncDetailHtml(_lastSyncData);
  overlay.style.display = '';
}

function closeSyncDetailModal() {
  document.getElementById('modal-sync-detail').style.display = 'none';
}

function _syncStepHtml(icon, label, status, detail) {
  const colors = { ok: 'var(--green)', failed: 'var(--red)', skipped: 'var(--text3)' };
  const icons = { ok: '✓', failed: '✗', skipped: '–' };
  const c = colors[status] || 'var(--text3)';
  const ic = icons[status] || icon;
  return `<div style="display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)">
    <div style="color:${c};font-size:14px;width:18px;text-align:center;flex-shrink:0;padding-top:1px">${ic}</div>
    <div style="flex:1;min-width:0">
      <div style="font-weight:500;color:var(--text);margin-bottom:2px">${label}</div>
      <div style="font-size:12px;color:var(--text3)">${detail}</div>
    </div>
  </div>`;
}

function _buildSyncDetailHtml(d) {
  let html = '';
  // Header with time info
  const timeAgo = dashTimeAgo(d.completed_at) || '';
  const duration = d.total_duration ? _fmtDuration(d.total_duration) : '';
  html += `<div style="margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border)">
    <div style="font-size:12px;color:var(--text3)">Completed ${timeAgo}${duration ? ' · Duration: ' + duration : ''}</div>
  </div>`;

  // 1. Lists
  if (d.lists_updated && d.lists_updated.length > 0) {
    const parts = d.lists_updated.map(l => {
      let ch = [];
      if (l.added > 0) ch.push(`<span style="color:var(--green)">+${l.added}</span>`);
      if (l.removed > 0) ch.push(`<span style="color:var(--red)">-${l.removed}</span>`);
      return `${l.name} (${ch.join(', ')})`;
    });
    html += _syncStepHtml('↻', 'List Refresh', 'ok', parts.join(' · '));
  } else {
    html += _syncStepHtml('↻', 'List Refresh', 'ok', 'No list changes');
  }

  // 2. VOD Sync per provider
  const providers = d.providers_detail || d.providers || [];
  if (providers.length > 0) {
    providers.forEach(p => {
      if (p.status === 'failed') {
        html += _syncStepHtml('⟳', `VOD: ${p.name}`, 'failed', p.error || 'Sync failed');
        return;
      }
      let lines = [];
      const nm = p.new_movies || 0, ns = p.new_series || 0;
      const em = p.existing_movies || 0, es = p.existing_series || 0;
      const fm = p.failed_movies || 0, fs = p.failed_series || 0;
      const sm = p.skipped_movies || 0, ss = p.skipped_series || 0;
      // Movies line
      if (nm > 0 || em > 0) {
        let mParts = [];
        if (nm > 0) mParts.push(`<span style="color:var(--green)">${nm} new</span>`);
        mParts.push(`${em} existing`);
        if (fm > 0) mParts.push(`<span style="color:var(--red)">${fm} failed</span>`);
        if (sm > 0) mParts.push(`${sm} skipped`);
        lines.push(`Movies: ${mParts.join(', ')}`);
      }
      // Series line
      if (ns > 0 || es > 0) {
        let sParts = [];
        if (ns > 0) sParts.push(`<span style="color:var(--green)">${ns} new</span>`);
        sParts.push(`${es} existing`);
        if (fs > 0) sParts.push(`<span style="color:var(--red)">${fs} failed</span>`);
        if (ss > 0) sParts.push(`${ss} skipped`);
        lines.push(`Series: ${sParts.join(', ')}`);
      }
      // New titles
      if (p.movie_titles && p.movie_titles.length > 0) {
        lines.push(`<span style="color:var(--text3)">New movies: ${p.movie_titles.join(', ')}</span>`);
      }
      if (p.series_titles && p.series_titles.length > 0) {
        lines.push(`<span style="color:var(--text3)">New series: ${p.series_titles.join(', ')}</span>`);
      }
      // New categories
      if (p.new_categories && p.new_categories.length > 0) {
        lines.push(`<span style="color:var(--amber)">${p.new_categories.length} new categories: ${p.new_categories.join(', ')}</span>`);
      }
      const dur = p.duration_seconds ? ` (${_fmtDuration(p.duration_seconds)})` : '';
      html += _syncStepHtml('⟳', `VOD: ${p.name}${dur}`, 'ok', lines.join('<br>') || 'No changes');
    });
  }

  // 3. Radarr
  if (d.radarr_status) {
    const detail = d.radarr_status === 'failed' ? '<span style="color:var(--red)">Scan failed</span>'
      : d.radarr_new > 0 ? `<span style="color:var(--green)">${d.radarr_new} new movie${d.radarr_new !== 1 ? 's' : ''} imported</span>`
      : 'No new movies';
    html += _syncStepHtml('⬇', 'Radarr Scan', d.radarr_status, detail);
  }

  // 4. Sonarr
  if (d.sonarr_status) {
    const detail = d.sonarr_status === 'failed' ? '<span style="color:var(--red)">Scan failed</span>'
      : d.sonarr_new > 0 ? `<span style="color:var(--green)">${d.sonarr_new} new series imported</span>`
      : 'No new series';
    html += _syncStepHtml('⬇', 'Sonarr Scan', d.sonarr_status, detail);
  }

  // 5. Jellyfin Pipeline
  if (d.jellyfin_status) {
    const detail = d.jellyfin_status === 'failed' ? '<span style="color:var(--red)">Pipeline failed</span>'
      : d.tags_pushed > 0 ? `${d.tags_pushed} tags pushed, playlists refreshed`
      : 'Playlists refreshed';
    html += _syncStepHtml('☁', 'Jellyfin Pipeline', d.jellyfin_status, detail);
  }

  // 6. EPG
  if (d.epg_synced) {
    html += _syncStepHtml('📡', 'Live TV EPG', 'ok', d.epg_details || 'EPG data refreshed');
  }

  // 7. Cleanup
  if (d.orphans_removed > 0 || d.vod_orphans_removed > 0) {
    let parts = [];
    if (d.orphans_removed > 0) parts.push(`${d.orphans_removed} orphaned download(s)`);
    if (d.vod_orphans_removed > 0) parts.push(`${d.vod_orphans_removed} orphaned VOD record(s)`);
    html += _syncStepHtml('🧹', 'Cleanup', 'ok', 'Removed ' + parts.join(', '));
  }

  // Remove last border
  html = html.replace(/border-bottom:1px solid var\(--border\)"><\/div>\s*$/, '"></div>');

  return html;
}

function _fmtDuration(secs) {
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
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
      document.getElementById('stale-files-banner').style.display = '';
      document.getElementById('stale-strm-count').textContent = data.strm_count || 0;
    }
  } catch (e) { /* ignore */ }
}

// ── New provider content notice (nightly discovery) ──
async function checkNewContentNotice() {
  if (!state.currentUser?.is_admin) return;
  try {
    const n = await api('/api/sync/new-content-notice');
    const banner = document.getElementById('new-content-banner');
    if (!banner) return;
    if (!n.exists) { banner.style.display = 'none'; return; }

    const parts = [];
    const vodNames = n.vod || [];
    const liveNames = n.live || [];
    const vodTotal = n.vod_total || vodNames.length;
    const liveTotal = n.live_total || liveNames.length;
    if (vodTotal) {
      const shown = vodNames.slice(0, 4).join(', ');
      const extra = vodTotal > 4 ? ` and ${vodTotal - 4} more` : '';
      parts.push(`${vodTotal} new VOD categor${vodTotal !== 1 ? 'ies' : 'y'} (${shown}${extra})`);
    }
    if (liveTotal) {
      const shown = liveNames.slice(0, 4).join(', ');
      const extra = liveTotal > 4 ? ` and ${liveTotal - 4} more` : '';
      parts.push(`${liveTotal} new Live TV group${liveTotal !== 1 ? 's' : ''} (${shown}${extra})`);
    }
    document.getElementById('new-content-msg').textContent =
      `Your providers added ${parts.join(' and ')}. They're disabled until you enable the ones you want.`;
    document.getElementById('new-content-vod-btn').style.display = vodTotal ? '' : 'none';
    document.getElementById('new-content-live-btn').style.display = liveTotal ? '' : 'none';
    banner.style.display = '';
  } catch (e) { /* ignore */ }
}

async function dismissNewContentNotice() {
  const banner = document.getElementById('new-content-banner');
  if (banner) banner.style.display = 'none';
  try { await api('/api/sync/new-content-notice/dismiss', { method: 'POST' }); } catch {}
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
    ? `<img src="https://image.tmdb.org/t/p/w185${item.poster_path}" loading="lazy" onerror="this.outerHTML='<div class=\\'lib-card-poster-placeholder\\'>◫</div>'">`
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
let _addArrTvdbId = null;
let _addArrMediaType = 'movie';
let _epPickerSeasons = [];  // cached season list for current series
let _epPickerLoaded = {};   // season_number -> episodes array
// Add, Manage Episodes and Download More share one modal and one picker. Each
// opening clears everything the picker knows about the last title and takes a
// new token; an answer that arrives after the modal was opened again belongs to
// the earlier title and is dropped.
let _epPickerToken = 0;
function _resetEpisodePicker() {
  _epPickerSeasons = [];
  _epPickerLoaded = {};
  _vodEpisodes = {};
  _dlEpisodes = {};
  _unairedPerSeason = {};
  return ++_epPickerToken;
}

// Helper: resolve poster/backdrop URLs (TVDB sends full URLs, TMDB sends relative paths)
function _imgUrl(path, size) {
  if (!path) return '';
  if (path.startsWith('http') || path.startsWith('/api/discover/image-proxy/')) return path;
  return 'https://image.tmdb.org/t/p/' + size + path;
}

async function showAddToRadarrModal(tmdbId, title, year, posterPath) {
  showAddToArrModal(tmdbId, title, year, posterPath, 'movie');
}

async function showAddToArrModal(tmdbId, title, year, posterPath, mediaType, tvdbId) {
  _resetManageMode(); // ensure modal is in add mode
  const tok = _resetEpisodePicker();
  _addArrTmdbId = tmdbId;
  _addArrTvdbId = tvdbId || 0;
  _addArrMediaType = mediaType || 'movie';
  const isSeries = _addArrMediaType === 'series';
  const arrName = isSeries ? 'Sonarr' : 'Radarr';

  document.getElementById('add-arr-modal-title').textContent = `Add to ${arrName}`;

  const modalBox = document.getElementById('add-arr-modal-box');
  modalBox.className = 'modal modal-arr';

  const info = document.getElementById('add-radarr-movie-info');
  const posterSrc = _imgUrl(posterPath, 'w185');
  const posterImg = posterSrc
    ? `<img src="${posterSrc}" style="border-radius:8px;width:80px;height:120px;object-fit:cover">`
    : `<div style="width:80px;height:120px;background:var(--bg3);border-radius:8px;display:flex;align-items:center;justify-content:center;color:var(--text3)">&#9707;</div>`;
  info.innerHTML = `${posterImg}<div style="display:flex;flex-direction:column;justify-content:center"><div style="font-weight:600;font-size:17px">${title || 'Unknown'}</div><div style="color:var(--text2);font-size:14px">${year || ''}</div></div>`;

  const select = document.getElementById('add-radarr-quality');
  select.innerHTML = '<option value="">Loading...</option>';
  try {
    const endpoint = isSeries ? '/api/lists/sonarr-profiles' : '/api/lists/radarr-profiles';
    const profiles = await api(endpoint);
    if (tok !== _epPickerToken) return;
    select.innerHTML = profiles.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
  } catch (e) {
    if (tok !== _epPickerToken) return;
    select.innerHTML = '<option value="">Failed to load profiles</option>';
  }

  document.getElementById('sonarr-extra-fields').style.display = isSeries ? 'block' : 'none';
  const monitorWrap = document.getElementById('dl-more-monitor-wrap');
  if (monitorWrap) monitorWrap.style.display = 'none';

  // Reset episode picker state
  document.getElementById('episode-picker').style.display = 'none';
  document.getElementById('episode-picker-seasons').innerHTML = '';
  document.getElementById('add-sonarr-monitor').value = 'all';

  const btn = document.getElementById('add-radarr-confirm-btn');
  btn.disabled = false;
  btn.textContent = `Add to ${arrName}`;
  showModal('modal-add-to-radarr');
}

async function confirmAddToArr() {
  if (!_addArrTmdbId && !_addArrTvdbId) return;
  const isSeries = _addArrMediaType === 'series';
  const arrName = isSeries ? 'Sonarr' : 'Radarr';
  const btn = document.getElementById('add-radarr-confirm-btn');
  btn.disabled = true;
  btn.textContent = 'Adding...';
  const qualityId = document.getElementById('add-radarr-quality').value;
  const isTvdbOnly = !_addArrTmdbId && _addArrTvdbId;
  const body = isTvdbOnly ? { tvdb_ids: [_addArrTvdbId] } : { tmdb_ids: [_addArrTmdbId] };
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
      // The backend returns a plain-english reason in `detail` whenever it has
      // one — a bare "Failed to add" leaves the user nothing to act on.
      toast(r.detail || 'Failed to add', 'error', r.detail ? 8000 : undefined);
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
  const tok = _epPickerToken;
  const loading = document.getElementById('episode-picker-loading');
  loading.style.display = 'block';
  try {
    const isTvdbOnly = !_addArrTmdbId && _addArrTvdbId;
    const seasonsUrl = isTvdbOnly
      ? `/api/discover/seasons-tvdb/${_addArrTvdbId}`
      : `/api/discover/seasons/${_addArrTmdbId}`;
    const data = await api(seasonsUrl);
    if (tok !== _epPickerToken) return;
    _epPickerSeasons = (data.seasons || []).filter(s => s.season_number > 0);
    _renderSeasons();
  } catch (e) {
    if (tok !== _epPickerToken) return;
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
    const tok = _epPickerToken;
    const tmdbId = _downloadMoreTmdbId || _addArrTmdbId;
    const isTvdbOnly = !tmdbId && _addArrTvdbId;
    list.innerHTML = '<div style="padding:8px 28px;color:var(--text3);font-size:12px">Loading...</div>';
    try {
      const epUrl = isTvdbOnly
        ? `/api/discover/season-tvdb/${_addArrTvdbId}/${seasonNum}`
        : `/api/discover/season/${tmdbId}/${seasonNum}`;
      const data = await api(epUrl);
      if (tok !== _epPickerToken) return;
      _epPickerLoaded[seasonNum] = data.episodes || [];
      _renderEpisodes(seasonNum);
    } catch (e) {
      if (tok !== _epPickerToken) return;
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
  const tok = _resetEpisodePicker();

  const modalBox = document.getElementById('add-arr-modal-box');
  modalBox.className = 'modal modal-arr ep-expanded';

  document.getElementById('add-arr-modal-title').textContent = 'Manage Episodes';

  const info = document.getElementById('add-radarr-movie-info');
  const managePosterSrc = _imgUrl(posterPath, 'w185');
  const posterImg = managePosterSrc
    ? `<img src="${managePosterSrc}" style="border-radius:8px;width:80px;height:120px;object-fit:cover">`
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
    if (tok !== _epPickerToken) return;
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
    if (tok !== _epPickerToken) return;
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
let _unairedPerSeason = {};  // {season: count} unaired episodes from Sonarr

async function showDownloadMoreModal(tmdbId, title, year, posterPath) {
  _downloadMoreTmdbId = tmdbId;
  const tok = _resetEpisodePicker();

  const modalBox = document.getElementById('add-arr-modal-box');
  modalBox.className = 'modal modal-arr ep-expanded';

  document.getElementById('add-arr-modal-title').textContent = 'Download More Episodes';

  const info = document.getElementById('add-radarr-movie-info');
  const dlMorePosterSrc = _imgUrl(posterPath, 'w185');
  const posterImg = dlMorePosterSrc
    ? `<img src="${dlMorePosterSrc}" style="border-radius:8px;width:80px;height:120px;object-fit:cover">`
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
    if (tok !== _epPickerToken) return;
    select.innerHTML = profiles.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
  } catch (e) {
    if (tok !== _epPickerToken) return;
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
    if (tok !== _epPickerToken) return;

    if (vodData.has_episodes) {
      _vodEpisodes = vodData.episodes;
    }

    // Build downloaded episodes map and unaired counts from Sonarr
    const now = new Date();
    _unairedPerSeason = {};
    if (sonarrData.in_sonarr && sonarrData.episodes) {
      for (const ep of sonarrData.episodes) {
        if (ep.seasonNumber > 0) {
          // Track unaired episodes
          if (ep.airDateUtc && new Date(ep.airDateUtc) > now) {
            _unairedPerSeason[ep.seasonNumber] = (_unairedPerSeason[ep.seasonNumber] || 0) + 1;
          }
          // Track downloaded episodes (skip VOD — Sonarr sees .strm as hasFile)
          if (ep.hasFile) {
            const vodList = _vodEpisodes[String(ep.seasonNumber)] || _vodEpisodes[ep.seasonNumber] || [];
            if (vodList.includes(ep.episodeNumber)) continue;
            if (!_dlEpisodes[ep.seasonNumber]) _dlEpisodes[ep.seasonNumber] = [];
            _dlEpisodes[ep.seasonNumber].push(ep.episodeNumber);
          }
        }
      }
    }

    _epPickerSeasons = (seasonsData.seasons || []).filter(s => s.season_number > 0);
    _renderSeasonsWithVod();
  } catch (e) {
    if (tok !== _epPickerToken) return;
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
    const unaired = _unairedPerSeason[sn] || 0;
    const aired = Math.max(0, total - unaired);
    const vodEps = _vodEpisodes[String(sn)] || _vodEpisodes[sn] || [];
    const dlEps = _dlEpisodes[String(sn)] || _dlEpisodes[sn] || [];
    const haveCount = new Set([...vodEps, ...dlEps]).size;
    let countText, countClass, checked;
    if (haveCount === 0) {
      countText = `${aired} ep`;
      countClass = 'ep-count';
      checked = false;
    } else if (haveCount >= aired) {
      countText = `${aired}/${aired}`;
      countClass = 'ep-count ep-count-full';
      checked = true;
    } else {
      countText = `${haveCount}/${aired}`;
      countClass = 'ep-count ep-count-partial';
      checked = false;
    }
    const unairedLabel = unaired > 0 ? ` <span style="color:var(--text3);font-size:11px">+${unaired} upcoming</span>` : '';
    return `<div class="ep-picker-season" data-season="${sn}">
      <div class="ep-picker-season-header" onclick="toggleSeasonAccordion(${sn})">
        <span class="ep-arrow" id="ep-arrow-${sn}">▶</span>
        <input type="checkbox" class="ep-picker-season-check" ${checked ? 'checked' : ''} ${haveCount >= aired && aired > 0 ? 'disabled' : ''} onclick="event.stopPropagation();toggleSeasonAll(${sn}, this.checked)">
        <span>Season ${sn}${airYear}</span>
        <span class="${countClass}">${countText}</span>${unairedLabel}
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
  _addArrTvdbId = null;
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

// The detail modal is shared by every title. Each opening takes a number; an
// answer for an earlier opening (a slow one, then another title clicked) is
// dropped instead of replacing the title on screen.
let _detailSeq = 0;

async function showMediaDetail(tmdbId, mediaType) {
  const seq = ++_detailSeq;
  showModal('modal-media-detail');
  document.getElementById('detail-title').textContent = 'Loading...';
  document.getElementById('detail-body').innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';

  try {
    const data = await api(`/api/library/item/${mediaType}/${tmdbId}`);
    if (seq !== _detailSeq) return;
    document.getElementById('detail-title').textContent = data.title;
    const isSeries = mediaType === 'series';
    document.getElementById('detail-body').innerHTML = `
      <div class="detail-layout" style="display:flex;gap:20px">
        ${data.poster_path ? `<img src="${_imgUrl(data.poster_path, 'w185')}" class="detail-poster" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
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
          ${data.is_vod ? `<label class="detail-follow-toggle" style="margin-top:10px" title="Turn this off to keep the title in the catalog but stop Tentacle writing or repairing its .strm files — for shows you have switched to downloaded copies">
            <input type="checkbox" ${data.strm_managed ? 'checked' : ''} onchange="toggleStrmManaged('${mediaType}', ${tmdbId}, this.checked)">
            <span class="detail-follow-label">Manage .strm files</span>
          </label>` : ''}
          ${data.source === 'radarr' ? `<div style="margin-top:10px">
            <button class="btn btn-secondary btn-sm" title="Wrong language, burned-in subtitles, broken audio or a fake: delete this file, block its release, find another"
              onclick="replaceCopy('movie', ${tmdbId}, null, null, this)">Bad copy? Get another one</button>
          </div>` : ''}
          ${data.is_vod && !isSeries && state.currentUser?.is_admin ? `<div style="margin-top:10px">
            <button class="btn btn-danger btn-sm" title="The provider's stream is a different film than this title" onclick="openFixMatch(${tmdbId}, '${escapeJS(data.title || '')}')">Wrong movie? Fix it</button>
          </div>` : ''}
          <div id="detail-trailer-slot" style="margin-top:10px"></div>
        </div>
      </div>
      ${isSeries ? '<div id="detail-episodes" style="margin-top:20px"><div class="loading-state" style="padding:16px 0"><div class="spinner"></div></div></div>' : ''}`;

    // For series, load episode breakdown
    if (isSeries) _loadSeriesEpisodes(tmdbId, data);

    // Fetch trailer URL from TMDB in background
    api(`/api/library/tmdb/${mediaType}/${tmdbId}`).then(tmdbData => {
      if (seq !== _detailSeq) return;
      const slot = document.getElementById('detail-trailer-slot');
      if (slot && tmdbData.trailer_url) slot.innerHTML = _trailerBtn(tmdbData.trailer_url);
    }).catch(() => {});
  } catch (e) {
    if (seq !== _detailSeq) return;
    document.getElementById('detail-body').innerHTML = '<div class="empty-state"><p>Failed to load details</p></div>';
  }
}

// ── Wrong movie: the provider's stream is a different film than its label ──
async function reportWrongMovie(tmdbId, title, confirmed = false) {
  if (!confirmed && !confirm(
    `Does "${title}" play a different film?\n\n` +
    'Your IPTV provider labelled that stream wrong. This removes this copy from the library ' +
    'and stops that stream from being added again (you can undo the block under Library).\n\n' +
    'If you requested the real movie, Radarr keeps looking for it.'
  )) return false;
  try {
    const r = await api(`/api/library/wrong-match/movie/${tmdbId}`, { method: 'POST' });
    toast(r.message || `Removed the wrong copy of ${title}`);
    closeModal('modal-media-detail');
    closeModal('modal-fix-match');
    loadMatchSuspects();
    if (typeof fetchLibraryPage === 'function') { pages.lib.offset = 0; pages.lib.items = []; fetchLibraryPage(); }
    return true;
  } catch (e) {
    toast(e.message, 'error');
    return false;
  }
}

// Which film is this stream really? Candidates are ranked by the stream's real
// length (Jellyfin's probe) when known; picking one moves the copy there.
let _fm = { tmdbId: null, title: '', cands: [], armed: false, busy: false };

function openFixMatch(tmdbId, title) {
  _fm = { tmdbId, title, cands: [], armed: false, busy: false };
  let m = document.getElementById('modal-fix-match');
  if (!m) {
    m = document.createElement('div');
    m.className = 'modal-overlay'; m.id = 'modal-fix-match'; m.style.display = 'none';
    m.innerHTML = `<div class="modal" style="max-width:620px">
      <div class="modal-header"><div class="modal-title">Which movie is this really?</div>
        <button class="modal-close" onclick="closeModal('modal-fix-match')">✕</button></div>
      <div style="padding:0 20px 20px">
        <p class="fm-intro" id="fm-intro"></p>
        <div class="fm-frames-wrap"><button class="btn btn-secondary btn-sm" id="fm-show-frames" onclick="_fmFrames()">Not sure? Show pictures from the stream</button>
          <div id="fm-frames" class="fm-frames"></div></div>
        <div class="fm-search"><input id="fm-q" class="form-input" placeholder="Search for the right title"
          onkeydown="if(event.key==='Enter'){event.preventDefault();_fmLoad(this.value.trim())}">
          <button class="btn btn-secondary btn-sm" onclick="_fmLoad(document.getElementById('fm-q').value.trim())">Search</button></div>
        <div id="fm-list" class="fm-list"></div>
        <div id="fm-status" class="fm-status"></div>
        <div class="fm-footer"><button class="btn btn-danger btn-sm" id="fm-remove" onclick="_fmRemove()">None of these — remove it</button>
          <button class="btn btn-secondary btn-sm" onclick="closeModal('modal-fix-match')">Not sure — leave it for now</button></div>
      </div></div>`;
    document.body.appendChild(m);
  }
  document.getElementById('fm-q').value = '';
  document.getElementById('fm-status').textContent = '';
  document.getElementById('fm-remove').textContent = 'None of these — remove it';
  document.getElementById('fm-frames').innerHTML = '';
  document.getElementById('fm-show-frames').style.display = '';
  document.getElementById('fm-intro').innerHTML = `Your IPTV provider labelled this stream <strong>${escapeAttr(title)}</strong>, but it plays something else. <span id="fm-actual"></span>`;
  closeModal('modal-media-detail');
  showModal('modal-fix-match');
  _fmLoad('');
}

// "Bad copy? Get another one": blocklist the release, delete the file, search
// for a different one. Two clicks: the first says what will happen.
async function replaceCopy(mediaType, tmdbId, season, episode, btn) {
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    btn.dataset.label = btn.textContent;
    btn.textContent = mediaType === 'movie' ? 'Click again: delete this file and find another' : 'Again: replace';
    btn.classList.add('armed');
    setTimeout(() => { if (btn.dataset.armed === '1') { btn.dataset.armed = ''; btn.textContent = btn.dataset.label; btn.classList.remove('armed'); } }, 5000);
    return;
  }
  btn.dataset.armed = '';
  btn.disabled = true;
  try {
    const body = mediaType === 'series' ? { season_number: season, episode_number: episode } : {};
    const r = await api(`/api/library/replace/${mediaType}/${tmdbId}`, { method: 'POST', body });
    toast(r.message || 'Getting another copy');
    btn.textContent = mediaType === 'movie' ? 'Getting another copy…' : '…';
  } catch (e) {
    toast(e.message || 'Failed', 'error');
    btn.disabled = false; btn.textContent = btn.dataset.label; btn.classList.remove('armed');
  }
}

async function _fmLoad(q) {
  const list = document.getElementById('fm-list');
  list.innerHTML = `<div class="fm-note">${q ? 'Searching…' : 'Looking for likely matches…'}</div>`;
  try {
    const d = await api(`/api/library/fix-match/movie/${_fm.tmdbId}/suggestions${q ? '?q=' + encodeURIComponent(q) : ''}`);
    const langs = (d.audio_languages || []).map(l => l.name);
    const clues = [];
    if (d.actual_minutes) clues.push(`it plays ${d.actual_minutes} minutes`);
    if (langs.length === 1) clues.push(`its audio is ${langs[0]}`);
    document.getElementById('fm-actual').textContent = clues.length
      ? `Clues: ${clues.join(', ')} — films that fit are listed first.`
      : 'Pick the film it really is, or search for it.';
    _fm.cands = d.candidates || [];
    list.innerHTML = _fm.cands.length ? _fm.cands.map((c, i) => `
      <button class="fm-cand" onclick="_fmPick(${i})">
        ${c.poster_path ? `<img src="${_imgUrl(c.poster_path, 'w92')}" loading="lazy">` : '<div class="fm-noposter"></div>'}
        <span class="fm-cand-text"><span class="fm-cand-title">${escapeAttr(c.title)}</span>
          <span class="fm-cand-meta">${escapeAttr([c.year, c.runtime ? c.runtime + ' min' : '', c.language_name || ''].filter(Boolean).join(' · '))}
            ${c.runtime_matches ? '<span class="badge badge-green">same length</span>' : ''}
            ${c.language_matches ? '<span class="badge badge-green">same language</span>' : ''}
            ${c.in_library ? '<span class="badge badge-amber">already in library</span>' : ''}</span>
          ${c.overview ? `<span class="fm-cand-ov">${escapeAttr(c.overview)}</span>` : ''}</span>
      </button>`).join('') : '<div class="fm-note">No matches found — try searching for the title you saw.</div>';
  } catch (e) {
    list.innerHTML = `<div class="fm-note">${escapeAttr(e.message || 'Could not load suggestions')}</div>`;
  }
}

// Stills from the stream — only on request, since it opens a provider connection.
async function _fmFrames() {
  const btn = document.getElementById('fm-show-frames');
  const box = document.getElementById('fm-frames');
  const id = _fm.tmdbId;
  btn.style.display = 'none';
  box.innerHTML = '<div class="fm-note">Grabbing pictures from the stream… (can take a few seconds)</div>';
  try {
    const d = await api(`/api/library/fix-match/movie/${id}/frames`);
    if (_fm.tmdbId !== id) return;
    box.innerHTML = (d.frames || []).map(f =>
      `<figure class="fm-frame"><img src="${f.image}" alt=""><figcaption>${f.at_minutes} min</figcaption></figure>`).join('');
  } catch (e) {
    if (_fm.tmdbId !== id) return;
    box.innerHTML = `<div class="fm-note">${escapeAttr(e.message || 'Could not grab pictures from the stream')}</div>`;
    btn.style.display = '';
  }
}

async function _fmPick(i) {
  const c = _fm.cands[i];
  if (!c || _fm.busy) return;
  _fm.busy = true;
  const st = document.getElementById('fm-status');
  st.style.color = 'var(--text2)'; st.textContent = 'Fixing…';
  try {
    const r = await api(`/api/library/fix-match/movie/${_fm.tmdbId}`, { method: 'POST', body: { tmdb_id: c.tmdb_id } });
    toast(r.message || `Fixed: ${c.title}`);
    closeModal('modal-fix-match');
    loadMatchSuspects();
    pages.lib.offset = 0; pages.lib.items = []; fetchLibraryPage();
  } catch (e) {
    st.style.color = 'var(--red)'; st.textContent = e.message || 'Failed';
  } finally { _fm.busy = false; }
}

function _fmRemove() {
  const b = document.getElementById('fm-remove');
  if (!_fm.armed) {
    _fm.armed = true;
    b.textContent = 'Click again: remove this copy and block the stream';
    setTimeout(() => { _fm.armed = false; if (b) b.textContent = 'None of these — remove it'; }, 5000);
    return;
  }
  _fm.armed = false;
  reportWrongMovie(_fm.tmdbId, _fm.title, true);
}

async function loadMatchSuspects() {
  const card = document.getElementById('wrong-match-card');
  if (!card || !state.currentUser?.is_admin) return;
  try {
    const [s, b] = await Promise.all([api('/api/library/match-suspects'), api('/api/library/blocked-streams')]);
    const suspects = s.suspects || [], blocked = b.blocked || [];
    if (!suspects.length && !blocked.length) { card.style.display = 'none'; return; }
    card.style.display = '';
    document.getElementById('wrong-match-list').innerHTML = suspects.length ? suspects.map(x => `
      <div class="wm-row">
        ${x.poster_path ? `<img src="${_imgUrl(x.poster_path, 'w92')}" class="wm-poster" loading="lazy" onerror="this.style.visibility='hidden'">` : '<div class="wm-poster"></div>'}
        <div class="wm-info">
          <div class="wm-title">${escapeAttr(x.title || '')}</div>
          <div class="wm-meta">Plays <strong>${x.actual_minutes} min</strong> — this film is ${x.expected_minutes} min</div>
        </div>
        <div class="wm-actions">
          <button class="btn btn-primary btn-sm" onclick="openFixMatch(${x.tmdb_id}, '${escapeJS(x.title || '')}')">Fix it</button>
          <button class="btn btn-secondary btn-sm" onclick="dismissMatchSuspect(${x.tmdb_id})">It's fine</button>
        </div>
      </div>`).join('') : '<div class="wm-meta" style="padding:4px 0">Nothing flagged right now.</div>';
    const bl = document.getElementById('wrong-match-blocked');
    bl.innerHTML = blocked.length ? `<details><summary>${blocked.length} blocked stream${blocked.length === 1 ? '' : 's'}</summary>` +
      blocked.map(x => `<div class="wm-blocked-row"><span>${escapeAttr(x.title || '')} <span class="wm-meta">· ${escapeAttr(x.provider)} stream ${escapeAttr(x.stream)}</span></span>
        <button class="btn btn-secondary btn-sm" onclick="unblockStream(${x.id})">Unblock</button></div>`).join('') + '</details>' : '';
  } catch (e) { card.style.display = 'none'; }
}

async function dismissMatchSuspect(tmdbId) {
  try { await api(`/api/library/match-suspects/${tmdbId}/dismiss`, { method: 'POST' }); loadMatchSuspects(); }
  catch (e) { toast(e.message, 'error'); }
}

async function unblockStream(id) {
  if (!confirm('Unblock this stream? It will be imported again on the next sync.')) return;
  try { await api(`/api/library/blocked-streams/${id}`, { method: 'DELETE' }); toast('Unblocked'); loadMatchSuspects(); }
  catch (e) { toast(e.message, 'error'); }
}

async function toggleStrmManaged(mediaType, tmdbId, enabled) {
  // Off = keep the title in the catalog but stop writing/repairing its .strm
  // files. Offer to remove the ones already on disk, since the usual reason to
  // switch this off is that the provider's stream is broken and the title has
  // been replaced by downloaded copies.
  let deleteFiles = false;
  if (!enabled) {
    deleteFiles = confirm(
      'Stop managing .strm files for this title.\n\n' +
      'OK: also delete the .strm/.nfo files Tentacle wrote (downloaded episodes are left alone).\n' +
      'Cancel: leave the existing files in place.'
    );
  }
  try {
    const r = await api(`/api/library/strm-managed/${mediaType}/${tmdbId}`, {
      method: 'POST',
      body: { enabled, delete_files: deleteFiles },
    });
    toast(enabled
      ? 'Tentacle will keep .strm files for this title up to date'
      : `.strm management off${r.files_deleted ? ` — ${r.files_deleted} file(s) removed` : ''}`);
  } catch (e) {
    toast(e.message, 'error');
  }
}

let _detailEpState = {}; // { vodEps, dlEps, tmdbId, loaded: {sn: true} }

async function _loadSeriesEpisodes(tmdbId, seriesData) {
  const container = document.getElementById('detail-episodes');
  if (!container) return;
  const seq = _detailSeq;

  try {
    const [seasonsData, vodData, sonarrData] = await Promise.all([
      api(`/api/discover/seasons/${tmdbId}`),
      api(`/api/discover/vod-episodes/${tmdbId}`),
      api(`/api/discover/sonarr-episodes/${tmdbId}`).catch(() => ({ in_sonarr: false })),
    ]);
    // Another title's detail since: its episode state must not become this one's.
    if (seq !== _detailSeq) return;

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

    // Build per-season unaired counts from Sonarr air dates
    const unairedPerSeason = {};
    if (sonarrData.in_sonarr && sonarrData.episodes) {
      const now = new Date();
      for (const ep of sonarrData.episodes) {
        if (ep.seasonNumber > 0 && ep.airDateUtc) {
          const airDate = new Date(ep.airDateUtc);
          if (airDate > now) {
            unairedPerSeason[ep.seasonNumber] = (unairedPerSeason[ep.seasonNumber] || 0) + 1;
          }
        }
      }
    }

    _detailEpState = { vodEps, dlEps, sonarrEpMap, tmdbId, loaded: {}, unairedPerSeason };

    // Compute totals (exclude unaired episodes from the available count)
    let totalOwned = 0, totalAvailable = 0;
    for (const s of seasons) {
      const sn = s.season_number;
      const total = s.episode_count || 0;
      const unaired = unairedPerSeason[sn] || 0;
      const aired = Math.max(0, total - unaired);
      const vod = vodEps[String(sn)] || vodEps[sn] || [];
      const dl = dlEps[String(sn)] || dlEps[sn] || [];
      const have = new Set([...vod, ...dl]).size;
      totalOwned += have;
      totalAvailable += aired;
    }

    const isComplete = totalOwned >= totalAvailable && totalAvailable > 0;
    const countClass = isComplete ? 'ep-count-full' : (totalOwned > 0 ? 'ep-count-partial' : '');
    const downloadMoreBtn = !isComplete ? `<button class="btn btn-primary btn-sm" onclick="closeModal('modal-media-detail');showDownloadMoreModal(${tmdbId},'${escapeJS(seriesData.title||'')}','${escapeJS(seriesData.year||'')}','${escapeJS(seriesData.poster_path||'')}')">Download More</button>` : '';

    // Following toggle — only for ongoing series in Sonarr (not ended/canceled)
    const inSonarr = sonarrData.in_sonarr;
    const isFollowing = seriesData.following || false;
    const seriesEnded = ['Ended', 'Canceled'].includes(seriesData.status);
    const followToggle = inSonarr && !seriesEnded ? `<label class="detail-follow-toggle" title="Auto-download new episodes">
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
      const unaired = unairedPerSeason[sn] || 0;
      const aired = Math.max(0, total - unaired);
      const vod = vodEps[String(sn)] || vodEps[sn] || [];
      const dl = dlEps[String(sn)] || dlEps[sn] || [];
      const haveSet = new Set([...vod, ...dl]);
      const haveCount = haveSet.size;
      if (haveCount === 0) continue; // Only show seasons we have episodes for

      const seasonFull = haveCount >= aired && aired > 0;
      const cClass = seasonFull ? 'ep-count-full' : 'ep-count-partial';
      const airYear = s.air_date ? ` (${s.air_date.substring(0, 4)})` : '';
      const unairedLabel = unaired > 0 ? ` <span style="color:var(--text3);font-size:11px">+${unaired} upcoming</span>` : '';

      html += `<div class="detail-ep-season" data-season="${sn}">
        <div class="detail-ep-season-hdr" onclick="detailToggleSeason(${sn})">
          <span class="ep-arrow">▶</span>
          <span>Season ${sn}${airYear}</span>
          <span class="ep-count ${cClass}">${haveCount}/${aired}</span>${unairedLabel}
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
    if (seq !== _detailSeq) return;
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
  const st = _detailEpState;

  list.innerHTML = '<div style="padding:8px 12px;color:var(--text3);font-size:12px">Loading...</div>';
  try {
    const data = await api(`/api/discover/season/${_detailEpState.tmdbId}/${sn}`);
    if (st !== _detailEpState || !list.isConnected) return;
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

    // Build air date lookup for unaired detection
    const now = new Date();
    const airDateMap = {};
    for (const ep of tmdbEps) {
      if (ep.air_date) airDateMap[ep.episode_number] = ep.air_date;
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
        ${isDl && !isVod ? `<button class="ep-replace-btn" title="Bad copy? Get another one" aria-label="Bad copy? Get another one"
          onclick="replaceCopy('series', ${_detailEpState.tmdbId}, ${sn}, ${epNum}, this)">↻</button>` : ''}
      </div>`;
    }

    // Show unaired episodes at the end
    for (const ep of tmdbEps) {
      if (haveSet.has(ep.episode_number)) continue;
      const airDate = ep.air_date ? new Date(ep.air_date + 'T00:00:00') : null;
      if (!airDate || airDate > now) {
        const dateLabel = ep.air_date ? new Date(ep.air_date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : 'TBA';
        rows += `<div class="detail-ep-row" style="opacity:0.5">
          <span class="ep-num">${ep.episode_number}</span>
          <span class="detail-ep-name">${ep.name || ''}</span>
          <span class="ep-dl-badge" style="background:var(--bg3);color:var(--text3)">${dateLabel}</span>
        </div>`;
      }
    }
    list.innerHTML = rows;
  } catch {
    list.innerHTML = '<div style="padding:8px 12px;color:var(--text3);font-size:12px">Failed to load</div>';
  }
}

async function showCoverageDetail(tmdbId, mediaType, title, year, posterPath) {
  const seq = ++_detailSeq;
  showModal('modal-media-detail');
  document.getElementById('detail-title').textContent = 'Loading...';
  document.getElementById('detail-body').innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';

  try {
    const data = await api(`/api/library/item/${mediaType}/${tmdbId}`);
    if (seq !== _detailSeq) return;
    document.getElementById('detail-title').textContent = data.title;
    document.getElementById('detail-body').innerHTML = `
      <div class="detail-layout" style="display:flex;gap:20px">
        ${data.poster_path ? `<img src="${_imgUrl(data.poster_path, 'w185')}" class="detail-poster" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
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
    if (seq !== _detailSeq) return;
    // Item not in library — fetch from TMDB for overview
    try {
      const data = await api(`/api/library/tmdb/${mediaType}/${tmdbId}`);
      if (seq !== _detailSeq) return;
      const isSeries = mediaType === 'series';
      const arrLabel = isSeries ? 'Sonarr' : 'Radarr';
      document.getElementById('detail-title').textContent = data.title || title || 'Unknown';
      document.getElementById('detail-body').innerHTML = `
        <div class="detail-layout" style="display:flex;gap:20px">
          ${data.poster_path ? `<img src="${_imgUrl(data.poster_path, 'w185')}" class="detail-poster" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
          <div style="flex:1">
            <div style="font-size:13px;color:var(--text2);margin-bottom:12px">${data.year || '—'} · ${data.runtime ? data.runtime+'m · ' : ''}★ ${data.rating || '—'}</div>
            <p style="font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:16px">${data.overview || 'No overview available.'}</p>
            <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
              ${(data.genres||[]).map(g => `<span class="badge badge-gray">${g}</span>`).join('')}
            </div>
            <div style="margin-top:8px;padding-top:12px;border-top:1px solid var(--border)">
              <span class="badge" style="background:var(--red-dim);color:var(--red);margin-bottom:8px">Not in library</span>
              <div style="margin-top:8px">
                <button class="btn btn-primary btn-sm" onclick="closeModal('modal-media-detail');showAddToArrModal(${tmdbId},'${escapeJS(data.title||'')}','${escapeJS(data.year||'')}','${escapeJS(data.poster_path||'')}','${mediaType}')">Add to ${arrLabel}</button>${_trailerBtn(data.trailer_url)}
              </div>
            </div>
          </div>
        </div>`;
    } catch {
      if (seq !== _detailSeq) return;
      document.getElementById('detail-title').textContent = title || 'Unknown';
      document.getElementById('detail-body').innerHTML = `
        <div class="detail-layout" style="display:flex;gap:20px">
          ${posterPath ? `<img src="${_imgUrl(posterPath, 'w185')}" class="detail-poster" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
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
    const summary = `Added ${r.added} to ${label}${r.already_exists ? `, ${r.already_exists} already existed` : ''}${r.failed ? `, ${r.failed} failed` : ''}`;
    toast(r.detail ? `${summary}. ${r.detail}` : summary, r.failed ? 'error' : 'success', r.detail ? 8000 : undefined);
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

function dismissAutoPlaylistBanner() {
  localStorage.setItem('tentacle_dismiss_auto_banner', '1');
  const el = document.getElementById('auto-playlist-banner');
  if (el) el.style.display = 'none';
}

// toggleHomeScreenSection removed — Home Screen is now a full tab

// ── Auto Playlists ──────────────────────────────────────────────────────

const _autoCategoryLabels = { source: 'Sources', youtube: 'YouTube Channels', list: 'Lists', builtin: 'Built-in' };
const _autoCategoryOrder = ['source', 'youtube', 'list', 'builtin'];

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
        // A locked playlist is always on: the thing that created it (a YouTube
        // channel) is the decision, and taking it away means removing that.
        const control = p.locked
          ? `<span title="Always on — remove the channel from the YouTube page to remove this" style="display:inline-block;width:36px;text-align:center;color:var(--green);font-size:14px;flex-shrink:0">&#10003;</span>`
          : `<label style="position:relative;display:inline-block;width:36px;height:20px;flex-shrink:0;cursor:pointer">
              <input type="checkbox" ${checked} onchange="toggleAutoPlaylist('${escapeAttr(p.key)}')"
                style="opacity:0;width:0;height:0;position:absolute">
              <span style="position:absolute;top:0;left:0;right:0;bottom:0;background:${toggleBg};border-radius:10px;transition:0.2s"></span>
              <span style="position:absolute;top:2px;left:${togglePos};width:16px;height:16px;background:white;border-radius:50%;transition:0.2s"></span>
            </label>`;
        html += `
          <div class="auto-pl-row">
            ${control}
            <div class="auto-pl-info">
              <div style="font-size:13px;font-weight:500">${p.name}</div>
              <div style="font-size:11px;color:var(--text3)">${p.origin}</div>
            </div>
            <div class="auto-pl-actions">
              ${sortDrop}
              ${countBadge}
            </div>
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

  // Optimistically update toggle visual
  if (checkbox) {
    const label = checkbox.closest('label');
    if (label) {
      const spans = label.querySelectorAll('span');
      if (spans[0]) spans[0].style.background = enabled ? 'var(--accent)' : 'var(--bg3)';
      if (spans[1]) spans[1].style.left = enabled ? '18px' : '2px';
    }
  }

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
    // Reload all playlist sections to reflect updated state
    loadAutoPlaylists();
    loadTagRules();
    if (typeof loadHomeScreen === 'function') loadHomeScreen();
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
    const itemCounts = (r.refresh || {}).item_counts || {};
    const parts = [];
    if (created) parts.push(`${created} created`);
    if (updated) parts.push(`${updated} updated`);
    if (removed) parts.push(`${removed} removed`);
    if (artUpdated) parts.push(`${artUpdated} artwork uploaded`);
    // Show item counts for changed playlists
    const countParts = Object.entries(itemCounts).map(([name, count]) => `${name}: ${count} items`);
    if (countParts.length) parts.push(...countParts);
    toast(parts.length ? `Synced: ${parts.join(', ')}` : 'Playlists up to date');
  } catch (e) {
    toast('Sync failed: ' + e.message, 'error');
  }
}

async function resyncAllPlaylists() {
  const btn = document.getElementById('resync-all-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Syncing...'; }
  try {
    // Full resync runs in the background on the server and returns immediately —
    // rebuilding every playlist can take minutes, which used to trip the gateway
    // timeout. We poll /sync-status for completion.
    const r = await api('/api/smartlists/sync', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({full: true}) });
    if (r.status === 'already_running') {
      toast('A resync is already running — watching progress…', 'info');
    } else {
      toast('Resync started — running in the background', 'info');
    }
    pollResyncStatus();
  } catch (e) {
    toast('Resync failed to start: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Resync All'; }
  }
}

async function pollResyncStatus() {
  const btn = document.getElementById('resync-all-btn');
  let s;
  try {
    s = await api('/api/smartlists/sync-status');
  } catch (e) {
    setTimeout(pollResyncStatus, 5000); // transient error — keep polling
    return;
  }
  if (s.running) {
    setTimeout(pollResyncStatus, 3000);
    return;
  }
  // Finished
  if (btn) { btn.disabled = false; btn.textContent = 'Resync All'; }
  if (s.error) {
    toast('Resync failed: ' + s.error, 'error');
  } else if (s.summary) {
    const parts = [];
    if (s.summary.created) parts.push(`${s.summary.created} created`);
    if (s.summary.updated) parts.push(`${s.summary.updated} updated`);
    if (s.summary.removed) parts.push(`${s.summary.removed} removed`);
    if (s.summary.errors) parts.push(`${s.summary.errors} errors`);
    const t = s.summary.elapsed_seconds != null ? ` in ${s.summary.elapsed_seconds}s` : '';
    toast(`Resync complete${t}: ${parts.join(', ') || 'all up to date'}`, s.summary.errors ? 'error' : 'success');
  } else {
    toast('Resync complete');
  }
  loadAutoPlaylists();
  loadTagRules();
  loadHomeScreen();
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
    ? `<img src="${_imgUrl(item.poster_path, 'w200')}" alt="" loading="lazy">`
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
    const summary = `Added ${r.added} to ${label}${r.already_exists ? `, ${r.already_exists} already existed` : ''}${r.failed ? `, ${r.failed} failed` : ''}`;
    toast(r.detail ? `${summary}. ${r.detail}` : summary, r.failed ? 'error' : 'success', r.detail ? 8000 : undefined);
    if (btn) btn.textContent = `Done (${r.added} added)`;
  } catch (err) {
    toast(err.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = `Add all to ${label}`; }
  }
}

// Keep old function name for backwards compat
function addAllMissingToRadarr() { addAllMissingToArr('radarr'); }

// ── YOUTUBE ──────────────────────────────────────────────────────────────

async function loadYouTubePage() {
  const box = document.getElementById('yt-channels');
  const warn = document.getElementById('yt-unavailable');
  const addCard = document.getElementById('yt-add-card');
  try {
    const st = await api('/api/youtube/status');

    // Blockers the user can't fix from here.
    const blockers = [];
    if (!st.yt_dlp_available) blockers.push('This Tentacle image has no yt-dlp — pull a newer image.');
    if (!st.media_root_mounted) blockers.push(`<code>${escapeAttr(st.media_root)}</code> is not mounted. Add it to Tentacle&rsquo;s volumes and recreate the container.`);
    warn.style.display = blockers.length ? '' : 'none';
    if (blockers.length) warn.querySelector('.card-body').innerHTML = blockers.join('<br>');

    // There is no turn-on step: adding a channel is the decision, and the
    // address every pointer file carries is worked out then. The advanced
    // block shows what was worked out, and lets it be overridden.
    addCard.style.display = '';
    ytShowAddress('yt-address', st);
    const summary = document.getElementById('yt-address-summary');
    if (summary) {
      const r = st.reachable;
      summary.textContent = st.base_url
        ? `— ${st.base_url}${r ? (r.ok ? ' ✓' : ' ✕') : ''}`
        : (st.detected && st.detected.url ? `— will use ${st.detected.url}` : '— not worked out yet');
      summary.style.color = (r && !r.ok) ? 'var(--red)' : 'var(--text3)';
    }

    await loadYouTubeChannels();

    // An index started earlier may still be running — pick the progress back up.
    try {
      const rs = await api('/api/youtube/refresh/status');
      if (rs.running && !_ytPoll) ytStartPolling();
    } catch { /* not fatal */ }
  } catch (e) {
    box.innerHTML = `<div class="empty-state"><p>Could not load: ${escapeAttr(e.message)}</p></div>`;
  }
}

async function ytDiagnose() {
  const card = document.getElementById('yt-diag-card');
  const box = document.getElementById('yt-diag');
  card.style.display = '';
  box.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  try {
    const d = await api('/api/youtube/diagnose');
    const rows = d.checks.map(c => `
      <div style="display:flex;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);align-items:flex-start">
        <span style="color:var(--${c.ok ? 'green' : 'red'});font-weight:700;width:16px;flex-shrink:0">${c.ok ? '✓' : '✕'}</span>
        <div style="flex:1">
          <div style="font-size:13px;font-weight:${c.ok ? '400' : '600'}">${escapeAttr(c.name)}</div>
          <div style="font-size:12px;color:var(--text3);font-family:'DM Mono',monospace;word-break:break-all">${escapeAttr(String(c.detail))}</div>
          ${!c.ok && c.fix ? `<div style="font-size:12px;color:var(--text2);margin-top:4px">→ ${escapeAttr(c.fix)}</div>` : ''}
        </div>
      </div>`).join('');
    const head = d.blocking
      ? `<div style="font-size:13px;margin-bottom:10px">First thing to fix: <strong>${escapeAttr(d.blocking.name)}</strong></div>`
      : '<div style="font-size:13px;margin-bottom:10px;color:var(--green)">Everything checks out. If a video still won&rsquo;t play, check that the Tentacle address works <em>from the Jellyfin server</em>.</div>';
    box.innerHTML = head + rows;
  } catch (e) {
    box.innerHTML = `<div class="empty-state"><p>${escapeAttr(e.message)}</p></div>`;
  }
}

function ytShowAddress(inputId, st) {
  const input = document.getElementById(inputId);
  const status = document.getElementById(inputId + '-status');
  if (!input) return;
  if (document.activeElement !== input) input.value = st.base_url || '';
  if (!status) return;
  const r = st.reachable;
  const found = st.detected && st.detected.url;
  // Offer what was worked out whenever the saved address is missing or does
  // not answer — one click, no LAN address to know.
  const offer = found && (!st.base_url || (r && !r.ok))
    ? ` Detected <code>${escapeAttr(found)}</code> <a href="#" onclick="ytUseAddress('${inputId}', '${escapeJS(found)}');return false">use it</a>`
    : '';
  if (!st.base_url) {
    status.innerHTML = 'Not set.' + offer;
    status.style.color = found ? 'var(--text2)' : 'var(--red)';
    return;
  }
  if (!r) { status.textContent = ''; return; }
  status.innerHTML = (r.ok ? '✓ ' : '✕ ') + escapeAttr(r.detail) + offer;
  status.style.color = r.ok ? 'var(--green)' : 'var(--red)';
}

async function ytUseAddress(inputId, url) {
  const input = document.getElementById(inputId);
  if (input) input.value = url;
  await ytSaveAddress(inputId);
}

async function ytDetectAddress(inputId) {
  const status = document.getElementById(inputId + '-status');
  if (status) { status.textContent = 'Working it out…'; status.style.color = 'var(--text3)'; }
  try {
    const st = await api('/api/youtube/status');
    const found = st.detected && st.detected.url;
    if (found) {
      const input = document.getElementById(inputId);
      if (input) input.value = found;
      if (status) { status.innerHTML = `Detected <code>${escapeAttr(found)}</code> — press Save to use it.`; status.style.color = 'var(--green)'; }
    } else if (st.reachable && st.reachable.ok) {
      if (status) { status.innerHTML = '✓ The saved address already answers as Tentacle.'; status.style.color = 'var(--green)'; }
    } else {
      const tried = ((st.detected && st.detected.tried) || []).map(t => `${escapeAttr(t.url)}: ${escapeAttr(t.detail)}`).join('<br>');
      if (status) { status.innerHTML = 'Nothing answered as Tentacle.' + (tried ? '<br>' + tried : ''); status.style.color = 'var(--red)'; }
    }
  } catch (e) {
    if (status) { status.textContent = e.message; status.style.color = 'var(--red)'; }
  }
}

async function ytSaveAddress(inputId) {
  // Same endpoint the setup card uses: it validates the address, refuses
  // one that cannot be Tentacle, and repoints every existing video. The
  // on/off state is left exactly as it was.
  const base = (document.getElementById(inputId)?.value || '').trim();
  if (!base) { toast('Enter Tentacle\'s own address', 'error'); return; }
  try {
    const st = await api('/api/youtube/status');
    const r = await api('/api/youtube/setup', { method: 'POST', body: { enabled: !!st.enabled, base_url: base } });
    toast(r.rewritten
      ? `Address saved — ${r.rewritten} video${r.rewritten === 1 ? '' : 's'} repointed at it`
      : 'Address saved', r.reachable && !r.reachable.ok ? 'info' : 'success', 6000);
    const fresh = await api('/api/youtube/status');
    ytShowAddress('yt-address', fresh);
    ytShowAddress('settings-tentacle-address', fresh);
  } catch (e) {
    toast(e.message, 'error', 10000);
  }
}

async function loadTentacleAddress() {
  // Settings → Integrations: populate from the same source of truth.
  try {
    const st = await api('/api/youtube/status');
    ytShowAddress('settings-tentacle-address', st);
  } catch { /* the YouTube page shows the same thing */ }
}

async function loadYouTubeChannels() {
  const box = document.getElementById('yt-channels');
  const channels = await api('/api/youtube/channels');
  if (!channels.length) {
    box.innerHTML = '<div class="empty-state"><p>No channels yet. Paste a channel URL above — its newest videos appear in Jellyfin and stream on demand.</p></div>';
    return;
  }
  box.innerHTML = channels.map(c => {
    const blocked = c.blocked_until ? `<span class="badge badge-amber">backing off until ${new Date(c.blocked_until).toLocaleTimeString()}</span>` : '';
    const live = c.live_enabled ? `<span class="badge badge-blue">Live TV</span>${ytLiveState(c)}` : '';
    const shorts = c.include_shorts ? ' · Shorts' : '';
    const replays = c.include_streams ? ' · past live streams' : '';
    // Spelled out rather than hidden behind a hover: "indexed with 1 error"
    // with the reason only in a tooltip meant nobody ever saw the reason.
    const err = c.last_error
      ? `<div style="font-size:11px;margin-top:3px;color:var(--red)">Last run failed: ${escapeAttr(c.last_error)}</div>`
      : '';
    const checked = c.last_checked ? new Date(c.last_checked).toLocaleString() : 'not yet';
    return `<div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)">
      ${c.avatar_url ? `<img src="${escapeAttr(c.avatar_url)}" style="width:40px;height:40px;border-radius:50%;object-fit:cover">` : '<div style="width:40px;height:40px;border-radius:50%;background:var(--bg3)"></div>'}
      <div style="flex:1">
        <div style="font-weight:600">${escapeAttr(c.title)} ${live} ${blocked}</div>
        <div style="font-size:12px;color:var(--text3)">${c.library_count} in library · keeps newest ${c.keep_count}${shorts}${replays} · checked ${escapeAttr(checked)}</div>
        ${err}
        ${ytSkipNote(c)}
      </div>
      <button class="btn btn-secondary btn-sm" onclick="ytDeleteChannel(${c.id}, '${escapeJS(c.title)}')">Remove</button>
    </div>`;
  }).join('');
}

async function ytRefill() {
  const t = toast('Filling playlists…', 'loading', 0);
  try {
    const r = await api('/api/youtube/refill', { method: 'POST' });
    t.remove();
    toast(r.refilled
      ? `Refilled ${r.refilled} playlist${r.refilled === 1 ? '' : 's'}.`
      : (r.still_behind ? 'Jellyfin has not imported all the videos yet — give it a minute and try again.'
                        : 'Every playlist already holds its videos.'),
      r.refilled ? 'success' : 'info', 7000);
    ytDiagnose();
  } catch (e) {
    t.remove();
    toast(e.message, 'error');
  }
}

async function ytReprobe() {
  // Jellyfin probes a .strm once and keeps the answer. When what Tentacle
  // serves changes, an already-scanned item plays by the old description and
  // fails, with nothing visibly wrong on either side.
  const t = toast('Asking Jellyfin to re-check…', 'loading', 0);
  try {
    const r = await api('/api/youtube/reprobe', { method: 'POST' });
    t.remove();
    toast(r.scan_triggered
      ? `Marked ${r.touched} video${r.touched === 1 ? '' : 's'} — Jellyfin is re-scanning. Give it a minute, then try playing again.`
      : `Marked ${r.touched} video${r.touched === 1 ? '' : 's'}, but Jellyfin could not be reached. Check the Jellyfin settings.`,
      r.scan_triggered ? 'success' : 'error', 8000);
  } catch (e) {
    t.remove();
    toast(`Could not re-check: ${e.message || e}`, 'error');
  }
}

async function ytAddChannel() {
  const url = document.getElementById('yt-url').value.trim();
  if (!url) { toast('Paste a channel or playlist URL', 'error'); return; }
  const body = {
    url,
    keep_count: Math.max(1, Math.min(100, parseInt(document.getElementById('yt-keep').value) || 10)),
    include_shorts: document.getElementById('yt-inc-shorts').checked,
    live: document.getElementById('yt-live').checked,
  };
  const t = toast('Looking up channel…', 'loading', 0);
  try {
    const r = await api('/api/youtube/channels', { method: 'POST', body });
    t.remove();
    document.getElementById('yt-url').value = '';
    loadYouTubeChannels();
    // Indexing started on its own; the poll shows progress and says what to
    // do when it finishes. Nothing else is required of the user.
    ytStartPolling();
  } catch (e) {
    t.remove();
    toast(e.message, 'error', 8000);
  }
}

function ytSkipNote(c) {
  // Skips used to be silent, so a setting that excluded every upload looked
  // identical to nothing having been indexed.
  // Not looked at yet: the first index is running (or queued). An empty
  // library is expected for the next couple of minutes, not a problem.
  if (!c.last_checked) {
    return `<div style="font-size:11px;margin-top:3px;color:var(--text3)">Fetching its newest ${c.keep_count} videos…</div>`;
  }
  const skips = c.last_skips || {};
  const keys = Object.keys(skips);
  if (!keys.length && c.library_count > 0) return '';
  const parts = keys.map(k => `${skips[k]}× ${escapeAttr(k)}`).join(' · ');
  const warn = c.library_count === 0;
  // "Nothing to show" has two opposite causes — YouTube listed no uploads, or
  // it listed plenty and something excluded them all. Name which. Uploads are
  // always looked at, so "videos turned off" is not a case any more.
  const listing = c.last_listing || {};
  let why = '';
  if (warn) {
    if (listing.videos === 0) why = c.include_streams
      ? 'This channel has no uploads, so its past live streams are kept instead. '
      : 'This channel has no uploads or past live streams yet. ';
    else why = 'Nothing available for a home row. ';
  }
  return `<div style="font-size:11px;margin-top:3px;color:var(--${warn ? 'red' : 'text3'})">`
    + why
    + (parts ? `Skipped: ${parts}` : '')
    + (warn ? ` <a href="#" onclick="ytDiagnose();return false">why?</a>` : '')
    + '</div>';
}

function ytLiveState(c) {
  // A Live TV channel with nothing on returns "not streaming" and Jellyfin
  // shows that as a playback error, so say so here before anyone presses play.
  if (c.live_now) return ' <span class="badge badge-red">● LIVE</span>';
  if (c.upcoming) return ` <span class="badge badge-amber">${c.upcoming} upcoming</span>`;
  return ' <span class="badge badge-gray">nothing on</span>';
}

async function ytDeleteChannel(id, title) {
  // One action, and it does the whole thing: the channel, its videos, its
  // playlist and its home rows all go. A channel half-removed — gone from the
  // list but still in Jellyfin — is the state that used to need explaining.
  if (!confirm(`Remove "${title}"?\n\nIts videos, playlist and home row are removed from Jellyfin too.`)) return;
  try {
    const r = await api(`/api/youtube/channels/${id}`, { method: 'DELETE' });
    toast(`Removed ${title}${r.files_deleted ? ` — ${r.files_deleted} file(s) cleaned up` : ''}`);
    loadYouTubeChannels();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function ytRefreshNow() {
  try {
    await api('/api/youtube/refresh', { method: 'POST' });
  } catch (e) {
    toast(e.message, 'error', 8000);
    return;
  }
  // Indexing takes minutes (YouTube rate-limits guest lookups, so videos are
  // fetched a few seconds apart), so it runs in the background and we poll.
  ytStartPolling();
}

let _ytPoll = null;   // the progress poll's interval handle, while an index runs

function ytStartPolling() {
  if (_ytPoll) clearInterval(_ytPoll);
  const t = toast('Indexing…', 'loading', 0);
  const tick = async () => {
    let st;
    try {
      st = await api('/api/youtube/refresh/status');
    } catch {
      return;
    }
    if (st.running) {
      const chans = st.channels_total > 1 ? ` (${st.channels_done + 1}/${st.channels_total})` : '';
      // Progress in the user's terms — how many of the newest N are in hand —
      // matching the channel card's "Fetching its newest N videos…". What is
      // being looked at internally (a margin past N, a peek at the streams
      // tab) is not a number anyone asked for.
      const line = st.keep
        ? `Fetching newest videos for ${escapeAttr(st.channel || 'channel')}${chans} — ${Math.min(st.kept || 0, st.keep)} of ${st.keep}`
        : `${escapeAttr(st.channel || 'Working')}${chans}…`;
      t.innerHTML = `<span class="toast-spinner"></span> ${line}`;
      return;
    }
    clearInterval(_ytPoll); _ytPoll = null; t.remove();
    if (st.errors) {
      toast(`Indexed with ${st.errors} error(s): ${escapeAttr(st.error_detail || '')}`, 'error', 10000);
    } else {
      // "filling" means Jellyfin is still importing: the playlist is topped
      // up in the background as videos land, and the row appears on its own.
      const added = st.new ? `${st.new} new video${st.new === 1 ? '' : 's'} added.` : 'Nothing new since last time.';
      toast(st.filling
        ? `${added} Jellyfin is still picking them up — the playlist fills in on its own over the next minute or two.`
        : `${added}${st.new ? ' Playlist is ready; add it as a row on the Home Screen tab.' : ''}`,
        'success', 10000);
    }
    loadYouTubeChannels();
  };
  tick();
  _ytPoll = setInterval(tick, 2000);
}

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
    const itemCountEl = document.getElementById('home-hero-item-count');
    if (itemCountEl && config.hero) {
      itemCountEl.value = String(config.hero.item_count || 10);
    }
    const logoCheckbox = document.getElementById('home-hero-require-logo');
    if (logoCheckbox && config.hero) {
      logoCheckbox.checked = config.hero.require_logo !== false;
    }
    const trailerCheckbox = document.getElementById('home-hero-require-trailer');
    if (trailerCheckbox && config.hero) {
      trailerCheckbox.checked = config.hero.require_trailer === true;
    }
    const trailerAudioCheckbox = document.getElementById('home-hero-trailer-audio');
    if (trailerAudioCheckbox && config.hero) {
      trailerAudioCheckbox.checked = config.hero.trailer_audio !== false;
    }

    // Merge Continue Watching + Next Up toggle
    const mergeCheckbox = document.getElementById('merge-continue-checkbox');
    if (mergeCheckbox) {
      const mergeOn = config.merge_continue_watching === true;
      mergeCheckbox.checked = mergeOn;
      updateMergeContinueSlider(mergeOn);
    }

    // Card focus previews: all / local_only / off (absent = all)
    const previewsSelect = document.getElementById('card-previews-select');
    if (previewsSelect) {
      previewsSelect.value = ['all', 'local_only', 'off'].includes(config.card_previews) ? config.card_previews : 'all';
    }

    if (!homeRows.length) {
      listEl.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text3);font-size:13px">No rows configured — the default Jellyfin home screen will be used.</div>';
    } else {
      renderHomeRows();
    }

    // Load notification preference
    loadNotificationState();

    // Load toolbar buttons
    renderToolbarButtons(config.toolbar || [
      {id: 'search', enabled: true},
      {id: 'discover', enabled: true},
      {id: 'activity', enabled: true},
      {id: 'favorites', enabled: true},
      {id: 'libraries', enabled: true},
      {id: 'shuffle', enabled: false},
      {id: 'genres', enabled: false},
    ]);
  } catch (e) {
    listEl.innerHTML = '<div class="empty-state" style="padding:24px"><p>Failed to load home config</p></div>';
  }
}

function rowKey(row) {
  if (row.type === 'builtin') return `builtin:${row.section_id}`;
  return `playlist:${row.playlist_id}`;
}

const TOOLBAR_LABELS = {search: 'Search', discover: 'Discover', activity: 'Activity', favorites: 'Favorites', libraries: 'Libraries', shuffle: 'Shuffle (Android TV)', genres: 'Genres (Android TV)'};
const TOOLBAR_ICONS = {
  search: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>',
  discover: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12 10.9c-.61 0-1.1.49-1.1 1.1s.49 1.1 1.1 1.1c.61 0 1.1-.49 1.1-1.1s-.49-1.1-1.1-1.1zM12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm2.19 12.19L6 18l3.81-8.19L18 6l-3.81 8.19z"/></svg>',
  activity: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M11 7h2v2h-2zm0 4h2v6h-2zm1-9C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z"/></svg>',
  favorites: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>',
  libraries: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-8 12.5v-9l6 4.5-6 4.5z"/></svg>',
  shuffle: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zM14.83 13.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg>',
  genres: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M8.11,19.45C5.94,18.65 4.22,16.78 3.71,14.35L2.05,6.54C1.81,5.46 2.5,4.4 3.58,4.17L13.35,2.1L13.38,2.09C14.45,1.88 15.5,2.57 15.72,3.63L16.07,5.3L20.42,6.23H20.45C21.5,6.47 22.18,7.53 21.96,8.59L20.3,16.41C19.5,20.18 15.78,22.6 12,21.79C10.42,21.46 9.08,20.61 8.11,19.45M20,8.18L10.23,6.1L8.57,13.92V13.95C8,16.63 9.73,19.27 12.42,19.84C15.11,20.41 17.77,18.69 18.34,16L20,8.18M16,16.5C15.37,17.57 14.11,18.16 12.83,17.89C11.56,17.62 10.65,16.57 10.5,15.34L16,16.5M8.47,5.17L4,6.13L5.66,13.94L5.67,13.97C5.82,14.68 6.12,15.32 6.53,15.87C6.43,15.1 6.45,14.3 6.62,13.5L7.05,11.5C6.6,11.42 6.21,11.17 6,10.81C6.06,10.2 6.56,9.66 7.25,9.5C7.33,9.5 7.4,9.5 7.5,9.5L8.28,5.69C8.32,5.5 8.38,5.33 8.47,5.17M15.03,12.23C15.35,11.7 16.03,11.42 16.72,11.57C17.41,11.71 17.91,12.24 18,12.86C17.67,13.38 17,13.66 16.3,13.5C15.61,13.37 15.11,12.84 15.03,12.23M10.15,11.19C10.47,10.66 11.14,10.38 11.83,10.53C12.5,10.67 13.03,11.21 13.11,11.82C12.78,12.34 12.11,12.63 11.42,12.5C10.73,12.33 10.23,11.8 10.15,11.19M11.97,4.43L13.93,4.85L13.77,4.05L11.97,4.43Z"/></svg>',
};
let toolbarButtons = [];

// ── List reordering that works with a mouse AND a finger ──────────────────
// HTML5 drag-and-drop never fires on touch screens (and is flaky on the ones
// that half-support it), so rows are moved with Pointer Events instead: press
// the grip (any pointer) or the row itself (mouse only, so a swipe still
// scrolls the list), a floating copy follows the pointer, and the row's
// placeholder is re-slotted as neighbours are crossed. onReorder(from, to)
// fires once, on release, only when the position actually changed.
function makeSortable(listEl, itemSelector, onReorder) {
  if (!listEl) return;
  listEl._sortableOnReorder = onReorder;   // re-render keeps the same element; refresh the callback
  if (listEl._sortableBound) return;
  listEl._sortableBound = true;

  let drag = null;
  const items = () => Array.from(listEl.querySelectorAll(itemSelector));

  listEl.addEventListener('pointerdown', e => {
    if (drag) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const item = e.target.closest(itemSelector);
    if (!item || !listEl.contains(item)) return;
    const onGrip = !!e.target.closest('.row-grip');
    if (!onGrip && (e.pointerType !== 'mouse' || e.target.closest('input,select,button,label,a,textarea'))) return;
    e.preventDefault();
    const rect = item.getBoundingClientRect();
    const ghost = item.cloneNode(true);
    ghost.classList.add('sortable-ghost');
    ghost.style.width = rect.width + 'px';
    ghost.style.transform = `translate(${rect.left}px, ${rect.top}px)`;
    document.body.appendChild(ghost);
    item.classList.add('sorting-placeholder');
    drag = { el: item, ghost, left: rect.left, offsetY: e.clientY - rect.top, startIdx: items().indexOf(item), pointerId: e.pointerId };
    try { listEl.setPointerCapture(e.pointerId); } catch (_) {}
  });

  listEl.addEventListener('pointermove', e => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    e.preventDefault();
    drag.ghost.style.transform = `translate(${drag.left}px, ${e.clientY - drag.offsetY}px)`;
    // Slot the placeholder before the first neighbour whose midpoint is below the pointer
    const others = items().filter(el => el !== drag.el);
    const next = others.find(el => { const r = el.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
    if (next) { if (drag.el.nextElementSibling !== next) listEl.insertBefore(drag.el, next); }
    else if (listEl.lastElementChild !== drag.el) listEl.appendChild(drag.el);
    // Nudge the page when dragging near the top or bottom edge of the screen
    const edge = 56;
    if (e.clientY < edge) window.scrollBy(0, -12);
    else if (e.clientY > window.innerHeight - edge) window.scrollBy(0, 12);
  });

  const finish = e => {
    if (!drag || (e && e.pointerId != null && e.pointerId !== drag.pointerId)) return;
    const { el, ghost, startIdx } = drag;
    drag = null;
    ghost.remove();
    el.classList.remove('sorting-placeholder');
    const endIdx = items().indexOf(el);
    if (endIdx >= 0 && endIdx !== startIdx) listEl._sortableOnReorder(startIdx, endIdx);
  };
  listEl.addEventListener('pointerup', finish);
  listEl.addEventListener('pointercancel', finish);
  listEl.addEventListener('lostpointercapture', finish);
  listEl.addEventListener('contextmenu', e => { if (drag) e.preventDefault(); });
}

function renderToolbarButtons(buttons) {
  toolbarButtons = buttons;
  const listEl = document.getElementById('toolbar-buttons-list');
  if (!listEl) return;

  listEl.innerHTML = toolbarButtons.map((btn, i) => `
    <div class="home-row-item" data-toolbar-idx="${i}" style="padding:8px 12px;border-radius:6px;margin-bottom:0">
      <span class="row-grip" aria-label="Drag to reorder" title="Drag to reorder">⠿</span>
      <span style="display:flex;align-items:center;color:var(--text2);flex-shrink:0">${TOOLBAR_ICONS[btn.id] || ''}</span>
      <span class="row-name">${TOOLBAR_LABELS[btn.id] || btn.id}</span>
      <label style="position:relative;display:inline-block;width:36px;height:20px;flex-shrink:0;margin-left:auto">
        <input type="checkbox" ${btn.enabled ? 'checked' : ''} onchange="toggleToolbarButton(${i}, this.checked)" style="opacity:0;width:0;height:0">
        <span style="position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:${btn.enabled ? 'var(--accent)' : 'var(--bg3)'};border-radius:10px;transition:.2s"></span>
        <span style="position:absolute;height:14px;width:14px;left:${btn.enabled ? '19px' : '3px'};bottom:3px;background:white;border-radius:50%;transition:.2s"></span>
      </label>
    </div>
  `).join('');

  makeSortable(listEl, '.home-row-item', (from, to) => {
    const item = toolbarButtons.splice(from, 1)[0];
    toolbarButtons.splice(to, 0, item);
    renderToolbarButtons(toolbarButtons);
    saveToolbarConfig();
  });
}

function toggleToolbarButton(idx, enabled) {
  toolbarButtons[idx].enabled = enabled;
  renderToolbarButtons(toolbarButtons);
  saveToolbarConfig();
}

async function saveToolbarConfig() {
  try {
    const r = await api('/api/smartlists/toolbar', {
      method: 'POST',
      body: { buttons: toolbarButtons },
    });
    if (r.success) pushHomeConfig();
    else toast(r.message || 'Failed to save toolbar', 'error');
  } catch (e) {
    toast('Failed to save toolbar config', 'error');
  }
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
      <input type="number" min="5" max="30" value="${Math.min(row.max_items || 20, 30)}"
        onclick="event.stopPropagation()" onmousedown="event.stopPropagation()"
        onchange="saveRowMaxItemsByKey('${key}', this.value)"
        style="width:52px;padding:3px 4px;font-size:11px;text-align:center;background:var(--bg1);border:1px solid var(--border);border-radius:4px;color:var(--text);cursor:text"
        title="Max items in this row">`;
    // Card shape. Some content has no portrait artwork — a YouTube thumbnail
    // in a poster slot is cropped to a strip of its middle — so each row picks.
    const shapeSelect = isBuiltin ? '' : `
      <select onclick="event.stopPropagation()" onmousedown="event.stopPropagation()"
        onchange="saveRowShapeByKey('${key}', this.value)"
        style="padding:3px 4px;font-size:11px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;color:var(--text);cursor:pointer"
        title="Card shape for this row">
        <option value="poster" ${row.shape !== 'wide' ? 'selected' : ''}>Poster</option>
        <option value="wide" ${row.shape === 'wide' ? 'selected' : ''}>Wide</option>
      </select>`;
    return `
    <div class="home-row-item" data-idx="${i}">
      <span style="color:var(--text3);font-size:11px;width:20px;text-align:center;flex-shrink:0">${i + 1}</span>
      <span class="row-grip" aria-label="Drag to reorder" title="Drag to reorder">&#x2630;</span>
      <span class="row-name">${row.display_name}</span>
      <div class="row-controls">
        ${shapeSelect}
        ${maxItemsInput}
        ${badge}
        <button onclick="event.stopPropagation();removeHomeRowByKey('${key}')"
          style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:14px;padding:6px 8px;border-radius:4px"
          onmouseover="this.style.color='var(--red)'" onmouseout="this.style.color='var(--text3)'"
          title="Remove row">&#10005;</button>
      </div>
    </div>`;
  }).join('');

  makeSortable(listEl, '.home-row-item', reorderHomeRows);
}

async function reorderHomeRows(fromIdx, toIdx) {
  if (fromIdx === toIdx) return;
  const moved = homeRows.splice(fromIdx, 1)[0];
  homeRows.splice(toIdx, 0, moved);
  for (let i = 0; i < homeRows.length; i++) homeRows[i].order = i + 1;
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
  const requireTrailer = document.getElementById('home-hero-require-trailer')?.checked ?? false;
  const trailerAudio = document.getElementById('home-hero-trailer-audio')?.checked ?? true;
  const itemCount = parseInt(document.getElementById('home-hero-item-count')?.value) || 10;
  try {
    const r = await api('/api/smartlists/hero-sort', {
      method: 'POST',
      body: { sort_by: sortBy, sort_order: sortOrder, require_logo: requireLogo, require_trailer: requireTrailer, trailer_audio: trailerAudio, item_count: itemCount },
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

async function saveRowShapeByKey(key, shape) {
  try {
    const resp = await api('/api/smartlists/row-shape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row_key: key, shape }),
    });
    const row = homeRows.find(r => rowKey(r) === key);
    if (row) row.shape = shape;
    if (resp.notified === false) {
      // Saved, but clients that rely on the plugin broadcast (Android TV) won't see it.
      toast('Saved, but the Jellyfin plugin was not notified — ' + (resp.error || 'check the plugin is installed')
        + '. Android TV will not update until this is fixed.', 'warning', 8000);
    } else {
      toast(shape === 'wide' ? 'Row now uses wide cards' : 'Row now uses poster cards');
    }
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
    const youtube = playlists.filter(p => p.is_youtube);
    const others = playlists.filter(p => !p.is_youtube);
    if (youtube.length) {
      select.innerHTML += '<optgroup label="YouTube Channels">';
      for (const p of youtube) {
        select.innerHTML += `<option value="playlist:${p.playlist_id}">${p.name}</option>`;
      }
      select.innerHTML += '</optgroup>';
    }
    if (others.length) {
      select.innerHTML += '<optgroup label="Tentacle Playlists">';
      for (const p of others) {
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
          <div class="list-actions" style="display:flex;gap:6px;align-items:center">
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
let _matchCountTimer = null;

async function _ensureConditionOptions() {
  if (!_conditionOptions) {
    try { _conditionOptions = await api('/api/tags/condition-options'); }
    catch (_) { _conditionOptions = { sources: [], lists: [], genres: [] }; }
  }
  return _conditionOptions;
}

function _renderGenreChips(selected = []) {
  const container = document.getElementById('tr-genre-chips');
  if (!container) return;
  const genres = _conditionOptions?.genres || [];
  const selSet = new Set(selected.map(s => s.toLowerCase()));
  container.innerHTML = genres.map(g => {
    const sel = selSet.has(g.toLowerCase()) ? ' selected' : '';
    return `<span class="genre-chip${sel}" data-genre="${escapeAttr(g)}" onclick="toggleGenreChip(this)">${g}</span>`;
  }).join('');
  if (!genres.length) {
    container.innerHTML = '<span style="font-size:12px;color:var(--text3)">No genres found</span>';
  }
}

function toggleGenreChip(el) {
  el.classList.toggle('selected');
  // Show AND/OR toggle when 2+ genres selected
  const count = document.querySelectorAll('#tr-genre-chips .genre-chip.selected').length;
  const logicEl = document.getElementById('tr-genre-logic');
  if (logicEl) logicEl.style.display = count >= 2 ? '' : 'none';
  _scheduleMatchCount();
}

function _getSelectedGenres() {
  return Array.from(document.querySelectorAll('#tr-genre-chips .genre-chip.selected'))
    .map(el => el.dataset.genre);
}

function _scheduleMatchCount() {
  clearTimeout(_matchCountTimer);
  _matchCountTimer = setTimeout(_updateMatchCount, 400);
}

async function _updateMatchCount() {
  const el = document.getElementById('tr-match-count');
  if (!el || document.getElementById('tr-simple-filters').style.display === 'none') {
    if (el) el.style.display = 'none';
    return;
  }
  const conditions = _buildFriendlyConditions();
  if (!conditions.length) { el.style.display = 'none'; return; }
  const apply_to = document.getElementById('tr-apply-to').value;
  try {
    const r = await api('/api/smartlists/preview-count', { method: 'POST', body: { apply_to, conditions } });
    if (r.count === -1) {
      el.innerHTML = 'Preview not available for provider/list filters';
      el.style.display = '';
    } else {
      el.innerHTML = `<span class="count-num">${r.count}</span> item${r.count !== 1 ? 's' : ''} match`;
      el.style.display = '';
    }
  } catch { el.style.display = 'none'; }
}

function _buildFriendlyConditions() {
  const conditions = [];
  const contentSource = document.querySelector('input[name="tr-content-source"]:checked')?.value || 'all';
  if (contentSource === 'source') {
    const src = document.getElementById('tr-source-pick').value;
    if (src) conditions.push({ field: 'source', operator: 'equals', value: src });
  } else if (contentSource === 'list') {
    const lst = document.getElementById('tr-list-pick').value;
    if (lst) conditions.push({ field: 'list', operator: 'equals', value: lst });
  } else if (contentSource === 'downloaded') {
    conditions.push({ field: 'downloaded', operator: 'equals', value: 'yes' });
  }
  const genres = _getSelectedGenres();
  const genreLogic = (genres.length >= 2 && document.getElementById('tr-genre-logic')?.value) || 'and';
  for (const g of genres) {
    conditions.push({ field: 'genre', operator: 'contains', value: g, genre_logic: genreLogic });
  }
  const rating = document.getElementById('tr-filter-rating').value.trim();
  if (rating) conditions.push({ field: 'rating', operator: 'greater_than', value: rating });
  const year = document.getElementById('tr-filter-year').value.trim();
  if (year) conditions.push({ field: 'year', operator: 'greater_than', value: year });
  return conditions;
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
  document.getElementById('tr-filter-rating').value = '';
  document.getElementById('tr-filter-year').value = '';
  const glEl = document.getElementById('tr-genre-logic');
  if (glEl) { glEl.value = 'and'; glEl.style.display = 'none'; }
  document.getElementById('tr-source-section').style.display = '';
  document.getElementById('tr-simple-filters').style.display = '';
  document.getElementById('tr-advanced-section').style.display = 'none';
  document.getElementById('tr-advanced-label').textContent = 'Show advanced filters';
  document.getElementById('tr-match-count').style.display = 'none';

  // Populate source/list dropdowns + genre chips
  await _ensureConditionOptions();
  const srcPick = document.getElementById('tr-source-pick');
  srcPick.innerHTML = '<option value="">Select...</option>' +
    (_conditionOptions?.sources || []).map(s => `<option value="${escapeAttr(s)}">${s}</option>`).join('');
  const listPick = document.getElementById('tr-list-pick');
  listPick.innerHTML = '<option value="">Select...</option>' +
    (_conditionOptions?.lists || []).map(l => `<option value="${escapeAttr(l.tag)}">${l.name}</option>`).join('');
  _renderGenreChips([]);

  showModal('modal-tag-rule');
}

function onContentSourceChange() {
  const selected = document.querySelector('input[name="tr-content-source"]:checked')?.value || 'all';
  document.getElementById('tr-source-pick').style.display = selected === 'source' ? '' : 'none';
  document.getElementById('tr-list-pick').style.display = selected === 'list' ? '' : 'none';
  _scheduleMatchCount();
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

  // Always populate advanced mode conditions
  const condEl = document.getElementById('tr-conditions');
  condEl.innerHTML = '';
  for (const cond of (rule.conditions || [])) addRuleCondition(cond.field, cond.operator, cond.value);

  await _ensureConditionOptions();

  // Check if conditions can be shown in friendly mode
  const conds = rule.conditions || [];
  const canUseFriendly = conds.every(c => ['genre', 'rating', 'year', 'source', 'list', 'downloaded'].includes(c.field));

  if (canUseFriendly) {
    // Populate friendly builder from conditions
    const radios = document.querySelectorAll('input[name="tr-content-source"]');
    const srcCond = conds.find(c => c.field === 'source');
    const listCond = conds.find(c => c.field === 'list');
    const dlCond = conds.find(c => c.field === 'downloaded');
    for (const r of radios) {
      r.checked = srcCond ? r.value === 'source' : listCond ? r.value === 'list' : dlCond ? r.value === 'downloaded' : r.value === 'all';
    }
    const srcPick = document.getElementById('tr-source-pick');
    srcPick.innerHTML = '<option value="">Select...</option>' +
      (_conditionOptions?.sources || []).map(s => `<option value="${escapeAttr(s)}">${s}</option>`).join('');
    if (srcCond) { srcPick.value = srcCond.value; srcPick.style.display = ''; }
    else srcPick.style.display = 'none';
    const listPick = document.getElementById('tr-list-pick');
    listPick.innerHTML = '<option value="">Select...</option>' +
      (_conditionOptions?.lists || []).map(l => `<option value="${escapeAttr(l.tag)}">${l.name}</option>`).join('');
    if (listCond) { listPick.value = listCond.value; listPick.style.display = ''; }
    else listPick.style.display = 'none';

    const selectedGenres = conds.filter(c => c.field === 'genre').map(c => c.value);
    _renderGenreChips(selectedGenres);

    // Restore AND/OR genre logic
    const genreLogicVal = conds.find(c => c.field === 'genre' && c.genre_logic)?.genre_logic || 'and';
    const glEl = document.getElementById('tr-genre-logic');
    if (glEl) { glEl.value = genreLogicVal; glEl.style.display = selectedGenres.length >= 2 ? '' : 'none'; }

    const ratingCond = conds.find(c => c.field === 'rating');
    document.getElementById('tr-filter-rating').value = ratingCond ? ratingCond.value : '';
    const yearCond = conds.find(c => c.field === 'year');
    document.getElementById('tr-filter-year').value = yearCond ? yearCond.value : '';

    document.getElementById('tr-source-section').style.display = '';
    document.getElementById('tr-simple-filters').style.display = '';
    document.getElementById('tr-advanced-section').style.display = 'none';
    document.getElementById('tr-advanced-label').textContent = 'Show advanced filters';
    _scheduleMatchCount();
  } else {
    // Complex conditions — use advanced mode
    document.getElementById('tr-source-section').style.display = 'none';
    document.getElementById('tr-simple-filters').style.display = 'none';
    document.getElementById('tr-advanced-section').style.display = '';
    document.getElementById('tr-advanced-label').textContent = 'Use simple builder';
  }
  document.getElementById('tr-match-count').style.display = 'none';

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
    conditions.push(..._buildFriendlyConditions());
  }

  if (!conditions.length) { toast('At least one filter is required', 'error'); return; }

  try {
    if (ruleId) {
      await api(`/api/tags/rules/${ruleId}`, { method: 'PUT', body: { name, output_tag, apply_to, active, conditions } });
    } else {
      await api('/api/tags/rules', { method: 'POST', body: { name, output_tag, apply_to, active, conditions } });
    }
    _tagRulesCache = [];
    closeModal('modal-tag-rule');
    // Fast sync: only this one playlist (not all 20+)
    const r = await api('/api/smartlists/sync-one', { method: 'POST', body: { name, output_tag, apply_to, conditions } });
    loadTagRules();
    loadAutoPlaylists();
    if (typeof loadHomeScreen === 'function') loadHomeScreen();
    if (r.item_count !== undefined) {
      toast(`${r.is_new ? 'Created' : 'Updated'}: ${name} — ${r.item_count} items`);
    } else {
      toast(r.error || 'Sync failed', 'error');
    }
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
    loadAutoPlaylists();
    if (typeof loadHomeScreen === 'function') loadHomeScreen();
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
    // A series' downloaded copy comes from Sonarr, not Radarr.
    const hasRadarr = sources.some(s => s.source === 'radarr' || s.source === 'sonarr');
    const hasVod = sources.some(s => s.source.startsWith('provider_'));
    const sourceCards = sources.map(s => {
      const isRadarr = s.source === 'radarr' || s.source === 'sonarr';
      const sourceLabel = isRadarr ? `Downloaded (${s.source === 'sonarr' ? 'Sonarr' : 'Radarr'})` :
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
              ${hasVod ? `<button class="btn btn-secondary btn-sm" onclick="resolveDup(${dup.id}, 'keep_vod', '${dup.media_type}')">Keep VOD</button>` : ''}
              <button class="btn btn-secondary btn-sm" onclick="resolveDup(${dup.id}, 'keep_both')">Keep Both</button>
            </div>` : ''}
        </div>
      </div>`;
  }).join('');
}

function resolveDup(id, resolution, mediaType) {
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
    warnEl.textContent = mediaType === 'series'
      ? 'The series will be deleted from Sonarr and its downloaded episodes will be permanently removed.'
      : 'The movie will be deleted from Radarr and the downloaded file will be permanently removed.';
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
    if (_logEventSource && _logEventSource.readyState === EventSource.CLOSED) return;
    _logEventSource.close();
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

document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => { if (state.currentUser?.is_admin) initLogViewer(); }, 500);
});

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
  _pollActivity();
  _activityPollTimer = setInterval(_pollActivity, 3000);
}

// The tab polls every 3 s, but with a slow Radarr/Sonarr one answer can take a
// minute. Polls used to pile up behind it (a dozen requests in flight, which
// also held the browser's connections every other page needs); now a poll is
// skipped while the last one is out — unless it has been out for
// POLL_STALE_MS, so one request that never answers can't stop the tab updating.
// loadActivity() drops an answer older than one already shown.
let _activityPollSince = 0;
let _activityPollToken = 0;
function _pollActivity() {
  if (_activityPollSince && Date.now() - _activityPollSince < POLL_STALE_MS) return;
  const tok = ++_activityPollToken;
  _activityPollSince = Date.now();
  loadActivity().finally(() => { if (tok === _activityPollToken) _activityPollSince = 0; });
}

function stopActivityPolling() {
  if (_activityPollTimer) {
    clearInterval(_activityPollTimer);
    _activityPollTimer = null;
  }
}

// Answers can arrive out of order (a poll and a refresh after an action); an
// older one must not replace a newer one already shown.
let _activitySeq = 0;
let _activityShownSeq = 0;
async function loadActivity() {
  const seq = ++_activitySeq;
  try {
    const data = await api('/api/activity');
    if (seq < _activityShownSeq) return;
    _activityShownSeq = seq;
    _activityData = data;
    // Always update badge count
    const count = (data.downloads || []).length + (data.searching || []).length + (data.unreleased || []).length;
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

// Activity re-renders every 3s. Replacing innerHTML re-created every poster
// <img>, which paints blank for a frame before the cached image decodes — the
// whole tab flashed on each poll. _morphInto patches the live DOM to match the
// new markup instead, so unchanged cards (and their images) are left alone.
function _morphNode(from, to) {
  if (from.nodeType !== to.nodeType || from.nodeName !== to.nodeName) {
    from.parentNode.replaceChild(to, from);
    return;
  }
  if (from.nodeType === 3 || from.nodeType === 8) {
    if (from.nodeValue !== to.nodeValue) from.nodeValue = to.nodeValue;
    return;
  }
  if (from.nodeType !== 1) return;
  for (const a of [...from.attributes]) if (!to.hasAttribute(a.name)) from.removeAttribute(a.name);
  for (const a of [...to.attributes]) if (from.getAttribute(a.name) !== a.value) from.setAttribute(a.name, a.value);
  _morphChildren(from, to);
}
function _morphChildren(from, to) {
  const fc = [...from.childNodes], tc = [...to.childNodes];
  tc.forEach((n, i) => (i < fc.length ? _morphNode(fc[i], n) : from.appendChild(n)));
  fc.slice(tc.length).forEach(n => from.removeChild(n));
}
function _morphInto(el, html) {
  const tpl = document.createElement('div');
  tpl.innerHTML = html;
  _morphChildren(el, tpl);
}

// Posters that failed once render as the placeholder from then on, so a poll
// doesn't retry (and blink) a broken image every 3 seconds.
const _activityBadPosters = new Set();
function _activityPosterFailed(img) {
  _activityBadPosters.add(img.getAttribute('src'));
  img.outerHTML = '<div class="activity-poster-placeholder">◫</div>';
}
function _activityPoster(path) {
  const src = path ? _imgUrl(path, 'w185') : '';
  if (!src || _activityBadPosters.has(src)) return '<div class="activity-poster-placeholder">◫</div>';
  return `<img src="${src}" loading="lazy" onerror="_activityPosterFailed(this)">`;
}

// ── Search again / Remove on a Searching card ──────────────────────────
// State lives here, not in the DOM: the tab re-renders every 3 seconds.
const _actArmed = {};   // key -> ms until which "Remove" waits for its second click
const _actBusy = {};    // key -> 'search' | 'remove' while a request is out
function _actKey(item) { return `${item.media_type}:${item.tmdb_id || 0}:${item.tvdb_id || 0}`; }
function _actItem(key) { return ((_activityData && _activityData.searching) || []).find(x => _actKey(x) === key); }
function _actBody(item) {
  return { media_type: item.media_type === 'series' ? 'series' : 'movie', tmdb_id: item.tmdb_id || 0, tvdb_id: item.tvdb_id || 0 };
}

async function activitySearchAgain(key) {
  const item = _actItem(key);
  if (!item || _actBusy[key]) return;
  _actBusy[key] = 'search'; renderActivity();
  try {
    const r = await api('/api/activity/arr/search', { method: 'POST', body: _actBody(item) });
    toast(r.message || `Searching again for ${item.title}`);
  } catch (e) {
    toast(e.message || 'Search failed', 'error');
  } finally {
    delete _actBusy[key]; renderActivity();
  }
}

// Shows: stop Sonarr looking for the missing episodes, keep everything on
// disk. One missing episode goes straight through; several open a checklist.
async function activityStopMissing(key) {
  const item = _actItem(key);
  if (!item || _actBusy[key]) return;
  const labels = item.missing_labels || [];
  if (labels.length > 1) { _openStopMissing(key, item); return; }
  await _stopMissing(key, item, null);
}

async function _stopMissing(key, item, episodes) {
  _actBusy[key] = 'stop'; renderActivity();
  try {
    const body = _actBody(item);
    if (episodes) body.episodes = episodes;
    const r = await api('/api/activity/arr/stop-missing', { method: 'POST', body });
    toast(r.message || 'Stopped looking');
    closeModal('modal-stop-missing');
    await loadActivity();
  } catch (e) {
    toast(e.message || 'Failed', 'error');
  } finally {
    delete _actBusy[key]; renderActivity();
  }
}

let _smKey = null;
function _openStopMissing(key, item) {
  _smKey = key;
  let m = document.getElementById('modal-stop-missing');
  if (!m) {
    m = document.createElement('div');
    m.className = 'modal-overlay'; m.id = 'modal-stop-missing'; m.style.display = 'none';
    m.innerHTML = `<div class="modal" style="max-width:460px">
      <div class="modal-header"><div class="modal-title" id="sm-title"></div>
        <button class="modal-close" onclick="closeModal('modal-stop-missing')">✕</button></div>
      <div style="padding:0 20px 20px">
        <p class="sm-intro" id="sm-intro"></p>
        <div class="sm-tools"><button class="btn btn-secondary btn-sm" onclick="_smAll(true)">All</button>
          <button class="btn btn-secondary btn-sm" onclick="_smAll(false)">None</button></div>
        <div id="sm-list" class="sm-list" onchange="_smCount()"></div>
        <div class="sm-footer"><button class="btn btn-secondary btn-sm" onclick="closeModal('modal-stop-missing')">Cancel</button>
          <button class="btn btn-primary btn-sm" id="sm-go" onclick="_smGo()"></button></div>
      </div></div>`;
    document.body.appendChild(m);
  }
  document.getElementById('sm-title').textContent = `Stop looking — ${item.title}`;
  const disk = item.episodes_on_disk || 0;
  document.getElementById('sm-intro').textContent =
    `Sonarr stops searching for the ticked episodes. ${disk ? `The ${disk} downloaded episode${disk === 1 ? '' : 's'} stay, and n` : 'N'}ew episodes are still grabbed as they air. You can undo this from Manage Episodes.`;
  document.getElementById('sm-list').innerHTML = (item.missing_labels || []).map(l =>
    `<label class="sm-row"><input type="checkbox" checked value="${escapeAttr(l)}"> ${escapeAttr(l)}</label>`).join('');
  _smCount();
  showModal('modal-stop-missing');
}
function _smChosen() { return [...document.querySelectorAll('#sm-list input:checked')].map(c => c.value); }
function _smAll(on) { document.querySelectorAll('#sm-list input').forEach(c => { c.checked = on; }); _smCount(); }
function _smCount() {
  const n = _smChosen().length, b = document.getElementById('sm-go');
  b.textContent = n === 1 ? `Stop looking for ${_smChosen()[0]}` : `Stop looking for ${n} episodes`;
  b.disabled = n === 0;
}
function _smGo() {
  const item = _actItem(_smKey);
  if (!item) { closeModal('modal-stop-missing'); return; }
  const chosen = _smChosen();
  // Everything ticked = "all missing": also covers episodes past the 50 listed.
  _stopMissing(_smKey, item, chosen.length === (item.missing_labels || []).length ? null : chosen);
}

// "Today 9 PM" / "Tomorrow" / "Thursday" for an air time, in the viewer's zone.
function _airDay(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  const days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (days <= 0) return `Today ${time}`;
  if (days === 1) return `Tomorrow ${time}`;
  return d.toLocaleDateString([], { weekday: 'long' }) + ` ${time}`;
}

function _ago(iso) {
  if (!iso) return '';
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const h = Math.round(mins / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

// ── Why? / Pick a release ──────────────────────────────────────────────
// Radarr/Sonarr's interactive search, summed up: nothing found, usable
// releases, or everything rejected and why — and a list to pick from.
let _rc = null;
function openReleaseCheck(key) {
  const item = _actItem(key);
  if (!item) return;
  _rc = { key, item, data: null, busy: false };
  let m = document.getElementById('modal-release-check');
  if (!m) {
    m = document.createElement('div');
    m.className = 'modal-overlay'; m.id = 'modal-release-check'; m.style.display = 'none';
    m.innerHTML = `<div class="modal" style="max-width:720px">
      <div class="modal-header"><div class="modal-title" id="rc-title"></div>
        <button class="modal-close" onclick="closeModal('modal-release-check')">✕</button></div>
      <div style="padding:0 20px 20px">
        <p class="rc-summary" id="rc-summary"></p>
        <div class="rc-meta" id="rc-meta"></div>
        <div id="rc-list" class="rc-list"></div>
      </div></div>`;
    document.body.appendChild(m);
  }
  document.getElementById('rc-title').textContent = `Why hasn't ${item.title} downloaded?`;
  showModal('modal-release-check');
  _rcLoad(false);
}

async function _rcLoad(fresh) {
  const { item } = _rc;
  const sum = document.getElementById('rc-summary'), meta = document.getElementById('rc-meta'), list = document.getElementById('rc-list');
  sum.textContent = 'Asking your indexers… this can take up to a minute.';
  meta.innerHTML = ''; list.innerHTML = '';
  try {
    const d = await api('/api/activity/arr/check', { method: 'POST', body: { ..._actBody(item), fresh } });
    if (!_rc || _rc.item !== item) return;
    _rc.data = d;
    sum.textContent = d.summary;
    meta.innerHTML = `${d.scope ? `${escapeAttr(d.scope)} · ` : ''}Checked ${escapeAttr(_ago(d.checked_at))}
      · <a href="#" onclick="event.preventDefault();_rcLoad(true)">Check again</a>`;
    list.innerHTML = (d.releases || []).map((r, i) => `<div class="rc-row${r.rejected ? ' rejected' : ''}">
        <div class="rc-info">
          <div class="rc-title">${escapeAttr(r.title)}</div>
          <div class="rc-facts">${[r.quality, _fmtBytes(r.size_bytes), r.protocol === 'torrent' && r.seeders != null ? `${r.seeders} seeders` : r.protocol,
            r.languages, r.indexer, r.age_days != null ? `${r.age_days} d old` : ''].filter(Boolean).map(escapeAttr).join(' · ')}</div>
          ${r.rejected ? `<div class="rc-why" title="${escapeAttr((r.raw_reasons || []).join('\n'))}">${escapeAttr((r.reasons || []).join(', ') || 'rejected')}</div>` : ''}
        </div>
        <button class="btn ${r.rejected ? 'btn-secondary' : 'btn-primary'} btn-sm" onclick="_rcGrab(${i}, this)">${r.rejected ? 'Download anyway' : 'Download'}</button>
      </div>`).join('');
  } catch (e) {
    if (!_rc || _rc.item !== item) return;
    sum.textContent = e.message || 'The check failed';
    meta.innerHTML = '<a href="#" onclick="event.preventDefault();_rcLoad(true)">Try again</a>';
  }
}

async function _rcGrab(i, btn) {
  if (!_rc || !_rc.data || _rc.busy) return;
  const r = _rc.data.releases[i];
  _rc.busy = true; btn.disabled = true; btn.textContent = 'Sending…';
  try {
    const res = await api('/api/activity/arr/grab', { method: 'POST', body: { ..._actBody(_rc.item), guid: r.guid, indexer_id: r.indexer_id } });
    toast(res.message || 'Sent to your download client');
    closeModal('modal-release-check');
    await loadActivity();
  } catch (e) {
    toast(e.message || 'Download failed', 'error');
    btn.disabled = false; btn.textContent = r.rejected ? 'Download anyway' : 'Download';
  } finally { if (_rc) _rc.busy = false; }
}

async function activityRemove(key) {
  const item = _actItem(key);
  if (!item || _actBusy[key]) return;
  // First click arms it (and says what will be deleted); the second, within 5s, removes.
  if (!((_actArmed[key] || 0) > Date.now())) {
    _actArmed[key] = Date.now() + 5000;
    renderActivity();
    setTimeout(() => { if ((_actArmed[key] || 0) <= Date.now()) { delete _actArmed[key]; renderActivity(); } }, 5100);
    return;
  }
  delete _actArmed[key];
  _actBusy[key] = 'remove'; renderActivity();
  try {
    const body = _actBody(item);
    if (item.media_type === 'series' && item.episodes_on_disk > 0) body.delete_downloaded = true;
    const r = await api('/api/activity/arr/remove', { method: 'POST', body });
    toast(r.message || `Removed ${item.title}`);
    await loadActivity();
  } catch (e) {
    toast(e.message || 'Remove failed', 'error');
  } finally {
    delete _actBusy[key]; renderActivity();
  }
}

// "5m" / "3h" / "2d" since an ISO timestamp, or '' when unknown.
function _activityWaited(iso) {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return '';
  const mins = Math.floor(ms / 60000);
  if (mins < 60) return Math.max(mins, 1) + 'm';
  const hrs = Math.floor(mins / 60);
  if (hrs < 48) return hrs + 'h';
  return Math.floor(hrs / 24) + 'd';
}

// A card opens the title's detail, the same one Discover shows. Movies need a
// TMDB id; a show without one opens by its TVDB id.
function _actOpenAttrs(item) {
  const tmdb = parseInt(item.tmdb_id) || 0, tvdb = parseInt(item.tvdb_id) || 0;
  const type = item.media_type === 'series' ? 'series' : 'movie';
  if (!tmdb && !(type === 'series' && tvdb)) return '';
  return ` role="button" tabindex="0" data-open="${type}:${tmdb}:${tvdb}"`;
}
// One listener for the whole tab: it re-renders every 3 seconds. The card's
// own buttons and links keep their clicks.
function _activityCardOpen(e) {
  if (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
  const card = e.target.closest('.activity-card[data-open]');
  if (!card) return;
  if (e.type === 'keydown' ? e.target !== card : e.target.closest('button, a, input, label')) return;
  e.preventDefault();
  const [type, tmdb, tvdb] = card.dataset.open.split(':');
  showDiscoverDetail(+tmdb, type, undefined, undefined, undefined, undefined, +tvdb);
}

function renderActivity(data) {
  const content = document.getElementById('activity-content');
  if (!content) return;
  if (!content._actOpen) {
    content._actOpen = true;
    content.addEventListener('click', _activityCardOpen);
    content.addEventListener('keydown', _activityCardOpen);
  }
  if (!data) data = _activityData;
  if (!data) { content.innerHTML = '<div class="activity-empty">Loading…</div>'; return; }

  const downloads = data.downloads || [];
  const unreleased = data.unreleased || [];
  let html = '';

  // What in Radarr/Sonarr is stopping downloads (indexers, download client, disk).
  const problems = data.problems || [];
  if (problems.length > 0) {
    html += `<div class="arr-problems activity-problems${problems.some(p => p.level === 'error') ? ' error' : ''}">
      <div class="arr-problems-head">⚠ Searches may not work right now</div>
      <ul>${problems.slice(0, 4).map(p => `<li><strong>${escapeAttr(p.app)}:</strong> ${escapeAttr(p.message)}</li>`).join('')}</ul></div>`;
  }

  if (downloads.length > 0) {
    html += '<div class="activity-section-title">Downloading</div><div class="activity-grid">';
    html += downloads.map(dl => {
      const poster = _activityPoster(dl.poster_path);
      const statusClass = 'dl-' + (dl.status || 'downloading');
      const statusLabel = dl.status === 'importing' ? 'Importing' :
        dl.status === 'queued' ? 'Queued' :
        dl.status === 'warning' ? 'Warning' :
        dl.status === 'stuck' ? 'Stuck' :
        dl.status === 'import_blocked' ? 'Import blocked' : 'Downloading';
      const pct = Math.min(Math.max(dl.progress || 0, 0), 100);
      const epLabel = dl.episode ? ' · ' + escapeAttr(dl.episode) : '';
      const etaLabel = dl.eta ? ' · ' + escapeAttr(dl.eta) : '';
      const sizeLabel = dl.size_remaining ? escapeAttr(dl.size_remaining) + ' left' : '';
      const qualityLabel = dl.quality ? escapeAttr(dl.quality) : '';
      const reqByLabel = dl.requested_by ? `<span class="activity-requested-by">${escapeAttr(dl.requested_by)}</span>` : '';
      const metaParts = [qualityLabel, sizeLabel].filter(Boolean).join(' · ');
      return `<div class="activity-card"${_actOpenAttrs(dl)}>
        <div class="activity-poster">${poster}</div>
        <div class="activity-info">
          <div class="activity-title">${escapeAttr(dl.title)}${epLabel}</div>
          <div class="activity-meta">${dl.year || ''}${metaParts ? ' · ' + metaParts : ''} ${reqByLabel}</div>
          <div class="activity-progress">
            <div class="activity-progress-bar"><div class="activity-progress-fill ${statusClass}" style="width:${pct}%"></div></div>
            <span class="activity-progress-label">${statusLabel} · ${pct.toFixed(1)}%${etaLabel}</span>
          </div>
        </div>
      </div>`;
    }).join('');
    html += '</div>';
  }

  const searching = data.searching || [];
  if (searching.length > 0) {
    html += '<div class="activity-section-title">Searching</div><div class="activity-grid">';
    html += searching.map(item => {
      const poster = _activityPoster(item.poster_path);
      const waited = _activityWaited(item.waiting_since);
      const reqByLabel = item.requested_by ? `<span class="activity-requested-by">${escapeAttr(item.requested_by)}</span>` : '';
      const key = _actKey(item);
      const busy = _actBusy[key];
      const armed = !busy && (_actArmed[key] || 0) > Date.now();
      const arr = item.media_type === 'series' ? 'Sonarr' : 'Radarr';
      const isShow = item.media_type === 'series';
      const disk = isShow ? (item.episodes_on_disk || 0) : 0;
      const removeText = busy === 'remove' ? 'Removing…'
        : armed ? (disk ? `Click again: delete show + ${disk} episode${disk === 1 ? '' : 's'}` : 'Click again: delete + folder')
        : (disk ? 'Delete show' : 'Remove');
      const removeTitle = disk ? `Delete the whole show from ${arr}, including ${disk} downloaded episode${disk === 1 ? '' : 's'}`
        : `Remove from ${arr}, folder included`;
      return `<div class="activity-card"${_actOpenAttrs(item)}>
        <div class="activity-poster">${poster}</div>
        <div class="activity-info">
          <div class="activity-title">${escapeAttr(item.title)}${item.episode ? ' · ' + escapeAttr(item.episode) : ''}</div>
          <div class="activity-meta">${escapeAttr(item.year || '')} · Looking for a release ${reqByLabel}</div>
          ${item.check ? `<div class="activity-check ${escapeAttr(item.check.state)}" title="${escapeAttr(item.check.summary)}">${escapeAttr(item.check.short)}</div>` : ''}
          <div class="activity-countdown activity-searching">${waited ? 'Searching for ' + escapeAttr(waited) : 'Searching'}</div>
          <div class="activity-actions">
            <button class="activity-act-btn" ${busy ? 'disabled' : ''} onclick="activitySearchAgain('${escapeAttr(key)}')">${busy === 'search' ? 'Searching…' : 'Search again'}</button>
            <button class="activity-act-btn" ${busy ? 'disabled' : ''} title="Check what your indexers have and why nothing downloaded; pick a release yourself"
              onclick="openReleaseCheck('${escapeAttr(key)}')">Why? / Pick</button>
            ${isShow ? `<button class="activity-act-btn" ${busy ? 'disabled' : ''} title="Stop Sonarr looking for the missing episodes; keep everything downloaded"
              onclick="activityStopMissing('${escapeAttr(key)}')">${busy === 'stop' ? 'Stopping…' : 'Stop looking'}</button>` : ''}
            <button class="activity-act-btn activity-act-remove${armed ? ' armed' : ''}" ${busy ? 'disabled' : ''} title="${escapeAttr(removeTitle)}"
              onclick="activityRemove('${escapeAttr(key)}')">${removeText}</button>
          </div>
        </div>
      </div>`;
    }).join('');
    html += '</div>';
  }

  const recent = data.recently_downloaded || [];
  if (recent.length > 0) {
    html += '<div class="activity-section-title">Recently Downloaded</div><div class="activity-grid">';
    html += recent.map(item => {
      const poster = _activityPoster(item.poster_path);
      const hrs = item.hours_remaining != null ? `${item.hours_remaining}h left` : '';
      return `<div class="activity-card"${_actOpenAttrs(item)}>
        <div class="activity-poster">${poster}</div>
        <div class="activity-info">
          <div class="activity-title">${escapeAttr(item.title)}${item.episode ? ' · ' + escapeAttr(item.episode) : ''}</div>
          <div class="activity-meta">${item.year || ''} · Ready to watch</div>
          ${hrs ? `<div class="activity-countdown" style="color:var(--green)">${hrs}</div>` : ''}
        </div>
      </div>`;
    }).join('');
    html += '</div>';
  }

  const coming = data.coming_up || [];
  if (coming.length > 0) {
    html += '<div class="activity-section-title">Coming up this week</div><div class="activity-grid">';
    html += coming.map(item => `<div class="activity-card"${_actOpenAttrs(item)}>
        <div class="activity-poster">${_activityPoster(item.poster_path)}</div>
        <div class="activity-info">
          <div class="activity-title">${escapeAttr(item.title)} · ${escapeAttr(item.episode)}</div>
          <div class="activity-meta">${escapeAttr(item.episode_title || '')}</div>
          <div class="coming-day">${escapeAttr(_airDay(item.air_date_utc))}</div>
        </div>
      </div>`).join('');
    html += '</div>';
  }

  if (unreleased.length > 0) {
    html += '<div class="activity-section-title">Upcoming Releases</div><div class="activity-grid">';
    html += unreleased.map(item => {
      const poster = _activityPoster(item.poster_path);
      let daysUntil = '';
      if (item.release_date) {
        const rd = new Date(item.release_date + 'T00:00:00');
        const now = new Date();
        const diff = Math.ceil((rd - now) / 86400000);
        daysUntil = diff <= 0 ? 'Releasing soon' : diff === 1 ? 'Tomorrow' : diff + ' days';
      }
      return `<div class="activity-card"${_actOpenAttrs(item)}>
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
    html = '<div class="activity-empty">No active downloads, searches or upcoming releases</div>';
  }

  _morphInto(content, html);
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
  streaming: 'New on Streaming',
  genres: 'Genres',
};

let _streamingProviders = null;      // [{slug,name}] once loaded
let _streamingActiveProvider = null;
let _genreList = { movies: null, series: null };
let _genreActive = null;
let _genreMode = 'top_rated';  // 'top_rated' | 'new'
let _missingLists = { movies: null, series: null };
let _missingActiveList = 'all';

async function loadDiscover() {
  const grid = document.getElementById('discover-grid');
  const tabsEl = document.getElementById('discover-section-tabs');
  grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text3)"><span class="toast-spinner"></span> Loading…</div>';
  try {
    // Fetch discover content and (if not already cached) activity in parallel, so
    // cards can show Downloading / Awaiting Release badges on first paint.
    const [data] = await Promise.all([
      api(`/api/discover?type=${_discoverType}`),
      _activityData ? Promise.resolve() : loadActivity().catch(() => {}),
    ]);
    _discoverSections = data.sections || [];
    if (!_discoverSections.length) {
      tabsEl.innerHTML = '';
      grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>No content found. Check your TMDB bearer token in Settings.</p></div>';
      return;
    }
    // Render section tabs
    tabsEl.innerHTML = _discoverSections.map(sec => {
      const label = DISCOVER_SECTION_LABELS[sec.id] || sec.title;
      return `<button class="discover-sec-tab" data-section="${sec.id}" onclick="switchDiscoverSection('${sec.id}')">${label}<span style="font-size:11px;background:var(--bg3);color:var(--text3);padding:1px 7px;border-radius:10px">${sec.items.length}</span></button>`;
    }).join('') +
      `<button class="discover-sec-tab" data-section="streaming" onclick="switchDiscoverSection('streaming')">New on Streaming</button>` +
      `<button class="discover-sec-tab" data-section="genres" onclick="switchDiscoverSection('genres')">Genres</button>`;
    // Activate first or previously active section
    const targetId = (_discoverActiveSection === 'streaming' || _discoverActiveSection === 'genres'
      || (_discoverActiveSection && _discoverSections.find(s => s.id === _discoverActiveSection)))
      ? _discoverActiveSection : _discoverSections[0].id;
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
    // The strip scrolls sideways on phones: keep the chosen tab in view
    if (active && btn.scrollIntoView) btn.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
  const pills = document.getElementById('discover-streaming-pills');
  if (sectionId === 'streaming') {
    if (pills) pills.style.display = 'flex';
    loadStreamingSection();
    return;
  }
  if (sectionId === 'genres') {
    if (pills) { pills.style.display = 'block'; }
    loadGenreSection();
    return;
  }
  if (pills) pills.style.display = 'none';
  const section = _discoverSections.find(s => s.id === sectionId);
  if (section) renderDiscoverGrid(section.items);
}

async function loadStreamingSection() {
  const pills = document.getElementById('discover-streaming-pills');
  const grid = document.getElementById('discover-grid');
  if (!_streamingProviders) {
    try {
      const r = await api('/api/discover/providers');
      _streamingProviders = r.providers || [];
    } catch (e) { _streamingProviders = []; }
  }
  if (!_streamingProviders.length) {
    if (pills) pills.innerHTML = '';
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>No streaming services configured.</p></div>';
    return;
  }
  if (!_streamingActiveProvider || !_streamingProviders.find(p => p.slug === _streamingActiveProvider)) {
    _streamingActiveProvider = _streamingProviders[0].slug;
  }
  if (pills) {
    pills.innerHTML = _streamingProviders.map(p => {
      const active = p.slug === _streamingActiveProvider;
      return `<button onclick="selectStreamingProvider('${p.slug}')" style="padding:6px 14px;border-radius:16px;border:1px solid ${active ? 'var(--accent)' : 'var(--border2)'};background:${active ? 'var(--accent)' : 'var(--bg2)'};color:${active ? '#fff' : 'var(--text3)'};font-size:12px;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif">${p.name}</button>`;
    }).join('');
  }
  grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text3)"><span class="toast-spinner"></span> Loading…</div>';
  try {
    if (!_activityData) await loadActivity().catch(() => {});
    const data = await api(`/api/discover/streaming?provider=${_streamingActiveProvider}&type=${_discoverType}`);
    renderDiscoverGrid(data.items || []);
  } catch (e) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>Failed to load: ${e.message}</p></div>`;
  }
}

function selectStreamingProvider(slug) {
  _streamingActiveProvider = slug;
  loadStreamingSection();
}

async function loadGenreSection() {
  const pills = document.getElementById('discover-streaming-pills');
  const grid = document.getElementById('discover-grid');
  if (!_genreList[_discoverType]) {
    try {
      const r = await api(`/api/discover/genres?type=${_discoverType}`);
      _genreList[_discoverType] = r.genres || [];
    } catch (e) { _genreList[_discoverType] = []; }
  }
  const genres = _genreList[_discoverType];
  if (!genres.length) {
    if (pills) pills.innerHTML = '';
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>No genres available.</p></div>';
    return;
  }
  if (!_genreActive || !genres.find(g => g.id === _genreActive)) _genreActive = genres[0].id;
  if (pills) {
    const modeBtn = (m, label) => {
      const on = _genreMode === m;
      return `<button onclick="setGenreMode('${m}')" style="padding:6px 14px;border-radius:16px;border:1px solid ${on ? 'var(--accent)' : 'var(--border2)'};background:${on ? 'var(--accent)' : 'var(--bg2)'};color:${on ? '#fff' : 'var(--text3)'};font-size:12px;font-weight:600;cursor:pointer;font-family:'DM Sans',sans-serif">${label}</button>`;
    };
    const genrePills = genres.map(g => {
      const active = g.id === _genreActive;
      return `<button onclick="selectGenre(${g.id})" style="padding:6px 14px;border-radius:16px;border:1px solid ${active ? 'var(--accent)' : 'var(--border2)'};background:${active ? 'var(--accent)' : 'var(--bg2)'};color:${active ? '#fff' : 'var(--text3)'};font-size:12px;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif">${g.name}</button>`;
    }).join('');
    pills.innerHTML =
      `<div style="display:flex;gap:8px;width:100%;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--border2)">${modeBtn('top_rated','Top Rated')}${modeBtn('new','Newly Released')}</div>` +
      `<div style="display:flex;flex-wrap:wrap;gap:8px">${genrePills}</div>`;
  }
  grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text3)"><span class="toast-spinner"></span> Loading…</div>';
  try {
    if (!_activityData) await loadActivity().catch(() => {});
    const data = await api(`/api/discover/genre?genre_id=${_genreActive}&type=${_discoverType}&mode=${_genreMode}`);
    renderDiscoverGrid(data.items || []);
  } catch (e) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>Failed to load: ${e.message}</p></div>`;
  }
}

function selectGenre(id) {
  _genreActive = id;
  loadGenreSection();
}

function setGenreMode(m) {
  _genreMode = m;
  loadGenreSection();
}

async function loadListsSection() {
  const pills = document.getElementById('discover-streaming-pills');
  const grid = document.getElementById('discover-grid');
  if (!_missingLists[_discoverType]) {
    try {
      const r = await api(`/api/discover/lists?type=${_discoverType}`);
      _missingLists[_discoverType] = r.lists || [];
    } catch (e) { _missingLists[_discoverType] = []; }
  }
  const lists = _missingLists[_discoverType];
  const tabs = [{ id: 'all', name: 'All' }].concat(lists.map(l => ({ id: String(l.id), name: l.name })));
  if (!tabs.find(t => t.id === _missingActiveList)) _missingActiveList = 'all';
  if (pills) {
    pills.innerHTML = tabs.map(t => {
      const active = t.id === _missingActiveList;
      return `<button onclick="selectList('${t.id}')" style="padding:6px 14px;border-radius:16px;border:1px solid ${active ? 'var(--accent)' : 'var(--border2)'};background:${active ? 'var(--accent)' : 'var(--bg2)'};color:${active ? '#fff' : 'var(--text3)'};font-size:12px;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif">${t.name}</button>`;
    }).join('');
  }
  grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text3)"><span class="toast-spinner"></span> Loading…</div>';
  try {
    if (!_activityData) await loadActivity().catch(() => {});
    const data = await api(`/api/discover/list-missing?list_id=${_missingActiveList}&type=${_discoverType}`);
    renderDiscoverGrid(data.items || []);
  } catch (e) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>Failed to load: ${e.message}</p></div>`;
  }
}

function selectList(id) {
  _missingActiveList = id;
  loadListsSection();
}

// Live download / unreleased state for a discover item, from /api/activity (_activityData).
// Mirrors the Jellyfin plugin: downloading and awaiting-release items hide the add button.
function _discoverDownloadInfo(tmdbId) {
  if (!tmdbId || !_activityData || !_activityData.downloads) return null;
  return _activityData.downloads.find(d => d.tmdb_id == tmdbId) || null;
}
function _discoverUnreleasedInfo(tmdbId) {
  if (!tmdbId || !_activityData || !_activityData.unreleased) return null;
  return _activityData.unreleased.find(u => u.tmdb_id == tmdbId) || null;
}

function renderDiscoverGrid(items) {
  const grid = document.getElementById('discover-grid');
  if (!items.length) {
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>No content in this section</p></div>';
    return;
  }
  grid.innerHTML = items.map(item => {
    const posterSrc = _imgUrl(item.poster_path, 'w185');
    const poster = posterSrc
      ? `<img src="${posterSrc}" loading="lazy" onerror="this.outerHTML='<div class=\\'lib-card-poster-placeholder\\'>◫</div>'">`
      : `<div class="lib-card-poster-placeholder">◫</div>`;
    const tvdbId = item.tvdb_id || 0;
    const tmdbId = item.tmdb_id || 0;
    // Precedence mirrors the plugin: downloading → in library → awaiting release → addable
    const dlInfo = _discoverDownloadInfo(tmdbId);
    const ulInfo = !dlInfo ? _discoverUnreleasedInfo(tmdbId) : null;
    let badge, addBtn = '';
    if (dlInfo) {
      const pct = (dlInfo.progress || 0).toFixed(0);
      const st = dlInfo.status === 'importing' ? 'Importing' : dlInfo.status === 'queued' ? 'Queued' :
        dlInfo.status === 'stuck' ? 'Stuck' : dlInfo.status === 'import_blocked' ? 'Import blocked' : `Downloading ${pct}%`;
      badge = `<span class="badge ${dlInfo.status === 'stuck' ? 'badge-red' : 'badge-accent'}" style="font-size:9px;padding:1px 5px">${st}</span>`;
    } else if (item.in_library) {
      badge = `<span class="badge badge-green" style="font-size:9px;padding:1px 5px">In Library</span>`;
    } else if (ulInfo) {
      badge = `<span class="badge badge-amber" style="font-size:9px;padding:1px 5px">Awaiting Release</span>`;
    } else if (item.requested) {
      badge = `<span class="badge badge-blue" style="font-size:9px;padding:1px 5px">In ${item.media_type === 'series' ? 'Sonarr' : 'Radarr'}</span>`;
    } else {
      badge = `<span class="badge" style="font-size:9px;padding:1px 5px;background:var(--bg3);color:var(--text3)">${item.media_type === 'movie' ? 'Movie' : 'Show'}</span>`;
      addBtn = `<button onclick="event.stopPropagation();showAddToArrModal(${tmdbId},'${escapeJS(item.title)}','${escapeJS(item.year||'')}','${escapeJS(item.poster_path||'')}','${item.media_type}',${tvdbId})" class="lib-card-add-btn" title="Add to ${item.media_type === 'series' ? 'Sonarr' : 'Radarr'}">+</button>`;
    }
    const clickHandler = `onclick="showDiscoverDetail(${tmdbId},'${escapeAttr(item.media_type)}','${escapeJS(item.title)}','${escapeJS(item.year||'')}','${escapeJS(item.poster_path||'')}',${!!item.in_library},${tvdbId})"`;
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

async function showDiscoverDetail(tmdbId, mediaType, title, year, posterPath, inLibrary, tvdbId) {
  const seq = ++_detailSeq;
  showModal('modal-media-detail');
  document.getElementById('detail-title').textContent = 'Loading...';
  document.getElementById('detail-body').innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';

  const isTvdbOnly = !tmdbId && tvdbId;
  try {
    const detailUrl = isTvdbOnly
      ? `/api/discover/detail-tvdb/${tvdbId}`
      : `/api/discover/detail/${mediaType}/${tmdbId}`;
    const data = await api(detailUrl);
    if (seq !== _detailSeq) return;
    const isSeries = mediaType === 'series';
    const arrLabel = isSeries ? 'Sonarr' : 'Radarr';
    const detailTvdbId = data.tvdb_id || tvdbId || 0;
    const detailTmdbId = data.tmdb_id || tmdbId || 0;
    // Use detail response in_library (authoritative) over card-level flag
    const isInLibrary = data.in_library !== undefined ? data.in_library : inLibrary;
    // Live download / unreleased state (from /api/activity) — mirrors the plugin
    const dlInfo = _discoverDownloadInfo(detailTmdbId);
    const ulInfo = !dlInfo ? _discoverUnreleasedInfo(detailTmdbId) : null;
    // "In Library" has to lead somewhere. VOD (.strm) titles used to show the
    // badge and offer only download-again actions, with no way to reach the
    // episodes the user already had.
    const watchBtn = data.jellyfin_url
      ? ` <a class="btn btn-primary btn-sm" style="margin-left:6px" href="${escapeAttr(data.jellyfin_url)}" target="_blank" rel="noopener">Watch in Jellyfin</a>`
      : '';
    let actionBtn;
    if (dlInfo) {
      const pct = (dlInfo.progress || 0).toFixed(0);
      const st = dlInfo.status === 'importing' ? 'Importing…' : dlInfo.status === 'queued' ? 'Queued' :
        dlInfo.status === 'stuck' ? 'Stuck' : dlInfo.status === 'import_blocked' ? 'Import blocked' : `Downloading ${pct}%`;
      const eta = dlInfo.eta ? ` · ETA ${dlInfo.eta}` : '';
      actionBtn = `<span class="badge ${dlInfo.status === 'stuck' ? 'badge-red' : 'badge-accent'}" style="font-size:12px;padding:4px 10px">${st}${eta}</span>`;
    } else if (isInLibrary && isSeries && data.library_source === 'sonarr') {
      actionBtn = `<span class="badge badge-green" style="font-size:12px;padding:4px 10px">In Library</span>${watchBtn} <button class="btn btn-secondary btn-sm" style="margin-left:6px" onclick="closeModal('modal-media-detail');showManageEpisodesModal(${detailTmdbId},'${escapeJS(data.title||title||'')}','${escapeJS(data.year||year||'')}','${escapeJS(data.poster_path||posterPath||'')}')">Manage Episodes</button>`;
    } else if (isInLibrary && isSeries && data.library_source && data.library_source.startsWith('provider_')) {
      actionBtn = `<span class="badge badge-green" style="font-size:12px;padding:4px 10px">In Library</span>${watchBtn} <button class="btn btn-secondary btn-sm" style="margin-left:6px" onclick="closeModal('modal-media-detail');showDownloadMoreModal(${detailTmdbId},'${escapeJS(data.title||title||'')}','${escapeJS(data.year||year||'')}','${escapeJS(data.poster_path||posterPath||'')}')">Download Remaining Episodes</button>`;
    } else if (isInLibrary) {
      actionBtn = `<span class="badge badge-green" style="font-size:12px;padding:4px 10px">In Library</span>${watchBtn}`;
    } else if (ulInfo) {
      const ulLabel = (ulInfo.release_type && ulInfo.release_type !== 'TBA') ? `${ulInfo.release_type} release: ${ulInfo.release_date}` : ulInfo.release_date;
      actionBtn = `<span class="badge badge-amber" style="font-size:12px;padding:4px 10px">⏳ Awaiting Release</span> <span style="font-size:12px;color:var(--text3);margin-left:6px">${escapeAttr(ulLabel)}</span>`;
    } else if (data.requested) {
      actionBtn = `<span class="badge badge-blue" style="font-size:12px;padding:4px 10px">In ${isSeries ? 'Sonarr' : 'Radarr'} — searching for release</span>`;
    } else {
      actionBtn = `<button class="btn btn-primary btn-sm" onclick="closeModal('modal-media-detail');showAddToArrModal(${detailTmdbId},'${escapeJS(data.title||title||'')}','${escapeJS(data.year||year||'')}','${escapeJS(data.poster_path||posterPath||'')}','${mediaType}',${detailTvdbId})">Add to ${arrLabel}</button>`;
    }
    document.getElementById('detail-title').textContent = data.title || title || 'Unknown';
    const detailPosterSrc = _imgUrl(data.poster_path, 'w185');
    document.getElementById('detail-body').innerHTML = `
      <div class="detail-layout" style="display:flex;gap:20px">
        ${detailPosterSrc ? `<img src="${detailPosterSrc}" class="detail-poster" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
        <div style="flex:1">
          <div style="font-size:13px;color:var(--text2);margin-bottom:12px">${data.year || year || '—'} · ${data.runtime ? data.runtime+'m · ' : ''}★ ${data.rating || '—'}</div>
          <p style="font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:16px">${data.overview || 'No overview available.'}</p>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
            ${(data.genres||[]).map(g => `<span class="badge badge-gray">${g}</span>`).join('')}
          </div>
          <div style="margin-top:8px">
            ${actionBtn}${_trailerBtn(data.trailer_url)}
          </div>
        </div>
      </div>`;
  } catch {
    if (seq !== _detailSeq) return;
    document.getElementById('detail-title').textContent = title || 'Unknown';
    document.getElementById('detail-body').innerHTML = `
      <div class="detail-layout" style="display:flex;gap:20px">
        ${_imgUrl(posterPath, 'w185') ? `<img src="${_imgUrl(posterPath, 'w185')}" class="detail-poster" style="width:120px;height:180px;object-fit:cover;border-radius:6px;flex-shrink:0">` : ''}
        <div style="flex:1">
          <div style="font-size:13px;color:var(--text2);margin-bottom:12px">${year || '—'}</div>
          <p style="font-size:13px;color:var(--text2)">Could not load details.</p>
        </div>
      </div>`;
  }
}

function setDiscoverType(type, btn) {
  _discoverType = type;
  // Reset server sections between Movies/TV, but keep the streaming section pinned.
  if (['streaming', 'genres', 'missing'].indexOf(_discoverActiveSection) === -1) _discoverActiveSection = null;
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
  dirtyChannels: {},  // persists across page/search/filter changes
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

      liveState.dirtyChannels = {};  // Reset pending changes on fresh page load
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
      // No IPTV provider, but YouTube channels can still be Live TV channels:
      // show the Channels tab so they are not invisible on this page.
      if (await loadLiveYouTubeChannels()) {
        if (tabsEl) tabsEl.style.display = 'flex';
        const chPanel = document.getElementById('live-panel-channels');
        if (chPanel) chPanel.style.display = '';
        showLiveTab('channels');
      }
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
  loadLiveYouTubeChannels();
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

// ── YouTube channels on the Live TV page ──────────────────────────────────
// Added on the YouTube page; listed here because that is where every other
// Live TV channel is, and where one can be switched on or off later. Returns
// how many are currently on Live TV.
async function loadLiveYouTubeChannels() {
  const card = document.getElementById('live-youtube-card');
  const el = document.getElementById('live-youtube-list');
  if (!card || !el) return 0;
  let channels = [];
  try { channels = await api('/api/youtube/channels'); } catch { channels = []; }
  if (!channels.length) { card.style.display = 'none'; return 0; }

  const on = channels.filter(c => c.live_enabled);
  const off = channels.filter(c => !c.live_enabled);
  card.style.display = '';
  document.getElementById('live-youtube-count').textContent = `(${on.length} on Live TV)`;
  el.innerHTML = [...on, ...off].map(c => `
    <div class="live-ch-row">
      ${c.avatar_url ? `<img class="live-ch-logo" src="${escapeAttr(c.avatar_url)}" loading="lazy" onerror="this.style.display='none'">` : '<div class="live-ch-logo"></div>'}
      <span class="live-ch-name">${escapeAttr(c.title)}${c.live_enabled ? ytLiveState(c) : ''}</span>
      <span class="live-ch-group">${c.live_enabled ? `Channel ${escapeAttr(String(c.guide_number || ''))}` : 'not on Live TV'}</span>
      <span class="live-ch-epg-badge ${c.guide_programmes ? 'has-epg' : 'no-epg'}">${c.guide_programmes ? `${c.guide_programmes} in guide` : 'No guide entries'}</span>
      <button class="live-toggle ${c.live_enabled ? 'on' : ''}" onclick="toggleLiveYouTube(${c.id}, ${!c.live_enabled})" style="flex-shrink:0"></button>
    </div>`).join('');
  return on.length;
}

async function toggleLiveYouTube(id, enabled) {
  try {
    await api(`/api/youtube/channels/${id}/live`, { method: 'POST', body: { enabled } });
    toast(enabled
      ? 'Added to Live TV — looking for its streams, then Jellyfin\'s guide is refreshed'
      : 'Removed from Live TV — Jellyfin\'s guide is being refreshed', 'info', 6000);
    loadLiveYouTubeChannels();
  } catch (e) {
    toast(e.message, 'error');
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

  // Apply pending dirty overrides so toggled channels stay correct across page/search changes
  for (const ch of channels) {
    if (ch.id in liveState.dirtyChannels) {
      ch.enabled = liveState.dirtyChannels[ch.id];
    }
  }

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
    _updateDirtyBadge();
    return;
  }

  liveState.lastToggledChIdx = idx;
  ch.enabled = enabled;
  liveState.dirtyChannels[ch.id] = enabled;
  if (enabled) btn.classList.add('on'); else btn.classList.remove('on');
  _updateDirtyBadge();
}

function toggleAllChannels(enabled) {
  if (!liveState.pageChannels || !liveState.pageChannels.length) return;
  liveState.pageChannels.forEach((ch, i) => {
    ch.enabled = enabled;
    liveState.dirtyChannels[ch.id] = enabled;
    const row = document.querySelector(`.live-ch-row[data-ch-idx="${i}"] .live-toggle`);
    if (row) { if (enabled) row.classList.add('on'); else row.classList.remove('on'); }
  });
  _updateDirtyBadge();
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
    _updateDirtyBadge();
    toast(`Saved ${enableIds.length + disableIds.length} channel changes`);
  } catch (e) {
    toast(`Failed: ${e.message}`, 'error');
  }
}

function _updateDirtyBadge() {
  const count = Object.keys(liveState.dirtyChannels).length;
  const btn = document.getElementById('live-save-ch-btn');
  if (btn) btn.textContent = count ? `Save Channels (${count})` : 'Save Channels';
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
// ── Notification toggle (Home Screen tab) ──
async function loadNotificationState() {
  const checkbox = document.getElementById('notif-enabled-checkbox');
  if (!checkbox) return;
  try {
    const data = await api('/api/notifications');
    const enabled = data.notifications_enabled !== false;
    checkbox.checked = enabled;
    updateNotifSlider(enabled);
  } catch { checkbox.checked = true; updateNotifSlider(true); }
}

function updateNotifSlider(enabled) {
  const slider = document.getElementById('notif-slider');
  const track = slider?.previousElementSibling;
  if (slider) slider.style.transform = enabled ? 'translateX(18px)' : 'translateX(0)';
  if (track) track.style.background = enabled ? 'var(--accent)' : 'var(--bg3)';
}

async function toggleNotificationsFromCheckbox(checked) {
  updateNotifSlider(checked);
  try { await api('/api/notifications/toggle', { method: 'POST' }); } catch {}
}

// ── Merge Continue Watching + Next Up toggle ──
function updateMergeContinueSlider(enabled) {
  const slider = document.getElementById('merge-continue-slider');
  const track = slider?.previousElementSibling;
  if (slider) slider.style.transform = enabled ? 'translateX(18px)' : 'translateX(0)';
  if (track) track.style.background = enabled ? 'var(--accent)' : 'var(--bg3)';
}

async function saveMergeContinueWatching(checked) {
  updateMergeContinueSlider(checked);
  try {
    await api('/api/smartlists/merge-continue-watching', { method: 'POST', body: { enabled: checked } });
    toast(checked ? 'Continue Watching & Next Up combined' : 'Continue Watching & Next Up separated', 'success');
  } catch (e) {
    updateMergeContinueSlider(!checked);
    const cb = document.getElementById('merge-continue-checkbox');
    if (cb) cb.checked = !checked;
    toast('Failed to save setting', 'error');
  }
}

async function saveCardPreviews(mode) {
  const labels = { all: 'Previews on for all cards', local_only: 'Previews only for local files', off: 'Card previews off' };
  try {
    await api('/api/smartlists/card-previews', { method: 'POST', body: { mode } });
    toast(labels[mode] || 'Saved', 'success');
  } catch (e) {
    toast('Failed to save setting', 'error');
    loadHomeScreen();   // show what is actually saved
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Health page — housekeeping, downloads & audit trail
// ═══════════════════════════════════════════════════════════════════════════

function loadHealthPage() {
  loadHealthDownloadSettings();
  loadHealthDownloads();
  loadHealthMissing();
  loadHealthStreams();
  loadHealthDeletions();
  startHealthPolling();
}

// ── Stream health ───────────────────────────────────────────────────────────

async function loadHealthStreams() {
  const el = document.getElementById('health-streams');
  const summaryEl = document.getElementById('health-streams-summary');
  if (!el) return;
  try {
    const data = await api('/api/health/streams');
    const entries = data.entries || [];
    const countEl = document.getElementById('health-streams-count');
    if (countEl) countEl.textContent = entries.length ? `(${entries.length} dead)` : '';
    if (summaryEl) {
      const lr = data.last_run;
      summaryEl.textContent = lr
        ? `Last sweep: ${timeAgo(_healthDate(lr.at))} — ${lr.probed} probed, ${lr.new_bad} new dead, ${lr.cleared} recovered`
        : 'No sweep has run yet — runs nightly, or trigger one now';
    }
    if (!entries.length) {
      el.innerHTML = '<div class="empty-state"><p>No dead streams detected</p></div>';
      return;
    }
    const rows = entries.map(e => {
      const name = escapeHtml(e.title) + (e.episode ? ` <span style="color:var(--text3)">${escapeHtml(e.episode)}</span>` : '');
      const first = _healthDate(e.first_failed_at);
      const last = _healthDate(e.last_checked_at);
      return `<tr>
        <td>${name}</td>
        <td><span class="badge badge-accent" style="font-size:10px">${e.media_type === 'movie' ? 'Movie' : 'Series'}</span></td>
        <td style="white-space:nowrap;color:var(--text3);font-size:12px">${first ? timeAgo(first) : '—'}</td>
        <td style="white-space:nowrap;color:var(--text3);font-size:12px">${last ? timeAgo(last) : '—'} · ×${e.fail_count || 1}</td>
        <td style="white-space:nowrap;text-align:right"><div style="display:flex;gap:6px;justify-content:flex-end">
          <button class="btn btn-secondary btn-sm" onclick="healthClearStream(${e.id}, this)" title="Unmark without touching files">Clear</button>
          <button class="btn btn-danger btn-sm" onclick="healthRemoveStream(${e.id}, this)" title="Delete the dead .strm/.nfo from disk">Remove</button>
        </div></td>
      </tr>`;
    }).join('');
    el.innerHTML = `<div style="overflow-x:auto"><table>
      <thead><tr><th>Title</th><th>Type</th><th>First failed</th><th>Last checked</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  } catch (e) {
    el.innerHTML = '<div class="empty-state"><p>Failed to load stream health</p></div>';
  }
}

async function healthRecheckStreams(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Rechecking…'; }
  try {
    const r = await api('/api/health/streams/recheck', { method: 'POST' });
    const left = r.remaining ? ` — ${r.remaining} more to check, press again` : '';
    toast(r.cleared.length ? `${r.cleared.length} stream(s) recovered and cleared${left}` : `${r.rechecked} rechecked — still dead${left}`,
          r.cleared.length ? 'success' : 'info');
    loadHealthStreams();
  } catch (e) {
    toast(e.message || 'Recheck failed', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Recheck bad'; }
  }
}

async function healthRunStreamSweep(btn) {
  if (btn) btn.disabled = true;
  try {
    await api('/api/health/streams/sweep', { method: 'POST' });
    toast('Sweep started — results appear here as it progresses', 'info');
    setTimeout(loadHealthStreams, 15000);
  } catch (e) {
    toast(e.message || 'Sweep failed to start', 'error');
  } finally {
    if (btn) setTimeout(() => { btn.disabled = false; }, 5000);
  }
}

async function healthClearStream(id, btn) {
  if (btn) btn.disabled = true;
  try {
    await api('/api/health/streams/clear', { method: 'POST', body: { id } });
    loadHealthStreams();
  } catch (e) {
    toast(e.message || 'Clear failed', 'error');
    if (btn) btn.disabled = false;
  }
}

async function healthRemoveStream(id, btn) {
  if (!confirm('Delete this dead stream file from disk? Movies also lose their library record.')) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/health/streams/remove', { method: 'POST', body: { id } });
    toast(`Removed: ${r.title}`);
    loadHealthStreams();
    loadHealthDeletions();
  } catch (e) {
    toast(e.message || 'Remove failed', 'error');
    if (btn) btn.disabled = false;
  }
}

// ── Missing content ─────────────────────────────────────────────────────────

let _healthMissingTab = 'movies';

function showHealthMissingTab(tab) {
  _healthMissingTab = tab;
  for (const t of ['movies', 'episodes']) {
    const btn = document.getElementById(`health-missing-tab-${t}`);
    if (btn) btn.className = `btn ${t === tab ? 'btn-primary' : 'btn-secondary'} btn-sm`;
  }
  loadHealthMissing();
}

function _fmtBytes(b) {
  if (!b) return '—';
  if (b >= 1073741824) return (b / 1073741824).toFixed(1) + ' GB';
  if (b >= 1048576) return Math.round(b / 1048576) + ' MB';
  return Math.round(b / 1024) + ' KB';
}

async function loadHealthMissing() {
  const el = document.getElementById('health-missing');
  if (!el) return;
  el.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  const kind = _healthMissingTab === 'movies' ? 'movie' : 'episode';
  try {
    const items = await api(`/api/health/missing/${_healthMissingTab}`);
    const countEl = document.getElementById('health-missing-count');
    if (countEl) countEl.textContent = `(${items.length})`;
    if (!items.length) {
      el.innerHTML = '<div class="empty-state"><p>Nothing missing — library is complete</p></div>';
      return;
    }
    const rows = items.map(it => {
      const name = _healthMissingTab === 'movies'
        ? `${escapeHtml(it.title)} <span style="color:var(--text3)">${it.year || ''}</span>`
        : `${escapeHtml(it.series)} <span style="color:var(--text3)">S${String(it.season).padStart(2, '0')}E${String(it.episode).padStart(2, '0')}</span> ${escapeHtml(it.title || '')}`;
      const aired = _healthMissingTab === 'episodes' && it.air_date
        ? `<td style="white-space:nowrap;color:var(--text3);font-size:12px">${it.air_date.slice(0, 10)}</td>`
        : (_healthMissingTab === 'episodes' ? '<td>—</td>' : '');
      return `<tr id="health-missing-row-${it.id}">
        <td>${name}</td>
        ${aired}
        <td style="white-space:nowrap;text-align:right"><div style="display:flex;gap:6px;justify-content:flex-end">
          <button class="btn btn-secondary btn-sm" onclick="healthDiagnose('${kind}', ${it.id}, this)">Diagnose</button>
          <button class="btn btn-secondary btn-sm" onclick="healthSearchMissing('${kind}', ${it.id}, this)">Search</button>
        </div></td>
      </tr>`;
    }).join('');
    const airedHead = _healthMissingTab === 'episodes' ? '<th>Aired</th>' : '';
    el.innerHTML = `<div style="overflow-x:auto"><table>
      <thead><tr><th>Title</th>${airedHead}<th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  } catch (e) {
    el.innerHTML = '<div class="empty-state"><p>Failed to load missing content</p></div>';
  }
}

async function healthDiagnose(kind, id, btn) {
  const row = document.getElementById(`health-missing-row-${id}`);
  if (!row) return;
  const existing = document.getElementById(`health-diag-${id}`);
  if (existing) { existing.remove(); return; }
  if (btn) btn.disabled = true;
  try {
    const d = await api(`/api/health/missing/diagnose?kind=${kind}&id=${id}`);
    const cols = row.children.length;
    let inner;
    if (!d.total_found) {
      inner = '<div style="color:var(--text3);font-size:13px;padding:8px 0">No releases found on any indexer right now.</div>';
    } else {
      const reasons = (d.top_reasons || []).map(([r, n]) =>
        `<span class="badge badge-amber" style="font-size:10px;margin:2px 4px 2px 0">${escapeHtml(r)} ×${n}</span>`).join('');
      const cands = (d.candidates || [])
        .sort((a, b) => (a.rejected === b.rejected) ? 0 : (a.rejected ? 1 : -1))
        .slice(0, 15)
        .map(c => `<tr>
          <td style="font-size:12px;max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeAttr(c.title || '')}">${escapeHtml(c.title || '')}</td>
          <td style="font-size:12px;white-space:nowrap">${escapeHtml(c.quality || '—')}</td>
          <td style="font-size:12px;white-space:nowrap">${_fmtBytes(c.size_bytes)}</td>
          <td style="font-size:12px;white-space:nowrap">${c.protocol === 'torrent' ? (c.seeders ?? '?') + ' seeds' : escapeHtml(c.protocol || '')}</td>
          <td style="font-size:11px;color:var(--text3)">${c.rejected ? escapeHtml((c.rejections || []).join('; ')) : '<span class="badge badge-green" style="font-size:10px">Grabbable</span>'}</td>
          <td style="text-align:right"><button class="btn ${c.rejected ? 'btn-secondary' : 'btn-primary'} btn-sm" onclick="healthGrabRelease('${kind}', '${escapeAttr(c.guid)}', ${c.indexer_id}, this)">Grab</button></td>
        </tr>`).join('');
      inner = `
        <div style="font-size:12px;color:var(--text2);margin-bottom:6px">
          ${d.total_found} release(s) found · ${d.grabbable_now} grabbable now
        </div>
        ${reasons ? `<div style="margin-bottom:8px">${reasons}</div>` : ''}
        <div style="overflow-x:auto"><table>
          <thead><tr><th>Release</th><th>Quality</th><th>Size</th><th>Source</th><th>Rejection</th><th></th></tr></thead>
          <tbody>${cands}</tbody>
        </table></div>
        ${d.candidates.length > 15 ? `<div style="font-size:11px;color:var(--text3);margin-top:4px">Showing 15 of ${d.candidates.length} releases</div>` : ''}`;
    }
    row.insertAdjacentHTML('afterend',
      `<tr id="health-diag-${id}"><td colspan="${cols}" style="background:var(--bg2)">${inner}</td></tr>`);
  } catch (e) {
    toast(e.message || 'Diagnose failed', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function healthGrabRelease(kind, guid, indexerId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    await api('/api/health/missing/grab', { method: 'POST', body: { kind, guid, indexer_id: indexerId } });
    toast('Release sent to download client');
    if (btn) btn.textContent = 'Sent';
  } catch (e) {
    toast(e.message || 'Grab failed', 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Grab'; }
  }
}

async function healthSearchMissing(kind, id, btn) {
  if (btn) btn.disabled = true;
  try {
    await api('/api/health/missing/search', { method: 'POST', body: { kind, id } });
    toast('Search triggered — check the Downloads section shortly');
  } catch (e) {
    toast(e.message || 'Search failed', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Downloads health ────────────────────────────────────────────────────────

let _healthPollTimer = null;

function startHealthPolling() {
  stopHealthPolling();
  _healthPollTimer = setInterval(() => {
    if (state.currentPage !== 'health') { stopHealthPolling(); return; }
    loadHealthDownloads();
  }, 5000);
}

function stopHealthPolling() {
  if (_healthPollTimer) { clearInterval(_healthPollTimer); _healthPollTimer = null; }
}

async function loadHealthDownloadSettings() {
  try {
    const s = await api('/api/health/downloads/settings');
    const fix = document.getElementById('health-auto-fix');
    const imp = document.getElementById('health-auto-import');
    if (fix) fix.checked = !!s.auto_fix;
    if (imp) imp.checked = !!s.auto_import;
  } catch (_) {}
}

async function saveHealthDownloadSettings() {
  const fix = document.getElementById('health-auto-fix');
  const imp = document.getElementById('health-auto-import');
  try {
    await api('/api/health/downloads/settings', {
      method: 'POST',
      body: { auto_fix: !!fix?.checked, auto_import: !!imp?.checked },
    });
    toast('Download automation settings saved');
  } catch (e) {
    toast('Failed to save settings', 'error');
    loadHealthDownloadSettings();
  }
}

const _DL_STATUS_META = {
  stuck:          { label: 'Stuck', cls: 'badge-red' },
  import_blocked: { label: 'Import blocked', cls: 'badge-amber' },
  warning:        { label: 'Warning', cls: 'badge-amber' },
  downloading:    { label: 'Downloading', cls: 'badge-accent' },
  importing:      { label: 'Importing', cls: 'badge-green' },
  queued:         { label: 'Queued', cls: 'badge-accent' },
};

async function loadHealthDownloads() {
  const el = document.getElementById('health-downloads');
  if (!el) return;
  try {
    const data = await api('/api/activity');
    const downloads = data.downloads || [];
    if (!downloads.length) {
      el.innerHTML = '<div class="empty-state"><p>No active downloads</p></div>';
      return;
    }
    const rows = downloads.map(d => {
      const meta = _DL_STATUS_META[d.status] || { label: d.status, cls: 'badge-accent' };
      const name = escapeHtml(d.title) + (d.episode ? ` <span style="color:var(--text3)">${escapeHtml(d.episode)}</span>` : '');
      const sub = [d.quality, d.protocol, d.indexer].filter(Boolean).map(escapeHtml).join(' · ');
      const prog = d.progress != null ? `${d.progress}%` : '—';
      const stallNote = d.status === 'stuck' && d.stalled_minutes > 0
        ? `<div style="font-size:11px;color:var(--text3)">no progress for ${Math.round(d.stalled_minutes)}m</div>` : '';
      const reason = d.reason
        ? `<div style="font-size:12px;color:var(--amber);margin-top:2px">${escapeHtml(d.reason)}</div>` : '';
      let actions = '';
      if (d.queue_id != null && d.status === 'stuck') {
        actions = `<button class="btn btn-primary btn-sm" onclick="healthFixDownload('${d.source}', ${d.queue_id}, this)">Fix</button>
                   <button class="btn btn-secondary btn-sm" onclick="healthRemoveDownload('${d.source}', ${d.queue_id}, true, this)">Remove</button>`;
      } else if (d.queue_id != null && d.status === 'import_blocked') {
        actions = `<button class="btn btn-primary btn-sm" onclick="healthImportDownload('${d.source}', '${escapeAttr(d.download_id || '')}', this)">Import</button>
                   <button class="btn btn-secondary btn-sm" onclick="healthRemoveDownload('${d.source}', ${d.queue_id}, false, this)">Remove</button>`;
      }
      return `<tr>
        <td><span class="badge ${meta.cls}" style="font-size:10px">${meta.label}</span></td>
        <td>${name}${reason}${stallNote}</td>
        <td style="white-space:nowrap;color:var(--text3);font-size:12px">${sub || '—'}</td>
        <td style="white-space:nowrap">${prog}${d.eta ? ` <span style="color:var(--text3)">· ${escapeHtml(d.eta)}</span>` : ''}</td>
        <td style="white-space:nowrap;text-align:right"><div style="display:flex;gap:6px;justify-content:flex-end">${actions}</div></td>
      </tr>`;
    }).join('');
    el.innerHTML = `<div style="overflow-x:auto"><table>
      <thead><tr><th>Status</th><th>Title</th><th>Release</th><th>Progress</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  } catch (e) {
    el.innerHTML = '<div class="empty-state"><p>Failed to load downloads</p></div>';
  }
}

async function healthFixDownload(source, queueId, btn) {
  if (!confirm('Cancel this download, blocklist the release, and grab an alternative?')) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/health/downloads/fix', { method: 'POST', body: { source, queue_id: queueId } });
    toast(r.replaced ? `Replaced with ${r.picked_protocol} release` : 'Cancelled — no alternative release found',
          r.replaced ? 'success' : 'info');
    loadHealthDownloads();
    loadHealthDeletions();
  } catch (e) {
    toast(e.message || 'Fix failed', 'error');
    if (btn) btn.disabled = false;
  }
}

async function healthRemoveDownload(source, queueId, deleteFile, btn) {
  if (!confirm(deleteFile ? 'Remove this download and delete the file?' : 'Remove from queue? The file stays on disk.')) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/health/downloads/remove', { method: 'POST', body: { source, queue_id: queueId, delete_file: deleteFile } });
    toast(`Removed: ${r.title}`);
    loadHealthDownloads();
    loadHealthDeletions();
  } catch (e) {
    toast(e.message || 'Remove failed', 'error');
    if (btn) btn.disabled = false;
  }
}

async function healthImportDownload(source, downloadId, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/health/downloads/manual-import', { method: 'POST', body: { source, download_id: downloadId } });
    if (r.ok) {
      toast(`Imported: ${r.title || 'download'}`);
    } else {
      toast(r.reason || 'Could not import automatically', 'info', 6000);
    }
    loadHealthDownloads();
  } catch (e) {
    toast(e.message || 'Import failed', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _healthDate(iso) {
  if (!iso) return null;
  // Backend timestamps are naive UTC — append Z so they render in local time
  return new Date(/[Z+]/.test(iso.slice(10)) ? iso : iso + 'Z');
}

const _DELETION_KIND_META = {
  'download-delete':   { label: 'Download delete', cls: 'badge-red' },
  'jellyfin-delete':   { label: 'Jellyfin delete', cls: 'badge-red' },
  'provider-cascade':  { label: 'Provider delete', cls: 'badge-red' },
  'duplicate-resolve': { label: 'Duplicate', cls: 'badge-amber' },
  'orphan-sweep':      { label: 'Orphan sweep', cls: 'badge-accent' },
  'vod-sweep':         { label: 'VOD sweep', cls: 'badge-accent' },
  'stale-cleanup':     { label: 'Stale cleanup', cls: 'badge-amber' },
  'download-fix':      { label: 'Download fix', cls: 'badge-amber' },
  'stream-health':     { label: 'Stream health', cls: 'badge-amber' },
};

async function loadHealthDeletions() {
  const el = document.getElementById('health-deletions');
  if (!el) return;
  try {
    const entries = await api('/api/health/deletions?limit=200');
    if (!entries.length) {
      el.innerHTML = '<div class="empty-state"><p>No deletions recorded yet</p></div>';
      return;
    }
    const rows = entries.map(e => {
      const meta = _DELETION_KIND_META[e.kind] || { label: e.kind, cls: 'badge-accent' };
      const d = _healthDate(e.created_at);
      const when = d ? `<span title="${d.toLocaleString()}">${timeAgo(d)}</span>` : '—';
      const who = e.reason === 'manual' && e.user_name ? escapeHtml(e.user_name) : e.reason;
      return `<tr>
        <td style="white-space:nowrap">${when}</td>
        <td><span class="badge ${meta.cls}" style="font-size:10px">${meta.label}</span></td>
        <td>${escapeHtml(e.name || '')}</td>
        <td style="white-space:nowrap;color:var(--text3)">${escapeHtml(who || '')}</td>
        <td style="color:var(--text3);font-size:12px">${escapeHtml(e.detail || '')}</td>
      </tr>`;
    }).join('');
    el.innerHTML = `<div style="overflow-x:auto"><table>
      <thead><tr><th>When</th><th>Action</th><th>Item</th><th>By</th><th>Detail</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  } catch (e) {
    el.innerHTML = '<div class="empty-state"><p>Failed to load deletion log</p></div>';
  }
}

(function exposeGlobals() {
  const fns = [
    // Activity (inline handlers)
    _activityPosterFailed, activitySearchAgain, activityRemove, activityStopMissing, _smAll, _smCount, _smGo, openReleaseCheck, _rcLoad, _rcGrab, replaceCopy,
    // Wrong movie (mislabelled provider streams)
    reportWrongMovie, dismissMatchSuspect, unblockStream, openFixMatch, _fmLoad, _fmFrames, _fmPick, _fmRemove,
    // Lists page
    loadLists, loadListCards,
    saveQuickList, onQuickListUrlInput, onQuickListNameInput, onModalUrlInput, onModalNameInput,
    loadListCoverageInline, addAllMissingFromCard,
    fetchList, deleteList, showListCoverage, showAddList, saveList,
    // Coverage modal
    addAllMissingToRadarr, addAllMissingToArr, setCoverageFilter,
    // Jellyfin page (home screen + playlists + discover tabs)
    loadJellyfinPage, loadAutoPlaylists, toggleAutoPlaylist, dismissAutoPlaylistBanner,
    showAddTagRule, editTagRule, deleteTagRule, saveTagRule, onContentSourceChange, toggleAdvancedFilters,
    addRuleCondition, updateCondOps, onCollectionNameInput, syncSmartLists, refreshTags, syncPlaylistsToJellyfin, resyncAllPlaylists, setPlaylistSort,
    pushHomeConfig, updateHeroPick, updateHeroSort, saveRowMaxItems, saveRowMaxItemsByKey, saveRowShapeByKey, toggleNotificationsFromCheckbox, saveMergeContinueWatching, saveCardPreviews, dismissNewContentNotice, toggleGenreChip, _scheduleMatchCount, toggleToolbarButton,
    showAddHomeRow, hideAddHomeRow, confirmAddHomeRow, removeHomeRow, removeHomeRowByKey,
    reorderHomeRows, rowKey,
    // Library
    openSyncDetailModal, closeSyncDetailModal,
    loadLibListPills, setLibList, setLibListStatus, setLibSort, scrollListPills,
    showMediaDetail, showCoverageDetail, filterByTag, searchLibrary, setLibType, setLibSrc,
    loadMoreLibrary, showAddToRadarrModal, showAddToArrModal, confirmAddToRadarr, confirmAddToArr,
    onMonitorPresetChange, toggleSeasonAccordion, toggleSeasonAll, updateSeasonCheckbox, epPickerSelectAll, epPickerSelectNone,
    showManageEpisodesModal, confirmManageEpisodes,
    showDownloadMoreModal, confirmDownloadMore, detailToggleSeason, toggleFollow,
    // YouTube
    loadYouTubePage, loadYouTubeChannels, ytAddChannel, ytDeleteChannel, ytRefreshNow, ytStartPolling, ytDiagnose, ytSkipNote, ytReprobe, ytRefill, loadLiveYouTubeChannels, toggleLiveYouTube, ytSaveAddress, ytShowAddress, loadTentacleAddress, ytUseAddress, ytDetectAddress,
    // Following
    loadFollowing,
    toggleStrmManaged,
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
    // Health
    loadHealthPage, loadHealthDeletions, loadHealthDownloads, stopHealthPolling,
    saveHealthDownloadSettings, healthFixDownload, healthRemoveDownload, healthImportDownload,
    showHealthMissingTab, loadHealthMissing, healthDiagnose, healthGrabRelease, healthSearchMissing,
    loadHealthStreams, healthRecheckStreams, healthRunStreamSweep, healthClearStream, healthRemoveStream,
    // Discover
    loadDiscoverPage, loadDiscover, setDiscoverType, switchDiscoverSection, selectStreamingProvider, selectGenre, setGenreMode, selectList, showDiscoverDetail,
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