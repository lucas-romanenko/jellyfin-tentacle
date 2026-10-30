// Tentacle Recordings — Jellyfin Web's Live TV "Recordings" and "Schedule" pages
//
// The Tentacle guide's Recordings (#/livetv?tab=3) and Schedule (#/livetv?tab=4)
// buttons open these instead of Jellyfin's own tabs, styled like the guide:
//   - Schedule: what will record, soonest first, by day: Tentacle's teams and
//     rules (why, which channel, what waits and why) and what people set with
//     Record in Jellyfin; what teams and rules in preview would record, folded.
//   - Recordings: what Jellyfin has recorded, newest first (a tap opens it to
//     play), and what is recording now.
//   - A click plays (Resume where it was left). A recording still recording
//     asks first (as the TV, #239): Watch live (its channel, on the stream the
//     recording already reads: no new connection), Resume from … (when a
//     position is saved), Play from start. Right-click / a long press (as the
//     TV's long press): Resume, Play from start, Mark watched / unwatched,
//     Delete recording… (when Jellyfin allows it, confirmed), Details;
//     recording now: the same three, Mark watched / unwatched, Details.
//   - Play from start plays the part already recorded from 0:00 in Jellyfin's
//     player: seeking stays inside what's written, the bar grows, the end lands
//     just behind it and keeps playing, and Jump to live in the player bar
//     switches to the channel. Leaving saves the position and doesn't mark it
//     watched (see "A recording still recording" below).
// Read-only: teams and rules are picked in Tentacle (Recordings page). Data:
// TentacleDiscover/Recordings/* (the plugin passes the caller's own token on to
// Tentacle) and Jellyfin's LiveTv/Recordings and LiveTv/Timers.
// Live: Jellyfin's TimerCreated/TimerCancelled/SeriesTimer* messages and
// Tentacle's "recordings" push re-read the page; else every 30 s while open.
(function () {
  'use strict';

  var POLL_MS = 30000;
  var R = { active: false, tab: 0, container: null, poll: null, socket: null, seq: 0, items: {} };

  function esc(str) {
    if (str === null || str === undefined) return '';
    var d = document.createElement('div');
    d.textContent = String(str);
    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function api() { return window.ApiClient; }
  function apiGet(path, params) { return api().getJSON(api().getUrl(path, params)); }

  function routeTab() {
    var h = (location.hash || '').replace('#', '').replace(/^\//, '');
    var path = h.split('?')[0];
    if (path !== 'livetv' && path !== 'livetv.html') return 0;
    var m = h.match(/[?&]tab=(\d+)/);
    var t = m ? parseInt(m[1], 10) : 0;
    return t === 3 || t === 4 ? t : 0;
  }

  function fmtTime(t) { return new Date(t).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
  function dayOf(t) {
    var today = new Date(); today.setHours(0, 0, 0, 0);
    var d = new Date(t); d.setHours(0, 0, 0, 0);
    var n = Math.round((d - today) / 86400000);
    if (n === 0) return 'Today';
    if (n === 1) return 'Tomorrow';
    if (n === -1) return 'Yesterday';
    return new Date(t).toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
  }

  // ── Activate ─────────────────────────────────────────────────────────

  function onRoute() {
    var t = routeTab();
    if (!t) { deactivate(); return; }
    activate(t);
  }

  function activate(tab) {
    if (window.TentacleStandAside && window.TentacleStandAside()) return; // TV layout: Jellyfin's pages
    ensureContainer();
    var first = !R.active;
    R.active = true;
    R.tab = tab;
    document.body.classList.add('tentacle-recordings-active');
    R.container.style.display = 'flex';
    if (first || !R.container.firstChild) renderShell();
    else markTab();
    load();
    if (!R.poll) R.poll = setInterval(function () { watchSocket(); if (!document.hidden) load(); }, POLL_MS);
    watchSocket();
  }

  function deactivate() {
    if (!R.active) return;
    R.active = false;
    document.body.classList.remove('tentacle-recordings-active');
    if (R.container) R.container.style.display = 'none';
    if (R.poll) { clearInterval(R.poll); R.poll = null; }
  }

  function ensureContainer() {
    if (R.container) return;
    var el = document.createElement('div');
    el.id = 'tentacleRecordingsContainer';
    document.body.appendChild(el);
    el.addEventListener('click', function (e) {
      var a = e.target.closest('[data-item]');
      if (!a) return;
      var item = R.items[a.getAttribute('data-item')];
      if (item && R.tab === 3) {
        e.preventDefault();
        if (recording(item)) recordingMenu(item, { el: a, short: true });
        else play(item, false);
        return;
      }
      location.hash = '#/details?id=' + encodeURIComponent(a.getAttribute('data-item'));
    });
    if (window.TentacleMenu) {
      window.TentacleMenu.onGesture(el, '.tr-card[data-item]', function (card, x, y) {
        var item = R.items[card.getAttribute('data-item')];
        if (item) recordingMenu(item, { x: x, y: y, el: card });
      });
    }
    R.container = el;
  }

  var ICON_BACK = '<svg viewBox="0 0 24 24"><path d="M15.4 7.4L14 6l-6 6 6 6 1.4-1.4L10.8 12z"/></svg>';

  function renderShell() {
    R.container.innerHTML =
      '<div class="tr-top">' +
        '<a class="tr-back" href="#/livetv" title="Guide">' + ICON_BACK + '<span>Guide</span></a>' +
        '<div class="tr-title"></div>' +
        '<div class="tr-toggle">' +
          '<a class="tr-seg" data-tab="4" href="#/livetv?tab=4">Schedule</a>' +
          '<a class="tr-seg" data-tab="3" href="#/livetv?tab=3">Recordings</a>' +
        '</div>' +
      '</div>' +
      '<div class="tr-body"><div class="tr-none">Loading…</div></div>';
    markTab();
  }

  function markTab() {
    R.container.querySelector('.tr-title').textContent = R.tab === 4 ? 'Schedule' : 'Recordings';
    Array.prototype.forEach.call(R.container.querySelectorAll('.tr-seg'), function (s) {
      s.classList.toggle('active', s.getAttribute('data-tab') === String(R.tab));
    });
  }

  // ── Data ─────────────────────────────────────────────────────────────

  function load() {
    var seq = ++R.seq, tab = R.tab;
    var uid = api() && api().getCurrentUserId();
    if (!uid) return;
    var p = tab === 4
      ? Promise.all([
        apiGet('TentacleDiscover/Recordings/upcoming', { userId: uid }).catch(function () { return null; }),
        apiGet('TentacleDiscover/Recordings/rules', { userId: uid }).catch(function () { return null; }),
        apiGet('LiveTv/Timers', { IsActive: false }).catch(function () { return null; }),
      ]).then(function (r) { return renderSchedule(r[0], r[1], r[2]); })
      : apiGet('LiveTv/Recordings', { UserId: uid, Fields: 'Overview,DateCreated,PremiereDate,CanDelete', EnableUserData: true, EnableImageTypes: 'Primary,Thumb', ImageTypeLimit: 1, Limit: 300 })
        .then(function (r) {
          R.items = {};
          ((r && r.Items) || []).forEach(function (i) { R.items[i.Id] = i; });
          return renderRecordings(r);
        }, function () { return '<div class="tr-none">Recordings couldn’t be read from Jellyfin.</div>'; });
    p.then(function (html) {
      if (seq !== R.seq || !R.active || R.tab !== tab) return;
      var body = R.container.querySelector('.tr-body');
      var y = body.scrollTop;
      body.innerHTML = html;
      body.scrollTop = y;
    });
  }

  // Tentacle's list (its rules and Jellyfin's own timers); Jellyfin's timers alone
  // when Tentacle can't be asked (an older server).
  function renderSchedule(up, rules, timers) {
    var items = [];
    if (up) {
      (up.rules || []).forEach(function (b) { items.push(b); });
      (up.jellyfin || []).forEach(function (t) { items.push(Object.assign({ jf: true }, t)); });
    } else {
      ((timers && timers.Items) || []).forEach(function (t) {
        if (t.Status !== 'New' && t.Status !== 'InProgress') return;
        items.push({ jf: true, title: t.Name, channel: t.ChannelName, start: t.StartDate, stop: t.EndDate,
          state: t.Status === 'InProgress' ? 'recording' : 'booked', series: !!t.SeriesTimerId });
      });
    }
    items.sort(function (a, b) { return Date.parse(a.start) - Date.parse(b.start); });
    var real = items.filter(function (i) { return i.state !== 'preview'; });
    var preview = items.filter(function (i) { return i.state === 'preview'; });
    var html = '';
    html += real.length ? '<div class="tr-list">' + byDay(real) + '</div>'
      : '<div class="tr-none">Nothing set to record.' + (preview.length ? ' Teams and rules below are in preview.' : '') + '</div>';
    if (preview.length) {
      html += '<details class="tr-list tr-preview"><summary>In preview: would record ' + preview.length +
        ' <span>nothing is booked for these</span></summary>' + byDay(preview) + '</details>';
    }
    var rl = (rules && rules.rules) || [];
    if (rl.length) {
      html += '<div class="tr-rules"><div class="tr-h">Teams and rules</div><div class="tr-chips">' + rl.map(function (r) {
        return '<span class="tr-chip' + (r.mode === 'record' ? ' on' : '') + '">' + esc(r.team_name || r.name) +
          '<small>' + (r.mode === 'record' ? 'records' : 'preview') + '</small></span>';
      }).join('') + '</div></div>';
    }
    html += '<p class="tr-foot">Follow teams and set rules in Tentacle, on its Recordings page.</p>';
    return html;
  }

  var PILL = {
    booked: ['Will record', 'ok'], recording: ['Recording', 'rec'], waiting: ['Waiting', 'warn'],
    covered: ['Set in Jellyfin', ''], failed: ['Not booked', 'warn'], lost: ['Booking again', 'warn'], preview: ['', ''],
  };

  function byDay(items) {
    var day = '', out = '';
    items.forEach(function (it) {
      var d = dayOf(Date.parse(it.start));
      if (d !== day) { day = d; out += '<div class="tr-day">' + esc(d) + '</div>'; }
      var meta = [];
      meta.push(it.from_event ? 'Event channel' : (it.channel || ''));
      if (it.jf) meta.push(it.series ? 'Series, set in Jellyfin' : 'Set in Jellyfin');
      else if (it.rule) meta.push(it.rule);
      if (it.state === 'waiting' || it.state === 'failed') meta.push('<em>' + esc(it.reason || '') + '</em>');
      var pill = it.jf ? (it.state === 'recording' ? PILL.recording : ['', '']) : (PILL[it.state] || ['', '']);
      out += '<div class="tr-row">' +
        '<span class="tr-time">' + esc(fmtTime(Date.parse(it.start)) + ' – ' + fmtTime(Date.parse(it.stop))) + '</span>' +
        '<span class="tr-what"><b>' + esc(it.title || '') + '</b><span class="tr-meta">' +
          meta.map(function (m) { return m.indexOf('<em>') === 0 ? m : '<span>' + esc(m) + '</span>'; }).join('') + '</span></span>' +
        (pill[0] ? '<span class="tr-pill ' + pill[1] + '">' + esc(pill[0]) + '</span>' : '') +
      '</div>';
    });
    return out;
  }

  function renderRecordings(r) {
    var items = (r && r.Items) || [];
    if (!items.length) return '<div class="tr-none">No recordings yet. Record from the guide, or follow a team in Tentacle.</div>';
    // finished recordings have no StartDate: DateCreated (asked for in Fields) is when it recorded; no date → no time
    var whenOf = function (i) { return Date.parse(i.StartDate || i.DateCreated || i.PremiereDate) || 0; };
    items.sort(function (a, b) { return whenOf(b) - whenOf(a); });
    var now = items.filter(function (i) { return i.Status === 'InProgress'; });
    var done = items.filter(function (i) { return i.Status !== 'InProgress'; });
    var card = function (i) {
      var tag = i.ImageTags && (i.ImageTags.Thumb || i.ImageTags.Primary);
      var img = tag ? api().getUrl('Items/' + i.Id + '/Images/' + (i.ImageTags.Thumb ? 'Thumb' : 'Primary'), { tag: tag, maxWidth: 360 }) : '';
      var when = whenOf(i);
      var name = i.EpisodeTitle && i.Name ? i.Name + ': ' + i.EpisodeTitle : (i.Name || '');
      return '<button class="tr-card" data-item="' + esc(i.Id) + '" title="' + esc(name) + '">' +
        '<span class="tr-img"' + (img ? ' style="background-image:url(\'' + esc(img) + '\')"' : '') + '>' +
          (i.Status === 'InProgress' ? '<span class="tr-pill rec">Recording</span>' : '') + '</span>' +
        '<span class="tr-cname">' + esc(name) + '</span>' +
        '<span class="tr-cmeta">' + esc((when ? dayOf(when) + ' ' + fmtTime(when) : '') + (i.ChannelName ? ' · ' + i.ChannelName : '')) + '</span>' +
      '</button>';
    };
    var html = '';
    if (now.length) html += '<div class="tr-h">Recording now</div><div class="tr-grid">' + now.map(card).join('') + '</div>';
    html += '<div class="tr-h">Recorded</div><div class="tr-grid">' + done.map(card).join('') + '</div>';
    return html;
  }

  // ── Play, and the menu ───────────────────────────────────────────────

  function recording(item) { return item.Status === 'InProgress'; }

  function position(item) { return (item.UserData && item.UserData.PlaybackPositionTicks) || 0; }

  // A finished recording plays where it was left (`fromStart`: from 0:00).
  function play(item, fromStart) {
    var pm = window.playbackManager;
    if (!pm || typeof pm.play !== 'function') { location.hash = '#/details?id=' + encodeURIComponent(item.Id); return; }
    var opts = { ids: [item.Id] };
    if (!fromStart && position(item)) opts.startPositionTicks = position(item);
    pm.play(opts);
  }

  // A recording still recording: its channel live. Jellyfin joins the stream
  // the recording reads. Its channel id from the item, else from its timer.
  function watchLive(item) {
    channelOf(item).then(function (id) {
      if (id && window.TentacleLiveTV) window.TentacleLiveTV.playChannel(id);
      else window.TentacleMenu.toast('The channel couldn’t be found.');
    });
  }

  function channelOf(item) {
    if (item.ChannelId) return Promise.resolve(item.ChannelId);
    if (!item.TimerId) return Promise.resolve(null);
    return apiGet('LiveTv/Timers/' + item.TimerId).then(function (t) { return (t && t.ChannelId) || null; }, function () { return null; });
  }

  // "1:28", "1:02:05"
  function clock(ticks) {
    var t = Math.floor(ticks / 1e7), h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, sec = t % 60;
    var two = function (n) { return (n < 10 ? '0' : '') + n; };
    return h ? h + ':' + two(m) + ':' + two(sec) : m + ':' + two(sec);
  }

  // What a recording still recording offers: Watch live, Resume from …
  // (a saved position), Play from start, as the TV.
  function recordingChoices(item) {
    var pos = position(item);
    return [
      { key: 'live', label: 'Watch live' },
      pos ? { key: 'resume', label: 'Resume from ' + clock(pos) } : null,
      { key: 'start', label: 'Play from start' },
    ].filter(Boolean);
  }

  function setWatched(item, watched) {
    var a = api();
    Promise.resolve(a.ajax({ type: watched ? 'POST' : 'DELETE', url: a.getUrl('UserPlayedItems/' + item.Id, { userId: a.getCurrentUserId() }) }))
      .then(function () { setTimeout(load, 300); }, function () { window.TentacleMenu.toast('That didn’t work. Try again in a moment.'); });
  }

  function remove(item) {
    var a = api();
    Promise.resolve(a.ajax({ type: 'DELETE', url: a.getUrl('Items/' + item.Id) }))
      .then(function () { window.TentacleMenu.toast('Recording deleted'); setTimeout(load, 300); },
        function () { window.TentacleMenu.toast('The recording couldn’t be deleted.'); });
  }

  function recordingMenu(item, at) {
    var M = window.TentacleMenu;
    var name = item.EpisodeTitle && item.Name ? item.Name + ': ' + item.EpisodeTitle : (item.Name || '');
    var details = { label: 'Details', run: function () { location.hash = '#/details?id=' + encodeURIComponent(item.Id); } };
    var items;
    if (recording(item)) {
      items = recordingChoices(item).map(function (c) {
        return { label: c.label, run: function () {
          if (c.key === 'live') watchLive(item);
          else Growing.play(item, c.key === 'resume' ? position(item) : 0);
        } };
      });
      // a click asks only how to play; right-click / a long press has the rest
      if (!at.short) {
        var watched = !!(item.UserData && item.UserData.Played);
        items.push({ label: watched ? 'Mark unwatched' : 'Mark watched', run: function () { setWatched(item, !watched); } }, details);
      }
    } else {
      var played = !!(item.UserData && item.UserData.Played);
      items = [
        position(item) ? { label: 'Resume', run: function () { play(item, false); } } : null,
        { label: position(item) ? 'Play from start' : 'Play', run: function () { play(item, true); } },
        { label: played ? 'Mark unwatched' : 'Mark watched', run: function () { setWatched(item, !played); } },
        item.CanDelete ? { label: 'Delete recording…', run: function () {
          M.confirm({ message: name + ' is deleted for everyone in the household. This can’t be undone.',
            confirm: 'Delete recording', cancel: 'Keep it', danger: true, anchor: at.el, run: function () { remove(item); } });
        } } : null,
        details,
      ];
    }
    M.open({ title: name, items: items, x: at.x, y: at.y, anchor: at.el });
  }

  // ── A recording still recording, from its start (#239) ──────────────
  //
  // While it records, Jellyfin swaps the item's own media source for one of
  // its own (another id, infinite, no runtime) offered only as HLS: an EVENT
  // playlist that ffmpeg remuxes from 0:00 of the growing file and keeps
  // appending to. Jellyfin Web plays it once asked without a media source id
  // (it never names one); a second reader of the file, no new provider
  // connection. What differs from a finished recording:
  // - Start at 1 ms: hls.js takes no start for "play at the live edge".
  // - Resume: from 1 ms too, then a seek once the remux has reached the
  //   position (a start position would make ffmpeg skip the part before it).
  // - Seeks land at most EDGE_MS before the end of what's written, so the end
  //   plays the latest part instead of running dry.
  // - Jellyfin treats a position on an item without a runtime as "watched to
  //   the end": the first progress report marked it watched at 0:00 (10.11.8).
  //   Its reports leave the position out, the stop is reported failed (Jellyfin
  //   then leaves the user data alone) and the position is saved on its own.
  //   A recording that finished while watched and played to its end stops as
  //   any other (watched).
  // - Jump to live in the player bar: the channel, on the recording's stream.
  var EDGE_MS = 10000, RESUME_WAIT_MS = 45000, START_TICKS = 10000, NOT_STARTED_MS = 60000;
  var GR = { id: null, item: null, resume: 0, since: 0, ended: false, video: null, written: 0, grewAt: 0, asked: 0 };

  function normId(id) { return String(id || '').replace(/-/g, '').toLowerCase(); }

  // Where a seek lands: never past what's written less EDGE_MS (0 or less written: as asked).
  function growingSeekTarget(ms, writtenMs) {
    if (ms <= 0) return 0;
    if (writtenMs <= 0) return ms;
    return Math.min(ms, Math.max(0, writtenMs - EDGE_MS));
  }

  // Whether a resume can seek now: the remux has reached it, or it waited long enough.
  function growingResumeReady(targetMs, writtenMs, waitedMs) {
    return writtenMs >= targetMs + EDGE_MS || waitedMs >= RESUME_WAIT_MS;
  }

  // The position to save on stop: under a minute in is "not started", as Jellyfin's own threshold.
  function growingStopTicks(ticks) { return ticks >= NOT_STARTED_MS * 1e4 ? ticks : 0; }

  // A playback report about the recording, as it goes to Jellyfin; `stop` is
  // filled in for a stop whose position must be saved on its own.
  function growingReport(method, info) {
    if (!GR.id || !info || normId(info.ItemId) !== GR.id) return info;
    var out = Object.assign({}, info);
    var ticks = out.PositionTicks;
    delete out.PositionTicks;
    if (method === 'reportPlaybackStopped') {
      if (GR.ended) return info;
      out.Failed = true;
      out.__save = growingStopTicks(ticks || 0);
    }
    return out;
  }

  function patchReports() {
    var a = api();
    if (!a || a.__tgGrowing) return;
    a.__tgGrowing = true;
    ['reportPlaybackStart', 'reportPlaybackProgress', 'reportPlaybackStopped'].forEach(function (m) {
      var orig = a[m];
      if (typeof orig !== 'function') return;
      a[m] = function (info) {
        var mine = !!(GR.id && info && normId(info.ItemId) === GR.id);
        if (GR.id && !mine && m === 'reportPlaybackStart') stopGrowing();
        if (!mine) return orig.call(this, info);
        var out = growingReport(m, info), id = GR.id, ended = GR.ended;
        if (m === 'reportPlaybackStopped') stopGrowing();
        if (out === info) {
          var done = orig.call(this, info);
          // finished while watched, to its end: watched (Jellyfin may not know its runtime yet)
          if (m === 'reportPlaybackStopped' && ended) {
            Promise.resolve(done).catch(function () { }).then(function () {
              return a.ajax({ type: 'POST', url: a.getUrl('UserPlayedItems/' + id, { userId: a.getCurrentUserId() }) });
            }).then(function () { if (R.active) setTimeout(load, 300); }, function () { });
          }
          return done;
        }
        var save = out.__save;
        delete out.__save;
        var sent = orig.call(this, out);
        if (m === 'reportPlaybackStopped') {
          Promise.resolve(sent).catch(function () { }).then(function () {
            return a.ajax({ type: 'POST', url: a.getUrl('UserItems/' + id + '/UserData', { userId: a.getCurrentUserId() }),
              data: JSON.stringify({ PlaybackPositionTicks: save }), contentType: 'application/json' });
          }).then(function () { if (R.active) setTimeout(load, 300); }, function () { });
        }
        return sent;
      };
    });
  }

  function stopGrowing() {
    GR.id = null; GR.item = null; GR.resume = 0; GR.ended = false; GR.written = 0; GR.asked = 0;
    if (GR.video) { GR.video.removeEventListener('seeking', onSeeking); GR.video.removeEventListener('ended', onEnded); }
    GR.video = null;
    var b = document.querySelector('.tr-live-btn');
    if (b) b.remove();
  }

  function writtenMs(v) { return v && isFinite(v.duration) ? v.duration * 1000 : 0; }

  function onSeeking(e) {
    var v = e.target, to = growingSeekTarget(v.currentTime * 1000, writtenMs(v));
    if (to < v.currentTime * 1000 - 1) v.currentTime = to / 1000;
  }
  function onEnded() { GR.ended = true; }

  // Playing at the end of what's written and it stopped growing: when the
  // recording has finished, it ends as any recording watched to its end (the
  // player is stopped: the cut-off last frame of a finished recording made
  // hls.js recover and replay the last seconds over and over).
  function checkFinished(v) {
    var w = writtenMs(v), now = Date.now();
    if (w > GR.written) { GR.written = w; GR.grewAt = now; return; }
    if (GR.ended || v.currentTime * 1000 < w - EDGE_MS - 2000 || now - GR.grewAt < 8000 || now - GR.asked < 10000) return;
    GR.asked = now;
    var id = GR.id;
    apiGet('Users/' + api().getCurrentUserId() + '/Items/' + GR.item.Id).then(function (i) {
      if (GR.id !== id || !i || recording(i)) return;
      GR.ended = true;
      var a = api(), ev = window.Events;
      try { if (ev && a) ev.trigger(a, 'message', [{ MessageType: 'Playstate', Data: { Command: 'Stop' } }]); } catch (e) { }
    }, function () { });
  }

  // Every half second while it plays: the player's video, a pending resume, the button.
  function growingTick() {
    if (!GR.id) return;
    if (!/^#\/video\b/.test(location.hash)) {
      if (Date.now() - GR.since > 60000) stopGrowing(); // never got to the player
      return;
    }
    var v = document.querySelector('video.htmlvideoplayer') || document.querySelector('.videoPlayerContainer video');
    if (v && v !== GR.video) {
      if (GR.video) { GR.video.removeEventListener('seeking', onSeeking); GR.video.removeEventListener('ended', onEnded); }
      GR.video = v;
      v.addEventListener('seeking', onSeeking);
      v.addEventListener('ended', onEnded);
    }
    if (v && GR.resume && v.readyState >= 2) {
      var target = GR.resume / 1e4;
      if (growingResumeReady(target, writtenMs(v), Date.now() - GR.since)) {
        GR.resume = 0;
        v.currentTime = growingSeekTarget(target, writtenMs(v)) / 1000;
      }
    }
    if (v) checkFinished(v);
    liveButton();
  }

  var ICON_LIVE = '<svg viewBox="0 0 24 24"><path d="M21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h5v2h8v-2h5c1.1 0 1.99-.9 1.99-2L23 5c0-1.1-.9-2-2-2zm0 14H3V5h18v12zm-5-6l-7 4V7z"/></svg>';

  // Jump to live, in Jellyfin's player bar right after ⏩ (as the TV).
  function liveButton() {
    if (document.querySelector('.tr-live-btn')) return;
    var bar = document.querySelector('.videoOsdBottom .buttons');
    if (!bar) return;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'paper-icon-button-light tr-live-btn';
    b.title = 'Jump to live';
    b.setAttribute('aria-label', 'Jump to live');
    b.innerHTML = ICON_LIVE + '<span>Jump to live</span>';
    var ff = bar.querySelector('.btnFastForward');
    if (ff && ff.parentNode) ff.parentNode.insertBefore(b, ff.nextSibling); else bar.insertBefore(b, bar.firstChild);
    b.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (GR.item) watchLive(GR.item);
    });
  }

  var Growing = {
    play: function (item, resumeTicks) {
      var pm = window.playbackManager;
      if (!pm || typeof pm.play !== 'function') { location.hash = '#/details?id=' + encodeURIComponent(item.Id); return; }
      patchReports();
      stopGrowing();
      GR.id = normId(item.Id); GR.item = item; GR.resume = resumeTicks || 0; GR.since = Date.now();
      pm.play({ ids: [item.Id], startPositionTicks: START_TICKS });
    },
  };
  setInterval(growingTick, 500);

  // ── Live ─────────────────────────────────────────────────────────────

  function watchSocket() {
    var ws = api() && api()._webSocket;
    if (!ws || ws === R.socket || typeof ws.addEventListener !== 'function') return;
    R.socket = ws;
    ws.addEventListener('message', function (e) {
      if (e.target !== R.socket) return;
      var msg;
      try { msg = JSON.parse(e.data); } catch (x) { return; }
      var t = msg && msg.MessageType;
      var tentacle = t === 'RefreshProgress' && msg.Data && msg.Data.ItemId === 'tentacle' &&
        String(msg.Data.Tentacle || '').split(',').indexOf('recordings') >= 0;
      if (tentacle || /^(Series)?Timer(Created|Cancelled)$/.test(t || '')) {
        if (R.active) setTimeout(load, 500);
        // Jellyfin tells only the browser's newest tab: the guide in another tab hears it here
        if (relay) { try { relay.postMessage({ MessageType: 'TentacleRecordings' }); } catch (x) { } }
      }
    });
  }

  // The guide's channel between tabs (tentacle-livetv.js): its recordings news reaches this page too.
  var relay = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('tentacle-livetv') : null;
  if (relay) {
    relay.onmessage = function (e) { if (R.active && e.data && e.data.MessageType === 'TentacleRecordings') setTimeout(load, 500); };
    if (relay.unref) relay.unref();
  }

  // Listen in every tab, whatever page it shows: Jellyfin sends to the newest tab only, and
  // this passes recordings news on to a guide or Schedule open in another tab.
  setInterval(watchSocket, 5000);

  ['hashchange', 'popstate'].forEach(function (ev) { window.addEventListener(ev, function () { setTimeout(onRoute, 30); }); });
  document.addEventListener('viewshow', function () { setTimeout(onRoute, 30); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', onRoute);
  else onRoute();

  window.TentacleRecordings = { isActive: function () { return R.active; }, internals: { renderSchedule: renderSchedule, renderRecordings: renderRecordings, routeTab: routeTab, recordingChoices: recordingChoices,
    growingSeekTarget: growingSeekTarget, growingResumeReady: growingResumeReady, growingStopTicks: growingStopTicks, growingReport: growingReport, GR: GR } };
})();
