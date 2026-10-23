// Tentacle Search — Unified search replacing Jellyfin's native results with TMDB
//
// Architecture (Moonfin pattern — body-level overlay, immune to view transitions):
//   - #tentacleSearchResults mounted on document.body (stable, outside Jellyfin views)
//   - Body class `tentacle-search-active` hides ALL native search results in ALL views
//   - viewshow + hashchange + popstate for navigation detection
//   - Generation counter cancels stale API responses on rapid navigation
//   - Scoped input finding: only searches the active (visible) view for the native input
//   - On navigation away: cleanup() removes element + body class + listeners
(function () {
  'use strict';

  var SEARCH = {
    active: false,
    debounceTimer: null,
    lastQuery: '',
    mediaFilter: 'all',
    results: [],
    inputObserver: null,
    nativeInput: null,
    hideStyle: null,
    generation: 0,       // incremented on every nav, stale searches check this
    _onInputChange: null, // bound listener ref for cleanup
    musicShow: false,    // Tentacle's music module + its Jellyfin music integration are on
    musicSeq: 0,
    searchSeq: 0,        // the newest search; older answers are dropped
    channels: [],
    channelTotal: 0,
    error: '',
    nativeFor: null,     // a kids profile's user id: Jellyfin's own search for them (#773)
  };

  function apiGet(path) {
    return window.ApiClient.getJSON(window.ApiClient.getUrl(path));
  }

  function apiPost(path, body) {
    return window.ApiClient.fetch({
      url: window.ApiClient.getUrl(path),
      type: 'POST',
      dataType: 'json',
      contentType: 'application/json',
      data: JSON.stringify(body),
      headers: { accept: 'application/json' }
    }).catch(function (err) {
      // A non-2xx rejects with the raw Response: surface Tentacle's `detail`.
      if (err && typeof err.json === 'function') {
        var status = err.status;
        return err.json().catch(function () { return null; }).then(function (b) {
          var e = new Error((b && b.detail) || ('Request failed (HTTP ' + status + ')'));
          e.status = status;
          throw e;
        });
      }
      throw (err instanceof Error) ? err : new Error('Could not reach the server');
    });
  }

  function musicUrl(path) {
    return 'TentacleMusic/' + path + (path.indexOf('?') === -1 ? '?' : '&') + 'userId=' + window.ApiClient.getCurrentUserId();
  }

  function esc(str) {
    if (!str) return '';
    var d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  // Attribute-context escaping (escapes quotes, unlike esc()).
  function escAttr(str) {
    if (str == null) return '';
    return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Validate + encode a URL for use in an attribute. Allows http(s) and
  // root-relative paths (Tentacle/Jellyfin proxies); rejects anything else
  // (e.g. javascript: / data:). Returns '' if not allowed.
  function safeUrl(url) {
    if (!url) return '';
    var s = String(url).trim();
    if (s.charAt(0) === '/') return encodeURI(s);
    if (/^https?:\/\//i.test(s)) return encodeURI(s);
    return '';
  }

  // ── Route detection ──────────────────────────────────────────────────

  function isSearchPage() {
    var h = (location.hash || '').replace('#', '').replace(/^\//, '').split('?')[0];
    if (h === 'search' || h === 'search.html') return true;
    // Newer jellyfin-web builds can route without the hash — check the path too
    var p = (location.pathname || '').replace(/\/+$/, '');
    return /\/search$/.test(p);
  }

  // ── Find the ACTIVE view's search input (not stale views) ──────────
  // Jellyfin SPA keeps old views in the DOM. We must only look in the
  // currently visible view to avoid attaching to a stale hidden input.
  function findActiveView() {
    // Jellyfin marks the active view — try multiple strategies
    var views = document.querySelectorAll('.view');
    for (var i = views.length - 1; i >= 0; i--) {
      var v = views[i];
      // Active view is the last visible one — use getComputedStyle to catch
      // both inline styles AND CSS class-based hiding (not just inline style)
      if (v.offsetParent !== null || getComputedStyle(v).display !== 'none') {
        return v;
      }
    }
    // Fallback: last view in DOM (most recently added = active in Jellyfin SPA)
    return views.length ? views[views.length - 1] : null;
  }

  function findSearchInputIn(scope) {
    if (!scope) return null;
    // Legacy jellyfin-web search page markup
    var legacy = scope.querySelector('.searchfields-txtSearch')
      || scope.querySelector('input.emby-input[type="text"][data-action="search"]')
      || scope.querySelector('.searchPage input[type="text"]')
      || scope.querySelector('[data-type="search"] input[type="text"]');
    if (legacy) return legacy;
    // Newer (React/MUI) jellyfin-web search page — markup changed entirely.
    // Skip Tentacle's own inputs (discover overlay etc.) when scanning broadly.
    var cands = scope.querySelectorAll('input[type="search"], input.MuiInputBase-input, input[placeholder="Search"]');
    for (var i = 0; i < cands.length; i++) {
      var c = cands[i];
      if (c.closest('#tentacleSearchResults')) continue;
      if (c.closest('[id^="tentacle"], [id^="mh-"], [class^="md-"], [class*=" md-"]')) continue;
      return c;
    }
    return null;
  }

  function findNativeSearchInput() {
    var view = findActiveView();
    var scope = view || document;
    return findSearchInputIn(scope);
  }

  // ── Navigation Handlers ─────────────────────────────────────────────

  function onViewShow(e) {
    var type = e.detail && e.detail.type;
    var viewEl = e.target || null;
    if (type === 'search' || (!type && isSearchPage())) {
      onSearchPage(viewEl);
    } else if (isSearchPage()) {
      onSearchPage(viewEl);
    } else {
      onLeavingSearch();
    }
  }

  function onNavChange() {
    if (isSearchPage()) {
      onSearchPage(null);
    } else {
      onLeavingSearch();
    }
  }

  // ── Search Page Entry ─────────────────────────────────────────────

  function onSearchPage(viewEl) {
    // Dismiss other Tentacle overlays — only one visible at a time
    if (window.TentacleDiscover && window.TentacleDiscover.isActive && window.TentacleDiscover.isActive()) window.TentacleDiscover.hide();
    if (window.TentacleActivity && window.TentacleActivity.isActive && window.TentacleActivity.isActive()) window.TentacleActivity.hide();

    // If already active with a live container, just make sure it's visible
    if (SEARCH.active && document.getElementById('tentacleSearchResults')) return;

    // Clean any stale state first
    cleanup();

    // No gate: Tentacle clients always show OUR search. If the backend is
    // unreachable, doSearch renders an error state inside our UI instead of
    // silently reverting to Jellyfin's native search.
    waitForSearchInput(viewEl);
  }

  function onLeavingSearch() {
    SEARCH.generation++;
    cleanup();
  }

  function waitForSearchInput(viewEl) {
    var input = findSearchInputIn(viewEl) || findNativeSearchInput();
    if (input) {
      attachToInput(input);
      return;
    }
    // Wait for Jellyfin to render the search input — scope observer to active view
    if (SEARCH.inputObserver) SEARCH.inputObserver.disconnect();

    var observeTarget = viewEl || findActiveView() || document.body;

    var found = false;

    SEARCH.inputObserver = new MutationObserver(function () {
      if (found) return;
      var inp = findSearchInputIn(viewEl) || findNativeSearchInput();
      if (inp) {
        found = true;
        SEARCH.inputObserver.disconnect();
        SEARCH.inputObserver = null;
        attachToInput(inp);
      }
    });
    SEARCH.inputObserver.observe(observeTarget, { childList: true, subtree: true });

    // Polling fallback — MutationObserver misses cached/reused views where no
    // DOM changes occur. Poll every 200ms for up to 3 seconds.
    var pollAttempts = 0;
    var pollTimer = setInterval(function () {
      pollAttempts++;
      if (found || pollAttempts > 15) {
        clearInterval(pollTimer);
        return;
      }
      var inp = findSearchInputIn(viewEl) || findNativeSearchInput();
      if (inp) {
        found = true;
        clearInterval(pollTimer);
        if (SEARCH.inputObserver) {
          SEARCH.inputObserver.disconnect();
          SEARCH.inputObserver = null;
        }
        attachToInput(inp);
      }
    }, 200);

    // Safety timeout — don't observe forever
    setTimeout(function () {
      if (SEARCH.inputObserver) {
        SEARCH.inputObserver.disconnect();
        SEARCH.inputObserver = null;
        if (!found) {
          console.warn('[TentacleSearch] Native search kept: could not find the search input after 10s — this jellyfin-web version may use markup we do not recognize. Please report your Jellyfin version.');
        }
      }
    }, 10000);
  }

  // ── Attach to native input ───────────────────────────────────────────

  function currentUserId() {
    return window.ApiClient && window.ApiClient.getCurrentUserId ? window.ApiClient.getCurrentUserId() : null;
  }

  function attachToInput(input) {
    // A kids profile keeps Jellyfin's own search (see doSearch: data.kids).
    if (SEARCH.active || (SEARCH.nativeFor && SEARCH.nativeFor === currentUserId())) return;
    SEARCH.active = true;
    SEARCH.nativeInput = input;

    var gen = ++SEARCH.generation;

    // Hide ALL native search results via body class (CSS handles the rest)
    injectHideCSS();

    // Create our results container on document.body (stable, immune to view transitions)
    getOrCreateContainer();
    placeResults();
    // Jellyfin's page and Tentacle's header can still be settling: place it again
    [80, 400, 1200].forEach(function (ms) { setTimeout(function () { if (gen === SEARCH.generation) placeResults(); }, ms); });
    window.addEventListener('resize', placeResults);

    SEARCH._onInputChange = function (e) { onInputChange(e, gen); };
    input.addEventListener('input', SEARCH._onInputChange);

    var existing = input.value.trim();
    if (existing) {
      SEARCH.lastQuery = existing;
      doSearch(existing, gen);
    }
  }

  function onInputChange(e, gen) {
    // Stale check — did navigation happen since we attached?
    if (gen !== SEARCH.generation) return;

    var q = e.target.value.trim();
    if (SEARCH.debounceTimer) clearTimeout(SEARCH.debounceTimer);

    if (!q) {
      SEARCH.lastQuery = '';
      clearResults();
      return;
    }

    SEARCH.debounceTimer = setTimeout(function () {
      if (gen !== SEARCH.generation) return;
      if (q !== SEARCH.lastQuery) {
        SEARCH.lastQuery = q;
        doSearch(q, gen);
      }
    }, 400);
  }

  // ── CSS injection to nuke ALL native search results ──────────────────
  // Uses body class so it hides native results in ALL views (stale or fresh)

  function injectHideCSS() {
    if (SEARCH.hideStyle) return;
    document.body.classList.add('tentacle-search-active');
    var style = document.createElement('style');
    style.id = 'tentacleSearchHideNative';
    style.textContent = [
      // Native search result cards
      'body.tentacle-search-active .card.overflowPortraitCard { display: none !important; }',
      'body.tentacle-search-active .card.overflowBackdropCard { display: none !important; }',
      'body.tentacle-search-active .card.overflowSquareCard { display: none !important; }',
      // Native section headers
      'body.tentacle-search-active .verticalSection > .sectionTitle { display: none !important; }',
      // Native "no results" messages
      'body.tentacle-search-active .noItemsMessage { display: none !important; }',
      'body.tentacle-search-active .emby-scroller-alert { display: none !important; }',
      // Catch-all for Jellyfin messages
      'body.tentacle-search-active .searchPage .padded-left, body.tentacle-search-active .searchPage .padded-right { display: none !important; }',
      // Hide native itemsContainers
      'body.tentacle-search-active .itemsContainer:not(#tentacleSearchGrid) { display: none !important; }',
      // Hide native vertical sections
      'body.tentacle-search-active .verticalSection { display: none !important; }',
      // Hide native search label/icon that may overlap
      'body.tentacle-search-active .searchTabButton { display: none !important; }',
      'body.tentacle-search-active .headerSearchButton { display: none !important; }',
      // Our container always visible
      '#tentacleSearchResults { display: block !important; }',
    ].join('\n');
    document.head.appendChild(style);
    SEARCH.hideStyle = style;
  }

  function removeHideCSS() {
    document.body.classList.remove('tentacle-search-active');
    if (SEARCH.hideStyle) {
      SEARCH.hideStyle.remove();
      SEARCH.hideStyle = null;
    }
  }

  // ── Container — mounted on document.body (stable parent) ──────────

  function getOrCreateContainer() {
    var existing = document.getElementById('tentacleSearchResults');
    if (existing) return existing;

    var container = document.createElement('div');
    container.id = 'tentacleSearchResults';
    container.className = 'tentacle-search-section';
    container.innerHTML =
      '<div class="tentacle-search-header">' +
        '<div class="tentacle-search-filters">' +
          '<button class="tentacle-search-filter-btn ts-active" data-tstype="all">All</button>' +
          '<button class="tentacle-search-filter-btn" data-tstype="movies">Movies</button>' +
          '<button class="tentacle-search-filter-btn" data-tstype="series">TV Shows</button>' +
          '<button class="tentacle-search-filter-btn" data-tstype="channels">Live TV</button>' +
          '<button class="tentacle-search-filter-btn" data-tstype="music" id="tsMusicFilterBtn" style="display:none">Music</button>' +
        '</div>' +
      '</div>' +
      '<div id="tentacleSearchGrid">' + hintHtml() + '</div>';

    // Single delegated click handler for the entire container
    container.addEventListener('click', function (e) {
      if (onMusicClick(e)) return;
      // Filter button click
      var filterBtn = e.target.closest('.tentacle-search-filter-btn');
      if (filterBtn) {
        container.querySelectorAll('.tentacle-search-filter-btn').forEach(function (x) { x.classList.remove('ts-active'); });
        filterBtn.classList.add('ts-active');
        SEARCH.mediaFilter = filterBtn.getAttribute('data-tstype');
        if (SEARCH.lastQuery) doSearch(SEARCH.lastQuery, SEARCH.generation);
        return;
      }

      // Live TV channel: Jellyfin's own channel (the search asked Jellyfin), played by id
      var channelCard = e.target.closest('.ts-channel-card');
      if (channelCard) {
        e.preventDefault();
        e.stopPropagation();
        playChannel(channelCard.getAttribute('data-channel-id'));
        return;
      }

      // "See all" under the Live TV row: the Live TV filter
      var more = e.target.closest('.ts-section-more');
      if (more) {
        var btn = container.querySelector('.tentacle-search-filter-btn[data-tstype="channels"]');
        if (btn) btn.click();
        return;
      }

      // Card click
      var card = e.target.closest('.ts-card');
      if (card) {
        e.preventDefault();
        e.stopPropagation();
        var tmdb = parseInt(card.getAttribute('data-tmdb'), 10) || 0;
        var tvdb = parseInt(card.getAttribute('data-tvdb'), 10) || 0;
        var item = findItem(tmdb, tvdb);
        console.log('[TentacleSearch] Card clicked, tmdb=' + tmdb, 'tvdb=' + tvdb, 'item=', item);
        if (!item) return;
        if (item.in_library) {
          goToLibraryItem(item);
        } else {
          openModal(item);
        }
      }
    });

    // Mount on document.body — outside any Jellyfin view element
    // This is the key architectural fix: our container can never be trapped
    // in a stale hidden view on SPA navigation
    document.body.appendChild(container);
    loadMusicConfig();

    return container;
  }

  // The results start right under Jellyfin's search box, wherever it is at this
  // width (a fixed offset put the filters on top of the box on some widths).
  function placeResults() {
    var c = document.getElementById('tentacleSearchResults');
    var inp = SEARCH.nativeInput;
    if (!c || !inp || !inp.getBoundingClientRect) return;
    var box = (inp.closest && inp.closest('.searchFields')) || inp;
    var r = box.getBoundingClientRect();
    if (!r.height) return;
    c.style.top = Math.round(r.bottom + 4) + 'px';
  }

  // ── Search ───────────────────────────────────────────────────────────

  // The page before anything is typed: say what it finds, so it never looks empty.
  function hintHtml() {
    return '<div class="tentacle-search-hint">Find movies, shows and Live TV channels</div>';
  }

  var CHANNEL_ROW = 8;   // Live TV cards shown under "All" (one row); the Live TV filter shows them all

  // Live TV channels come from Jellyfin, with the person's own token: the names,
  // numbers and logos their guide shows, only channels they may watch, and the
  // id to play. (Tentacle's channel list holds the providers' raw stream names,
  // which Jellyfin doesn't know: a click found nothing.)
  function searchChannels(query) {
    var userId = currentUserId();
    return apiGet('Users/' + userId + '/Items?SearchTerm=' + encodeURIComponent(query) +
      '&IncludeItemTypes=LiveTvChannel&Recursive=true&Limit=60&Fields=ChannelInfo&EnableUserData=false')
      .then(function (data) { return { items: (data && data.Items) || [], total: (data && data.TotalRecordCount) || 0 }; });
  }

  function doSearch(query, gen) {
    getOrCreateContainer();
    var grid = document.getElementById('tentacleSearchGrid');
    if (!grid) return;

    if (SEARCH.mediaFilter === 'music') { doMusicSearch(query, gen, grid); return; }
    var seq = ++SEARCH.searchSeq;
    var filter = SEARCH.mediaFilter;
    var wantChannels = filter === 'all' || filter === 'channels';
    var wantTitles = filter !== 'channels';
    // null = still asking; an array = answered
    SEARCH.channels = wantChannels ? null : [];
    SEARCH.channelTotal = 0;
    SEARCH.results = wantTitles ? null : [];
    SEARCH.error = '';
    render(query);

    function stale() { return gen !== SEARCH.generation || seq !== SEARCH.searchSeq; }

    if (wantChannels) {
      searchChannels(query).then(function (r) {
        if (stale()) return;
        SEARCH.channels = r.items;
        SEARCH.channelTotal = r.total;
        render(query);
      }).catch(function () {
        if (stale()) return;
        SEARCH.channels = [];
        render(query);
      });
    }

    if (!wantTitles) return;
    apiGet('TentacleDiscover/Search?q=' + encodeURIComponent(query) + '&type=' + filter)
      .then(function (data) {
        // Stale check — did user navigate away or type on during the API call?
        if (stale()) return;

        // A kids profile: Tentacle's search is TMDB's catalogue, not theirs.
        // Jellyfin's own search (their age limit applied) shows instead, for
        // this person until the page reloads (#773).
        if (data && data.kids) {
          SEARCH.nativeFor = currentUserId();
          SEARCH.generation++;
          cleanup();
          return;
        }

        // Channels come from Jellyfin (searchChannels), not from this answer.
        SEARCH.results = ((data && data.items) || []).filter(function (it) { return it.media_type !== 'channel'; });
        // {error, message}: Tentacle couldn't be asked (busy, not set up) —
        // not the same as nothing matching.
        if (!SEARCH.results.length && data && data.error && data.message) SEARCH.error = String(data.message);
        render(query);
      })
      .catch(function () {
        if (stale()) return;
        // Tentacle search is the only search — show an honest error state in
        // our UI instead of silently reverting to Jellyfin's native results.
        SEARCH.results = [];
        SEARCH.error = 'Search is unavailable — the Tentacle server could not be reached.';
        render(query);
      });
  }

  // ── Render ───────────────────────────────────────────────────────────

  function render(query) {
    var grid = document.getElementById('tentacleSearchGrid');
    if (!grid) return;
    var filter = SEARCH.mediaFilter;
    var chans = SEARCH.channels, titles = SEARCH.results;
    var loading = '<div class="tentacle-search-loading"><div class="md-spinner"></div>Searching...</div>';
    var html = '';

    if (filter === 'channels') {
      if (chans === null) { grid.innerHTML = loading; return; }
      grid.innerHTML = chans.length
        ? '<div class="tentacle-search-grid ts-channel-grid">' + chans.map(channelCard).join('') + '</div>'
        : '<div class="tentacle-search-empty">No Live TV channels match “' + esc(query) + '”</div>';
      return;
    }

    if (filter === 'all' && chans && chans.length) {
      var shown = chans.slice(0, CHANNEL_ROW);
      var total = Math.max(SEARCH.channelTotal || 0, chans.length);
      html += '<div class="ts-section"><div class="ts-section-head"><span class="ts-section-title">Live TV</span>' +
        (total > shown.length ? '<button class="ts-section-more">See all ' + total + '</button>' : '') + '</div>' +
        '<div class="tentacle-search-grid ts-channel-grid ts-channel-row">' + shown.map(channelCard).join('') + '</div></div>';
    }

    if (titles === null) {
      html += loading;
    } else if (SEARCH.error) {
      // Tentacle couldn't answer (refused, unreachable): say why, under any channels
      html += '<div class="tentacle-search-empty">' + esc(SEARCH.error) + '</div>';
    } else if (titles.length) {
      html += '<div class="ts-section">' +
        (filter === 'all' && chans && chans.length ? '<div class="ts-section-head"><span class="ts-section-title">Movies &amp; Shows</span></div>' : '') +
        titlesGrid(titles) + '</div>';
    } else if (chans !== null && !(filter === 'all' && chans.length)) {
      html += '<div class="tentacle-search-empty">' + 'No results for \u201c' + esc(query) + '\u201d' + '</div>';
    }
    grid.innerHTML = html;
  }

  function channelCard(ch) {
    var tag = ch.ImageTags && ch.ImageTags.Primary;
    var logo = tag ? window.ApiClient.getScaledImageUrl(ch.Id, { type: 'Primary', maxWidth: 320, tag: tag }) : '';
    var logoHtml = logo
      ? '<img src="' + escAttr(logo) + '" loading="lazy" alt="" onerror="this.style.display=\'none\'">'
      : '<div class="ts-channel-initial">' + esc((ch.Name || '?').charAt(0)) + '</div>';
    return '<div class="ts-card ts-channel-card" data-channel-id="' + escAttr(ch.Id) + '" title="' + escAttr(ch.Name) + '">' +
      '<div class="ts-channel-logo">' + logoHtml + '</div>' +
      '<div class="ts-card-info">' +
        '<div class="ts-card-title">' + esc(ch.Name) + '</div>' +
        '<div class="ts-card-meta">' + (ch.ChannelNumber ? 'Ch ' + esc(String(ch.ChannelNumber)) : 'Live TV') + '</div>' +
      '</div></div>';
  }

  function playChannel(id) {
    if (!id) return;
    var pm = window.playbackManager;
    if (pm && typeof pm.play === 'function') {
      cleanup();
      pm.play({ ids: [id], serverId: window.ApiClient.serverId() });
    } else {
      window.location.hash = '#/details?id=' + encodeURIComponent(id);
    }
  }

  function titlesGrid(items) {
    var getDownloadInfo = (window.TentacleDiscover && window.TentacleDiscover.getDownloadInfo)
      ? window.TentacleDiscover.getDownloadInfo
      : function () { return null; };

    return '<div class="tentacle-search-grid">' +
      items.map(function (item) {
        // TMDB movie/series card
        var posterUrl = item.poster_path
          ? (item.poster_path.startsWith('http') || item.poster_path.startsWith('/TentacleDiscover/') ? item.poster_path : 'https://image.tmdb.org/t/p/w342' + item.poster_path)
          : '';
        var posterSafe = safeUrl(posterUrl);
        var posterHtml = posterSafe
          ? '<img src="' + escAttr(posterSafe) + '" loading="lazy" onerror="this.style.display=\'none\'">'
          : '<div class="ts-card-poster-placeholder">&#9707;</div>';

        var dlInfo = getDownloadInfo(item.tmdb_id);
        var isMovie = (item.media_type || 'movie') !== 'series';
        var typeBadge = '<div class="ts-card-badge ts-badge-type">' + (isMovie ? 'Movie' : 'TV') + '</div>';
        var statusBadge = '';

        if (dlInfo) {
          var pct = (dlInfo.progress || 0).toFixed(1);
          var statusText = dlInfo.status === 'importing' ? 'Importing' : dlInfo.status === 'queued' ? 'Queued' : dlInfo.status === 'stuck' ? 'Stuck' : dlInfo.status === 'import_blocked' ? 'Import blocked' : 'Downloading ' + pct + '%';
          statusBadge = '<div class="ts-card-badge ts-badge-status ts-badge-downloading">' + statusText + '</div>';
        } else if (item.in_library) {
          statusBadge = '<div class="ts-card-badge ts-badge-status ts-badge-inlib">In Library</div>';
        } else if (item.requested) {
          statusBadge = '<div class="ts-card-badge ts-badge-status ts-badge-requested">' + (item.media_type === 'series' ? 'In Sonarr' : 'In Radarr') + '</div>';
        }

        var ratingHtml = item.rating
          ? '<span class="ts-card-meta-rating">\u2605 ' + esc(String(item.rating)) + '</span>'
          : '';
        var year = parseInt(item.year, 10);   // "0" = unknown, as TVDB-only shows send it
        var yearHtml = year > 0 ? String(year) : '\u2014';
        var sep = item.rating ? ' \u00b7 ' : '';

        return '<div class="ts-card" data-tmdb="' + escAttr(item.tmdb_id || 0) + '" data-tvdb="' + escAttr(item.tvdb_id || 0) + '" data-type="' + escAttr(item.media_type || 'movie') + '">' +
          '<div class="ts-card-poster">' + posterHtml + typeBadge + statusBadge + '</div>' +
          '<div class="ts-card-info">' +
            '<div class="ts-card-title">' + esc(item.title) + '</div>' +
            '<div class="ts-card-meta">' + yearHtml + sep + ratingHtml + '</div>' +
          '</div></div>';
      }).join('') +
    '</div>';
  }

  // ── Navigation ───────────────────────────────────────────────────────

  function findItem(tmdbId, tvdbId) {
    for (var i = 0; i < SEARCH.results.length; i++) {
      var r = SEARCH.results[i];
      if (tmdbId && r.tmdb_id === tmdbId) return r;
      if (tvdbId && r.tvdb_id === tvdbId) return r;
    }
    return null;
  }

  function openModal(item) {
    console.log('[TentacleSearch] openModal called, TentacleDiscover=', !!window.TentacleDiscover, 'showDetailModal=', !!(window.TentacleDiscover && window.TentacleDiscover.showDetailModal));
    if (window.TentacleDiscover && window.TentacleDiscover.showDetailModal) {
      window.TentacleDiscover.showDetailModal(item);
    } else if (window.TentacleDetails && window.TentacleDetails.show && item.jellyfin_id) {
      window.TentacleDetails.show(item.jellyfin_id, item.media_type === 'series' ? 'Series' : 'Movie');
    } else {
      console.warn('[TentacleSearch] No detail handler available');
    }
  }

  function goToLibraryItem(item) {
    var userId = window.ApiClient.getCurrentUserId();
    var itemType = item.media_type === 'series' ? 'Series' : 'Movie';
    console.log('[TentacleSearch] goToLibraryItem called for:', item.title, 'type:', itemType);
    apiGet('Users/' + userId + '/Items?searchTerm=' + encodeURIComponent(item.title) +
      '&IncludeItemTypes=' + itemType + '&Recursive=true&Limit=10&Fields=ProviderIds')
      .then(function (result) {
        var items = (result && result.Items) || [];
        console.log('[TentacleSearch] Library search returned', items.length, 'items');
        var match = null;
        for (var i = 0; i < items.length; i++) {
          if ((items[i].Name || '').toLowerCase() === (item.title || '').toLowerCase()) {
            match = items[i];
            break;
          }
        }
        if (!match && items.length) match = items[0];
        if (match) {
          console.log('[TentacleSearch] Opening details for:', match.Name, match.Id);
          if (window.TentacleDetails && window.TentacleDetails.show) {
            window.TentacleDetails.show(match.Id, match.Type);
          } else {
            window.location.hash = '#/details?id=' + match.Id;
          }
        } else {
          console.warn('[TentacleSearch] No library match found for:', item.title, '- falling back to detail modal');
          openModal(item);
        }
      }).catch(function (err) {
        console.error('[TentacleSearch] goToLibraryItem error:', err);
        openModal(item);
      });
  }

  // ── Cleanup ──────────────────────────────────────────────────────────

  function clearResults() {
    SEARCH.searchSeq++;
    var grid = document.getElementById('tentacleSearchGrid');
    if (grid) grid.innerHTML = hintHtml();
    SEARCH.results = [];
    SEARCH.channels = [];
  }

  function cleanup() {
    if (SEARCH.debounceTimer) {
      clearTimeout(SEARCH.debounceTimer);
      SEARCH.debounceTimer = null;
    }
    if (SEARCH.inputObserver) {
      SEARCH.inputObserver.disconnect();
      SEARCH.inputObserver = null;
    }
    // Remove input listener from the native input
    if (SEARCH.nativeInput && SEARCH._onInputChange) {
      SEARCH.nativeInput.removeEventListener('input', SEARCH._onInputChange);
      SEARCH._onInputChange = null;
    }
    window.removeEventListener('resize', placeResults);
    removeHideCSS();
    SEARCH.active = false;
    SEARCH.lastQuery = '';
    SEARCH.results = [];
    SEARCH.channels = [];
    SEARCH.mediaFilter = 'all';
    SEARCH.nativeInput = null;
    var el = document.getElementById('tentacleSearchResults');
    if (el) el.remove();
    closeMusicOverlay();
  }

  // ── Music (Tentacle's music module; only with its Jellyfin music integration on) ──

  var MUSIC_STATUS = {
    in_library: ['ts-badge-inlib', 'In Library'],
    downloading: ['ts-badge-downloading', 'Downloading'],
    wanted: ['ts-badge-requested', 'Wanted'],
    needs_review: ['ts-badge-review', 'Needs review']
  };

  function loadMusicConfig() {
    apiGet(musicUrl('Config')).then(function (c) {
      SEARCH.musicShow = !!(c && c.show);
      var btn = document.getElementById('tsMusicFilterBtn');
      if (btn) btn.style.display = SEARCH.musicShow ? '' : 'none';
    }).catch(function () { SEARCH.musicShow = false; });
  }

  function musicBadge(a) {
    var s = MUSIC_STATUS[a.status];
    if (!s) return '<div class="ts-card-badge ts-badge-type">' + esc(a.type || 'Album') + '</div>';
    var label = a.status === 'downloading' && a.progress != null ? 'Downloading ' + a.progress + '%' : s[1];
    return '<div class="ts-card-badge ts-badge-status ' + s[0] + '">' + label + '</div>';
  }

  function musicDuration(ms) {
    if (!ms) return '';
    var t = Math.round(ms / 1000);
    return Math.floor(t / 60) + ':' + ('0' + (t % 60)).slice(-2);
  }

  function musicAlbumCard(a, showArtist) {
    var cover = safeUrl(a.cover);
    return '<div class="ts-card ts-music-card ts-music-album" data-mbid="' + escAttr(a.mbid) + '">' +
      '<div class="ts-card-poster">' + (cover ? '<img src="' + escAttr(cover) + '" loading="lazy" onerror="this.style.display=\'none\'">' : '') +
        '<div class="ts-card-poster-placeholder">&#9835;</div>' + musicBadge(a) + '</div>' +
      '<div class="ts-card-info"><div class="ts-card-title">' + esc(a.title) + '</div>' +
      '<div class="ts-card-meta">' + (showArtist === false ? '' : esc(a.artist || '') + (a.year ? ' \u00b7 ' : '')) + esc(a.year || '') + '</div></div></div>';
  }

  function doMusicSearch(query, gen, grid) {
    var seq = ++SEARCH.musicSeq;
    grid.innerHTML = '<div class="tentacle-search-loading"><div class="md-spinner"></div>Searching MusicBrainz...</div>';
    apiGet(musicUrl('Search?q=' + encodeURIComponent(query))).then(function (d) {
      if (gen !== SEARCH.generation || seq !== SEARCH.musicSeq || !d || d.stale) return;
      var artists = d.artists || [], albums = d.albums || [], songs = d.songs || [];
      if (!artists.length && !albums.length && !songs.length) {
        grid.innerHTML = '<div class="tentacle-search-empty">No music found for \u201c' + esc(query) + '\u201d</div>';
        return;
      }
      var html = '';
      if (artists.length) {
        html += '<div class="ts-music-heading">Artists</div><div class="ts-music-rows">' + artists.map(function (a) {
          return '<div class="ts-music-row ts-music-artist" data-mbid="' + escAttr(a.mbid) + '"><div class="ts-music-icon">' + esc((a.name || '?').charAt(0)) + '</div>' +
            '<div><div class="ts-music-row-title">' + esc(a.name) + '</div><div class="ts-music-row-sub">' +
            esc([a.type, a.country, a.disambiguation].filter(Boolean).join(' \u00b7 ')) + '</div></div></div>';
        }).join('') + '</div>';
      }
      if (albums.length) {
        html += '<div class="ts-music-heading">Albums</div><div class="tentacle-search-grid">' + albums.map(function (a) { return musicAlbumCard(a); }).join('') + '</div>';
      }
      if (songs.length) {
        html += '<div class="ts-music-heading">Songs</div><div class="ts-music-rows">' + songs.map(function (t) {
          return '<div class="ts-music-row ts-music-song" data-title="' + escAttr(t.title) + '" data-artist="' + escAttr(t.artist_mbid) + '"><div class="ts-music-icon">&#9834;</div>' +
            '<div><div class="ts-music-row-title">' + esc(t.title) + '</div><div class="ts-music-row-sub">' + esc(t.artist) +
            (t.length ? ' \u00b7 ' + musicDuration(t.length) : '') + '</div></div></div>';
        }).join('') + '</div>';
      }
      grid.innerHTML = html;
    }).catch(function () {
      if (gen !== SEARCH.generation || seq !== SEARCH.musicSeq) return;
      grid.innerHTML = '<div class="tentacle-search-empty">Music search is unavailable right now.</div>';
    });
  }

  function onMusicClick(e) {
    var req = e.target.closest('[data-ts-music-request]');
    if (req) {
      e.preventDefault(); e.stopPropagation();
      requestMusicAlbum(req.getAttribute('data-ts-music-request'), req, parseInt(req.getAttribute('data-tracks'), 10) || 0);
      return true;
    }
    var open = e.target.closest('[data-ts-music-open]');
    if (open) {
      e.preventDefault(); e.stopPropagation();
      openInJellyfin(open.getAttribute('data-ts-music-open'), open.getAttribute('data-title'));
      return true;
    }
    var album = e.target.closest('.ts-music-album');
    if (album) { e.preventDefault(); e.stopPropagation(); openMusicAlbum(album.getAttribute('data-mbid')); return true; }
    var artist = e.target.closest('.ts-music-artist');
    if (artist) { e.preventDefault(); e.stopPropagation(); openMusicArtist(artist.getAttribute('data-mbid')); return true; }
    var song = e.target.closest('.ts-music-song');
    if (song) { e.preventDefault(); e.stopPropagation(); openMusicSong(song.getAttribute('data-title'), song.getAttribute('data-artist')); return true; }
    return false;
  }

  function musicOverlay(title, html) {
    var ov = document.getElementById('tsMusicOverlay');
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'tsMusicOverlay';
      ov.className = 'ts-music-overlay';
      ov.innerHTML = '<div class="ts-music-modal" role="dialog" aria-modal="true"><div class="ts-music-modal-head">' +
        '<div class="ts-music-modal-title"></div><button class="ts-music-close" aria-label="Close">&#10005;</button></div>' +
        '<div class="ts-music-modal-body"></div></div>';
      ov.addEventListener('click', function (e) {
        if (e.target === ov || e.target.closest('.ts-music-close')) { closeMusicOverlay(); return; }
        onMusicClick(e);
      });
      document.body.appendChild(ov);
    }
    ov.querySelector('.ts-music-modal-title').textContent = title;
    ov.querySelector('.ts-music-modal-body').innerHTML = html;
  }

  function closeMusicOverlay() {
    var ov = document.getElementById('tsMusicOverlay');
    if (ov) ov.remove();
  }

  function musicNorm(s) {
    return (s || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
      .replace(/[\u2018\u2019`\u00b4']/g, '').replace(/[^\w\s]/g, ' ').split(/\s+/).filter(Boolean).join(' ');
  }

  function musicTracklist(discs, highlight) {
    if (!discs || !discs.length) return '';
    return discs.map(function (d) {
      return (discs.length > 1 ? '<div class="ts-music-disc">Disc ' + esc(String(d.position)) + '</div>' : '') +
        '<div class="ts-music-tracks">' + (d.tracks || []).map(function (t) {
          var hl = highlight && ((highlight.recordings || []).indexOf(t.recording_mbid) !== -1 || musicNorm(t.title) === highlight.title);
          return '<div class="ts-music-track' + (hl ? ' ts-hl' : '') + '"><span class="ts-music-n">' + esc(String(t.number || '')) + '</span>' +
            '<span class="ts-music-t">' + esc(t.title) + '</span><span class="ts-music-len">' + musicDuration(t.length) + '</span></div>';
        }).join('') + '</div>';
    }).join('');
  }

  function musicAlbumHtml(a, highlight) {
    var cover = safeUrl(a.cover);
    var actions = '';
    if (a.status === 'available' && !(a.review && (a.review.options || []).length)) actions += '<button class="ts-music-btn ts-primary" data-ts-music-request="' + escAttr(a.mbid) + '">Request</button>';
    if (a.status === 'in_library') actions += '<button class="ts-music-btn" data-ts-music-open="' + escAttr(a.mbid) + '" data-title="' + escAttr(a.title) + '">Open in Jellyfin</button>';
    var badge = MUSIC_STATUS[a.status] ? '<span class="ts-music-pill ' + MUSIC_STATUS[a.status][0] + '">' + MUSIC_STATUS[a.status][1] + '</span>' : '';
    var note = a.verdict && (a.verdict.message || a.verdict.state);
    var html = '<div class="ts-music-album-head">' + (cover ? '<img class="ts-music-cover" src="' + escAttr(cover) + '" onerror="this.style.visibility=\'hidden\'">' : '') +
      '<div><div class="ts-music-row-title"><a href="#" class="ts-music-artist ts-music-link" data-mbid="' + escAttr(a.artist_mbid) + '">' + esc(a.artist) + '</a></div>' +
      '<div class="ts-music-row-sub">' + esc([a.year, a.type].filter(Boolean).join(' \u00b7 ')) + '</div>' + badge +
      (note ? '<div class="ts-music-row-sub" style="margin-top:6px">' + esc(note) + '</div>' : '') +
      (actions ? '<div class="ts-music-actions">' + actions + '</div>' : '') + '</div></div>';
    if (a.original && a.release) {
      html += '<div class="ts-music-original"><strong>Original:</strong> ' + esc(a.original.year) + ' \u00b7 ' + a.original.tracks + ' tracks' +
        (a.original.discs > 1 ? ' \u00b7 ' + a.original.discs + ' discs' : '') + '</div>' + musicTracklist(a.release.tracklist, highlight);
    } else if (a.review) {
      html += '<div class="ts-music-original">' + esc(a.review.message) + ' Tentacle won\u2019t guess: pick one.</div>' +
        (a.review.options || []).map(function (o) {
          return '<div class="ts-music-option"><strong>' + o.tracks + ' tracks</strong> <span class="ts-music-row-sub">' + esc((o.examples || []).join('; ')) + '</span>' +
            (a.status === 'available' ? ' <button class="ts-music-btn" data-ts-music-request="' + escAttr(a.mbid) + '" data-tracks="' + o.tracks + '">Request with ' + o.tracks + ' tracks</button>' : '') +
            musicTracklist(o.tracklist) + '</div>';
        }).join('');
    }
    return html;
  }

  function musicError(err) {
    return '<div class="tentacle-search-empty">' + esc((err && err.message) || 'Tentacle could not load this.') + '</div>';
  }

  function openMusicAlbum(mbid, highlight, intro) {
    var seq = ++SEARCH.musicSeq;
    musicOverlay('Loading\u2026', '<div class="tentacle-search-loading"><div class="md-spinner"></div></div>');
    apiGet(musicUrl('Album/' + encodeURIComponent(mbid))).then(function (a) {
      if (seq !== SEARCH.musicSeq) return;
      musicOverlay(a.title || 'Album', (intro || '') + musicAlbumHtml(a, highlight));
    }).catch(function (err) { if (seq === SEARCH.musicSeq) musicOverlay('Album', musicError(err)); });
  }

  function openMusicArtist(mbid) {
    var seq = ++SEARCH.musicSeq;
    musicOverlay('Loading\u2026', '<div class="tentacle-search-loading"><div class="md-spinner"></div></div>');
    apiGet(musicUrl('Artist/' + encodeURIComponent(mbid))).then(function (a) {
      if (seq !== SEARCH.musicSeq) return;
      var other = (a.other || []);
      musicOverlay(a.name || 'Artist',
        '<div class="ts-music-row-sub">' + esc([a.type, a.country, a.years, a.disambiguation].filter(Boolean).join(' \u00b7 ')) + '</div>' +
        '<div class="ts-music-heading">Studio albums</div><div class="tentacle-search-grid">' + (a.studio || []).map(function (c) { return musicAlbumCard(c, false); }).join('') + '</div>' +
        (other.length ? '<details class="ts-music-more"><summary>Show all releases (' + other.length + ')</summary><div class="tentacle-search-grid">' +
          other.map(function (c) { return musicAlbumCard(c, false); }).join('') + '</div></details>' : ''));
    }).catch(function (err) { if (seq === SEARCH.musicSeq) musicOverlay('Artist', musicError(err)); });
  }

  function openMusicSong(title, artistMbid) {
    var seq = ++SEARCH.musicSeq;
    musicOverlay(title, '<div class="tentacle-search-loading"><div class="md-spinner"></div>Finding the original album...</div>');
    apiGet(musicUrl('Song?title=' + encodeURIComponent(title) + '&artist=' + encodeURIComponent(artistMbid))).then(function (s) {
      if (seq !== SEARCH.musicSeq) return;
      if (s.album) {
        musicOverlay(s.album.title || title, '<div class="ts-music-original">\u201c' + esc(title) + '\u201d first appeared on this studio album.</div>' + musicAlbumHtml(s.album, s.highlight));
      } else {
        musicOverlay(title, '<div class="tentacle-search-empty">' + esc(s.message || '') + '</div>');
      }
    }).catch(function (err) { if (seq === SEARCH.musicSeq) musicOverlay(title, musicError(err)); });
  }

  function requestMusicAlbum(mbid, btn, tracks) {
    btn.disabled = true;
    var label = btn.textContent;
    btn.textContent = 'Requesting\u2026';
    var body = { mbid: mbid };
    if (tracks) body.tracks = tracks;
    apiPost(musicUrl('Request'), body).then(function () {
      btn.textContent = '\u2713 Requested';
    }).catch(function (err) {
      btn.disabled = false;
      btn.textContent = label;
      var note = document.createElement('div');
      note.className = 'ts-music-row-sub ts-music-err';
      note.textContent = (err && err.message) || 'The request failed.';
      btn.parentNode.appendChild(note);
    });
  }

  function openInJellyfin(mbid, title) {
    var userId = window.ApiClient.getCurrentUserId();
    apiGet('Users/' + userId + '/Items?IncludeItemTypes=MusicAlbum&Recursive=true&Fields=ProviderIds&Limit=50&SearchTerm=' + encodeURIComponent(title || ''))
      .then(function (data) {
        var items = (data && data.Items) || [];
        var match = null;
        for (var i = 0; i < items.length && !match; i++) {
          var ids = items[i].ProviderIds || {};
          if (ids.MusicBrainzReleaseGroup === mbid || ids.MusicBrainzAlbum === mbid) match = items[i];
        }
        if (!match) {
          var t = musicNorm(title);
          for (var j = 0; j < items.length && !match; j++) if (musicNorm(items[j].Name) === t) match = items[j];
        }
        if (match) {
          closeMusicOverlay();
          cleanup();
          window.location.hash = '#/details?id=' + match.Id;
        }
      }).catch(function () {});
  }

  // ── Bootstrap ────────────────────────────────────────────────────────

  function init() {
    if (window.TentacleStandAside && window.TentacleStandAside()) return; // TV layout — native search renders
    // Primary: viewshow — Jellyfin's own SPA navigation event (most reliable)
    document.addEventListener('viewshow', onViewShow);

    // Fallback: hashchange + popstate for edge cases (browser back/forward)
    window.addEventListener('hashchange', onNavChange);
    window.addEventListener('popstate', onNavChange);

    // Handle initial page load
    if (isSearchPage()) {
      onSearchPage(null);
    }
  }

  function waitForReady() {
    if (window.ApiClient) {
      init();
    } else {
      var attempts = 0;
      var timer = setInterval(function () {
        attempts++;
        if (window.ApiClient) {
          clearInterval(timer);
          init();
        } else if (attempts > 100) {
          clearInterval(timer);
        }
      }, 200);
    }
  }

  // ── Public API ──────────────────────────────────────────────────────
  window.TentacleSearch = {
    hide: function () { if (SEARCH.active) { SEARCH.generation++; cleanup(); } },
    isActive: function () { return SEARCH.active; },
    activate: function () {
      if (!SEARCH.active && isSearchPage()) {
        console.log('[TS] activate() called — re-initializing search');
        onSearchPage(null);
      }
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', waitForReady);
  } else {
    waitForReady();
  }

})();
