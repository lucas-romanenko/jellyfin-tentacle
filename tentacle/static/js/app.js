// Tentacle - Frontend App

// ── State ─────────────────────────────────────────────────────────────────
const state = {
  currentPage: 'dashboard',
  currentSettingsSection: 'providers',
  providers: [],
  categories: [],
  catFilter: 'all',
  editingProviderId: null,
};

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  setupNavigation();
  await checkSetup();
  loadDashboard();
  loadProviders();  // Populate state.providers for sync buttons
  checkRunningSyncs();
  setInterval(loadDashboard, 30000);
});

function setupNavigation() {
  // Sidebar nav
  document.querySelectorAll('.nav-item[data-page]').forEach(btn => {
    btn.addEventListener('click', () => showPage(btn.dataset.page));
  });

  // Settings nav
  document.querySelectorAll('.settings-nav-item[data-section]').forEach(btn => {
    btn.addEventListener('click', () => showSettingsSection(btn.dataset.section));
  });
}

async function checkSetup() {
  try {
    const s = await api('/api/settings/raw');
    if (s.setup_complete !== 'true') {
      document.getElementById('setup-overlay').style.display = 'flex';
      // Pre-fill with existing values if any
      if (s.jellyfin_url) document.getElementById('setup-jellyfin-url').value = s.jellyfin_url;
      if (s.radarr_url) document.getElementById('setup-radarr-url').value = s.radarr_url;
      if (s.sonarr_url) document.getElementById('setup-sonarr-url').value = s.sonarr_url;
    }
  } catch (e) {}
}

// ── Navigation ────────────────────────────────────────────────────────────
function showPage(name) {
  // Disconnect library SSE when leaving library page
  if (state.currentPage === 'library' && name !== 'library') {
    if (typeof disconnectLibraryStream === 'function') disconnectLibraryStream();
  }

  state.currentPage = name;

  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item[data-page]').forEach(n => n.classList.remove('active'));

  document.getElementById(`page-${name}`)?.classList.add('active');
  document.querySelector(`.nav-item[data-page="${name}"]`)?.classList.add('active');

  const titles = {
    dashboard: ['Dashboard', 'Overview & activity'],
    vod: ['VOD', 'Categories, sync & content'],
    library: ['Library', 'Movies, series & duplicates'],
    'live-tv': ['Live TV', 'Channels, groups & EPG'],
    discover: ['Discover', 'Trending, popular & list subscriptions'],
    playlists: ['Playlists', 'Manage Jellyfin playlists & home screen'],
    settings: ['Settings', 'Providers, connections & config'],
  };

  const [title, sub] = titles[name] || [name, ''];
  document.getElementById('topbar-title').textContent = title;
  document.getElementById('topbar-sub').textContent = sub;

  // Load page data
  if (name === 'settings') { loadSettings(); loadProviders(); }
  if (name === 'vod') loadVodPage();
  if (name === 'library') loadLibrary();
  if (name === 'playlists') loadPlaylistsPage();
  if (name === 'discover') loadDiscover();
  if (name === 'live-tv') loadLiveTV();
}

// ── Tab switching helpers ──────────────────────────────────────────────────

function toggleDashHistory() {
  const body = document.getElementById('dash-history-body');
  const toggle = document.getElementById('dash-history-toggle');
  if (body.style.display === 'none') {
    body.style.display = '';
    toggle.textContent = '▾ Hide';
    loadHistory();
  } else {
    body.style.display = 'none';
    toggle.textContent = '▸ Show';
  }
}

function showLibTab(tab) {
  document.querySelectorAll('[data-libtab]').forEach(t => t.classList.remove('active'));
  document.querySelector(`[data-libtab="${tab}"]`)?.classList.add('active');
  document.getElementById('lib-tab-browse').style.display = tab === 'browse' ? '' : 'none';
  document.getElementById('lib-tab-duplicates').style.display = tab === 'duplicates' ? '' : 'none';
  if (tab === 'duplicates') loadDuplicates();
}

function showDiscoverTab(tab) {
  document.querySelectorAll('[data-discovertab]').forEach(t => t.classList.remove('active'));
  document.querySelector(`[data-discovertab="${tab}"]`)?.classList.add('active');
  document.getElementById('discover-tab-browse').style.display = tab === 'browse' ? '' : 'none';
  document.getElementById('discover-tab-lists').style.display = tab === 'lists' ? '' : 'none';
  if (tab === 'lists') loadLists();
}

