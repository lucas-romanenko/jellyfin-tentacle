// Tentacle Favorites — the Favorites tab as rows, like Tentacle's home
//
// Architecture (same pattern as tentacle-livetv.js):
//   - #tentacleFavoritesContainer mounted on document.body (z-index 900)
//   - Body class `tentacle-favorites-active` while shown
//   - Route-driven: active on the home route with ?tab=1 (Jellyfin's Favorites
//     tab) — back/forward and refresh work naturally
//   - One row per kind, each scrolling sideways (home's .mh-row markup), in
//     this order: Movies, Shows, Live TV, YouTube, Recordings, then Episodes,
//     Collections, Playlists, music and anything else. Empty rows are hidden.
//   - What a person may see is Jellyfin's to decide: every list is asked for
//     with their own token, so kids' limits and YouTube channels they don't
//     follow (BlockedTags) never reach the page.
//   - Live updates: a favourite set or cleared anywhere (Tentacle, the guide's
//     star, another device) arrives as UserDataChanged on Jellyfin's websocket
//     and the page reads its lists again within a second; rows are redrawn
//     only where something changed, keeping their scroll. No socket: every 10 s.
//   - Click a card → Tentacle details (channels: play); the heart (on hover)
//     takes it off the list.
(function () {
  'use strict';

  var EMPTY_HTML = '<div class="tfav-empty">No favorites yet. Mark movies, shows or channels with the ♥ heart and they’ll show up here.</div>';

  // Tentacle's own tags. Videos it fetched from YouTube carry yt:<channel>
  // (a bare "youtube" tag is also a TMDB keyword on ordinary films), and a
  // YouTube channel's Live TV channel carries tentacle-youtube-<id>.
  var YT_VIDEO_TAG = /^yt:/i;
  var YT_LIVE_TAG = /^tentacle-youtube-/i;

  // Row order. `types` are Jellyfin item types; Live TV, YouTube and
  // Recordings are filled by classify() rather than by type alone.
  var ROWS = [
    { key: 'movies', title: 'Movies', types: ['Movie'], shape: 'poster' },
    { key: 'shows', title: 'Shows', types: ['Series'], shape: 'poster' },
    { key: 'livetv', title: 'Live TV', types: [], shape: 'channel' },
    { key: 'youtube', title: 'YouTube', types: [], shape: 'wide' },
    { key: 'recordings', title: 'Recordings', types: [], shape: 'wide' },
    { key: 'seasons', title: 'Seasons', types: ['Season'], shape: 'poster' },
    { key: 'episodes', title: 'Episodes', types: ['Episode'], shape: 'wide' },
    { key: 'collections', title: 'Collections', types: ['BoxSet'], shape: 'poster' },
    { key: 'playlists', title: 'Playlists', types: ['Playlist'], shape: 'square' },
    { key: 'artists', title: 'Artists', types: ['MusicArtist'], shape: 'square' },
    { key: 'albums', title: 'Albums', types: ['MusicAlbum'], shape: 'square' },
    { key: 'songs', title: 'Songs', types: ['Audio'], shape: 'square' },
    { key: 'videos', title: 'Videos', types: ['Video', 'MusicVideo'], shape: 'wide' },
    { key: 'books', title: 'Books', types: ['Book', 'AudioBook'], shape: 'poster' },
    { key: 'photos', title: 'Photos', types: ['Photo', 'PhotoAlbum'], shape: 'wide' },
  ];
  var ROW_BY_TYPE = {};
  ROWS.forEach(function (r) { r.types.forEach(function (t) { ROW_BY_TYPE[t] = r.key; }); });

  var PAGE = 500;                 // favourites per request (paged to the end)
  var POLL_MS = 10000;            // no websocket: read again this often
  var CHANNEL_REFRESH_MS = 60000; // what's on now / progress bars
  var FIELDS = 'PrimaryImageAspectRatio,ProductionYear,PremiereDate,Tags';

  var FAV = {
    active: false,
    container: null,
    rowsEl: null,
    userId: '',
    data: { items: null, channels: null, recordingIds: null },
    load: { busy: false, again: false, timer: null, at: 0, gen: 0 },
    rowKeys: {},
    socket: null,
    channelTimer: null,
    tick: null,
  };

  function api() { return window.ApiClient; }

  function esc(str) {
    if (str == null) return '';
    var d = document.createElement('div');
    d.textContent = String(str);
    return d.innerHTML;
  }

  // Attribute-context escaping (escapes quotes, unlike esc()).
  function escAttr(str) {
    if (str == null) return '';
    return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function apiGet(path) {
    return api().getJSON(api().getUrl(path));
  }

  function normId(id) { return String(id || '').replace(/-/g, '').toLowerCase(); }

  // ── Route detection ────────────────────────────────────────────────
  // Favorites is Jellyfin's home tab=1
  function isFavoritesPage() {
    var h = location.hash || '';
    var isHome = h === '' || h === '#/' || /^#\/home(\.html)?(\?|$)/.test(h);
    if (!isHome) return false;
    return /[?&]tab=1\b/.test(h);
  }

  // ── Activate / Deactivate ──────────────────────────────────────────

  function activate() {
    if (window.TentacleStandAside && window.TentacleStandAside()) return; // TV layout — native page renders
    if (FAV.active) { scheduleLoad(0); return; }
    FAV.active = true;
    document.body.classList.add('tentacle-favorites-active');
    ensureContainer();
    FAV.container.style.display = 'block';
    learnUser();
    watchSocket();
    // Back on the page: what it showed last time at once, then fresh lists.
    if (FAV.data.items) render();
    scheduleLoad(0);
    if (!FAV.tick) FAV.tick = setInterval(onTick, 2000);
    startChannelRefresh();
  }

  function deactivate() {
    if (!FAV.active) return;
    FAV.active = false;
    document.body.classList.remove('tentacle-favorites-active');
    if (FAV.container) FAV.container.style.display = 'none';
    clearTimeout(FAV.channelTimer);
    FAV.channelTimer = null;
  }

  // ── Container ──────────────────────────────────────────────────────

  function ensureContainer() {
    if (FAV.container) return;
    var el = document.createElement('div');
    el.id = 'tentacleFavoritesContainer';
    // No page title: the navbar's Favorites pill already says where you are.
    el.innerHTML = '<div class="tfav-rows"><div class="tfav-loading"><div class="mh-spinner-sm"></div></div></div>';
    document.body.appendChild(el);
    el.addEventListener('click', onContainerClick);
    FAV.container = el;
    FAV.rowsEl = el.querySelector('.tfav-rows');
  }

  // ── Fetch ──────────────────────────────────────────────────────────
  //
  // Three independent reads, each drawn as soon as it arrives (the first
  // rows are on screen while the others are still on their way):
  //   1. every favourite of the person, all kinds, one paged query;
  //   2. their favourite Live TV channels, with what's on now;
  //   3. which favourites are recordings (the DVR's folders), if any.

  function learnUser() {
    if (!FAV.userId && api()) FAV.userId = api().getCurrentUserId() || '';
    return FAV.userId;
  }

  function scheduleLoad(delay) {
    if (FAV.load.timer) clearTimeout(FAV.load.timer);
    FAV.load.timer = setTimeout(function () { FAV.load.timer = null; loadAll(); }, delay);
  }

  function loadAll() {
    if (!FAV.active || !api()) return;
    var uid = learnUser();
    if (!uid) { scheduleLoad(500); return; }
    if (FAV.load.busy) { FAV.load.again = true; return; }
    FAV.load.busy = true;
    FAV.load.at = Date.now();
    var gen = ++FAV.load.gen;
    var fresh = function () { return gen === FAV.load.gen && FAV.active; };

    var items = fetchAllPages(
      'Users/' + uid + '/Items?Filters=IsFavorite&Recursive=true&ExcludeItemTypes=TvChannel,Program' +
      '&SortBy=SortName&SortOrder=Ascending&Fields=' + FIELDS +
      '&EnableUserData=false&ImageTypeLimit=1&EnableImageTypes=Primary,Thumb,Backdrop'
    ).then(function (list) {
      if (!fresh()) return;
      FAV.data.items = list;
      render();
    });

    var channels = loadChannels(fresh);

    var recordings = apiGet('LiveTv/Recordings/Folders?userId=' + uid)
      .then(function (data) {
        var folders = (data && data.Items) || [];
        return Promise.all(folders.map(function (f) {
          return fetchAllPages('Users/' + uid + '/Items?Filters=IsFavorite&Recursive=true&ParentId=' + f.Id +
            '&Fields=ParentId&EnableUserData=false&EnableImages=false');
        }));
      })
      .then(function (lists) {
        var ids = {};
        lists.forEach(function (l) { l.forEach(function (i) { ids[normId(i.Id)] = true; }); });
        return ids;
      })
      .catch(function () { return {}; })   // no DVR or no Live TV access
      .then(function (ids) {
        if (!fresh()) return;
        FAV.data.recordingIds = ids;
        if (FAV.data.items) render();
      });

    var done = function () {
      FAV.load.busy = false;
      if (FAV.load.again) { FAV.load.again = false; scheduleLoad(0); }
    };
    Promise.all([
      items.catch(function (e) { console.warn('[Tentacle Favorites] favourites unavailable', e); }),
      channels, recordings,
    ]).then(done, done);
  }

  // Page through a list to its end. Stop on what the server reports (its
  // total, or an empty page) rather than on a page coming back as long as was
  // asked for: a server may cap Limit lower.
  function fetchAllPages(path) {
    var step = function (acc) {
      return apiGet(path + '&StartIndex=' + acc.length + '&Limit=' + PAGE).then(function (data) {
        var page = (data && data.Items) || [];
        var all = acc.concat(page);
        var total = data && typeof data.TotalRecordCount === 'number' ? data.TotalRecordCount : null;
        var more = page.length > 0 && (total === null ? page.length >= PAGE : all.length < total);
        return more ? step(all) : all;
      });
    };
    return step([]);
  }

  // Jellyfin's own favourites on the one lineup, filtered by Jellyfin to the
  // channels this person may see (kids: only kids channels). No Live TV
  // access: none.
  function loadChannels(fresh) {
    var uid = learnUser();
    return apiGet('LiveTv/Channels?UserId=' + uid + '&IsFavorite=true&AddCurrentProgram=true&EnableUserData=false' +
                  '&Fields=Tags&ImageTypeLimit=1&EnableImageTypes=Primary')
      .then(function (data) { return (data && data.Items) || []; })
      .catch(function () { return []; })
      .then(function (list) {
        if (fresh && !fresh()) return;
        FAV.data.channels = list;
        render();
      });
  }

  // What's on changes on its own: read the channels again every minute
  // (one small request) so the programme and its progress bar keep up.
  function startChannelRefresh() {
    clearTimeout(FAV.channelTimer);
    FAV.channelTimer = setTimeout(function () {
      if (!FAV.active) return;
      if (!document.hidden) loadChannels(function () { return FAV.active; });
      startChannelRefresh();
    }, CHANNEL_REFRESH_MS);
  }

  // ── Sort into rows ─────────────────────────────────────────────────

  function hasTag(item, re) {
    return (item.Tags || []).some(function (t) { return re.test(t); });
  }

  // { rowKey: [entries] }, entry = { kind: 'item'|'channel', item }
  function classify(items, channels, recordingIds) {
    var out = {};
    var add = function (key, entry) { (out[key] = out[key] || []).push(entry); };
    (channels || []).slice().sort(byNumber).forEach(function (ch) {
      add(hasTag(ch, YT_LIVE_TAG) ? 'youtube' : 'livetv', { kind: 'channel', item: ch });
    });
    (items || []).forEach(function (it) {
      if (it.Type === 'TvChannel' || it.Type === 'Program') return;
      var key;
      if (recordingIds && recordingIds[normId(it.Id)]) key = 'recordings';
      else if (hasTag(it, YT_VIDEO_TAG)) key = 'youtube';
      else key = ROW_BY_TYPE[it.Type];
      if (key) add(key, { kind: 'item', item: it });
    });
    return out;   // YouTube: its live channels first, then videos
  }

  // Lineup order (the guide's), then name.
  function byNumber(a, b) {
    var na = parseFloat(a.ChannelNumber || a.Number), nb = parseFloat(b.ChannelNumber || b.Number);
    if (isNaN(na) !== isNaN(nb)) return isNaN(na) ? 1 : -1;
    if (!isNaN(na) && na !== nb) return na - nb;
    return String(a.Name || '').localeCompare(String(b.Name || ''));
  }

  // ── Render ─────────────────────────────────────────────────────────
  //
  // Each row is redrawn only when what it shows changed (its key), so a live
  // update leaves the other rows — and their scroll — exactly as they were.

  // The rows to show, in order: [{ def, entries, key }], empty ones left out.
  // `key` is what the row's cards show, so an unchanged row can be left alone.
  function planRows(d, now) {
    var groups = classify(d.items, d.channels, d.recordingIds);
    return ROWS.filter(function (def) { return (groups[def.key] || []).length; }).map(function (def) {
      var entries = groups[def.key];
      return { def: def, entries: entries, key: entries.map(function (e) { return entryKey(e, now); }).join('|') };
    });
  }

  function rowHtml(plan, now) {
    return plan.entries.map(function (e) {
      return e.kind === 'channel' ? channelCard(e.item, now) : card(e.item, plan.def.shape);
    }).join('');
  }

  function render() {
    if (!FAV.container) return;
    var d = FAV.data;
    var now = Date.now();
    var plans = planRows(d, now);
    var rowsEl = FAV.rowsEl;
    var loading = rowsEl.querySelector('.tfav-loading');
    var any = plans.length > 0;
    var shown = {};
    plans.forEach(function (p) { shown[p.def.key] = true; });

    // Rows that emptied go; the rest are put in ROWS order, created as needed.
    ROWS.forEach(function (def) {
      if (shown[def.key]) return;
      var gone = rowsEl.querySelector('.mh-row[data-row="' + def.key + '"]');
      if (gone) gone.remove();
      delete FAV.rowKeys[def.key];
    });
    var prev = loading || null;
    plans.forEach(function (p) {
      var rowEl = rowsEl.querySelector('.mh-row[data-row="' + p.def.key + '"]');
      if (!rowEl) {
        rowEl = createRow(p.def);
        rowsEl.insertBefore(rowEl, prev ? prev.nextSibling : rowsEl.firstChild);
      }
      prev = rowEl;
      if (FAV.rowKeys[p.def.key] === p.key) return;
      FAV.rowKeys[p.def.key] = p.key;
      var itemsEl = rowEl.querySelector('.mh-row-items');
      var left = itemsEl.scrollLeft;
      itemsEl.innerHTML = rowHtml(p, now);
      itemsEl.scrollLeft = left;
      updateArrows(rowEl);
    });

    // Nothing to say until the favourites themselves have been read.
    var known = d.items !== null;
    if (loading && (any || known)) loading.remove();
    var empty = rowsEl.querySelector('.tfav-empty');
    if (!any && known && d.channels !== null) {
      if (!empty) rowsEl.insertAdjacentHTML('beforeend', EMPTY_HTML);
    } else if (empty && any) {
      empty.remove();
    }
  }

  // What a card shows, to the minute for channels (programme and progress)
  function entryKey(e, now) {
    var it = e.item;
    if (e.kind !== 'channel') {
      return it.Id + ':' + (it.Name || '') + ':' + JSON.stringify(it.ImageTags || {});
    }
    var p = it.CurrentProgram;
    var pk = p ? p.Id + ':' + Math.round(progressPct(p, now)) : '';
    return it.Id + ':' + (it.Name || '') + ':' + (it.ChannelNumber || '') + ':' + pk;
  }

  function createRow(def) {
    var row = document.createElement('div');
    row.className = 'mh-row tfav-row tfav-row-' + def.shape;
    row.setAttribute('data-row', def.key);
    row.innerHTML =
      '<div class="mh-row-header"><h2 class="mh-row-title">' + esc(def.title) + '</h2></div>' +
      '<div class="mh-row-scroll">' +
        '<div class="mh-row-arrow mh-row-arrow-left" data-scroll="-1">&#x2039;</div>' +
        '<div class="mh-row-items"></div>' +
        '<div class="mh-row-arrow mh-row-arrow-right" data-scroll="1">&#x203A;</div>' +
      '</div>';
    var itemsEl = row.querySelector('.mh-row-items');
    if (itemsEl.addEventListener) {
      itemsEl.addEventListener('scroll', function () { updateArrows(row); }, { passive: true });
    }
    return row;
  }

  // Arrows only where there is more to scroll to.
  function updateArrows(row) {
    var el = row.querySelector('.mh-row-items');
    if (!el || !row.classList) return;
    var max = (el.scrollWidth || 0) - (el.clientWidth || 0);
    row.classList.toggle('tfav-at-start', !(el.scrollLeft > 4));
    row.classList.toggle('tfav-at-end', !(el.scrollLeft < max - 4));
  }

  function imageUrl(item, type, tag, maxWidth) {
    return api().getUrl('Items/' + item.Id + '/Images/' + type, { maxWidth: maxWidth, tag: tag, quality: 90 });
  }

  // The best picture for a card's shape: wide cards prefer Thumb, then
  // Backdrop (a poster cropped into 16:9 is a strip of its middle).
  function pickImage(item, shape) {
    var tags = item.ImageTags || {};
    if (shape === 'wide') {
      if (tags.Thumb) return imageUrl(item, 'Thumb', tags.Thumb, 500);
      if (item.BackdropImageTags && item.BackdropImageTags.length) return imageUrl(item, 'Backdrop', item.BackdropImageTags[0], 500);
    }
    if (tags.Primary) return imageUrl(item, 'Primary', tags.Primary, shape === 'wide' ? 500 : 300);
    if (item.AlbumId && item.AlbumPrimaryImageTag) {
      return api().getUrl('Items/' + item.AlbumId + '/Images/Primary', { maxWidth: 300, tag: item.AlbumPrimaryImageTag, quality: 90 });
    }
    return null;
  }

  var HEART = '<svg viewBox="0 -960 960 960" fill="currentColor"><path d="m480-120-58-52q-101-91-167-157T150-447.5Q111-500 95.5-544T80-634q0-94 63-157t157-63q52 0 99 22t81 62q34-40 81-62t99-22q94 0 157 63t63 157q0 46-15.5 90T810-447.5Q771-395 705-329T538-172l-58 52Z"/></svg>';

  function unfavButton(id) {
    return '<button class="tfav-unfav" title="Remove from Favorites" aria-label="Remove from Favorites" data-unfav="' + escAttr(id) + '">' + HEART + '</button>';
  }

  function subtitleFor(item) {
    if (item.Type === 'Episode') {
      var s = item.ParentIndexNumber, e = item.IndexNumber;
      return (s != null && e != null ? 'S' + s + ':E' + e + ' · ' : '') + (item.Name || '');
    }
    if (item.Type === 'Season') return item.SeriesName || '';
    if (item.Type === 'Audio' || item.Type === 'MusicAlbum') {
      return item.AlbumArtist || (item.Artists && item.Artists[0]) || '';
    }
    // A YouTube video's year says nothing; the day it came out does.
    if (hasTag(item, YT_VIDEO_TAG) && item.PremiereDate) return shortDate(item.PremiereDate);
    return item.ProductionYear ? String(item.ProductionYear) : '';
  }

  function shortDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var opts = { month: 'short', day: 'numeric' };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    try { return d.toLocaleDateString(undefined, opts); } catch (e) { return d.toDateString(); }
  }

  function card(item, shape) {
    var img = pickImage(item, shape);
    var title = item.Type === 'Episode' ? (item.SeriesName || item.Name) : item.Name;
    var sub = subtitleFor(item);
    var cls = shape === 'wide' ? 'mh-card mh-card-wide' : shape === 'square' ? 'mh-card tfav-card-square' : 'mh-card';
    var posterCls = shape === 'wide' ? 'mh-card-poster mh-card-poster-wide' : shape === 'square' ? 'mh-card-poster tfav-poster-square' : 'mh-card-poster';
    return '<div class="' + cls + ' tfav-card" data-id="' + escAttr(item.Id) + '" data-type="' + escAttr(item.Type) + '">' +
        '<div class="' + posterCls + '">' +
          (img ? '<img src="' + escAttr(img) + '" alt="" loading="lazy" onerror="this.style.display=\'none\'">'
               : '<div class="mh-card-no-poster">🎬</div>') +
          unfavButton(item.Id) +
        '</div>' +
        '<div class="mh-card-info">' +
          '<div class="mh-card-title tfav-card-name" title="' + escAttr(title) + '">' + esc(title) + '</div>' +
          (sub ? '<div class="mh-card-subtitle" title="' + escAttr(sub) + '">' + esc(sub) + '</div>' : '') +
        '</div>' +
      '</div>';
  }

  // ── Live TV channel card ───────────────────────────────────────────
  // The channel is the thing here: its logo whole on a calm panel, its number
  // and name, what's on now with how far in it is; tap = watch.

  function progressPct(p, now) {
    if (!p || !p.StartDate || !p.EndDate) return 0;
    var start = Date.parse(p.StartDate), end = Date.parse(p.EndDate);
    if (!(end > start)) return 0;
    return Math.max(0, Math.min(100, (now - start) * 100 / (end - start)));
  }

  function onNow(p, now) {
    return p && p.EndDate && Date.parse(p.EndDate) > now && (!p.StartDate || Date.parse(p.StartDate) <= now);
  }

  function minutesLeft(p, now) {
    var left = Math.max(1, Math.round((Date.parse(p.EndDate) - now) / 60000));
    return left >= 60 ? Math.floor(left / 60) + 'h ' + (left % 60) + 'm left' : left + 'm left';
  }

  function channelCard(ch, now) {
    // No tag in the URL (Live TV images 500 with one), and no token.
    var logo = ch.ImageTags && ch.ImageTags.Primary
      ? api().getUrl('Items/' + ch.Id + '/Images/Primary', { maxWidth: 400, quality: 90 })
      : null;
    var p = onNow(ch.CurrentProgram, now) ? ch.CurrentProgram : null;
    var num = ch.ChannelNumber || ch.Number || '';
    var pct = p ? Math.max(2, Math.round(progressPct(p, now))) : 0;
    return '<div class="mh-card mh-card-wide tfav-channel" data-channel-id="' + escAttr(ch.Id) + '">' +
        '<div class="mh-card-poster mh-card-poster-wide mh-onnow-logo">' +
          // the name stands in for a missing logo (CSS hides it after an <img>)
          (logo ? '<img src="' + escAttr(logo) + '" alt="" loading="lazy" onerror="this.remove()">' : '') +
          '<div class="tfav-logo-name">' + esc(ch.Name) + '</div>' +
          (num ? '<div class="tfav-chno">' + esc(num) + '</div>' : '') +
          (p ? '<div class="mh-card-progress"><div class="mh-card-progress-bar" style="width:' + pct + '%"></div></div>' : '') +
          '<div class="mh-card-play-overlay"><div class="mh-card-play-icon">▶</div></div>' +
          unfavButton(ch.Id) +
        '</div>' +
        '<div class="mh-card-info">' +
          '<div class="mh-card-title tfav-card-name" title="' + escAttr(ch.Name) + '">' + esc(ch.Name) + '</div>' +
          (p
            ? '<div class="mh-card-subtitle tfav-now" title="' + escAttr(p.Name || '') + '">' + esc(p.Name || '') + '</div>' +
              '<span class="mh-card-remaining">' + minutesLeft(p, now) + '</span>'
            : '<div class="mh-card-subtitle tfav-now tfav-now-none">Nothing in the guide now</div>') +
        '</div>' +
      '</div>';
  }

  // ── Interactions ───────────────────────────────────────────────────

  function onContainerClick(e) {
    var arrow = e.target.closest('[data-scroll]');
    if (arrow) {
      var items = arrow.parentElement.querySelector('.mh-row-items');
      var dir = arrow.getAttribute('data-scroll') === '-1' ? -1 : 1;
      if (items) items.scrollBy({ left: dir * items.clientWidth * 0.8, behavior: 'smooth' });
      return;
    }

    var unfav = e.target.closest('[data-unfav]');
    if (unfav) {
      e.preventDefault();
      e.stopPropagation();
      unfavorite(unfav.getAttribute('data-unfav'));
      return;
    }

    // Live TV channel → play (same behaviour as the Live TV page)
    var channelEl = e.target.closest('[data-channel-id]');
    if (channelEl) {
      var channelId = channelEl.getAttribute('data-channel-id');
      if (window.TentacleLiveTV && window.TentacleLiveTV.playChannel) {
        window.TentacleLiveTV.playChannel(channelId);
      } else {
        window.location.hash = '#/details?id=' + channelId;
      }
      return;
    }

    var cardEl = e.target.closest('.tfav-card');
    if (!cardEl) return;
    var itemId = cardEl.getAttribute('data-id');
    var itemType = cardEl.getAttribute('data-type');
    if (!itemId) return;
    if ((itemType === 'Movie' || itemType === 'Series') && window.TentacleDetails && window.TentacleDetails.show) {
      window.TentacleDetails.show(itemId, itemType);
    } else {
      window.location.hash = '#/details?id=' + itemId;
    }
  }

  // Take it off the page at once; the server's UserDataChanged then confirms
  // (and a failure puts it back on the next read).
  function unfavorite(itemId) {
    var uid = learnUser();
    var drop = function (list) {
      return (list || []).filter(function (i) { return normId(i.Id) !== normId(itemId); });
    };
    FAV.data.items = FAV.data.items && drop(FAV.data.items);
    FAV.data.channels = FAV.data.channels && drop(FAV.data.channels);
    render();
    var again = function () { scheduleLoad(0); };
    try {
      if (api().updateFavoriteStatus) {
        // jellyfin-apiclient's signature is (userId, itemId, isFavorite).
        api().updateFavoriteStatus(uid, itemId, false).catch(again);
      } else {
        api().ajax({
          type: 'DELETE',
          url: api().getUrl('Users/' + uid + '/FavoriteItems/' + itemId),
        }).catch(again);
      }
    } catch (err) { again(); }
  }

  // ── Live updates ───────────────────────────────────────────────────
  //
  // Jellyfin web's own websocket (ApiClient._webSocket; a new one after each
  // reconnect): we only listen. Watched in every tab, so the tab that gets a
  // message passes it on to the others (Jellyfin sends a browser's messages to
  // one socket only, the newest tab's). The Live TV guide relays on its own
  // channel too; both are heard.

  var relay = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('tentacle-favorites') : null;
  var guideRelay = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('tentacle-livetv') : null;
  [relay, guideRelay].forEach(function (ch) {
    if (!ch) return;
    ch.onmessage = function (e) { onMessage(e.data); };
    if (ch.unref) ch.unref();   // node (the unit tests)
  });

  function watchSocket() {
    var ws = api() && api()._webSocket;
    if (!ws || ws === FAV.socket || typeof ws.addEventListener !== 'function') return;
    var reconnect = !!FAV.socket;
    FAV.socket = ws;
    ws.addEventListener('message', onSocketMessage);
    // Messages sent while it was down are lost: read the lists again.
    if (reconnect && FAV.active) scheduleLoad(0);
  }

  function socketOpen() {
    var ws = api() && api()._webSocket;
    return !!ws && ws === FAV.socket && ws.readyState === 1;
  }

  function onSocketMessage(e) {
    if (e.target && e.target !== FAV.socket) return;
    var msg;
    try { msg = JSON.parse(e.data); } catch (x) { return; }
    if (!msg || (msg.MessageType !== 'UserDataChanged' && msg.MessageType !== 'LibraryChanged')) return;
    var copy = { MessageType: msg.MessageType, Data: msg.MessageType === 'UserDataChanged' ? msg.Data : null };
    if (relay) { try { relay.postMessage(copy); } catch (x) { /* closed */ } }
    onMessage(copy);
  }

  function onMessage(msg) {
    if (!FAV.active || !msg) return;
    if (msg.MessageType === 'UserDataChanged') {
      var d = msg.Data;
      learnUser();
      if (!d || (FAV.userId && normId(d.UserId) !== normId(FAV.userId))) return;
      var favChange = (d.UserDataList || []).some(function (u) { return typeof u.IsFavorite === 'boolean'; });
      if (favChange) scheduleLoad(150);
    } else if (msg.MessageType === 'LibraryChanged') {
      // a channel renamed, a YouTube channel followed, an item gone
      scheduleLoad(1500);
    }
  }

  function onTick() {
    watchSocket();
    if (!FAV.active) return;
    // No websocket (blocked, or reconnecting): read the lists every 10 s.
    if (!socketOpen() && !document.hidden && Date.now() - FAV.load.at >= POLL_MS) scheduleLoad(0);
  }

  // Back to the tab after a while: read again at once (a hidden tab may have
  // missed what happened meanwhile).
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && FAV.active && Date.now() - FAV.load.at >= 5000) scheduleLoad(0);
  });

  // ── Navigation listeners ───────────────────────────────────────────

  function onRouteChange() {
    if (isFavoritesPage()) {
      // Small delay to let Jellyfin finish its view transition
      setTimeout(activate, 50);
    } else {
      deactivate();
    }
  }

  document.addEventListener('viewshow', onRouteChange);
  window.addEventListener('hashchange', onRouteChange);
  window.addEventListener('popstate', onRouteChange);

  // Every tab listens (and relays), whichever page it is on.
  FAV.tick = setInterval(onTick, 2000);
  if (FAV.tick && FAV.tick.unref) FAV.tick.unref();   // node (the unit tests)

  // Public API — lets other Tentacle overlays dismiss this one (it sits at
  // z-index 900, same layer as the Live TV overlay)
  window.TentacleFavorites = {
    isActive: function () { return FAV.active; },
    hide: function () { deactivate(); },
    // For the unit tests (tests/test_favorites_rows.py)
    internals: { FAV: FAV, ROWS: ROWS, classify: classify, planRows: planRows, rowHtml: rowHtml, onMessage: onMessage,
                 fetchAllPages: fetchAllPages, unfavorite: unfavorite, scheduleLoad: scheduleLoad },
  };

  // Initial check (deep link / refresh straight onto the favorites route)
  if (isFavoritesPage()) {
    setTimeout(activate, 100);
  }
})();
