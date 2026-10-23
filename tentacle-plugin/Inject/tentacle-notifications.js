/**
 * Tentacle Notifications — Toast notifications for download completions.
 * Reads /TentacleDiscover/Notifications when Tentacle says something changed
 * (live, below) and every 60 s as a safety net, and shows toast popups.
 * Per-user: only shows notifications for the current user's downloads (and,
 * for an admin who follows them, other people's, marked "for Sam").
 *
 * Live: Tentacle tells the person's Jellyfin sessions through the plugin
 * (POST /Tentacle/Notify): a RefreshProgress message with ItemId "tentacle"
 * and the topics that changed ("requests", "activity", "notifications").
 * This script hears it on Jellyfin web's own socket and passes it on as a
 * window event "tentacle-changed" ({ topics: [...] }) for the Discover and
 * Activity overlays and the navbar badge. After the socket comes back, or
 * the tab comes back to the front, everything is read again (a message sent
 * meanwhile was lost).
 */
(function () {
  'use strict';

  var TN = {
    apiClient: null,
    userId: null,
    pollInterval: null,
    knownIds: new Set(),  // Track IDs we've already shown toasts for
    toastQueue: [],
    isShowingToast: false,
  };

  function init() {
    if (!window.ApiClient || !window.ApiClient.getCurrentUserId()) {
      setTimeout(init, 1000);
      return;
    }
    TN.apiClient = window.ApiClient;
    TN.userId = TN.apiClient.getCurrentUserId();

    // Read now, then live (watchSocket), with a slow poll as a safety net
    poll();
    TN.pollInterval = setInterval(poll, 60000);
    watchSocket();
    setInterval(watchSocket, 2000);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) changed(ALL_TOPICS, true);
    });

    // Re-init on user switch
    document.addEventListener('viewshow', function () {
      if (window.ApiClient && window.ApiClient.getCurrentUserId() !== TN.userId) {
        TN.userId = window.ApiClient.getCurrentUserId();
        TN.knownIds.clear();
      }
    });
  }

  // ── Live: Tentacle's changes on Jellyfin web's socket ───────────────
  var ALL_TOPICS = ['requests', 'activity', 'notifications'];

  // Jellyfin sends a browser's messages to one socket only (the newest tab's):
  // the tab that hears one passes it to the others.
  var relay = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('tentacle-changes') : null;
  if (relay) {
    relay.onmessage = function (e) { if (e.data && e.data.topics) changed(e.data.topics, true); };
    if (relay.unref) relay.unref();
  }

  // Jellyfin web's own socket (a new object after each reconnect); we only listen.
  function watchSocket() {
    var ws = window.ApiClient && window.ApiClient._webSocket;
    if (!ws || ws === TN.socket || typeof ws.addEventListener !== 'function') return;
    var again = !!TN.socket;
    TN.socket = ws;
    ws.addEventListener('message', function (e) {
      if (e.target !== TN.socket) return;
      var topics = topicsOf(e.data);
      if (topics) changed(topics, false);
    });
    if (again) changed(ALL_TOPICS, true);
  }

  // The topics of a Tentacle message, or null for any other message.
  function topicsOf(raw) {
    var msg;
    try { msg = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch (x) { return null; }
    if (!msg || msg.MessageType !== 'RefreshProgress' || !msg.Data || msg.Data.ItemId !== 'tentacle') return null;
    return String(msg.Data.Tentacle || '').split(',').filter(Boolean);
  }

  function changed(topics, relayed) {
    if (!relayed && relay) { try { relay.postMessage({ topics: topics }); } catch (x) { } }
    if (topics.indexOf('notifications') !== -1) poll();
    try { window.dispatchEvent(new CustomEvent('tentacle-changed', { detail: { topics: topics } })); } catch (x) { }
  }

  TN.topicsOf = topicsOf;

  function poll() {
    if (!TN.apiClient || !TN.userId) return;

    var url = TN.apiClient.getUrl('TentacleDiscover/Notifications?userId=' + TN.userId);
    TN.apiClient.getJSON(url).then(function (data) {
      if (!data || !data.notifications || !data.notifications.length) return;
      if (!data.notifications_enabled) return;

      // Show toasts for new notifications we haven't seen
      for (var i = 0; i < data.notifications.length; i++) {
        var notif = data.notifications[i];
        if (!TN.knownIds.has(notif.id)) {
          TN.knownIds.add(notif.id);
          TN.toastQueue.push(notif);
        }
      }
      processQueue();
    }).catch(function () {
      // Silently ignore — plugin may not be available
    });
  }

  function processQueue() {
    if (TN.isShowingToast || !TN.toastQueue.length) return;
    TN.isShowingToast = true;

    var notif = TN.toastQueue.shift();
    showToast(notif, function () {
      TN.isShowingToast = false;
      // Show next toast after a short gap
      if (TN.toastQueue.length) {
        setTimeout(processQueue, 500);
      }
    });
  }

  // The heading says what the message is about. Older servers send no kind:
  // their messages are all "ready to watch".
  // An admin's message about someone else's download says whose it is.
  function toastLabel(notif) {
    if (notif.kind === 'request') return 'Request';
    if (notif.kind === 'problem') return notif.title || 'Problem';
    return notif.for ? 'Ready to Watch \u00b7 for ' + notif.for : 'Ready to Watch';
  }

  function showToast(notif, onDone) {
    var toast = document.createElement('div');
    toast.className = 'tentacle-toast';

    // Poster. poster_path is backend-supplied text; escape it before it lands in an
    // attribute, otherwise a value containing a quote breaks out of src="..." .
    var posterUrl = notif.poster_path
      ? escapeAttr('https://image.tmdb.org/t/p/w154' + notif.poster_path)
      : '';

    toast.innerHTML =
      '<div class="tentacle-toast-inner">' +
        (posterUrl ? '<img class="tentacle-toast-poster" src="' + posterUrl + '" alt="" />' : '') +
        '<div class="tentacle-toast-content">' +
          '<div class="tentacle-toast-label">' + escapeHtml(toastLabel(notif)) + '</div>' +
          '<div class="tentacle-toast-title">' + escapeHtml(notif.message) + '</div>' +
        '</div>' +
        '<button class="tentacle-toast-close" aria-label="Dismiss">&times;</button>' +
      '</div>';

    document.body.appendChild(toast);

    // Click poster/title to navigate to item
    var inner = toast.querySelector('.tentacle-toast-inner');
    inner.style.cursor = notif.jellyfin_item_id ? 'pointer' : 'default';
    if (notif.jellyfin_item_id) {
      inner.addEventListener('click', function (e) {
        if (e.target.classList.contains('tentacle-toast-close')) return;
        dismissAndNavigate(notif);
        removeToast(toast, onDone);
      });
    }

    // Dismiss button
    toast.querySelector('.tentacle-toast-close').addEventListener('click', function () {
      dismissNotification(notif.id);
      removeToast(toast, onDone);
    });

    // Animate in
    requestAnimationFrame(function () {
      toast.classList.add('tentacle-toast-show');
    });

    // Auto-dismiss after 8 seconds
    setTimeout(function () {
      if (toast.parentNode) {
        dismissNotification(notif.id);
        removeToast(toast, onDone);
      }
    }, 8000);
  }

  function removeToast(toast, onDone) {
    if (!toast.parentNode) {
      if (onDone) onDone();
      return;
    }
    toast.classList.remove('tentacle-toast-show');
    toast.classList.add('tentacle-toast-hide');
    setTimeout(function () {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
      if (onDone) onDone();
    }, 400);
  }

  function dismissNotification(id) {
    if (!TN.apiClient || !TN.userId) return;
    var url = TN.apiClient.getUrl('TentacleDiscover/Notifications/' + id + '/Dismiss?userId=' + TN.userId);
    TN.apiClient.ajax({ type: 'POST', url: url }).catch(function () {});
  }

  function dismissAndNavigate(notif) {
    dismissNotification(notif.id);
    if (notif.jellyfin_item_id) {
      // Use TentacleDetails overlay if available, otherwise navigate
      if (window.TentacleDetails && window.TentacleDetails.show) {
        var itemType = notif.media_type === 'series' ? 'Series' : 'Movie';
        window.TentacleDetails.show(notif.jellyfin_item_id, itemType);
      } else {
        var detailUrl = '#!/details?id=' + notif.jellyfin_item_id;
        if (window.Emby && window.Emby.Page) {
          window.Emby.Page.show(detailUrl);
        } else {
          window.location.hash = detailUrl;
        }
      }
    }
  }

  function escapeHtml(str) {
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function escapeAttr(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // Start when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
