/**
 * Tentacle Details Overlay
 * Full-screen modal for item details, inspired by Moonfin.
 * Intercepts card clicks on the Tentacle home and Jellyfin library pages.
 */
(function () {
    'use strict';

    var TD = {
        overlay: null,
        apiClient: null,
        userId: null,
        history: [],
        navigatingBack: false,
        initialized: false
    };

    // ── SVG Icons ──
    var ICONS = {
        back: '<svg viewBox="0 0 24 24" width="22" height="22"><path fill="#fff" d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>',
        play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
        check: '<svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>',
        heart: '<svg viewBox="0 0 24 24"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>',
        heartOutline: '<svg viewBox="0 0 24 24"><path d="M16.5 3c-1.74 0-3.41.81-4.5 2.09C10.91 3.81 9.24 3 7.5 3 4.42 3 2 5.42 2 8.5c0 3.78 3.4 6.86 8.55 11.54L12 21.35l1.45-1.32C18.6 15.36 22 12.28 22 8.5 22 5.42 19.58 3 16.5 3zm-4.4 15.55l-.1.1-.1-.1C7.14 14.24 4 11.39 4 8.5 4 6.5 5.5 5 7.5 5c1.54 0 3.04.99 3.57 2.36h1.87C13.46 5.99 14.96 5 16.5 5c2 0 3.5 1.5 3.5 3.5 0 2.89-3.14 5.74-7.9 10.05z"/></svg>',
        person: '<svg viewBox="0 0 24 24"><path fill="rgba(255,255,255,0.15)" d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>'
    };

    // ── Bootstrap ──
    function waitForReady() {
        if (window.ApiClient && window.ApiClient.getCurrentUserId()) {
            TD.apiClient = window.ApiClient;
            TD.userId = window.ApiClient.getCurrentUserId();
            init();
        } else {
            setTimeout(waitForReady, 300);
        }
    }

    function init() {
        if (TD.initialized) return;
        TD.initialized = true;
        createOverlayDOM();
        attachListeners();
    }

    // ── Overlay DOM ──
    function createOverlayDOM() {
        var el = document.createElement('div');
        el.className = 'td-overlay';
        el.innerHTML =
            '<div class="td-backdrop"></div>' +
            '<div class="td-panel">' +
            '  <button class="td-back" aria-label="Back">' + ICONS.back + '</button>' +
            '  <div class="td-content"></div>' +
            '</div>';
        document.body.appendChild(el);
        TD.overlay = el;

        el.querySelector('.td-back').addEventListener('click', goBack);
        el.querySelector('.td-panel').addEventListener('click', function (e) {
            if (e.target === el.querySelector('.td-panel') || e.target === el.querySelector('.td-backdrop')) {
                closeOverlay();
            }
        });
    }

    // ── Event Listeners ──
    function attachListeners() {
        document.addEventListener('click', onCardClick, true);
        document.addEventListener('keydown', function (e) {
            if (!isOpen()) return;
            if (e.key === 'Escape' || e.keyCode === 27 || e.keyCode === 461 || e.keyCode === 10009) {
                e.preventDefault();
                e.stopPropagation();
                goBack();
            }
        }, true);
    }

    // ── Card Click Interception ──
    var IGNORE_SELECTORS = [
        '.videoOsdBottom', '.videoOsdTop', '.dialog', '.dialogContainer',
        '.actionSheetContent', '.subtitleSync', '.osdFocusContainer',
        '.focuscontainer-x', '.upNextContainer', '.videoOsd',
        '.mh-hero', '.mh-hero-slide'
    ];

    var IGNORE_BUTTON_SELECTORS = [
        '.cardOverlayButton', '.btnPlayItem', '.btnMoreCommands',
        '.paper-icon-button-light', '.mh-card-play-overlay',
        '.td-btn', '.td-btn-circle', '.td-back'
    ];

    function onCardClick(e) {
        // Don't intercept if inside ignored containers
        for (var i = 0; i < IGNORE_SELECTORS.length; i++) {
            if (e.target.closest(IGNORE_SELECTORS[i])) return;
        }

        // Don't intercept button clicks
        for (var j = 0; j < IGNORE_BUTTON_SELECTORS.length; j++) {
            if (e.target.closest(IGNORE_BUTTON_SELECTORS[j])) return;
        }

        // Find the card element
        var card = e.target.closest('.card, .listItem, .mh-card, .mh-card-wide');
        if (!card) return;

        // Must be in a valid section
        var validParent = card.closest(
            '.homeSection, .section, .itemsContainer, .libraryPage, ' +
            '#tentacle-home, .mh-row, .td-section'
        );
        if (!validParent) return;

        // Extract item ID
        var itemId = extractItemId(card);
        if (!itemId) return;

        e.preventDefault();
        e.stopPropagation();

        showDetails(itemId);
    }

    function extractItemId(card) {
        var id = card.getAttribute('data-id') || card.getAttribute('data-itemid');
        if (id) return id;

        var inner = card.querySelector('[data-id]');
        if (inner) return inner.getAttribute('data-id');

        inner = card.querySelector('[data-itemid]');
        if (inner) return inner.getAttribute('data-itemid');

        // Tentacle home cards store the ID in onclick handler or data attribute
        var tdId = card.getAttribute('data-item-id');
        if (tdId) return tdId;

        // Try href
        var link = card.querySelector('a[href]') || (card.tagName === 'A' ? card : null);
        if (link) {
            var href = link.getAttribute('href') || '';
            var match = href.match(/id=([a-f0-9]+)/i) || href.match(/([a-f0-9]{32})/i);
            if (match) return match[1];
        }

        return null;
    }

    // ── Show Details ──
    function showDetails(itemId) {
        if (!TD.overlay) return;

        // Push current to history if we're already showing something
        var currentId = TD.overlay.getAttribute('data-current-id');
        if (currentId && !TD.navigatingBack) {
            TD.history.push(currentId);
        }
        TD.navigatingBack = false;

        TD.overlay.setAttribute('data-current-id', itemId);

        // Show loading state
        var content = TD.overlay.querySelector('.td-content');
        content.innerHTML = '<div class="td-loading"><div class="mh-spinner"></div></div>';
        TD.overlay.classList.add('active');
        document.body.style.overflow = 'hidden';

        // Fetch item data
        fetchItem(itemId).then(function (item) {
            if (!item) {
                closeOverlay();
                return;
            }

            // Set backdrop
            var backdropUrl = getImageUrl(item, 'Backdrop', 1920);
            var backdrop = TD.overlay.querySelector('.td-backdrop');
            backdrop.style.backgroundImage = backdropUrl ? 'url(' + backdropUrl + ')' : 'none';

            // Render content
            renderDetails(item);

            // Fetch additional data in parallel
            fetchAdditionalData(item);
        }).catch(function () {
            closeOverlay();
        });
    }

    // ── Fetch Item ──
    function fetchItem(itemId) {
        return TD.apiClient.getJSON(TD.apiClient.getUrl(
            'Users/' + TD.userId + '/Items/' + itemId,
            { Fields: 'Overview,Genres,People,Studios,ProviderIds,MediaSources,Chapters' }
        ));
    }

    function fetchSimilar(itemId) {
        return TD.apiClient.getJSON(TD.apiClient.getUrl(
            'Items/' + itemId + '/SimilarItems',
            { UserId: TD.userId, Limit: 12, Fields: 'PrimaryImageAspectRatio' }
        )).then(function (r) { return r.Items || []; })
          .catch(function () { return []; });
    }

    function fetchSeasons(seriesId) {
        return TD.apiClient.getJSON(TD.apiClient.getUrl(
            'Shows/' + seriesId + '/Seasons',
            { UserId: TD.userId, Fields: 'PrimaryImageAspectRatio' }
        )).then(function (r) { return r.Items || []; })
          .catch(function () { return []; });
    }

    function fetchEpisodes(seriesId, seasonId) {
        return TD.apiClient.getJSON(TD.apiClient.getUrl(
            'Shows/' + seriesId + '/Episodes',
            { UserId: TD.userId, SeasonId: seasonId, Fields: 'Overview,PrimaryImageAspectRatio', Limit: 50 }
        )).then(function (r) { return r.Items || []; })
          .catch(function () { return []; });
    }

    // ── Render Details ──
    function renderDetails(item) {
        var content = TD.overlay.querySelector('.td-content');
        var html = '';

        // Header (info + poster)
        html += '<div class="td-header">';
        html += '<div class="td-info">';

        // Episode header
        if (item.Type === 'Episode' && item.SeriesName) {
            html += '<div class="td-episode-header">' + esc(item.SeriesName);
            html += '<span class="td-episode-badge">S' + pad(item.ParentIndexNumber) + 'E' + pad(item.IndexNumber) + '</span>';
            html += '</div>';
        }

        // Logo or title
        var logoUrl = getImageUrl(item, 'Logo', 500);
        if (logoUrl) {
            html += '<img class="td-logo" src="' + attr(logoUrl) + '" alt="' + attr(item.Name) + '" />';
        } else {
            html += '<h1 class="td-title">' + esc(item.Name || '') + '</h1>';
        }

        // Meta line
        html += buildMetaLine(item);

        // Tagline
        if (item.Taglines && item.Taglines.length > 0) {
            html += '<div class="td-tagline">' + esc(item.Taglines[0]) + '</div>';
        }

        // Genres
        if (item.Genres && item.Genres.length > 0) {
            html += '<div class="td-genres">';
            item.Genres.slice(0, 5).forEach(function (g) {
                html += '<span class="td-genre">' + esc(g) + '</span>';
            });
            html += '</div>';
        }

        // Overview
        if (item.Overview) {
            html += '<div class="td-overview">' + esc(item.Overview) + '</div>';
        }

        html += '</div>'; // .td-info

        // Poster
        var posterUrl = item.Type === 'Episode'
            ? getImageUrl(item, 'Primary', 400) || getImageUrl(item, 'Thumb', 400)
            : getImageUrl(item, 'Primary', 400);
        if (posterUrl) {
            var posterClass = item.Type === 'Episode' ? 'td-poster td-poster-wide' : 'td-poster';
            html += '<div class="td-poster-wrap"><img class="' + posterClass + '" src="' + attr(posterUrl) + '" loading="lazy" /></div>';
        }

        html += '</div>'; // .td-header

        // Action buttons
        html += buildActions(item);

        // Sections placeholder
        html += '<div class="td-sections"></div>';

        content.innerHTML = html;

        // Wire up button handlers
        wireActions(item);
    }

    // ── Meta Line ──
    function buildMetaLine(item) {
        var parts = [];

        if (item.ProductionYear) {
            parts.push(esc(String(item.ProductionYear)));
        }

        if (item.CommunityRating) {
            parts.push('<span class="td-meta-rating">&#9733; ' + item.CommunityRating.toFixed(1) + '</span>');
        }

        if (item.OfficialRating) {
            parts.push('<span class="td-meta-age">' + esc(item.OfficialRating) + '</span>');
        }

        if (item.RunTimeTicks) {
            var mins = Math.round(item.RunTimeTicks / 600000000);
            if (mins >= 60) {
                parts.push(Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm');
            } else {
                parts.push(mins + 'm');
            }
        }

        if (item.Type === 'Series' && item.Status) {
            parts.push(esc(item.Status));
        }

        if (parts.length === 0) return '';
        return '<div class="td-meta">' + parts.join('<span class="td-meta-sep">&#9679;</span>') + '</div>';
    }

    // ── Action Buttons ──
    function buildActions(item) {
        var html = '<div class="td-actions">';
        var hasResume = item.UserData && item.UserData.PlaybackPositionTicks > 0;

        if (item.Type !== 'Person') {
            if (hasResume) {
                var pct = Math.round((item.UserData.PlaybackPositionTicks / item.RunTimeTicks) * 100);
                html += '<button class="td-btn td-btn-resume" data-action="resume">' + ICONS.play + ' Resume (' + pct + '%)</button>';
                html += '<button class="td-btn td-btn-play" data-action="play">' + ICONS.play + ' Restart</button>';
            } else if (item.Type === 'Series') {
                // No direct play for series — show play if there are episodes
                html += '<button class="td-btn td-btn-play" data-action="play">' + ICONS.play + ' Play</button>';
            } else {
                html += '<button class="td-btn td-btn-play" data-action="play">' + ICONS.play + ' Play</button>';
            }
        }

        // Watched toggle
        if (item.UserData) {
            var watchedClass = item.UserData.Played ? ' active' : '';
            html += '<div class="td-btn-group">';
            html += '<button class="td-btn-circle' + watchedClass + '" data-action="watched" title="Watched">' + ICONS.check + '</button>';
            html += '<span class="td-btn-label">' + (item.UserData.Played ? 'Watched' : 'Unwatched') + '</span>';
            html += '</div>';
        }

        // Favorite toggle
        if (item.UserData) {
            var favClass = item.UserData.IsFavorite ? ' active' : '';
            var favIcon = item.UserData.IsFavorite ? ICONS.heart : ICONS.heartOutline;
            html += '<div class="td-btn-group">';
            html += '<button class="td-btn-circle' + favClass + '" data-action="favorite" title="Favorite">' + favIcon + '</button>';
            html += '<span class="td-btn-label">' + (item.UserData.IsFavorite ? 'Favorited' : 'Favorite') + '</span>';
            html += '</div>';
        }

        // Go to series (for episodes)
        if (item.Type === 'Episode' && item.SeriesId) {
            html += '<button class="td-btn td-btn-play" data-action="series" style="background:rgba(255,255,255,0.12);color:#fff;">' +
                    'Go to Series</button>';
        }

        html += '</div>';
        return html;
    }

    function wireActions(item) {
        var content = TD.overlay.querySelector('.td-content');

        content.querySelectorAll('[data-action]').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                var action = btn.getAttribute('data-action');

                switch (action) {
                    case 'play':
                        playItem(item, 0);
                        break;
                    case 'resume':
                        playItem(item, item.UserData ? item.UserData.PlaybackPositionTicks : 0);
                        break;
                    case 'watched':
                        toggleWatched(item, btn);
                        break;
                    case 'favorite':
                        toggleFavorite(item, btn);
                        break;
                    case 'series':
                        if (item.SeriesId) showDetails(item.SeriesId);
                        break;
                }
            });
        });
    }

    // ── Play Item ──
    function playItem(item, startTicks) {
        closeOverlay();

        // Navigate to Jellyfin's native player
        var hash = '#/details?id=' + item.Id;
        window.location.hash = hash;

        // After a short delay, try to auto-click the play button
        setTimeout(function () {
            var playBtn = document.querySelector('.mainDetailButtons .btnPlay, .detailButton-play, [data-action="resume"], [data-action="play"]');
            if (playBtn) {
                playBtn.click();
            }
        }, 800);
    }

    // ── Toggle Watched ──
    function toggleWatched(item, btn) {
        var isPlayed = btn.classList.contains('active');
        var url = 'Users/' + TD.userId + '/PlayedItems/' + item.Id;
        var label = btn.parentElement.querySelector('.td-btn-label');

        if (isPlayed) {
            TD.apiClient.ajax({ type: 'DELETE', url: TD.apiClient.getUrl(url) }).then(function () {
                btn.classList.remove('active');
                if (label) label.textContent = 'Unwatched';
                item.UserData.Played = false;
            });
        } else {
            TD.apiClient.ajax({ type: 'POST', url: TD.apiClient.getUrl(url) }).then(function () {
                btn.classList.add('active');
                if (label) label.textContent = 'Watched';
                item.UserData.Played = true;
            });
        }
    }

    // ── Toggle Favorite ──
    function toggleFavorite(item, btn) {
        var isFav = btn.classList.contains('active');
        var url = 'Users/' + TD.userId + '/FavoriteItems/' + item.Id;
        var label = btn.parentElement.querySelector('.td-btn-label');

        if (isFav) {
            TD.apiClient.ajax({ type: 'DELETE', url: TD.apiClient.getUrl(url) }).then(function () {
                btn.classList.remove('active');
                btn.innerHTML = ICONS.heartOutline;
                if (label) label.textContent = 'Favorite';
                item.UserData.IsFavorite = false;
            });
        } else {
            TD.apiClient.ajax({ type: 'POST', url: TD.apiClient.getUrl(url) }).then(function () {
                btn.classList.add('active');
                btn.innerHTML = ICONS.heart;
                if (label) label.textContent = 'Favorited';
                item.UserData.IsFavorite = true;
            });
        }
    }

    // ── Fetch Additional Data ──
    function fetchAdditionalData(item) {
        var sections = TD.overlay.querySelector('.td-sections');
        if (!sections) return;

        // Cast
        if (item.People && item.People.length > 0) {
            renderCast(sections, item.People);
        }

        // Series → Seasons
        if (item.Type === 'Series') {
            fetchSeasons(item.Id).then(function (seasons) {
                if (seasons.length > 0) {
                    renderSeasons(sections, seasons);
                }
            });
        }

        // Season or Episode → Episodes
        if (item.Type === 'Season' && item.SeriesId) {
            fetchEpisodes(item.SeriesId, item.Id).then(function (eps) {
                if (eps.length > 0) {
                    renderEpisodes(sections, eps);
                }
            });
        }
        if (item.Type === 'Episode' && item.SeriesId && item.SeasonId) {
            fetchEpisodes(item.SeriesId, item.SeasonId).then(function (eps) {
                if (eps.length > 0) {
                    renderEpisodes(sections, eps, item.Id);
                }
            });
        }

        // Similar items (for movies and series)
        if (item.Type === 'Movie' || item.Type === 'Series') {
            fetchSimilar(item.Id).then(function (similar) {
                if (similar.length > 0) {
                    renderSimilar(sections, similar);
                }
            });
        }
    }

    // ── Render Cast ──
    function renderCast(container, people) {
        var actors = people.filter(function (p) {
            return p.Type === 'Actor';
        }).slice(0, 15);
        if (actors.length === 0) return;

        var section = createSection('Cast');
        var items = section.querySelector('.td-section-items');

        actors.forEach(function (person) {
            var card = document.createElement('div');
            card.className = 'td-cast-card';

            var imgHtml;
            if (person.PrimaryImageTag) {
                var imgUrl = TD.apiClient.getUrl('Items/' + person.Id + '/Images/Primary', {
                    maxHeight: 200, quality: 80, tag: person.PrimaryImageTag
                });
                imgHtml = '<img class="td-cast-img" src="' + attr(imgUrl) + '" loading="lazy" />';
            } else {
                imgHtml = '<div class="td-cast-placeholder">' + ICONS.person + '</div>';
            }

            card.innerHTML = imgHtml +
                '<div class="td-cast-name">' + esc(person.Name || '') + '</div>' +
                '<div class="td-cast-role">' + esc(person.Role || person.Type || '') + '</div>';

            items.appendChild(card);
        });

        container.appendChild(section);
        setupScrollArrows(section);
    }

    // ── Render Seasons ──
    function renderSeasons(container, seasons) {
        var section = createSection('Seasons');
        var items = section.querySelector('.td-section-items');

        seasons.forEach(function (season) {
            var card = document.createElement('div');
            card.className = 'td-card';
            card.style.position = 'relative';

            var imgUrl = getImageUrl(season, 'Primary', 300);
            var imgHtml = imgUrl
                ? '<img class="td-card-poster" src="' + attr(imgUrl) + '" loading="lazy" />'
                : '<div class="td-card-no-img">&#127909;</div>';

            // Unplayed count
            var badge = '';
            if (season.UserData && season.UserData.UnplayedItemCount > 0) {
                badge = '<div class="td-unplayed-count" style="position:absolute;top:6px;right:6px;">' +
                        season.UserData.UnplayedItemCount + '</div>';
            }

            card.innerHTML = imgHtml + badge +
                '<div class="td-card-title">' + esc(season.Name || '') + '</div>';

            card.addEventListener('click', function (e) {
                e.stopPropagation();
                showDetails(season.Id);
            });
            items.appendChild(card);
        });

        container.appendChild(section);
        setupScrollArrows(section);
    }

    // ── Render Episodes ──
    function renderEpisodes(container, episodes, currentEpId) {
        var section = createSection('Episodes');
        var items = section.querySelector('.td-section-items');

        episodes.forEach(function (ep) {
            var card = document.createElement('div');
            card.className = 'td-episode-card';

            var thumbUrl = getImageUrl(ep, 'Primary', 400) || getImageUrl(ep, 'Thumb', 400);
            var thumbHtml;
            if (thumbUrl) {
                thumbHtml = '<div style="position:relative;">' +
                    '<img class="td-episode-thumb" src="' + attr(thumbUrl) + '" loading="lazy" />' +
                    (ep.IndexNumber ? '<span class="td-episode-num">E' + ep.IndexNumber + '</span>' : '') +
                    '</div>';
            } else {
                thumbHtml = '<div style="position:relative;">' +
                    '<div class="td-episode-no-thumb">&#9654;</div>' +
                    (ep.IndexNumber ? '<span class="td-episode-num">E' + ep.IndexNumber + '</span>' : '') +
                    '</div>';
            }

            var progressHtml = '';
            if (ep.UserData && ep.UserData.PlayedPercentage > 0 && ep.UserData.PlayedPercentage < 100) {
                progressHtml = '<div class="td-episode-progress"><div class="td-episode-progress-bar" style="width:' +
                    Math.min(ep.UserData.PlayedPercentage, 100) + '%"></div></div>';
            }

            var metaText = '';
            if (ep.Overview) {
                metaText = esc(ep.Overview.length > 120 ? ep.Overview.substring(0, 120) + '...' : ep.Overview);
            }

            card.innerHTML = thumbHtml +
                '<div class="td-episode-title">' + esc(ep.Name || '') + '</div>' +
                '<div class="td-episode-meta">' + metaText + '</div>' +
                progressHtml;

            if (currentEpId && ep.Id === currentEpId) {
                card.style.outline = '2px solid #00a4dc';
                card.style.borderRadius = '8px';
            }

            card.addEventListener('click', function (e) {
                e.stopPropagation();
                showDetails(ep.Id);
            });
            items.appendChild(card);
        });

        container.appendChild(section);
        setupScrollArrows(section);

        // Scroll current episode into view
        if (currentEpId) {
            setTimeout(function () {
                var current = items.querySelector('[style*="outline"]');
                if (current) {
                    current.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
                }
            }, 100);
        }
    }

    // ── Render Similar ──
    function renderSimilar(container, similar) {
        var section = createSection('More Like This');
        var items = section.querySelector('.td-section-items');

        similar.forEach(function (item) {
            var card = document.createElement('div');
            card.className = 'td-card';

            var imgUrl = getImageUrl(item, 'Primary', 300);
            var imgHtml = imgUrl
                ? '<img class="td-card-poster" src="' + attr(imgUrl) + '" loading="lazy" />'
                : '<div class="td-card-no-img">&#127909;</div>';

            var meta = [];
            if (item.ProductionYear) meta.push(item.ProductionYear);
            if (item.CommunityRating) meta.push('&#9733; ' + item.CommunityRating.toFixed(1));

            card.innerHTML = imgHtml +
                '<div class="td-card-title">' + esc(item.Name || '') + '</div>' +
                '<div class="td-card-meta">' + meta.join(' &middot; ') + '</div>';

            card.addEventListener('click', function (e) {
                e.stopPropagation();
                showDetails(item.Id);
            });
            items.appendChild(card);
        });

        container.appendChild(section);
        setupScrollArrows(section);
    }

    // ── Section Helper ──
    function createSection(title) {
        var section = document.createElement('div');
        section.className = 'td-section';
        section.innerHTML =
            '<div class="td-section-title">' + esc(title) + '</div>' +
            '<div class="td-section-scroll">' +
            '  <div class="td-scroll-arrow td-scroll-arrow-left">&#10094;</div>' +
            '  <div class="td-section-items"></div>' +
            '  <div class="td-scroll-arrow td-scroll-arrow-right">&#10095;</div>' +
            '</div>';
        return section;
    }

    function setupScrollArrows(section) {
        var items = section.querySelector('.td-section-items');
        var left = section.querySelector('.td-scroll-arrow-left');
        var right = section.querySelector('.td-scroll-arrow-right');

        if (left) {
            left.addEventListener('click', function () {
                items.scrollBy({ left: -items.clientWidth * 0.8, behavior: 'smooth' });
            });
        }
        if (right) {
            right.addEventListener('click', function () {
                items.scrollBy({ left: items.clientWidth * 0.8, behavior: 'smooth' });
            });
        }
    }

    // ── Image URL Helper ──
    function getImageUrl(item, imageType, maxWidth) {
        if (!item) return null;

        // Check ImageTags
        if (item.ImageTags && item.ImageTags[imageType]) {
            return TD.apiClient.getUrl('Items/' + item.Id + '/Images/' + imageType, {
                maxWidth: maxWidth, quality: 90, tag: item.ImageTags[imageType]
            });
        }

        // Backdrop from BackdropImageTags
        if (imageType === 'Backdrop' && item.BackdropImageTags && item.BackdropImageTags.length > 0) {
            return TD.apiClient.getUrl('Items/' + item.Id + '/Images/Backdrop/0', {
                maxWidth: maxWidth, quality: 90, tag: item.BackdropImageTags[0]
            });
        }

        // For episodes, try parent images
        if (item.Type === 'Episode') {
            if (imageType === 'Backdrop' && item.ParentBackdropImageTags && item.ParentBackdropImageTags.length > 0) {
                return TD.apiClient.getUrl('Items/' + item.ParentBackdropItemId + '/Images/Backdrop/0', {
                    maxWidth: maxWidth, quality: 90, tag: item.ParentBackdropImageTags[0]
                });
            }
            if (imageType === 'Logo' && item.ParentLogoImageTag && item.ParentLogoItemId) {
                return TD.apiClient.getUrl('Items/' + item.ParentLogoItemId + '/Images/Logo', {
                    maxWidth: maxWidth, quality: 90, tag: item.ParentLogoImageTag
                });
            }
        }

        // For seasons, try series images
        if (item.Type === 'Season') {
            if (imageType === 'Backdrop' && item.ParentBackdropImageTags && item.ParentBackdropImageTags.length > 0) {
                return TD.apiClient.getUrl('Items/' + item.ParentBackdropItemId + '/Images/Backdrop/0', {
                    maxWidth: maxWidth, quality: 90, tag: item.ParentBackdropImageTags[0]
                });
            }
            if (imageType === 'Logo' && item.ParentLogoImageTag && item.ParentLogoItemId) {
                return TD.apiClient.getUrl('Items/' + item.ParentLogoItemId + '/Images/Logo', {
                    maxWidth: maxWidth, quality: 90, tag: item.ParentLogoImageTag
                });
            }
        }

        return null;
    }

    // ── Navigation ──
    function goBack() {
        if (TD.history.length > 0) {
            TD.navigatingBack = true;
            var prevId = TD.history.pop();
            showDetails(prevId);
        } else {
            closeOverlay();
        }
    }

    function closeOverlay() {
        if (!TD.overlay) return;
        TD.overlay.classList.remove('active');
        TD.overlay.removeAttribute('data-current-id');
        TD.history = [];
        document.body.style.overflow = '';
    }

    function isOpen() {
        return TD.overlay && TD.overlay.classList.contains('active');
    }

    // ── Utility ──
    function esc(str) {
        if (!str) return '';
        return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function attr(str) {
        if (!str) return '';
        return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function pad(n) {
        if (n == null) return '0';
        return n < 10 ? '0' + n : String(n);
    }

    // ── Expose for Tentacle Home ──
    window.TentacleDetails = {
        show: showDetails,
        close: closeOverlay,
        isOpen: isOpen
    };

    // ── Start ──
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', waitForReady);
    } else {
        waitForReady();
    }
})();
