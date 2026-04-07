// Tentacle Home — replaces Jellyfin's default homepage with Tentacle sections
// Injected into index.html via Harmony patch on PhysicalFileProvider
// Visual style adapted from jellyfin-plugin-media-bar + jellyfin-plugin-home-sections
(function () {
  'use strict';

  var MH = {
    initialized: false,
    heroInterval: null,
    heroIndex: 0,
    heroTotal: 0,
    apiClient: null,
    userId: null,
    observer: null,
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

    observeHomePage();
    checkAndRender();

    window.addEventListener('hashchange', checkAndRender);
    window.addEventListener('popstate', function () {
      setTimeout(checkAndRender, 100);
    });
  }

  function isHomePage() {
    var hash = location.hash || '';
    return hash === '' || hash === '#/' || hash === '#/home.html' || hash === '#/home';
  }

  function observeHomePage() {
    MH.observer = new MutationObserver(function () {
      if (isHomePage()) {
        setTimeout(checkAndRender, 200);
      }
    });
    MH.observer.observe(document.body, { childList: true, subtree: true });
  }

  function checkAndRender() {
    if (!isHomePage()) {
      cleanup();
      return;
    }

    var container = document.querySelector('.homeSectionsContainer');
    if (!container) return;

    if (document.getElementById('tentacle-home')) return;

    renderHomePage(container);
  }

  function cleanup() {
    if (MH.heroInterval) {
      clearInterval(MH.heroInterval);
      MH.heroInterval = null;
    }
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
  function renderHomePage(container) {
    container.style.display = 'none';

    var mhHome = document.createElement('div');
    mhHome.id = 'tentacle-home';
    mhHome.innerHTML = '<div class="mh-loading"><div class="mh-spinner"></div></div>';
    container.parentNode.insertBefore(mhHome, container);

    apiGet('TentacleHome/Sections?userId=' + MH.userId)
      .then(function (data) {
        console.log('[Tentacle] Sections API response:', JSON.stringify(data, null, 2));

        if (!data.enabled || !data.sections || !data.sections.length) {
          console.warn('[Tentacle] No sections returned or disabled');
          mhHome.innerHTML = '';
          container.style.display = '';
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
      })
      .catch(function (err) {
        console.error('[Tentacle] Failed to load sections:', err);
        mhHome.innerHTML = '';
        container.style.display = '';
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
  function loadBuiltinSection(container, section) {
    var sectionId = section.sectionId;
    console.log('[Tentacle] Loading builtin section:', sectionId, section.displayText);

    var row = document.createElement('div');
    row.className = 'mh-row';

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

    // "My Media" sections use library tiles instead of standard cards
    if (sectionId === 'smalllibrarytiles' || sectionId === 'smalllibrarytiles_small') {
      loadLibraryTiles(row, sectionId === 'smalllibrarytiles_small');
      return;
    }

    var apiPath = getBuiltinApiPath(sectionId);
    if (!apiPath) {
      console.warn('[Tentacle] No API path for builtin section:', sectionId);
      row.classList.add('mh-row-hidden');
      return;
    }

    apiGet(apiPath)
      .then(function (data) {
        var itemsEl = row.querySelector('.mh-row-items');
        itemsEl.classList.remove('mh-loading-row');

        // /Items/Latest returns a bare array; others return {Items: [...]}
        var items = Array.isArray(data) ? data : (data.Items || data.items || []);
        console.log('[Tentacle] Builtin section "' + sectionId + '" returned ' + items.length + ' items');

        if (!items.length) {
          row.classList.add('mh-row-hidden');
          return;
        }

        itemsEl.innerHTML = '';
        // Pick the right card type for this section
        var useWideCards = (sectionId === 'resumevideo' || sectionId === 'resumeaudio' || sectionId === 'resumebook');
        var isLiveTv = (sectionId === 'livetv' || sectionId === 'activerecordings');
        var isNextUp = (sectionId === 'nextup');
        items.forEach(function (item) {
          if (useWideCards) {
            itemsEl.appendChild(createWideCard(item));
          } else if (isLiveTv) {
            itemsEl.appendChild(createChannelCard(item));
          } else if (isNextUp) {
            itemsEl.appendChild(createNextUpCard(item));
          } else {
            itemsEl.appendChild(createCard(item));
          }
        });
      })
      .catch(function (err) {
        console.error('[Tentacle] Failed to load builtin section "' + sectionId + '":', err);
        row.classList.add('mh-row-hidden');
      });
  }

  function getBuiltinApiPath(sectionId) {
    var base = 'Users/' + MH.userId;
    var commonFields = 'PrimaryImageAspectRatio,MediaSourceCount,Overview,Genres,CommunityRating,OfficialRating,RunTimeTicks,ProductionYear,UserData,ProviderIds';
    switch (sectionId) {
      case 'resumevideo':
        return base + '/Items/Resume?Limit=12&Recursive=true&Fields=' + commonFields + '&ImageTypeLimit=1&EnableImageTypes=Primary,Backdrop,Thumb&MediaTypes=Video';
      case 'resumeaudio':
        return base + '/Items/Resume?Limit=12&Recursive=true&Fields=' + commonFields + '&ImageTypeLimit=1&EnableImageTypes=Primary&MediaTypes=Audio';
      case 'resumebook':
        return base + '/Items/Resume?Limit=12&Recursive=true&Fields=' + commonFields + '&ImageTypeLimit=1&EnableImageTypes=Primary&MediaTypes=Book';
      case 'nextup':
        return 'Shows/NextUp?UserId=' + MH.userId + '&Limit=24&Fields=' + commonFields + '&ImageTypeLimit=1&EnableImageTypes=Primary,Backdrop,Thumb';
      case 'latestmedia':
        return base + '/Items/Latest?Limit=16&Fields=' + commonFields + '&ImageTypeLimit=1&EnableImageTypes=Primary,Backdrop,Thumb';
      case 'livetv':
        return 'LiveTv/Channels?UserId=' + MH.userId + '&Limit=12&Fields=PrimaryImageAspectRatio&ImageTypeLimit=1&EnableImageTypes=Primary';
      case 'activerecordings':
        return 'LiveTv/Recordings?UserId=' + MH.userId + '&IsInProgress=true&Fields=PrimaryImageAspectRatio&ImageTypeLimit=1&EnableImageTypes=Primary';
      default:
        return null;
    }
  }

  function loadLibraryTiles(row, small) {
    apiGet('Users/' + MH.userId + '/Views')
      .then(function (data) {
        var itemsEl = row.querySelector('.mh-row-items');
        itemsEl.classList.remove('mh-loading-row');

        if (!data.Items || !data.Items.length) {
          row.classList.add('mh-row-hidden');
          return;
        }

        itemsEl.innerHTML = '';
        data.Items.forEach(function (item) {
          var tile = document.createElement('div');
          tile.className = small ? 'mh-lib-tile mh-lib-tile-sm' : 'mh-lib-tile';
          tile.onclick = function () {
            var ct = (item.CollectionType || '').toLowerCase();
            var route = ct === 'movies' ? 'movies' :
                        ct === 'tvshows' ? 'tv' :
                        ct === 'music' ? 'music' :
                        ct === 'livetv' ? 'livetv' : null;
            var sid = item.ServerId || MH.apiClient.serverId();
            if (ct === 'playlists') {
              window.location.hash = '#/list?parentId=' + item.Id + '&serverId=' + sid;
            } else if (route) {
              window.location.hash = '#/' + route + '?topParentId=' + item.Id + '&collectionType=' + ct;
            } else {
              window.location.hash = '#/list?parentId=' + item.Id + '&serverId=' + sid;
            }
          };

          var posterTag = item.ImageTags && item.ImageTags.Primary ? item.ImageTags.Primary : '';
          var posterUrl = posterTag ? getImageUrl(item.Id, 'Primary', posterTag, 300) : '';

          tile.innerHTML =
            '<div class="mh-lib-tile-img">' +
              (posterUrl ? '<img src="' + posterUrl + '" alt="" loading="lazy">' : '') +
            '</div>' +
            '<div class="mh-lib-tile-name">' + escapeHtml(item.Name) + '</div>';
          itemsEl.appendChild(tile);
        });
      })
      .catch(function () {
        row.classList.add('mh-row-hidden');
      });
  }

  function createWideCard(item) {
    var card = document.createElement('div');
    card.className = 'mh-card mh-card-wide';
    card.setAttribute('data-item-id', item.Id);
    card.onclick = function () {
      if (window.TentacleDetails) { window.TentacleDetails.show(item.Id); return; }
      window.location.hash = '#/details?id=' + item.Id;
    };

    // Prefer backdrop/thumb for wide cards
    var imgTag = '';
    var imgType = 'Primary';
    if (item.BackdropImageTags && item.BackdropImageTags.length) {
      imgTag = item.BackdropImageTags[0];
      imgType = 'Backdrop';
    } else if (item.ImageTags && item.ImageTags.Thumb) {
      imgTag = item.ImageTags.Thumb;
      imgType = 'Thumb';
    } else if (item.ImageTags && item.ImageTags.Primary) {
      imgTag = item.ImageTags.Primary;
      imgType = 'Primary';
    }
    var imgUrl = imgTag ? getImageUrl(item.Id, imgType, imgTag, 500) : '';

    // Progress bar for resume items
    var progressHtml = '';
    if (item.UserData && item.UserData.PlayedPercentage) {
      var pct = Math.round(item.UserData.PlayedPercentage);
      progressHtml =
        '<div class="mh-card-progress">' +
          '<div class="mh-card-progress-bar" style="width:' + pct + '%"></div>' +
        '</div>' +
        '<div class="mh-card-progress-text">' + pct + '%</div>';
    }

    // Build display name with episode info
    var titleLine = '';
    var subtitleLine = '';
    if (item.SeriesName) {
      titleLine = item.SeriesName;
      var epLabel = '';
      if (item.ParentIndexNumber != null && item.IndexNumber != null) {
        epLabel = 'S' + item.ParentIndexNumber + ':E' + item.IndexNumber + ' · ';
      }
      subtitleLine = epLabel + (item.Name || '');
    } else {
      titleLine = item.Name || '';
    }

    // Remaining time
    var remainingHtml = '';
    if (item.RunTimeTicks && item.UserData && item.UserData.PlaybackPositionTicks) {
      var remainMins = Math.round((item.RunTimeTicks - item.UserData.PlaybackPositionTicks) / 600000000);
      if (remainMins > 0) {
        remainingHtml = '<span class="mh-card-remaining">' + remainMins + 'm left</span>';
      }
    }

    card.innerHTML =
      '<div class="mh-card-poster mh-card-poster-wide">' +
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

  function createNextUpCard(item) {
    var card = document.createElement('div');
    card.className = 'mh-card mh-card-wide';
    card.setAttribute('data-item-id', item.Id);
    card.onclick = function () {
      if (window.TentacleDetails) { window.TentacleDetails.show(item.Id); return; }
      window.location.hash = '#/details?id=' + item.Id;
    };

    // Prefer backdrop/thumb for wide Next Up cards
    var imgTag = '';
    var imgType = 'Primary';
    if (item.BackdropImageTags && item.BackdropImageTags.length) {
      imgTag = item.BackdropImageTags[0];
      imgType = 'Backdrop';
    } else if (item.ImageTags && item.ImageTags.Thumb) {
      imgTag = item.ImageTags.Thumb;
      imgType = 'Thumb';
    } else if (item.ImageTags && item.ImageTags.Primary) {
      imgTag = item.ImageTags.Primary;
      imgType = 'Primary';
    }
    var imgUrl = imgTag ? getImageUrl(item.Id, imgType, imgTag, 500) : '';

    // Episode label badge
    var epBadge = '';
    if (item.ParentIndexNumber != null && item.IndexNumber != null) {
      epBadge = '<div class="mh-card-ep-badge">S' + item.ParentIndexNumber + ':E' + item.IndexNumber + '</div>';
    }

    card.innerHTML =
      '<div class="mh-card-poster mh-card-poster-wide">' +
        (imgUrl
          ? '<img src="' + imgUrl + '" alt="" loading="lazy">'
          : '<div class="mh-card-no-poster">🎬</div>') +
        epBadge +
        '<div class="mh-card-play-overlay">' +
          '<div class="mh-card-play-icon">▶</div>' +
        '</div>' +
      '</div>' +
      '<div class="mh-card-info">' +
        '<div class="mh-card-title" title="' + escapeAttr(item.SeriesName || item.Name) + '">' + escapeHtml(item.SeriesName || item.Name) + '</div>' +
        '<div class="mh-card-subtitle" title="' + escapeAttr(item.Name) + '">' + escapeHtml(item.Name || '') + '</div>' +
      '</div>';

    return card;
  }

  // ── Row Sections ──────────────────────────────────────────────────────
  function loadRow(container, section) {
    var row = document.createElement('div');
    row.className = 'mh-row';

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
        data.Items.forEach(function (item) {
          itemsEl.appendChild(createCard(item));
        });
      })
      .catch(function () {
        // Hide failed rows
        row.classList.add('mh-row-hidden');
      });
  }

  function createChannelCard(item) {
    var card = document.createElement('div');
    card.className = 'mh-card';
    card.setAttribute('data-item-id', item.Id);
    card.onclick = function () {
      if (window.TentacleDetails) { window.TentacleDetails.show(item.Id); return; }
      window.location.hash = '#/details?id=' + item.Id;
    };

    // Live TV channels: request image without tag to avoid 500 errors
    var baseUrl = MH.apiClient.serverAddress();
    var token = MH.apiClient.accessToken();
    var imgUrl = baseUrl + '/Items/' + item.Id + '/Images/Primary?maxWidth=300&quality=90&api_key=' + token;

    var channelNumber = item.ChannelNumber ? item.ChannelNumber + ' · ' : '';
    var meta = channelNumber + (item.CurrentProgram ? item.CurrentProgram.Name || '' : '');

    card.innerHTML =
      '<div class="mh-card-poster">' +
        '<img src="' + imgUrl + '" alt="" loading="lazy" onerror="this.parentNode.innerHTML=\'<div class=mh-card-no-poster>📺</div>\'">' +
        '<div class="mh-card-play-overlay">' +
          '<div class="mh-card-play-icon">▶</div>' +
        '</div>' +
      '</div>' +
      '<div class="mh-card-info">' +
        '<div class="mh-card-title" title="' + escapeAttr(item.Name) + '">' + escapeHtml(item.Name) + '</div>' +
        (meta ? '<div class="mh-card-meta">' + escapeHtml(meta) + '</div>' : '') +
      '</div>';

    return card;
  }

  function createCard(item) {
    var card = document.createElement('div');
    card.className = 'mh-card';
    card.setAttribute('data-item-id', item.Id);
    card.onclick = function () {
      if (window.TentacleDetails) { window.TentacleDetails.show(item.Id); return; }
      window.location.hash = '#/details?id=' + item.Id;
    };

    var posterTag = item.ImageTags && item.ImageTags.Primary ? item.ImageTags.Primary : '';
    var posterUrl = posterTag ? getImageUrl(item.Id, 'Primary', posterTag, 300) : '';

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
    var metaParts = [year ? escapeHtml(String(year)) : '', rating, officialRating].filter(Boolean);
    var metaHtml = metaParts.join(' <span class="mh-card-meta-dot">·</span> ');

    card.innerHTML =
      '<div class="mh-card-poster">' +
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
      MdbList.fetchRatings(item).then(function (ratings) {
        if (ratings && MdbList.buildRatingsHtml) {
          ratingEl.innerHTML = MdbList.buildRatingsHtml(ratings, 'compact') || '';
        }
      }).catch(function () {});
    }

    return card;
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

  // ── Start ─────────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', waitForReady);
  } else {
    waitForReady();
  }
})();
