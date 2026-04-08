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

  // ── Route detection ──────────────────────────────────────────────────

  function isSearchPage() {
    var h = (location.hash || '').replace('#', '').replace(/^\//, '').split('?')[0];
    return h === 'search' || h === 'search.html';
  }

  // ── Find the ACTIVE view's search input (not stale views) ──────────
  // Jellyfin SPA keeps old views in the DOM. We must only look in the
  // currently visible view to avoid attaching to a stale hidden input.
  function findActiveView() {
    // Jellyfin marks the active view — try multiple strategies
    var views = document.querySelectorAll('.view');
    for (var i = views.length - 1; i >= 0; i--) {
      var v = views[i];
      // Active view is the last visible one, or has data-type="search"
      if (v.offsetParent !== null || v.style.display !== 'none') {
        return v;
      }
    }
    // Fallback: last view in DOM (most recently added = active in Jellyfin SPA)
    return views.length ? views[views.length - 1] : null;
  }

  function findNativeSearchInput() {
    // Try active view first, then fall back to global search.
    // The active view scoping can miss the input if the view transition
    // hasn't settled yet, so we must not be overly strict here.
    var view = findActiveView();
    var scopes = view ? [view, document] : [document];
    for (var s = 0; s < scopes.length; s++) {
      var scope = scopes[s];
      var input = scope.querySelector('.searchfields-txtSearch')
        || scope.querySelector('input.emby-input[type="text"][data-action="search"]')
        || scope.querySelector('.searchPage input[type="text"]')
        || scope.querySelector('[data-type="search"] input[type="text"]');
      if (input) return input;
    }
    return null;
  }

  // ── Navigation Handlers ─────────────────────────────────────────────

  function onViewShow(e) {
    var type = e.detail && e.detail.type;
    if (type === 'search' || (!type && isSearchPage())) {
      onSearchPage();
    } else if (isSearchPage()) {
      onSearchPage();
    } else {
      onLeavingSearch();
    }
  }

  function onNavChange() {
    if (isSearchPage()) {
      onSearchPage();
    } else {
      onLeavingSearch();
    }
  }

  // ── Search Page Entry ─────────────────────────────────────────────

  function onSearchPage() {
    // Dismiss all other Tentacle overlays — only one should be visible at a time
    if (window.TentacleDiscover && window.TentacleDiscover.isActive && window.TentacleDiscover.isActive()) window.TentacleDiscover.hide();
    if (window.TentacleActivity && window.TentacleActivity.isActive && window.TentacleActivity.isActive()) window.TentacleActivity.hide();

    // If already active with a live container, just make sure it's visible
    if (SEARCH.active && document.getElementById('tentacleSearchResults')) return;

    // Clean any stale state first
    cleanup();

    waitForSearchInput();
  }

  function onLeavingSearch() {
    SEARCH.generation++;
    cleanup();
  }

  function waitForSearchInput() {
    var input = findNativeSearchInput();
    if (input) {
      attachToInput(input);
      return;
    }
    // Wait for Jellyfin to render the search input
    // Observe document.body (not scoped to active view) because the view
    // element itself may not exist yet during SPA transitions
    if (SEARCH.inputObserver) SEARCH.inputObserver.disconnect();

    SEARCH.inputObserver = new MutationObserver(function () {
      var inp = findNativeSearchInput();
      if (inp) {
        SEARCH.inputObserver.disconnect();
        SEARCH.inputObserver = null;
        attachToInput(inp);
      }
    });
    SEARCH.inputObserver.observe(document.body, { childList: true, subtree: true });

    // Safety timeout — don't observe forever
    setTimeout(function () {
      if (SEARCH.inputObserver) {
        SEARCH.inputObserver.disconnect();
        SEARCH.inputObserver = null;
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
      // Hide native search RESULTS but NOT the search input field.
      // The search input lives inside .searchPage — we must preserve it.
      // Only hide result containers, cards, and section headers below the input.
      'body.tentacle-search-active .card.overflowPortraitCard { display: none !important; }',
      'body.tentacle-search-active .card.overflowBackdropCard { display: none !important; }',
      'body.tentacle-search-active .card.overflowSquareCard { display: none !important; }',
      'body.tentacle-search-active .itemsContainer:not(#tentacleSearchGrid) { display: none !important; }',
      'body.tentacle-search-active .noItemsMessage { display: none !important; }',
      'body.tentacle-search-active .emby-scroller-alert { display: none !important; }',
      // Hide section titles and scrollers that contain native results
      'body.tentacle-search-active .searchResults .sectionTitle { display: none !important; }',
      'body.tentacle-search-active .searchResults .emby-scroller { display: none !important; }',
      'body.tentacle-search-active .searchResults .verticalSection { display: none !important; }',
      // Hide native search tab button
      'body.tentacle-search-active .searchTabButton { display: none !important; }',
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

      // Card click
      var card = e.target.closest('.ts-card');
      if (card) {
        e.preventDefault();
        e.stopPropagation();
        var tmdb = parseInt(card.getAttribute('data-tmdb'), 10);
        var item = findItem(tmdb);
        console.log('[TentacleSearch] Card clicked, tmdb=' + tmdb, 'item=', item);
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
        grid.innerHTML = '<div class="tentacle-search-empty">Search failed</div>';
      });
  }

  // ── Render ───────────────────────────────────────────────────────────

  function renderResults(items, container) {
    var getDownloadInfo = (window.TentacleDiscover && window.TentacleDiscover.getDownloadInfo)
      ? window.TentacleDiscover.getDownloadInfo
      : function () { return null; };

    container.innerHTML = '<div class="tentacle-search-grid">' +
      items.map(function (item) {
        var posterUrl = item.poster_path
          ? 'https://image.tmdb.org/t/p/w342' + item.poster_path
          : '';
        var posterHtml = posterUrl
          ? '<img src="' + posterUrl + '" loading="lazy" onerror="this.style.display=\'none\'">'
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
          ? '<span class="ts-card-meta-rating">\u2605 ' + item.rating + '</span>'
          : '';
        var yearHtml = item.year || '\u2014';
        var sep = item.rating ? ' \u00b7 ' : '';

        return '<div class="ts-card" data-tmdb="' + item.tmdb_id + '" data-type="' + (item.media_type || 'movie') + '">' +
          '<div class="ts-card-poster">' + posterHtml + typeBadge + statusBadge + '</div>' +
          '<div class="ts-card-info">' +
            '<div class="ts-card-title">' + esc(item.title) + '</div>' +
            '<div class="ts-card-meta">' + yearHtml + sep + ratingHtml + '</div>' +
          '</div></div>';
      }).join('') +
    '</div>';

  }

  // ── Navigation ───────────────────────────────────────────────────────

  function findItem(tmdbId) {
    for (var i = 0; i < SEARCH.results.length; i++) {
      if (SEARCH.results[i].tmdb_id === tmdbId) return SEARCH.results[i];
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
          console.warn('[TentacleSearch] No library match found for:', item.title);
        }
      }).catch(function (err) {
        console.error('[TentacleSearch] goToLibraryItem error:', err);
      });
  }

  // ── Cleanup ──────────────────────────────────────────────────────────

  function clearResults() {
    var grid = document.getElementById('tentacleSearchGrid');
    if (grid) grid.innerHTML = '';
    SEARCH.results = [];
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
      onSearchPage();
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
    hide: function () {
      if (SEARCH.active) {
        onLeavingSearch();
      }
    },
    isActive: function () {
      return SEARCH.active;
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', waitForReady);
  } else {
    waitForReady();
  }

})();
