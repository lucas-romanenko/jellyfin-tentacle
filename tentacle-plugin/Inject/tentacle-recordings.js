// Tentacle Recordings — Jellyfin Web's Live TV "Recordings" and "Schedule" pages
//
// The Tentacle guide's Recordings (#/livetv?tab=3) and Schedule (#/livetv?tab=4)
// buttons open these instead of Jellyfin's own tabs, styled like the guide:
//   - Schedule: what will record, soonest first, by day: Tentacle's teams and
//     rules (why, which channel, what waits and why) and what people set with
//     Record in Jellyfin; what teams and rules in preview would record, folded.
//   - Recordings: what Jellyfin has recorded, newest first (a tap opens it to
//     play), and what is recording now.
// Read-only: teams and rules are picked in Tentacle (Recordings page). Data:
// TentacleDiscover/Recordings/* (the plugin passes the caller's own token on to
// Tentacle) and Jellyfin's LiveTv/Recordings and LiveTv/Timers.
// Live: Jellyfin's TimerCreated/TimerCancelled/SeriesTimer* messages and
// Tentacle's "recordings" push re-read the page; else every 30 s while open.
(function () {
  'use strict';

  var POLL_MS = 30000;
  var R = { active: false, tab: 0, container: null, poll: null, socket: null, seq: 0 };

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
    if (!t || (window.TentacleStandAside && window.TentacleStandAside())) { deactivate(); return; }
    activate(t);
  }

  function activate(tab) {
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
      if (a) { location.hash = '#/details?id=' + encodeURIComponent(a.getAttribute('data-item')); }
    });
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
      : apiGet('LiveTv/Recordings', { UserId: uid, Fields: 'Overview', EnableImageTypes: 'Primary,Thumb', ImageTypeLimit: 1, Limit: 300 })
        .then(renderRecordings, function () { return '<div class="tr-none">Recordings couldn’t be read from Jellyfin.</div>'; });
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
    items.sort(function (a, b) { return Date.parse(b.DateCreated || b.StartDate || 0) - Date.parse(a.DateCreated || a.StartDate || 0); });
    var now = items.filter(function (i) { return i.Status === 'InProgress'; });
    var done = items.filter(function (i) { return i.Status !== 'InProgress'; });
    var card = function (i) {
      var tag = i.ImageTags && (i.ImageTags.Thumb || i.ImageTags.Primary);
      var img = tag ? api().getUrl('Items/' + i.Id + '/Images/' + (i.ImageTags.Thumb ? 'Thumb' : 'Primary'), { tag: tag, maxWidth: 360 }) : '';
      var when = Date.parse(i.StartDate || i.DateCreated || 0);
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

  // ── Live ─────────────────────────────────────────────────────────────

  function watchSocket() {
    var ws = api() && api()._webSocket;
    if (!ws || ws === R.socket || typeof ws.addEventListener !== 'function') return;
    R.socket = ws;
    ws.addEventListener('message', function (e) {
      if (!R.active || e.target !== R.socket) return;
      var msg;
      try { msg = JSON.parse(e.data); } catch (x) { return; }
      var t = msg && msg.MessageType;
      var tentacle = t === 'RefreshProgress' && msg.Data && msg.Data.ItemId === 'tentacle' &&
        String(msg.Data.Tentacle || '').split(',').indexOf('recordings') >= 0;
      if (tentacle || /^(Series)?Timer(Created|Cancelled)$/.test(t || '')) setTimeout(load, 500);
    });
  }

  ['hashchange', 'popstate'].forEach(function (ev) { window.addEventListener(ev, function () { setTimeout(onRoute, 30); }); });
  document.addEventListener('viewshow', function () { setTimeout(onRoute, 30); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', onRoute);
  else onRoute();

  window.TentacleRecordings = { isActive: function () { return R.active; }, internals: { renderSchedule: renderSchedule, renderRecordings: renderRecordings, routeTab: routeTab } };
})();
