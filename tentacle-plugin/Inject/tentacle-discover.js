// Tentacle Discover & Activity — standalone overlays for Jellyfin
// v5 — Discover and Activity as independent full-screen pages
(function () {
  'use strict';
  console.log('[Tentacle] v5 — Discover + Activity standalone overlays');

  // ── Shared state ────────────────────────────────────────────────────
  var MD = {
    initialized: false,
    enabled: null,
    sections: null,
    mediaFilter: 'movies',
    activeSection: null,
    active: false,
    loaded: false,
    loadedAt: 0,          // timestamp of last render — stale after 5 min
    activityData: null,   // shared — used for download badges on discover cards
  };

  var ACT = {
    active: false,
    loaded: false,
    timer: null,
  };

  // ── Bootstrap ───────────────────────────────────────────────────────
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
    // Navigation listeners are registered once at the bottom of the IIFE (navHandler)
    // to avoid duplicate firing. hashchange/popstate/pushState/replaceState all
    // funnel through navHandler which handles hide + tryInject.
  }

  function isHomePage() {
    var h = location.hash || '';
    return h === '' || h === '#/' || h === '#/home.html' || h === '#/home';
  }

  var _tryInjectTimer = null;
  var _tryInjectRetries = 0;
  function tryInject() {
    if (_tryInjectTimer) { clearTimeout(_tryInjectTimer); _tryInjectTimer = null; }
    if (!isHomePage()) { _tryInjectRetries = 0; return; }
    var slider = document.querySelector('.emby-tabs-slider');
    if (!slider) {
      if (_tryInjectRetries < 20) {
        _tryInjectRetries++;
        _tryInjectTimer = setTimeout(tryInject, 300);
      }
      return;
    }
    _tryInjectRetries = 0;

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

    // Always start background polling for navbar badge
    startBadgePolling();
  }

  // ── Tab button ──────────────────────────────────────────────────────
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

  // ── API helpers ─────────────────────────────────────────────────────
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

  function esc(str) {
    if (!str) return '';
    var d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  // ════════════════════════════════════════════════════════════════════
  //  DISCOVER MODULE
  // ════════════════════════════════════════════════════════════════════

  function getOrCreateDiscoverOverlay() {
    var el = document.getElementById('mdDiscoverOverlay');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'mdDiscoverOverlay';
    el.className = 'md-discover-overlay';
    el.style.display = 'none';
    document.body.appendChild(el);
    return el;
  }

  function showDiscover() {
    // Hide activity if open
    if (ACT.active) hideActivity();

    MD.active = true;

    document.querySelectorAll('.emby-tabs-slider .emby-tab-button').forEach(function (b) {
      b.classList.remove('emby-tab-button-active');
    });
    var btn = document.getElementById('mdDiscoverTab');
    if (btn) btn.classList.add('emby-tab-button-active');

    // Reset loaded flag if overlay was removed from DOM (SPA cleanup)
    if (MD.loaded && !document.getElementById('mdDiscoverOverlay')) {
      MD.loaded = false;
    }

    var overlay = getOrCreateDiscoverOverlay();
    overlay.style.display = 'block';

    var stale = MD.loadedAt && (Date.now() - MD.loadedAt > 5 * 60 * 1000);
    if (!MD.loaded || stale) {
      MD.loaded = true;
      MD.loadedAt = Date.now();
      renderDiscoverPage(overlay);
    }
  }

  function hideDiscover() {
    MD.active = false;

    var btn = document.getElementById('mdDiscoverTab');
    if (btn) btn.classList.remove('emby-tab-button-active');

    var overlay = document.getElementById('mdDiscoverOverlay');
    if (overlay) overlay.style.display = 'none';
  }

  // ── Render discover page skeleton ───────────────────────────────────
  function renderDiscoverPage(container) {
    container.innerHTML =
      '<div class="md-discover-header">' +
        '<div class="md-discover-title">Discover</div>' +
        '<div class="md-filter-group">' +
          '<button class="md-filter-btn md-active" data-mdtype="movies">Movies</button>' +
          '<button class="md-filter-btn" data-mdtype="series">TV Shows</button>' +
        '</div>' +
      '</div>' +
      '<div class="md-section-tabs" id="mdSectionTabs"></div>' +
      '<div id="mdDiscoverContent" class="md-discover-content">' +
        '<div class="md-loading"><div class="md-spinner"></div><br>Loading...</div>' +
      '</div>';

    // Media type filter
    container.querySelectorAll('.md-filter-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        container.querySelectorAll('.md-filter-btn').forEach(function (x) { x.classList.remove('md-active'); });
        b.classList.add('md-active');
        MD.mediaFilter = b.getAttribute('data-mdtype');
        MD.activeSection = null;
        fetchDiscoverData();
      });
    });

    fetchDiscoverData();
  }

  // ── Fetch discover sections ─────────────────────────────────────────
  function fetchDiscoverData() {
    var content = document.getElementById('mdDiscoverContent');
    if (content) content.innerHTML = '<div class="md-loading"><div class="md-spinner"></div><br>Loading...</div>';

    var typeParam = MD.mediaFilter === 'series' ? 'series' : 'movies';

    // Fetch discover items and activity data (for download badges) in parallel
    var itemsPromise = apiGet('TentacleDiscover/Items?type=' + typeParam);
    var activityPromise = apiGet('TentacleDiscover/Activity').catch(function () {
      return { downloads: [], unreleased: [] };
    });

    Promise.all([itemsPromise, activityPromise]).then(function (results) {
      var data = results[0];
      var activity = results[1];
      MD.activityData = activity;
      MD.sections = data.sections || [];

      // Dispatch activity count for navbar badge
      var actCount = (activity.downloads || []).length + (activity.unreleased || []).length;
      window.dispatchEvent(new CustomEvent('tentacle-activity-count', { detail: actCount }));

      renderSectionTabs();
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

  // ── Section tabs ────────────────────────────────────────────────────
  var SECTION_LABELS = {
    popular: 'Popular',
    now_playing: 'Now Playing',
    upcoming: 'Upcoming',
    on_the_air: 'On the Air',
    top_rated: 'Top Rated',
    missing: 'From My Lists',
  };

  function renderSectionTabs() {
    var tabsEl = document.getElementById('mdSectionTabs');
    if (!tabsEl || !MD.sections) return;

    tabsEl.innerHTML = MD.sections.map(function (sec) {
      var count = sec.items.length;
      return '<button class="md-section-tab" data-section="' + sec.id + '">' +
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

    var section = MD.sections && MD.sections.find(function (s) { return s.id === sectionId; });
    if (!section) return;

    renderGrid(section);
  }

  // ── Grid rendering ──────────────────────────────────────────────────
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
        var listTag = item.list_name
          ? '<div class="md-card-list-tag">' + esc(item.list_name) + '</div>'
          : '';
        return '<div class="md-card" data-tmdb="' + item.tmdb_id + '" data-type="' + (item.media_type || 'movie') + '">' +
          '<div class="md-card-poster">' + poster + badge + addBtn + '</div>' +
          '<div class="md-card-info">' +
            '<div class="md-card-title">' + esc(item.title) + '</div>' +
            '<div class="md-card-meta">' + (item.year || '\u2014') + ' \u00b7 \u2605 ' + (item.rating || '\u2014') + '</div>' +
            listTag +
          '</div></div>';
      }).join('') +
    '</div>';

    // Card click -> detail modal
    content.querySelectorAll('.md-card').forEach(function (card) {
      card.addEventListener('click', function (e) {
        if (e.target.closest('.md-card-add')) return;
        var tmdb = parseInt(card.getAttribute('data-tmdb'), 10);
        var item = findItem(tmdb);
        if (item) showDetailModal(item);
      });
    });

    // Add button click -> detail modal
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
    if (!MD.sections) return null;
    for (var i = 0; i < MD.sections.length; i++) {
      var items = MD.sections[i].items;
      for (var j = 0; j < items.length; j++) {
        if (items[j].tmdb_id === tmdbId) return items[j];
      }
    }
    return null;
  }

  function getDownloadInfo(tmdbId) {
    if (!tmdbId || !MD.activityData || !MD.activityData.downloads) return null;
    return MD.activityData.downloads.find(function (dl) { return dl.tmdb_id == tmdbId; }) || null;
  }

  // ── Detail Modal ────────────────────────────────────────────────────
  function showDetailModal(item) {
    var mediaType = item.media_type === 'series' ? 'series' : 'movie';
    apiGet('TentacleDiscover/Detail/' + mediaType + '/' + item.tmdb_id).then(function (details) {
      if (details && !details.error) {
        item.overview = details.overview || item.overview || '';
        item.rating = details.rating || item.rating;
        item.backdrop_path = details.backdrop_path || item.backdrop_path || null;
        item.year = details.year || item.year;
        if (details.library_source) item.library_source = details.library_source;
        if (details.in_library !== undefined) item.in_library = details.in_library;
      }
      renderModal(item);
    }).catch(function () {
      renderModal(item);
    });
  }

  function renderModal(item) {
    var old = document.getElementById('mdDetailModal');
    if (old) old.remove();
    MD._currentItem = item;

    var backdrop = item.backdrop_path
      ? '<img src="https://image.tmdb.org/t/p/w780' + item.backdrop_path + '">'
      : '';

    var downloadSection = '';
    var modalDlInfo = getDownloadInfo(item.tmdb_id);
    if (modalDlInfo) {
      var dlPct = (modalDlInfo.progress || 0).toFixed(1);
      var dlStatus = modalDlInfo.status === 'importing' ? 'Importing...' : modalDlInfo.status === 'queued' ? 'Queued' : 'Downloading ' + dlPct + '%';
      var dlEta = modalDlInfo.eta ? ' \u00b7 ETA ' + modalDlInfo.eta : '';
      var dlSize = modalDlInfo.size_remaining ? ' \u00b7 ' + modalDlInfo.size_remaining + ' left' : '';
      downloadSection =
        '<div class="md-inlib-row">' +
          '<div class="md-downloading-badge">' + dlStatus + dlEta + dlSize + '</div>' +
        '</div>';
    } else if (item.in_library) {
      var isSonarr = item.media_type === 'series' && item.library_source === 'sonarr';
      var isVod = item.media_type === 'series' && item.library_source && item.library_source.indexOf('provider_') === 0;
      var extraBtn = isSonarr
        ? '<button id="mdManageEpisodes" class="md-view-library-btn" style="margin-left:8px">Manage Episodes</button>'
        : isVod
        ? '<button id="mdDownloadMore" class="md-view-library-btn" style="margin-left:8px">Download More Episodes</button>'
        : '';
      downloadSection =
        '<div class="md-inlib-row">' +
          '<div class="md-inlib-badge">\u2713 Already in library</div>' +
          '<button id="mdViewInLibrary" class="md-view-library-btn">View in Library</button>' +
          extraBtn +
        '</div>' +
        '<div id="mdManageSection" style="display:none">' +
          '<div class="md-ep-picker">' +
            '<div class="md-ep-picker-header">' +
              '<span>Episodes</span>' +
              '<span><button class="md-ep-btn" onclick="window._mdEpSelectAll()">All</button>' +
              '<button class="md-ep-btn" onclick="window._mdEpSelectNone()">None</button></span>' +
            '</div>' +
            '<div id="mdEpSeasons" class="md-ep-seasons"></div>' +
          '</div>' +
          '<div id="mdDownloadMoreQuality" style="display:none;margin-top:8px">' +
            '<label style="font-size:12px;color:rgba(255,255,255,.6)">Quality Profile</label>' +
            '<select id="mdDownloadMoreProfile" class="md-select" style="margin-top:4px"></select>' +
          '</div>' +
          '<div style="margin-top:10px;display:flex;gap:8px;justify-content:flex-end">' +
            '<button id="mdManageSaveBtn" class="md-download-btn" style="font-size:13px;padding:8px 20px">Save Changes</button>' +
          '</div>' +
          '<div id="mdDownloadStatus" class="md-download-status"></div>' +
        '</div>';
    } else {
      var isSeries = item.media_type === 'series';
      var arrLabel = isSeries ? 'Sonarr' : 'Radarr';
      var monitorSelect = isSeries
        ? '<select id="mdMonitorSelect" class="md-select">' +
            '<option value="all">All Episodes (including future)</option>' +
            '<option value="firstSeason">First Season</option>' +
            '<option value="lastSeason">Last Season</option>' +
            '<option value="pilot">Pilot Only</option>' +
            '<option value="custom">Pick Episodes...</option>' +
          '</select>'
        : '';
      var episodePicker = isSeries
        ? '<div id="mdEpisodePicker" class="md-ep-picker" style="display:none">' +
            '<div class="md-ep-picker-header">' +
              '<span>Episodes</span>' +
              '<span><button class="md-ep-btn" onclick="window._mdEpSelectAll()">All</button>' +
              '<button class="md-ep-btn" onclick="window._mdEpSelectNone()">None</button></span>' +
            '</div>' +
            '<div id="mdEpSeasons" class="md-ep-seasons"></div>' +
          '</div>'
        : '';
      downloadSection =
        '<div class="md-download-row">' +
          '<select id="mdProfileSelect" class="md-select"><option>Loading...</option></select>' +
          monitorSelect +
          '<button id="mdDownloadBtn" class="md-download-btn">Add to ' + arrLabel + '</button>' +
        '</div>' +
        episodePicker +
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
          '<div class="md-modal-subtitle">' + (item.year || '') + ' \u00b7 ' + (item.media_type === 'movie' ? 'Movie' : 'TV Show') + ' \u00b7 \u2605 ' + (item.rating || '\u2014') + '</div>' +
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
          viewBtn.textContent = 'Finding\u2026';
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
      var manageBtn = overlay.querySelector('#mdManageEpisodes');
      if (manageBtn) {
        manageBtn.addEventListener('click', function () {
          _mdLoadManageEpisodes(item);
        });
      }
      var dlMoreBtn = overlay.querySelector('#mdDownloadMore');
      if (dlMoreBtn) {
        dlMoreBtn.addEventListener('click', function () {
          _mdLoadDownloadMore(item);
        });
      }
    } else {
      loadDownloadOptions(item);
      var monitorSel = overlay.querySelector('#mdMonitorSelect');
      if (monitorSel) {
        MD._epSeasons = [];
        MD._epLoaded = {};
        monitorSel.addEventListener('change', function () {
          var picker = document.getElementById('mdEpisodePicker');
          if (!picker) return;
          if (monitorSel.value !== 'custom') { picker.style.display = 'none'; return; }
          picker.style.display = 'block';
          if (MD._epSeasons.length > 0) return;
          var container = document.getElementById('mdEpSeasons');
          container.innerHTML = '<div style="padding:12px;color:#999;font-size:13px">Loading seasons...</div>';
          apiGet('TentacleDiscover/Seasons/' + item.tmdb_id).then(function (data) {
            MD._epSeasons = (data.seasons || []).filter(function (s) { return s.season_number > 0; });
            _mdRenderSeasons();
          }).catch(function () {
            container.innerHTML = '<div style="padding:12px;color:#999;font-size:13px">Failed to load</div>';
          });
        });
      }
    }
  }

  // ── Episode picker helpers (shared by Discover modal) ───────────────
  function _mdRenderSeasons() {
    var container = document.getElementById('mdEpSeasons');
    if (!container) return;
    container.innerHTML = MD._epSeasons.map(function (s) {
      var sn = s.season_number;
      var airYear = s.air_date ? ' (' + s.air_date.substring(0, 4) + ')' : '';
      var total = s.episode_count || 0;
      var vodEps = (MD._vodEpisodes || {})[String(sn)] || (MD._vodEpisodes || {})[sn] || [];
      var dlEps = (MD._dlEpisodes || {})[String(sn)] || (MD._dlEpisodes || {})[sn] || [];
      var haveSet = {}; vodEps.forEach(function(e){haveSet[e]=1}); dlEps.forEach(function(e){haveSet[e]=1});
      var haveCount = Object.keys(haveSet).length;
      var countText, countClass, checked;
      if (haveCount === 0) { countText = total + ' ep'; countClass = 'md-ep-count'; checked = false; }
      else if (haveCount >= total) { countText = total + '/' + total; countClass = 'md-ep-count md-count-full'; checked = true; }
      else { countText = haveCount + '/' + total; countClass = 'md-ep-count md-count-partial'; checked = false; }
      return '<div class="md-ep-season" data-season="' + sn + '">' +
        '<div class="md-ep-season-hdr" onclick="window._mdToggleSeason(' + sn + ')">' +
          '<span class="md-ep-arrow" id="mdEpArrow' + sn + '">&#9654;</span>' +
          '<input type="checkbox" ' + (checked ? 'checked' : '') + ' ' + (haveCount >= total && total > 0 ? 'disabled' : '') + ' onclick="event.stopPropagation();window._mdToggleSeasonAll(' + sn + ',this.checked)">' +
          '<span>' + esc(s.name || 'Season ' + sn) + airYear + '</span>' +
          '<span class="' + countClass + '">' + countText + '</span>' +
        '</div>' +
        '<div class="md-ep-list" id="mdEpList' + sn + '"></div>' +
      '</div>';
    }).join('');
  }

  window._mdToggleSeason = function (sn) {
    var list = document.getElementById('mdEpList' + sn);
    var arrow = document.getElementById('mdEpArrow' + sn);
    if (!list) return;
    var isOpen = list.classList.contains('open');
    if (isOpen) { list.classList.remove('open'); if (arrow) arrow.classList.remove('open'); return; }
    list.classList.add('open'); if (arrow) arrow.classList.add('open');
    if (!MD._epLoaded[sn]) {
      list.innerHTML = '<div style="padding:8px 12px;color:#999;font-size:12px">Loading...</div>';
      apiGet('TentacleDiscover/Season/' + MD._currentItem.tmdb_id + '/' + sn).then(function (data) {
        MD._epLoaded[sn] = data.episodes || [];
        _mdRenderEpisodes(sn);
      }).catch(function () {
        list.innerHTML = '<div style="padding:8px 12px;color:#999;font-size:12px">Failed</div>';
      });
    }
  };

  function _mdRenderEpisodes(sn) {
    var eps = MD._epLoaded[sn] || [];
    var list = document.getElementById('mdEpList' + sn);
    if (!list) return;
    var vodEps = (MD._vodEpisodes || {})[String(sn)] || (MD._vodEpisodes || {})[sn] || [];
    var dlEps = (MD._dlEpisodes || {})[String(sn)] || (MD._dlEpisodes || {})[sn] || [];
    list.innerHTML = eps.map(function (ep) {
      var num = 'S' + String(sn).padStart(2, '0') + 'E' + String(ep.episode_number).padStart(2, '0');
      var airDate = ep.air_date ? ep.air_date.substring(0, 10) : '';
      var isVod = vodEps.indexOf(ep.episode_number) !== -1;
      var isDl = dlEps.indexOf(ep.episode_number) !== -1;
      if (isVod || isDl) {
        var badgeText = isVod ? 'VOD' : 'DL';
        var badgeClass = isVod ? 'md-badge-vod' : 'md-badge-dl';
        return '<label class="md-ep-row md-ep-have">' +
          '<input type="checkbox" checked disabled data-season="' + sn + '" data-episode="' + ep.episode_number + '">' +
          '<span class="md-ep-num">' + num + '</span>' +
          '<span class="md-ep-title">' + esc(ep.name || '') + '</span>' +
          '<span class="md-ep-dl ' + badgeClass + '">' + badgeText + '</span>' +
        '</label>';
      }
      return '<label class="md-ep-row">' +
        '<input type="checkbox" data-season="' + sn + '" data-episode="' + ep.episode_number + '" onchange="window._mdUpdateSeasonCb(' + sn + ')">' +
        '<span class="md-ep-num">' + num + '</span>' +
        '<span class="md-ep-title">' + esc(ep.name || '') + '</span>' +
        '<span class="md-ep-date">' + airDate + '</span>' +
      '</label>';
    }).join('');
    _mdUpdateSeasonStatus(sn, eps.length);
  }

  function _mdUpdateSeasonStatus(sn, totalEps) {
    var vodEps = (MD._vodEpisodes || {})[String(sn)] || (MD._vodEpisodes || {})[sn] || [];
    var dlEps = (MD._dlEpisodes || {})[String(sn)] || (MD._dlEpisodes || {})[sn] || [];
    var haveSet = {}; vodEps.forEach(function(e){haveSet[e]=1}); dlEps.forEach(function(e){haveSet[e]=1});
    var haveCount = Object.keys(haveSet).length;
    var seasonEl = document.querySelector('.md-ep-season[data-season="' + sn + '"]');
    if (!seasonEl) return;
    var seasonCb = seasonEl.querySelector('.md-ep-season-hdr input[type="checkbox"]');
    if (seasonCb) { var full = haveCount >= totalEps && totalEps > 0; seasonCb.checked = full; seasonCb.disabled = full; }
    var countEl = seasonEl.querySelector('.md-ep-count');
    if (!countEl) return;
    if (haveCount === 0) { countEl.textContent = totalEps + ' ep'; countEl.className = 'md-ep-count'; }
    else if (haveCount >= totalEps) { countEl.textContent = totalEps + '/' + totalEps; countEl.className = 'md-ep-count md-count-full'; }
    else { countEl.textContent = haveCount + '/' + totalEps; countEl.className = 'md-ep-count md-count-partial'; }
  }

  window._mdToggleSeasonAll = function (sn, checked) {
    var list = document.getElementById('mdEpList' + sn);
    if (!list) return;
    if (checked && !list.classList.contains('open')) {
      window._mdToggleSeason(sn);
    }
    setTimeout(function () {
      list.querySelectorAll('input[type="checkbox"]:not(:disabled)').forEach(function (cb) { cb.checked = checked; });
    }, 100);
  };

  window._mdUpdateSeasonCb = function (sn) {
    var list = document.getElementById('mdEpList' + sn);
    if (!list) return;
    var cbs = list.querySelectorAll('input[type="checkbox"]:not(:disabled)');
    var allChecked = Array.from(cbs).length > 0 && Array.from(cbs).every(function (cb) { return cb.checked; });
    var seasonCb = document.querySelector('.md-ep-season[data-season="' + sn + '"] .md-ep-season-hdr input[type="checkbox"]');
    if (seasonCb) seasonCb.checked = allChecked;
  };

  window._mdEpSelectAll = function () {
    document.querySelectorAll('#mdEpSeasons input[type="checkbox"]:not(:disabled)').forEach(function (cb) { cb.checked = true; });
  };

  window._mdEpSelectNone = function () {
    document.querySelectorAll('#mdEpSeasons input[type="checkbox"]:not(:disabled)').forEach(function (cb) { cb.checked = false; });
  };

  function _mdGetSelectedEpisodes() {
    var selected = [];
    document.querySelectorAll('#mdEpSeasons .md-ep-row input[type="checkbox"]:checked:not(:disabled)').forEach(function (cb) {
      selected.push({ season: parseInt(cb.dataset.season, 10), episode: parseInt(cb.dataset.episode, 10) });
    });
    return selected;
  }

  function _mdLoadManageEpisodes(item) {
    var section = document.getElementById('mdManageSection');
    if (!section) return;
    section.style.display = 'block';
    var container = document.getElementById('mdEpSeasons');
    container.innerHTML = '<div style="padding:12px;color:#999;font-size:13px">Loading episodes...</div>';

    var uid = window.ApiClient.getCurrentUserId();
    apiGet('TentacleDiscover/SonarrEpisodes/' + item.tmdb_id + '?userId=' + uid).then(function (data) {
      if (!data.in_sonarr) {
        container.innerHTML = '<div style="padding:12px;color:#999;font-size:13px">Series not found in Sonarr</div>';
        return;
      }
      var seasonMap = {};
      (data.episodes || []).forEach(function (ep) {
        if (ep.seasonNumber === 0) return;
        if (!seasonMap[ep.seasonNumber]) seasonMap[ep.seasonNumber] = [];
        seasonMap[ep.seasonNumber].push(ep);
      });
      var seasonNums = Object.keys(seasonMap).map(Number).sort(function (a, b) { return a - b; });

      container.innerHTML = seasonNums.map(function (sn) {
        var eps = seasonMap[sn];
        var allMonitored = eps.every(function (e) { return e.monitored; });
        return '<div class="md-ep-season" data-season="' + sn + '">' +
          '<div class="md-ep-season-hdr" onclick="window._mdToggleSeason(' + sn + ')">' +
            '<span class="md-ep-arrow" id="mdEpArrow' + sn + '">&#9654;</span>' +
            '<input type="checkbox" ' + (allMonitored ? 'checked' : '') + ' onclick="event.stopPropagation();window._mdToggleSeasonAll(' + sn + ',this.checked)">' +
            '<span>Season ' + sn + '</span>' +
            '<span class="md-ep-count">' + eps.length + ' ep</span>' +
          '</div>' +
          '<div class="md-ep-list" id="mdEpList' + sn + '"></div>' +
        '</div>';
      }).join('');

      MD._epLoaded = {};
      seasonNums.forEach(function (sn) {
        MD._epLoaded[sn] = true;
        var list = document.getElementById('mdEpList' + sn);
        list.innerHTML = seasonMap[sn].map(function (ep) {
          var num = 'S' + String(sn).padStart(2, '0') + 'E' + String(ep.episodeNumber).padStart(2, '0');
          var dlBadge = ep.hasFile ? '<span class="md-ep-dl">\u2713</span>' : '';
          return '<label class="md-ep-row">' +
            '<input type="checkbox" ' + (ep.monitored ? 'checked' : '') + ' data-season="' + sn + '" data-episode="' + ep.episodeNumber + '" onchange="window._mdUpdateSeasonCb(' + sn + ')">' +
            '<span class="md-ep-num">' + num + '</span>' +
            '<span class="md-ep-title">' + esc(ep.title || '') + '</span>' +
            dlBadge +
          '</label>';
        }).join('');
      });
    }).catch(function () {
      container.innerHTML = '<div style="padding:12px;color:#999;font-size:13px">Failed to load</div>';
    });

    var saveBtn = document.getElementById('mdManageSaveBtn');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        var selected = _mdGetSelectedEpisodes();
        saveBtn.disabled = true;
        saveBtn.textContent = 'Saving...';
        var status = document.getElementById('mdDownloadStatus');
        var uid2 = window.ApiClient.getCurrentUserId();
        apiPost('TentacleDiscover/ManageEpisodes?userId=' + uid2, {
          tmdb_id: item.tmdb_id,
          selected_episodes: selected,
        }).then(function (r) {
          if (status) { status.style.color = '#4caf50'; status.textContent = '\u2713 Updated \u2014 monitoring ' + (r.monitored || 0) + ' episode(s)' + (r.searching ? ', searching ' + r.searching + ' new' : ''); }
          saveBtn.textContent = 'Saved!';
        }).catch(function (err) {
          if (status) { status.style.color = '#f44336'; status.textContent = 'Error: ' + (err.message || 'Failed'); }
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save Changes';
        });
      });
    }
  }

  function _mdLoadDownloadMore(item) {
    var section = document.getElementById('mdManageSection');
    if (!section) return;
    section.style.display = 'block';
    var container = document.getElementById('mdEpSeasons');
    container.innerHTML = '<div style="padding:12px;color:#999;font-size:13px">Loading episodes...</div>';

    var qualityDiv = document.getElementById('mdDownloadMoreQuality');
    if (qualityDiv) qualityDiv.style.display = 'block';
    var profileSelect = document.getElementById('mdDownloadMoreProfile');
    if (profileSelect) {
      profileSelect.innerHTML = '<option value="">Loading...</option>';
      var uid = window.ApiClient.getCurrentUserId();
      apiGet('TentacleDiscover/SonarrProfiles?userId=' + uid).then(function (profiles) {
        profileSelect.innerHTML = (profiles || []).map(function (p) {
          return '<option value="' + p.id + '">' + esc(p.name) + '</option>';
        }).join('');
      }).catch(function () {
        profileSelect.innerHTML = '<option value="">Failed</option>';
      });
    }

    MD._vodEpisodes = {};
    MD._dlEpisodes = {};
    MD._epSeasons = [];
    MD._epLoaded = {};

    var uid = window.ApiClient.getCurrentUserId();
    Promise.all([
      apiGet('TentacleDiscover/Seasons/' + item.tmdb_id),
      apiGet('TentacleDiscover/VodEpisodes/' + item.tmdb_id),
      apiGet('TentacleDiscover/SonarrEpisodes/' + item.tmdb_id + '?userId=' + uid).catch(function () { return { in_sonarr: false }; }),
    ]).then(function (results) {
      var seasonsData = results[0];
      var vodData = results[1];
      var sonarrData = results[2];

      if (vodData && vodData.has_episodes) {
        MD._vodEpisodes = vodData.episodes || {};
      }

      if (sonarrData && sonarrData.in_sonarr && sonarrData.episodes) {
        sonarrData.episodes.forEach(function (ep) {
          if (ep.hasFile && ep.seasonNumber > 0) {
            var vodList = (MD._vodEpisodes || {})[String(ep.seasonNumber)] || (MD._vodEpisodes || {})[ep.seasonNumber] || [];
            if (vodList.indexOf(ep.episodeNumber) !== -1) return;
            if (!MD._dlEpisodes[ep.seasonNumber]) MD._dlEpisodes[ep.seasonNumber] = [];
            MD._dlEpisodes[ep.seasonNumber].push(ep.episodeNumber);
          }
        });
      }

      MD._epSeasons = (seasonsData.seasons || []).filter(function (s) { return s.season_number > 0; });
      _mdRenderSeasons();
    }).catch(function () {
      container.innerHTML = '<div style="padding:12px;color:#999;font-size:13px">Failed to load</div>';
    });

    var saveBtn = document.getElementById('mdManageSaveBtn');
    if (saveBtn) {
      saveBtn.textContent = 'Add to Sonarr';
      saveBtn.addEventListener('click', function () {
        var selected = _mdGetSelectedEpisodes();
        if (selected.length === 0) {
          var status = document.getElementById('mdDownloadStatus');
          if (status) { status.style.color = '#f44336'; status.textContent = 'Select at least one episode'; }
          return;
        }
        saveBtn.disabled = true;
        saveBtn.textContent = 'Adding...';
        var uid2 = window.ApiClient.getCurrentUserId();
        var profileId = profileSelect ? profileSelect.value : '';
        var body = {
          tmdb_ids: [item.tmdb_id],
          selected_episodes: selected,
        };
        if (profileId) body.quality_profile_id = parseInt(profileId, 10);
        apiPost('TentacleDiscover/AddToSonarr?userId=' + uid2, body).then(function (r) {
          var status2 = document.getElementById('mdDownloadStatus');
          if (r.added > 0) {
            if (status2) { status2.style.color = '#4caf50'; status2.textContent = '\u2713 Added \u2014 downloading ' + selected.length + ' episode(s)'; }
            saveBtn.textContent = 'Done!';
          } else if (r.already_exists > 0) {
            if (status2) { status2.style.color = '#ff9800'; status2.textContent = 'Already in Sonarr'; }
            saveBtn.disabled = false;
            saveBtn.textContent = 'Add to Sonarr';
          } else {
            if (status2) { status2.style.color = '#f44336'; status2.textContent = r.detail || 'Failed to add'; }
            saveBtn.disabled = false;
            saveBtn.textContent = 'Add to Sonarr';
          }
        }).catch(function (err) {
          var status3 = document.getElementById('mdDownloadStatus');
          if (status3) { status3.style.color = '#f44336'; status3.textContent = 'Error: ' + (err.message || 'Failed'); }
          saveBtn.disabled = false;
          saveBtn.textContent = 'Add to Sonarr';
        });
      });
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
      var monitorVal = monitorEl ? monitorEl.value : 'all';
      body.season_folder = true;
      if (monitorVal === 'custom') {
        var selected = _mdGetSelectedEpisodes();
        if (selected.length === 0) {
          status.style.color = '#f44336';
          status.textContent = 'Select at least one episode';
          btn.disabled = false;
          btn.textContent = 'Add to Sonarr';
          return;
        }
        body.monitor = 'none';
        body.selected_episodes = selected;
      } else {
        body.monitor = monitorVal;
      }
    }

    apiPost(ep, body).then(function () {
      status.style.color = '#4caf50';
      status.textContent = '\u2713 Added to ' + (isSeries ? 'Sonarr' : 'Radarr');
      btn.textContent = 'Added!';
      item.in_library = true;
    }).catch(function (err) {
      status.style.color = '#f44336';
      status.textContent = 'Error: ' + (err.message || 'Failed');
      btn.disabled = false;
      btn.textContent = 'Retry';
    });
  }


  // ════════════════════════════════════════════════════════════════════
  //  ACTIVITY MODULE (standalone overlay)
  // ════════════════════════════════════════════════════════════════════

  function getOrCreateActivityOverlay() {
    var el = document.getElementById('mdActivityOverlay');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'mdActivityOverlay';
    el.className = 'md-activity-overlay';
    el.style.display = 'none';
    document.body.appendChild(el);
    return el;
  }

  function showActivity() {
    // Hide discover if open
    if (MD.active) hideDiscover();

    ACT.active = true;

    // Reset loaded flag if overlay was removed from DOM (SPA cleanup)
    if (ACT.loaded && !document.getElementById('mdActivityOverlay')) {
      ACT.loaded = false;
    }

    var overlay = getOrCreateActivityOverlay();
    overlay.style.display = 'block';

    if (!ACT.loaded) {
      ACT.loaded = true;
      renderActivityPage(overlay);
    } else {
      // Refresh data on re-open
      fetchActivityData();
    }

    startActivityPolling();
  }

  function hideActivity() {
    ACT.active = false;
    stopActivityPolling();

    var overlay = document.getElementById('mdActivityOverlay');
    if (overlay) overlay.style.display = 'none';
  }

  function renderActivityPage(container) {
    container.innerHTML =
      '<div class="md-act-header">' +
        '<div class="md-act-title-row">' +
          '<div class="md-act-title">Activity</div>' +
          '<div class="md-act-summary" id="mdActSummary"></div>' +
        '</div>' +
      '</div>' +
      '<div id="mdActContent" class="md-act-content">' +
        '<div class="md-loading"><div class="md-spinner"></div><br>Loading...</div>' +
      '</div>';

    fetchActivityData();
  }

  function fetchActivityData() {
    apiGet('TentacleDiscover/Activity').then(function (data) {
      MD.activityData = data;
      renderActivityContent(data);

      // Update navbar badge
      var count = (data.downloads || []).length + (data.unreleased || []).length;
      window.dispatchEvent(new CustomEvent('tentacle-activity-count', { detail: count }));
    }).catch(function () {
      var c = document.getElementById('mdActContent');
      if (c) c.innerHTML = '<div class="md-loading">Failed to load activity data</div>';
    });
  }

  function renderActivityContent(data) {
    var content = document.getElementById('mdActContent');
    if (!content) return;

    var downloads = data.downloads || [];
    var unreleased = data.unreleased || [];

    // Update summary
    var summary = document.getElementById('mdActSummary');
    if (summary) {
      var parts = [];
      if (downloads.length > 0) parts.push(downloads.length + ' downloading');
      if (unreleased.length > 0) parts.push(unreleased.length + ' upcoming');
      summary.textContent = parts.length > 0 ? parts.join(' \u00b7 ') : 'All clear';
    }

    var html = '';

    if (downloads.length > 0) {
      html += '<div class="md-act-section">' +
        '<div class="md-act-section-header">' +
          '<svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>' +
          '<span>Downloads</span>' +
          '<span class="md-act-section-count">' + downloads.length + '</span>' +
        '</div>' +
        '<div class="md-act-dl-grid">' +
        downloads.map(function (dl) {
          var poster = dl.poster_path
            ? '<img src="https://image.tmdb.org/t/p/w185' + dl.poster_path + '" loading="lazy" onerror="this.parentElement.innerHTML=\'<div class=md-act-poster-ph>&#9707;</div>\'">'
            : '<div class="md-act-poster-ph">&#9707;</div>';
          var statusClass = dl.status === 'importing' ? 'importing' :
            dl.status === 'queued' ? 'queued' :
            dl.status === 'warning' ? 'warning' : 'downloading';
          var statusLabel = dl.status === 'importing' ? 'Importing' :
            dl.status === 'queued' ? 'Queued' :
            dl.status === 'warning' ? 'Warning' : 'Downloading';
          var progressPct = Math.min(Math.max(dl.progress || 0, 0), 100);
          var epLabel = dl.episode ? ' \u00b7 ' + esc(dl.episode) : '';
          var etaLabel = dl.eta ? esc(dl.eta) : '';
          var sizeLabel = dl.size_remaining ? esc(dl.size_remaining) + ' left' : '';
          var qualityLabel = dl.quality ? esc(dl.quality) : '';

          return '<div class="md-act-card md-act-status-' + statusClass + '">' +
            '<div class="md-act-poster">' + poster + '</div>' +
            '<div class="md-act-info">' +
              '<div class="md-act-card-title">' + esc(dl.title) + epLabel + '</div>' +
              '<div class="md-act-card-meta">' +
                '<span class="md-act-status-badge md-act-badge-' + statusClass + '">' + statusLabel + '</span>' +
                (qualityLabel ? '<span>' + qualityLabel + '</span>' : '') +
                (sizeLabel ? '<span>' + sizeLabel + '</span>' : '') +
              '</div>' +
              '<div class="md-act-progress-row">' +
                '<div class="md-act-progress-bar"><div class="md-act-progress-fill md-act-fill-' + statusClass + '" style="width:' + progressPct + '%"></div></div>' +
                '<span class="md-act-progress-text">' + progressPct.toFixed(1) + '%' + (etaLabel ? ' \u00b7 ' + etaLabel : '') + '</span>' +
              '</div>' +
            '</div>' +
          '</div>';
        }).join('') +
        '</div></div>';
    }

    if (unreleased.length > 0) {
      html += '<div class="md-act-section">' +
        '<div class="md-act-section-header">' +
          '<svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z"/></svg>' +
          '<span>Upcoming Releases</span>' +
          '<span class="md-act-section-count">' + unreleased.length + '</span>' +
        '</div>' +
        '<div class="md-act-upcoming-grid">' +
        unreleased.map(function (item) {
          var poster = item.poster_path
            ? '<img src="https://image.tmdb.org/t/p/w185' + item.poster_path + '" loading="lazy" onerror="this.parentElement.innerHTML=\'<div class=md-act-upcoming-ph>&#9707;</div>\'">'
            : '<div class="md-act-upcoming-ph">&#9707;</div>';
          var countdown = '';
          var countdownClass = '';
          if (item.release_date) {
            var rd = new Date(item.release_date + 'T00:00:00');
            var now = new Date();
            var diff = Math.ceil((rd - now) / 86400000);
            if (diff <= 0) { countdown = 'Releasing soon'; countdownClass = 'md-act-cd-soon'; }
            else if (diff === 1) { countdown = 'Tomorrow'; countdownClass = 'md-act-cd-soon'; }
            else if (diff <= 7) { countdown = diff + ' days'; countdownClass = 'md-act-cd-week'; }
            else { countdown = diff + ' days'; countdownClass = 'md-act-cd-later'; }
          }
          return '<div class="md-act-upcoming-card">' +
            '<div class="md-act-upcoming-poster">' + poster +
              (countdown ? '<div class="md-act-countdown-badge ' + countdownClass + '">' + countdown + '</div>' : '') +
            '</div>' +
            '<div class="md-act-upcoming-info">' +
              '<div class="md-act-upcoming-title">' + esc(item.title) + '</div>' +
              '<div class="md-act-upcoming-date">' + esc(item.release_date || '') + '</div>' +
            '</div>' +
          '</div>';
        }).join('') +
        '</div></div>';
    }

    if (!html) {
      html =
        '<div class="md-act-empty">' +
          '<svg viewBox="0 0 24 24" width="48" height="48"><path fill="currentColor" opacity="0.3" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>' +
          '<div>No active downloads or upcoming releases</div>' +
        '</div>';
    }

    content.innerHTML = html;
  }

  // ── Polling (shared) ────────────────────────────────────────────────
  function startActivityPolling() {
    stopActivityPolling();
    ACT.timer = setInterval(function () {
      if (!ACT.active && !MD.active) {
        stopActivityPolling();
        return;
      }
      apiGet('TentacleDiscover/Activity').then(function (data) {
        MD.activityData = data;
        var count = (data.downloads || []).length + (data.unreleased || []).length;
        window.dispatchEvent(new CustomEvent('tentacle-activity-count', { detail: count }));

        // Re-render activity page if visible
        if (ACT.active) {
          renderActivityContent(data);
        }
      }).catch(function () {});
    }, 3000);
  }

  function stopActivityPolling() {
    if (ACT.timer) {
      clearInterval(ACT.timer);
      ACT.timer = null;
    }
  }

  // Background badge polling — runs on home page even when overlays are closed
  var _badgeTimer = null;
  function startBadgePolling() {
    if (_badgeTimer) return; // already running
    _badgeTimer = setInterval(function () {
      if (!isHomePage()) { stopBadgePolling(); return; }
      // Don't poll if either overlay has its own polling
      if (ACT.timer) return;
      apiGet('TentacleDiscover/Activity').then(function (data) {
        MD.activityData = data;
        var count = (data.downloads || []).length + (data.unreleased || []).length;
        window.dispatchEvent(new CustomEvent('tentacle-activity-count', { detail: count }));
      }).catch(function () {});
    }, 10000); // slower interval for background
  }

  function stopBadgePolling() {
    if (_badgeTimer) {
      clearInterval(_badgeTimer);
      _badgeTimer = null;
    }
  }


  // ── Public API ──────────────────────────────────────────────────────
  window.TentacleDiscover = {
    show: function () {
      showDiscover();
    },
    // Backwards compat — now opens standalone Activity
    showActivity: function () {
      window.TentacleActivity.show();
    },
    hide: function () {
      hideDiscover();
    },
    isActive: function () {
      return MD.active;
    },
    // Shared functions for tentacle-search.js
    showDetailModal: showDetailModal,
    getDownloadInfo: getDownloadInfo,
    esc: esc,
  };

  window.TentacleActivity = {
    show: function () {
      showActivity();
    },
    hide: function () {
      hideActivity();
    },
    isActive: function () {
      return ACT.active;
    }
  };

  // ── Start ───────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', waitForReady);
  } else {
    waitForReady();
  }

  var _navTimer = null;
  var navHandler = function () {
    if (MD.active && !isHomePage()) hideDiscover();
    if (ACT.active && !isHomePage()) hideActivity();
    if (!isHomePage()) { stopBadgePolling(); }
    // Debounce tryInject to avoid multiple concurrent calls from rapid nav events
    if (_navTimer) clearTimeout(_navTimer);
    _navTimer = setTimeout(function () { _navTimer = null; tryInject(); }, 300);
  };
  window.addEventListener('hashchange', navHandler);
  window.addEventListener('popstate', navHandler);
  window.addEventListener('pageshow', navHandler);

})();
