// Tentacle - live updates for the open page.
//
// One channel per signed-in tab (GET /api/updates, server-sent events,
// services/live_updates.py). The server sends short "this changed" notes by
// topic; the page re-reads only what it shows for that topic. When the
// channel (re)connects, after the tab was in the background or the line
// dropped, everything shown is read again: notes sent meanwhile are not
// replayed. A tab hidden for a minute closes its channel and opens it again
// when it comes back to the front.

const live = {
  es: null,
  connected: false,
  everConnected: false,
  hiddenTimer: null,
  pending: new Set(),
  progressOnly: true,
  timer: null,
  lastWrite: 0,       // when this tab last changed something (api() non-GET)
  handlers: {},
};
const LIVE_HIDDEN_CLOSE_MS = 60000;
const LIVE_GATHER_MS = 150;          // a burst (approve = requests + activity) is one read
const LIVE_OWN_CHANGE_MS = 2500;     // this tab's own save: its page is already up to date

function liveStart() {
  if (live.es || typeof EventSource === 'undefined' || !state.currentUser) return;
  const es = new EventSource('/api/updates');
  live.es = es;
  es.addEventListener('hello', () => {
    live.connected = true;
    // Every (re)connect but the first after sign-in: catch up on what was missed.
    if (live.everConnected) liveQueue('resync');
    live.everConnected = true;
  });
  es.addEventListener('change', (e) => {
    let note;
    try { note = JSON.parse(e.data); } catch (_) { return; }
    if (!note || !note.topic) return;
    if (note.topic === 'library') { liveLibrary(note.data || {}); return; }
    liveQueue(note.topic, note.data);
  });
  es.onerror = () => {
    live.connected = false;
    if (state.sessionExpired) liveStop();
    // Otherwise EventSource reconnects by itself (retry: 3 s), then says hello.
  };
}

function liveStop() {
  if (live.es) { live.es.close(); live.es = null; }
  live.connected = false;
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(live.hiddenTimer);
    live.hiddenTimer = setTimeout(liveStop, LIVE_HIDDEN_CLOSE_MS);
  } else {
    clearTimeout(live.hiddenTimer);
    if (!live.es && state.currentUser && !state.sessionExpired) {
      liveStart();   // its hello catches up (everConnected)
    }
  }
});

function liveQueue(topic, data) {
  live.pending.add(topic);
  if (topic === 'activity' && !(data && data.progress)) live.progressOnly = false;
  if (live.timer) return;
  live.timer = setTimeout(liveRun, LIVE_GATHER_MS);
}

function liveRun() {
  const topics = live.pending;
  const progressOnly = live.progressOnly;
  live.pending = new Set();
  live.progressOnly = true;
  live.timer = null;
  const all = topics.has('resync');
  // Each loader at most once per run (several topics share one: a reconnect
  // runs them all).
  const ran = new Set();
  live.once = (fn, ...args) => {
    if (typeof fn !== 'function' || ran.has(fn)) return;
    ran.add(fn);
    fn(...args);
  };
  for (const [topic, fn] of Object.entries(live.handlers)) {
    if (!all && !topics.has(topic)) continue;
    try { fn({ resync: all, progressOnly: !all && topic === 'activity' && progressOnly }); } catch (e) { console.warn('live', topic, e); }
  }
}

// Call a page's loader (if that page's script defines it), once per run.
function _run(name, ...args) { live.once(globalThis[name], ...args); }

// Something this tab just saved: a note about it shouldn't redraw what the
// person is busy with (the page already shows it).
function liveOwnChange() { return Date.now() - live.lastWrite < LIVE_OWN_CHANGE_MS; }

// A control on the page has the focus or a dialog is open: redrawing now
// would close a list being picked from or lose what is being typed.
function liveBusy(pageId) {
  return !!(document.querySelector('.modal-overlay[style*="flex"]')
    || document.activeElement?.closest?.(`#${pageId} select, #${pageId} input, #${pageId} textarea`));
}

const _isAdmin = () => !!(state.currentUser && state.currentUser.is_admin);

// ── What each topic redraws ────────────────────────────────────────────────

live.handlers.requests = () => {
  if (_isAdmin()) {
    if (state.currentPage === 'requests') _run('loadRequests');
    else _run('famLoadBadge');
  }
  if (state.currentPage === 'discover') {
    _run('loadActivity');
    _run('famLoadMyRequests');
  }
  liveDetailRefresh();
};

