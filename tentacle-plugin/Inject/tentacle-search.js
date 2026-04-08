// Tentacle Search — TMDB results injected into Jellyfin's native /search page
(function () {
  'use strict';

  var SEARCH = {
    active: false,
    debounceTimer: null,
    lastQuery: '',
    mediaFilter: 'all',
    results: [],
    inputObserver: null,
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

  function waitForSearchInput() {
    var input = findNativeSearchInput();
    if (input) {
      attachToInput(input);
      return;
    }
    // Wait for SPA to render the search page
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
    // Safety: stop observing after 10s
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

    input.addEventListener('input', onInputChange);

    // If input already has text (e.g. navigating back), trigger immediately
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
      return;
    }

    SEARCH.debounceTimer = setTimeout(function () {
      if (q !== SEARCH.lastQuery) {
        SEARCH.lastQuery = q;
        doTmdbSearch(q);
      }
    }, 400);
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
        '<div class="tentacle-search-title">TMDB Results</div>' +
        '<div class="md-filter-group">' +
          '<button class="md-filter-btn md-active" data-tstype="all">All</button>' +
          '<button class="md-filter-btn" data-tstype="movies">Movies</button>' +
          '<button class="md-filter-btn" data-tstype="series">TV Shows</button>' +
        '</div>' +
      '</div>' +
      '<div id="tentacleSearchGrid"></div>';

    // Wire up filter buttons
    container.querySelectorAll('.md-filter-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        container.querySelectorAll('.md-filter-btn').forEach(function (x) { x.classList.remove('md-active'); });
        btn.classList.add('md-active');
        SEARCH.mediaFilter = btn.getAttribute('data-tstype');
        if (SEARCH.lastQuery) doTmdbSearch(SEARCH.lastQuery);
      });
    });

    // Insert after Jellyfin's search results
    var searchPage = document.querySelector('.searchResults')
      || document.querySelector('.searchPage')
      || document.querySelector('[data-type="search"]');
    if (searchPage) {
      searchPage.parentNode.insertBefore(container, searchPage.nextSibling);
    } else {
      // Fallback: append to main content area
      var main = document.querySelector('.mainAnimatedPages') || document.querySelector('main') || document.body;
      main.appendChild(container);
    }

    return container;
  }

  // ── TMDB search ──────────────────────────────────────────────────────

  function doTmdbSearch(query) {
    var container = getOrCreateContainer();
    var grid = document.getElementById('tentacleSearchGrid');
    if (!grid) return;

    grid.innerHTML = '<div class="md-loading"><div class="md-spinner"></div><br>Searching TMDB...</div>';

    var typeParam = SEARCH.mediaFilter;
    apiGet('TentacleDiscover/Search?q=' + encodeURIComponent(query) + '&type=' + typeParam)
      .then(function (data) {
        var items = data.items || [];
        SEARCH.results = items;
        if (!items.length) {
          grid.innerHTML = '<div class="md-loading" style="padding:20px;font-size:14px;">No TMDB results for \u201c' + esc(query) + '\u201d</div>';
          return;
        }
        renderSearchGrid(items, grid);
      })
      .catch(function () {
        grid.innerHTML = '<div class="md-loading" style="padding:20px;font-size:14px;">TMDB search failed</div>';
      });
  }

  // ── Render results grid ──────────────────────────────────────────────

  function renderSearchGrid(items, container) {
    // Check if discover module has download info available
    var getDownloadInfo = (window.TentacleDiscover && window.TentacleDiscover.getDownloadInfo)
      ? window.TentacleDiscover.getDownloadInfo
      : function () { return null; };

    container.innerHTML = '<div class="md-discover-grid">' +
      items.map(function (item) {
        var poster = item.poster_path
          ? '<img src="https://image.tmdb.org/t/p/w185' + item.poster_path + '" loading="lazy" onerror="this.style.display=\'none\'">'
          : '<div class="md-card-poster-placeholder">&#9707;</div>';

        var dlInfo = getDownloadInfo(item.tmdb_id);
        var badge, addBtn;
        if (dlInfo) {
          var pct = (dlInfo.progress || 0).toFixed(1);
          var statusText = dlInfo.status === 'importing' ? 'Importing' : dlInfo.status === 'queued' ? 'Queued' : 'Downloading ' + pct + '%';
          badge = '<div class="md-card-badge md-badge-downloading">' + statusText + '</div>';
          addBtn = '';
        } else if (item.in_library) {
          badge = '<div class="md-card-badge md-badge-inlib">In Library</div>';
          addBtn = '';
        } else {
          badge = '<div class="md-card-badge md-badge-type">' + (item.media_type === 'movie' ? 'Movie' : 'Show') + '</div>';
          addBtn = '<button class="md-card-add" data-tmdb="' + item.tmdb_id + '">+</button>';
        }

        return '<div class="md-card" data-tmdb="' + item.tmdb_id + '" data-type="' + (item.media_type || 'movie') + '">' +
          '<div class="md-card-poster">' + poster + badge + addBtn + '</div>' +
          '<div class="md-card-info">' +
            '<div class="md-card-title">' + esc(item.title) + '</div>' +
            '<div class="md-card-meta">' + (item.year || '\u2014') + ' \u00b7 \u2605 ' + (item.rating || '\u2014') + '</div>' +
          '</div></div>';
      }).join('') +
    '</div>';

    // Card click handlers
    container.querySelectorAll('.md-card').forEach(function (card) {
      card.addEventListener('click', function (e) {
        if (e.target.closest('.md-card-add')) return;
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

    container.querySelectorAll('.md-card-add').forEach(function (btn) {
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
    // Use the shared detail modal from tentacle-discover.js
    if (window.TentacleDiscover && window.TentacleDiscover.showDetailModal) {
      window.TentacleDiscover.showDetailModal(item);
    } else {
      console.warn('[Tentacle Search] Detail modal not available — discover module not loaded');
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
      // Try exact title match first
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
    }).catch(function () {
      // Silently fail — item may not be findable by search
    });
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
    SEARCH.active = false;
    SEARCH.lastQuery = '';
    SEARCH.results = [];
    SEARCH.mediaFilter = 'all';
    var el = document.getElementById('tentacleSearchResults');
    if (el) el.remove();
  }

  // ── Bootstrap ────────────────────────────────────────────────────────

  function init() {
    window.addEventListener('hashchange', onRouteChange);
    window.addEventListener('popstate', onRouteChange);
    window.addEventListener('viewshow', onRouteChange);
    // Check on load in case we're already on search page
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
