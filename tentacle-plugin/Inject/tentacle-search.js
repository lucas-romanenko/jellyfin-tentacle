// Tentacle Search — Unified search replacing Jellyfin's native results with TMDB
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

  function onRouteChange() {
    if (isSearchPage()) {
      waitForSearchInput();
    } else {
      cleanup();
    }
  }

  // ── Find Jellyfin's native search input ──────────────────────────────

  function findNativeSearchInput() {
    return document.querySelector('.searchfields-txtSearch')
      || document.querySelector('input.emby-input[type="text"][data-action="search"]')
      || document.querySelector('.searchPage input[type="text"]')
      || document.querySelector('[data-type="search"] input[type="text"]');
  }

  function waitForSearchInput() {
    var input = findNativeSearchInput();
    if (input) {
      attachToInput(input);
      return;
    }
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

    // Immediately hide native results with CSS
    injectHideCSS();

    input.addEventListener('input', onInputChange);

    var existing = input.value.trim();
    if (existing) {
      SEARCH.lastQuery = existing;
      doSearch(existing);
    }
  }

  function onInputChange(e) {
    var q = e.target.value.trim();
    if (SEARCH.debounceTimer) clearTimeout(SEARCH.debounceTimer);

    if (!q) {
      SEARCH.lastQuery = '';
      clearResults();
      return;
    }

    SEARCH.debounceTimer = setTimeout(function () {
      if (q !== SEARCH.lastQuery) {
        SEARCH.lastQuery = q;
        doSearch(q);
      }
    }, 400);
  }

  // ── CSS injection to nuke ALL native search results ──────────────────

  function injectHideCSS() {
    if (SEARCH.hideStyle) return;
    var style = document.createElement('style');
    style.id = 'tentacleSearchHideNative';
    style.textContent = [
      // Native search result cards
      '.card.overflowPortraitCard { display: none !important; }',
      '.card.overflowBackdropCard { display: none !important; }',
      '.card.overflowSquareCard { display: none !important; }',
      // Native section headers
      '.verticalSection > .sectionTitle { display: none !important; }',
      // Native "no results" messages
      '.noItemsMessage { display: none !important; }',
      '.emby-scroller-alert { display: none !important; }',
      // Catch-all for Jellyfin messages
      '.searchPage .padded-left, .searchPage .padded-right { display: none !important; }',
      // Hide native itemsContainers
      '.itemsContainer:not(#tentacleSearchGrid) { display: none !important; }',
      // Hide native vertical sections
      '.verticalSection { display: none !important; }',
      // Hide native search label/icon that may overlap
      '.searchTabButton { display: none !important; }',
      '.headerSearchButton { display: none !important; }',
      // Our container always visible
      '#tentacleSearchResults { display: block !important; }',
      '#tentacleSearchResults * { /* no override */ }',
    ].join('\n');
    document.head.appendChild(style);
    SEARCH.hideStyle = style;
  }

  function removeHideCSS() {
    if (SEARCH.hideStyle) {
      SEARCH.hideStyle.remove();
      SEARCH.hideStyle = null;
    }
  }

  // ── Container ────────────────────────────────────────────────────────

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
        if (SEARCH.lastQuery) doSearch(SEARCH.lastQuery);
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

    // Insert into the page
    var parent = null;
    if (SEARCH.nativeInput) {
      parent = SEARCH.nativeInput.closest('.view')
        || SEARCH.nativeInput.closest('.searchPage')
        || SEARCH.nativeInput.closest('[data-type="search"]');
    }
    if (!parent) {
      parent = document.querySelector('.view')
        || document.querySelector('.mainAnimatedPages')
        || document.body;
    }
    parent.appendChild(container);

    return container;
  }

  // ── Search ───────────────────────────────────────────────────────────

  function doSearch(query) {
    var container = getOrCreateContainer();
    var grid = document.getElementById('tentacleSearchGrid');
    if (!grid) return;

    grid.innerHTML = '<div class="tentacle-search-loading"><div class="md-spinner"></div>Searching...</div>';

    apiGet('TentacleDiscover/Search?q=' + encodeURIComponent(query) + '&type=' + SEARCH.mediaFilter)
      .then(function (data) {
        var items = data.items || [];
        SEARCH.results = items;
        if (!items.length) {
          grid.innerHTML = '<div class="tentacle-search-empty">No results for \u201c' + esc(query) + '\u201d</div>';
          return;
        }
        renderResults(items, grid);
      })
      .catch(function () {
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
    } else {
      // Fallback: navigate to home and trigger discover with search
      console.warn('[TentacleSearch] showDetailModal not available, using fallback');
      alert('Detail modal not available. Please visit the home page first to initialize Discover, then try again.');
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
    if (SEARCH.inputObserver) {
      SEARCH.inputObserver.disconnect();
      SEARCH.inputObserver = null;
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
    window.addEventListener('hashchange', onRouteChange);
    window.addEventListener('popstate', onRouteChange);
    window.addEventListener('viewshow', onRouteChange);
    onRouteChange();
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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', waitForReady);
  } else {
    waitForReady();
  }

})();
