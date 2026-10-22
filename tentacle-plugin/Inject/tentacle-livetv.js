// Tentacle Live TV — the guide, replacing Jellyfin's Live TV landing and guide tabs
//
// Architecture (same pattern as tentacle-search.js):
//   - #tentacleLiveTvContainer mounted on document.body
//   - Body class `tentacle-livetv-active` hides native content
//   - Active on #/livetv with no tab, tab 0 (Programs) or tab 1 (Guide);
//     Channels, Recordings, Schedule and Series stay native.
//   - Favorites / All toggle: Favorites = this person's favourite channels
//     (Jellyfin favourites are per user), All = every channel they can see.
//     The choice is kept in Jellyfin's display preferences (id tentacle-livetv,
//     client tentacle, CustomPrefs.guideView) so it follows the person, plus
//     localStorage for an instant first paint.
//   - Dates only as far as the guide data goes: today up to the day of the
//     furthest programme end (in the current view), not Jellyfin's 14 days.
//   - Fast with thousands of channels: one channel list request, rows are
//     virtualized (only the visible ones exist), programmes are fetched per
//     day for the visible rows in batches (POST LiveTv/Programs).
//   - Click a programme on now / a channel → play; a later programme → its
//     details page (record from there); the star toggles the favourite.
//   - Live: a favourite changed anywhere (Tentacle's My Channels, this guide,
//     another device) shows within about a second (Jellyfin's UserDataChanged
//     on its websocket); a lineup change (LibraryChanged, Jellyfin's or the one
//     Tentacle sends after setting Live TV up) re-reads the channel list. Only
//     the rows that changed are redrawn; the scroll position stays.
(function () {
  'use strict';

  var PREFS_ID = 'tentacle-livetv';
  var PREFS_CLIENT = 'tentacle';
  var BATCH = 100;            // channels per programme request
  var OVERSCAN = 8;           // rows rendered above/below the viewport
  var PREFETCH = 30;          // rows whose programmes are fetched ahead
  var POLL_MS = 10000;        // channel list re-read this often when there is no websocket

  var G = {
    active: false,
    container: null,
    notice: '',             // Tentacle's plain words while Live TV is down ("" = fine)
    userId: '',
    view: '',               // 'favorites' | 'all'
    channels: null,         // every channel this person can see (Jellyfin order)
    loadError: false,
    query: '',
    rows: [],               // channels shown (view + search)
    days: [],               // [{start: ms, end: ms}]
    dayIndex: 0,
    maxEnd: {},             // view -> furthest programme end (ms, 0 = no data)
    progs: {},              // dayStart -> {channelId: [programmes]}
    pending: {},            // dayStart + ':' + channelId -> true
    mounted: {},            // channelId -> row element
    tick: null,
    live: null,             // live-update timer (socket watch, fallback poll)
    socket: null,           // the Jellyfin websocket we listen on
    reload: { timer: null, busy: false, again: false, at: 0 },
    scrollRaf: 0,
    m: null,                // metrics for the current screen size
  };

  // ── Helpers ────────────────────────────────────────────────────────

  function esc(str) {
    if (str === null || str === undefined) return '';
    var d = document.createElement('div');
    d.textContent = String(str);
    return d.innerHTML;
  }

  // For attributes: esc() leaves quotes as they are, which would end the attribute.
  function escAttr(str) {
    return esc(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function api() { return window.ApiClient; }

  function apiGet(path, params) {
    return api().getJSON(api().getUrl(path, params));
  }

  function apiPost(path, body) {
    return api().ajax({
      type: 'POST',
      url: api().getUrl(path),
      data: JSON.stringify(body),
      contentType: 'application/json',
      dataType: 'json',
    });
  }

  function metrics() {
    var phone = window.innerWidth < 700;
    return phone
      ? { phone: true, ppm: 4, rowH: 60, chanW: 92, headH: 34 }
      : { phone: false, ppm: 6.5, rowH: 66, chanW: 260, headH: 38 };
  }

  var MIN = 60000;

  function startOfDay(t) {
    var d = new Date(t);
    d.setHours(0, 0, 0, 0);
    return d.getTime();
  }

  function addDays(t, n) {
    var d = new Date(t);
    d.setDate(d.getDate() + n);
    return d.getTime();
  }

  function fmtTime(t) {
    return new Date(t).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  // "2:00 – 4:30 PM": the AM/PM once when both ends share it.
  function fmtRange(a, b) {
    var x = fmtTime(a), y = fmtTime(b);
    var mx = x.match(/^(.*?)\s?([AaPp]\.?[Mm]\.?)$/), my = y.match(/^(.*?)\s?([AaPp]\.?[Mm]\.?)$/);
    if (mx && my && mx[2] === my[2]) x = mx[1];
    return x + ' – ' + y;
  }

  function dayLabel(start, i) {
    if (i === 0) return 'Today';
    if (i === 1) return 'Tomorrow';
    var d = new Date(start);
    return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + d.getDate();
  }

  // ── Route detection ────────────────────────────────────────────────

  function isGuidePage() {
    var h = (location.hash || '').replace('#', '').replace(/^\//, '');
    var path = h.split('?')[0];
    if (path !== 'livetv' && path !== 'livetv.html') return false;
    var match = h.match(/[?&]tab=(\d+)/);
    if (match && parseInt(match[1], 10) > 1) return false;
    return true;
  }

  // ── Activate / Deactivate ──────────────────────────────────────────

  function activate() {
    if (window.TentacleStandAside && window.TentacleStandAside()) return; // TV layout — native page renders
    if (G.active) return;
    G.active = true;
    document.body.classList.add('tentacle-livetv-active');
    ensureContainer();
    G.container.style.display = 'flex';
    start();
  }

  function deactivate() {
    if (!G.active) return;
    G.active = false;
    document.body.classList.remove('tentacle-livetv-active');
    if (G.container) G.container.style.display = 'none';
    if (G.tick) { clearInterval(G.tick); G.tick = null; }
    if (G.live) { clearInterval(G.live); G.live = null; }
    if (G.reload.timer) { clearTimeout(G.reload.timer); G.reload.timer = null; }
  }

  // ── Container ──────────────────────────────────────────────────────

  function ensureContainer() {
    if (G.container) return;
    var el = document.createElement('div');
    el.id = 'tentacleLiveTvContainer';
    document.body.appendChild(el);
    el.addEventListener('click', onClick);
    el.addEventListener('input', onInput);
    el.addEventListener('focusout', function (e) {
      if (e.target.matches('.tg-search input') && !e.target.value) {
        var top = el.querySelector('.tg-top');
        if (top) top.classList.remove('searching');
      }
    });
    G.container = el;
    window.addEventListener('resize', function () {
      if (!G.active) return;
      var m = metrics();
      if (G.m && m.phone === G.m.phone) return;
      renderGrid(true);
    });
  }

  // Family: "Live TV is unavailable right now. Lucas knows." from Tentacle
  // (TentacleDiscover/FamilyStatus), shown above the guide while it lasts.
  function loadNotice() {
    return apiGet('TentacleDiscover/FamilyStatus', { userId: G.userId })
      .then(function (r) {
        var m = ((r && r.messages) || []).filter(function (x) { return x.area === 'livetv'; })[0];
        G.notice = m ? String(m.text || '') : '';
      })
      .catch(function () { G.notice = ''; });
  }

  function showNotice() {
    var n = G.container && G.container.querySelector('.tltv-notice');
    if (n) { n.textContent = G.notice; n.style.display = G.notice ? '' : 'none'; }
  }

  // ── Start: user, saved view, channels ─────────────────────────────

  function start() {
    var uid = api().getCurrentUserId();
    if (uid !== G.userId) {
      G.userId = uid;
      G.view = '';
      G.channels = null;
      G.progs = {};
      G.pending = {};
      G.maxEnd = {};
      G.query = '';
    }
    if (!G.view) {
      var saved = '';
      try { saved = localStorage.getItem('tentacle-guide-view-' + uid) || ''; } catch (e) { }
      G.view = saved === 'all' ? 'all' : 'favorites';
    }
    G.m = metrics();
    renderShell();
    loadNotice().then(showNotice);
    loadSavedView();
    loadChannels().then(function () { if (G.active) applyView(true); });
    if (G.tick) clearInterval(G.tick);
    G.tick = setInterval(onTick, 30000);
    G.reload.at = Date.now();
    watchSocket();
    if (G.live) clearInterval(G.live);
    G.live = setInterval(onLiveTick, 2000);
  }

  // The choice follows the person between browsers: Jellyfin's display
  // preferences are per user. localStorage only makes the first paint right.
  function loadSavedView() {
    apiGet('DisplayPreferences/' + PREFS_ID, { userId: G.userId, client: PREFS_CLIENT })
      .then(function (p) {
        var v = p && p.CustomPrefs && p.CustomPrefs.guideView;
        if ((v === 'all' || v === 'favorites') && v !== G.view && G.active) {
          G.view = v;
          rememberLocal();
          renderToggle();
          if (G.channels) applyView(true);
        }
      })
      .catch(function () { });
  }

  function rememberLocal() {
    try { localStorage.setItem('tentacle-guide-view-' + G.userId, G.view); } catch (e) { }
  }

  function saveView() {
    rememberLocal();
    var path = 'DisplayPreferences/' + PREFS_ID;
    var q = { userId: G.userId, client: PREFS_CLIENT };
    apiGet(path, q).catch(function () { return {}; }).then(function (p) {
      p = p || {};
      p.Id = PREFS_ID;
      p.Client = PREFS_CLIENT;
      p.CustomPrefs = p.CustomPrefs || {};
      p.CustomPrefs.guideView = G.view;
      return api().ajax({
        type: 'POST',
        url: api().getUrl(path, q),
        data: JSON.stringify(p),
        contentType: 'application/json',
      });
    }).catch(function () { });
  }

  function loadChannels() {
    G.loadError = false;
    return fetchChannels().then(function (list) {
      G.channels = list;
    }).catch(function () {
      G.channels = [];
      G.loadError = true;
    });
  }

  // Every channel this person can see, in Jellyfin's order.
  function fetchChannels() {
    return apiGet('LiveTv/Channels', {
      UserId: G.userId,
      EnableImages: true,
      ImageTypeLimit: 1,
      EnableImageTypes: 'Primary',
      EnableUserData: true,
      AddCurrentProgram: false,
      EnableFavoriteSorting: false,
      Fields: '',
    }).then(function (r) {
      return ((r && r.Items) || []).map(function (c) {
        return {
          id: c.Id,
          name: c.Name || '',
          number: c.ChannelNumber || c.Number || '',
          logo: (c.ImageTags && c.ImageTags.Primary) || '',
          fav: !!(c.UserData && c.UserData.IsFavorite),
        };
      });
    });
  }

  // Furthest programme end in the guide for this view (favourites: only
  // their channels), so the date strip stops where the data stops.
  function loadMaxEnd(view) {
    if (G.maxEnd[view] !== undefined) return Promise.resolve(G.maxEnd[view]);
    var body = {
      UserId: G.userId,
      SortBy: ['StartDate'],
      SortOrder: ['Descending'],
      Limit: 1,
      EnableImages: false,
      EnableUserData: false,
      EnableTotalRecordCount: false,
    };
    if (view === 'favorites') {
      var ids = favChannels().map(function (c) { return c.id; });
      if (!ids.length) { G.maxEnd[view] = 0; return Promise.resolve(0); }
      body.ChannelIds = ids;
    }
    return apiPost('LiveTv/Programs', body).then(function (r) {
      var p = r && r.Items && r.Items[0];
      G.maxEnd[view] = p ? Date.parse(p.EndDate) : 0;
      return G.maxEnd[view];
    }).catch(function () { G.maxEnd[view] = 0; return 0; });
  }

  function favChannels() {
    return (G.channels || []).filter(function (c) { return c.fav; });
  }

  function computeDays(maxEnd) {
    var today = startOfDay(Date.now());
    var days = [];
    var last = maxEnd > Date.now() ? startOfDay(maxEnd - 1) : today;
    for (var s = today, i = 0; s <= last && i < 21; s = addDays(s, 1), i++) {
      var end = addDays(s, 1);
      // The last day ends with the data (to the next whole hour), not at midnight.
      if (s === last && maxEnd > s && maxEnd < end) {
        end = Math.min(end, Math.ceil(maxEnd / (60 * MIN)) * 60 * MIN);
      }
      days.push({ start: s, end: end });
    }
    return days;
  }

  // ── View: Favorites / All + search ────────────────────────────────

  function computeRows() {
    var base = G.view === 'favorites' ? favChannels() : (G.channels || []);
    var q = G.query.trim().toLowerCase();
    G.rows = q ? base.filter(function (c) {
      return c.name.toLowerCase().indexOf(q) !== -1 || String(c.number).indexOf(q) === 0;
    }) : base;
  }

  function applyView(scrollToNow) {
    computeRows();
    renderToggle();
    loadMaxEnd(G.view).then(function (maxEnd) {
      if (!G.active) return;
      var before = G.days.length && G.days[G.dayIndex] ? G.days[G.dayIndex].start : 0;
      G.days = computeDays(maxEnd);
      G.dayIndex = 0;
      for (var i = 0; i < G.days.length; i++) if (G.days[i].start === before) G.dayIndex = i;
      renderDays();
      renderGrid(scrollToNow);
    });
  }

  // ── Render: shell (header, toggle, days) ──────────────────────────

  var ICON_REC = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><path d="M12 2a10 10 0 100 20 10 10 0 000-20zm0 18a8 8 0 110-16 8 8 0 010 16z"/></svg>';
  var ICON_SCHED = '<svg viewBox="0 0 24 24"><path d="M19 4h-1V2h-2v2H8V2H6v2H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2V6a2 2 0 00-2-2zm0 16H5V9h14v11zM7 11h5v5H7z"/></svg>';
  var ICON_SEARCH = '<svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0016 9.5 6.5 6.5 0 109.5 16a6.47 6.47 0 004.23-1.57l.27.28v.79l5 5L20.5 19l-5-5zm-6 0A4.5 4.5 0 1114 9.5 4.49 4.49 0 019.5 14z"/></svg>';
  var STAR_ON = '<svg viewBox="0 0 24 24"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>';
  var STAR_OFF = '<svg viewBox="0 0 24 24"><path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/></svg>';

  function renderShell() {
    G.mounted = {};
    G.container.innerHTML =
      '<div class="tg-top' + (G.query ? ' searching' : '') + '">' +
        '<div class="tg-title">Live TV</div>' +
        '<div class="tg-toggle" role="tablist"></div>' +
        '<label class="tg-search">' + ICON_SEARCH +
          '<input type="search" placeholder="Find a channel" aria-label="Find a channel" value="' + escAttr(G.query) + '">' +
        '</label>' +
        '<div class="tg-links">' +
          '<a class="tg-link" href="#/livetv?tab=3" title="Recordings">' + ICON_REC + '<span>Recordings</span></a>' +
          '<a class="tg-link" href="#/livetv?tab=4" title="Schedule">' + ICON_SCHED + '<span>Schedule</span></a>' +
        '</div>' +
      '</div>' +
      '<div class="tltv-notice" role="status"' + (G.notice ? '' : ' style="display:none"') + '>' + esc(G.notice) + '</div>' +
      '<div class="tg-days"></div>' +
      '<div class="tg-body"><div class="tg-loading">Loading the guide…</div></div>';
    renderToggle();
  }

  function renderToggle() {
    var t = G.container && G.container.querySelector('.tg-toggle');
    if (!t) return;
    var n = G.channels ? favChannels().length : null;
    t.innerHTML = ['favorites', 'all'].map(function (v) {
      var label = v === 'favorites' ? 'Favorites' : 'All';
      var count = '';
      if (G.channels) count = ' <span class="tg-count">' + (v === 'favorites' ? n : G.channels.length).toLocaleString() + '</span>';
      return '<button type="button" role="tab" data-view="' + v + '" aria-selected="' + (G.view === v) + '"' +
        ' class="tg-seg' + (G.view === v ? ' on' : '') + '">' + label + count + '</button>';
    }).join('');
  }

  function renderDays() {
    var el = G.container.querySelector('.tg-days');
    if (!el) return;
    // One day of data: no strip, nothing to choose.
    if (G.days.length < 2) { el.innerHTML = ''; el.style.display = 'none'; return; }
    el.style.display = '';
    el.innerHTML = G.days.map(function (d, i) {
      return '<button type="button" class="tg-day' + (i === G.dayIndex ? ' on' : '') + '" data-day="' + i + '">' +
        esc(dayLabel(d.start, i)) + '</button>';
    }).join('');
  }

  // ── Render: grid ──────────────────────────────────────────────────

  // Pixels from the start of the day shown (or of `day`).
  function x(t, day) {
    day = day || G.days[G.dayIndex];
    return (t - day.start) / MIN * (G.m || metrics()).ppm;
  }

  function renderGrid(scrollToNow) {
    G.m = metrics();
    G.mounted = {};
    var body = G.container.querySelector('.tg-body');
    if (!body) return;
    var m = G.m;
    var old = body.querySelector('.tg-scroller');
    var keepLeft = old ? old.scrollLeft : 0;

    // Still loading (Favorites / All clicked before the list came): the
    // load draws the grid when it arrives.
    if (!G.channels) return;
    if (G.loadError) {
      body.innerHTML = '<div class="tg-empty">The channels didn\'t load. <button type="button" class="tg-btn" data-action="retry">Try again</button></div>';
      return;
    }
    if (!G.channels.length) {
      body.innerHTML = '<div class="tg-empty">No channels yet.</div>';
      return;
    }
    if (!G.rows.length) {
      if (G.query) {
        body.innerHTML = '<div class="tg-empty">No channel matches “' + esc(G.query) + '”.</div>';
      } else {
        body.innerHTML = '<div class="tg-empty">' +
          '<div>No favorite channels yet. In All, tap the ☆ next to a channel to add it here.</div>' +
          '<button type="button" class="tg-btn" data-view="all">Show all channels</button></div>';
      }
      return;
    }

    var day = G.days[G.dayIndex];
    var width = Math.round(x(day.end));
    var height = G.rows.length * m.rowH;

    var head = '';
    for (var t = day.start; t < day.end; t += 30 * MIN) {
      head += '<div class="tg-tick" style="left:' + Math.round(x(t)) + 'px">' + esc(fmtTime(t)) + '</div>';
    }

    body.innerHTML =
      '<div class="tg-scroller' + (m.phone ? ' phone' : '') + '" style="--row:' + m.rowH + 'px;--chan:' + m.chanW + 'px;--head:' + m.headH + 'px">' +
        '<div class="tg-canvas" style="width:' + (m.chanW + width) + 'px;height:' + (m.headH + height) + 'px">' +
          '<div class="tg-head" style="width:' + (m.chanW + width) + 'px">' +
            '<div class="tg-corner"></div>' +
            '<div class="tg-ticks" style="width:' + width + 'px">' + head + '<div class="tg-nowpill"></div></div>' +
          '</div>' +
          '<div class="tg-rows"></div>' +
          '<div class="tg-now"></div>' +
        '</div>' +
      '</div>';

    var sc = body.querySelector('.tg-scroller');
    sc.addEventListener('scroll', onScroll, { passive: true });
    placeNow();
    if (scrollToNow) {
      var now = Date.now();
      sc.scrollLeft = (now >= day.start && now < day.end) ? Math.max(0, x(now) - 30 * m.ppm) : 0;
      sc.scrollTop = 0;
    } else {
      sc.scrollLeft = keepLeft;
    }
    drawRows();
  }

  function placeNow() {
    var line = G.container.querySelector('.tg-now');
    var pill = G.container.querySelector('.tg-nowpill');
    if (!line) return;
    var day = G.days[G.dayIndex], now = Date.now();
    if (!day || now < day.start || now >= day.end) {
      line.style.display = 'none';
      if (pill) pill.style.display = 'none';
      return;
    }
    var left = G.m.chanW + x(now);
    line.style.display = '';
    line.style.left = left + 'px';
    if (pill) {
      pill.style.display = '';
      pill.style.left = x(now) + 'px';
      pill.textContent = fmtTime(now);
      // The half-hour labels the pill would cover step aside.
      var ticks = G.container.querySelectorAll('.tg-tick');
      for (var i = 0; i < ticks.length; i++) {
        var tx = parseFloat(ticks[i].style.left);
        ticks[i].style.visibility = (tx > x(now) - 80 && tx < x(now) + 40) ? 'hidden' : '';
      }
    }
  }

  function onScroll() {
    if (G.scrollRaf) return;
    G.scrollRaf = requestAnimationFrame(function () { G.scrollRaf = 0; drawRows(); });
  }

  // Mount the rows in view (plus a few), drop the rest; fetch programmes ahead.
  function drawRows() {
    var sc = G.container && G.container.querySelector('.tg-scroller');
    var layer = sc && sc.querySelector('.tg-rows');
    if (!layer) return;
    var m = G.m;
    var top = Math.max(0, sc.scrollTop - m.headH);
    var first = Math.max(0, Math.floor(top / m.rowH) - OVERSCAN);
    var last = Math.min(G.rows.length - 1, Math.ceil((top + sc.clientHeight) / m.rowH) + OVERSCAN);

    var keep = {};
    for (var i = first; i <= last; i++) {
      var c = G.rows[i];
      keep[c.id] = true;
      var el = G.mounted[c.id];
      if (!el) {
        el = document.createElement('div');
        el.className = 'tg-row';
        el.dataset.channelId = c.id;
        el.innerHTML = chanCell(c) + '<div class="tg-progs"></div>';
        G.mounted[c.id] = el;
        fillProgs(el, c.id);
        layer.appendChild(el);
      }
      el.style.top = (m.headH + i * m.rowH) + 'px';
    }
    Object.keys(G.mounted).forEach(function (id) {
      if (!keep[id]) { G.mounted[id].remove(); delete G.mounted[id]; }
    });

    var want = [];
    var lo = Math.max(0, first - PREFETCH), hi = Math.min(G.rows.length - 1, last + PREFETCH);
    for (var j = lo; j <= hi; j++) want.push(G.rows[j].id);
    fetchProgs(want);
  }

  function chanCell(c) {
    var logo = c.logo
      ? '<img class="tg-logo" loading="lazy" alt="" src="' + escAttr(api().getUrl('Items/' + c.id + '/Images/Primary', { maxHeight: 96, tag: c.logo, quality: 90 })) + '">'
      : '<div class="tg-logo tg-logo-text">' + esc(c.name.slice(0, 3)) + '</div>';
    return '<div class="tg-chan" data-action="play" title="' + escAttr(c.name) + '">' +
      logo +
      '<div class="tg-chan-text">' +
        '<div class="tg-chan-name">' + esc(c.name) + '</div>' +
        (c.number ? '<div class="tg-chan-num">' + esc(c.number) + '</div>' : '') +
      '</div>' +
      '<button type="button" class="tg-star' + (c.fav ? ' on' : '') + '" data-action="star" aria-pressed="' + c.fav + '"' +
        ' title="' + (c.fav ? 'Remove from Favorites' : 'Add to Favorites') + '">' + (c.fav ? STAR_ON : STAR_OFF) + '</button>' +
    '</div>';
  }

  function fillProgs(rowEl, channelId) {
    var day = G.days[G.dayIndex];
    var list = G.progs[day.start] && G.progs[day.start][channelId];
    rowEl.querySelector('.tg-progs').innerHTML = progsHtml(list, day, Date.now());
  }

  // One channel's programmes for `day` (undefined = still loading).
  function progsHtml(list, day, now) {
    if (!list) return '<div class="tg-prog tg-wait" style="left:0;width:100%"></div>';
    if (!list.length) {
      return '<div class="tg-prog tg-none" style="left:0;width:' + Math.round(x(day.end, day)) + 'px"><span class="tg-sticky">No guide information</span></div>';
    }
    var html = '';
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      var s = Math.max(p.s, day.start), e = Math.min(p.e, day.end);
      if (e <= s) continue;
      var left = Math.round(x(s, day)), w = Math.round(x(e, day)) - left;
      var cls = 'tg-prog' + (p.e <= now ? ' past' : (p.s <= now ? ' live' : ''));
      var prog = p.e > p.s && p.s <= now && now < p.e ? (now - p.s) / (p.e - p.s) * 100 : 0;
      html += '<div class="' + cls + '" data-action="prog" data-id="' + esc(p.id) + '" data-s="' + p.s + '" data-e="' + p.e + '"' +
        ' style="left:' + left + 'px;width:' + Math.max(0, w - 2) + 'px" title="' + escAttr(p.name + (p.ov ? '\n\n' + p.ov : '')) + '">' +
        '<span class="tg-sticky">' +
          '<span class="tg-prog-name">' + (p.s < day.start ? '‹ ' : '') + esc(p.name) + '</span>' +
          (w > 96 ? '<span class="tg-prog-time">' + esc(fmtRange(p.s, p.e)) + '</span>' : '') +
        '</span>' +
        (prog ? '<i class="tg-bar" style="width:' + prog.toFixed(1) + '%"></i>' : '') +
      '</div>';
    }
    return html;
  }

  function fetchProgs(ids) {
    var day = G.days[G.dayIndex];
    var cache = G.progs[day.start] = G.progs[day.start] || {};
    var need = ids.filter(function (id) { return !cache[id] && !G.pending[day.start + ':' + id]; });
    for (var i = 0; i < need.length; i += BATCH) {
      loadBatch(day, need.slice(i, i + BATCH));
    }
  }

  function loadBatch(day, ids) {
    ids.forEach(function (id) { G.pending[day.start + ':' + id] = true; });
    var done = function () { ids.forEach(function (id) { delete G.pending[day.start + ':' + id]; }); };
    apiPost('LiveTv/Programs', {
      UserId: G.userId,
      ChannelIds: ids,
      MinEndDate: new Date(day.start).toISOString(),
      MaxStartDate: new Date(day.end).toISOString(),
      SortBy: ['StartDate'],
      EnableImages: false,
      EnableUserData: false,
      EnableTotalRecordCount: false,
      Fields: ['Overview'],
    }).then(function (r) {
      done();
      var cache = G.progs[day.start] = G.progs[day.start] || {};
      ids.forEach(function (id) { cache[id] = []; });
      ((r && r.Items) || []).forEach(function (p) {
        (cache[p.ChannelId] = cache[p.ChannelId] || []).push({
          id: p.Id,
          name: p.EpisodeTitle && p.Name ? p.Name + ': ' + p.EpisodeTitle : (p.Name || ''),
          s: Date.parse(p.StartDate),
          e: Date.parse(p.EndDate),
          ov: p.Overview || '',
        });
      });
      if (!G.active || G.days[G.dayIndex] !== day) return;
      ids.forEach(function (id) { if (G.mounted[id]) fillProgs(G.mounted[id], id); });
    }).catch(done);
  }

  // Every 30 s: move the now line; at a programme change redraw what's in view.
  function onTick() {
    if (!G.active) return;
    placeNow();
    var today = startOfDay(Date.now());
    if (G.days.length && G.days[0].start !== today) { applyView(true); return; }
    Object.keys(G.mounted).forEach(function (id) { fillProgs(G.mounted[id], id); });
  }

  // ── Events ────────────────────────────────────────────────────────

  function onInput(e) {
    if (!e.target.matches('.tg-search input')) return;
    G.query = e.target.value || '';
    applyView(false);
  }

  function setView(v) {
    if (v === G.view) return;
    G.view = v;
    G.query = '';
    var input = G.container.querySelector('.tg-search input');
    if (input) input.value = '';
    saveView();
    applyView(true);
  }

  function onClick(e) {
    var t = e.target;
    var search = t.closest('.tg-search');
    if (search) {
      var top = G.container.querySelector('.tg-top');
      if (top && !top.classList.contains('searching')) {
        top.classList.add('searching');
        search.querySelector('input').focus();
      }
      return;
    }
    var viewBtn = t.closest('[data-view]');
    if (viewBtn) { e.preventDefault(); setView(viewBtn.dataset.view); return; }

    var dayBtn = t.closest('[data-day]');
    if (dayBtn) {
      e.preventDefault();
      G.dayIndex = parseInt(dayBtn.dataset.day, 10) || 0;
      renderDays();
      renderGrid(true);
      return;
    }

    if (t.closest('[data-action="retry"]')) {
      e.preventDefault();
      loadChannels().then(function () { if (G.active) applyView(true); });
      return;
    }

    var row = t.closest('.tg-row');
    if (!row) return;
    var id = row.dataset.channelId;

    if (t.closest('[data-action="star"]')) { e.preventDefault(); e.stopPropagation(); toggleFav(id); return; }

    var cell = t.closest('[data-action="prog"]');
    if (cell) {
      e.preventDefault();
      var now = Date.now();
      if (+cell.dataset.s <= now && now < +cell.dataset.e) playChannel(id);
      else location.hash = '#/details?id=' + cell.dataset.id + '&serverId=' + api().serverId();
      return;
    }
    if (t.closest('[data-action="play"]')) { e.preventDefault(); playChannel(id); }
  }

  function toggleFav(id) {
    var c = (G.channels || []).filter(function (x) { return x.id === id; })[0];
    if (!c) return;
    c.fav = !c.fav;
    G.maxEnd.favorites = undefined;
    var redraw = function () { refreshRows([id]); };
    redraw();
    var p = c.fav
      ? api().ajax({ type: 'POST', url: api().getUrl('UserFavoriteItems/' + id, { userId: G.userId }) })
      : api().ajax({ type: 'DELETE', url: api().getUrl('UserFavoriteItems/' + id, { userId: G.userId }) });
    Promise.resolve(p).catch(function () { c.fav = !c.fav; redraw(); });
  }

  // ── Live updates ──────────────────────────────────────────────────

  function normId(id) { return String(id || '').replace(/-/g, '').toLowerCase(); }

  // Jellyfin web's own websocket (ApiClient._webSocket; a new one after each
  // reconnect). We only listen; Jellyfin web keeps handling it as before.
  function watchSocket() {
    var ws = api() && api()._webSocket;
    if (!ws || ws === G.socket || typeof ws.addEventListener !== 'function') return;
    var reconnect = !!G.socket;
    G.socket = ws;
    ws.addEventListener('message', onSocketMessage);
    // Messages sent while it was down are lost: read the list again.
    if (reconnect && G.active) scheduleReload(0);
  }

  function socketOpen() {
    var ws = api() && api()._webSocket;
    return !!ws && ws === G.socket && ws.readyState === 1;
  }

  // Jellyfin sends a browser's messages to one socket only (the newest tab's),
  // so the tab that gets one passes it to the other tabs of this browser.
  var relay = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('tentacle-livetv') : null;
  if (relay) {
    relay.onmessage = function (e) { onMessage(e.data); };
    if (relay.unref) relay.unref();   // node (the unit tests)
  }

  function onSocketMessage(e) {
    if (e.target !== G.socket) return;
    var msg;
    try { msg = JSON.parse(e.data); } catch (x) { return; }
    if (!msg || (msg.MessageType !== 'UserDataChanged' && msg.MessageType !== 'LibraryChanged')) return;
    var copy = { MessageType: msg.MessageType, Data: msg.MessageType === 'UserDataChanged' ? msg.Data : null };
    if (relay) { try { relay.postMessage(copy); } catch (x) { } }
    onMessage(copy);
  }

  function onMessage(msg) {
    if (!G.active || !msg) return;
    if (msg.MessageType === 'UserDataChanged') onUserData(msg.Data);
    else if (msg.MessageType === 'LibraryChanged') scheduleReload(1500);
  }

  // Someone's favourites changed: apply the channels of ours that changed.
  function onUserData(data) {
    learnUser();
    if (!data || !G.channels || !G.userId || normId(data.UserId) !== normId(G.userId)) return;
    var byId = {};
    G.channels.forEach(function (c) { byId[normId(c.id)] = c; });
    var changed = [];
    (data.UserDataList || []).forEach(function (u) {
      var c = byId[normId(u.ItemId)];
      if (!c || typeof u.IsFavorite !== 'boolean' || c.fav === u.IsFavorite) return;
      c.fav = u.IsFavorite;
      changed.push(c.id);
    });
    if (!changed.length) return;
    G.maxEnd.favorites = undefined;
    refreshRows(changed);
  }

  // A first page load opens the guide before Jellyfin web has signed in, so
  // the user id may still be unknown at start().
  function learnUser() {
    if (!G.userId && api()) G.userId = api().getCurrentUserId() || '';
  }

  function onLiveTick() {
    if (!G.active) return;
    learnUser();
    watchSocket();
    // No websocket (blocked, or reconnecting): read the list every 10 s.
    if (!socketOpen() && Date.now() - G.reload.at >= POLL_MS) scheduleReload(0);
  }

  function scheduleReload(delay) {
    if (G.reload.timer) clearTimeout(G.reload.timer);
    G.reload.timer = setTimeout(function () { G.reload.timer = null; reloadChannels(); }, delay);
  }

  // Read the channel list again (one request, ~60 ms for 1,700 channels) and
  // redraw only what differs: channels that came or went, a new name, logo,
  // number or favourite.
  function reloadChannels() {
    if (!G.active || !G.channels || G.loadError) return;
    if (G.reload.busy) { G.reload.again = true; return; }
    G.reload.busy = true;
    G.reload.at = Date.now();
    var uid = G.userId;
    fetchChannels().then(function (list) {
      G.reload.busy = false;
      if (G.reload.again) { G.reload.again = false; scheduleReload(0); }
      if (!G.active || uid !== G.userId || !G.channels) return;
      var d = diffChannels(G.channels, list);
      if (!d.changed.length && !d.moved) return;
      G.channels = list;
      if (d.moved) G.maxEnd = {};
      else if (d.favs) G.maxEnd.favorites = undefined;
      refreshRows(d.changed);
    }).catch(function () {
      G.reload.busy = false;
      if (G.reload.again) { G.reload.again = false; scheduleReload(1000); }
    });
  }

  // What differs between two channel lists: `moved` when channels came, went
  // or changed order; `changed` the ids whose cell looks different.
  function diffChannels(old, list) {
    var was = {};
    old.forEach(function (c) { was[c.id] = c; });
    var moved = old.length !== list.length, favs = false, changed = [];
    list.forEach(function (c, i) {
      var o = was[c.id];
      if (!o) { moved = true; return; }
      if (old[i].id !== c.id) moved = true;
      if (o.fav !== c.fav) favs = true;
      if (o.fav !== c.fav || o.name !== c.name || o.number !== c.number || o.logo !== c.logo) changed.push(c.id);
    });
    return { moved: moved, favs: favs, changed: changed };
  }

  // The channels changed (a favourite, the lineup) but not the view: redraw
  // in place. Rows that came or went move the others; the cells of `ids` are
  // drawn again; the scroll position and the day stay.
  function refreshRows(ids) {
    (ids || []).forEach(function (id) {
      var c = (G.channels || []).filter(function (x) { return x.id === id; })[0];
      var el = G.mounted[id];
      if (c && el) el.querySelector('.tg-chan').outerHTML = chanCell(c);
    });
    var before = G.rows.length;
    computeRows();
    renderToggle();
    var sc = G.container && G.container.querySelector('.tg-scroller');
    loadMaxEnd(G.view).then(function (maxEnd) {
      if (!G.active) return;
      var days = computeDays(maxEnd);
      var sameDays = days.length === G.days.length && days.every(function (d, i) {
        return d.start === G.days[i].start && d.end === G.days[i].end;
      });
      sc = G.container.querySelector('.tg-scroller');
      if (!sameDays || !sc || !G.rows.length || !before) {
        var top = sc ? sc.scrollTop : 0, left = sc ? sc.scrollLeft : 0;
        var start = G.days[G.dayIndex] ? G.days[G.dayIndex].start : 0;
        G.days = days;
        G.dayIndex = 0;
        for (var i = 0; i < days.length; i++) if (days[i].start === start) G.dayIndex = i;
        renderDays();
        renderGrid(false);
        var now = G.container.querySelector('.tg-scroller');
        if (now) { now.scrollTop = top; now.scrollLeft = left; drawRows(); }
        return;
      }
      var canvas = sc.querySelector('.tg-canvas');
      if (canvas) canvas.style.height = (G.m.headH + G.rows.length * G.m.rowH) + 'px';
      drawRows();
    });
  }

  // Listen in every tab, also off the guide: this tab may be the one
  // Jellyfin talks to while another tab shows the guide.
  var watcher = setInterval(watchSocket, 2000);
  if (watcher && watcher.unref) watcher.unref();   // node (the unit tests)

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && G.active) scheduleReload(0);
  });

  // ── Playback ──────────────────────────────────────────────────────

  // Play in this tab. Jellyfin Web 10.11 keeps its playbackManager inside its
  // bundle (no global), so the guide used to ask the server to play
  // (sendPlayCommand). The server sends that to the browser's newest
  // websocket: with two Jellyfin tabs open it started the channel in the
  // other tab, a hidden player (sound with no picture) that held the
  // provider connection while the clicked tab played too, and a Stop from
  // the server reached the wrong tab. Instead we hand Jellyfin Web's own
  // message handler (serverNotifications, bound to the ApiClient through
  // the global Events) the same Play message, locally: its playbackManager
  // plays here, stopping whatever this tab played first.
  function localPlayer() {
    var a = window.ApiClient, ev = window.Events;
    var handlers = a && a._callbacks && a._callbacks.message;
    if (!ev || typeof ev.trigger !== 'function' || !handlers || !handlers.length) return null;
    return {
      play: function (opts) {
        opts = opts || {};
        var data = { ItemIds: opts.ids || [], PlayCommand: 'PlayNow' };
        if (opts.startPositionTicks) data.StartPositionTicks = opts.startPositionTicks;
        if (opts.mediaSourceId) data.MediaSourceId = opts.mediaSourceId;
        if (opts.audioStreamIndex != null) data.AudioStreamIndex = opts.audioStreamIndex;
        if (opts.subtitleStreamIndex != null) data.SubtitleStreamIndex = opts.subtitleStreamIndex;
        if (opts.startIndex != null) data.StartIndex = opts.startIndex;
        try {
          leftPlayerAt = 0;
          ev.trigger(a, 'message', [{ MessageType: 'Play', Data: data }]);
          return Promise.resolve();
        } catch (err) {
          return Promise.reject(err);
        }
      }
    };
  }

  // The other Tentacle pages (details, search) look for a global
  // playbackManager and otherwise go through the server too: give them this.
  if (typeof window.playbackManager === 'undefined') {
    window.playbackManager = {
      play: function (opts) {
        var p = localPlayer();
        return p ? p.play(opts) : Promise.reject(new Error('Jellyfin Web is not ready to play'));
      }
    };
  }

  function playChannel(channelId) {
    var p = localPlayer();
    if (p) {
      p.play({ ids: [channelId] }).catch(function (err) {
        console.error('[Tentacle LiveTV] play failed, opening the channel page', err);
        navigateToPlay(channelId);
      });
      return;
    }
    // Not signed in yet / not Jellyfin Web: the channel's page (play from
    // there). Never the server's play command, which may play elsewhere.
    navigateToPlay(channelId);
  }

  function navigateToPlay(channelId) {
    location.hash = '#/details?id=' + channelId;
  }

  // ── A live player left running after the player was closed ────────
  //
  // Jellyfin Web retries a live channel that fails (copy → video
  // transcode → audio transcode). A retry still on its way when the viewer
  // goes Back (or the error the closing player throws, which starts one)
  // plays after the player page has gone: sound with no picture in this
  // tab, holding the provider connection. Jellyfin Web only stops a video
  // when its player page closes, so live TV that keeps playing off that
  // page after it closed (not picture-in-picture) is stopped here, the way
  // a remote Stop does, and taken off the page if it keeps going. A play
  // that is starting (it plays a moment before its page opens) is left
  // alone: only a video whose time runs on for STRAY_MS off the page counts.
  var STRAY_MS = 1500;
  var leftPlayerAt = 0, strayTimer = null, strayStopped = 0;

  function onPlayerPage() { return /^#\/video\b/.test(location.hash); }

  function liveHlsLoadedRecently() {
    if (!window.performance || !performance.getEntriesByType) return false;
    var list = performance.getEntriesByType('resource');
    for (var i = list.length - 1; i >= 0; i--) {
      var n = list[i].name || '';
      if (/\/(master|main|live)\.m3u8/i.test(n)) {
        return /\/live\.m3u8|[?&]LiveStreamId=/i.test(n) && performance.now() - list[i].startTime < 120000;
      }
    }
    return false;
  }

  function strayVideos(now) {
    var out = [];
    if (!leftPlayerAt || onPlayerPage()) return out;
    document.querySelectorAll('video').forEach(function (v) {
      if (v.paused || v.ended || v === document.pictureInPictureElement) {
        v.__tgOff = null;
        return;
      }
      if (!v.__tgOff) v.__tgOff = { at: now, t: v.currentTime };
      if (now - v.__tgOff.at >= STRAY_MS && v.currentTime - v.__tgOff.t >= 0.5) out.push(v);
    });
    return out.length && liveHlsLoadedRecently() ? out : [];
  }

  function checkStrayPlayer() {
    strayTimer = null;
    var stray = strayVideos(Date.now());
    if (stray.length) {
      if (!strayStopped) {
        strayStopped = Date.now();
        console.warn('[Tentacle LiveTV] live TV still playing after the player closed: stopping it');
        var a = window.ApiClient, ev = window.Events;
        try {
          if (ev && a) ev.trigger(a, 'message', [{ MessageType: 'Playstate', Data: { Command: 'Stop' } }]);
        } catch (e) { /* taken off below */ }
      } else if (Date.now() - strayStopped >= 500) {
        stray.forEach(function (v) {
          try { v.pause(); v.removeAttribute('src'); v.load(); } catch (e) { /* gone */ }
        });
        strayStopped = 0;
      }
    } else {
      strayStopped = 0;
    }
    // watched for two minutes after the page closed (a 'playing' starts it again)
    if (leftPlayerAt && !onPlayerPage() && Date.now() - leftPlayerAt < 120000) {
      strayTimer = setTimeout(checkStrayPlayer, 250);
    }
  }

  // Jellyfin Web opens its player page with pushState (no hashchange):
  // watch the route instead
  var wasOnPlayerPage = onPlayerPage();
  function watchPlayerPage(e) {
    var on = onPlayerPage();
    if (on) {
      leftPlayerAt = 0;
    } else if (wasOnPlayerPage) {
      leftPlayerAt = Date.now();
    }
    wasOnPlayerPage = on;
    if (leftPlayerAt && !strayTimer && (e && e.type === 'playing' || Date.now() - leftPlayerAt < 120000)) {
      strayTimer = setTimeout(checkStrayPlayer, 250);
    }
  }
  window.addEventListener('popstate', watchPlayerPage);
  window.addEventListener('hashchange', watchPlayerPage);
  setInterval(watchPlayerPage, 500);
  // media events don't bubble: listen in the capture phase
  document.addEventListener('playing', watchPlayerPage, true);

  // ── Navigation listeners ───────────────────────────────────────────

  function onRouteChange() {
    if (isGuidePage()) {
      // Small delay to let Jellyfin finish its view transition
      setTimeout(activate, 50);
    } else {
      deactivate();
    }
  }

  document.addEventListener('viewshow', onRouteChange);
  window.addEventListener('hashchange', onRouteChange);
  window.addEventListener('popstate', onRouteChange);

  // Public API — lets other Tentacle overlays (Discover/Activity) dismiss this
  // one. The Live TV overlay sits at z-index 900, so anything opened without
  // dismissing it first would be invisible underneath.
  window.TentacleLiveTV = {
    isActive: function () { return G.active; },
    hide: function () { deactivate(); },
    // Shared with the Favorites page (favorited channel cards play on click)
    playChannel: playChannel,
    // For the unit tests (tests/test_livetv_guide.py)
    internals: { diffChannels: diffChannels, onUserData: onUserData, G: G, computeDays: computeDays, isGuidePage: isGuidePage, progsHtml: progsHtml, chanCell: chanCell, fmtRange: fmtRange },
  };

  // Initial check
  if (isGuidePage()) {
    setTimeout(activate, 100);
  }
})();