live.handlers.activity = ({ progressOnly }) => {
  if (state.currentPage === 'discover') {
    const panel = document.getElementById('discover-tab-activity');
    const open = panel && panel.style.display !== 'none';
    // Progress only moves bars: only an open Activity tab shows them.
    if (!progressOnly || open) _run('loadActivity');
    if (!progressOnly) liveDetailRefresh();
  }
  if (state.currentPage === 'health') _run('loadHealthDownloads');
  // A download became ready to watch: its Library card gets ▶ Play.
  if (!progressOnly && state.currentPage === 'library') _run('libRefreshWatch');
};

live.handlers.notifications = () => {};

live.handlers.people = () => {
  if (state.currentPage === 'people') _run('famRefreshPeople');
};

live.handlers.me = async () => {
  // An admin changed this person (kids profile, how requests work, Live TV).
  try {
    const me = await fetch('/api/auth/me');
    if (me.ok) { state.currentUser = await me.json(); applyUserRole(); }
  } catch (_) { /* the next note tries again */ }
  _run('famInit');
  if (state.currentPage === 'mychannels') _run('loadMyChannels');
};

live.handlers.health = () => {
  if (!_isAdmin()) { _run('famRefreshBanner'); return; }
  if (state.currentPage === 'health') { _run('loadHealthPage'); return; }
  // A provider switched address (the Health banner's Switch): its card
  if (state.currentPage === 'sources' && state.sourcesTab === 'providers' && !liveOwnChange()
      && !liveBusy('page-sources')) _run('loadProviders');
  _run('loadArrProblems');
  _run('loadProviderProblems');
  _run('loadHealthAlerts');
};

// What's wrong with Live TV's provider changed (down, back, some channels):
// the banner says so without a reload.
live.handlers.status = () => {
  if (!_isAdmin()) { _run('famRefreshBanner'); return; }
  if (state.currentPage === 'health') { _run('loadHealthPage'); return; }
  _run('loadProviderProblems');
};

live.handlers.home = () => {
  if (state.currentPage !== 'jellyfin' || liveOwnChange() || liveBusy('page-jellyfin')) return;
  _run('loadJellyfinPage');
};

live.handlers.settings = () => {
  if (state.currentPage === 'jellyfin') _run('loadNotificationState');
};

live.handlers.mychannels = () => {
  if (state.currentPage === 'mychannels' && !liveOwnChange() && !liveBusy('page-mychannels')) _run('loadMyChannels');
};

live.handlers.livetv = () => {
  if (liveOwnChange()) return;
  if (state.currentPage === 'mychannels' && !liveBusy('page-mychannels')) _run('loadMyChannels');
  if (state.currentPage === 'sources' && state.sourcesTab === 'live' && !liveBusy('page-sources')) _run('loadLiveTV');
};

// Someone's YouTube channels changed (services/youtube/follows.py).
live.handlers.youtube = () => {
  if (state.currentPage === 'myyoutube' && !liveOwnChange() && !liveBusy('page-myyoutube')) _run('loadMyYouTube');
  if (_isAdmin() && state.currentPage === 'sources' && state.sourcesTab === 'youtube' && !liveOwnChange()
      && !liveBusy('page-sources')) _run('loadYouTubeChannels');
};

live.handlers.groups = () => {
  if (!_isAdmin()) return;
  _run('nfpLoadCounts');
  if (state.currentPage === 'sources' && !liveOwnChange() && !liveBusy('page-sources')) {
    if (state.sourcesTab === 'live') _run('loadNewGroups');
    if (state.sourcesTab === 'providers') _run('loadNewCategories');
  }
};

// A team or rule changed, or what will record (everyone: recordings are the household's).
live.handlers.recordings = () => {
  if (state.currentPage === 'recordings' && !liveBusy('page-recordings')) _run('loadRecordings');
  if (_isAdmin() && state.currentPage === 'sources' && state.sourcesTab === 'live') _run('recArtLoad');
};

// A reconnect runs every topic's handler (above); and the Library.
live.handlers.resync = () => {
  if (state.currentPage === 'library') _run('_debouncedLibRefresh');
};

// Library: titles that came or went (the old per-page stream's events).
function liveLibrary(d) {
  if (state.currentPage !== 'library') return;
  if (d.event === 'movie_added' || d.event === 'series_added') { _debouncedLibRefresh(); return; }
  if (d.event === 'movie_removed' || d.event === 'series_removed') libRemoveCard(d.tmdb_id);
}

// The Discover detail that is open follows its title's state (asked,
// approved and downloading, declined).
function liveDetailRefresh() {
  const m = document.getElementById('modal-media-detail');
  if (!m || m.style.display !== 'flex' || !_discoverDetailArgs) return;
  showDiscoverDetail(..._discoverDetailArgs, true);
}
