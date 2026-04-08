// Tentacle Search — TMDB results replace Jellyfin's native /search results
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
    hiddenElements: [],
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

  function log(msg) {
    console.log('[Tentacle Search] ' + msg);
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

    log('Attached to search input: ' + input.className);

    // Log the search page DOM structure for debugging
    var pageView = input.closest('[data-type="search"]') || input.closest('.searchPage') || input.closest('[is="emby-scroller"]');
    if (pageView) {
      log('Page container: <' + pageView.tagName + ' class="' + pageView.className + '">');
      var children = pageView.children;
      for (var i = 0; i < children.length; i++) {
        log('  Child[' + i + ']: <' + children[i].tagName + ' class="' + children[i].className + '" id="' + (children[i].id || '') + '">');
      }
    }

    // Also log up the tree from the input
    var el = input;
    var path = [];
    while (el && el !== document.body) {
      path.push('<' + el.tagName.toLowerCase() + (el.className ? '.' + el.className.split(' ').join('.') : '') + (el.id ? '#' + el.id : '') + '>');
      el = el.parentElement;
    }
    log('Input DOM path: ' + path.join(' → '));

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

  // ── Hide/show native Jellyfin search results (JS-based) ─────────────

  function hideNativeResults() {
    // Restore any previously hidden elements first
    showNativeResults();

    // Find the content area below the search input — hide everything that's not ours
    // Strategy: find the scrollable content area that contains native results
    var searchPage = null;
    if (SEARCH.nativeInput) {
      // Walk up from the input to find the page-level container
      searchPage = SEARCH.nativeInput.closest('.searchPage')
        || SEARCH.nativeInput.closest('[data-type="search"]')
        || SEARCH.nativeInput.closest('.view');
    }
    if (!searchPage) {
      searchPage = document.querySelector('.searchPage')
        || document.querySelector('[data-type="search"]');
    }

    if (!searchPage) {
      log('Could not find search page container to hide native results');
      return;
    }

    log('Hiding native results in: <' + searchPage.tagName + ' class="' + searchPage.className + '">');

    // Find all section-like elements that contain native results
    // Jellyfin typically renders result groups as sections/divs with headers
    var candidates = searchPage.querySelectorAll('.searchResultGroup, .section, .verticalSection, .itemsContainer, [class*="searchResult"]');

    if (candidates.length === 0) {
      // Fallback: hide all direct children except search field and our container
      var children = searchPage.children;
      for (var i = 0; i < children.length; i++) {
        var child = children[i];
        if (child.id === 'tentacleSearchResults') continue;
        if (child.contains(SEARCH.nativeInput)) continue;
        if (child.querySelector && child.querySelector('.searchfields-txtSearch, input[type="text"]')) continue;
        if (child.style.display !== 'none') {
          SEARCH.hiddenElements.push({ el: child, prev: child.style.display });
          child.style.display = 'none';
          log('Hid child: <' + child.tagName + ' class="' + child.className + '">');
        }
      }
    } else {
      candidates.forEach(function (el) {
        if (el.id === 'tentacleSearchResults') return;
        if (el.contains(document.getElementById('tentacleSearchResults'))) return;
        if (el.style.display !== 'none') {
          SEARCH.hiddenElements.push({ el: el, prev: el.style.display });
          el.style.display = 'none';
          log('Hid result section: <' + el.tagName + ' class="' + el.className + '">');
        }
      });
    }
  }

  function showNativeResults() {
    SEARCH.hiddenElements.forEach(function (item) {
      item.el.style.display = item.prev;
    });
    SEARCH.hiddenElements = [];
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

    // Find the best place to insert our container
    var searchPage = null;
    if (SEARCH.nativeInput) {
      searchPage = SEARCH.nativeInput.closest('.searchPage')
        || SEARCH.nativeInput.closest('[data-type="search"]')
        || SEARCH.nativeInput.closest('.view');
    }
    if (!searchPage) {
      searchPage = document.querySelector('.searchPage')
        || document.querySelector('[data-type="search"]')
        || document.querySelector('.mainAnimatedPages')
        || document.body;
    }

    searchPage.appendChild(container);
    log('Inserted container into: <' + searchPage.tagName + ' class="' + searchPage.className + '">');

    return container;
  }

  // ── TMDB search ──────────────────────────────────────────────────────

  function doTmdbSearch(query) {
    var container = getOrCreateContainer();
    hideNativeResults();
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
    SEARCH.hiddenElements = [];
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
