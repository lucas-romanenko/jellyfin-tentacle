// Tentacle Home — replaces Jellyfin's default homepage with Tentacle sections
// Injected into index.html via Harmony patch on PhysicalFileProvider
//
// Architecture (Moonfin pattern — body-level overlay, immune to view transitions):
//   - Primary nav detection: `viewshow` event (Jellyfin's own SPA event, most reliable)
//   - Fallback nav detection: `hashchange` + `popstate`
//   - Body class `tentacle-home-active` hides ALL native .homeSectionsContainer in ALL views
//   - #tentacle-home mounted on .mainAnimatedPages (stable, outside Jellyfin's view elements)
//   - Generation counter cancels stale renders on rapid navigation
//   - On failure: cleanupHome() removes element + body class → native Jellyfin shows
(function () {
  'use strict';

  var MH = {
    initialized: false,
    heroInterval: null,
    heroIndex: 0,
    heroTotal: 0,
    apiClient: null,
    userId: null,
    generation: 0,      // incremented on every navigation, stale renders check this
    lastHash: null,      // dedup rapid event firing
    versionPollTimer: null,  // live-update polling timer
    lastVersion: -1,         // last known playlist version from backend
    activeSections: null,    // sections from last render (for targeted refresh)
  };

  // ── Bootstrap ─────────────────────────────────────────────────────────
  function waitForReady() {
    if (window.ApiClient && window.ApiClient.getCurrentUserId()) {
      MH.apiClient = window.ApiClient;
      MH.userId = MH.apiClient.getCurrentUserId();
      init();
    } else {
      setTimeout(waitForReady, 300);
    }
  }

  function init() {
    if (MH.initialized) return;
    MH.initialized = true;
    console.log('[TH] init() — attaching listeners');

    // Primary: viewshow — Jellyfin's own SPA navigation event (most reliable)
    // Fires on the view element, bubbles to document. e.detail.type = "home"
    document.addEventListener('viewshow', onViewShow);

    // Fallback: hashchange + popstate for edge cases (browser back/forward, direct hash set)
    window.addEventListener('hashchange', onNavChange);
    window.addEventListener('popstate', onNavChange);

    // Handle initial page load (viewshow may have already fired before our script loaded)
    console.log('[TH] init() — isHomePage()=' + isHomePage() + ' hash=' + JSON.stringify(location.hash));
    if (isHomePage()) {
      onHomePage();
    }
  }

  // ── Navigation Handlers ─────────────────────────────────────────────
  function onViewShow(e) {
    var type = e.detail && e.detail.type;
    var hash = location.hash || '';
    console.log('[TH] viewshow fired — type=' + JSON.stringify(type) + ' hash=' + JSON.stringify(hash) + ' ts=' + Date.now());
    // The home view's Favorites tab (tab=1) is native — isHomePage() is false
    // there, so keep the type==='home' fast-path (covers the hash lagging
    // viewshow on first load) but suppress it when the hash says a non-zero tab.
    var favTab = /[?&]tab=[1-9]/.test(hash);
    if ((type === 'home' && !favTab) || isHomePage()) {
      onHomePage();
    } else {
      onLeavingHome();
    }
  }

  function onNavChange() {
    var hash = location.hash || '';
    console.log('[TH] onNavChange — hash=' + JSON.stringify(hash) + ' lastHash=' + JSON.stringify(MH.lastHash) + ' ts=' + Date.now());
    if (hash === MH.lastHash) {
      console.log('[TH] onNavChange — DEDUP, skipping');
      return;
    }
    MH.lastHash = hash;

    if (isHomePage()) {
      onHomePage();
    } else {
      onLeavingHome();
    }
  }

  function isHomePage() {
    // Normalize the hash the same way tentacle-navbar.js does: strip the
    // leading '#/', then drop any query string and file extension. This keeps
    // the home detection consistent across navbar, home, and mediabar.
    var raw = location.hash || '';
    var h = raw.replace('#', '').replace(/^\//, '').split('?')[0].split('.')[0];
    if (h !== '' && h !== 'home') return false;
    // The home view has a Home tab (tab 0 / none) and a Favorites tab (tab 1).
    // Only the Home tab is the Tentacle home; on Favorites (any non-zero tab)
    // step aside so the native Favorites content shows, not Tentacle's rows.
    var m = raw.match(/[?&]tab=(\d+)/);
    return !m || m[1] === '0';
  }

  // ── Home Page Entry ─────────────────────────────────────────────────
  // Moonfin pattern: body class hides ALL native containers (any view,
  // stale or fresh). Tentacle renders into a stable mount point that
  // Jellyfin's view transitions can never touch.
  function onHomePage() {
    if (window.TentacleStandAside && window.TentacleStandAside()) return; // TV layout — native home renders
    // Detect user switch — if userId changed, tear down and re-render
    var currentUserId = MH.apiClient && MH.apiClient.getCurrentUserId();
    if (currentUserId && currentUserId !== MH.userId) {
      console.log('[TH] User switched: ' + MH.userId + ' → ' + currentUserId);
      MH.userId = currentUserId;
      MH.lastVersion = -1;
      cleanupHome();
      // The navbar keeps the toolbar it fetched for the previous user; the
      // rows below are re-fetched for the new one, the toolbar must be too.
      refreshChrome();
    }
    if (document.getElementById('tentacle-home')) return;

    var gen = ++MH.generation;
    console.log('[TH] onHomePage() — gen=' + gen + ' ts=' + Date.now());

    // Hide ALL native home containers via body class (CSS handles the rest)
    document.body.classList.add('tentacle-home-active');

    // Find a stable mount point — mainAnimatedPages is the main content area
    // that persists across view transitions. Our element lives here, outside
    // any individual Jellyfin view element.
    var mount = document.querySelector('.mainAnimatedPages, .skinBody, #skin-container') || document.body;

    var mhHome = document.createElement('div');
    mhHome.id = 'tentacle-home';
    mhHome.innerHTML = '<div class="mh-loading"><div class="mh-spinner"></div></div>';
    mount.prepend(mhHome);
    console.log('[TH] onHomePage() — #tentacle-home prepended to ' + (mount.className || mount.tagName));

    renderHomePage(mhHome, gen);
  }

  // ── Leaving Home ────────────────────────────────────────────────────
  function onLeavingHome() {
    MH.generation++;
    console.log('[TH] onLeavingHome() — gen=' + MH.generation + ' ts=' + Date.now());
    cleanupHome();
  }

  function cleanupHome() {
    if (MH.heroInterval) {
      clearInterval(MH.heroInterval);
      MH.heroInterval = null;
    }
    stopVersionPolling();
    var old = document.getElementById('tentacle-home');
    if (old) old.remove();

    // Remove body class — unhides ALL native containers in all views
    document.body.classList.remove('tentacle-home-active');
  }

  // ── API Helpers ───────────────────────────────────────────────────────
  function apiGet(path) {
    var url = MH.apiClient.getUrl(path);
    return MH.apiClient.getJSON(url);
  }

  function getImageUrl(itemId, imageType, tag, maxWidth) {
    return MH.apiClient.getUrl('Items/' + itemId + '/Images/' + imageType, {
      tag: tag,
      maxWidth: maxWidth || 300,
      quality: 90,
    });
  }

  // ── Render Home Page ──────────────────────────────────────────────────
  // mhHome is already created and in the DOM (with loading spinner).
  // Body class already hides all native containers. We just populate it.
  function renderHomePage(mhHome, gen) {
    console.log('[TH] renderHomePage() — gen=' + gen + ' ts=' + Date.now());

    apiGet('TentacleHome/Sections?userId=' + MH.userId)
      .then(function (data) {
        // Stale check — did user navigate away during the API call?
        // Remove only this visit's element (already detached once the user left).
        // cleanupHome() would remove the home of a LATER visit to Home that is
        // on screen now, and nothing would bring it back.
        if (gen !== MH.generation) {
          console.log('[TH] renderHomePage API callback — STALE gen=' + gen + '/' + MH.generation + ', removing');
          mhHome.remove();
          return;
        }

        console.log('[Tentacle] Sections API response:', JSON.stringify(data, null, 2));

        if (!data.enabled || !data.sections || !data.sections.length) {
          console.warn('[Tentacle] No sections returned or disabled');
          cleanupHome(); // falls back to native Jellyfin home
          return;
        }

        mhHome.innerHTML = '';

        var heroSection = data.sections.find(function (s) { return s.type === 'hero'; });
        var contentSections = data.sections.filter(function (s) { return s.type === 'row' || s.type === 'builtin'; });
        console.log('[Tentacle] Hero:', heroSection ? heroSection.displayText : 'none', '| Content sections:', contentSections.length, contentSections.map(function(s) { return s.type + ':' + (s.sectionId || s.playlistId); }));

        var rowsContainer = document.createElement('div');
        rowsContainer.id = 'mh-rows-container';
        mhHome.appendChild(rowsContainer);

        // Hero is handled by tentacle-mediabar.js (Moonfin-style full-screen media bar)
        // If mediabar is not available, fall back to the built-in hero
        if (heroSection && !window.TentacleMediaBar) {
          var heroContainer = document.createElement('div');
          heroContainer.id = 'mh-hero-container';
          mhHome.insertBefore(heroContainer, rowsContainer);
          loadHero(heroContainer);
        }

        contentSections.forEach(function (section) {
          if (section.type === 'builtin') {
            loadBuiltinSection(rowsContainer, section);
          } else {
            loadRow(rowsContainer, section);
          }
        });

        // Store sections for live-update polling and start it
        MH.activeSections = contentSections;
        startVersionPolling(gen);
      })
      .catch(function (err) {
        console.error('[Tentacle] Failed to load sections:', err);
        if (gen !== MH.generation) { mhHome.remove(); return; } // a later visit's home stays
        cleanupHome(); // falls back to native Jellyfin home
      });
  }

  // ── Hero Section ──────────────────────────────────────────────────────
  function loadHero(container) {
    apiGet('TentacleHome/Hero?userId=' + MH.userId)
      .then(function (data) {
        if (!data.Items || !data.Items.length) return;
        renderHero(container, data.Items);
      })
      .catch(function (err) {
        console.error('[Tentacle] Failed to load hero:', err);
      });
  }

  function renderHero(container, items) {
    container.innerHTML = '';
    MH.heroTotal = items.length;

    var hero = document.createElement('div');
    hero.className = 'mh-hero';

    // Build slides
    items.forEach(function (item, i) {
      hero.appendChild(createSlide(item, i));
    });

    // Left/Right arrows
    if (items.length > 1) {
      var leftArrow = document.createElement('div');
      leftArrow.className = 'mh-hero-arrow mh-hero-arrow-left';
      leftArrow.innerHTML = '&#x2039;';
      leftArrow.onclick = function () {
        var idx = (MH.heroIndex - 1 + MH.heroTotal) % MH.heroTotal;
        goToSlide(hero, idx);
        resetHeroTimer(hero);
      };
      hero.appendChild(leftArrow);

      var rightArrow = document.createElement('div');
      rightArrow.className = 'mh-hero-arrow mh-hero-arrow-right';
      rightArrow.innerHTML = '&#x203A;';
      rightArrow.onclick = function () {
        var idx = (MH.heroIndex + 1) % MH.heroTotal;
        goToSlide(hero, idx);
        resetHeroTimer(hero);
      };
      hero.appendChild(rightArrow);
    }

    // Dots
    if (items.length > 1) {
      var dots = document.createElement('div');
      dots.className = 'mh-hero-dots';
      items.forEach(function (_, i) {
        var dot = document.createElement('span');
        dot.className = 'mh-hero-dot' + (i === 0 ? ' active' : '');
        dot.onclick = function () {
          goToSlide(hero, i);
          resetHeroTimer(hero);
        };
        dots.appendChild(dot);
      });
      hero.appendChild(dots);
    }

    container.appendChild(hero);

    // Auto-cycle every 8 seconds (matching Media Bar)
    if (items.length > 1) {
      MH.heroIndex = 0;
      startHeroTimer(hero);
    }
  }

  function createSlide(item, index) {
    var slide = document.createElement('div');
    slide.className = 'mh-hero-slide' + (index === 0 ? ' active' : '');
    slide.dataset.index = index;

    // Backdrop
    var backdropTag = '';
    if (item.BackdropImageTags && item.BackdropImageTags.length) {
      backdropTag = item.BackdropImageTags[0];
    } else if (item.ImageTags && item.ImageTags.Backdrop) {
      backdropTag = item.ImageTags.Backdrop;
    }
    var backdropUrl = backdropTag ? getImageUrl(item.Id, 'Backdrop', backdropTag, 1920) : '';

    // Logo
    var logoTag = item.ImageTags && item.ImageTags.Logo ? item.ImageTags.Logo : '';
    var logoUrl = logoTag ? getImageUrl(item.Id, 'Logo', logoTag, 500) : '';

    // Build metadata
    var year = item.ProductionYear || '';
    var rating = item.CommunityRating ? item.CommunityRating.toFixed(1) : '';
    var officialRating = item.OfficialRating || '';
    var runtime = '';
    if (item.RunTimeTicks) {
      var mins = Math.round(item.RunTimeTicks / 600000000);
      if (mins >= 60) {
        runtime = Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
      } else {
        runtime = mins + 'm';
      }
    }

    var genres = (item.Genres || []).slice(0, 3);
    var overview = truncate(item.Overview || '', 360);

    // Build HTML
    var html = '';

    // Backdrop + overlay + gradient
    html += '<div class="mh-hero-backdrop" style="background-image:url(\'' + backdropUrl + '\')"></div>';
    html += '<div class="mh-hero-overlay"></div>';
    html += '<div class="mh-hero-gradient"></div>';

    // Logo or title
    if (logoUrl) {
      html += '<div class="mh-hero-logo-container">';
      html += '<img class="mh-hero-logo" src="' + logoUrl + '" alt="' + escapeAttr(item.Name) + '">';
      html += '</div>';
    } else {
      html += '<div class="mh-hero-logo-container">';
      html += '<h1 class="mh-hero-title">' + escapeHtml(item.Name) + '</h1>';
      html += '</div>';
    }

    // Info line (year, rating, age rating, runtime)
    var infoParts = [];
    if (year) infoParts.push(escapeHtml(String(year)));
    if (rating) infoParts.push('<span class="mh-hero-rating"><span class="mh-star">★</span> ' + escapeHtml(rating) + '</span>');
    if (officialRating) infoParts.push('<span class="mh-hero-age-rating">' + escapeHtml(officialRating) + '</span>');
    if (runtime) infoParts.push(escapeHtml(runtime));

    html += '<div class="mh-hero-info">';
    html += infoParts.join(' <span class="mh-separator">●</span> ');
    html += '</div>';

    // Genre line
    if (genres.length > 0) {
      html += '<div class="mh-hero-genre">';
      for (var g = 0; g < genres.length; g++) {
        if (g > 0) html += '<span class="mh-genre-sep">◆</span>';
        html += '<span>' + escapeHtml(genres[g]) + '</span>';
      }
      html += '</div>';
    }

    // Overview/plot
    if (overview) {
      html += '<p class="mh-hero-overview">' + escapeHtml(overview) + '</p>';
    }

    // Buttons
    html += '<div class="mh-hero-buttons">';
    html += '<button class="mh-hero-btn mh-hero-btn-play" onclick="if(window.TentacleDetails){window.TentacleDetails.show(\'' + item.Id + '\')}else{window.location.hash=\'#/details?id=' + item.Id + '\'}">';
    html += '<span class="mh-btn-icon">▶</span> More Info';
    html += '</button>';
    html += '<button class="mh-hero-btn-info" onclick="if(window.TentacleDetails){window.TentacleDetails.show(\'' + item.Id + '\')}else{window.location.hash=\'#/details?id=' + item.Id + '\'}" title="More Info">';
    html += 'ℹ';
    html += '</button>';
    html += '</div>';

    slide.innerHTML = html;
    return slide;
  }

  function startHeroTimer(hero) {
    if (MH.heroInterval) clearInterval(MH.heroInterval);
    MH.heroInterval = setInterval(function () {
      MH.heroIndex = (MH.heroIndex + 1) % MH.heroTotal;
      goToSlide(hero, MH.heroIndex);
    }, 8000);
  }

  function resetHeroTimer(hero) {
    startHeroTimer(hero);
  }

  function goToSlide(hero, index) {
    MH.heroIndex = index;
    var slides = hero.querySelectorAll('.mh-hero-slide');
    var dots = hero.querySelectorAll('.mh-hero-dot');
    slides.forEach(function (s, i) {
      if (i === index) {
        s.classList.add('active');
      } else {
        s.classList.remove('active');
      }
    });
    dots.forEach(function (d, i) {
      d.classList.toggle('active', i === index);
    });
  }

  // ── Built-in Jellyfin Sections ──────────────────────────────────────
  // Tentacle's own rows that aren't playlists (the plugin sends no other):
  // Continue Watching and Recordings come from the plugin, one list the TV app
  // draws too, in the shape chosen in Tentacle; On Now draws its own cards.
  var TENTACLE_ROW_PATHS = {
    resumevideo: 'TentacleHome/ContinueWatching',
    recordings: 'TentacleHome/Recordings',
  };

  function loadBuiltinSection(container, section) {
    var sectionId = section.sectionId;
    var row = document.createElement('div');
    row.className = 'mh-row';
    row.setAttribute('data-section-id', sectionId);
    if (section.shape) row.setAttribute('data-shape', section.shape);

    row.innerHTML =
      '<div class="mh-row-header">' +
        '<h2 class="mh-row-title">' + escapeHtml(section.displayText) + '</h2>' +
      '</div>' +
      '<div class="mh-row-scroll">' +
        '<div class="mh-row-arrow mh-row-arrow-left">&#x2039;</div>' +
        '<div class="mh-row-items mh-loading-row"><div class="mh-spinner-sm"></div></div>' +
        '<div class="mh-row-arrow mh-row-arrow-right">&#x203A;</div>' +
      '</div>';

    container.appendChild(row);

    var scrollEl = row.querySelector('.mh-row-items');
    var leftArrow = row.querySelector('.mh-row-arrow-left');
    var rightArrow = row.querySelector('.mh-row-arrow-right');
    if (leftArrow) {
      leftArrow.onclick = function () {
        scrollEl.scrollBy({ left: -scrollEl.clientWidth * 0.8, behavior: 'smooth' });
      };
    }
    if (rightArrow) {
      rightArrow.onclick = function () {
        scrollEl.scrollBy({ left: scrollEl.clientWidth * 0.8, behavior: 'smooth' });
      };
    }

    if (sectionId === 'onnow') {
      loadOnNow(row);
      return;
    }
    if (!TENTACLE_ROW_PATHS[sectionId]) {
      row.classList.add('mh-row-hidden');
      return;
    }
    fillTentacleRow(row, section, true);
  }

  // Fill (or refresh in place) Continue Watching or Recordings. `first`: the
  // row was just drawn with its spinner; else only a change redraws it.
  function fillTentacleRow(row, section, first) {
    var gen = MH.generation;
    apiGet(TENTACLE_ROW_PATHS[section.sectionId] + '?userId=' + MH.userId)
      .then(function (data) {
        if (gen !== MH.generation) return;
        var itemsEl = row.querySelector('.mh-row-items');
        if (!itemsEl) return;
        itemsEl.classList.remove('mh-loading-row');
        var items = (data && data.Items) || [];
        var wide = section.shape === 'wide';
        // What the cards show: the items, their saved place and the shape
        var key = (wide ? 'w|' : 'p|') + items.map(function (i) {
          return i.Id + ':' + ((i.UserData && i.UserData.PlaybackPositionTicks) || 0);
        }).join(',');
        if (!first && key === row._tentacleKey) return;
        row._tentacleKey = key;
        if (!items.length) {
          row.classList.add('mh-row-hidden');
          itemsEl.innerHTML = '';
          return;
        }
        row.classList.remove('mh-row-hidden');
        itemsEl.innerHTML = '';
        items.forEach(function (item) {
          itemsEl.appendChild(section.sectionId === 'resumevideo'
            ? createContinueCard(item, wide) : createCard(item, wide));
        });
      })
      .catch(function (err) {
        console.warn('[Tentacle] ' + section.displayText + ' unavailable:', err);
        if (first) row.classList.add('mh-row-hidden');
      });
  }

  // ── On Now: what's on right now on your favourite channels ──────────
  // Jellyfin's own favourites (the Plan B lineup), filtered by Jellyfin to the
  // channels this person may see (kids: only kids channels). Hidden when there
  // are no favourites or nothing is on. Read again every 5 s while Home is
  // showing, so a channel starred anywhere (Tentacle's My Channels, the guide,
  // another device) and a programme ending show within seconds; the cards are
  // only redrawn when something on them changed.
  var ON_NOW_REFRESH_MS = 5000;

  function loadOnNow(row) {
    var gen = MH.generation;
    clearTimeout(row._onNowTimer);
    var again = function () {
      row._onNowTimer = setTimeout(function () {
        if (gen !== MH.generation || !document.body.contains(row)) return;
        if (document.hidden) { again(); return; }
        loadOnNow(row);
      }, ON_NOW_REFRESH_MS);
    };
    apiGet('LiveTv/Channels?UserId=' + MH.userId + '&IsFavorite=true&AddCurrentProgram=true&EnableUserData=true' +
           '&Fields=PrimaryImageAspectRatio&ImageTypeLimit=1&EnableImageTypes=Primary&Limit=100')
      .then(function (data) {
        if (gen !== MH.generation) return;
        var itemsEl = row.querySelector('.mh-row-items');
        if (!itemsEl) return;
        itemsEl.classList.remove('mh-loading-row');
        var now = Date.now();
        var onNow = ((data && data.Items) || []).filter(function (ch) {
          var p = ch.CurrentProgram;
          return p && p.EndDate && Date.parse(p.EndDate) > now && (!p.StartDate || Date.parse(p.StartDate) <= now);
        }).slice(0, 30);
        // What the cards show, to the minute
        var key = onNow.map(function (ch) {
          return ch.Id + ':' + ch.CurrentProgram.Id + ':' + Math.round((Date.parse(ch.CurrentProgram.EndDate) - now) / 60000);
        }).join(',');
        if (key !== row._onNowKey) {
          row._onNowKey = key;
          if (!onNow.length) {
            row.classList.add('mh-row-hidden');
          } else {
            row.classList.remove('mh-row-hidden');
            itemsEl.innerHTML = '';
            onNow.forEach(function (ch) { itemsEl.appendChild(createOnNowCard(ch, now)); });
          }
        }
        again();
      })
      .catch(function (err) {
        console.warn('[Tentacle] On Now unavailable:', err);
        if (!row._onNowKey) row.classList.add('mh-row-hidden');
        again();
      });
  }

  // On Now's own card (no poster/wide choice): a square tile with the channel's
  // logo whole on a dark panel and how far the programme is, the programme and
  // "channel · time left" under it. The TV app draws the same card.
  function createOnNowCard(channel, now) {
    var p = channel.CurrentProgram;
    var card = document.createElement('div');
    card.className = 'mh-card mh-card-onnow';
    card.setAttribute('data-item-id', channel.Id);
    card.onclick = function () { playLive(channel.Id); };

    // No tag in the URL (Live TV images 500 with one), and no token in an <img src>
    // (ApiClient.getUrl; Jellyfin's image endpoints don't need it).
    var logo = MH.apiClient.getUrl('Items/' + channel.Id + '/Images/Primary', { maxWidth: 300, quality: 90 });
    var start = p.StartDate ? Date.parse(p.StartDate) : now;
    var end = Date.parse(p.EndDate);
    var pct = end > start ? Math.max(2, Math.min(100, Math.round((now - start) * 100 / (end - start)))) : 0;
    var left = Math.max(1, Math.round((end - now) / 60000));
    var leftText = left >= 60 ? Math.floor(left / 60) + 'h ' + (left % 60) + 'm left' : left + 'm left';

    card.innerHTML =
      '<div class="mh-card-poster mh-onnow-logo">' +
        '<div class="mh-onnow-name">' + escapeHtml(channel.Name || '') + '</div>' +
        // No logo: the channel's name instead (the picture's parent keeps the overlay, #173)
        '<img src="' + logo + '" alt="" loading="lazy" onerror="this.parentNode.classList.add(\'mh-onnow-nologo\');this.remove()">' +
        '<div class="mh-card-progress"><div class="mh-card-progress-bar" style="width:' + pct + '%"></div></div>' +
        '<div class="mh-card-play-overlay"><div class="mh-card-play-icon">▶</div></div>' +
      '</div>' +
      '<div class="mh-card-info">' +
        '<div class="mh-card-title" title="' + escapeAttr(p.Name || '') + '">' + escapeHtml(p.Name || channel.Name) + '</div>' +
        '<div class="mh-card-subtitle" title="' + escapeAttr(channel.Name) + '">' +
          escapeHtml(channel.Name) + ' · ' + leftText + '</div>' +
      '</div>';
    return card;
  }

  // Tune in the way the Live TV and Favorites pages do (tentacle-livetv.js:
  // Jellyfin Web 10.11 has no global playbackManager, so it plays in this tab
  // through Jellyfin Web's own message handler)
  function playLive(channelId) {
    if (window.TentacleLiveTV && typeof window.TentacleLiveTV.playChannel === 'function') {
      window.TentacleLiveTV.playChannel(channelId);
      return;
    }
    window.location.hash = '#/details?id=' + channelId;
  }

  // A Continue Watching card: something stopped part-way (its progress and the
  // time left) or the next episode of a show being watched. Wide shows the
  // episode's still or the title's backdrop; poster the show's (or film's) poster.
  function createContinueCard(item, wide) {
    var card = document.createElement('div');
    card.className = wide ? 'mh-card mh-card-wide' : 'mh-card';
    card.setAttribute('data-item-id', item.Id);
    card.onclick = function () {
      if (window.TentacleDetails) { window.TentacleDetails.show(item.Id); return; }
      window.location.hash = '#/details?id=' + item.Id;
    };

    var imgUrl = '';
    if (wide) {
      if (item.ImageTags && item.ImageTags.Thumb) imgUrl = getImageUrl(item.Id, 'Thumb', item.ImageTags.Thumb, 500);
      else if (item.Type === 'Episode' && item.ImageTags && item.ImageTags.Primary) imgUrl = getImageUrl(item.Id, 'Primary', item.ImageTags.Primary, 500);
      else if (item.BackdropImageTags && item.BackdropImageTags.length) imgUrl = getImageUrl(item.Id, 'Backdrop', item.BackdropImageTags[0], 500);
      else if (item.ParentBackdropItemId && item.ParentBackdropImageTags && item.ParentBackdropImageTags.length) imgUrl = getImageUrl(item.ParentBackdropItemId, 'Backdrop', item.ParentBackdropImageTags[0], 500);
    } else if (item.SeriesId && item.SeriesPrimaryImageTag) {
      imgUrl = getImageUrl(item.SeriesId, 'Primary', item.SeriesPrimaryImageTag, 300);
    }
    if (!imgUrl && item.ImageTags && item.ImageTags.Primary) imgUrl = getImageUrl(item.Id, 'Primary', item.ImageTags.Primary, wide ? 500 : 300);

    var ud = item.UserData || {};
    var progressHtml = '';
    if (ud.PlaybackPositionTicks > 0 && ud.PlayedPercentage) {
      progressHtml =
        '<div class="mh-card-progress">' +
          '<div class="mh-card-progress-bar" style="width:' + Math.round(ud.PlayedPercentage) + '%"></div>' +
        '</div>';
    }

    var titleLine = item.SeriesName || item.Name || '';
    var subtitleLine = '';
    if (item.SeriesName) {
      var epLabel = (item.ParentIndexNumber != null && item.IndexNumber != null)
        ? 'S' + item.ParentIndexNumber + ':E' + item.IndexNumber + ' · ' : '';
      subtitleLine = epLabel + (item.Name || '');
    }

    var remainingHtml = '';
    if (item.RunTimeTicks && ud.PlaybackPositionTicks) {
      var remainMins = Math.round((item.RunTimeTicks - ud.PlaybackPositionTicks) / 600000000);
      if (remainMins > 0) remainingHtml = '<span class="mh-card-remaining">' + remainMins + 'm left</span>';
    } else if (item.SeriesName) {
      remainingHtml = '<span class="mh-card-remaining">Next episode</span>';
    }

    card.innerHTML =
      '<div class="' + (wide ? 'mh-card-poster mh-card-poster-wide' : 'mh-card-poster') + '">' +
        (imgUrl
          ? '<img src="' + imgUrl + '" alt="" loading="lazy">'
          : '<div class="mh-card-no-poster">🎬</div>') +
        progressHtml +
        '<div class="mh-card-play-overlay">' +
          '<div class="mh-card-play-icon">▶</div>' +
        '</div>' +
      '</div>' +
      '<div class="mh-card-info">' +
        '<div class="mh-card-title" title="' + escapeAttr(titleLine) + '">' + escapeHtml(titleLine) + '</div>' +
        (subtitleLine
          ? '<div class="mh-card-subtitle" title="' + escapeAttr(subtitleLine) + '">' + escapeHtml(subtitleLine) + '</div>'
          : '') +
        remainingHtml +
      '</div>';

    return card;
  }

  // ── Row Sections ──────────────────────────────────────────────────────
  function loadRow(container, section) {
    var row = document.createElement('div');
    row.className = 'mh-row';
    row.setAttribute('data-playlist-id', section.playlistId);
    // The shape the row was drawn with, on the row itself, so a live update
    // can tell whether it changed by looking at the DOM rather than at
    // whatever was remembered about the sections.
    row.setAttribute('data-shape', section.shape === 'wide' ? 'wide' : 'poster');

    row.innerHTML =
      '<div class="mh-row-header">' +
        '<h2 class="mh-row-title">' + escapeHtml(section.displayText) + '</h2>' +
      '</div>' +
      '<div class="mh-row-scroll">' +
        '<div class="mh-row-arrow mh-row-arrow-left">&#x2039;</div>' +
        '<div class="mh-row-items mh-loading-row"><div class="mh-spinner-sm"></div></div>' +
        '<div class="mh-row-arrow mh-row-arrow-right">&#x203A;</div>' +
      '</div>';

    container.appendChild(row);

    // Wire up scroll arrows
    var scrollEl = row.querySelector('.mh-row-items');
    var leftArrow = row.querySelector('.mh-row-arrow-left');
    var rightArrow = row.querySelector('.mh-row-arrow-right');
    if (leftArrow) {
      leftArrow.onclick = function () {
        scrollEl.scrollBy({ left: -scrollEl.clientWidth * 0.8, behavior: 'smooth' });
      };
    }
    if (rightArrow) {
      rightArrow.onclick = function () {
        scrollEl.scrollBy({ left: scrollEl.clientWidth * 0.8, behavior: 'smooth' });
      };
    }

    apiGet('TentacleHome/Section/' + section.playlistId + '?userId=' + MH.userId)
      .then(function (data) {
        var itemsEl = row.querySelector('.mh-row-items');
        itemsEl.classList.remove('mh-loading-row');

        if (!data.Items || !data.Items.length) {
          // Hide empty rows entirely
          row.classList.add('mh-row-hidden');
          return;
        }

        itemsEl.innerHTML = '';
        var wide = section.shape === 'wide';
        data.Items.forEach(function (item) {
          itemsEl.appendChild(createCard(item, wide));
        });
      })
      .catch(function () {
        // Hide failed rows
        row.classList.add('mh-row-hidden');
      });
  }

  // A Live TV recording is a Movie to Jellyfin, with the year it was recorded: its card
  // says "Recording · Fri 23 Oct" instead (the air date from Jellyfin's file name,
  // "Title 2026_10_23_20_00_00.ts", else when the file was made), as the TV app does.
  function recordingMeta(item) {
    var path = item.Path || '';
    var file = path.split(/[\\/]/).pop();
    var stamp = /(\d{4})_(\d{2})_(\d{2})_\d{2}_\d{2}_\d{2}\.ts$/i.exec(file);
    if (!stamp && !/[\\/]livetv[\\/]recordings[\\/]/i.test(path)) return null;
    var when = stamp ? new Date(+stamp[1], +stamp[2] - 1, +stamp[3])
      : (item.DateCreated ? new Date(item.DateCreated) : null);
    var day = when && !isNaN(when.getTime())
      ? when.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' }) : '';
    return day ? 'Recording · ' + day : 'Recording';
  }

  // `wide` draws 16:9 cards instead of 2:3. Some content has no portrait
  // artwork at all — a YouTube thumbnail in a poster slot is cropped to a strip
  // of its middle — so the row says which shape it wants.
  function createCard(item, wide) {
    var card = document.createElement('div');
    card.className = wide ? 'mh-card mh-card-wide' : 'mh-card';
    card.setAttribute('data-item-id', item.Id);
    card.onclick = function () {
      if (window.TentacleDetails) { window.TentacleDetails.show(item.Id); return; }
      window.location.hash = '#/details?id=' + item.Id;
    };

    var posterTag = '';
    var posterType = 'Primary';
    if (wide) {
      // Thumb is the only one of the three that is meant to be 16:9; Backdrop
      // is a reasonable stand-in. Primary is the last resort and will be
      // cropped, which is still better than an empty card.
      if (item.ImageTags && item.ImageTags.Thumb) {
        posterTag = item.ImageTags.Thumb;
        posterType = 'Thumb';
      } else if (item.BackdropImageTags && item.BackdropImageTags.length) {
        posterTag = item.BackdropImageTags[0];
        posterType = 'Backdrop';
      }
    }
    if (!posterTag && item.ImageTags && item.ImageTags.Primary) {
      posterTag = item.ImageTags.Primary;
      posterType = 'Primary';
    }
    var posterUrl = posterTag ? getImageUrl(item.Id, posterType, posterTag, wide ? 500 : 300) : '';

    // Indicators (watched, favorite)
    var indicatorsHtml = '';
    if (item.UserData) {
      if (item.UserData.IsFavorite) {
        indicatorsHtml += '<div class="mh-card-badge mh-card-badge-fav" title="Favorite"><svg viewBox="0 -960 960 960" fill="currentColor"><path d="m480-120-58-52q-101-91-167-157T150-447.5Q111-500 95.5-544T80-634q0-94 63-157t157-63q52 0 99 22t81 62q34-40 81-62t99-22q94 0 157 63t63 157q0 46-15.5 90T810-447.5Q771-395 705-329T538-172l-58 52Z"/></svg></div>';
      }
      if (item.UserData.Played) {
        indicatorsHtml += '<div class="mh-card-badge mh-card-badge-watched" title="Watched"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M21 7L9 19l-5.5-5.5 1.41-1.41L9 16.17 19.59 5.59 21 7z"/></svg></div>';
      }
    }

    // Build display name — show series info for episodes (Next Up)
    var displayName = item.Name || '';
    var subtitleLine = '';
    if (item.SeriesName) {
      displayName = item.SeriesName;
      var epLabel = '';
      if (item.ParentIndexNumber != null && item.IndexNumber != null) {
        epLabel = 'S' + item.ParentIndexNumber + ':E' + item.IndexNumber + ' · ';
      }
      subtitleLine = epLabel + (item.Name || '');
    }

    // Metadata line
    var year = item.ProductionYear || '';
    var rating = item.CommunityRating ? '<span class="mh-card-meta-rating">★ ' + item.CommunityRating.toFixed(1) + '</span>' : '';
    var officialRating = item.OfficialRating ? '<span class="mh-card-meta-cert">' + escapeHtml(item.OfficialRating) + '</span>' : '';
    var recording = recordingMeta(item);
    if (recording) year = '';
    var metaParts = [recording ? escapeHtml(recording) : '', year ? escapeHtml(String(year)) : '', rating, officialRating].filter(Boolean);
    var metaHtml = metaParts.join(' <span class="mh-card-meta-dot">·</span> ');

    card.innerHTML =
      '<div class="' + (wide ? 'mh-card-poster mh-card-poster-wide' : 'mh-card-poster') + '">' +
        (posterUrl
          ? '<img src="' + posterUrl + '" alt="" loading="lazy">'
          : '<div class="mh-card-no-poster">🎬</div>') +
        indicatorsHtml +
        '<div class="mh-card-play-overlay">' +
          '<div class="mh-card-play-icon">▶</div>' +
        '</div>' +
      '</div>' +
      '<div class="mh-card-info">' +
        '<div class="mh-card-title" title="' + escapeAttr(displayName) + '">' + escapeHtml(displayName) + '</div>' +
        (subtitleLine
          ? '<div class="mh-card-subtitle" title="' + escapeAttr(subtitleLine) + '">' + escapeHtml(subtitleLine) + '</div>'
          : '') +
        '<div class="mh-card-meta">' + metaHtml + '</div>' +
      '</div>';

    // Lazy-load MDBList ratings if available
    if (typeof MdbList !== 'undefined' && MdbList.isEnabled && MdbList.isEnabled()) {
      var ratingEl = document.createElement('div');
      ratingEl.className = 'mh-card-mdblist';
      card.querySelector('.mh-card-info').appendChild(ratingEl);
      // Capture the generation so a fetch that resolves after the user navigates
      // away or the rows are rebuilt doesn't write into a recycled/detached card.
      var ratingsGen = MH.generation;
      MdbList.fetchRatings(item).then(function (ratings) {
        if (ratingsGen !== MH.generation) return; // navigated/rebuilt — stale
        if (!ratingEl.isConnected) return;          // card removed from DOM
        if (ratings && MdbList.buildRatingsHtml) {
          ratingEl.innerHTML = MdbList.buildRatingsHtml(ratings, 'compact') || '';
        }
      }).catch(function () {});
    }

    return card;
  }

  // ── Live-Update Polling ─────────────────────────────────────────────
  // Polls backend version counter every 5s. When version changes,
  // re-fetches playlist row items and updates them in-place.
  // The C# plugin also broadcasts LibraryChanged via WebSocket on every
  // refresh, so Android TV gets instant updates. Web polling is a fallback.
  function startVersionPolling(gen) {
    stopVersionPolling();
    MH.versionPollInFlight = false;
    // Seed initial version. The rows were just fetched fresh, but the toolbar
    // and the hero were not: if the version moved while the user was on
    // another page, a change made then (toolbar, hero) was never applied,
    // because polling only runs on Home and re-seeding swallowed the change.
    apiGet('TentacleHome/Version')
      .then(function (data) {
        var v = data.version || 0;
        var movedWhileAway = MH.lastVersion !== -1 && v !== MH.lastVersion;
        MH.lastVersion = v;
        if (movedWhileAway && gen === MH.generation) refreshChrome();
      })
      .catch(function () {});

    MH.versionPollTimer = setInterval(function () {
      // Stop if generation changed (navigated away)
      if (gen !== MH.generation) { stopVersionPolling(); return; }
      if (!document.getElementById('tentacle-home')) { stopVersionPolling(); return; }
      // One poll at a time: behind a slow backend every tick used to add
      // another request on top of the one still outstanding.
      if (MH.versionPollInFlight) return;
      MH.versionPollInFlight = true;

      apiGet('TentacleHome/Version')
        .then(function (data) {
          MH.versionPollInFlight = false;
          // Left Home (or re-rendered) while the poll was in flight: nothing to refresh.
          if (gen !== MH.generation) return;
          var newVersion = data.version || 0;
          if (newVersion === MH.lastVersion) return;
          console.log('[TH] Playlist version changed: ' + MH.lastVersion + ' → ' + newVersion);
          MH.lastVersion = newVersion;
          refreshPlaylistRows(gen);
          // Also refresh the toolbar and the hero in case their config changed
          refreshChrome();
        })
        .catch(function () { MH.versionPollInFlight = false; });
    }, 5000);
  }

  // The parts of the page the rows refresh does not rebuild: the navbar's
  // toolbar buttons and the media bar hero. Both re-read their own config.
  function refreshChrome() {
    if (window.TentacleNavbar && window.TentacleNavbar.refreshToolbar) {
      window.TentacleNavbar.refreshToolbar();
    }
    if (window.TentacleMediaBar && window.TentacleMediaBar.refreshIfChanged) {
      window.TentacleMediaBar.refreshIfChanged();
    }
  }

  function stopVersionPolling() {
    if (MH.versionPollTimer) {
      clearInterval(MH.versionPollTimer);
      MH.versionPollTimer = null;
    }
  }

  function refreshPlaylistRows(gen) {
    if (!MH.activeSections) return;

    // Re-fetch sections to detect structural changes (reorder, add, remove)
    apiGet('TentacleHome/Sections?userId=' + MH.userId)
      .then(function (data) {
        if (gen !== MH.generation) return;
        // Only a successful "enabled" answer may change the rows. `enabled: false`
        // with no sections emptied the whole container (#257); the next visit to
        // Home falls back to the native home if the home really was turned off.
        if (!data || data.enabled !== true || !Array.isArray(data.sections)) return;

        var newSections = data.sections.filter(function (s) { return s.type === 'row' || s.type === 'builtin'; });
        // The key includes each row's shape: a poster/wide change is a change
        // to the row's structure, not to its items, and comparing IDs alone
        // left it invisible until a full page reload — unlike a reorder, which
        // the same backend push made appear at once.
        // The title is structure too (it changes no items), and so are
        // Continue Watching's "Include next episodes" and item limit.
        var keyOf = function (s) {
          return ((s.type === 'row' ? s.playlistId : s.sectionId) || '') + ':' + (s.shape || '') +
            ':' + (s.displayText || '') +
            (s.type === 'builtin' ? ':' + !!s.includeNextUp + ':' + (s.maxItems || '') : '');
        };
        var oldKeys = MH.activeSections.map(keyOf).join(',');
        var newKeys = newSections.map(keyOf).join(',');

        if (oldKeys !== newKeys) {
          // Structure changed — rebuild rows container
          console.log('[TH] Home structure changed, rebuilding rows');
          var rowsContainer = document.getElementById('mh-rows-container');
          if (!rowsContainer) return;
          rowsContainer.innerHTML = '';
          newSections.forEach(function (section) {
            if (section.type === 'builtin') {
              loadBuiltinSection(rowsContainer, section);
            } else {
              loadRow(rowsContainer, section);
            }
          });
          MH.activeSections = newSections;
          return;
        }

        // Structure unchanged — refresh items in-place for playlist rows.
        // Use the fresh sections, not the remembered ones: a row's shape can
        // change with nothing else about it changing.
        MH.activeSections = newSections;
        newSections.forEach(function (section) {
          if (section.type !== 'builtin' || !TENTACLE_ROW_PATHS[section.sectionId]) return;
          var row = document.querySelector('.mh-row[data-section-id="' + section.sectionId + '"]');
          if (row) fillTentacleRow(row, section, false);
        });
        var sections = newSections.filter(function (s) { return s.type === 'row' && s.playlistId; });
        sections.forEach(function (section) {
          apiGet('TentacleHome/Section/' + section.playlistId + '?userId=' + MH.userId)
            .then(function (itemData) {
              if (gen !== MH.generation) return;
              var row = document.querySelector('.mh-row[data-playlist-id="' + section.playlistId + '"]');
              if (!row) return;
              var itemsEl = row.querySelector('.mh-row-items');
              if (!itemsEl) return;

              if (!itemData.Items || !itemData.Items.length) {
                row.classList.add('mh-row-hidden');
                return;
              }

              var wide = section.shape === 'wide';
              var drawnWide = row.getAttribute('data-shape') === 'wide';
              var currentIds = [];
              itemsEl.querySelectorAll('.mh-card[data-item-id]').forEach(function (c) {
                currentIds.push(c.getAttribute('data-item-id'));
              });
              var newIds = itemData.Items.map(function (i) { return i.Id; });

              // Same items AND same shape: nothing to do. Same items in a
              // different shape is a change — the one that used to be missed.
              if (wide === drawnWide && currentIds.length === newIds.length && currentIds.every(function (id, idx) { return id === newIds[idx]; })) {
                return;
              }

              console.log('[TH] Updating row "' + section.displayText + '": ' + currentIds.length + ' → ' + newIds.length + ' items' + (wide !== drawnWide ? ', shape → ' + (wide ? 'wide' : 'poster') : ''));
              row.classList.remove('mh-row-hidden');
              row.setAttribute('data-shape', wide ? 'wide' : 'poster');
              itemsEl.innerHTML = '';
              itemData.Items.forEach(function (item) {
                itemsEl.appendChild(createCard(item, wide));
              });
            })
            .catch(function () {});
        });
      })
      .catch(function () {
        // The plugin could not read the home config (503) or the request failed:
        // keep the rows, and forget the version so the next poll asks again
        // instead of waiting for the next change (#257).
        if (gen === MH.generation) MH.lastVersion = -1;
      });
  }

  // ── Utilities ─────────────────────────────────────────────────────────
  function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function escapeAttr(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function truncate(str, max) {
    if (!str || str.length <= max) return str || '';
    return str.substring(0, max) + '...';
  }

  // ── A card's menu (right-click, a long press; the TV's long press) ────
  // Continue Watching (a card with a saved place): Remove from Continue
  // Watching (the saved place goes to 0, watched stays as it was), Play from
  // start, Mark watched, Details; the card leaves the row at once. Other
  // cards: Mark watched / unwatched, Details. Channel cards keep their click.
  var CARD_MENU = '#tentacle-home .mh-card[data-item-id]:not(.mh-card-onnow)';

  function cardMenu(card, x, y) {
    var M = window.TentacleMenu, a = window.ApiClient;
    if (!M || !a) return;
    var id = card.getAttribute('data-item-id'), uid = a.getCurrentUserId();
    a.getJSON(a.getUrl('Users/' + uid + '/Items/' + id)).then(function (item) {
      if (!item || item.Type === 'TvChannel' || item.Type === 'Program') return;
      var ud = item.UserData || {};
      var details = { label: 'Details', run: function () {
        if (window.TentacleDetails) window.TentacleDetails.show(id); else location.hash = '#/details?id=' + id;
      } };
      var played = function (on) {
        return a.ajax({ type: on ? 'POST' : 'DELETE', url: a.getUrl('UserPlayedItems/' + id, { userId: uid }) });
      };
      var leaveRow = function () { card.remove(); };
      var failed = function () { M.toast('That didn’t work. Try again in a moment.'); };
      var items;
      if (ud.PlaybackPositionTicks > 0) {
        items = [
          { label: 'Remove from Continue Watching', run: function () {
            Promise.resolve(a.ajax({ type: 'POST', url: a.getUrl('UserItems/' + id + '/UserData', { userId: uid }),
              data: JSON.stringify({ PlaybackPositionTicks: 0 }), contentType: 'application/json' })).then(leaveRow, failed);
          } },
          { label: 'Play from start', run: function () {
            if (window.playbackManager) window.playbackManager.play({ ids: [id], startPositionTicks: 0 });
          } },
          { label: 'Mark watched', run: function () { Promise.resolve(played(true)).then(leaveRow, failed); } },
          details,
        ];
      } else {
        items = [
          { label: ud.Played ? 'Mark unwatched' : 'Mark watched', run: function () {
            Promise.resolve(played(!ud.Played)).then(function () { M.toast(ud.Played ? 'Marked unwatched' : 'Marked watched'); }, failed);
          } },
          details,
        ];
      }
      M.open({ title: item.Name || '', items: items, x: x, y: y, anchor: card });
    }).catch(function () { });
  }

  var cardMenuBound = false;
  function bindCardMenu() {
    if (cardMenuBound || !window.TentacleMenu) return;
    cardMenuBound = true;
    window.TentacleMenu.onGesture(document, CARD_MENU, cardMenu);
  }

  // ── Start ─────────────────────────────────────────────────────────────
  // (the menu is Tentacle's, from tentacle-livetv.js, which loads after this)
  document.addEventListener('DOMContentLoaded', bindCardMenu);
  setTimeout(bindCardMenu, 0);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', waitForReady);
  } else {
    waitForReady();
  }
})();
