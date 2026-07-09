// Tentacle Live TV — Favorites grid replacing native Jellyfin Live TV page
//
// Architecture (same pattern as tentacle-search.js):
//   - #tentacleLiveTvContainer mounted on document.body
//   - Body class `tentacle-livetv-active` hides native content
//   - Active on #/livetv (tab 0 or no tab). Inactive on ?tab=1+ so Guide/Recordings work natively.
//   - Fetches favorites via LiveTv/Channels?IsFavorite=true&AddCurrentProgram=true
//   - Click card → playbackManager.play({ ids: [channelId] })
//   - Guide button → navigates to native Guide tab
//   - 60s auto-refresh for now-playing info
(function () {
  'use strict';

  var LIVETV = {
    active: false,
    refreshTimer: null,
    container: null,
  };

  // ── Helpers ────────────────────────────────────────────────────────

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

  function isLiveTvFavoritesPage() {
    var h = (location.hash || '').replace('#', '').replace(/^\//, '');
    var path = h.split('?')[0];
    if (path !== 'livetv' && path !== 'livetv.html') return false;
    // Only active on tab 0 (favorites) or no tab specified
    var match = h.match(/[?&]tab=(\d+)/);
    if (match && parseInt(match[1], 10) > 0) return false;
    return true;
  }

  // ── Activate / Deactivate ──────────────────────────────────────────

  function activate() {
    if (LIVETV.active) { refresh(); return; }
    LIVETV.active = true;
    document.body.classList.add('tentacle-livetv-active');
    ensureContainer();
    LIVETV.container.style.display = 'block';
    refresh();
    LIVETV.refreshTimer = setInterval(refresh, 60000);
  }

  function deactivate() {
    if (!LIVETV.active) return;
    LIVETV.active = false;
    document.body.classList.remove('tentacle-livetv-active');
    if (LIVETV.container) LIVETV.container.style.display = 'none';
    if (LIVETV.refreshTimer) { clearInterval(LIVETV.refreshTimer); LIVETV.refreshTimer = null; }
  }

  // ── Container ──────────────────────────────────────────────────────

  function ensureContainer() {
    if (LIVETV.container) return;
    var el = document.createElement('div');
    el.id = 'tentacleLiveTvContainer';
    document.body.appendChild(el);
    el.addEventListener('click', onContainerClick);
    LIVETV.container = el;
  }

  // ── Fetch & Render ─────────────────────────────────────────────────

  function refresh() {
    if (!LIVETV.active) return;
    ensureContainer();

    apiGet('LiveTv/Channels?IsFavorite=true&AddCurrentProgram=true&SortBy=SortName&SortOrder=Ascending')
      .then(function (result) {
        if (!LIVETV.active) return;
        var channels = (result && result.Items) || [];
        render(channels);
      })
      .catch(function () {
        if (!LIVETV.active) return;
        LIVETV.container.innerHTML = renderHeader() +
          '<div class="tltv-empty"><div class="tltv-empty-icon"><svg viewBox="0 0 24 24"><path d="M21 6h-7.59l3.29-3.29L16 2l-4 4-4-4-.71.71L10.59 6H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h18c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 14H3V8h18v12z"/></svg></div>Failed to load channels.</div>';
      });
  }

  function render(channels) {
    var html = renderHeader();

    if (channels.length === 0) {
      html += '<div class="tltv-empty">' +
        '<div class="tltv-empty-icon"><svg viewBox="0 0 24 24"><path d="M21 6h-7.59l3.29-3.29L16 2l-4 4-4-4-.71.71L10.59 6H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h18c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 14H3V8h18v12z"/></svg></div>' +
        '<div>No favorite channels.<br>Mark channels as favorites in the <a href="#/livetv.html?tab=1" onclick="event.stopPropagation()">Guide</a> to see them here.</div>' +
        '</div>';
    } else {
      html += '<div class="tltv-grid">';
      for (var i = 0; i < channels.length; i++) {
        html += renderCard(channels[i]);
      }
      html += '</div>';
    }

    LIVETV.container.innerHTML = html;
  }

  function renderHeader() {
    return '<div class="tltv-header">' +
      '<div class="tltv-title">Live TV</div>' +
      '<button class="tltv-guide-btn" data-action="guide">' +
        '<svg viewBox="0 0 24 24"><path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z"/></svg>' +
        'Guide' +
      '</button>' +
    '</div>';
  }

  function renderCard(channel) {
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
      var total = end - start;
      if (total > 0) {
        progress = Math.min(100, Math.max(0, ((now - start) / total) * 100));
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

  // ── Click handling ─────────────────────────────────────────────────

  function onContainerClick(e) {
    // Guide button
    var guideBtn = e.target.closest('[data-action="guide"]');
    if (guideBtn) {
      e.preventDefault();
      location.hash = '#/livetv.html?tab=1';
      return;
    }

    // Channel card → play
    var card = e.target.closest('.tltv-card');
    if (card && card.dataset.channelId) {
      e.preventDefault();
      playChannel(card.dataset.channelId);
    }
  }

  function playChannel(channelId) {
    // Try playbackManager first (global in Jellyfin web)
    var pm = (typeof playbackManager !== 'undefined') ? playbackManager : null;
    if (pm) {
      try {
        pm.play({
          ids: [channelId],
          serverId: window.ApiClient.serverId()
        }).catch(function (err) {
          console.error('[Tentacle LiveTV] playbackManager.play() failed, trying sendPlayCommand', err);
          playViaSession(channelId);
        });
        return;
      } catch (err) {
        console.error('[Tentacle LiveTV] playbackManager.play() threw', err);
      }
    }

    // Fallback: send play command via session API
    playViaSession(channelId);
  }

  function playViaSession(channelId) {
    var api = window.ApiClient;
    if (!api) return;

    if (typeof api.sendPlayCommand === 'function') {
      var deviceId = api.deviceId();
      api.getSessions({ DeviceId: deviceId }).then(function (sessions) {
        if (sessions && sessions.length > 0) {
          return api.sendPlayCommand(sessions[0].Id, {
            ItemIds: [channelId],
            PlayCommand: 'PlayNow'
          });
        }
        throw new Error('No session');
      }).catch(function (err) {
        console.error('[Tentacle LiveTV] sendPlayCommand failed, trying URL navigation', err);
        navigateToPlay(channelId);
      });
    } else {
      navigateToPlay(channelId);
    }
  }

  function navigateToPlay(channelId) {
    // Last resort: navigate to the item which triggers Jellyfin's native playback
    location.hash = '#/details?id=' + channelId;
  }

  // ── Navigation listeners ───────────────────────────────────────────

  function onRouteChange() {
    if (isLiveTvFavoritesPage()) {
      // Small delay to let Jellyfin finish its view transition
      setTimeout(activate, 50);
    } else {
      deactivate();
    }
  }

  document.addEventListener('viewshow', onRouteChange);
  window.addEventListener('hashchange', onRouteChange);
  window.addEventListener('popstate', onRouteChange);

  // Public API — lets other Tentacle overlays (Discover/Activity) dismiss this
  // one. The Live TV overlay sits at z-index 900, so anything opened without
  // dismissing it first would be invisible underneath.
  window.TentacleLiveTV = {
    isActive: function () { return LIVETV.active; },
    hide: function () { deactivate(); },
  };

  // Initial check
  if (isLiveTvFavoritesPage()) {
    setTimeout(activate, 100);
  }
})();
