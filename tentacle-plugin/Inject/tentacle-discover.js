// Tentacle Discover — Jellyfin tab for trending, popular, lists & upcoming
// v4 — sections-based layout with category tabs
(function () {
  'use strict';
  console.log('[Tentacle Discover] v4 sections loaded');

  var MD = {
    initialized: false,
    enabled: null,
    sections: null,
    mediaFilter: 'all',    // all | movies | series
    activeSection: null,    // trending | popular | missing | upcoming | activity
    active: false,
    loaded: false,
    searchQuery: '',
    searchTimeout: null,
    searchMode: false,
    searchResults: [],
    activityData: null,
    activityTimer: null,
  };

  // ── Bootstrap ─────────────────────────────────────────────────────
  function waitForReady() {
    if (window.ApiClient && window.ApiClient.getCurrentUserId()) {
      init();
    } else {
      setTimeout(waitForReady, 300);
    }
  }

  function init() {
    if (MD.initialized) return;
    MD.initialized = true;
    tryInject();
    window.addEventListener('hashchange', function () {
      if (MD.active) hideDiscover();
      setTimeout(tryInject, 200);
    });
    window.addEventListener('popstate', function () {
      if (MD.active) hideDiscover();
      setTimeout(tryInject, 200);
    });
  }

  function isHomePage() {
    var h = location.hash || '';
    return h === '' || h === '#/' || h === '#/home.html' || h === '#/home';
  }

  function tryInject() {
    if (!isHomePage()) return;
    var slider = document.querySelector('.emby-tabs-slider');
    if (!slider) { setTimeout(tryInject, 300); return; }

    // Always re-check config from server (plugin clears cache on refresh)
    apiGet('TentacleDiscover/Config').then(function (cfg) {
      MD.enabled = cfg && cfg.discover_in_jellyfin === true;
      var existingTab = slider.querySelector('#mdDiscoverTab');
      if (MD.enabled && !existingTab) {
        addTab(slider);
      } else if (!MD.enabled && existingTab) {
        existingTab.remove();
        if (MD.active) hideDiscover();
      }
    }).catch(function () {
      MD.enabled = false;
    });
  }

  // ── Tab button ────────────────────────────────────────────────────
  function addTab(slider) {
    if (slider.querySelector('#mdDiscoverTab')) return;

    var title = document.createElement('div');
    title.classList.add('emby-button-foreground');
    title.textContent = 'Discover';

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('is', 'emby-button');
    btn.classList.add('emby-tab-button', 'emby-button');
    btn.setAttribute('data-index', '99');
    btn.id = 'mdDiscoverTab';
    slider.appendChild(btn);
    btn.appendChild(title);

    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      showDiscover();
    });

    slider.addEventListener('click', function (e) {
      var tabBtn = e.target.closest('.emby-tab-button');
      if (tabBtn && tabBtn.id !== 'mdDiscoverTab' && MD.active) {
        hideDiscover();
      }
    });
  }

  // ── Overlay container ───────────────────────────────────────────
  function getOrCreateOverlay() {
    var el = document.getElementById('mdDiscoverOverlay');
    if (el) return el;

    el = document.createElement('div');
    el.id = 'mdDiscoverOverlay';
    el.className = 'md-discover-overlay';
    el.style.display = 'none';
    document.body.appendChild(el);
    return el;
  }

  // ── Show / Hide ───────────────────────────────────────────────────
  function showDiscover() {
    MD.active = true;

    document.querySelectorAll('.emby-tabs-slider .emby-tab-button').forEach(function (b) {
      b.classList.remove('emby-tab-button-active');
    });
    var btn = document.getElementById('mdDiscoverTab');
    if (btn) btn.classList.add('emby-tab-button-active');

    var overlay = getOrCreateOverlay();
    overlay.style.display = 'block';

    if (!MD.loaded) {
      MD.loaded = true;
      renderPage(overlay);
    }
  }

  function hideDiscover() {
    MD.active = false;
    stopActivityPolling();

    var btn = document.getElementById('mdDiscoverTab');
    if (btn) btn.classList.remove('emby-tab-button-active');

    var overlay = document.getElementById('mdDiscoverOverlay');
    if (overlay) overlay.style.display = 'none';
  }

  // ── API helpers ───────────────────────────────────────────────────
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
    });
  }

  // ── Render page skeleton ──────────────────────────────────────────
  function renderPage(container) {
    container.innerHTML =
      '<div class="md-discover-header">' +
        '<div class="md-discover-title">Discover</div>' +
        '<div style="display:flex;align-items:center;gap:12px">' +
          '<div class="md-search-box">' +
            '<input type="text" id="mdSearchInput" class="md-search-input" placeholder="Search TMDB…" autocomplete="off">' +
            '<button id="mdSearchClear" class="md-search-clear" style="display:none">&times;</button>' +
          '</div>' +
          '<div class="md-filter-group">' +
            '<button class="md-filter-btn md-active" data-mdtype="all">All</button>' +
            '<button class="md-filter-btn" data-mdtype="movies">Movies</button>' +
            '<button class="md-filter-btn" data-mdtype="series">Shows</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div class="md-section-tabs" id="mdSectionTabs"></div>' +
      '<div id="mdDiscoverContent" class="md-discover-content">' +
        '<div class="md-loading"><div class="md-spinner"></div><br>Loading…</div>' +
      '</div>';

    // Media type filter
    container.querySelectorAll('.md-filter-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        container.querySelectorAll('.md-filter-btn').forEach(function (x) { x.classList.remove('md-active'); });
        b.classList.add('md-active');
        MD.mediaFilter = b.getAttribute('data-mdtype');
        if (MD.searchMode && MD.searchQuery) {
          doSearch(MD.searchQuery);
        } else {
          fetchData();
        }
      });
    });

    // Search input
    var searchInput = document.getElementById('mdSearchInput');
    var searchClear = document.getElementById('mdSearchClear');
    if (searchInput) {
      searchInput.addEventListener('input', function () {
        var q = searchInput.value.trim();
        searchClear.style.display = q ? 'flex' : 'none';
        if (MD.searchTimeout) clearTimeout(MD.searchTimeout);
        if (!q) {
          exitSearch();
          return;
        }
        MD.searchTimeout = setTimeout(function () {
          MD.searchQuery = q;
          doSearch(q);
        }, 400);
      });
      searchInput.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
          searchInput.value = '';
          searchClear.style.display = 'none';
          exitSearch();
        }
      });
    }
    if (searchClear) {
      searchClear.addEventListener('click', function () {
        searchInput.value = '';
        searchClear.style.display = 'none';
        exitSearch();
      });
    }

    fetchData();
  }

  // ── Fetch sections from API ───────────────────────────────────────
  function fetchData() {
    var content = document.getElementById('mdDiscoverContent');
    if (content) content.innerHTML = '<div class="md-loading"><div class="md-spinner"></div><br>Loading…</div>';

    var typeParam = MD.mediaFilter === 'all' ? 'all' : MD.mediaFilter === 'movies' ? 'movies' : 'series';

    // Fetch discover items and activity in parallel
    var itemsPromise = apiGet('TentacleDiscover/Items?type=' + typeParam);
    var activityPromise = apiGet('TentacleDiscover/Activity').catch(function () {
      return { downloads: [], unreleased: [] };
    });

    Promise.all([itemsPromise, activityPromise]).then(function (results) {
      var data = results[0];
      var activity = results[1];
      MD.activityData = activity;
      MD.sections = data.sections || [];

      // Always show activity tab
      var activityCount = (activity.downloads || []).length + (activity.unreleased || []).length;
      MD.sections.unshift({ id: 'activity', title: 'Activity', items: [], _activityCount: activityCount });

      renderSectionTabs();
      startActivityPolling();
      if (MD.sections.length > 0) {
        var targetId = MD.activeSection || MD.sections[0].id;
        var found = MD.sections.find(function (s) { return s.id === targetId; });
        if (!found) targetId = MD.sections[0].id;
        switchSection(targetId);
      } else {
        var c = document.getElementById('mdDiscoverContent');
        if (c) c.innerHTML = '<div class="md-loading">No content found</div>';
      }
    }).catch(function () {
      var c = document.getElementById('mdDiscoverContent');
      if (c) c.innerHTML = '<div class="md-loading">Failed to load discover content</div>';
    });
  }

  // ── Search ───────────────────────────────────────────────────────
  function doSearch(query) {
    MD.searchMode = true;
    var tabsEl = document.getElementById('mdSectionTabs');
    if (tabsEl) tabsEl.innerHTML = '';
    var content = document.getElementById('mdDiscoverContent');
    if (content) content.innerHTML = '<div class="md-loading"><div class="md-spinner"></div><br>Searching…</div>';

    var typeParam = MD.mediaFilter === 'all' ? 'all' : MD.mediaFilter === 'movies' ? 'movies' : 'series';
    apiGet('TentacleDiscover/Search?q=' + encodeURIComponent(query) + '&type=' + typeParam).then(function (data) {
      var items = data.items || [];
      MD.searchResults = items;
      if (!items.length) {
        if (content) content.innerHTML = '<div class="md-loading">No results for "' + esc(query) + '"</div>';
        return;
      }
      renderGrid({ id: 'search', title: 'Search Results', items: items });
    }).catch(function () {
      if (content) content.innerHTML = '<div class="md-loading">Search failed</div>';
    });
  }

  function exitSearch() {
    MD.searchMode = false;
    MD.searchQuery = '';
    fetchData();
  }

  // ── Section tabs ──────────────────────────────────────────────────
  var SECTION_LABELS = {
    activity: 'Activity',
    trending: 'Trending',
    popular: 'Popular',
    missing: 'From Your Lists',
    upcoming: 'Upcoming',
  };

  function renderSectionTabs() {
    var tabsEl = document.getElementById('mdSectionTabs');
    if (!tabsEl || !MD.sections) return;

    tabsEl.innerHTML = MD.sections.map(function (sec) {
      var count = sec.id === 'activity' ? (sec._activityCount || 0) : sec.items.length;
      return '<button class="md-section-tab' + (sec.id === 'activity' ? ' md-activity-tab' : '') + '" data-section="' + sec.id + '">' +
        esc(SECTION_LABELS[sec.id] || sec.title) +
        '<span class="md-section-count">' + count + '</span>' +
      '</button>';
    }).join('');

    tabsEl.querySelectorAll('.md-section-tab').forEach(function (btn) {
      btn.addEventListener('click', function () {
        switchSection(btn.getAttribute('data-section'));
      });
    });
  }

  function switchSection(sectionId) {
    MD.activeSection = sectionId;

    document.querySelectorAll('.md-section-tab').forEach(function (btn) {
      btn.classList.toggle('md-section-active', btn.getAttribute('data-section') === sectionId);
    });

    if (sectionId === 'activity') {
      renderActivity();
      return;
    }

    var section = MD.sections && MD.sections.find(function (s) { return s.id === sectionId; });
    if (!section) return;

    renderGrid(section);
  }

  // ── Grid rendering ────────────────────────────────────────────────
  function renderGrid(section) {
    var content = document.getElementById('mdDiscoverContent');
    if (!content) return;

    var items = section.items || [];
    if (!items.length) {
      content.innerHTML = '<div class="md-loading">No content in this section</div>';
      return;
    }

    content.innerHTML = '<div class="md-discover-grid">' +
      items.map(function (item) {
        var poster = item.poster_path
          ? '<img src="https://image.tmdb.org/t/p/w185' + item.poster_path + '" loading="lazy" onerror="this.style.display=\'none\'">'
          : '<div class="md-card-poster-placeholder">&#9707;</div>';
        var badge = item.in_library
          ? '<div class="md-card-badge md-badge-inlib">In Library</div>'
          : '<div class="md-card-badge md-badge-type">' + (item.media_type === 'movie' ? 'Movie' : 'Show') + '</div>';
        var addBtn = !item.in_library
          ? '<button class="md-card-add" data-tmdb="' + item.tmdb_id + '">+</button>'
          : '';
        var listTag = item.list_name
          ? '<div class="md-card-list-tag">' + esc(item.list_name) + '</div>'
          : '';
        return '<div class="md-card" data-tmdb="' + item.tmdb_id + '" data-type="' + (item.media_type || 'movie') + '">' +
          '<div class="md-card-poster">' + poster + badge + addBtn + '</div>' +
          '<div class="md-card-info">' +
            '<div class="md-card-title">' + esc(item.title) + '</div>' +
            '<div class="md-card-meta">' + (item.year || '—') + ' · ★ ' + (item.rating || '—') + '</div>' +
            listTag +
          '</div></div>';
      }).join('') +
    '</div>';

    // Card click → detail modal
    content.querySelectorAll('.md-card').forEach(function (card) {
      card.addEventListener('click', function (e) {
        if (e.target.closest('.md-card-add')) return;
        var tmdb = parseInt(card.getAttribute('data-tmdb'), 10);
        var item = findItem(tmdb);
        if (item) showDetailModal(item);
      });
    });

    // Add button click → detail modal
    content.querySelectorAll('.md-card-add').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var tmdb = parseInt(btn.getAttribute('data-tmdb'), 10);
        var item = findItem(tmdb);
        if (item) showDetailModal(item);
      });
    });
  }

  function findItem(tmdbId) {
    // Check search results first
    if (MD.searchResults) {
      for (var k = 0; k < MD.searchResults.length; k++) {
        if (MD.searchResults[k].tmdb_id === tmdbId) return MD.searchResults[k];
      }
    }
    if (!MD.sections) return null;
    for (var i = 0; i < MD.sections.length; i++) {
      var items = MD.sections[i].items;
      for (var j = 0; j < items.length; j++) {
        if (items[j].tmdb_id === tmdbId) return items[j];
      }
    }
    return null;
  }

  // ── Detail Modal ──────────────────────────────────────────────────
  function showDetailModal(item) {
    // If item is missing metadata (e.g. from lists), fetch details first
    if (!item.overview && !item.backdrop_path) {
      var mediaType = item.media_type === 'series' ? 'series' : 'movie';
      apiGet('TentacleDiscover/Detail/' + mediaType + '/' + item.tmdb_id).then(function (details) {
        if (details && !details.error) {
          item.overview = details.overview || '';
          item.rating = details.rating || item.rating;
          item.backdrop_path = details.backdrop_path || null;
          item.year = details.year || item.year;
        }
        renderModal(item);
      }).catch(function () {
        renderModal(item);
      });
    } else {
      renderModal(item);
    }
  }

  function renderModal(item) {
    var old = document.getElementById('mdDetailModal');
    if (old) old.remove();

    var backdrop = item.backdrop_path
      ? '<img src="https://image.tmdb.org/t/p/w780' + item.backdrop_path + '">'
      : '';

    var downloadSection = '';
    if (item.in_library) {
      downloadSection =
        '<div class="md-inlib-row">' +
          '<div class="md-inlib-badge">✓ Already in library</div>' +
          '<button id="mdViewInLibrary" class="md-view-library-btn">View in Library</button>' +
        '</div>';
    } else {
      var isSeries = item.media_type === 'series';
      var arrLabel = isSeries ? 'Sonarr' : 'Radarr';
      var monitorSelect = isSeries
        ? '<select id="mdMonitorSelect" class="md-select">' +
            '<option value="all">All Episodes</option>' +
            '<option value="firstSeason">First Season</option>' +
            '<option value="lastSeason">Last Season</option>' +
            '<option value="pilot">Pilot Only</option>' +
            '<option value="none">None</option>' +
          '</select>'
        : '';
      downloadSection =
        '<div class="md-download-row">' +
          '<select id="mdProfileSelect" class="md-select"><option>Loading...</option></select>' +
          monitorSelect +
          '<button id="mdDownloadBtn" class="md-download-btn">Add to ' + arrLabel + '</button>' +
        '</div>' +
        '<div id="mdDownloadStatus" class="md-download-status"></div>';
    }

    var overlay = document.createElement('div');
    overlay.id = 'mdDetailModal';
    overlay.className = 'md-modal-overlay';
    overlay.innerHTML =
      '<div class="md-modal">' +
        '<button class="md-modal-close">&times;</button>' +
        '<div class="md-modal-backdrop">' + backdrop + '<div class="md-modal-backdrop-gradient"></div></div>' +
        '<div class="md-modal-body">' +
          '<div class="md-modal-title">' + esc(item.title) + '</div>' +
          '<div class="md-modal-subtitle">' + (item.year || '') + ' · ' + (item.media_type === 'movie' ? 'Movie' : 'TV Show') + ' · ★ ' + (item.rating || '—') + '</div>' +
          (item.overview ? '<div class="md-modal-overview">' + esc(item.overview) + '</div>' : '') +
          downloadSection +
        '</div>' +
      '</div>';

    document.body.appendChild(overlay);
    requestAnimationFrame(function () { overlay.classList.add('md-visible'); });

    overlay.querySelector('.md-modal-close').addEventListener('click', closeModal);
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closeModal();
    });

    if (item.in_library) {
      var viewBtn = overlay.querySelector('#mdViewInLibrary');
      if (viewBtn) {
        viewBtn.addEventListener('click', function () {
          viewBtn.disabled = true;
          viewBtn.textContent = 'Finding…';
          findJellyfinItem(item).then(function (itemId) {
            if (itemId) {
              closeModal();
              window.location.hash = '#/details?id=' + itemId;
            } else {
              viewBtn.textContent = 'Not found';
              viewBtn.disabled = true;
            }
          });
        });
      }
    } else {
      loadDownloadOptions(item);
    }
  }

  function findJellyfinItem(item) {
    var userId = window.ApiClient.getCurrentUserId();
    var itemType = item.media_type === 'series' ? 'Series' : 'Movie';
    var url = window.ApiClient.getUrl('Users/' + userId + '/Items', {
      searchTerm: item.title,
      IncludeItemTypes: itemType,
      Recursive: true,
      Limit: 5,
    });
    return window.ApiClient.getJSON(url).then(function (result) {
      var items = (result && result.Items) || [];
      // Try exact title + year match
      var match = items.find(function (i) {
        var titleMatch = (i.Name || '').toLowerCase() === (item.title || '').toLowerCase();
        var yearMatch = !item.year || String(i.ProductionYear || '') === String(item.year);
        return titleMatch && yearMatch;
      });
      if (!match) match = items[0];
      return match ? match.Id : null;
    }).catch(function () {
      return null;
    });
  }

  function closeModal() {
    var m = document.getElementById('mdDetailModal');
    if (m) {
      m.classList.remove('md-visible');
      setTimeout(function () { m.remove(); }, 200);
    }
  }

  function loadDownloadOptions(item) {
    var isSeries = item.media_type === 'series';
    var uid = window.ApiClient.getCurrentUserId();
    var profileEp = isSeries ? 'TentacleDiscover/SonarrProfiles?userId=' + uid : 'TentacleDiscover/RadarrProfiles?userId=' + uid;

    apiGet(profileEp).then(function (profiles) {
      profiles = profiles || [];
      var ps = document.getElementById('mdProfileSelect');
      if (!ps) return;

      ps.innerHTML = profiles.map(function (p) {
        return '<option value="' + p.id + '">' + esc(p.name) + '</option>';
      }).join('');

      var btn = document.getElementById('mdDownloadBtn');
      if (btn) {
        btn.addEventListener('click', function () {
          doDownload(item, ps.value);
        });
      }
    }).catch(function () {
      var s = document.getElementById('mdDownloadStatus');
      if (s) { s.style.color = '#f44336'; s.textContent = 'Failed to load profiles'; }
    });
  }

  function doDownload(item, profileId) {
    var btn = document.getElementById('mdDownloadBtn');
    var status = document.getElementById('mdDownloadStatus');
    if (!btn || !status) return;

    btn.disabled = true;
    btn.textContent = 'Adding...';
    status.textContent = '';

    var isSeries = item.media_type === 'series';
    var uid = window.ApiClient.getCurrentUserId();
    var ep = isSeries ? 'TentacleDiscover/AddToSonarr?userId=' + uid : 'TentacleDiscover/AddToRadarr?userId=' + uid;

    var body = {
      tmdb_ids: [item.tmdb_id],
      quality_profile_id: parseInt(profileId, 10),
    };
    if (isSeries) {
      var monitorEl = document.getElementById('mdMonitorSelect');
      body.monitor = monitorEl ? monitorEl.value : 'all';
      body.season_folder = true;
    }

    apiPost(ep, body).then(function () {
      status.style.color = '#4caf50';
      status.textContent = '✓ Added to ' + (isSeries ? 'Sonarr' : 'Radarr');
      btn.textContent = 'Added!';
      item.in_library = true;
    }).catch(function (err) {
      status.style.color = '#f44336';
      status.textContent = 'Error: ' + (err.message || 'Failed');
      btn.disabled = false;
      btn.textContent = 'Retry';
    });
  }

  // ── Activity rendering ────────────────────────────────────────────
  function renderActivity() {
    var content = document.getElementById('mdDiscoverContent');
    if (!content || !MD.activityData) return;

    var downloads = MD.activityData.downloads || [];
    var unreleased = MD.activityData.unreleased || [];
    var html = '';

    if (downloads.length > 0) {
      html += '<div class="md-activity-section">' +
        '<div class="md-activity-section-title">Downloading</div>' +
        '<div class="md-activity-grid">' +
        downloads.map(function (dl) {
          var poster = dl.poster_path
            ? '<img src="https://image.tmdb.org/t/p/w185' + dl.poster_path + '" loading="lazy" onerror="this.style.display=\'none\'">'
            : '<div class="md-card-poster-placeholder">&#9707;</div>';
          var statusClass = 'md-dl-' + (dl.status || 'downloading');
          var statusLabel = dl.status === 'importing' ? 'Importing' :
            dl.status === 'queued' ? 'Queued' :
            dl.status === 'warning' ? 'Warning' : 'Downloading';
          var progressPct = Math.min(Math.max(dl.progress || 0, 0), 100);
          var epLabel = dl.episode ? ' · ' + esc(dl.episode) : '';
          var etaLabel = dl.eta ? ' · ' + esc(dl.eta) : '';
          var sizeLabel = dl.size_remaining ? esc(dl.size_remaining) + ' left' : '';
          var qualityLabel = dl.quality ? esc(dl.quality) : '';
          var metaParts = [qualityLabel, sizeLabel].filter(Boolean).join(' · ');

          return '<div class="md-activity-card">' +
            '<div class="md-activity-poster">' + poster + '</div>' +
            '<div class="md-activity-info">' +
              '<div class="md-activity-title">' + esc(dl.title) + epLabel + '</div>' +
              '<div class="md-activity-meta">' + (dl.year || '') + (metaParts ? ' · ' + metaParts : '') + '</div>' +
              '<div class="md-activity-progress">' +
                '<div class="md-progress-bar"><div class="md-progress-fill ' + statusClass + '" style="width:' + progressPct + '%"></div></div>' +
                '<span class="md-progress-label">' + statusLabel + ' · ' + progressPct.toFixed(1) + '%' + etaLabel + '</span>' +
              '</div>' +
            '</div>' +
          '</div>';
        }).join('') +
        '</div></div>';
    }

    if (unreleased.length > 0) {
      html += '<div class="md-activity-section">' +
        '<div class="md-activity-section-title">Upcoming Releases</div>' +
        '<div class="md-activity-grid">' +
        unreleased.map(function (item) {
          var poster = item.poster_path
            ? '<img src="https://image.tmdb.org/t/p/w185' + item.poster_path + '" loading="lazy" onerror="this.style.display=\'none\'">'
            : '<div class="md-card-poster-placeholder">&#9707;</div>';
          var daysUntil = '';
          if (item.release_date) {
            var rd = new Date(item.release_date + 'T00:00:00');
            var now = new Date();
            var diff = Math.ceil((rd - now) / 86400000);
            daysUntil = diff <= 0 ? 'Releasing soon' : diff === 1 ? 'Tomorrow' : diff + ' days';
          }
          return '<div class="md-activity-card">' +
            '<div class="md-activity-poster">' + poster + '</div>' +
            '<div class="md-activity-info">' +
              '<div class="md-activity-title">' + esc(item.title) + '</div>' +
              '<div class="md-activity-meta">' + (item.year || '') + ' · ' + esc(item.release_date || '') + '</div>' +
              (daysUntil ? '<div class="md-activity-countdown">' + esc(daysUntil) + '</div>' : '') +
            '</div>' +
          '</div>';
        }).join('') +
        '</div></div>';
    }

    if (!html) {
      html = '<div class="md-loading">No active downloads or upcoming releases</div>';
    }

    content.innerHTML = html;
  }

  function startActivityPolling() {
    stopActivityPolling();
    MD.activityTimer = setInterval(function () {
      if (!MD.active) {
        stopActivityPolling();
        return;
      }
      apiGet('TentacleDiscover/Activity').then(function (data) {
        MD.activityData = data;
        // Always update tab badge count
        var activitySection = MD.sections && MD.sections.find(function (s) { return s.id === 'activity'; });
        if (activitySection) {
          activitySection._activityCount = (data.downloads || []).length + (data.unreleased || []).length;
          var tabBtn = document.querySelector('.md-section-tab[data-section="activity"] .md-section-count');
          if (tabBtn) tabBtn.textContent = activitySection._activityCount;
        }
        // Only re-render if user is viewing the activity tab
        if (MD.activeSection === 'activity') {
          renderActivity();
        }
      }).catch(function () {});
    }, 3000);
  }

  function stopActivityPolling() {
    if (MD.activityTimer) {
      clearInterval(MD.activityTimer);
      MD.activityTimer = null;
    }
  }

  function esc(str) {
    if (!str) return '';
    var d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  // ── Start ─────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', waitForReady);
  } else {
    waitForReady();
  }

  var navHandler = function () {
    if (MD.active && !isHomePage()) hideDiscover();
    setTimeout(tryInject, 500);
  };
  window.addEventListener('popstate', navHandler);
  window.addEventListener('pageshow', navHandler);

  var origPush = history.pushState;
  history.pushState = function () {
    origPush.apply(history, arguments);
    navHandler();
  };
  var origReplace = history.replaceState;
  history.replaceState = function () {
    origReplace.apply(history, arguments);
    navHandler();
  };

})();
