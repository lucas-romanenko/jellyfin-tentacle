// Tentacle Live TV — the guide, replacing Jellyfin's Live TV landing and guide tabs
//
// Architecture (same pattern as tentacle-search.js):
//   - #tentacleLiveTvContainer mounted on document.body
//   - Body class `tentacle-livetv-active` hides native content
//   - Active on #/livetv with no tab, tab 0 (Programs) or tab 1 (Guide);
//     Channels and Series stay native; Recordings and Schedule are Tentacle's pages (tentacle-recordings.js).
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
//   - A dot marks what will record (Jellyfin's timers, Tentacle's teams and
//     rules; hollow: a team or rule in preview would), within seconds of a
//     change here, in Tentacle or on another device.
//   - Recordings and Schedule open Tentacle's pages (tentacle-recordings.js).
//   - Live: a favourite changed anywhere (Tentacle's My Channels, this guide,
//     another device) shows within about a second (Jellyfin's UserDataChanged
//     on its websocket); a lineup change (LibraryChanged, Jellyfin's or the one
//     Tentacle sends after setting Live TV up) re-reads the channel list. Only
//     the rows that changed are redrawn; the scroll position stays.
//   - Keeps your place: leaving the guide (a channel, a programme's page)
//     and coming back shows it as it was: the view, the day, the rows and
//     the time you had scrolled to (the current time again if you were at
//     now), with the channel you watched highlighted and in view. Channel
//     up/down in the player counts: the guide comes back on the channel
//     you ended on (Jellyfin's session for this browser says which).
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
    place: null,            // where the guide was when it was left (see keepPlace)
    lastId: '',             // the channel last watched from the guide (highlighted)
    marks: {},              // markKey(channel, start) -> 'rec' | 'wait' | 'pv' (what will record)
    marksAt: 0,
    overlay: false,         // the guide over the player (its Guide button)
    manage: null,           // may this person set recordings (Jellyfin's EnableLiveTvManagement)
    cursor: null,           // the keyboard's place: {id: channel, t: a time in the programme}
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
    // A first page load opens the guide before Jellyfin web has signed in:
    // an empty UserId is refused (400), none is the signed-in caller.
    if (body && 'UserId' in body) { learnUser(); body.UserId = G.userId || undefined; }
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

  // ── A small menu (right-click, a long press on touch, the Menu key) ──
  // Tentacle's one menu on the web, like the TV app's: small, dark, rounded,
  // purple focus. Next to what was pressed on a computer, a sheet at the
  // bottom on a phone. Arrow keys move, Enter picks, Escape or a click
  // outside closes. Also used by the Recordings page and Home
  // (window.TentacleMenu).

  var MENU = { el: null, back: null };

  function phoneLayout() { return window.innerWidth < 700; }

  // opts: { title, body (html), items: [{ label, hint, danger, run }], x, y, anchor }
  function openMenu(opts) {
    closeMenu();
    var items = (opts.items || []).filter(Boolean);
    var scrim = document.createElement('div');
    scrim.className = 'tmenu-scrim' + (phoneLayout() ? ' sheet' : '');
    scrim.innerHTML = '<div class="tmenu" role="menu" aria-label="' + escAttr(opts.title || 'Menu') + '">' +
      (opts.title ? '<div class="tmenu-title">' + esc(opts.title) + '</div>' : '') +
      (opts.body || '') +
      items.map(function (it, i) {
        return '<button type="button" role="menuitem" class="tmenu-item' + (it.danger ? ' danger' : '') + '" data-i="' + i + '">' +
          '<span>' + esc(it.label) + '</span>' + (it.hint ? '<small>' + esc(it.hint) + '</small>' : '') + '</button>';
      }).join('') +
      '</div>';
    document.body.appendChild(scrim);
    var box = scrim.querySelector('.tmenu');
    MENU.el = scrim;
    MENU.back = document.activeElement;
    if (!phoneLayout()) {
      var r = opts.anchor && opts.anchor.getBoundingClientRect ? opts.anchor.getBoundingClientRect() : null;
      var x = opts.x != null ? opts.x : (r ? r.left : window.innerWidth / 2);
      var y = opts.y != null ? opts.y : (r ? r.bottom + 4 : window.innerHeight / 3);
      var w = box.offsetWidth, h = box.offsetHeight;
      box.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px';
      box.style.top = Math.max(8, Math.min(y, window.innerHeight - h - 8)) + 'px';
    }
    scrim.addEventListener('click', function (e) {
      var b = e.target.closest('.tmenu-item');
      if (b) {
        var it = items[+b.dataset.i];
        closeMenu();
        if (it && it.run) it.run();
        return;
      }
      if (!e.target.closest('.tmenu')) closeMenu();
    });
    scrim.addEventListener('contextmenu', function (e) { e.preventDefault(); if (!e.target.closest('.tmenu')) closeMenu(); });
    // one highlight: the pointer moves the keyboard's focus along
    scrim.addEventListener('mousemove', function (e) {
      var b = e.target.closest('.tmenu-item');
      if (b && document.activeElement !== b && e.movementX + e.movementY !== 0) b.focus({ preventScroll: true });
    });
    var first = scrim.querySelector('.tmenu-item');
    if (first) first.focus({ preventScroll: true });
    return scrim;
  }

  function closeMenu() {
    if (!MENU.el) return false;
    MENU.el.remove();
    MENU.el = null;
    var back = MENU.back;
    MENU.back = null;
    if (back && back.focus && document.contains(back)) { try { back.focus({ preventScroll: true }); } catch (e) { } }
    return true;
  }

  // "Remove PBS Kids from your Favorites?" Remove / Cancel
  function confirmMenu(opts) {
    return openMenu({
      title: opts.message, anchor: opts.anchor, x: opts.x, y: opts.y,
      items: [
        { label: opts.confirm, danger: !!opts.danger, run: opts.run },
        { label: opts.cancel || 'Cancel' },
      ],
    });
  }

  // Keys inside the menu (capture: before Jellyfin's own keys, e.g. the player's)
  document.addEventListener('keydown', function (e) {
    if (!MENU.el) return;
    var list = Array.prototype.slice.call(MENU.el.querySelectorAll('.tmenu-item'));
    var i = list.indexOf(document.activeElement);
    if (e.key === 'Escape' || e.key === 'Backspace' || e.key === 'GoBack' || e.key === 'BrowserBack') {
      closeMenu();
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || (e.key === 'Tab' && list.length)) {
      var d = e.key === 'ArrowUp' || (e.key === 'Tab' && e.shiftKey) ? -1 : 1;
      var n = list[(i + d + list.length) % list.length];
      if (n) n.focus();
    } else if (e.key === 'Enter' || e.key === ' ') {
      if (i >= 0) list[i].click();
    } else {
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);
  window.addEventListener('hashchange', closeMenu);
  window.addEventListener('popstate', closeMenu);

  // A short line at the bottom ("Program set to record").
  var toastTimer = null;
  function toast(text) {
    var t = document.querySelector('.tmenu-toast');
    if (!t) {
      t = document.createElement('div');
      t.className = 'tmenu-toast';
      t.setAttribute('role', 'status');
      document.body.appendChild(t);
    }
    t.textContent = text;
    t.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('on'); }, 3200);
  }

  // Right-click, and a long press on touch (iOS sends no contextmenu), on
  // what matches `selector` inside `root`: handler(el, x, y). The click that
  // ends a long press is swallowed.
  function onMenuGesture(root, selector, handler) {
    var timer = null, start = null, fired = 0;
    // capture: a row that handles its own touches (Home's) doesn't hide them
    root.addEventListener('contextmenu', function (e) {
      var el = e.target.closest && e.target.closest(selector);
      if (!el || !root.contains(el)) return;
      e.preventDefault();
      if (Date.now() - fired < 800) return;   // the long press already opened it
      fired = Date.now();
      handler(el, e.clientX, e.clientY);
    }, true);
    root.addEventListener('touchstart', function (e) {
      var el = e.target.closest && e.target.closest(selector);
      if (!el || e.touches.length !== 1) return;
      var t = e.touches[0];
      start = { x: t.clientX, y: t.clientY };
      clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        if (Date.now() - fired < 800) return;
        fired = Date.now();
        if (navigator.vibrate) { try { navigator.vibrate(10); } catch (x) { } }
        handler(el, start.x, start.y);
      }, 500);
    }, { passive: true, capture: true });
    var cancel = function (e) {
      // the finger lifted after a long press: no click (it would land on the menu's backdrop)
      if (e.type === 'touchend' && Date.now() - fired < 1500 && e.cancelable) e.preventDefault();
      if (!timer) return;
      if (e.type === 'touchmove') {
        var t = e.touches[0];
        if (t && Math.abs(t.clientX - start.x) < 10 && Math.abs(t.clientY - start.y) < 10) return;
      }
      clearTimeout(timer);
      timer = null;
    };
    root.addEventListener('touchmove', cancel, { passive: true, capture: true });
    root.addEventListener('touchend', cancel, true);
    root.addEventListener('touchcancel', cancel, true);
    document.addEventListener('click', function (e) {
      if (Date.now() - fired < 800 && !(e.target.closest && e.target.closest('.tmenu'))) { e.preventDefault(); e.stopImmediatePropagation(); }
    }, true);
  }

  window.TentacleMenu = { open: openMenu, close: closeMenu, confirm: confirmMenu, toast: toast, onGesture: onMenuGesture };

  // ── A live picture in a <video>: fragmented MP4 read as it comes ────
  // Tentacle sends the multi-view picture and the guide's preview as
  // fragmented MP4 (a browser plays neither MPEG-TS nor switchable sound
  // tracks). Read with fetch and fed to Media Source Extensions (Managed
  // Media Source on iPhone), kept close to live: what is more than a few
  // seconds behind is skipped, what was played is let go.

  function mediaSourceClass() {
    return window.ManagedMediaSource || window.MediaSource || null;
  }

  function LivePicture(video, opts) {
    this.video = video;
    this.opts = opts || {};
    this.abort = null;
    this.ms = null;
    this.sb = null;
    this.queue = [];
    this.stopped = false;
    this.timer = null;
    this.url = '';
  }

  LivePicture.supported = function () { return !!mediaSourceClass(); };

  // Resolves once the picture plays, rejects when it doesn't come.
  LivePicture.prototype.start = function (url, headers) {
    var self = this, MS = mediaSourceClass();
    this.url = url;
    if (!MS) return Promise.reject(new Error('This browser can\'t play live pictures'));
    var ms = this.ms = new MS();
    var v = this.video;
    if (window.ManagedMediaSource && MS === window.ManagedMediaSource) v.disableRemotePlayback = true;
    v.src = URL.createObjectURL(ms);
    var ctl = this.abort = typeof AbortController !== 'undefined' ? new AbortController() : null;
    return new Promise(function (resolve, reject) {
      var done = false;
      var fail = function (err) {
        if (done) { if (!self.stopped && self.opts.onEnd) self.opts.onEnd(err); return; }
        done = true;
        reject(err);
      };
      v.addEventListener('playing', function onPlaying() {
        v.removeEventListener('playing', onPlaying);
        if (!done) { done = true; resolve(); }
      });
      // the stream ended (the session closed, the server went away) and what came has played
      v.addEventListener('ended', function () { if (!self.stopped) fail(new Error('ended')); });
      v.addEventListener('error', function () { if (!self.stopped) fail(new Error('This picture can\u2019t be played')); });
      ms.addEventListener('sourceopen', function () {
        try { self.sb = ms.addSourceBuffer(self.opts.mime || 'video/mp4; codecs="avc1.640028"'); }
        catch (e) { fail(e); return; }
        self.sb.addEventListener('updateend', function () { self.pump(); });
        fetch(url, { headers: headers || {}, signal: ctl ? ctl.signal : undefined, cache: 'no-store' }).then(function (r) {
          if (!r.ok) {
            return r.json().catch(function () { return {}; }).then(function (j) {
              throw new Error((j && (j.detail || j.error)) || ('HTTP ' + r.status));
            });
          }
          var reader = r.body.getReader();
          var read = function () {
            return reader.read().then(function (x) {
              if (self.stopped) return;
              if (x.done) {
                // what came still plays; then the media source ends ('ended' above)
                self.finished = true;
                if (!self.sawData) { fail(new Error('ended')); return; }
                self.pump();
                return;
              }
              self.sawData = true;
              self.queue.push(x.value);
              self.pump();
              return read();
            });
          };
          return read();
        }).catch(function (e) { if (!self.stopped) fail(e); });
      }, { once: true });
      var p = v.play();
      if (p && p.catch) p.catch(function () { });
      self.timer = setInterval(function () { self.keepLive(); }, 1000);
    });
  };

  LivePicture.prototype.pump = function () {
    var sb = this.sb;
    if (!sb || sb.updating || this.stopped) return;
    if (!this.queue.length) {
      if (this.finished && this.ms && this.ms.readyState === 'open') { try { this.ms.endOfStream(); } catch (e) { } }
      return;
    }
    var n = 0, i;
    for (i = 0; i < this.queue.length; i++) n += this.queue[i].length;
    var buf = new Uint8Array(n), at = 0;
    for (i = 0; i < this.queue.length; i++) { buf.set(this.queue[i], at); at += this.queue[i].length; }
    this.queue = [];
    try { sb.appendBuffer(buf); } catch (e) {
      // full: let go of what was played and try again
      if (e && e.name === 'QuotaExceededError') { this.queue.unshift(buf); this.trim(true); }
    }
  };

  // Near the live edge: start there, and jump forward when 4 s behind
  // (a pause, a slow tab). Played seconds are let go.
  LivePicture.prototype.keepLive = function () {
    var v = this.video, b = v.buffered;
    if (this.stopped || !b || !b.length) return;
    var end = b.end(b.length - 1);
    if (v.currentTime < b.start(b.length - 1) || end - v.currentTime > 4) {
      try { v.currentTime = Math.max(b.start(b.length - 1), end - 1); } catch (e) { }
    }
    if (v.paused && !this.opts.paused) { var p = v.play(); if (p && p.catch) p.catch(function () { }); }
    this.trim(false);
  };

  LivePicture.prototype.trim = function (hard) {
    var sb = this.sb, v = this.video;
    if (!sb || sb.updating || !v.buffered.length) return;
    var start = v.buffered.start(0), keep = v.currentTime - (hard ? 2 : 20);
    if (keep - start > (hard ? 0.5 : 10)) { try { sb.remove(start, keep); } catch (e) { } }
  };

  LivePicture.prototype.stop = function () {
    this.stopped = true;
    clearInterval(this.timer);
    if (this.abort) { try { this.abort.abort(); } catch (e) { } }
    var v = this.video;
    try { v.pause(); } catch (e) { }
    try { v.removeAttribute('src'); v.load(); } catch (e) { }
    if (this.ms && this.ms.readyState === 'open') { try { this.ms.endOfStream(); } catch (e) { } }
    this.ms = this.sb = null;
    this.queue = [];
  };

  function authHeaders() {
    var a = api();
    var t = a && typeof a.accessToken === 'function' ? a.accessToken() : '';
    return t ? { 'X-Emby-Token': t } : {};
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

  // `force`: the guide over the player (not a route of its own).
  function activate(force) {
    if (!force && window.TentacleStandAside && window.TentacleStandAside()) return; // TV layout — native page renders
    if (G.active) return;
    G.active = true;
    document.body.classList.add('tentacle-livetv-active');
    ensureContainer();
    G.container.classList.toggle('tg-overlay', !!G.overlay);
    G.container.style.display = 'flex';
    start();
    // back from a multi-view channel played full screen: the grid again
    if (MV.session && MV.fullScreen && !G.overlay) openGrid();
  }

  function deactivate() {
    if (!G.active) return;
    keepPlace();
    G.active = false;
    document.body.classList.remove('tentacle-livetv-active');
    if (G.container) G.container.style.display = 'none';
    if (G.tick) { clearInterval(G.tick); G.tick = null; }
    if (G.live) { clearInterval(G.live); G.live = null; }
    if (G.reload.timer) { clearTimeout(G.reload.timer); G.reload.timer = null; }
    if (PV.poll) { clearInterval(PV.poll); PV.poll = null; }
    stopPreview();
    closeMenu();
    if (MV.picking) endPicking();
    // left the guide (not for a multi-view channel full screen): multi-view ends
    if (MV.session && !MV.fullScreen) leaveMultiView();
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
    el.addEventListener('mouseover', onHover);
    onMenuGesture(el, '.tg-prog[data-id], .tg-chan', function (t, x, y) {
      var row = t.closest('.tg-row');
      if (!row) return;
      if (t.classList.contains('tg-chan')) channelMenu(row.dataset.channelId, { x: x, y: y, el: t });
      else programmeMenu(row.dataset.channelId, t.dataset.id, { x: x, y: y, el: t });
    });
    window.addEventListener('resize', function () {
      if (!G.active) return;
      placeBox();
      var m = metrics();
      if (G.m && m.phone === G.m.phone) return;
      renderGrid(true);
    });
  }

  // What's wrong with Live TV's provider, in Tentacle's plain words
  // ("IPTV Bears is down right now (their server isn't answering) since
  // 6:30 PM. ...", TentacleDiscover/FamilyStatus), shown above the guide
  // while it lasts: read again every minute and when Tentacle says
  // "status", so it goes away by itself.
  function loadNotice() {
    G.noticeAt = Date.now();
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
    loadMarks();
    var place = G.place && G.place.uid === uid && G.channels && !G.loadError ? G.place : null;
    G.place = null;
    if (place) {
      // Back from a channel: the list we had draws at once, where it was,
      // then a re-read brings in what changed meanwhile (rows redrawn in place).
      G.view = place.view;
      applyView(place);
      scheduleReload(0);
    } else {
      loadSavedView();
      loadChannels().then(function () { if (G.active) applyView(true); });
    }
    if (G.tick) clearInterval(G.tick);
    G.tick = setInterval(onTick, 30000);
    G.reload.at = Date.now();
    watchSocket();
    if (G.live) clearInterval(G.live);
    G.live = setInterval(onLiveTick, 2000);
    if (PV.poll) clearInterval(PV.poll);
    PV.poll = null;
    showBox();
    if (!G.overlay && !G.m.phone) {
      pollPreview();
      PV.poll = setInterval(pollPreview, PREVIEW_POLL_MS);
    }
    if (G.overlay) setTimeout(placeBox, 0);
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
      return trimMaxEnd(view, p ? Date.parse(p.EndDate) : 0);
    }).then(function (end) {
      G.maxEnd[view] = end;
      return end;
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

  // `scrollToNow`: true = the current time, top row; false = keep the
  // scroll; a place (keepPlace) = back where the guide was left.
  function applyView(scrollToNow) {
    computeRows();
    renderToggle();
    loadMaxEnd(G.view).then(function (maxEnd) {
      if (!G.active) return;
      var place = scrollToNow && typeof scrollToNow === 'object' ? scrollToNow : null;
      var before = place ? place.day : (G.days.length && G.days[G.dayIndex] ? G.days[G.dayIndex].start : 0);
      G.days = computeDays(maxEnd);
      G.dayIndex = 0;
      var found = false;
      for (var i = 0; i < G.days.length; i++) if (G.days[i].start === before) { G.dayIndex = i; found = true; }
      renderDays();
      // the day it was on has gone (past midnight): today, now
      if (place && !found) place = null;
      renderGrid(place ? false : !!scrollToNow);
      if (place) restorePlace(place);
      if (G.overlay && G.lastId) { scrollToChannel(G.lastId); placeBox(); }
    });
  }

  // ── Render: shell (header, toggle, days) ──────────────────────────

  var ICON_REC = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><path d="M12 2a10 10 0 100 20 10 10 0 000-20zm0 18a8 8 0 110-16 8 8 0 010 16z"/></svg>';
  var ICON_SCHED = '<svg viewBox="0 0 24 24"><path d="M19 4h-1V2h-2v2H8V2H6v2H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2V6a2 2 0 00-2-2zm0 16H5V9h14v11zM7 11h5v5H7z"/></svg>';
  var ICON_SEARCH = '<svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0016 9.5 6.5 6.5 0 109.5 16a6.47 6.47 0 004.23-1.57l.27.28v.79l5 5L20.5 19l-5-5zm-6 0A4.5 4.5 0 1114 9.5 4.49 4.49 0 019.5 14z"/></svg>';
  var STAR_ON = '<svg viewBox="0 0 24 24"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>';
  var ICON_MV = '<svg viewBox="0 0 24 24"><path d="M3 3h8v8H3V3zm2 2v4h4V5H5zm8-2h8v8h-8V3zm2 2v4h4V5h-4zM3 13h8v8H3v-8zm2 2v4h4v-4H5zm8-2h8v8h-8v-8zm2 2v4h4v-4h-4z"/></svg>';
  var ICON_BACK = '<svg viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>';
  var STAR_OFF = '<svg viewBox="0 0 24 24"><path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/></svg>';

  function renderShell() {
    G.mounted = {};
    G.container.innerHTML =
      '<div class="tg-top' + (G.query ? ' searching' : '') + '">' +
        (G.overlay
          ? '<button type="button" class="tg-link tg-back" data-action="close-overlay" title="Back to the channel (Esc)">' + ICON_BACK + '<span>Back to the channel</span></button>'
          : '<div class="tg-title">Live TV</div>') +
        '<div class="tg-toggle" role="tablist"></div>' +
        (G.overlay ? '' : '<button type="button" class="tg-link tg-mv" data-action="mv" title="Multi-view: 2 to 4 channels at once">' + ICON_MV + '<span>Multi-view</span></button>') +
        '<label class="tg-search">' + ICON_SEARCH +
          '<input type="search" placeholder="Find a channel" aria-label="Find a channel" value="' + escAttr(G.query) + '">' +
        '</label>' +
        (G.overlay ? '' : '<div class="tg-links">' +
          '<a class="tg-link" href="#/livetv?tab=3" title="Recordings">' + ICON_REC + '<span>Recordings</span></a>' +
          '<a class="tg-link" href="#/livetv?tab=4" title="Schedule">' + ICON_SCHED + '<span>Schedule</span></a>' +
        '</div>') +
      '</div>' +
      '<div class="tltv-notice" role="status"' + (G.notice ? '' : ' style="display:none"') + '>' + esc(G.notice) + '</div>' +
      '<div class="tg-pickbar" style="display:none"></div>' +
      '<div class="tg-info' + (PV.allowed || G.overlay ? '' : ' nobox') + '">' +
        '<div class="tg-box" data-action="box" title="' + (G.overlay ? 'Back to the channel' : 'Watch') + '">' +
          '<img class="tg-box-img" alt=""><video muted playsinline></video></div>' +
        '<div class="tg-info-text"><div class="tg-info-title"></div><div class="tg-info-when"></div><div class="tg-info-ov"></div></div>' +
      '</div>' +
      '<div class="tg-days"></div>' +
      '<div class="tg-body"><div class="tg-loading">Loading the guide…</div></div>';
    renderToggle();
    renderPickBar();
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

  // ── Keeping the place ─────────────────────────────────────────────

  // Where the guide is: the view and day, the top row (by channel, so a
  // row that came or went meanwhile doesn't shift it) and the time at the
  // left edge, or "now" when it was at the current time.
  function keepPlace() {
    if (!G.channels || G.loadError || !G.days.length) return;
    var sc = G.container && G.container.querySelector('.tg-scroller');
    var m = G.m || metrics(), day = G.days[G.dayIndex];
    var place = { uid: G.userId, view: G.view, day: day.start, top: 0, topId: '', topOff: 0,
      leftT: day.start, atNow: true, channelId: G.lastId };
    if (sc) {
      var i = Math.floor(sc.scrollTop / m.rowH);
      place.top = sc.scrollTop;
      place.topId = G.rows[i] ? G.rows[i].id : '';
      place.topOff = sc.scrollTop - i * m.rowH;
      place.leftT = day.start + sc.scrollLeft / m.ppm * MIN;
      var now = Date.now();
      place.atNow = now >= day.start && now < day.end &&
        Math.abs(sc.scrollLeft - Math.max(0, x(now) - 30 * m.ppm)) <= 15 * m.ppm;
    }
    G.place = place;
  }

  function restorePlace(place) {
    var sc = G.container && G.container.querySelector('.tg-scroller');
    if (!sc) return;
    var m = G.m, day = G.days[G.dayIndex], now = Date.now();
    var at = G.rows.map(function (c) { return c.id; });
    var i = place.topId ? at.indexOf(place.topId) : -1;
    sc.scrollTop = i >= 0 ? i * m.rowH + place.topOff : place.top;
    sc.scrollLeft = place.atNow && now >= day.start && now < day.end
      ? Math.max(0, x(now) - 30 * m.ppm) : Math.round(x(place.leftT));
    // the channel watched (or the one channel up/down ended on) in view
    var w = place.channelId ? at.indexOf(place.channelId) : -1;
    if (w >= 0) {
      var view = sc.clientHeight - m.headH;
      if (w * m.rowH < sc.scrollTop || (w + 1) * m.rowH > sc.scrollTop + view) {
        sc.scrollTop = Math.max(0, w * m.rowH - Math.round((view - m.rowH) / 2));
      }
    }
    drawRows();
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
        el.className = 'tg-row' + (c.id === G.lastId ? ' tg-last' : '');
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
    var pick = MV.picking ? MV.picks.indexOf(c.id) : -1;
    return '<div class="tg-chan' + (pick >= 0 ? ' tg-picked' : '') + '" data-action="play" title="' + escAttr(c.name) + '">' +
      '<span class="tg-logo-wrap">' + logo + (pick >= 0 ? '<b class="tg-pickno">' + (pick + 1) + '</b>' : '') + '</span>' +
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
    rowEl.querySelector('.tg-progs').innerHTML = progsHtml(list, day, Date.now(), channelId);
  }

  // ── What will record: a dot on the programme ──────────────────────
  // Jellyfin's timers (Record, a series, and the ones Tentacle's teams and
  // rules made) are red; a team's or rule's game still waiting for a free
  // connection is amber; one a team or rule in preview would record, hollow.
  // By channel and start, so it needs nothing from the programme request.

  var MARK_RANK = { pv: 1, wait: 2, rec: 3 };
  var MARK_TITLE = { rec: 'Will record', wait: 'Will record when a connection is free', pv: 'Preview: a team or rule would record this' };

  function markKey(channelId, startMs) { return normId(channelId) + '|' + Math.round(startMs / 60000); }

  function loadMarks() {
    var uid = G.userId;
    G.marksAt = Date.now();
    return Promise.all([
      apiGet('LiveTv/Timers', { IsActive: false }).catch(function () { return null; }),
      apiGet('TentacleDiscover/Recordings/marks', { userId: uid }).catch(function () { return null; }),
    ]).then(function (r) {
      var marks = {};
      var put = function (ch, start, kind) {
        var t = Date.parse(start);
        if (!ch || isNaN(t)) return;
        var k = markKey(ch, t);
        if (!marks[k] || MARK_RANK[kind] > MARK_RANK[marks[k]]) marks[k] = kind;
      };
      ((r[0] && r[0].Items) || []).forEach(function (t) {
        if (t.Status === 'New' || t.Status === 'InProgress') put(t.ChannelId, t.StartDate, 'rec');
      });
      ((r[1] && r[1].marks) || []).forEach(function (m) {
        put(m.channel_id, m.start, m.state === 'preview' ? 'pv' : (m.state === 'booked' || m.state === 'recording' || m.state === 'covered') ? 'rec' : 'wait');
      });
      var before = JSON.stringify(G.marks);
      G.marks = marks;
      if (G.active && JSON.stringify(marks) !== before) {
        Object.keys(G.mounted).forEach(function (id) { fillProgs(G.mounted[id], id); });
      }
    });
  }

  // One channel's programmes for `day` (undefined = still loading).
  function progsHtml(list, day, now, channelId) {
    if (!list) return '<div class="tg-prog tg-wait" style="left:0;width:100%"></div>';
    if (!list.length) {
      return '<div class="tg-prog tg-none" style="left:0;width:' + Math.round(x(day.end, day)) + 'px"><span class="tg-sticky">No guide information</span></div>';
    }
    var html = '', drawn = day.start;
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      // Programmes that overlap (a guide merged from several sources) never
      // draw on top of each other: a later one starts where the last ended.
      var s = Math.max(p.s, drawn), e = Math.min(p.e, day.end);
      if (e <= s) continue;
      drawn = e;
      var left = Math.round(x(s, day)), w = Math.round(x(e, day)) - left;
      var mark = channelId && p.e > now ? G.marks[markKey(channelId, p.s)] : '';
      var cls = 'tg-prog' + (p.e <= now ? ' past' : (p.s <= now ? ' live' : '')) + (mark ? ' tg-mark-' + mark : '');
      var prog = p.e > p.s && p.s <= now && now < p.e ? (now - p.s) / (p.e - p.s) * 100 : 0;
      html += '<div class="' + cls + '" data-action="prog" data-id="' + esc(p.id) + '" data-s="' + p.s + '" data-e="' + p.e + '"' +
        ' style="left:' + left + 'px;width:' + Math.max(0, w - 2) + 'px" title="' + escAttr(p.name + (p.ov ? '\n\n' + p.ov : '')) + '">' +
        '<span class="tg-sticky">' +
          '<span class="tg-prog-name">' + (mark ? '<i class="tg-recdot" title="' + escAttr(MARK_TITLE[mark]) + '"></i>' : '') +
            (p.s < day.start ? '‹ ' : '') + esc(p.name) + '</span>' +
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
      EnableImages: true,
      ImageTypeLimit: 1,
      EnableImageTypes: ['Thumb', 'Primary'],
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
          img: p.ImageTags && (p.ImageTags.Thumb ? { type: 'Thumb', tag: p.ImageTags.Thumb } :
            p.ImageTags.Primary ? { type: 'Primary', tag: p.ImageTags.Primary } : null),
        });
      });
      if (!G.active || G.days[G.dayIndex] !== day) return;
      ids.forEach(function (id) { if (G.mounted[id]) fillProgs(G.mounted[id], id); });
      firstInfo();
      if (G.cursor) showCursor(false);
    }).catch(function () {
      done();
      // not read (Jellyfin still starting, a blip): the rows in view ask again
      setTimeout(function () { if (G.active) drawRows(); }, 2000);
    });
  }

  // Every 30 s: move the now line; at a programme change redraw what's in view.
  function onTick() {
    if (!G.active) return;
    placeNow();
    if (Date.now() - G.marksAt >= 60000) loadMarks();
    if (Date.now() - (G.noticeAt || 0) >= 60000) loadNotice().then(showNotice);
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

    var act = t.closest('[data-action]');
    var a = act && act.dataset.action;
    if (a === 'mv') { e.preventDefault(); if (MV.picking) endPicking(); else startPicking(); return; }
    if (a === 'mv-start') { e.preventDefault(); startMultiView(); return; }
    if (a === 'mv-cancel') { e.preventDefault(); endPicking(); return; }
    if (a === 'close-overlay') { e.preventDefault(); closeOverlay(); return; }
    var unpick = t.closest('[data-unpick]');
    if (unpick) { e.preventDefault(); togglePick(unpick.dataset.unpick); return; }
    if (a === 'box') {
      e.preventDefault();
      if (G.overlay) { closeOverlay(); return; }
      var key = (G.container.querySelector('.tg-info').dataset.key || '').split('|')[0];
      if (key) pickOrPlay(key);
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

    if (t.closest('[data-action="star"]')) {
      e.preventDefault(); e.stopPropagation();
      if (MV.picking) { togglePick(id); return; }
      starClicked(id, t.closest('[data-action="star"]'));
      return;
    }

    var cell = t.closest('[data-action="prog"]');
    if (cell) {
      e.preventDefault();
      G.cursor = { id: id, t: +cell.dataset.s + 1 };
      progClicked(id, cell, { x: e.clientX, y: e.clientY });
      return;
    }
    if (t.closest('[data-action="play"]')) { e.preventDefault(); pickOrPlay(id); }
  }

  // The pointer over a programme or a channel: the header shows it.
  function onHover(e) {
    if (!infoShown() || MENU.el) return;
    var row = e.target.closest && e.target.closest('.tg-row');
    if (!row) return;
    var id = row.dataset.channelId;
    var cell = e.target.closest('.tg-prog[data-id]');
    if (cell) showInfo(id, progById(id, cell.dataset.id));
    else if (e.target.closest('.tg-chan')) showInfo(id, progAt(id, Date.now()));
  }

  // When the guide opens: the header shows what's on now on the channel
  // last watched (or the first row), without starting a preview.
  function firstInfo() {
    var el = G.container && G.container.querySelector('.tg-info');
    if (!el || el.dataset.key || !G.rows.length || !infoShown()) return;
    var id = G.lastId && channelById(G.lastId) && G.rows.some(function (c) { return c.id === G.lastId; }) ? G.lastId : G.rows[0].id;
    var p = progAt(id, Date.now());
    if (!p) return;
    var wanted = PV.wanted;
    showInfo(id, p);
    if (!G.overlay) { clearTimeout(PV.timer); PV.wanted = wanted; }
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

  // ── The header: what the pointer (or the keyboard) is on ───────────
  // Like the TV's guide header: a box, the programme's name, when and
  // where, and a few lines about it. The box shows the programme's picture
  // (else the channel's logo) and, after resting 1.5 s on a programme that
  // is on now, its channel playing muted (live preview). The box is there
  // only while Tentacle says a connection is spare for previews
  // (TentacleDiscover/LivePreview, read every 15 s); without it the name
  // moves to the left edge. In the player's guide the box holds the
  // channel playing. Not on phones (no pointer to rest; a tap plays).

  var PREVIEW_REST_MS = 1500;
  var PREVIEW_POLL_MS = 15000;
  var PREVIEW_HOLD_MS = 10000;   // a previewed channel clicked: the player joins its stream meanwhile
  var PV = { allowed: false, asked: 0, poll: null, timer: null, channelId: '', player: null, wanted: '', held: [] };

  function infoShown() { return !!(G.container && G.container.querySelector('.tg-info')) && (G.overlay || !G.m || !G.m.phone); }

  function channelById(id) {
    var c = null;
    (G.channels || []).some(function (x) { if (x.id === id) { c = x; return true; } return false; });
    return c;
  }

  function progAt(channelId, t) {
    var day = G.days[G.dayIndex];
    var list = day && G.progs[day.start] && G.progs[day.start][channelId];
    var hit = null;
    (list || []).some(function (p) { if (p.s <= t && t < p.e) { hit = p; return true; } return false; });
    return hit;
  }

  function progById(channelId, id) {
    var hit = null;
    Object.keys(G.progs).some(function (d) {
      var list = G.progs[d][channelId];
      return (list || []).some(function (p) { if (p.id === id) { hit = p; return true; } return false; });
    });
    return hit;
  }

  function logoUrl(c, h) {
    return c && c.logo ? api().getUrl('Items/' + c.id + '/Images/Primary', { maxHeight: h || 96, tag: c.logo, quality: 90 }) : '';
  }

  function whenText(p, c) {
    var day = startOfDay(p.s), today = startOfDay(Date.now());
    var d = day === today ? '' : (day === addDays(today, 1) ? 'Tomorrow ' :
      new Date(p.s).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' }) + ' · ');
    return d + fmtRange(p.s, p.e) + (c ? ' · ' + c.name : '');
  }

  // The header shows channel `channelId` and programme `p` (null: the channel alone).
  function showInfo(channelId, p) {
    if (!infoShown()) return;
    var c = channelById(channelId);
    if (!c) return;
    var el = G.container.querySelector('.tg-info');
    var key = channelId + '|' + (p ? p.id : '');
    if (el.dataset.key === key) return;
    el.dataset.key = key;
    var now = Date.now();
    var onNow = !p || (p.s <= now && now < p.e);
    el.querySelector('.tg-info-title').textContent = p ? p.name : c.name;
    var when = el.querySelector('.tg-info-when');
    when.textContent = p ? whenText(p, c) : (c.number ? c.number + ' · ' : '') + c.name;
    when.classList.toggle('live', !!p && onNow);
    var ov = el.querySelector('.tg-info-ov');
    ov.textContent = p ? (p.ov || '') : '';
    var img = el.querySelector('.tg-box-img');
    var pic = p && p.img ? api().getUrl('Items/' + p.id + '/Images/' + p.img.type, { maxWidth: 480, tag: p.img.tag, quality: 85 }) : '';
    var src = pic || logoUrl(c, 160);
    img.classList.toggle('logo', !pic);
    if (src) { if (img.getAttribute('src') !== src) img.setAttribute('src', src); img.style.visibility = ''; }
    else { img.removeAttribute('src'); img.style.visibility = 'hidden'; }
    if (!G.overlay) schedulePreview(onNow ? channelId : '');
  }

  function showBox() {
    var el = G.container && G.container.querySelector('.tg-info');
    if (el) el.classList.toggle('nobox', !(PV.allowed || G.overlay));
  }

  function pollPreview() {
    learnUser();
    if (!G.active || G.overlay || (G.m && G.m.phone) || !G.userId) return;
    PV.asked = Date.now();
    var q = { userId: G.userId };
    if (PV.player) q.previewing = true;
    apiGet('TentacleDiscover/LivePreview', q).then(function (r) {
      var was = PV.allowed;
      PV.allowed = !!(r && r.previews);
      if (!PV.allowed) stopPreview();
      if (was !== PV.allowed) showBox();
    }).catch(function () { });
  }

  function schedulePreview(channelId) {
    if (channelId && channelId === PV.channelId && PV.player) return;
    clearTimeout(PV.timer);
    PV.wanted = channelId;
    if (PV.channelId && PV.channelId !== channelId) stopPreview();
    if (!channelId || !PV.allowed || G.overlay || MV.picking || (G.m && G.m.phone) || !LivePicture.supported()) return;
    PV.timer = setTimeout(function () { startPreview(channelId); }, PREVIEW_REST_MS);
  }

  function startPreview(channelId) {
    if (!G.active || PV.wanted !== channelId || !G.container) return;
    var box = G.container.querySelector('.tg-box');
    var video = box && box.querySelector('video');
    if (!video) return;
    stopPreview();
    PV.channelId = channelId;
    var player = PV.player = new LivePicture(video, {
      mime: 'video/mp4; codecs="avc1.42C01E"',
      onEnd: function () { if (PV.player === player) stopPreview(); },
    });
    video.muted = true;
    player.start(api().getUrl('TentacleDiscover/LivePreview/' + channelId + '/Stream'), authHeaders()).then(function () {
      if (PV.player === player) box.classList.add('playing');
    }).catch(function () {
      if (PV.player === player) stopPreview();
      // refused (no connection spare): ask again now rather than in 15 s
      pollPreview();
    });
  }

  // `hold`: the channel is about to play full screen; its stream stays open
  // a few seconds longer so the player joins it (no second connection).
  function stopPreview(hold) {
    clearTimeout(PV.timer);
    var player = PV.player;
    PV.player = null;
    PV.channelId = '';
    var box = G.container && G.container.querySelector('.tg-box');
    if (box) box.classList.remove('playing');
    if (!player) return;
    if (hold) {
      player.video.muted = true;
      player.opts.paused = true;
      try { player.video.pause(); } catch (e) { }
      PV.held.push(player);
      setTimeout(function () {
        player.stop();
        PV.held = PV.held.filter(function (x) { return x !== player; });
        if (box && !PV.player) {
          var v = box.querySelector('video');
          if (v === player.video) { /* the box's video is free again */ }
        }
      }, PREVIEW_HOLD_MS);
      // the box gets a fresh <video> so a new preview doesn't stop the held one
      if (box) {
        var fresh = document.createElement('video');
        fresh.muted = true;
        fresh.playsInline = true;
        fresh.setAttribute('playsinline', '');
        player.video.replaceWith(fresh);
      }
      return;
    }
    player.stop();
  }

  // ── The programme menu ─────────────────────────────────────────────
  // Like the TV's long press: Watch now (on now) or Tune to channel, Record
  // this / Record every episode (or Cancel this recording… / Cancel series…,
  // confirmed) for people who may manage recordings while it hasn't ended,
  // then Details. A click on a programme that isn't on now opens it too.

  function canManage() {
    var a = api();
    if (G.manage != null || !a || typeof a.getCurrentUser !== 'function') return Promise.resolve(!!G.manage);
    return Promise.resolve(a.getCurrentUser()).then(function (u) {
      G.manage = !!(u && u.Policy && (u.Policy.EnableLiveTvManagement || u.Policy.IsAdministrator));
      return G.manage;
    }).catch(function () { return false; });
  }

  // What the menu offers, in order (also what the details sheet shows).
  function programmeChoices(p, info, manage, now) {
    var onNow = p.s <= now && now < p.e, ended = p.e <= now;
    var out = [onNow ? 'watch' : 'tune'];
    if (manage && !ended) {
      out.push(info.TimerId ? 'cancel' : 'record');
      var episodes = info.IsSeries === true || info.IsMovie !== true;
      if (episodes || info.SeriesTimerId) out.push(info.SeriesTimerId ? 'cancelSeries' : 'recordSeries');
    }
    out.push('details');
    return out;
  }

  function programmeMenu(channelId, progId, at) {
    var c = channelById(channelId);
    var p = progById(channelId, progId);
    if (!c || !p) return;
    Promise.all([
      apiGet('LiveTv/Programs/' + progId, { UserId: G.userId }).catch(function () { return {}; }),
      canManage(),
    ]).then(function (r) {
      var info = r[0] || {};
      var items = programmeItems(c, p, info, r[1]);
      openMenu({ title: p.name + ' · ' + fmtTime(p.s), items: items, x: at.x, y: at.y, anchor: at.el });
    });
  }

  function programmeItems(c, p, info, manage) {
    var label = {
      watch: 'Watch now', tune: 'Tune to channel', record: 'Record this', recordSeries: 'Record every episode',
      cancel: 'Cancel this recording…', cancelSeries: 'Cancel series…', details: 'Details',
    };
    return programmeChoices(p, info, manage, Date.now()).map(function (k) {
      var it = { label: label[k] };
      if (k === 'watch' || k === 'tune') it.run = function () { closeOverlayFor(c.id); playChannel(c.id); };
      if (k === 'record') it.run = function () { record(p, false); };
      if (k === 'recordSeries') it.run = function () { record(p, true); };
      if (k === 'cancel') it.run = function () {
        var now = Date.now();
        confirmMenu({
          message: p.s <= now ? 'This programme is recording now. Cancelling stops the recording.' : 'Cancel this recording?',
          confirm: 'Cancel recording', cancel: 'Keep it', danger: true,
          run: function () { cancelTimer('LiveTv/Timers/' + info.TimerId); },
        });
      };
      if (k === 'cancelSeries') {
        it.hint = 'Stops every recording of this series';
        it.run = function () {
          confirmMenu({
            message: 'Cancel every recording of ' + p.name + '?', confirm: 'Cancel series', cancel: 'Keep it', danger: true,
            run: function () { cancelTimer('LiveTv/SeriesTimers/' + info.SeriesTimerId); },
          });
        };
      }
      if (k === 'details') it.run = function () { programmeDetails(c, p, info, manage); };
      return it;
    });
  }

  // Details: the programme's own sheet (no Jellyfin page with a blank
  // picture): picture if it has one, name, when, channel, what it is about,
  // and the same actions.
  function programmeDetails(c, p, info, manage) {
    var tag = info.ImageTags && (info.ImageTags.Thumb ? ['Thumb', info.ImageTags.Thumb] : info.ImageTags.Primary ? ['Primary', info.ImageTags.Primary] : null);
    var pic = tag ? api().getUrl('Items/' + p.id + '/Images/' + tag[0], { maxWidth: 640, tag: tag[1], quality: 85 }) : '';
    var meta = [info.OfficialRating, (info.Genres || []).slice(0, 3).join(', ')].filter(Boolean).join(' · ');
    var body = '<div class="tmenu-details">' +
      (pic ? '<img class="tmenu-pic" alt="" src="' + escAttr(pic) + '">' : '') +
      '<div class="tmenu-when">' + esc(whenText(p, c)) + '</div>' +
      (meta ? '<div class="tmenu-meta">' + esc(meta) + '</div>' : '') +
      ((info.Overview || p.ov) ? '<div class="tmenu-ov">' + esc(info.Overview || p.ov) + '</div>' : '') +
      '</div>';
    var items = programmeItems(c, p, info, manage).filter(function (it) { return it.label !== 'Details'; });
    var m = openMenu({ title: p.name, body: body, items: items });
    if (m) m.classList.add('details');
  }

  function record(p, series) {
    apiGet('LiveTv/Timers/Defaults', { programId: p.id }).then(function (d) {
      return apiPost(series ? 'LiveTv/SeriesTimers' : 'LiveTv/Timers', d);
    }).then(function () {
      toast(series ? 'Every episode will record' : 'Program set to record');
      setTimeout(loadMarks, 400);
    }, function () {
      toast('The recording couldn’t be set. Try again in a moment.');
    });
  }

  function cancelTimer(path) {
    Promise.resolve(api().ajax({ type: 'DELETE', url: api().getUrl(path) })).then(function () {
      toast('Recording cancelled');
      setTimeout(loadMarks, 400);
    }, function () {
      toast('That couldn’t be cancelled. Try again in a moment.');
    });
  }

  // A channel's name: Watch, and its star (taking it off asks first, as on the TV).
  function channelMenu(channelId, at) {
    var c = channelById(channelId);
    if (!c) return;
    openMenu({
      title: c.name, x: at.x, y: at.y, anchor: at.el,
      items: [
        { label: 'Watch now', run: function () { closeOverlayFor(c.id); playChannel(c.id); } },
        { label: c.fav ? 'Remove from Favorites…' : 'Add to Favorites', run: function () { starClicked(c.id, at.el); } },
      ],
    });
  }

  function starClicked(id, anchor) {
    var c = channelById(id);
    if (!c) return;
    if (!c.fav) { toggleFav(id); toast('Added to Favorites'); return; }
    confirmMenu({
      message: 'Remove ' + c.name + ' from your Favorites?', confirm: 'Remove', anchor: anchor,
      run: function () { toggleFav(id); },
    });
  }

  // ── The keyboard in the guide ──────────────────────────────────────
  // Arrows move a cursor over the programmes (Up/Down keep the time),
  // Enter is a click, the Menu key (or Shift+F10) opens the menu, the
  // header shows what the cursor is on. Home / End: now / the row's last.

  function moveCursor(dRow, dProg) {
    if (!G.rows.length || !G.days.length) return;
    var day = G.days[G.dayIndex], now = Date.now();
    var cur = G.cursor;
    if (!cur || G.rows.map(function (c) { return c.id; }).indexOf(cur.id) < 0) {
      var sc0 = G.container.querySelector('.tg-scroller');
      var first = sc0 ? Math.min(G.rows.length - 1, Math.floor(sc0.scrollTop / G.m.rowH)) : 0;
      cur = { id: G.rows[first].id, t: (now >= day.start && now < day.end) ? now : day.start };
      dRow = 0; dProg = 0;
    }
    var i = G.rows.map(function (c) { return c.id; }).indexOf(cur.id);
    i = Math.max(0, Math.min(G.rows.length - 1, i + dRow));
    var id = G.rows[i].id, t = cur.t;
    var list = (G.progs[day.start] && G.progs[day.start][id]) || [];
    if (dProg && list.length) {
      var k = -1;
      list.forEach(function (p, j) { if (p.s <= t && t < p.e) k = j; });
      if (k < 0) k = dProg > 0 ? -1 : list.length;
      k = Math.max(0, Math.min(list.length - 1, k + dProg));
      t = Math.max(list[k].s, day.start) + 1;
    }
    G.cursor = { id: id, t: t };
    showCursor(true);
  }

  function showCursor(scroll) {
    var cur = G.cursor;
    if (!cur || !G.container) return;
    var sc = G.container.querySelector('.tg-scroller');
    var i = G.rows.map(function (c) { return c.id; }).indexOf(cur.id);
    if (sc && scroll && i >= 0) {
      var m = G.m, view = sc.clientHeight - m.headH;
      if (i * m.rowH < sc.scrollTop) sc.scrollTop = i * m.rowH;
      else if ((i + 1) * m.rowH > sc.scrollTop + view) sc.scrollTop = (i + 1) * m.rowH - view;
      var p = progAt(cur.id, cur.t);
      var day = G.days[G.dayIndex];
      if (p) {
        var left = x(Math.max(p.s, day.start)), right = x(Math.min(p.e, day.end));
        var w = sc.clientWidth - m.chanW;
        if (left < sc.scrollLeft) sc.scrollLeft = left;
        else if (right > sc.scrollLeft + w) sc.scrollLeft = Math.min(left, right - w);
      }
      drawRows();
    }
    var old = G.container.querySelectorAll('.tg-prog.kfocus');
    for (var j = 0; j < old.length; j++) old[j].classList.remove('kfocus');
    var row = G.mounted[cur.id], prog = progAt(cur.id, cur.t);
    if (row && prog) {
      var cell = row.querySelector('.tg-prog[data-id="' + cssId(prog.id) + '"]');
      if (cell) cell.classList.add('kfocus');
    }
    showInfo(cur.id, prog);
  }

  function cssId(id) { return String(id).replace(/["\\]/g, ''); }

  function onGuideKey(e) {
    if (!G.active || MENU.el || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    var t = e.target;
    if (t && t.closest && (t.closest('input, textarea, select') || t.closest('.tg-top, .tg-days, .tg-pickbar'))) return;
    if (MV.view && MV.view.style.display !== 'none') return;   // the multi-view grid has its own keys
    var k = e.key, menuKey = k === 'ContextMenu' || (k === 'F10' && e.shiftKey);
    if (k === 'ArrowDown') moveCursor(1, 0);
    else if (k === 'ArrowUp') moveCursor(-1, 0);
    else if (k === 'ArrowRight') moveCursor(0, 1);
    else if (k === 'ArrowLeft') moveCursor(0, -1);
    else if (k === 'PageDown' || k === 'PageUp') {
      var n = Math.max(1, Math.floor((G.container.querySelector('.tg-scroller').clientHeight - G.m.headH) / G.m.rowH) - 1);
      moveCursor(k === 'PageDown' ? n : -n, 0);
    } else if ((k === 'Enter' || menuKey) && G.cursor) {
      var p = progAt(G.cursor.id, G.cursor.t);
      var row = G.mounted[G.cursor.id];
      var cell = row && p && row.querySelector('.tg-prog[data-id="' + cssId(p.id) + '"]');
      var r = cell ? cell.getBoundingClientRect() : { left: 200, bottom: 200 };
      if (menuKey) { if (p) programmeMenu(G.cursor.id, p.id, { x: r.left + 8, y: r.bottom + 4, el: cell }); }
      else if (cell) progClicked(G.cursor.id, cell, { x: r.left + 8, y: r.bottom + 4 });
      else pickOrPlay(G.cursor.id);
    } else if (k === 'Escape' && G.overlay) {
      closeOverlay();
    } else if (k === 'Escape' && MV.picking) {
      endPicking();
    } else {
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
  }
  document.addEventListener('keydown', onGuideKey, true);

  // A programme clicked (or Enter on it): on now → its channel plays at
  // once; otherwise its menu. In multi-view's pick mode it picks the channel.
  function progClicked(channelId, cell, at) {
    if (MV.picking) { togglePick(channelId); return; }
    var now = Date.now();
    if (+cell.dataset.s <= now && now < +cell.dataset.e) {
      closeOverlayFor(channelId);
      watch(channelId);
    } else {
      programmeMenu(channelId, cell.dataset.id, { x: at.x, y: at.y, el: cell });
    }
  }

  function pickOrPlay(channelId) {
    if (MV.picking) { togglePick(channelId); return; }
    closeOverlayFor(channelId);
    watch(channelId);
  }

  // Play a channel from the guide; a channel being previewed keeps its
  // stream for a few seconds so the player joins it.
  function watch(channelId) {
    if (PV.player && PV.channelId === channelId) stopPreview(true);
    else stopPreview();
    playChannel(channelId);
  }

  // ── Dates only as far as real guide data (as on the TV) ────────────
  // One stray listing days ahead (an event channel) used to add a tab for
  // every day up to it. A day is offered only when it has at least a
  // quarter of the programmes of the busiest day before it; the first day
  // that hasn't ends the strip. Counted with count-only requests.

  var DAY_SHARE = 0.25;

  // counts: programmes per day from today, in order. How many days to offer.
  function daysWithData(counts) {
    var n = 0, best = 0;
    for (var i = 0; i < counts.length; i++) {
      var c = counts[i] || 0;
      if (i > 0 && c < best * DAY_SHARE) break;
      if (i > 0 && c === 0) break;
      best = Math.max(best, c);
      n = i + 1;
    }
    return Math.max(1, n);
  }

  function countDay(view, start) {
    var body = {
      UserId: G.userId,
      MinStartDate: new Date(start).toISOString(),
      MaxStartDate: new Date(addDays(start, 1) - 1).toISOString(),
      Limit: 0,
      EnableImages: false,
      EnableUserData: false,
      EnableTotalRecordCount: true,
    };
    if (view === 'favorites') body.ChannelIds = favChannels().map(function (c) { return c.id; });
    return apiPost('LiveTv/Programs', body).then(function (r) { return (r && r.TotalRecordCount) || 0; });
  }

  // The furthest end worth showing for `view`: maxEnd cut at the last day with real data.
  function trimMaxEnd(view, maxEnd) {
    var today = startOfDay(Date.now());
    if (!(maxEnd > addDays(today, 1))) return Promise.resolve(maxEnd);
    var starts = [];
    for (var s = today; s < maxEnd && starts.length < 21; s = addDays(s, 1)) starts.push(s);
    var counts = [];
    var next = function (i) {
      if (i >= starts.length) return Promise.resolve();
      return countDay(view, starts[i]).then(function (n) {
        counts.push(n);
        if (i > 0 && daysWithData(counts) < counts.length) return;   // this day failed: stop counting
        return next(i + 1);
      });
    };
    return next(0).then(function () {
      var n = daysWithData(counts);
      return n >= starts.length ? maxEnd : addDays(starts[n - 1], 1);
    }, function () { return maxEnd; });
  }

  // ── Multi-view: 2-4 channels in one picture (as on the TV) ─────────
  // Multi-view in the header asks Tentacle first (free connections; its
  // own words when there aren't enough), then the guide picks: a click on
  // a channel or one of its programmes ticks it (a numbered mark, again
  // unticks), the bar shows the picks and "2 of 4 · Start". Tentacle makes
  // one picture of them (TentacleDiscover/MultiView, the TV's session),
  // here as fMP4 with the sound of the channel being heard. A click on a
  // channel hears it (a second read, swapped in when it plays), a double
  // click (or its ⤢, Enter) fills the screen with it: the grid's own picture
  // zoomed onto that channel, so it is there at once, costs no connection
  // and rides through the provider's cuts like the grid does (a player of
  // the channel stalled on every cut: with 4 channels open the provider
  // closes each one every 10-15 s). Esc, Back or a double click returns to
  // the grid; arrows go to the next channel. Its menu swaps or removes a
  // channel. Leaving gives every connection back at once. The screen stays
  // awake while it plays.

  var MV_KEEPALIVE_MS = 15000;
  var MV = { picking: false, picks: [], max: 4, session: null, items: [], view: null, heard: 0, focus: 0, zoom: -1,
    player: null, spare: null, keep: null, wake: null, fullScreen: false, busy: false, hideTimer: null };

  function mvUrl(path) { return api().getUrl('TentacleDiscover/MultiView' + (path || '')); }

  function mvRequest(type, path, body) {
    return new Promise(function (resolve, reject) {
      var a = api();
      fetch(a.getUrl('TentacleDiscover/MultiView' + (path || '')), {
        method: type,
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: body ? JSON.stringify(body) : undefined,
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (r.ok) resolve(j);
          else reject(new Error((j && (j.detail || j.message)) || 'Multi-view didn’t start. Try again in a moment.'));
        });
      }, function () { reject(new Error('Multi-view didn’t start. Try again in a moment.')); });
    });
  }

  function startPicking() {
    if (MV.busy) return;
    if (!LivePicture.supported()) { toast('This browser can’t play multi-view.'); return; }
    MV.busy = true;
    apiGet('TentacleDiscover/MultiView', { userId: G.userId }).then(function (r) {
      MV.busy = false;
      if (!r || r.available === false) { toast((r && r.message) || 'Multi-view isn’t available on this server'); return; }
      if (!r.can_start) { toast(r.message || 'Not enough free connections for multi-view right now.'); return; }
      MV.picking = true;
      MV.picks = [];
      MV.max = Math.max(2, Math.min(4, r.max_tiles || 4));
      stopPreview();
      G.container.classList.add('tg-picking');
      renderPickBar();
      redrawMounted();
    }, function () {
      MV.busy = false;
      toast('Multi-view isn’t available on this server');
    });
  }

  function endPicking() {
    MV.picking = false;
    MV.picks = [];
    if (G.container) G.container.classList.remove('tg-picking');
    renderPickBar();
    redrawMounted();
  }

  function togglePick(id) {
    var i = MV.picks.indexOf(id);
    if (i >= 0) MV.picks.splice(i, 1);
    else if (MV.picks.length >= MV.max) {
      toast(MV.max < 4 ? 'Only ' + MV.max + ' channels: that is how many connections are free' : 'Up to 4 channels');
      return;
    } else MV.picks.push(id);
    renderPickBar();
    redrawMounted();
  }

  function redrawMounted() {
    Object.keys(G.mounted).forEach(function (id) {
      var c = channelById(id), el = G.mounted[id];
      if (c && el) el.querySelector('.tg-chan').outerHTML = chanCell(c);
    });
  }

  function renderPickBar() {
    var bar = G.container && G.container.querySelector('.tg-pickbar');
    if (!bar) return;
    if (!MV.picking) { bar.innerHTML = ''; bar.style.display = 'none'; return; }
    bar.style.display = '';
    var n = MV.picks.length;
    bar.innerHTML =
      '<div class="tg-pick-title">Multi-view <span>Pick 2 to ' + MV.max + ' channels</span></div>' +
      '<div class="tg-pick-list">' + (n ? MV.picks.map(function (id, i) {
        var c = channelById(id) || { name: '', id: id };
        return '<button type="button" class="tg-pick-chip" data-unpick="' + escAttr(id) + '" title="Take ' + escAttr(c.name) + ' out">' +
          '<b>' + (i + 1) + '</b>' + esc(c.name) + '<i>×</i></button>';
      }).join('') : '<span class="tg-pick-none">Nothing picked yet</span>') + '</div>' +
      '<button type="button" class="tg-btn tg-pick-start" data-action="mv-start"' + (n < 2 ? ' disabled' : '') + '>' +
        (n ? n + ' of ' + MV.max + ' · Start' : 'Start') + '</button>' +
      '<button type="button" class="tg-link tg-pick-cancel" data-action="mv-cancel">Cancel</button>';
  }

  function startMultiView() {
    if (MV.busy || MV.picks.length < 2) return;
    MV.busy = true;
    var items = MV.picks.slice();
    var btn = G.container.querySelector('.tg-pick-start');
    if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
    mvRequest('POST', '', { item_ids: items }).then(function (s) {
      MV.busy = false;
      MV.session = s;
      MV.items = items;
      MV.heard = 0;
      MV.focus = 0;
      endPicking();
      openGrid();
    }, function (err) {
      MV.busy = false;
      toast(err.message);
      renderPickBar();
    });
  }

  function itemOf(i) {
    var t = MV.session && MV.session.tiles[i];
    return (t && t.item_id) || MV.items[i] || '';
  }

  function tileName(i) {
    var t = MV.session && MV.session.tiles[i];
    var c = channelById(itemOf(i)) || channelById(normFind(itemOf(i)));
    return (c && c.name) || (t && t.name) || '';
  }

  function normFind(id) {
    var n = normId(id), hit = '';
    (G.channels || []).some(function (c) { if (normId(c.id) === n) { hit = c.id; return true; } return false; });
    return hit;
  }

  function openGrid() {
    if (!MV.session) return;
    MV.fullScreen = false;
    var el = MV.view;
    if (!el) {
      el = MV.view = document.createElement('div');
      el.id = 'tentacleMultiView';
      el.innerHTML =
        '<div class="mv-stage"><div class="mv-zoom"><video class="mv-video" playsinline></video><video class="mv-video" playsinline></video><div class="mv-tiles"></div></div></div>' +
        '<div class="mv-top"><button type="button" class="mv-back" data-mv="leave" title="Back to the guide (Esc)">' +
          '<svg viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg><span>Guide</span></button>' +
          '<div class="mv-title">Multi-view</div><div class="mv-hint">Click a channel to hear it · double-click to fill the screen</div></div>' +
        '<div class="mv-zoombar"><button type="button" class="mv-back" data-mv="unzoom" title="Back to all channels (Esc)">' +
          '<svg viewBox="0 0 24 24"><path d="M3 3h8v8H3V3zm10 0h8v8h-8V3zM3 13h8v8H3v-8zm10 0h8v8h-8v-8z"/></svg><span>All channels</span></button>' +
          '<div class="mv-title mv-zoomname"></div></div>' +
        '<div class="mv-state" role="status"></div>';
      document.body.appendChild(el);
      el.addEventListener('click', onGridClick);
      el.addEventListener('dblclick', function (e) {
        if (e.target.closest('[data-mv], .mv-top, .mv-zoombar')) return;
        e.preventDefault();
        if (MV.zoom >= 0) { unzoom(); return; }
        var t = e.target.closest('.mv-tile');
        if (t) zoomTile(+t.dataset.i);
      });
      onMenuGesture(el, '.mv-tile', function (t, x, y) { tileMenu(+t.dataset.i, { x: x, y: y }); });
      el.addEventListener('mousemove', wakeChrome);
      el.addEventListener('touchstart', wakeChrome, { passive: true });
    }
    document.body.classList.add('tentacle-multiview-active');
    el.style.display = '';
    renderTiles();
    setState('Starting…');
    playHeard();
    keepAwake(true);
    wakeChrome();
    if (!MV.keep) MV.keep = setInterval(keepMultiView, MV_KEEPALIVE_MS);
  }

  function wakeChrome() {
    if (!MV.view) return;
    MV.view.classList.remove('idle');
    clearTimeout(MV.hideTimer);
    MV.hideTimer = setTimeout(function () { if (MV.view) MV.view.classList.add('idle'); }, 3500);
  }

  function setState(text, retry) {
    var s = MV.view && MV.view.querySelector('.mv-state');
    if (!s) return;
    s.innerHTML = text ? '<div>' + esc(text) + '</div>' +
      (retry ? '<div class="mv-state-btns"><button type="button" class="tg-btn" data-mv="retry">Try again</button>' +
        '<button type="button" class="tg-link" data-mv="leave">Back to the guide</button></div>' : '') : '';
    s.style.display = text ? '' : 'none';
  }

  // The heard and focused tile marked in place (a click must not rebuild the
  // tiles: a double click needs both clicks on the same element).
  function markTiles() {
    var box = MV.view && MV.view.querySelector('.mv-tiles');
    var s = MV.session;
    if (!box || !s) return;
    var els = box.querySelectorAll('.mv-tile');
    if (els.length !== s.tiles.length) { renderTiles(); return; }
    for (var i = 0; i < els.length; i++) {
      els[i].classList.toggle('heard', i === MV.heard);
      els[i].classList.toggle('focus', i === MV.focus);
      var note = els[i].querySelector('.mv-note');
      if (i === MV.heard && !note) els[i].querySelector('.mv-name').insertAdjacentHTML('afterbegin', '<i class="mv-note">♪</i>');
      if (i !== MV.heard && note) note.remove();
    }
  }

  function renderTiles() {
    var box = MV.view && MV.view.querySelector('.mv-tiles');
    var s = MV.session;
    if (!box || !s) return;
    var W = s.width || 1920, H = s.height || 1080;
    box.innerHTML = s.tiles.map(function (t, i) {
      var ended = t.live === false;
      return '<div class="mv-tile' + (i === MV.heard ? ' heard' : '') + (i === MV.focus ? ' focus' : '') + '" data-i="' + i + '" tabindex="-1"' +
        ' style="left:' + (t.x / W * 100) + '%;top:' + (t.y / H * 100) + '%;width:' + (t.width / W * 100) + '%;height:' + (t.height / H * 100) + '%">' +
        '<span class="mv-name">' + (i === MV.heard ? '<i class="mv-note">♪</i>' : '') + esc(tileName(i)) + (ended ? ' · stopped' : '') + '</span>' +
        '<span class="mv-acts">' +
          '<button type="button" class="mv-act" data-mv="full" title="Watch full screen"><svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg></button>' +
          '<button type="button" class="mv-act" data-mv="menu" title="More"><svg viewBox="0 0 24 24"><path d="M12 8a2 2 0 100-4 2 2 0 000 4zm0 2a2 2 0 100 4 2 2 0 000-4zm0 6a2 2 0 100 4 2 2 0 000-4z"/></svg></button>' +
        '</span></div>';
    }).join('');
  }

  // The picture with tile `MV.heard`'s sound; a second <video> takes over
  // when it plays, so the picture never goes black for a sound change.
  function playHeard() {
    var s = MV.session;
    if (!s || !MV.view) return;
    var vids = MV.view.querySelectorAll('.mv-video');
    var cur = MV.player;
    var video = cur && cur.video === vids[0] ? vids[1] : vids[0];
    if (MV.spare) { MV.spare.stop(); MV.spare = null; }
    var player = new LivePicture(video, {
      mime: 'video/mp4; codecs="avc1.640028, mp4a.40.2"',
      onEnd: function () {
        if (MV.player !== player || !MV.session) return;
        // a blip (the connection to the server): read again, quietly, twice
        if ((MV.retries || 0) < 2) { MV.retries = (MV.retries || 0) + 1; playHeard(); return; }
        setState('The picture stopped.', true);
      },
    });
    MV.spare = player;
    video.muted = true;   // heard once it takes over: never two sounds at once
    player.start(api().getUrl('TentacleDiscover/MultiView/' + s.id + '/Web', { audio: MV.heard })).then(function () {
      if (MV.spare !== player) { player.stop(); return; }
      MV.spare = null;
      MV.retries = 0;
      var old = MV.player;
      MV.player = player;
      if (old) { old.video.classList.remove('on'); old.stop(); }
      video.classList.add('on');
      video.muted = false;
      setState('');
    }, function (err) {
      if (MV.spare === player) MV.spare = null;
      player.stop();
      if (!MV.session) return;
      if (!MV.player) setState(err && /ended|404/.test(err.message) ? 'Multi-view ended' : 'The picture didn’t start.', true);
    });
  }

  function hear(i) {
    if (!MV.session || i < 0 || i >= MV.session.tiles.length) return;
    MV.focus = i;
    if (i !== MV.heard) { MV.heard = i; markTiles(); playHeard(); }
    else markTiles();
  }

  function onGridClick(e) {
    wakeChrome();
    var b = e.target.closest('[data-mv]');
    var tile = e.target.closest('.mv-tile');
    if (b) {
      e.preventDefault();
      var what = b.dataset.mv;
      if (what === 'leave') leaveMultiView();
      else if (what === 'retry') { setState('Starting…'); playHeard(); }
      else if (what === 'full' && tile) zoomTile(+tile.dataset.i);
      else if (what === 'unzoom') unzoom();
      else if (what === 'menu' && tile) {
        var r = b.getBoundingClientRect();
        tileMenu(+tile.dataset.i, { x: r.left, y: r.bottom + 4, el: b });
      }
      return;
    }
    if (tile && MV.zoom < 0) hear(+tile.dataset.i);
  }

  // Tile i fills the screen: the grid's picture scaled onto it, its sound heard.
  function zoomTile(i) {
    var s = MV.session;
    if (!s || !MV.view || i < 0 || i >= s.tiles.length) return;
    closeMenu();
    hear(i);
    MV.zoom = i;
    var t = s.tiles[i], W = s.width || 1920, H = s.height || 1080;
    MV.view.querySelector('.mv-zoom').style.transform =
      'scale(' + (W / t.width) + ',' + (H / t.height) + ') translate(' + (-t.x / W * 100) + '%,' + (-t.y / H * 100) + '%)';
    MV.view.querySelector('.mv-zoomname').textContent = tileName(i);
    MV.view.classList.add('zoomed');
    wakeChrome();
  }

  function unzoom() {
    if (!MV.view) return;
    MV.zoom = -1;
    MV.view.querySelector('.mv-zoom').style.transform = '';
    MV.view.classList.remove('zoomed');
    markTiles();
  }

  function tileMenu(i, at) {
    if (!MV.session) return;
    MV.focus = i;
    markTiles();
    var n = MV.session.tiles.length;
    openMenu({
      title: tileName(i), x: at.x, y: at.y, anchor: at.el,
      items: [
        { label: 'Fill the screen', run: function () { zoomTile(i); } },
        { label: 'Open in the player', hint: 'Jellyfin\u2019s player: pause, subtitles', run: function () { fullScreenTile(i); } },
        { label: 'Swap channel', run: function () { swapMenu(i, at); } },
        n > 2 ? { label: 'Remove from multi-view', run: function () { changeTiles(i, null); } } : null,
      ],
    });
  }

  function swapMenu(i, at) {
    var inGrid = {};
    MV.session.tiles.forEach(function (t, j) { inGrid[normId(itemOf(j))] = true; });
    var favs = favChannels().filter(function (c) { return !inGrid[normId(c.id)]; });
    if (!favs.length) { toast('No other favourite channels'); return; }
    openMenu({
      title: 'Swap for', x: at.x, y: at.y, anchor: at.el,
      items: favs.slice(0, 30).map(function (c) {
        return { label: c.name, run: function () { changeTiles(i, c.id); } };
      }),
    });
  }

  // Tile i swapped for channel `id`, or removed (id null): the same session.
  function changeTiles(i, id) {
    var s = MV.session;
    if (!s || MV.busy) return;
    var items = s.tiles.map(function (t, j) { return itemOf(j); });
    if (id) items[i] = id; else items.splice(i, 1);
    MV.busy = true;
    setState(id ? 'Swapping…' : 'Removing…');
    mvRequest('PUT', '/' + s.id, { item_ids: items }).then(function (ns) {
      MV.busy = false;
      MV.session = ns;
      MV.items = items;
      if (MV.heard >= ns.tiles.length) MV.heard = 0;
      if (!id && MV.heard > i) MV.heard--;
      MV.focus = Math.min(MV.focus, ns.tiles.length - 1);
      unzoom();
      playHeard();
    }, function (err) {
      MV.busy = false;
      setState('');
      toast(err.message);
    });
  }

  // Full screen: the channel in Jellyfin's player (its stream is open on
  // the server already, so no new connection); the session stays (kept
  // alive), and Back in the player returns to the grid.
  function fullScreenTile(i) {
    var id = itemOf(i);
    if (!id) return;
    MV.fullScreen = true;
    MV.fullAt = Date.now();
    MV.sawPlayer = false;
    MV.heard = i;
    stopGridPlayers();
    hideGrid();
    playChannel(normFind(id) || id);
  }

  function stopGridPlayers() {
    if (MV.spare) { MV.spare.stop(); MV.spare = null; }
    if (MV.player) { MV.player.stop(); MV.player.video.classList.remove('on'); MV.player = null; }
    keepAwake(false);
  }

  function hideGrid() {
    if (MV.view) MV.view.style.display = 'none';
    document.body.classList.remove('tentacle-multiview-active');
    closeMenu();
  }

  function leaveMultiView() {
    var s = MV.session;
    MV.zoom = -1;
    if (MV.view) { MV.view.classList.remove('zoomed'); MV.view.querySelector('.mv-zoom').style.transform = ''; }
    MV.session = null;
    MV.fullScreen = false;
    stopGridPlayers();
    hideGrid();
    clearInterval(MV.keep);
    MV.keep = null;
    if (s) mvRequest('DELETE', '/' + s.id).catch(function () { });
  }

  function keepMultiView() {
    var s = MV.session;
    if (!s) { clearInterval(MV.keep); MV.keep = null; return; }
    // full screen in the player and then somewhere else: it's over
    if (MV.fullScreen && !onPlayerPage() && !isGuidePage()) { leaveMultiView(); return; }
    mvRequest('POST', '/' + s.id + '/Keepalive').then(function (d) {
      if (MV.session !== s || !d || !d.tiles) return;
      var was = JSON.stringify(s.tiles.map(function (t) { return t.live; }));
      s.tiles = d.tiles;
      if (JSON.stringify(d.tiles.map(function (t) { return t.live; })) !== was) renderTiles();
    }, function (err) {
      if (MV.session === s && /ended|404/i.test(err.message)) {
        MV.session = null;
        stopGridPlayers();
        if (MV.view && MV.view.style.display !== 'none') setState('Multi-view ended', false);
        setTimeout(hideGrid, 2500);
      }
    });
  }

  // The screen stays on while the grid plays (a phone or laptop would dim
  // and lock), and is let go when it stops.
  function keepAwake(on) {
    if (!on) {
      if (MV.wake) { try { MV.wake.release(); } catch (e) { } MV.wake = null; }
      return;
    }
    if (MV.wake || !navigator.wakeLock || document.hidden) return;
    navigator.wakeLock.request('screen').then(function (w) {
      if (!MV.player && !MV.spare) { w.release(); return; }
      MV.wake = w;
      w.addEventListener('release', function () { if (MV.wake === w) MV.wake = null; });
    }).catch(function () { });
  }

  // The browser closes or goes elsewhere: every connection back now, not after 45 s.
  window.addEventListener('pagehide', function () {
    var s = MV.session;
    if (!s) return;
    try {
      fetch(mvUrl('/' + s.id), { method: 'DELETE', headers: authHeaders(), keepalive: true });
    } catch (e) { }
  });

  function onGridKey(e) {
    if (!MV.view || MV.view.style.display === 'none' || !MV.session || MENU.el) return;
    var n = MV.session.tiles.length, i = MV.focus, k = e.key;
    var cols = 2, row = Math.floor(i / cols), col = i % cols;
    wakeChrome();
    if (MV.zoom >= 0) {
      if (k === 'Escape' || k === 'Backspace' || k === 'Enter') unzoom();
      else if (k === 'ArrowRight' || k === 'ArrowDown') zoomTile((MV.zoom + 1) % n);
      else if (k === 'ArrowLeft' || k === 'ArrowUp') zoomTile((MV.zoom + n - 1) % n);
      else return;
      e.preventDefault();
      e.stopImmediatePropagation();
      return;
    }
    if (k === 'ArrowRight' && col === 0 && i + 1 < n) hear(i + 1);
    else if (k === 'ArrowLeft' && col === 1) hear(i - 1);
    else if (k === 'ArrowDown' && row === 0 && i + 2 < n) hear(i + 2);
    else if (k === 'ArrowDown' && row === 0 && n === 3 && i === 1) hear(2);
    else if (k === 'ArrowUp' && row === 1) hear(i - 2);
    else if (k === 'Enter') zoomTile(i);
    else if (k === 'ContextMenu' || (k === 'F10' && e.shiftKey)) {
      var t = MV.view.querySelector('.mv-tile[data-i="' + i + '"]');
      var r = t ? t.getBoundingClientRect() : { left: 100, top: 100 };
      tileMenu(i, { x: r.left + 16, y: r.top + 16 });
    } else if (k === 'Escape' || k === 'Backspace') leaveMultiView();
    else if (k.indexOf('Arrow') !== 0) return;
    e.preventDefault();
    e.stopImmediatePropagation();
  }
  document.addEventListener('keydown', onGridKey, true);

  // ── In the player: recent channels and the guide (as on the TV) ────
  // While a Live TV channel plays, Jellyfin's player gets two buttons:
  //   - Recent channels: the person's last channels, newest first, up to
  //     10, without the one playing, so the first card flips back to the
  //     previous channel. From Jellyfin's own per-person last-played date
  //     (set when any device starts a channel: it follows the person) plus
  //     this browser's own tunes; their favourites when there are none yet.
  //     Re-read while open when their data changes anywhere.
  //   - Guide: Tentacle's guide over the player, the channel playing on in
  //     the header's box, opened on its row; a programme on now (or a
  //     channel) switches to it, Escape or the box goes back.

  var RECENT_MAX = 10;
  var PL = { item: null, asked: 0, band: null, items: null, focus: 0, open: false, tunes: [], reread: null };

  function tunesKey() { return 'tentacle-recent-tunes-' + (G.userId || (api() && api().getCurrentUserId()) || ''); }

  function loadTunes() {
    try { PL.tunes = JSON.parse(localStorage.getItem(tunesKey()) || '[]') || []; } catch (e) { PL.tunes = []; }
    return PL.tunes;
  }

  function rememberTune(id) {
    loadTunes();
    var n = normId(id);
    PL.tunes = [{ id: id, at: Date.now() }].concat(PL.tunes.filter(function (t) { return normId(t.id) !== n; })).slice(0, RECENT_MAX + 1);
    try { localStorage.setItem(tunesKey(), JSON.stringify(PL.tunes)); } catch (e) { }
  }

  // Server's last-played list + this browser's tunes → newest first, without `playing`.
  function mergeRecents(server, tunes, playing, max) {
    var seen = {}, out = [];
    var all = (server || []).map(function (c) {
      return { c: c, at: Date.parse(c.UserData && c.UserData.LastPlayedDate) || 0 };
    });
    var byId = {};
    all.forEach(function (x) { byId[normId(x.c.Id)] = x; });
    (tunes || []).forEach(function (t) {
      var x = byId[normId(t.id)];
      if (x) x.at = Math.max(x.at, t.at);
      else all.push({ c: { Id: t.id, local: true }, at: t.at });
    });
    all.sort(function (a, b) { return b.at - a.at; });
    all.forEach(function (x) {
      var id = normId(x.c.Id);
      if (!x.at || seen[id] || id === normId(playing)) return;
      seen[id] = true;
      out.push(x.c);
    });
    return out.slice(0, max || RECENT_MAX);
  }

  function readRecents() {
    var uid = api().getCurrentUserId();
    var q = {
      UserId: uid, SortBy: 'DatePlayed', SortOrder: 'Descending', Limit: RECENT_MAX + 1,
      AddCurrentProgram: true, EnableUserData: true, EnableImageTypes: 'Primary', ImageTypeLimit: 1,
    };
    return apiGet('LiveTv/Channels', q).then(function (r) {
      var server = ((r && r.Items) || []).filter(function (c) { return c.UserData && c.UserData.LastPlayedDate; });
      var playing = PL.item ? PL.item.Id : '';
      var list = mergeRecents(server, loadTunes(), playing, RECENT_MAX);
      // this browser's tunes the server doesn't list yet: read those channels
      var missing = list.filter(function (c) { return c.local; }).map(function (c) { return c.Id; });
      var full = missing.length ? apiGet('Items', { Ids: missing.join(','), UserId: uid, Fields: 'ChannelInfo', EnableUserData: true })
        .then(function (x) { return (x && x.Items) || []; }).catch(function () { return []; }) : Promise.resolve([]);
      return full.then(function (extra) {
        var byId = {};
        extra.forEach(function (c) { byId[normId(c.Id)] = c; });
        list = list.map(function (c) { return c.local ? byId[normId(c.Id)] : c; }).filter(Boolean);
        if (list.length) return { title: 'Recent channels', items: list };
        return apiGet('LiveTv/Channels', { UserId: uid, IsFavorite: true, AddCurrentProgram: true, EnableUserData: true, Limit: 30 })
          .then(function (f) {
            var favs = ((f && f.Items) || []).filter(function (c) { return normId(c.Id) !== normId(playing); });
            return { title: 'Your favorite channels', items: favs };
          });
      });
    });
  }

  function recentCard(c, i) {
    var p = c.CurrentProgram, now = Date.now();
    var s = p && Date.parse(p.StartDate), e = p && Date.parse(p.EndDate);
    var pct = s && e && e > s ? Math.max(0, Math.min(100, (now - s) / (e - s) * 100)) : 0;
    var left = e > now ? Math.max(1, Math.round((e - now) / MIN)) : 0;
    var logo = c.ImageTags && c.ImageTags.Primary
      ? '<img alt="" src="' + escAttr(api().getUrl('Items/' + c.Id + '/Images/Primary', { maxHeight: 80, tag: c.ImageTags.Primary, quality: 90 })) + '">'
      : '<span class="tr-noimg">' + esc((c.Name || '').slice(0, 3)) + '</span>';
    return '<button type="button" class="tpl-card' + (i === PL.focus ? ' focus' : '') + '" data-ch="' + escAttr(c.Id) + '" data-i="' + i + '" title="' + escAttr(c.Name || '') + '">' +
      '<span class="tpl-logo">' + logo + (c.ChannelNumber ? '<b>' + esc(c.ChannelNumber) + '</b>' : '') + '</span>' +
      '<span class="tpl-name">' + esc(c.Name || '') + '</span>' +
      '<span class="tpl-now">' + esc(p ? p.Name : '') + '</span>' +
      (left ? '<span class="tpl-left">' + (left >= 60 ? Math.floor(left / 60) + ' h ' + (left % 60) + ' min left' : left + ' min left') + '</span>' : '') +
      (pct ? '<i class="tpl-bar" style="width:' + pct.toFixed(1) + '%"></i>' : '') +
    '</button>';
  }

  function openRecents() {
    if (!PL.band) {
      var b = PL.band = document.createElement('div');
      b.id = 'tentacleRecentBand';
      b.innerHTML = '<div class="tpl-head"><span class="tpl-title"></span>' +
        '<button type="button" class="tpl-close" title="Close (Esc)">×</button></div><div class="tpl-row"></div>';
      document.body.appendChild(b);
      b.addEventListener('click', function (e) {
        if (e.target.closest('.tpl-close')) { closeRecents(); return; }
        var card = e.target.closest('.tpl-card');
        if (card) { closeRecents(); switchChannel(card.dataset.ch); }
      });
    }
    PL.open = true;
    PL.focus = 0;
    document.body.classList.add('tentacle-recents-open');
    PL.band.style.display = '';
    if (PL.items) renderRecents();
    else PL.band.querySelector('.tpl-row').innerHTML = '<div class="tpl-wait">Loading…</div>';
    refreshRecents();
  }

  function refreshRecents() {
    readRecents().then(function (r) {
      PL.items = r;
      if (PL.open) renderRecents();
    }).catch(function () {
      if (PL.open && !PL.items) PL.band.querySelector('.tpl-row').innerHTML = '<div class="tpl-wait">Your channels couldn’t be read.</div>';
    });
  }

  function renderRecents() {
    var r = PL.items;
    PL.band.querySelector('.tpl-title').textContent = r.title;
    var row = PL.band.querySelector('.tpl-row');
    var x = row.scrollLeft;
    PL.focus = Math.min(PL.focus, Math.max(0, r.items.length - 1));
    row.innerHTML = r.items.length ? r.items.map(recentCard).join('') :
      '<div class="tpl-wait">No channels yet. Channels you watch show up here.</div>';
    row.scrollLeft = x;
    var f = row.querySelector('.tpl-card.focus');
    if (f) f.focus({ preventScroll: true });
  }

  function closeRecents() {
    PL.open = false;
    document.body.classList.remove('tentacle-recents-open');
    if (PL.band) PL.band.style.display = 'none';
  }

  function switchChannel(id) {
    rememberTune(id);
    playChannel(id);
  }

  function onPlayerKey(e) {
    if (MENU.el) return;
    if (PL.open) {
      var cards = PL.band.querySelectorAll('.tpl-card');
      var k = e.key;
      if (k === 'ArrowRight' || k === 'ArrowLeft') {
        PL.focus = Math.max(0, Math.min(cards.length - 1, PL.focus + (k === 'ArrowRight' ? 1 : -1)));
        if (cards[PL.focus]) cards[PL.focus].focus();
      } else if (k === 'Enter' && cards[PL.focus]) { cards[PL.focus].click(); }
      else if (k === 'Escape' || k === 'ArrowUp' || k === 'Backspace') closeRecents();
      else if (k === 'ArrowDown') { /* stays */ }
      else return;
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }
  document.addEventListener('keydown', onPlayerKey, true);

  // What this browser plays (Jellyfin keeps its player to itself; the
  // server's session for this browser says), asked every few seconds on
  // the player page.
  function askPlaying() {
    var a = api();
    if (!a || typeof a.deviceId !== 'function' || Date.now() - PL.asked < 3000) return;
    PL.asked = Date.now();
    apiGet('Sessions', { DeviceId: a.deviceId() }).then(function (list) {
      var item = null;
      (list || []).forEach(function (s) { if (s.NowPlayingItem) item = s.NowPlayingItem; });
      var was = PL.item && PL.item.Id;
      PL.item = item;
      if (item && item.Type === 'TvChannel') {
        if (normId(item.Id) !== normId(was)) {
          rememberTune(item.Id);
          PL.items = null;
          if (PL.open) refreshRecents();
          if (G.overlay) { G.lastId = normFind(item.Id) || item.Id; scrollToChannel(G.lastId); }
        }
        G.lastId = normFind(item.Id) || G.lastId;
        if (G.place) G.place.channelId = G.lastId;
      }
      osdButtons();
    }).catch(function () { });
  }

  var ICON_RECENT = '<svg viewBox="0 0 24 24"><path d="M13 3a9 9 0 00-9 9H1l3.89 3.89.07.14L9 12H6a7 7 0 112.05 4.95l-1.42 1.42A9 9 0 1013 3zm-1 5v5l4.25 2.52.77-1.28-3.52-2.09V8H12z"/></svg>';
  var ICON_GUIDE = '<svg viewBox="0 0 24 24"><path d="M3 4h18v2H3V4zm0 4h5v5H3V8zm7 0h11v2H10V8zm0 3h8v2h-8v-2zM3 15h5v5H3v-5zm7 0h11v2H10v-2zm0 3h8v2h-8v-2z"/></svg>';

  // The two buttons in Jellyfin's player bar, only while a channel plays.
  function osdButtons() {
    var bar = document.querySelector('.videoOsdBottom .buttons');
    var live = onPlayerPage() && PL.item && PL.item.Type === 'TvChannel';
    var mine = bar && bar.querySelector('.tg-osd');
    if (!live) { if (mine) mine.remove(); return; }
    if (!bar || mine) return;
    var wrap = document.createElement('div');
    wrap.className = 'tg-osd';
    wrap.innerHTML =
      '<button type="button" is="paper-icon-button-light" class="paper-icon-button-light autoSize tg-osd-btn" data-osd="recent" title="Recent channels">' + ICON_RECENT + '</button>' +
      '<button type="button" is="paper-icon-button-light" class="paper-icon-button-light autoSize tg-osd-btn" data-osd="guide" title="Guide">' + ICON_GUIDE + '</button>';
    var before = bar.querySelector('.btnUserRating') || bar.querySelector('.btnSubtitles');
    if (before) bar.insertBefore(wrap, before); else bar.appendChild(wrap);
    wrap.addEventListener('click', function (e) {
      var b = e.target.closest('[data-osd]');
      if (!b) return;
      e.preventDefault();
      e.stopPropagation();
      if (b.dataset.osd === 'recent') { if (PL.open) closeRecents(); else openRecents(); }
      else openOverlay();
    });
  }

  // ── The guide over the player ──────────────────────────────────────

  function openOverlay() {
    closeRecents();
    if (G.overlay) return;
    G.overlay = true;
    if (PL.item) G.lastId = normFind(PL.item.Id) || PL.item.Id;
    document.body.classList.add('tentacle-guide-over-player');
    G.active = false;
    activate(true);
  }

  function closeOverlay() {
    if (!G.overlay) return;
    deactivate();
    G.overlay = false;
    document.body.classList.remove('tentacle-guide-over-player');
    if (G.container) G.container.classList.remove('tg-overlay');
    document.body.style.removeProperty('--tgbox-x');
  }

  // A channel picked in the player's guide: the guide goes, the channel plays in the player.
  function closeOverlayFor(channelId) {
    if (!G.overlay) return;
    closeOverlay();
    rememberTune(channelId);
  }

  // Jellyfin's picture sits in the header's box while the guide is open.
  function placeBox() {
    if (!G.overlay || !G.container) return;
    var box = G.container.querySelector('.tg-box');
    if (!box) return;
    var r = box.getBoundingClientRect();
    var st = document.body.style;
    st.setProperty('--tgbox-x', r.left + 'px');
    st.setProperty('--tgbox-y', r.top + 'px');
    st.setProperty('--tgbox-w', r.width + 'px');
    st.setProperty('--tgbox-h', r.height + 'px');
  }

  function scrollToChannel(id) {
    var sc = G.container && G.container.querySelector('.tg-scroller');
    var i = G.rows.map(function (c) { return c.id; }).indexOf(id);
    if (!sc || i < 0) return;
    var m = G.m, view = sc.clientHeight - m.headH;
    sc.scrollTop = Math.max(0, i * m.rowH - Math.round((view - m.rowH) / 2));
    Object.keys(G.mounted).forEach(function (k) { G.mounted[k].classList.toggle('tg-last', k === id); });
    drawRows();
    var p = progAt(id, Date.now());
    G.cursor = { id: id, t: Date.now() };
    showInfo(id, p);
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
    if (msg && msg.MessageType === 'RefreshProgress' && msg.Data && msg.Data.ItemId === 'tentacle'
        && String(msg.Data.Tentacle || '').split(',').indexOf('status') >= 0) {
      if (relay) { try { relay.postMessage({ MessageType: 'TentacleStatus' }); } catch (x) { } }
      onMessage({ MessageType: 'TentacleStatus' });
    }
    if (msg && (/^(Series)?Timer(Created|Cancelled)$/.test(msg.MessageType || '') || (msg.MessageType === 'RefreshProgress'
        && msg.Data && msg.Data.ItemId === 'tentacle' && String(msg.Data.Tentacle || '').split(',').indexOf('recordings') >= 0))) {
      // a recording set or cancelled, here or in Tentacle: this tab, and the
      // browser's other tabs (Jellyfin tells only the newest one)
      if (relay) { try { relay.postMessage({ MessageType: 'TentacleRecordings' }); } catch (x) { } }
      onMessage({ MessageType: 'TentacleRecordings' });
      return;
    }
    if (!msg || (msg.MessageType !== 'UserDataChanged' && msg.MessageType !== 'LibraryChanged')) return;
    // the player's recent channels, open: a channel played (or starred) on another device shows
    if (PL.open && msg.MessageType === 'UserDataChanged' && msg.Data && normId(msg.Data.UserId) === normId(api().getCurrentUserId())) {
      clearTimeout(PL.reread);
      PL.reread = setTimeout(refreshRecents, 500);
    }
    var copy = { MessageType: msg.MessageType, Data: msg.MessageType === 'UserDataChanged' ? msg.Data : null };
    if (relay) { try { relay.postMessage(copy); } catch (x) { } }
    onMessage(copy);
  }

  function onMessage(msg) {
    if (!G.active || !msg) return;
    if (msg.MessageType === 'TentacleRecordings') { setTimeout(loadMarks, 500); return; }
    if (msg.MessageType === 'TentacleStatus') { loadNotice().then(showNotice); return; }
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
    // a first page load opens the guide before Jellyfin signed in: ask about previews once it has
    if (PV.poll && !PV.asked) pollPreview();
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
    if (document.hidden) stopPreview();
    // the screen lock is let go while hidden: multi-view takes it again
    if (!document.hidden && MV.view && MV.view.style.display !== 'none' && MV.session) keepAwake(true);
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
        if (p) return p.play(opts);
        // not ready to play: the item's page, as these pages did without a player
        var ids = (opts && opts.ids) || [];
        if (ids.length) navigateToPlay(ids[0]);
        return Promise.resolve();
      }
    };
  }

  function playChannel(channelId) {
    G.lastId = channelId;
    rememberTune(channelId);
    PL.item = { Id: channelId, Type: 'TvChannel' };   // the player's buttons at once; the session confirms
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
      // live TV plays through hls.js (a blob: source); a theme video doesn't
      if (v.paused || v.ended || v === document.pictureInPictureElement || !/^blob:/.test(v.currentSrc || '')) {
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
      askPlaying();
      osdButtons();
    } else if (wasOnPlayerPage) {
      leftPlayerAt = Date.now();
      PL.item = null;
      closeRecents();
    }
    wasOnPlayerPage = on;
    // a multi-view channel in Jellyfin's player: Back (or a player that never
    // opened) brings the grid back, whatever route events Jellyfin sent
    if (MV.session && MV.fullScreen) {
      if (on) MV.sawPlayer = true;
      else if (isGuidePage() && (MV.sawPlayer || Date.now() - MV.fullAt > 8000)) openGrid();
    }
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
    if (G.overlay) {
      if (onPlayerPage()) return;
      closeOverlay();
    }
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
    openOverlay: openOverlay,
    internals: { apiPost: apiPost, daysWithData: daysWithData, mergeRecents: mergeRecents, programmeChoices: programmeChoices,
      LivePicture: LivePicture, MV: MV, PV: PV, PL: PL, diffChannels: diffChannels, onUserData: onUserData, G: G, computeDays: computeDays, isGuidePage: isGuidePage, progsHtml: progsHtml, chanCell: chanCell, fmtRange: fmtRange, keepPlace: keepPlace, restorePlace: restorePlace },
  };

  // Initial check
  if (isGuidePage()) {
    setTimeout(activate, 100);
  }
})();
