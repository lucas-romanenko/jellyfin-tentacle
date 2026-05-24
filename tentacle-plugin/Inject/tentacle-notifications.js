/**
 * Tentacle Notifications — Toast notifications for download completions.
 * Polls /TentacleDiscover/Notifications every 15s and shows toast popups.
 * Per-user: only shows notifications for the current user's downloads.
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

    // Start polling
    poll();
    TN.pollInterval = setInterval(poll, 15000);

    // Re-init on user switch
    document.addEventListener('viewshow', function () {
      if (window.ApiClient && window.ApiClient.getCurrentUserId() !== TN.userId) {
        TN.userId = window.ApiClient.getCurrentUserId();
        TN.knownIds.clear();
      }
    });
  }

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

  function showToast(notif, onDone) {
    var toast = document.createElement('div');
    toast.className = 'tentacle-toast';

    // Poster
    var posterUrl = notif.poster_path
      ? 'https://image.tmdb.org/t/p/w154' + notif.poster_path
      : '';

    toast.innerHTML =
      '<div class="tentacle-toast-inner">' +
        (posterUrl ? '<img class="tentacle-toast-poster" src="' + posterUrl + '" alt="" />' : '') +
        '<div class="tentacle-toast-content">' +
          '<div class="tentacle-toast-label">Ready to Watch</div>' +
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

  // Start when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
