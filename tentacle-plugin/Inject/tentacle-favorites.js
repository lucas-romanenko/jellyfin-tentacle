// Tentacle Favorites — styled favorites page replacing Jellyfin's native tab
//
// Architecture (same pattern as tentacle-livetv.js):
//   - #tentacleFavoritesContainer mounted on document.body (z-index 900)
//   - Body class `tentacle-favorites-active` while shown
//   - Route-driven: active on the home route with ?tab=1 (Jellyfin's Favorites
//     tab) — back/forward and refresh work naturally
//   - Sections: Movies, Shows, Episodes, Collections, Playlists (empty hidden)
//   - Click card → Tentacle details overlay (fallback: native details page)
//   - Heart button unfavorites in place
(function () {
  'use strict';

  var FAV = {
    active: false,
    container: null,
    loading: false,
  };

  var SECTIONS = [
    { key: 'movies', title: 'Movies', types: 'Movie', shape: 'poster' },
    { key: 'shows', title: 'Shows', types: 'Series', shape: 'poster' },
    { key: 'episodes', title: 'Episodes', types: 'Episode', shape: 'wide' },
    { key: 'collections', title: 'Collections', types: 'BoxSet', shape: 'poster' },
    { key: 'playlists', title: 'Playlists', types: 'Playlist', shape: 'poster' },
  ];

  function esc(str) {
    if (!str) return '';
    var d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  function apiGet(path) {
    return window.ApiClient.getJSON(window.ApiClient.getUrl(path));
  }

  // ── Route detection ────────────────────────────────────────────────
  // Favorites is Jellyfin's home tab=1
  function isFavoritesPage() {
    var h = location.hash || '';
    if (!/^#?\/?(home(\.html)?)?(\?|$)/.test(h.replace('#', ''))) {
      // Not a home route at all
      if (!/^#\/home(\.html)?\?/.test(h)) return false;
    }
    var isHome = h === '' || h === '#/' || /^#\/home(\.html)?(\?|$)/.test(h);
    if (!isHome) return false;
    return /[?&]tab=1\b/.test(h);
  }

  // ── Activate / Deactivate ──────────────────────────────────────────

  function activate() {
    if (FAV.active) { refresh(); return; }
    FAV.active = true;
    document.body.classList.add('tentacle-favorites-active');
    ensureContainer();
    FAV.container.style.display = 'block';
    refresh();
  }

  function deactivate() {
    if (!FAV.active) return;
    FAV.active = false;
    document.body.classList.remove('tentacle-favorites-active');
    if (FAV.container) FAV.container.style.display = 'none';
  }

  // ── Container ──────────────────────────────────────────────────────

  function ensureContainer() {
    if (FAV.container) return;
    var el = document.createElement('div');
    el.id = 'tentacleFavoritesContainer';
    document.body.appendChild(el);
    el.addEventListener('click', onContainerClick);
    FAV.container = el;
  }

  // ── Fetch & Render ─────────────────────────────────────────────────

  function refresh() {
    if (!FAV.active || FAV.loading) return;
    FAV.loading = true;
    ensureContainer();

    var uid = window.ApiClient.getCurrentUserId();
    var fetches = SECTIONS.map(function (sec) {
      return apiGet(
        'Users/' + uid + '/Items?Filters=IsFavorite&Recursive=true&IncludeItemTypes=' + sec.types +
        '&SortBy=SortName&SortOrder=Ascending&Limit=200' +
        '&Fields=PrimaryImageAspectRatio,ProductionYear,SeriesName' +
        '&ImageTypeLimit=1&EnableImageTypes=Primary,Thumb'
      ).then(function (data) {
        return { section: sec, items: (data && data.Items) || [] };
      }).catch(function () {
        return { section: sec, items: [] };
      });
    });

    // Favorited Live TV channels (with now-playing info, like the Live TV page)
    var channelsFetch = apiGet('LiveTv/Channels?IsFavorite=true&AddCurrentProgram=true&SortBy=SortName&SortOrder=Ascending')
      .then(function (data) { return (data && data.Items) || []; })
      .catch(function () { return []; });

    Promise.all([Promise.all(fetches), channelsFetch]).then(function (all) {
      FAV.loading = false;
      if (!FAV.active) return;
      render(all[0], all[1]);
    }).catch(function () {
      FAV.loading = false;
    });
  }

  function imageUrl(item, wide) {
    var tag = item.ImageTags && item.ImageTags.Primary;
    if (!tag) return null;
    return window.ApiClient.getUrl('Items/' + item.Id + '/Images/Primary', {
      maxHeight: wide ? 240 : 360,
      tag: tag,
      quality: 90,
    });
  }

  function render(results, channels) {
    channels = channels || [];
    var total = results.reduce(function (n, r) { return n + r.items.length; }, 0) + channels.length;

    var html =
      '<div class="tfav-header">' +
        '<div class="tfav-title">Favorites</div>' +
        '<div class="tfav-count">' + total + ' item' + (total !== 1 ? 's' : '') + '</div>' +
      '</div>';

    if (!total) {
      html += '<div class="tfav-empty">No favorites yet. Mark movies, shows, episodes or channels with the ♥ heart and they’ll show up here.</div>';
      FAV.container.innerHTML = html;
      return;
    }

    var sectionHtml = function (r) {
      if (!r.items.length) return '';
      var wide = r.section.shape === 'wide';
      return '<div class="tfav-section">' +
          '<div class="tfav-section-title">' + esc(r.section.title) +
            '<span class="tfav-section-count">' + r.items.length + '</span></div>' +
          '<div class="tfav-grid ' + (wide ? 'tfav-grid-wide' : 'tfav-grid-poster') + '">' +
          r.items.map(function (item) { return card(item, wide); }).join('') +
          '</div>' +
        '</div>';
    };

    results.forEach(function (r, idx) {
      html += sectionHtml(r);
      // Live TV channels slot in after Episodes (index 2)
      if (idx === 2 && channels.length) {
        html += channelsSection(channels);
      }
    });

    FAV.container.innerHTML = html;
  }

  // ── Live TV channels section — same look as the Tentacle Live TV page
  //    (reuses .tltv-card styles from tentacle-livetv.css) ──────────────

  function channelsSection(channels) {
    return '<div class="tfav-section">' +
        '<div class="tfav-section-title">Live TV' +
          '<span class="tfav-section-count">' + channels.length + '</span></div>' +
        '<div class="tltv-grid">' +
        channels.map(channelCard).join('') +
        '</div>' +
      '</div>';
  }

  function channelCard(channel) {
    var name = esc(channel.Name || 'Unknown');
    var program = channel.CurrentProgram;
    var programName = program ? esc(program.Name || '') : '';
    var timeStr = '';
    var progress = 0;

    if (program && program.StartDate && program.EndDate) {
      var start = new Date(program.StartDate);
      var end = new Date(program.EndDate);
      var now = new Date();
      timeStr = formatTime(start) + ' - ' + formatTime(end);
      var totalMs = end - start;
      if (totalMs > 0) {
        progress = Math.min(100, Math.max(0, ((now - start) / totalMs) * 100));
      }
    }

    var logoHtml;
    if (channel.ImageTags && channel.ImageTags.Primary) {
      var imgUrl = window.ApiClient.getUrl('Items/' + channel.Id + '/Images/Primary', { maxHeight: 112, quality: 90 });
      logoHtml = '<div class="tltv-logo"><img src="' + imgUrl + '" alt="" loading="lazy" /></div>';
    } else {
      logoHtml = '<div class="tltv-logo"><div class="tltv-logo-fallback"><svg viewBox="0 0 24 24"><path d="M21 6h-7.59l3.29-3.29L16 2l-4 4-4-4-.71.71L10.59 6H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h18c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 14H3V8h18v12z"/></svg></div></div>';
    }

    return '<div class="tltv-card" data-channel-id="' + channel.Id + '">' +
      logoHtml +
      '<div class="tltv-info">' +
        '<div class="tltv-channel-name">' + name + '</div>' +
        (programName ? '<div class="tltv-program-name">' + programName + '</div>' : '') +
        (timeStr ? '<div class="tltv-program-time">' + timeStr + '</div>' : '') +
      '</div>' +
      '<button class="tfav-unfav" title="Remove from favorites" data-unfav="' + channel.Id + '">&#10084;</button>' +
      '<div class="tltv-progress"><div class="tltv-progress-bar" style="width:' + progress.toFixed(1) + '%"></div></div>' +
    '</div>';
  }

  function formatTime(date) {
    var h = date.getHours();
    var m = date.getMinutes();
    var ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    return h + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm;
  }

  function card(item, wide) {
    var img = imageUrl(item, wide);
    var poster = img
      ? '<img src="' + img + '" loading="lazy" onerror="this.style.display=\'none\'">'
      : '<div class="tfav-card-placeholder">&#9707;</div>';
    var sub = '';
    if (item.Type === 'Episode') {
      sub = esc(item.SeriesName || '');
      var s = item.ParentIndexNumber, e = item.IndexNumber;
      if (s != null && e != null) sub += (sub ? ' · ' : '') + 'S' + s + ':E' + e;
    } else if (item.ProductionYear) {
      sub = String(item.ProductionYear);
    }

    return '<div class="tfav-card ' + (wide ? 'tfav-card-wide' : 'tfav-card-poster') + '"' +
      ' data-id="' + item.Id + '" data-type="' + esc(item.Type) + '">' +
        '<div class="tfav-card-img">' + poster +
          '<button class="tfav-unfav" title="Remove from favorites" data-unfav="' + item.Id + '">&#10084;</button>' +
        '</div>' +
        '<div class="tfav-card-name" title="' + esc(item.Name) + '">' + esc(item.Name) + '</div>' +
        (sub ? '<div class="tfav-card-sub">' + sub + '</div>' : '') +
      '</div>';
  }

  // ── Interactions ───────────────────────────────────────────────────

  function onContainerClick(e) {
    // Unfavorite heart
    var unfav = e.target.closest('[data-unfav]');
    if (unfav) {
      e.preventDefault();
      e.stopPropagation();
      var id = unfav.getAttribute('data-unfav');
      unfavorite(id, unfav);
      return;
    }

    // Live TV channel card → play (same behavior as the Live TV page)
    var channelEl = e.target.closest('[data-channel-id]');
    if (channelEl) {
      var channelId = channelEl.getAttribute('data-channel-id');
      if (window.TentacleLiveTV && window.TentacleLiveTV.playChannel) {
        window.TentacleLiveTV.playChannel(channelId);
      } else {
        window.location.hash = '#/details?id=' + channelId;
      }
      return;
    }

    // Card → details
    var cardEl = e.target.closest('.tfav-card');
    if (!cardEl) return;
    var itemId = cardEl.getAttribute('data-id');
    var itemType = cardEl.getAttribute('data-type');
    if (!itemId) return;

    if ((itemType === 'Movie' || itemType === 'Series') &&
        window.TentacleDetails && window.TentacleDetails.show) {
      window.TentacleDetails.show(itemId, itemType);
    } else {
      window.location.hash = '#/details?id=' + itemId;
    }
  }

  function unfavorite(itemId, btnEl) {
    var uid = window.ApiClient.getCurrentUserId();
    var done = function () {
      var cardEl = btnEl.closest('.tfav-card') || btnEl.closest('.tltv-card');
      if (!cardEl) return;
      var grid = cardEl.parentElement;
      var section = cardEl.closest('.tfav-section');
      cardEl.remove();
      // Update section count / remove empty sections
      if (grid && section) {
        var left = grid.querySelectorAll('.tfav-card').length;
        if (left === 0) section.remove();
        else {
          var count = section.querySelector('.tfav-section-count');
          if (count) count.textContent = left;
        }
      }
    };
    try {
      if (window.ApiClient.updateFavoriteStatus) {
        window.ApiClient.updateFavoriteStatus(itemId, false).then(done).catch(function () {});
      } else {
        window.ApiClient.ajax({
          type: 'DELETE',
          url: window.ApiClient.getUrl('Users/' + uid + '/FavoriteItems/' + itemId),
        }).then(done).catch(function () {});
      }
    } catch (err) { /* keep card on failure */ }
  }

  // ── Navigation listeners ───────────────────────────────────────────

  function onRouteChange() {
    if (isFavoritesPage()) {
      // Small delay to let Jellyfin finish its view transition
      setTimeout(activate, 50);
    } else {
      deactivate();
    }
  }

  document.addEventListener('viewshow', onRouteChange);
  window.addEventListener('hashchange', onRouteChange);
  window.addEventListener('popstate', onRouteChange);

  // Public API — lets other Tentacle overlays dismiss this one (it sits at
  // z-index 900, same layer as the Live TV overlay)
  window.TentacleFavorites = {
    isActive: function () { return FAV.active; },
    hide: function () { deactivate(); },
  };

  // Initial check (deep link / refresh straight onto the favorites route)
  if (isFavoritesPage()) {
    setTimeout(activate, 100);
  }
})();
