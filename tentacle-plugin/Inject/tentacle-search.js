// Tentacle Search — TMDB results injected into Jellyfin's native /search page
// Hides native Jellyfin results and shows unified TMDB search with home-row-style cards
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
  };

  // ── Helpers ──────────────────────────────────────────────────────────

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

  function findSearchResultsContainer() {
    return document.querySelector('.searchResults')
      || document.querySelector('.itemsContainer.searchResults')
      || document.querySelector('.searchPage');
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

    input.addEventListener('input', onInputChange);

    var existing = input.value.trim();
    if (existing) {
      SEARCH.lastQuery = existing;
      doTmdbSearch(existing);
    }
  }

  function onInputChange(e) {
    var q = e.target.value.trim();
    if (SEARCH.debounceTimer) clearTimeout(SEARCH.debounceTimer);

    if (!q) {
      SEARCH.lastQuery = '';
      clearTmdbResults();
      showNativeResults();
      return;
    }

    SEARCH.debounceTimer = setTimeout(function () {
      if (q !== SEARCH.lastQuery) {
        SEARCH.lastQuery = q;
        doTmdbSearch(q);
      }
    }, 400);
  }

  // ── Hide/show native Jellyfin search results ────────────────────────

  function hideNativeResults() {
    var container = findSearchResultsContainer();
    if (container) {
      container.classList.add('tentacle-search-active');
    }
  }

  function showNativeResults() {
    var container = findSearchResultsContainer();
    if (container) {
      container.classList.remove('tentacle-search-active');
    }
  }

  // ── Inject TMDB results container ────────────────────────────────────

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

    container.querySelectorAll('.tentacle-search-filter-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        container.querySelectorAll('.tentacle-search-filter-btn').forEach(function (x) { x.classList.remove('ts-active'); });
        btn.classList.add('ts-active');
        SEARCH.mediaFilter = btn.getAttribute('data-tstype');
        if (SEARCH.lastQuery) doTmdbSearch(SEARCH.lastQuery);
      });
    });

    // Insert inside the search results container so hiding native children works
    var searchContainer = findSearchResultsContainer();
    if (searchContainer) {
      searchContainer.appendChild(container);
    } else {
      var main = document.querySelector('.mainAnimatedPages') || document.querySelector('main') || document.body;
      main.appendChild(container);
    }

    return container;
  }

  // ── TMDB search ──────────────────────────────────────────────────────

  function doTmdbSearch(query) {
    hideNativeResults();
    var container = getOrCreateContainer();
    var grid = document.getElementById('tentacleSearchGrid');
    if (!grid) return;

    grid.innerHTML = '<div class="tentacle-search-loading"><div class="md-spinner"></div>Searching...</div>';

    var typeParam = SEARCH.mediaFilter;
    apiGet('TentacleDiscover/Search?q=' + encodeURIComponent(query) + '&type=' + typeParam)
      .then(function (data) {
        var items = data.items || [];
        SEARCH.results = items;
        if (!items.length) {
          grid.innerHTML = '<div class="tentacle-search-empty">No results for \u201c' + esc(query) + '\u201d</div>';
          return;
        }
        renderSearchGrid(items, grid);
      })
      .catch(function () {
        grid.innerHTML = '<div class="tentacle-search-empty">Search failed</div>';
      });
  }

  // ── Render results as home-row-style horizontal scroll ──────────────

  function renderSearchGrid(items, container) {
    var getDownloadInfo = (window.TentacleDiscover && window.TentacleDiscover.getDownloadInfo)
      ? window.TentacleDiscover.getDownloadInfo
      : function () { return null; };

    container.innerHTML = '<div class="tentacle-search-row">' +
      items.map(function (item) {
        var posterUrl = item.poster_path
          ? 'https://image.tmdb.org/t/p/w342' + item.poster_path
          : '';
        var posterHtml = posterUrl
          ? '<img src="' + posterUrl + '" loading="lazy" onerror="this.style.display=\'none\'">'
          : '<div class="ts-card-poster-placeholder">&#9707;</div>';

        var dlInfo = getDownloadInfo(item.tmdb_id);
        var badge, addBtn;
        if (dlInfo) {
          var pct = (dlInfo.progress || 0).toFixed(1);
          var statusText = dlInfo.status === 'importing' ? 'Importing' : dlInfo.status === 'queued' ? 'Queued' : 'Downloading ' + pct + '%';
          badge = '<div class="ts-card-badge ts-badge-downloading">' + statusText + '</div>';
          addBtn = '';
        } else if (item.in_library) {
          badge = '<div class="ts-card-badge ts-badge-inlib">In Library</div>';
          addBtn = '';
        } else {
          badge = '';
          addBtn = '<button class="ts-card-add" data-tmdb="' + item.tmdb_id + '">+</button>';
        }

        var ratingHtml = item.rating
          ? '<span class="ts-card-meta-rating">\u2605 ' + item.rating + '</span>'
          : '';
        var yearHtml = item.year || '\u2014';
        var sep = item.rating ? ' \u00b7 ' : '';

        return '<div class="ts-card" data-tmdb="' + item.tmdb_id + '" data-type="' + (item.media_type || 'movie') + '">' +
          '<div class="ts-card-poster">' + posterHtml + badge + addBtn + '</div>' +
          '<div class="ts-card-info">' +
            '<div class="ts-card-title">' + esc(item.title) + '</div>' +
            '<div class="ts-card-meta">' + yearHtml + sep + ratingHtml + '</div>' +
          '</div></div>';
      }).join('') +
    '</div>';

    // Card click handlers
    container.querySelectorAll('.ts-card').forEach(function (card) {
      card.addEventListener('click', function (e) {
        if (e.target.closest('.ts-card-add')) return;
        var tmdb = parseInt(card.getAttribute('data-tmdb'), 10);
        var item = findSearchItem(tmdb);
        if (!item) return;

        if (item.in_library) {
          navigateToLibraryItem(item);
        } else {
          openDetailModal(item);
        }
      });
    });

    container.querySelectorAll('.ts-card-add').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var tmdb = parseInt(btn.getAttribute('data-tmdb'), 10);
        var item = findSearchItem(tmdb);
        if (item) openDetailModal(item);
      });
    });
  }

  // ── Item lookup & navigation ─────────────────────────────────────────

  function findSearchItem(tmdbId) {
    for (var i = 0; i < SEARCH.results.length; i++) {
      if (SEARCH.results[i].tmdb_id === tmdbId) return SEARCH.results[i];
    }
    return null;
  }

  function openDetailModal(item) {
    if (window.TentacleDiscover && window.TentacleDiscover.showDetailModal) {
      window.TentacleDiscover.showDetailModal(item);
    } else {
      console.warn('[Tentacle Search] Detail modal not available');
    }
  }

  function navigateToLibraryItem(item) {
    var userId = window.ApiClient.getCurrentUserId();
    var itemType = item.media_type === 'series' ? 'Series' : 'Movie';
    var url = window.ApiClient.getUrl('Users/' + userId + '/Items', {
      searchTerm: item.title,
      IncludeItemTypes: itemType,
      Recursive: true,
      Limit: 10,
      Fields: 'ProviderIds',
    });

    apiGet(url.replace(window.ApiClient.getUrl(''), '')).then(function (result) {
      var items = (result && result.Items) || [];
      var match = null;
      for (var i = 0; i < items.length; i++) {
        if ((items[i].Name || '').toLowerCase() === (item.title || '').toLowerCase()) {
          match = items[i];
          break;
        }
      }
      if (!match && items.length) match = items[0];

      if (match) {
        window.location.hash = '#/details?id=' + match.Id;
      }
    }).catch(function () {});
  }

  // ── Cleanup ──────────────────────────────────────────────────────────

  function clearTmdbResults() {
    var grid = document.getElementById('tentacleSearchGrid');
    if (grid) grid.innerHTML = '';
    SEARCH.results = [];
  }

  function cleanup() {
    if (SEARCH.inputObserver) {
      SEARCH.inputObserver.disconnect();
      SEARCH.inputObserver = null;
    }
    showNativeResults();
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
