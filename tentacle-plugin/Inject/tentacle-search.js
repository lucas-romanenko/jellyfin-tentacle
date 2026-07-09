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
    configEnabled: null,  // null = unknown, true/false once config fetched
  };

  function apiGet(path) {
    return window.ApiClient.getJSON(window.ApiClient.getUrl(path));
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

    // Gate the hijack on the discover/config enabled flag. If Tentacle search is
    // disabled (or config fetch fails), leave Jellyfin's native search untouched.
    checkConfigThen(function (enabled) {
      if (!enabled) {
        console.log('[TentacleSearch] Native search kept: Tentacle backend unreachable (check the plugin TentacleUrl setting).');
        return;
      }
      if (!isSearchPage()) return; // navigated away while fetching config
      waitForSearchInput(viewEl);
    });
  }

  // Reachability check (cached for the session): the unified search needs the
  // Tentacle backend for results, so keep native search when it's unreachable.
  // The backend now always returns discover_in_jellyfin=true (the global toggle
  // is retired — per-user toolbar config governs tab visibility), and the C#
  // proxy returns false when the backend is unreachable/unconfigured — so the
  // flag value IS the reachability signal.
  function checkConfigThen(cb) {
    if (SEARCH.configEnabled !== null) { cb(SEARCH.configEnabled); return; }
    apiGet('TentacleDiscover/Config').then(function (cfg) {
      SEARCH.configEnabled = !!(cfg && cfg.discover_in_jellyfin === true);
      cb(SEARCH.configEnabled);
    }).catch(function () {
      SEARCH.configEnabled = false;
      cb(false);
    });
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

  function attachToInput(input) {
    if (SEARCH.active) return;
    SEARCH.active = true;
    SEARCH.nativeInput = input;

    var gen = ++SEARCH.generation;

    // Hide ALL native search results via body class (CSS handles the rest)
    injectHideCSS();

    // Create our results container on document.body (stable, immune to view transitions)
    getOrCreateContainer();

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
        '<div class="tentacle-search-title">Search Results</div>' +
        '<div class="tentacle-search-filters">' +
          '<button class="tentacle-search-filter-btn ts-active" data-tstype="all">All</button>' +
          '<button class="tentacle-search-filter-btn" data-tstype="movies">Movies</button>' +
          '<button class="tentacle-search-filter-btn" data-tstype="series">TV Shows</button>' +
          '<button class="tentacle-search-filter-btn" data-tstype="channels">Live TV</button>' +
        '</div>' +
      '</div>' +
      '<div id="tentacleSearchGrid"></div>';

    // Single delegated click handler for the entire container
    container.addEventListener('click', function (e) {
      // Filter button click
      var filterBtn = e.target.closest('.tentacle-search-filter-btn');
      if (filterBtn) {
        container.querySelectorAll('.tentacle-search-filter-btn').forEach(function (x) { x.classList.remove('ts-active'); });
        filterBtn.classList.add('ts-active');
        SEARCH.mediaFilter = filterBtn.getAttribute('data-tstype');
        if (SEARCH.lastQuery) doSearch(SEARCH.lastQuery, SEARCH.generation);
        return;
      }

      // Channel card click — find Jellyfin channel and play stream directly
      var channelCard = e.target.closest('.ts-channel-card');
      if (channelCard) {
        e.preventDefault();
        e.stopPropagation();
        var chName = channelCard.querySelector('.ts-card-title');
        var name = chName ? chName.textContent.trim() : '';
        console.log('[TentacleSearch] Channel clicked:', name);
        if (name) {
          var userId = window.ApiClient.getCurrentUserId();
          apiGet('Users/' + userId + '/Items?SearchTerm=' + encodeURIComponent(name) +
            '&IncludeItemTypes=LiveTvChannel&Recursive=true&Limit=20')
            .then(function (data) {
              var items = (data && data.Items) || [];
              console.log('[TentacleSearch] Found', items.length, 'LiveTvChannel results:', items.map(function(i) { return i.Name; }));
              var nameLower = name.toLowerCase();
              var match = null;
              for (var i = 0; i < items.length; i++) {
                if ((items[i].Name || '').toLowerCase() === nameLower) { match = items[i]; break; }
              }
              if (!match && items.length) match = items[0];
              if (match) {
                console.log('[TentacleSearch] Playing channel:', match.Name, match.Id);
                var pm = (typeof playbackManager !== 'undefined') ? playbackManager : null;
                console.log('[TentacleSearch] playbackManager available:', !!pm);
                cleanup();
                if (pm) {
                  pm.play({ ids: [match.Id], serverId: window.ApiClient.serverId() });
                } else {
                  window.location.hash = '#/details?id=' + match.Id;
                }
              } else {
                console.warn('[TentacleSearch] No LiveTvChannel match found for:', name);
              }
            })
            .catch(function (err) {
              console.error('[TentacleSearch] Channel lookup failed:', err);
            });
        }
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

    return container;
  }

  // ── Search ───────────────────────────────────────────────────────────

  function doSearch(query, gen) {
    getOrCreateContainer();
    var grid = document.getElementById('tentacleSearchGrid');
    if (!grid) return;

    grid.innerHTML = '<div class="tentacle-search-loading"><div class="md-spinner"></div>Searching...</div>';

    apiGet('TentacleDiscover/Search?q=' + encodeURIComponent(query) + '&type=' + SEARCH.mediaFilter)
      .then(function (data) {
        // Stale check — did user navigate away during the API call?
        if (gen !== SEARCH.generation) return;

        var items = data.items || [];
        SEARCH.results = items;
        if (!items.length) {
          grid.innerHTML = '<div class="tentacle-search-empty">No results for \u201c' + esc(query) + '\u201d</div>';
          return;
        }
        renderResults(items, grid);
      })
      .catch(function () {
        if (gen !== SEARCH.generation) return;
        // Tentacle search failed — fall back to Jellyfin's native results rather
        // than leaving the user with a dead, CSS-hidden native search page.
        var el = document.getElementById('tentacleSearchResults');
        if (el) el.remove();
        removeHideCSS();
      });
  }

  // ── Render ───────────────────────────────────────────────────────────

  function renderResults(items, container) {
    var getDownloadInfo = (window.TentacleDiscover && window.TentacleDiscover.getDownloadInfo)
      ? window.TentacleDiscover.getDownloadInfo
      : function () { return null; };

    container.innerHTML = '<div class="tentacle-search-grid">' +
      items.map(function (item) {
        // Live TV channel card
        if (item.media_type === 'channel') {
          var logoSafe = safeUrl(item.logo_url);
          var logoHtml = logoSafe
            ? '<img src="' + escAttr(logoSafe) + '" loading="lazy" onerror="this.style.display=\'none\'">'
            : '<div class="ts-card-poster-placeholder">&#9654;</div>';
          var groupHtml = item.group_title ? esc(item.group_title) : 'Live TV';
          return '<div class="ts-card ts-channel-card" data-channel-id="' + escAttr(item.channel_id) + '">' +
            '<div class="ts-card-poster">' + logoHtml +
              '<div class="ts-card-badge ts-badge-type ts-badge-livetv">Live TV</div>' +
            '</div>' +
            '<div class="ts-card-info">' +
              '<div class="ts-card-title">' + esc(item.title) + '</div>' +
              '<div class="ts-card-meta">' + groupHtml + '</div>' +
            '</div></div>';
        }

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
          var statusText = dlInfo.status === 'importing' ? 'Importing' : dlInfo.status === 'queued' ? 'Queued' : 'Downloading ' + pct + '%';
          statusBadge = '<div class="ts-card-badge ts-badge-status ts-badge-downloading">' + statusText + '</div>';
        } else if (item.in_library) {
          statusBadge = '<div class="ts-card-badge ts-badge-status ts-badge-inlib">In Library</div>';
        }

        var ratingHtml = item.rating
          ? '<span class="ts-card-meta-rating">\u2605 ' + esc(String(item.rating)) + '</span>'
          : '';
        var yearHtml = item.year ? esc(String(item.year)) : '\u2014';
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
    var grid = document.getElementById('tentacleSearchGrid');
    if (grid) grid.innerHTML = '';
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
    removeHideCSS();
    SEARCH.active = false;
    SEARCH.lastQuery = '';
    SEARCH.results = [];
    SEARCH.channels = [];
    SEARCH.mediaFilter = 'all';
    SEARCH.nativeInput = null;
    var el = document.getElementById('tentacleSearchResults');
    if (el) el.remove();
  }

  // ── Bootstrap ────────────────────────────────────────────────────────

  function init() {
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
