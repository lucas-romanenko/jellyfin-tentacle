// Tentacle - subtitles and skipping in Jellyfin Web's player (Nov 2026).
//
// - The person's Skip intro / Skip credits choice (Tentacle → their
//   Subtitles & skipping page) becomes Jellyfin Web's own setting on this
//   device: its "media segment actions" are kept per device, so each device
//   is told (localStorage "<user>-segmentTypeAction__Intro|Outro": AskToSkip,
//   Skip or None), at start and whenever Tentacle says they changed.
// - The subtitles menu shows plain names ("English", "English SDH"): the
//   plugin already cut it to the person's languages; Jellyfin adds the format.
// - Under it: "Subtitles wrong? Get another" (the next one from the subtitle
//   site, lined up with the sound), for the language that is on.
// - After picking a subtitle that Tentacle is still reading out of a
//   provider title, a note says so and how long; the subtitle shows up by
//   itself when it is ready (the player's request waits for it).
(function () {
  'use strict';

  var ACTION = { button: 'AskToSkip', auto: 'Skip', off: 'None' };
  var FORMATS = /\s+-\s+(SUBRIP|SRT|ASS|SSA|WEBVTT|VTT|MOV_TEXT|TX3G|TEXT)$/i;
  var S = { prefs: null, item: null, asked: 0, note: null, poll: null, busy: false };

  function api() { return window.ApiClient; }
  function signedIn() {
    var a = api();
    try { return !!(a && a.getCurrentUserId && a.getCurrentUserId() && a.accessToken && a.accessToken()); } catch (e) { return false; }
  }
  function call(method, path, body) {
    var a = api();
    return fetch(a.getUrl(path), {
      method: method,
      headers: { 'Authorization': 'MediaBrowser Token="' + a.accessToken() + '"', 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) { var e = new Error(j.detail || r.status); e.status = r.status; throw e; } return j; });
    });
  }

  // ── Skip intro / credits: the person's choice on this device ──────────
  function applySkip(p) {
    var uid = api().getCurrentUserId();
    try {
      localStorage.setItem(uid + '-segmentTypeAction__Intro', ACTION[p.skip_intro] || 'AskToSkip');
      localStorage.setItem(uid + '-segmentTypeAction__Outro', ACTION[p.skip_credits] || 'AskToSkip');
    } catch (e) { }
  }
  function loadPrefs() {
    if (!signedIn()) return;
    call('GET', 'TentacleSubs/Prefs').then(function (p) { S.prefs = p; applySkip(p); }).catch(function () { });
  }

  // ── The player: what plays here ───────────────────────────────────────
  function onPlayer() { return /#!?\/video\b/.test(location.hash) || !!document.querySelector('.videoOsdBottom'); }
  function askPlaying() {
    var a = api();
    if (!a || !onPlayer() || Date.now() - S.asked < 3000) return Promise.resolve(S.item);
    S.asked = Date.now();
    return a.getJSON(a.getUrl('Sessions', { DeviceId: a.deviceId() })).then(function (list) {
      var item = null;
      (list || []).forEach(function (s) { if (s.NowPlayingItem) item = s.NowPlayingItem; });
      S.item = item;
      return item;
    }).catch(function () { return S.item; });
  }

  // ── The subtitles menu ────────────────────────────────────────────────
  function subtitleSheet(sheet) {
    var title = sheet.querySelector('.actionSheetTitle');
    var text = title ? title.textContent.trim().toLowerCase() : '';
    return /subtitle/.test(text);
  }
  function itemText(el) { var t = el.querySelector('.actionSheetItemText') || el; return t; }
  function langOf(label) {
    var p = S.prefs;
    if (!p || !p.languages) return null;
    var name = label.replace(/\s+SDH$/, '').replace(/\s+\(forced\)$/, '').trim().toLowerCase();
    var hit = p.languages.filter(function (l) { return l.name.toLowerCase() === name; })[0];
    return hit ? hit.code : null;
  }
  function tidy(sheet) {
    if (sheet.dataset.tentacleSubs) return;
    if (!subtitleSheet(sheet)) return;
    sheet.dataset.tentacleSubs = '1';
    var selected = null;
    sheet.querySelectorAll('.actionSheetMenuItem').forEach(function (el) {
      var t = itemText(el);
      t.textContent = t.textContent.replace(FORMATS, '');
      var check = el.querySelector('.actionsheetMenuItemIcon, .check');
      if (check && getComputedStyle(check).visibility !== 'hidden' && el.dataset.id !== '-1') selected = t.textContent.trim();
      el.addEventListener('click', function () { if (el.dataset.id !== '-1') setTimeout(watchReady, 1500); });
    });
    var lang = selected && langOf(selected);
    var box = sheet.querySelector('.actionSheetScroller') || sheet;
    if (!lang || !S.item) return;
    // A copy of Jellyfin's own item, so it lines up with the others.
    var model = sheet.querySelector('.actionSheetMenuItem');
    var more = model.cloneNode(true);
    more.removeAttribute('data-id');
    more.classList.add('tentacle-subs-another');
    var icon = more.querySelector('.actionsheetMenuItemIcon, .check');
    if (icon) icon.style.visibility = 'hidden';
    itemText(more).textContent = 'Subtitles wrong? Get another';
    more.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      var dlg = sheet.closest('.dialog');
      if (dlg) { var c = dlg.querySelector('.btnCloseActionSheet'); if (c) c.click(); else document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); }
      getAnother(lang, selected);
    });
    box.appendChild(more);
    itemText(more).style.whiteSpace = 'normal';
    // Jellyfin places the sheet for the items it made: keep it on screen
    // with this one too (it positions after this runs, so look again).
    var dlg = sheet.closest('.dialog') || sheet;
    var fit = function () {
      var r = dlg.getBoundingClientRect();
      var down = r.bottom - window.innerHeight + 8;
      var right = r.right - window.innerWidth + 8;
      if (down > 0) dlg.style.top = Math.max(8, r.top - down) + 'px';
      if (right > 0) dlg.style.left = Math.max(8, r.left - right) + 'px';
    };
    [0, 60, 250, 600].forEach(function (ms) { setTimeout(fit, ms); });
  }

  function showNote(text, sticky) {
    if (!S.note) {
      S.note = document.createElement('div');
      S.note.className = 'tentacle-subs-note';
      S.note.style.cssText = 'position:fixed;left:50%;bottom:7.5em;transform:translateX(-50%);z-index:100000;max-width:min(90vw,34em);'
        + 'background:rgba(20,20,20,.92);color:#fff;padding:.7em 1.1em;border-radius:.5em;font-size:1em;line-height:1.35;text-align:center;pointer-events:none';
      document.body.appendChild(S.note);
    }
    S.note.textContent = text;
    S.note.style.display = 'block';
    clearTimeout(S.note._t);
    if (!sticky) S.note._t = setTimeout(hideNote, 6000);
  }
  function hideNote() { if (S.note) S.note.style.display = 'none'; }

  function minutes(sec) {
    if (sec == null) return '';
    if (sec < 60) return 'less than a minute';
    var m = Math.round(sec / 60);
    return 'about ' + m + ' minute' + (m === 1 ? '' : 's');
  }

  // A subtitle was picked: say so while Tentacle is still reading it.
  function watchReady() {
    clearInterval(S.poll);
    askPlaying().then(function (item) {
      if (!item) return;
      var id = item.Id;
      var tick = function () {
        if (!onPlayer() || !S.item || S.item.Id !== id) { clearInterval(S.poll); hideNote(); return; }
        call('GET', 'TentacleSubs/Status/' + id).then(function (st) {
          if (st.state === 'running') {
            var left = st.seconds_left != null ? ' — ' + minutes(st.seconds_left) + ' left' : '';
            showNote('Getting the subtitles ready' + (st.percent != null ? ' (' + st.percent + '%)' : '') + left + '. They appear by themselves.', true);
          } else if (st.state === 'queued') {
            showNote('Getting the subtitles ready: starting' + (st.ahead ? ' after ' + st.ahead + ' other title' + (st.ahead === 1 ? '' : 's') : '') + '. They appear by themselves.', true);
          } else {
            clearInterval(S.poll);
            hideNote();
          }
        }).catch(function () { });
      };
      tick();
      S.poll = setInterval(tick, 4000);
    });
  }

  function getAnother(lang, label) {
    if (S.busy || !S.item) return;
    S.busy = true;
    showNote('Looking for other ' + label + ' subtitles…', true);
    call('POST', 'TentacleSubs/Another/' + S.item.Id, { language: lang }).then(function () {
      showNote('Other ' + label + ' subtitles are in. Turn subtitles off and pick ' + label + ' again to see them.');
    }).catch(function (e) {
      showNote(e.status === 404 ? 'No other ' + label + ' subtitles were found for this title.' : 'That didn’t work: ' + e.message);
    }).then(function () { S.busy = false; });
  }

  // ── wiring ────────────────────────────────────────────────────────────
  new MutationObserver(function () {
    document.querySelectorAll('.actionSheet').forEach(tidy);
    if (onPlayer()) askPlaying();
  }).observe(document.documentElement, { childList: true, subtree: true });

  // Tentacle says this person's choices changed (POST /Tentacle/Notify, topic
  // "watch"; tentacle-notifications.js passes every Tentacle message on).
  window.addEventListener('tentacle-changed', function (e) {
    var topics = (e.detail && e.detail.topics) || [];
    if (topics.indexOf('watch') !== -1) loadPrefs();
  });

  var tries = 0;
  (function start() {
    if (signedIn()) { loadPrefs(); return; }
    if (++tries < 600) setTimeout(start, 1000);
  })();
  document.addEventListener('viewshow', function () { if (!S.prefs) loadPrefs(); });
})();
