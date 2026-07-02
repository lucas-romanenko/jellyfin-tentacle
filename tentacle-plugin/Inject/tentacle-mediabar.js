// Tentacle Media Bar — full-screen hero spotlight with crossfade, trailer preview, and MDBList ratings
// Adapted from Moonfin mediabar.js — uses Tentacle's TentacleHome/Hero API endpoint
// Keeps moonfin- CSS class prefix for compatibility
(function () {
    'use strict';

    var MediaBar = {
        container: null,
        initialized: false,
        items: [],
        currentIndex: 0,
        isPaused: false,
        autoAdvanceTimer: null,
        isVisible: true,
        apiClient: null,
        userId: null,
        generation: 0,     // incremented on navigation, stale API responses check this

        // Trailer state machine
        _trailerState: 'idle', // idle | resolving | playing | unavailable
        _trailerPlayer: null,
        _trailerRevealTimer: null,
        _trailerVideoId: null,
        _sponsorSegments: [],
        _trailerRevealMs: 4000,
        _ytApiReady: false,
        _ytApiLoading: false,
        _crossfadeTimer: null,

        // Default settings (no external settings panel — hardcoded)
        _autoAdvance: true,
        _intervalMs: 8000,
        _trailerPreview: true,
        _overlayColor: 'rgba(0, 0, 0, 0.45)',
        _isMuted: true,       // current mute state (toggled by user)
        _defaultMuted: true,  // from hero config trailerAudio setting

        isHomePage: function () {
            // Normalize the hash the same way tentacle-navbar.js does so home
            // detection is consistent across navbar, home, and mediabar.
            var raw = location.hash || '';
            var h = raw.replace('#', '').replace(/^\//, '').split('?')[0].split('.')[0];
            if (h !== '' && h !== 'home') return false;
            // Home tab (tab 0 / none) shows the media bar; the Favorites tab
            // (tab 1) is native, so step aside there.
            var m = raw.match(/[?&]tab=(\d+)/);
            return !m || m[1] === '0';
        },

        init: function () {
            if (this.initialized) return;

            this.apiClient = window.ApiClient;
            this.userId = this.apiClient ? this.apiClient.getCurrentUserId() : null;
            if (!this.apiClient || !this.userId) return;

            console.log('[Tentacle] MediaBar initializing...');

            this.createMediaBar();
            this.container.classList.add('loading');

            if (this.isHomePage()) {
                document.body.classList.add('moonfin-mediabar-active');
            } else {
                this.container.classList.add('hidden');
            }

            this.setupEventListeners();
            this.initialized = true;

            // Fetch hero config to get trailerAudio default
            var self = this;
            var configUrl = this.apiClient.getUrl('TentacleHome/HeroConfig', { userId: this.userId });
            this.apiClient.getJSON(configUrl).then(function (cfg) {
                if (cfg && cfg.trailerAudio === false) {
                    self._defaultMuted = true;
                    self._isMuted = true;
                } else if (cfg && cfg.trailerAudio === true) {
                    self._defaultMuted = false;
                    self._isMuted = false;
                }
                self._updateMuteButton();
            }).catch(function () {});

            this.loadContent().then(function () {
                self.container.classList.remove('loading');
                if (self.items.length > 0 && self._autoAdvance) {
                    self.startAutoAdvance();
                }
                if (self.items.length === 0) {
                    document.body.classList.remove('moonfin-mediabar-active');
                    self.container.classList.add('empty');
                }
            }).catch(function (e) {
                console.error('[Tentacle] MediaBar: Failed to load -', e.message);
                if (self.container) self.container.classList.remove('loading');
                document.body.classList.remove('moonfin-mediabar-active');
                if (self.container) self.container.classList.add('empty');
            });
        },

        createMediaBar: function () {
            var existing = document.querySelector('.moonfin-mediabar');
            if (existing) existing.remove();

            var oc = this._overlayColor;

            this.container = document.createElement('div');
            this.container.className = 'moonfin-mediabar';
            this.container.innerHTML =
                '<div class="moonfin-mediabar-backdrop">' +
                    '<div class="moonfin-mediabar-backdrop-img moonfin-mediabar-backdrop-current"></div>' +
                    '<div class="moonfin-mediabar-backdrop-img moonfin-mediabar-backdrop-next"></div>' +
                '</div>' +
                '<div class="moonfin-mediabar-trailer-container"></div>' +
                '<div class="moonfin-mediabar-gradient"></div>' +
                '<div class="moonfin-mediabar-content">' +
                    '<div class="moonfin-mediabar-logo-container">' +
                        '<img class="moonfin-mediabar-logo" src="" alt="">' +
                    '</div>' +
                    '<div class="moonfin-mediabar-info" style="background: ' + oc + '">' +
                        '<div class="moonfin-mediabar-metadata">' +
                            '<span class="moonfin-mediabar-year"></span>' +
                            '<span class="moonfin-mediabar-rating-badge"></span>' +
                            '<span class="moonfin-mediabar-runtime"></span>' +
                            '<span class="moonfin-mediabar-genres"></span>' +
                        '</div>' +
                        '<div class="moonfin-mediabar-ratings"></div>' +
                        '<div class="moonfin-mediabar-overview"></div>' +
                    '</div>' +
                '</div>' +
                '<div class="moonfin-mediabar-nav">' +
                    '<button class="moonfin-mediabar-nav-btn moonfin-mediabar-prev" style="background: ' + oc + '">' +
                        '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg>' +
                    '</button>' +
                    '<button class="moonfin-mediabar-nav-btn moonfin-mediabar-next" style="background: ' + oc + '">' +
                        '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M8.59 16.59L10 18l6-6-6-6-1.41 1.41L13.17 12z"/></svg>' +
                    '</button>' +
                '</div>' +
                '<div class="moonfin-mediabar-dots-wrap" style="background: ' + oc + '">' +
                    '<div class="moonfin-mediabar-dots"></div>' +
                '</div>' +
                '<button class="moonfin-mediabar-mute-btn" title="Toggle audio">' +
                    '<svg class="moonfin-mute-icon" viewBox="0 0 24 24" fill="currentColor"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>' +
                    '<svg class="moonfin-unmute-icon" viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>' +
                '</button>';

            // Fixed position hero — append to body, rows scroll over it
            document.body.appendChild(this.container);
        },

        loadContent: function () {
            var self = this;
            var gen = ++this.generation;
            var url = this.apiClient.getUrl('TentacleHome/Hero', { userId: this.userId });
            return this.apiClient.getJSON(url).then(function (data) {
                // Stale check — did user navigate away during the API call?
                if (gen !== self.generation) return;

                self.items = (data && data.Items) || [];
                self.currentIndex = 0;

                if (self.items.length > 0) {
                    self.container.classList.remove('empty');
                    if (self.isHomePage()) {
                        document.body.classList.add('moonfin-mediabar-active');
                    }
                    self.updateDisplay();
                    self.updateDots();
                } else {
                    self.container.classList.add('empty');
                    document.body.classList.remove('moonfin-mediabar-active');
                }
            });
        },

        // ── Image URL helpers ──────────────────────────────────────────────

        getBackdropUrl: function (item) {
            var tag = '';
            if (item.BackdropImageTags && item.BackdropImageTags.length) {
                tag = item.BackdropImageTags[0];
            } else if (item.ImageTags && item.ImageTags.Backdrop) {
                tag = item.ImageTags.Backdrop;
            }
            if (!tag) return null;
            return this.apiClient.getUrl('Items/' + item.Id + '/Images/Backdrop', {
                tag: tag, maxWidth: 1920, quality: 90
            });
        },

        getLogoUrl: function (item) {
            var tag = item.ImageTags && item.ImageTags.Logo;
            if (!tag) return null;
            return this.apiClient.getUrl('Items/' + item.Id + '/Images/Logo', {
                tag: tag, maxWidth: 500, quality: 90
            });
        },

        // ── Display ────────────────────────────────────────────────────────

        updateDisplay: function () {
            var item = this.items[this.currentIndex];
            if (!item) return;

            this.stopTrailer();

            var backdropUrl = this.getBackdropUrl(item);
            this.updateBackdrop(backdropUrl);

            var logoUrl = this.getLogoUrl(item);
            var logoContainer = this.container.querySelector('.moonfin-mediabar-logo-container');
            var logoImg = this.container.querySelector('.moonfin-mediabar-logo');

            if (logoUrl) {
                logoImg.src = logoUrl;
                logoImg.alt = item.Name || '';
                logoContainer.classList.remove('hidden');
            } else {
                logoContainer.classList.add('hidden');
            }

            var yearEl = this.container.querySelector('.moonfin-mediabar-year');
            var ratingBadge = this.container.querySelector('.moonfin-mediabar-rating-badge');
            var runtimeEl = this.container.querySelector('.moonfin-mediabar-runtime');
            var genresEl = this.container.querySelector('.moonfin-mediabar-genres');
            var ratingsEl = this.container.querySelector('.moonfin-mediabar-ratings');
            var overviewEl = this.container.querySelector('.moonfin-mediabar-overview');

            yearEl.textContent = item.ProductionYear || '';

            if (item.OfficialRating) {
                ratingBadge.textContent = item.OfficialRating;
                ratingBadge.classList.remove('hidden');
            } else {
                ratingBadge.textContent = '';
                ratingBadge.classList.add('hidden');
            }

            if (item.RunTimeTicks) {
                var minutes = Math.round(item.RunTimeTicks / 600000000);
                var hours = Math.floor(minutes / 60);
                var mins = minutes % 60;
                runtimeEl.textContent = hours > 0 ? hours + 'h ' + mins + 'm' : mins + 'm';
            } else {
                runtimeEl.textContent = '';
            }

            if (item.Genres && item.Genres.length > 0) {
                genresEl.textContent = item.Genres.slice(0, 3).join(' \u2022 ');
            } else {
                genresEl.textContent = '';
            }

            // Basic ratings (TMDB + critic)
            var ratingParts = [];
            if (item.CommunityRating) {
                ratingParts.push('\u2605 ' + item.CommunityRating.toFixed(1));
            }
            if (item.CriticRating) {
                ratingParts.push('\uD83C\uDF45 ' + item.CriticRating + '%');
            }
            ratingsEl.textContent = ratingParts.join('  \u2022  ');

            // MDBList ratings (if available — tentacle-mdblist.js loads before us)
            if (typeof MdbList !== 'undefined' && MdbList.isEnabled && MdbList.isEnabled()) {
                var currentIdx = this.currentIndex;
                var self = this;
                MdbList.fetchRatings(item).then(function (mdbRatings) {
                    if (self.currentIndex !== currentIdx) return;
                    if (mdbRatings && mdbRatings.length > 0) {
                        var mdbHtml = MdbList.buildRatingsHtml(mdbRatings, 'compact');
                        if (mdbHtml) ratingsEl.innerHTML = mdbHtml;
                    }
                }).catch(function () {});
            }

            // Overview
            if (item.Overview) {
                var tmp = document.createElement('div');
                tmp.innerHTML = item.Overview;
                overviewEl.textContent = tmp.textContent || tmp.innerText || '';
            } else {
                overviewEl.textContent = '';
            }

            this.updateActiveDot();

            // Trailer preview
            if (this._trailerPreview) {
                this.fetchAndPlayTrailer(item, this.currentIndex);
            }
        },

        // ── Backdrop crossfade ─────────────────────────────────────────────

        updateBackdrop: function (url) {
            var current = this.container.querySelector('.moonfin-mediabar-backdrop-current');
            var next = this.container.querySelector('.moonfin-mediabar-backdrop-next');

            if (!url) {
                current.style.backgroundImage = '';
                return;
            }

            if (this._crossfadeTimer) {
                clearTimeout(this._crossfadeTimer);
                this._crossfadeTimer = null;
            }

            var img = new Image();
            var self = this;
            var doSwap = function () {
                next.style.transition = 'none';
                next.classList.remove('active');
                next.style.backgroundImage = "url('" + url + "')";

                void next.offsetWidth; // reflow
                next.style.transition = '';
                next.classList.add('active');

                self._crossfadeTimer = setTimeout(function () {
                    current.style.backgroundImage = "url('" + url + "')";
                    next.style.transition = 'none';
                    next.classList.remove('active');
                    void next.offsetWidth;
                    next.style.transition = '';
                    self._crossfadeTimer = null;
                }, 500);
            };

            img.onload = doSwap;
            img.onerror = doSwap;
            setTimeout(function () {
                if (!img.complete) doSwap();
            }, 300);
            img.src = url;

            this.preloadAdjacent();
        },

        preloadAdjacent: function () {
            if (!this.items || this.items.length < 2) return;
            var nextIdx = (this.currentIndex + 1) % this.items.length;
            var prevIdx = (this.currentIndex - 1 + this.items.length) % this.items.length;
            var nextUrl = this.getBackdropUrl(this.items[nextIdx]);
            var prevUrl = this.getBackdropUrl(this.items[prevIdx]);
            if (nextUrl) { var i1 = new Image(); i1.src = nextUrl; }
            if (prevUrl) { var i2 = new Image(); i2.src = prevUrl; }
        },

        // ── Dots ───────────────────────────────────────────────────────────

        updateDots: function () {
            var dotsContainer = this.container.querySelector('.moonfin-mediabar-dots');
            var html = '';
            for (var i = 0; i < this.items.length; i++) {
                html += '<button class="moonfin-mediabar-dot' + (i === this.currentIndex ? ' active' : '') + '" data-index="' + i + '"></button>';
            }
            dotsContainer.innerHTML = html;
        },

        updateActiveDot: function () {
            var dots = this.container.querySelectorAll('.moonfin-mediabar-dot');
            for (var i = 0; i < dots.length; i++) {
                dots[i].classList.toggle('active', i === this.currentIndex);
            }
        },

        // ── Navigation ─────────────────────────────────────────────────────

        nextSlide: function () {
            this.currentIndex = (this.currentIndex + 1) % this.items.length;
            this.updateDisplay();
            this.resetAutoAdvance();
        },

        prevSlide: function () {
            this.currentIndex = (this.currentIndex - 1 + this.items.length) % this.items.length;
            this.updateDisplay();
            this.resetAutoAdvance();
        },

        goToSlide: function (index) {
            if (index >= 0 && index < this.items.length) {
                this.currentIndex = index;
                this.updateDisplay();
                this.resetAutoAdvance();
            }
        },

        togglePause: function () {
            this.isPaused = !this.isPaused;
            this.container.classList.toggle('paused', this.isPaused);
            if (this.isPaused) {
                this.stopAutoAdvance();
            } else {
                this.startAutoAdvance();
            }
        },

        // ── Auto-advance ───────────────────────────────────────────────────

        startAutoAdvance: function () {
            if (!this._autoAdvance) return;
            if (this.autoAdvanceTimer) clearInterval(this.autoAdvanceTimer);
            var self = this;
            this.autoAdvanceTimer = setInterval(function () {
                if (!self.isPaused && self.isVisible && self._trailerState === 'idle') {
                    self.nextSlide();
                }
            }, this._intervalMs);
        },

        stopAutoAdvance: function () {
            if (this.autoAdvanceTimer) {
                clearInterval(this.autoAdvanceTimer);
                this.autoAdvanceTimer = null;
            }
        },

        resetAutoAdvance: function () {
            this.stopAutoAdvance();
            if (!this.isPaused) this.startAutoAdvance();
        },

        // ── Trailer preview (YouTube + SponsorBlock) ───────────────────────

        fetchAndPlayTrailer: function (item, expectedIndex) {
            var self = this;
            if (item.RemoteTrailers) {
                var videoId = this.extractYouTubeId(item.RemoteTrailers);
                if (videoId && this.currentIndex === expectedIndex) {
                    this.startTrailerPreview(videoId);
                }
                return;
            }

            // Fetch trailers from Jellyfin API
            var url = this.apiClient.getUrl('Users/' + this.userId + '/Items/' + item.Id, {
                Fields: 'RemoteTrailers'
            });
            this.apiClient.getJSON(url).then(function (data) {
                if (self.currentIndex !== expectedIndex) return;
                item.RemoteTrailers = data.RemoteTrailers || [];
                var videoId = self.extractYouTubeId(item.RemoteTrailers);
                if (videoId) self.startTrailerPreview(videoId);
            }).catch(function () {});
        },

        extractYouTubeId: function (trailers) {
            if (!trailers || trailers.length === 0) return null;
            for (var i = 0; i < trailers.length; i++) {
                var url = trailers[i].Url || trailers[i].url || '';
                var match = url.match(/(?:youtube\.com\/(?:watch\?v=|embed\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
                if (match) return match[1];
            }
            return null;
        },

        startTrailerPreview: function (videoId) {
            var self = this;
            this._trailerState = 'resolving';
            this._trailerVideoId = videoId;

            this._ensureYTApi(function () {
                if (self._trailerState !== 'resolving' || self._trailerVideoId !== videoId) return;
                self.fetchSponsorSegments(videoId).then(function (segments) {
                    self._sponsorSegments = segments;
                    self._loadYTPlayer(videoId);
                }).catch(function () {
                    self._sponsorSegments = [];
                    self._loadYTPlayer(videoId);
                });
            });
        },

        _ensureYTApi: function (callback) {
            if (this._ytApiReady && window.YT && window.YT.Player) {
                callback();
                return;
            }
            var self = this;
            if (!this._ytApiLoading) {
                this._ytApiLoading = true;
                var tag = document.createElement('script');
                tag.src = 'https://www.youtube.com/iframe_api';
                document.head.appendChild(tag);
            }
            var resolved = false;
            var checkInterval = setInterval(function () {
                if (window.YT && window.YT.Player) {
                    resolved = true;
                    clearInterval(checkInterval);
                    self._ytApiReady = true;
                    self._ytApiLoading = false;
                    callback();
                }
            }, 100);
            setTimeout(function () {
                clearInterval(checkInterval);
                if (resolved) return;
                // YT IFrame API never loaded — don't leave the trailer stuck in
                // 'resolving', which would permanently block auto-advance.
                self._ytApiLoading = false;
                if (self._trailerState === 'resolving') {
                    self._trailerState = 'idle';
                    if (self._autoAdvance && !self.isPaused && self.isVisible) self.resetAutoAdvance();
                }
            }, 10000);
        },

        _loadYTPlayer: function (videoId) {
            if (this._trailerState !== 'resolving') return;

            var self = this;
            var startTime = this.getTrailerStartTime(this._sponsorSegments);
            var trailerContainer = this.container.querySelector('.moonfin-mediabar-trailer-container');

            if (this._trailerPlayer) {
                try { this._trailerPlayer.destroy(); } catch (e) {}
                this._trailerPlayer = null;
            }

            var playerDiv = document.createElement('div');
            playerDiv.id = 'moonfin-yt-player-' + Date.now();
            playerDiv.className = 'moonfin-mediabar-trailer-iframe';
            trailerContainer.innerHTML = '';
            trailerContainer.appendChild(playerDiv);

            this._trailerState = 'playing';
            this.stopAutoAdvance();

            try {
                this._trailerPlayer = new YT.Player(playerDiv.id, {
                    videoId: videoId,
                    playerVars: {
                        autoplay: 1,
                        mute: self._isMuted ? 1 : 0,
                        controls: 0,
                        start: Math.floor(startTime),
                        rel: 0,
                        modestbranding: 1,
                        playsinline: 1,
                        showinfo: 0,
                        iv_load_policy: 3,
                        disablekb: 1,
                        fs: 0,
                        origin: window.location.origin
                    },
                    events: {
                        onReady: function (event) {
                            if (self._isMuted) {
                                event.target.mute();
                            } else {
                                event.target.unMute();
                                event.target.setVolume(100);
                            }
                            event.target.playVideo();
                            self._showMuteButton();
                            self._trailerRevealTimer = setTimeout(function () {
                                if (self._trailerState === 'playing') {
                                    var iframe = trailerContainer.querySelector('iframe');
                                    if (iframe) iframe.classList.add('visible');
                                    self.container.classList.add('trailer-active');
                                }
                            }, self._trailerRevealMs);
                        },
                        onStateChange: function (event) {
                            if (event.data === 0) self.stopTrailer();
                        },
                        onError: function () {
                            self._trailerState = 'unavailable';
                            self.stopTrailer();
                        }
                    }
                });
            } catch (e) {
                console.warn('[Tentacle] MediaBar: Failed to create YouTube player:', e);
                this._trailerState = 'unavailable';
            }
        },

        fetchSponsorSegments: function (videoId) {
            return new Promise(function (resolve) {
                var url = 'https://sponsor.ajay.app/api/skipSegments?videoID=' + videoId +
                    '&categories=["sponsor","selfpromo","intro","outro","interaction","music_offtopic"]';

                fetch(url).then(function (resp) {
                    if (!resp.ok) { resolve([]); return; }
                    return resp.json();
                }).then(function (data) {
                    if (!Array.isArray(data)) { resolve([]); return; }
                    var segments = [];
                    for (var i = 0; i < data.length; i++) {
                        if (data[i].segment && data[i].segment.length === 2) {
                            segments.push({ start: data[i].segment[0], end: data[i].segment[1] });
                        }
                    }
                    resolve(segments);
                }).catch(function () {
                    resolve([]);
                });
            });
        },

        getTrailerStartTime: function (segments) {
            var startTime = 0;
            if (!segments || segments.length === 0) return startTime;

            var sorted = segments.slice().sort(function (a, b) { return a.start - b.start; });
            for (var i = 0; i < sorted.length; i++) {
                if (sorted[i].start <= startTime + 1) {
                    startTime = Math.max(startTime, sorted[i].end);
                }
            }
            return Math.max(startTime, 5);
        },

        stopTrailer: function () {
            if (this._trailerRevealTimer) {
                clearTimeout(this._trailerRevealTimer);
                this._trailerRevealTimer = null;
            }

            this._hideMuteButton();
            if (this.container) this.container.classList.remove('trailer-active');

            if (this._trailerPlayer) {
                try { this._trailerPlayer.destroy(); } catch (e) {}
                this._trailerPlayer = null;
            }

            var trailerContainer = this.container ? this.container.querySelector('.moonfin-mediabar-trailer-container') : null;
            if (trailerContainer) trailerContainer.innerHTML = '';

            this._trailerState = 'idle';
            this._trailerVideoId = null;
            this._sponsorSegments = [];

            if (!this.isPaused && this._autoAdvance && !this.autoAdvanceTimer) {
                this.startAutoAdvance();
            }
        },

        // ── Show/Hide ──────────────────────────────────────────────────────

        show: function () {
            if (this.container) {
                var self = this;
                var wasDetached = !this.container.parentElement;
                // Re-attach to body if removed by SPA navigation
                if (wasDetached) {
                    document.body.appendChild(this.container);
                }
                this.container.classList.remove('disabled', 'hidden', 'scrolled-partial', 'scrolled-full');

                // Detect in-app user switch: if the current Jellyfin user changed
                // since init, reload so we don't show the previous user's hero.
                var userChanged = false;
                if (this.apiClient) {
                    var currentUserId = this.apiClient.getCurrentUserId();
                    if (currentUserId && currentUserId !== this.userId) {
                        this.userId = currentUserId;
                        userChanged = true;
                    }
                }

                if (this.isHomePage() && this.items && this.items.length > 0) {
                    document.body.classList.add('moonfin-mediabar-active');
                    // Scroll to top so hero is fully visible
                    window.scrollTo(0, 0);
                }
                // If container was detached/re-attached or the user switched,
                // reload content to avoid showing stale or another user's hero
                if ((wasDetached || userChanged) && this.apiClient) {
                    this.loadContent().then(function () {
                        if (self.items.length > 0 && self._autoAdvance) {
                            self.resetAutoAdvance();
                        }
                    }).catch(function () {});
                } else if (this.items && this.items.length > 0 && this._autoAdvance) {
                    // Restart the timer stopped by hide()
                    this.resetAutoAdvance();
                }
            }
        },

        hide: function () {
            this.generation++; // cancel any in-flight API calls
            if (this.container) {
                this.container.classList.add('hidden');
                document.body.classList.remove('moonfin-mediabar-active');
                this.stopTrailer();
                this.stopAutoAdvance(); // don't keep advancing after leaving home
            }
        },

        // ── Mute/unmute ──────────────────────────────────────────────────

        _updateMuteButton: function () {
            var btn = this.container ? this.container.querySelector('.moonfin-mediabar-mute-btn') : null;
            if (!btn) return;
            var muteIcon = btn.querySelector('.moonfin-mute-icon');
            var unmuteIcon = btn.querySelector('.moonfin-unmute-icon');
            if (muteIcon) muteIcon.style.display = this._isMuted ? 'block' : 'none';
            if (unmuteIcon) unmuteIcon.style.display = this._isMuted ? 'none' : 'block';
        },

        _showMuteButton: function () {
            var btn = this.container ? this.container.querySelector('.moonfin-mediabar-mute-btn') : null;
            if (btn) btn.classList.add('active');
            this._updateMuteButton();
        },

        _hideMuteButton: function () {
            var btn = this.container ? this.container.querySelector('.moonfin-mediabar-mute-btn') : null;
            if (btn) btn.classList.remove('active');
        },

        _applyMuteState: function () {
            if (!this._trailerPlayer) return;
            try {
                if (this._isMuted) {
                    this._trailerPlayer.mute();
                } else {
                    this._trailerPlayer.unMute();
                    this._trailerPlayer.setVolume(100);
                }
            } catch (e) {}
        },

        // ── Event listeners ────────────────────────────────────────────────

        setupEventListeners: function () {
            var self = this;

            // Prev/Next buttons
            this.container.querySelector('.moonfin-mediabar-prev').addEventListener('click', function (e) {
                e.stopPropagation();
                self.prevSlide();
            });

            this.container.querySelector('.moonfin-mediabar-next').addEventListener('click', function (e) {
                e.stopPropagation();
                self.nextSlide();
            });

            // Dots
            this.container.querySelector('.moonfin-mediabar-dots').addEventListener('click', function (e) {
                e.stopPropagation();
                var dot = e.target.closest('.moonfin-mediabar-dot');
                if (dot) self.goToSlide(parseInt(dot.dataset.index, 10));
            });

            // Mute/unmute button
            this.container.querySelector('.moonfin-mediabar-mute-btn').addEventListener('click', function (e) {
                e.stopPropagation();
                self._isMuted = !self._isMuted;
                self._applyMuteState();
                self._updateMuteButton();
            });

            // Click to show details
            this.container.addEventListener('click', function (e) {
                if (e.target.closest('.moonfin-mediabar-nav-btn, .moonfin-mediabar-dots, .moonfin-mediabar-dots-wrap, .moonfin-mediabar-mute-btn')) {
                    return;
                }
                var item = self.items[self.currentIndex];
                if (item) {
                    if (typeof Details !== 'undefined' && Details.showDetails) {
                        Details.showDetails(item.Id, item.Type);
                    } else if (window.TentacleDetails && window.TentacleDetails.show) {
                        window.TentacleDetails.show(item.Id, item.Type);
                    } else {
                        window.location.hash = '#/details?id=' + item.Id;
                    }
                }
            });

            // Touch swipe
            var touchStartX = 0;
            var touchStartY = 0;
            var touchMoved = false;

            this.container.addEventListener('touchstart', function (e) {
                var touch = e.touches[0];
                touchStartX = touch.clientX;
                touchStartY = touch.clientY;
                touchMoved = false;
            }, { passive: true });

            this.container.addEventListener('touchmove', function (e) {
                if (!touchStartX) return;
                var dx = Math.abs(e.touches[0].clientX - touchStartX);
                var dy = Math.abs(e.touches[0].clientY - touchStartY);
                if (dx > 10 || dy > 10) touchMoved = true;
                if (dx > dy && dx > 10) e.preventDefault();
            }, { passive: false });

            this.container.addEventListener('touchend', function (e) {
                if (!touchMoved) { touchStartX = 0; return; }
                var dx = e.changedTouches[0].clientX - touchStartX;
                if (Math.abs(dx) >= 50) {
                    if (dx < 0) self.nextSlide();
                    else self.prevSlide();
                }
                touchStartX = 0;
                touchMoved = false;
            }, { passive: true });

            // Keyboard
            this.container.addEventListener('keydown', function (e) {
                switch (e.key) {
                    case 'ArrowLeft': self.prevSlide(); e.preventDefault(); break;
                    case 'ArrowRight': self.nextSlide(); e.preventDefault(); break;
                    case ' ': self.togglePause(); e.preventDefault(); break;
                    case 'Enter':
                        var item = self.items[self.currentIndex];
                        if (item) {
                            if (typeof Details !== 'undefined' && Details.showDetails) {
                                Details.showDetails(item.Id, item.Type);
                            } else {
                                window.location.hash = '#/details?id=' + item.Id;
                            }
                        }
                        e.preventDefault();
                        break;
                }
            });

            // Hover → show nav arrows + mute button (gradient covers full hero area)
            var hoverEls = this.container.querySelectorAll('.moonfin-mediabar-content, .moonfin-mediabar-gradient');
            hoverEls.forEach(function (el) {
                el.addEventListener('mouseenter', function () {
                    self.container.classList.add('focused');
                });
                el.addEventListener('mouseleave', function () {
                    self.container.classList.remove('focused');
                });
            });

            // Tab visibility → pause trailer
            document.addEventListener('visibilitychange', function () {
                self.isVisible = !document.hidden;
                if (document.hidden) self.stopTrailer();
            });

            // Scroll-linked hero fade — Netflix-style parallax
            this._onScroll = function () {
                if (!self.container || !self.isHomePage()) return;
                var scrollY = window.scrollY || window.pageYOffset;
                var heroH = window.innerHeight * 0.6;

                // Fade out hero content as user scrolls
                if (scrollY > heroH) {
                    self.container.classList.add('scrolled-full');
                    self.container.classList.remove('scrolled-partial');
                } else if (scrollY > heroH * 0.3) {
                    self.container.classList.add('scrolled-partial');
                    self.container.classList.remove('scrolled-full');
                } else {
                    self.container.classList.remove('scrolled-partial', 'scrolled-full');
                }
            };
            window.addEventListener('scroll', this._onScroll, { passive: true });

            // Page navigation → show/hide
            // Primary: viewshow — Jellyfin's own SPA navigation event (most reliable)
            this._onViewShow = function (e) {
                var type = e.detail && e.detail.type;
                // Favorites tab (tab=1) is native — suppress the type==='home'
                // fast-path there so the media bar hides on Favorites.
                var favTab = /[?&]tab=[1-9]/.test(location.hash || '');
                if ((type === 'home' && !favTab) || self.isHomePage()) {
                    self.show();
                } else {
                    self.hide();
                }
            };
            document.addEventListener('viewshow', this._onViewShow);

            // Fallback: hashchange for edge cases (browser back/forward)
            this._onNavChange = function () {
                if (self.isHomePage()) {
                    self.show();
                } else {
                    self.hide();
                }
            };
            window.addEventListener('hashchange', this._onNavChange);
        }
    };

    // Expose globally for integration with home.js
    window.TentacleMediaBar = MediaBar;

    // Boot when API is ready
    function boot() {
        if (window.ApiClient && window.ApiClient.getCurrentUserId && window.ApiClient.getCurrentUserId()) {
            MediaBar.init();
        } else {
            setTimeout(boot, 500);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
