// Tentacle - ▶ Play links from Tentacle's dashboard (Oct 2026).
//
// Tentacle's Play buttons open this title's page in Jellyfin Web with
// "&tentaclePlay=1" (#/details?id=<item>&serverId=<server>&tentaclePlay=1,
// services/watch_links.py). Jellyfin Web has no play link of its own, so
// this script starts it: a movie or an episode from where this person
// stopped, a show at the episode they were watching or its next-up episode
// (the first one when they haven't started it). The flag is taken out of
// the address first, so Back and a reload show the page and don't play again.
// Not signed in yet: the request waits (a few minutes) and plays after the
// sign-in. Playback goes through window.playbackManager, which
// tentacle-livetv.js provides (plays in this tab).
(function () {
  'use strict';

  var KEY = 'tentacle-play';
  var WAIT_MS = 5 * 60 * 1000;     // a sign-in in between
  var TRY_MS = 400;
  var timer = null;

  function parse(hash) {
    var m = /^#!?\/details\?(.*)$/.exec(hash || '');
    if (!m) return null;
    var params = {};
    m[1].split('&').forEach(function (kv) {
      var i = kv.indexOf('=');
      if (i > 0) {
        try { params[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1)); } catch (e) { }
      }
    });
    if (params.tentaclePlay !== '1' || !/^[0-9a-fA-F-]{32,36}$/.test(params.id || '')) return null;
    return { id: params.id.replace(/-/g, ''), serverId: params.serverId || '' };
  }

  function withoutFlag(hash) {
    return hash.replace(/&tentaclePlay=1\b/, '').replace(/\?tentaclePlay=1&/, '?').replace(/\?tentaclePlay=1$/, '');
  }

  function save(req) {
    try { sessionStorage.setItem(KEY, JSON.stringify({ id: req.id, serverId: req.serverId, at: Date.now() })); } catch (e) { }
  }

  function pending() {
    try {
      var p = JSON.parse(sessionStorage.getItem(KEY) || 'null');
      if (p && p.id && Date.now() - (p.at || 0) < WAIT_MS) return p;
    } catch (e) { }
    forget();
    return null;
  }

  function forget() {
    try { sessionStorage.removeItem(KEY); } catch (e) { }
  }

  // A Play link was opened: remember it, show the page without the flag.
  function take() {
    var req = parse(location.hash);
    if (!req) return;
    save(req);
    try {
      history.replaceState(history.state, '', location.pathname + location.search + withoutFlag(location.hash));
    } catch (e) { }
  }

  function api() { return window.ApiClient; }

  function signedIn() {
    var a = api();
    try { return !!(a && a.getCurrentUserId && a.getCurrentUserId() && a.accessToken && a.accessToken()); } catch (e) { return false; }
  }

  function ready() {
    // tentacle-livetv.js gives playbackManager; it plays once Jellyfin Web's
    // message handler is bound to the signed-in ApiClient.
    var a = api();
    return signedIn() && window.playbackManager && typeof window.playbackManager.play === 'function'
      && a._callbacks && a._callbacks.message && a._callbacks.message.length;
  }

  function get(path) {
    var a = api();
    return a.getJSON(a.getUrl(path));
  }

  function ticks(item) {
    var d = item && item.UserData;
    return d && !d.Played && d.PlaybackPositionTicks ? d.PlaybackPositionTicks : 0;
  }

  // What to play for an item: [itemId, startTicks], or null.
  function choose(item) {
    var uid = api().getCurrentUserId();
    if (!item || !item.Id) return Promise.resolve(null);
    if (item.Type !== 'Series') {
      if (item.IsFolder) return Promise.resolve(null);
      return Promise.resolve([item.Id, ticks(item)]);
    }
    // A show: the episode in progress, else next up, else the first one.
    var fields = '&Fields=UserData&EnableImages=false';
    return get('Users/' + uid + '/Items?ParentId=' + item.Id + '&Recursive=true&IncludeItemTypes=Episode'
      + '&Filters=IsResumable&SortBy=DatePlayed&SortOrder=Descending&Limit=1' + fields)
      .then(function (r) {
        var ep = r && r.Items && r.Items[0];
        if (ep) return [ep.Id, ticks(ep)];
        return get('Shows/NextUp?SeriesId=' + item.Id + '&UserId=' + uid + '&Limit=1&EnableRewatching=false' + fields)
          .then(function (n) {
            var next = n && n.Items && n.Items[0];
            if (next) return [next.Id, ticks(next)];
            return get('Shows/' + item.Id + '/Episodes?UserId=' + uid + fields).then(function (all) {
              var eps = (all && all.Items) || [];
              var first = eps.filter(function (e) { return (e.ParentIndexNumber || 0) > 0 && e.LocationType !== 'Virtual'; })[0]
                || eps.filter(function (e) { return e.LocationType !== 'Virtual'; })[0];
              return first ? [first.Id, ticks(first)] : null;
            });
          });
      });
  }

  function play(req) {
    forget();
    var a = api();
    get('Users/' + a.getCurrentUserId() + '/Items/' + req.id)
      .then(choose)
      .then(function (what) {
        if (!what) return;   // nothing playable: the page is shown
        return window.playbackManager.play({
          ids: [what[0]],
          startPositionTicks: what[1] || 0,
          serverId: (a.serverId && a.serverId()) || req.serverId
        });
      })
      .catch(function (e) { console.warn('[Tentacle] Play from Tentacle failed; the page is shown', e); });
  }

  function attempt() {
    timer = null;
    var req = pending();
    if (!req) return;
    if (ready()) { play(req); return; }
    timer = setTimeout(attempt, TRY_MS);
  }

  function check() {
    take();
    if (!timer && pending()) attempt();
  }

  window.addEventListener('hashchange', check);
  document.addEventListener('viewshow', check);
  check();

  // For the unit tests (tests/test_watch_links.py)
  window.TentaclePlay = { internals: { parse: parse, withoutFlag: withoutFlag, choose: choose } };
})();