function showSettingsSection(name) {
  state.currentSettingsSection = name;
  document.querySelectorAll('.settings-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.settings-nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(`section-${name}`)?.classList.add('active');
  document.querySelector(`.settings-nav-item[data-section="${name}"]`)?.classList.add('active');
}

// ── API Helper ────────────────────────────────────────────────────────────
async function api(url, options = {}) {
  const defaults = { headers: { 'Content-Type': 'application/json' } };
  const merged = { ...defaults, ...options };
  if (merged.body && typeof merged.body === 'object') {
    merged.body = JSON.stringify(merged.body);
  }
  const r = await fetch(url, merged);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return r.json();
}

// ── Toast ─────────────────────────────────────────────────────────────────
function toast(msg, type = 'success', duration = 3500) {
  const icons = { success: '✓', error: '✕', info: 'ℹ', loading: '' };
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const icon = type === 'loading'
    ? '<span class="toast-spinner"></span>'
    : `<span style="color:var(--${type === 'success' ? 'green' : type === 'error' ? 'red' : 'blue'})">${icons[type]}</span>`;
  el.innerHTML = `${icon} ${msg}`;
  document.getElementById('toasts').appendChild(el);
  if (duration > 0) setTimeout(() => el.remove(), duration);
  return el;
}

// ── Setup Wizard ──────────────────────────────────────────────────────────
let _setupStep = 1;
// Track whether user skipped from step 3 to 5 (no Radarr/Sonarr)
let _setupSkippedArr = false;

function setupGoTo(step) {
  _setupStep = step;
  document.querySelectorAll('.setup-step').forEach(el => el.style.display = 'none');
  document.getElementById(`setup-step-${step}`).style.display = 'block';
  // Update progress pips
  document.querySelectorAll('.setup-pip').forEach(pip => {
    const s = parseInt(pip.dataset.step);
    pip.className = 'setup-pip' + (s < step ? ' done' : s === step ? ' active' : '');
  });
}

// Back button on step 5 — go to 4 if they came from arr, or 2 if they skipped
function setupGoBack() {
  setupGoTo(_setupSkippedArr ? 2 : 4);
}

async function testSetupJellyfin() {
  const url = document.getElementById('setup-jellyfin-url').value.trim();
  const key = document.getElementById('setup-jellyfin-key').value.trim();
  const el = document.getElementById('setup-jellyfin-result');
  if (!url || !key) { el.innerHTML = '<span style="color:var(--red)">Enter URL and API key first</span>'; return; }
  el.innerHTML = 'Testing...';
  try {
    const r = await api('/api/settings/test', { method: 'POST', body: { type: 'jellyfin', url, api_key: key } });
    el.innerHTML = `<span style="color:var(--green)">${r.message}</span>`;
  } catch (e) {
    el.innerHTML = `<span style="color:var(--red)">${e.message}</span>`;
  }
}

async function setupStep1Next() {
  const jfUrl = document.getElementById('setup-jellyfin-url').value.trim();
  const jfKey = document.getElementById('setup-jellyfin-key').value.trim();
  if (!jfUrl || !jfKey) {
    toast('Jellyfin URL and API key are required', 'error');
    return;
  }
  // Save Jellyfin settings immediately so login endpoint can use them
  try {
    await api('/api/settings', { method: 'POST', body: { settings: { jellyfin_url: jfUrl, jellyfin_api_key: jfKey } } });
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  setupGoTo(2);
}

async function setupStep2Next() {
  const username = document.getElementById('setup-jf-username').value.trim();
  const password = document.getElementById('setup-jf-password').value;
  const errorEl = document.getElementById('setup-jf-login-error');
  const successEl = document.getElementById('setup-jf-login-success');
  const btn = document.getElementById('setup-login-btn');

  if (!username) {
    errorEl.textContent = 'Username is required';
    errorEl.style.display = 'block';
    successEl.style.display = 'none';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Logging in...';
  errorEl.style.display = 'none';
  successEl.style.display = 'none';

  try {
    const r = await api('/api/settings/jellyfin-login', {
      method: 'POST',
      body: { username, password }
    });
    successEl.textContent = `Logged in as ${r.username}`;
    successEl.style.display = 'block';
    // Brief pause so user sees success, then advance
    setTimeout(() => setupGoTo(3), 600);
  } catch (e) {
    errorEl.textContent = e.message || 'Login failed';
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Login & Continue →';
  }
}

async function testSetupArr(type) {
  const url = document.getElementById(`setup-${type}-url`).value.trim();
  const key = document.getElementById(`setup-${type}-key`).value.trim();
  const el = document.getElementById(`setup-${type}-result`);
  if (!url || !key) { el.innerHTML = '<span style="color:var(--red)">Enter URL and API key</span>'; return; }
  el.innerHTML = 'Testing...';
  try {
    const r = await api('/api/settings/test', { method: 'POST', body: { type, url, api_key: key } });
    el.innerHTML = `<span style="color:var(--green)">${r.message}</span>`;
  } catch (e) {
    el.innerHTML = `<span style="color:var(--red)">${e.message}</span>`;
  }
}

async function setupStep3Next() {
  // Save Radarr/Sonarr settings
  const settings = {};
  const radarrUrl = document.getElementById('setup-radarr-url').value.trim();
  const radarrKey = document.getElementById('setup-radarr-key').value.trim();
  const sonarrUrl = document.getElementById('setup-sonarr-url').value.trim();
  const sonarrKey = document.getElementById('setup-sonarr-key').value.trim();
  if (radarrUrl) settings.radarr_url = radarrUrl;
  if (radarrKey) settings.radarr_api_key = radarrKey;
  if (sonarrUrl) settings.sonarr_url = sonarrUrl;
  if (sonarrKey) settings.sonarr_api_key = sonarrKey;

  if (Object.keys(settings).length) {
    try {
      await api('/api/settings', { method: 'POST', body: { settings } });
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  _setupSkippedArr = false;

  // Show webhook step only if at least one arr was configured
  if (radarrUrl || sonarrUrl) {
    // Pre-populate webhook host from Tentacle's own URL if possible
    const webhookHost = document.getElementById('setup-webhook-host');
    if (!webhookHost.value) {
      webhookHost.value = window.location.host;
    }
    updateSetupWebhookUrls();
    // Show relevant webhook sections
    document.getElementById('setup-webhook-radarr-section').style.display = radarrUrl ? 'block' : 'none';
    document.getElementById('setup-webhook-sonarr-section').style.display = sonarrUrl ? 'block' : 'none';
    setupGoTo(4);
  } else {
    setupGoTo(5);
  }
}

function updateSetupWebhookUrls() {
  let host = document.getElementById('setup-webhook-host').value.trim();
  if (!host) {
    document.getElementById('setup-webhook-urls').style.display = 'none';
    return;
  }
  host = host.replace(/^https?:\/\//, '');
  document.getElementById('setup-radarr-webhook-url').value = `http://${host}/api/radarr/webhook`;
  document.getElementById('setup-sonarr-webhook-url').value = `http://${host}/api/sonarr/webhook`;
  document.getElementById('setup-webhook-urls').style.display = 'block';
}

async function copySetupWebhook(type) {
  const url = document.getElementById(`setup-${type}-webhook-url`).value;
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    toast('Copied!');
  } catch (_) {
    toast('Copy failed — use HTTPS or copy manually', 'error');
  }
}

async function completeSetup() {
  const settings = {};
  const tmdb = document.getElementById('setup-tmdb').value.trim();
  const trakt = document.getElementById('setup-trakt').value.trim();
  const logodev = document.getElementById('setup-logodev').value.trim();
  if (tmdb) settings.tmdb_bearer_token = tmdb;
  if (trakt) settings.trakt_client_id = trakt;
  if (logodev) settings.logodev_api_key = logodev;

  // Save webhook host if it was set
  const webhookHost = document.getElementById('setup-webhook-host').value.trim();
  if (webhookHost) {
    const host = webhookHost.replace(/^https?:\/\//, '');
    settings.webhook_host = host;
    settings.sonarr_webhook_host = host;
  }

  settings.setup_complete = 'true';

  try {
    await api('/api/settings', { method: 'POST', body: { settings } });
    // Go to plugin install step instead of finishing
    setupGoTo(6);
  } catch (e) {
    toast(e.message, 'error');
  }
}

function finishSetup() {
  document.getElementById('setup-overlay').style.display = 'none';
  toast('Setup complete!');
  loadDashboard();
}

async function copyPluginUrl() {
  const url = document.getElementById('setup-plugin-url').value;
  try {
    await navigator.clipboard.writeText(url);
    toast('Copied!');
  } catch (_) {
    toast('Copy failed — use HTTPS or copy manually', 'error');
  }
}

function skipSetup() {
  document.getElementById('setup-overlay').style.display = 'none';
  api('/api/settings', { method: 'POST', body: { settings: { setup_complete: 'true' } } });
}

// ── Dashboard ─────────────────────────────────────────────────────────────
async function loadDashboard() {
  try {
    const [dash, providers, activity] = await Promise.all([
      api('/api/sync/dashboard'),
      api('/api/providers'),
      api('/api/sync/activity?limit=15'),
    ]);
    state.providers = providers;
    const cfg = dash.config || {};

    // Status cards — hide Radarr/Sonarr if not configured
    updateStatusCard('vod', dash.status.vod_sync.timestamp, dash.status.vod_sync.status === 'failed' ? 'error' : null, dash.running.vod_sync);
    const radarrCard = document.getElementById('status-radarr');
    const sonarrCard = document.getElementById('status-sonarr');
    if (radarrCard) radarrCard.style.display = cfg.radarr ? '' : 'none';
    if (sonarrCard) sonarrCard.style.display = cfg.sonarr ? '' : 'none';
    if (cfg.radarr) updateStatusCard('radarr', dash.status.radarr_scan.timestamp, null, dash.running.radarr_scan);
    if (cfg.sonarr) updateStatusCard('sonarr', dash.status.sonarr_scan.timestamp, null, dash.running.sonarr_scan);
    updateStatusCard('jellyfin', dash.status.jellyfin_push.timestamp);

    // Sidebar last sync
    const sidebarSync = document.getElementById('sidebar-last-sync');
    if (sidebarSync && dash.status.vod_sync.timestamp) {
      sidebarSync.textContent = dashTimeAgo(dash.status.vod_sync.timestamp);
    }

    // Library stats
    const lib = dash.library;
    document.getElementById('stat-movies').textContent = lib.total_movies.toLocaleString();
    document.getElementById('stat-movies-sub').textContent = `${lib.radarr_movies} downloaded, ${lib.vod_movies} VOD`;
    document.getElementById('stat-series').textContent = lib.total_series.toLocaleString();
    document.getElementById('stat-series-sub').textContent = `${lib.sonarr_series} downloaded, ${lib.vod_series} VOD`;
    document.getElementById('stat-downloaded').textContent = (lib.radarr_movies + lib.sonarr_series).toLocaleString();
    document.getElementById('stat-downloaded-sub').textContent = `${lib.radarr_movies} movies, ${lib.sonarr_series} series`;
    document.getElementById('stat-vod').textContent = (lib.vod_movies + lib.vod_series).toLocaleString();
    document.getElementById('stat-vod-sub').textContent = `${lib.vod_movies} movies, ${lib.vod_series} series`;

    // Duplicate badge
    const dup = document.getElementById('dup-badge');
    if (dup) {
      const pending = dash.library.pending_duplicates || 0;
      if (pending > 0) { dup.style.display = 'inline'; dup.textContent = pending; }
      else { dup.style.display = 'none'; }
    }

    // Activity feed
    renderActivityFeed(activity);

    // Getting Started checklist
    renderGettingStarted(cfg, dash);

    // Stale VOD files check (first startup only)
    checkStaleFiles();

    // Sync running state
    if (dash.running.vod_sync && !state._syncProviderId) {
      // Restore sync polling if a sync is running
      const active = providers.filter(p => p.active);
      if (active.length) {
        state._syncProviderId = active[0].id;
        setSyncRunning(true);
        pollSyncProgress();
      }
    }
  } catch (e) {
    console.error('Dashboard load error:', e);
  }
}

function renderGettingStarted(cfg, dash) {
  const el = document.getElementById('getting-started');
  const items = document.getElementById('getting-started-items');
  if (!el || !items) return;

  // Don't show if user dismissed it
  if (localStorage.getItem('tentacle_dismiss_checklist')) { el.style.display = 'none'; return; }

  const checks = [
    { done: true, label: 'Jellyfin connected' },
    { done: cfg.radarr, label: 'Radarr configured', hint: 'Optional — <a href="#" onclick="showPage(\'settings\');return false">Settings → Connections</a>' },
    { done: cfg.sonarr, label: 'Sonarr configured', hint: 'Optional — <a href="#" onclick="showPage(\'settings\');return false">Settings → Connections</a>' },
    { done: cfg.has_providers, label: 'IPTV provider added', hint: '<a href="#" onclick="showPage(\'settings\');return false">Settings → Providers → Add Provider</a>' },
    { done: dash.status.vod_sync.timestamp || (dash.library.total_movies + dash.library.total_series) > 0, label: 'Content synced' },
    { done: cfg.has_playlists, label: 'Playlists created', hint: '<a href="#" onclick="showPage(\'playlists\');return false">Go to Playlists</a>' },
    { done: cfg.has_home_screen, label: 'Home screen configured', hint: '<a href="#" onclick="showPage(\'playlists\');return false">Go to Playlists → Home Screen</a>' },
  ];

  const allDone = checks.every(c => c.done);
  // Also hide if user has been using the system for a while (has synced + has playlists)
  if (allDone) { el.style.display = 'none'; return; }

  // Show scanning banner if post-setup scan is running
  const scanning = dash.running.radarr_scan || dash.running.sonarr_scan;
  let html = '';
  if (scanning) {
    html += `<div style="padding:8px 12px;background:var(--bg2);border-radius:6px;margin-bottom:8px;font-size:13px;color:var(--amber)">Scanning your library... ${dash.library.radarr_movies} movies, ${dash.library.sonarr_series} series found so far</div>`;
  }

  for (const c of checks) {
    const icon = c.done ? '<span style="color:var(--green)">&#10003;</span>' : '<span style="color:var(--text3)">&#9675;</span>';
    const opacity = c.done ? 'opacity:0.5' : '';
    const hint = !c.done && c.hint ? ` <span style="font-size:11px;color:var(--text3)">${c.hint}</span>` : '';
    html += `<div style="padding:4px 12px;font-size:13px;${opacity}">${icon} ${c.label}${hint}</div>`;
  }

  items.innerHTML = html;
  el.style.display = '';
}

function dismissGettingStarted() {
  localStorage.setItem('tentacle_dismiss_checklist', '1');
  const el = document.getElementById('getting-started');
  if (el) el.style.display = 'none';
}

// ── Stale VOD Files ──────────────────────────────────────────────────────
let _staleData = null;

async function checkStaleFiles() {
  try {
    const data = await api('/api/settings/stale-files');
    if (!data.show) return;
    _staleData = data;
    document.getElementById('stale-strm-count').textContent = data.strm_count;
    document.getElementById('stale-files-banner').style.display = '';
  } catch (e) { /* ignore */ }
}

function confirmDeleteStaleFiles() {
  if (!_staleData) return;
  document.getElementById('stale-confirm-strm').textContent = _staleData.strm_count;
  document.getElementById('stale-confirm-nfo').textContent = _staleData.nfo_count;
  document.getElementById('stale-confirm-modal').style.display = 'flex';
}

async function executeDeleteStaleFiles() {
  document.getElementById('stale-confirm-modal').style.display = 'none';
  try {
    const res = await api('/api/settings/stale-files/delete', { method: 'POST' });
    document.getElementById('stale-files-banner').style.display = 'none';
    toast(`Deleted ${res.deleted_strm} .strm and ${res.deleted_nfo} .nfo files`);
  } catch (e) {
    toast('Failed to delete files', 'error');
  }
}

async function dismissStaleFiles() {
  try { await api('/api/settings/stale-files/dismiss', { method: 'POST' }); } catch (e) { /* ignore */ }
  document.getElementById('stale-files-banner').style.display = 'none';
}

function toggleAdvancedActions() {
  const body = document.getElementById('advanced-actions-body');
  const toggle = document.getElementById('advanced-toggle');
  if (body.style.display === 'none') {
    body.style.display = 'flex';
    toggle.innerHTML = '&#9662; Hide';
  } else {
    body.style.display = 'none';
    toggle.innerHTML = '&#9656; Show';
  }
}

// Note: timeAgo() is defined in pages.js (global scope) and accepts a Date object.
// dashTimeAgo() accepts an ISO string and is used by dashboard status cards.
function dashTimeAgo(isoStr) {
  if (!isoStr) return null;
  const d = new Date(isoStr.endsWith('Z') ? isoStr : isoStr + 'Z');
  const now = new Date();
  const diffMs = now - d;
  if (diffMs < 0) return 'just now';
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString();
}

function updateStatusCard(key, timestamp, errorStatus, isRunning) {
  const dot = document.getElementById(`dot-${key}`);
  const time = document.getElementById(`time-${key}`);
  if (!dot || !time) return;

  if (isRunning) {
    dot.className = 'dot dot-amber';
    time.textContent = 'Running...';
    return;
  }

  if (!timestamp) {
    dot.className = 'dot dot-gray';
    time.textContent = 'Never';
    return;
  }

  const ago = dashTimeAgo(timestamp);
  time.textContent = ago;

  if (errorStatus === 'error') {
    dot.className = 'dot dot-red';
  } else {
    // Green if within 24h, amber if older
    const ts = String(timestamp);
    const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z');
    const hrs = (Date.now() - d.getTime()) / 3600000;
    dot.className = hrs < 25 ? 'dot dot-green' : 'dot dot-amber';
  }
}

const _activityColors = {
  vod_sync: 'var(--accent)', radarr_scan: 'var(--green)', sonarr_scan: 'var(--pink)',
  radarr_add: 'var(--green)', sonarr_add: 'var(--pink)', radarr_remove: 'var(--red)',
  sonarr_remove: 'var(--red)', jellyfin_push: 'var(--amber)', list_fetch: 'var(--accent)',
  new_playlists: 'var(--purple)',
};

function renderActivityFeed(entries) {
  const el = document.getElementById('activity-feed');
  const btn = document.getElementById('activity-more-btn');
  if (!entries || !entries.length) {
    el.innerHTML = `<div style="padding:24px;text-align:center;color:var(--text3);font-size:13px">
      No activity yet — run a sync to get started
    </div>`;
    if (btn) btn.style.display = 'none';
    return;
  }

  el.innerHTML = entries.map(e => {
    let msg = e.message;
    if (e.event === 'new_playlists') {
      msg += ` <a href="#" onclick="showPage('playlists');return false" style="color:var(--accent);font-size:12px">View &rarr;</a>`;
    }
    return `
    <div class="activity-item">
      <div class="activity-icon" style="background:${_activityColors[e.event] || 'var(--text3)'}"></div>
      <div class="activity-msg">${msg}</div>
      <div class="activity-time">${dashTimeAgo(e.created_at) || ''}</div>
    </div>`;
  }).join('');

  if (btn) btn.style.display = entries.length >= 15 ? '' : 'none';
}

async function loadMoreActivity() {
  try {
    const entries = await api('/api/sync/activity?limit=50');
    const el = document.getElementById('activity-feed');
    el.style.maxHeight = '500px';
    renderActivityFeed(entries);
    const btn = document.getElementById('activity-more-btn');
    if (btn) btn.style.display = 'none';
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Providers ─────────────────────────────────────────────────────────────
async function loadProviders() {
  const grid = document.getElementById('provider-grid');
  try {
    const providers = await api('/api/providers');
    state.providers = providers;

    if (!providers.length) {
      grid.innerHTML = `
        <div style="grid-column:1/-1">
          <div class="empty-state">
            <div class="empty-icon">◉</div>
            <p style="margin-bottom:16px">No providers added yet</p>
            <button class="btn btn-primary" onclick="showAddProvider()">Add Your First Provider</button>
          </div>
        </div>`;
      return;
    }

    grid.innerHTML = providers.map(p => renderProviderCard(p)).join('');
  } catch (e) {
    grid.innerHTML = `<div class="empty-state"><p>Failed to load providers</p></div>`;
  }
}

function renderProviderCard(p) {
  const statusColor = p.status === 'ok' ? 'green' : p.status === 'error' ? 'red' : 'gray';
  const statusLabel = p.status === 'ok' ? 'Connected' : p.status === 'error' ? 'Error' : 'Untested';
  const expiry = p.expiry ? new Date(p.expiry).toLocaleDateString() : '—';

  // Capability badges
  let capBadges = '';
  if (p.has_vod) capBadges += '<span class="badge badge-accent" style="font-size:9px;padding:1px 5px">Movies</span>';
  if (p.has_series) capBadges += '<span class="badge badge-pink" style="font-size:9px;padding:1px 5px">Series</span>';
  if (p.has_live) capBadges += '<span class="badge badge-blue" style="font-size:9px;padding:1px 5px">IPTV</span>';
  if (!p.has_vod && !p.has_series && !p.has_live && p.status === 'ok') capBadges += '<span class="badge badge-gray" style="font-size:9px;padding:1px 5px">No content detected</span>';

  return `
    <div class="provider-card ${p.status === 'ok' ? 'active-provider' : ''}">
      <div class="provider-card-header">
        <div>
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px;flex-wrap:wrap">
            ${capBadges}
          </div>
          <div class="provider-card-name" style="margin-top:6px">${p.name}</div>
          <div class="provider-card-url">${p.server_url}</div>
        </div>
        <div style="display:flex;align-items:center;gap:6px">
          <div class="dot dot-${statusColor}"></div>
          <span style="font-size:11px;color:var(--text3)">${statusLabel}</span>
        </div>
      </div>

      <div class="provider-meta">
        <div class="provider-meta-item">Expires: ${expiry}</div>
        <div class="provider-meta-item">Max: ${p.max_connections || '?'} streams</div>
        ${p.last_tested ? `<div class="provider-meta-item">Tested: ${new Date(p.last_tested).toLocaleDateString()}</div>` : ''}
        ${p.last_synced ? `<div class="provider-meta-item">Synced: ${dashTimeAgo(p.last_synced)}</div>` : ''}
      </div>

      <div class="provider-actions">
        <button class="btn btn-secondary btn-sm" onclick="refreshProvider(${p.id})">Test</button>
        <button class="btn btn-secondary btn-sm" onclick="editProvider(${p.id})">Edit</button>
        <button class="btn btn-danger btn-sm" onclick="confirmDeleteProvider(${p.id}, '${p.name}')">Delete</button>
      </div>
    </div>`;
}

function showAddProvider() {
  state.editingProviderId = null;
  document.getElementById('provider-modal-title').textContent = 'Add Provider';
  document.getElementById('edit-provider-id').value = '';
  document.getElementById('p-name').value = '';
  document.getElementById('p-url').value = '';
  document.getElementById('p-user').value = '';
  document.getElementById('p-pass').value = '';
  document.getElementById('p-priority').value = '1';
  document.getElementById('p-require-tmdb').checked = true;
  showModal('modal-add-provider');
}

function editProvider(id) {
  const p = state.providers.find(p => p.id === id);
  if (!p) return;
  state.editingProviderId = id;
  document.getElementById('provider-modal-title').textContent = 'Edit Provider';
  document.getElementById('edit-provider-id').value = id;
  document.getElementById('p-name').value = p.name;
  document.getElementById('p-url').value = p.server_url;
  document.getElementById('p-user').value = p.username;
  document.getElementById('p-pass').value = '';
  document.getElementById('p-priority').value = p.priority;
  document.getElementById('p-require-tmdb').checked = p.require_tmdb_match !== false;
  showModal('modal-add-provider');
}

async function saveProvider() {
  const id = state.editingProviderId;
  const body = {
    name: document.getElementById('p-name').value.trim(),
    server_url: document.getElementById('p-url').value.trim(),
    username: document.getElementById('p-user').value.trim(),
    password: document.getElementById('p-pass').value.trim(),
    priority: parseInt(document.getElementById('p-priority').value) || 1,
    require_tmdb_match: document.getElementById('p-require-tmdb').checked,
  };

  if (!body.name || !body.server_url || !body.username) {
    toast('Name, URL and username are required', 'error');
    return;
  }

  // For edits, don't require password if empty
  if (id && !body.password) delete body.password;

  try {
    let providerId = id;
    if (id) {
      await api(`/api/providers/${id}`, { method: 'PUT', body });
      toast('Provider updated');
    } else {
      if (!body.password) { toast('Password required', 'error'); return; }
      const result = await api('/api/providers', { method: 'POST', body });
      providerId = result.id;
      toast('Provider added — connecting...', 'info');
    }
    closeModal('modal-add-provider');
    // Auto-test, fetch categories, and sync live groups
    try {
      const r = await api(`/api/providers/${providerId}/test`, { method: 'POST' });
      toast('Provider connected successfully');
      // Auto-fetch VOD categories if applicable
      if (r.has_vod || r.has_series) {
        api(`/api/providers/${providerId}/fetch-categories`, { method: 'POST' }).catch(() => {});
      }
      // Auto-sync live groups if applicable
      if (r.has_live) {
        api(`/api/live/sync/${providerId}`, { method: 'POST' }).catch(() => {});
      }
    } catch (e) {
      toast('Provider saved but connection failed — check credentials', 'error');
    }
    loadProviders();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function testProvider(id) {
  toast('Testing connection...', 'info');
  try {
    const r = await api(`/api/providers/${id}/test`, { method: 'POST' });
    toast(`Connected — expires ${new Date(r.expiry).toLocaleDateString()}, ${r.max_connections} max streams`);
    loadProviders();
  } catch (e) {
    toast(e.message, 'error');
    loadProviders();
  }
}

async function refreshProvider(id) {
  toast('Testing provider connection...', 'info');
  try {
    await api(`/api/providers/${id}/test`, { method: 'POST' });
    toast('Provider connection OK');
    loadProviders();
  } catch (e) {
    toast(e.message, 'error');
    loadProviders();
  }
}

function confirmDeleteProvider(id, name) {
  state.pendingDeleteProviderId = id;
  document.getElementById('delete-provider-name').textContent = name;
  showModal('modal-delete-provider');
}

async function executeDeleteProvider() {
  const id = state.pendingDeleteProviderId;
  if (!id) return;
  closeModal('modal-delete-provider');
  toast('Deleting provider and all content...', 'info');
  try {
    await api(`/api/providers/${id}`, { method: 'DELETE' });
    toast('Provider and all associated content deleted');
    loadProviders();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function fetchCategoriesForProvider(id) {
  try {
    await api(`/api/providers/${id}/fetch-categories`, { method: 'POST' });
  } catch (e) {}
}

// ── Categories Modal ───────────────────────────────────────────────────────
async function manageCategories(providerId, providerName) {
  document.getElementById('cat-modal-title').textContent = `Categories — ${providerName}`;
  document.getElementById('cat-list').innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  showModal('modal-categories');
  state.currentProviderId = providerId;

  // Fetch categories (also refresh from provider)
  try {
    await api(`/api/providers/${providerId}/fetch-categories`, { method: 'POST' });
  } catch (e) {}

  try {
    const cats = await api(`/api/providers/${providerId}/categories`);
    state.categories = cats;
    renderCatList();
  } catch (e) {
    document.getElementById('cat-list').innerHTML = '<div class="empty-state"><p>Failed to load categories</p></div>';
  }
}

function renderCatList() {
  const search = document.getElementById('cat-search').value.toLowerCase();
  const filter = state.catFilter;

  let cats = state.categories.filter(c => {
    if (search && !c.name.toLowerCase().includes(search)) return false;
    if (filter === 'movie') return c.type === 'movie';
    if (filter === 'series') return c.type === 'series';
    if (filter === 'active') return c.whitelisted;
    if (filter === 'english') return c.is_likely_english && !c.is_foreign;
    return true;
  });

  document.getElementById('cat-count').textContent =
    `Showing ${cats.length} of ${state.categories.length} categories — ${state.categories.filter(c=>c.whitelisted).length} active`;

  if (!cats.length) {
    document.getElementById('cat-list').innerHTML = '<div class="empty-state"><p>No categories match filter</p></div>';
    return;
  }

  document.getElementById('cat-list').innerHTML = cats.map(c => `
    <div class="cat-row ${c.whitelisted ? 'whitelisted' : ''}" data-id="${c.id}">
      <input type="checkbox" class="cat-checkbox" id="cat-${c.id}"
        ${c.whitelisted ? 'checked' : ''}
        onchange="toggleCatLocal(${c.id}, this.checked)">
      <label for="cat-${c.id}" class="cat-name" title="${c.name}">${c.name}</label>
      <span class="badge ${c.type === 'movie' ? 'badge-accent' : 'badge-pink'}">${c.type}</span>
      ${c.source_tag ? `<span class="badge badge-gray">${c.source_tag}</span>` : ''}
      ${c.title_count ? `<span style="font-size:11px;color:var(--text3);font-family:'DM Mono',monospace">${c.title_count}</span>` : ''}
    </div>
  `).join('');
}

function toggleCatLocal(id, checked) {
  const cat = state.categories.find(c => c.id === id);
  if (cat) cat.whitelisted = checked;
  const row = document.querySelector(`.cat-row[data-id="${id}"]`);
  if (row) row.classList.toggle('whitelisted', checked);
}

function filterCats() { renderCatList(); }

function setCatFilter(filter, btn) {
  state.catFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderCatList();
}

function selectAllVisible(checked) {
  document.querySelectorAll('.cat-checkbox').forEach(cb => {
    cb.checked = checked;
    const id = parseInt(cb.id.replace('cat-', ''));
    toggleCatLocal(id, checked);
  });
}

async function saveSelectedCategories() {
  const toEnable = state.categories.filter(c => c.whitelisted).map(c => String(c.id));
  const toDisable = state.categories.filter(c => !c.whitelisted).map(c => String(c.id));

  try {
    if (toEnable.length) {
      await api(`/api/providers/${state.currentProviderId}/categories/update`, {
        method: 'POST',
        body: { category_ids: toEnable, whitelisted: true }
      });
    }
    if (toDisable.length) {
      await api(`/api/providers/${state.currentProviderId}/categories/update`, {
        method: 'POST',
        body: { category_ids: toDisable, whitelisted: false }
      });
    }
    toast(`Saved — ${toEnable.length} categories active`);
    closeModal('modal-categories');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function previewSync(providerId) {
  toast('Fetching preview...', 'info');
  try {
    const r = await api(`/api/providers/${providerId}/preview`);
    toast(`Preview: ~${r.estimated_movies.toLocaleString()} movies from ${r.movie_categories} categories`, 'info', 6000);
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Settings ───────────────────────────────────────────────────────────────
async function loadSettings() {
  try {
    const settings = await api('/api/settings/raw');
    const fields = [
      'tmdb_bearer_token', 'radarr_url', 'radarr_api_key',
      'sonarr_url', 'sonarr_api_key',
      'jellyfin_url', 'jellyfin_api_key',
      'sync_schedule', 'recently_added_days', 'tmdb_match_threshold',
      'webhook_host', 'sonarr_webhook_host', 'trakt_client_id', 'logodev_api_key'
    ];
    fields.forEach(key => {
      const el = document.getElementById(key);
      if (el && settings[key]) el.value = settings[key];
    });
    showJellyfinLoginState(settings['jellyfin_user_id'] || '', settings['jellyfin_user_name'] || '');
    const discoverCb = document.getElementById('discover_in_jellyfin');
    if (discoverCb) discoverCb.checked = (settings['discover_in_jellyfin'] || '').toLowerCase() === 'true';
    loadPathStatus();
  } catch (e) {}
}

async function loadPathStatus() {
  const container = document.getElementById('paths-status');
  if (!container) return;
  try {
    const paths = await api('/api/settings/paths');
    let html = '';
    for (const [key, info] of Object.entries(paths)) {
      const ok = info.mounted && info.writable;
      const icon = ok
        ? '<span style="color:var(--green);font-size:16px">&#10003;</span>'
        : '<span style="color:var(--red);font-size:16px">&#10007;</span>';
      let status;
      if (ok) {
        status = '<span style="color:var(--green);font-size:12px">Mounted</span>';
      } else if (info.required) {
        status = '<span style="color:var(--red);font-size:12px">Not mounted — this is required</span>';
      } else {
        status = '<span style="color:var(--text3);font-size:12px">Not mounted — add a volume mount to your docker-compose.yml and restart</span>';
      }
      html += `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;${key !== 'tv' ? 'border-bottom:1px solid var(--border);' : ''}">
        <span style="width:20px;text-align:center">${icon}</span>
        <div style="flex:1">
          <div style="font-size:13px;font-weight:500;color:var(--text1)">${info.label}</div>
          <code style="font-size:11px;color:var(--text3)">${info.path}</code>
        </div>
        <div>${status}</div>
      </div>`;
    }
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = '<div style="color:var(--red);font-size:12px">Failed to check paths</div>';
  }
}

async function saveSettings() {
  const fields = [
    'tmdb_bearer_token', 'radarr_url', 'radarr_api_key',
    'sonarr_url', 'sonarr_api_key',
    'jellyfin_url', 'jellyfin_api_key',
    'sync_schedule', 'recently_added_days', 'tmdb_match_threshold',
    'webhook_host', 'sonarr_webhook_host', 'trakt_client_id', 'logodev_api_key'
  ];

  const settings = {};
  fields.forEach(key => {
    const el = document.getElementById(key);
    if (el) settings[key] = el.value.trim();
  });
  const discoverCb = document.getElementById('discover_in_jellyfin');
  if (discoverCb) settings['discover_in_jellyfin'] = discoverCb.checked ? 'true' : 'false';
  try {
    await api('/api/settings', { method: 'POST', body: { settings } });
    toast('Settings saved');
  } catch (e) {
    toast(e.message, 'error');
  }
}

function showJellyfinLoginState(userId, userName) {
  const statusEl = document.getElementById('jellyfin-login-status');
  const formEl = document.getElementById('jellyfin-login-form');
  if (userId && userName) {
    document.getElementById('jellyfin-logged-in-label').textContent = `Logged in as: ${userName}`;
    document.getElementById('jellyfin-user-id-display').textContent = `ID: ${userId}`;
    statusEl.style.display = 'block';
    formEl.style.display = 'none';
  } else {
    statusEl.style.display = 'none';
    formEl.style.display = 'block';
  }
}

function showJellyfinLogin() {
  document.getElementById('jellyfin-login-status').style.display = 'none';
  document.getElementById('jellyfin-login-form').style.display = 'block';
  document.getElementById('jf-username').value = '';
  document.getElementById('jf-password').value = '';
  document.getElementById('jf-login-error').style.display = 'none';
}

async function jellyfinLogin() {
  const username = document.getElementById('jf-username').value.trim();
  const password = document.getElementById('jf-password').value;
  const errorEl = document.getElementById('jf-login-error');
  const btn = document.getElementById('jf-login-btn');

  if (!username) {
    errorEl.textContent = 'Username is required';
    errorEl.style.display = 'block';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Logging in...';
  errorEl.style.display = 'none';

  try {
    const r = await api('/api/settings/jellyfin-login', {
      method: 'POST',
      body: { username, password }
    });
    toast(`Logged in as ${r.username}`);
    showJellyfinLoginState(r.user_id, r.username);
    toast('User saved. Run Sync SmartLists to update existing lists with this user.', 'info', 5000);
  } catch (e) {
    errorEl.textContent = e.message || 'Login failed';
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Login';
  }
}

async function testConnection(type) {
  const configs = {
    tmdb: { bearer_token: document.getElementById('tmdb_bearer_token')?.value.trim() },
    radarr: {
      url: document.getElementById('radarr_url')?.value.trim(),
      api_key: document.getElementById('radarr_api_key')?.value.trim()
    },
    sonarr: {
      url: document.getElementById('sonarr_url')?.value.trim(),
      api_key: document.getElementById('sonarr_api_key')?.value.trim()
    },
    jellyfin: {
      url: document.getElementById('jellyfin_url')?.value.trim(),
      api_key: document.getElementById('jellyfin_api_key')?.value.trim()
    }
  };

  toast(`Testing ${type}...`, 'info');
  try {
    const r = await api('/api/settings/test', {
      method: 'POST',
      body: { type, ...configs[type] }
    });
    toast(r.message);
  } catch (e) {
    toast(e.message, 'error');
  }
}

function getWebhookUrl() {
  let host = document.getElementById('webhook_host').value.trim();
  if (!host) return null;
  host = host.replace(/^https?:\/\//, '');
  return `http://${host}/api/radarr/webhook`;
}

async function copyWebhookUrl() {
  const url = getWebhookUrl();
  if (!url) { toast('Enter a webhook host first', 'error'); return; }
  try {
    await navigator.clipboard.writeText(url);
    toast('Copied!');
  } catch (_) {
    toast('Copy failed — use HTTPS or copy manually', 'error');
  }
}

async function testWebhookUrl() {
  const url = getWebhookUrl();
  if (!url) { toast('Enter a webhook host first', 'error'); return; }
  try {
    const r = await api('/api/settings/test-webhook', { method: 'POST', body: JSON.stringify({ url }) });
    toast(r.message);
  } catch (e) {
    toast('Webhook test failed: ' + e.message, 'error');
  }
}

// ── Sonarr Webhook ────────────────────────────────────────────────────────
function getSonarrWebhookUrl() {
  let host = document.getElementById('sonarr_webhook_host').value.trim();
  if (!host) return null;
  host = host.replace(/^https?:\/\//, '');
  return `http://${host}/api/sonarr/webhook`;
}

async function copySonarrWebhookUrl() {
  const url = getSonarrWebhookUrl();
  if (!url) { toast('Enter a webhook host first', 'error'); return; }
  try {
    await navigator.clipboard.writeText(url);
    toast('Copied!');
  } catch (_) {
    toast('Copy failed — use HTTPS or copy manually', 'error');
  }
}

async function testSonarrWebhookUrl() {
  const url = getSonarrWebhookUrl();
  if (!url) { toast('Enter a webhook host first', 'error'); return; }
  try {
    const r = await api('/api/settings/test-webhook', { method: 'POST', body: JSON.stringify({ url }) });
    toast(r.message);
  } catch (e) {
    toast('Webhook test failed: ' + e.message, 'error');
  }
}

// ── Radarr Root Folders ───────────────────────────────────────────────────
// ── Modals ─────────────────────────────────────────────────────────────────
function showModal(id) {
  document.getElementById(id).style.display = 'flex';
}

function closeModal(id) {
  document.getElementById(id).style.display = 'none';
}

// Close modal on overlay click
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal-overlay')) {
    e.target.style.display = 'none';
  }
});

// Close modal on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay').forEach(m => {
      if (m.id !== 'setup-overlay') m.style.display = 'none';
    });
  }
});

// ── Sync ──────────────────────────────────────────────────────────────────
async function checkRunningSyncs() {
  // Dashboard loadDashboard() handles detecting running syncs now
}

async function syncNow() {
  const active = (state.providers || []).filter(p => p.active);
  if (!active.length) {
    toast('No active providers. Activate a provider first.', 'error');
    return;
  }
  const provider = active[0];
  state._syncProviderId = provider.id;
  setSyncRunning(true);
  try {
    await api('/api/sync/trigger', {
      method: 'POST',
      body: { provider_id: provider.id, sync_type: 'full' }
    });
    toast('Full sync started — VOD, Radarr, Sonarr, Jellyfin', 'info');
    pollSyncProgress();
  } catch (e) {
    toast(e.message, 'error');
    setSyncRunning(false);
  }
}

async function runSync(type) {
  const active = (state.providers || []).filter(p => p.active);
  if (!active.length) {
    toast('No active providers. Activate a provider first.', 'error');
    return;
  }
  const provider = active[0];
  state._syncProviderId = provider.id;
  setSyncRunning(true);
  try {
    await api('/api/sync/trigger', {
      method: 'POST',
      body: { provider_id: provider.id, sync_type: type }
    });
    toast(`VOD ${type} sync started`, 'info');
    pollSyncProgress();
  } catch (e) {
    toast(e.message, 'error');
    setSyncRunning(false);
  }
}

async function cancelSync() {
  const pid = state._syncProviderId;
  if (!pid) { toast('No sync running', 'error'); return; }
  const cancelBtn = document.getElementById('sync-cancel-btn');
  if (cancelBtn) { cancelBtn.disabled = true; cancelBtn.textContent = 'Cancelling...'; }
  try {
    await api('/api/sync/cancel', {
      method: 'POST',
      body: { provider_id: pid }
    });
    toast('Cancel signal sent — finishing current item...', 'info');
  } catch (e) {
    toast(e.message, 'error');
    if (cancelBtn) { cancelBtn.disabled = false; cancelBtn.textContent = 'Cancel'; }
  }
}

function setSyncRunning(running) {
  const syncBtn = document.getElementById('sync-now-btn');
  const cancelBtn = document.getElementById('sync-cancel-btn');
  const progressEl = document.getElementById('dash-progress');
  if (running) {
    if (syncBtn) { syncBtn.disabled = true; syncBtn.textContent = 'Syncing...'; }
    if (cancelBtn) { cancelBtn.style.display = ''; cancelBtn.disabled = false; cancelBtn.textContent = 'Cancel'; }
    if (progressEl) progressEl.style.display = '';
    // Update VOD status card
    updateStatusCard('vod', null, null, true);
  } else {
    if (syncBtn) { syncBtn.disabled = false; syncBtn.textContent = 'Sync Now'; }
    if (cancelBtn) { cancelBtn.style.display = 'none'; }
    if (progressEl) progressEl.style.display = 'none';
    state._syncProviderId = null;
    if (state._syncPollInterval) { clearInterval(state._syncPollInterval); state._syncPollInterval = null; }
  }
}

function pollSyncProgress() {
  if (state._syncPollInterval) clearInterval(state._syncPollInterval);
  state._syncPollInterval = setInterval(async () => {
    const pid = state._syncProviderId;
    if (!pid) { clearInterval(state._syncPollInterval); return; }
    try {
      const p = await api(`/api/sync/progress/${pid}/poll`);
      const label = document.getElementById('dash-progress-label');
      const detail = document.getElementById('dash-progress-detail');

      if (p.phase === 'movies' || p.phase === 'series') {
        if (label) label.textContent = `Syncing VOD ${p.phase}...`;
        if (detail) {
          const s = p.stats || {};
          let line = p.category || '';
          if (s.item_pos > 0 && s.item_total > 0) {
            line += ` — ${s.item_pos}/${s.item_total}`;
            if (s.item_title) line += `: ${s.item_title}`;
            line += ` (${(s.movies_new||0)+(s.series_new||0)} new)`;
          } else if (s.item_title) {
            line += ` — ${s.item_title}`;
          }
          detail.textContent = line;
        }
      } else if (p.phase === 'jellyfin_scan') {
        if (label) label.textContent = 'Syncing to Jellyfin...';
        if (detail) detail.textContent = 'Scanning library, pushing tags, updating playlists';
      } else if (p.phase === 'complete') {
        const n = ((p.stats||{}).movies_new||0) + ((p.stats||{}).series_new||0);
        toast(n > 0 ? `Sync complete — ${n} new items` : 'Sync complete', 'success');
        setSyncRunning(false);
        loadDashboard();
      } else if (p.phase === 'cancelled') {
        toast('Sync was cancelled', 'info');
        setSyncRunning(false);
        loadDashboard();
      } else if (p.phase === 'error') {
        toast('Sync failed', 'error');
        setSyncRunning(false);
        loadDashboard();
      } else if (!p.running) {
        setSyncRunning(false);
        loadDashboard();
      }
    } catch (e) {
      // Don't kill polling on transient errors
    }
  }, 500);
}

async function refreshLists(btn) {
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Refreshing...';
  try {
    const r = await api('/api/lists/refresh-all', { method: 'POST' });
    toast(`Refreshed ${r.refreshed} lists${r.errors.length ? ` (${r.errors.length} errors)` : ''}`, 'success');
    loadDashboard();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

async function dashRefreshTags(btn) {
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Pushing...';
  try {
    const r = await api('/api/sync/refresh-tags', { method: 'POST' });
    toast(`Tags pushed — ${r.jellyfin_tagged || 0} items updated in Jellyfin`, 'success');
    loadDashboard();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}