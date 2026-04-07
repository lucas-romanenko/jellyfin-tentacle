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

        isHomePage: function () {
            var h = location.hash || '';
            return h === '' || h === '#/' || h === '#/home.html' || h === '#/home';
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

            var self = this;
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
                '</div>';

            document.body.appendChild(this.container);
        },

        loadContent: function () {
            var self = this;
            var url = this.apiClient.getUrl('TentacleHome/Hero', { userId: this.userId });
            return this.apiClient.getJSON(url).then(function (data) {
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
            var checkInterval = setInterval(function () {
                if (window.YT && window.YT.Player) {
                    clearInterval(checkInterval);
                    self._ytApiReady = true;
                    self._ytApiLoading = false;
                    callback();
                }
            }, 100);
            setTimeout(function () { clearInterval(checkInterval); }, 10000);
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
                        mute: 1,
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
                            event.target.mute();
                            event.target.playVideo();
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
                this.container.classList.remove('disabled', 'hidden');
                if (this.isHomePage() && this.items && this.items.length > 0) {
                    document.body.classList.add('moonfin-mediabar-active');
                }
            }
        },

        hide: function () {
            if (this.container) {
                this.container.classList.add('hidden');
                document.body.classList.remove('moonfin-mediabar-active');
                this.stopTrailer();
            }
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

            // Click to show details
            this.container.addEventListener('click', function (e) {
                if (e.target.closest('.moonfin-mediabar-nav-btn, .moonfin-mediabar-dots, .moonfin-mediabar-dots-wrap')) {
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

            // Hover → show nav arrows
            this.container.addEventListener('mouseenter', function () {
                self.container.classList.add('focused');
            });
            this.container.addEventListener('mouseleave', function () {
                self.container.classList.remove('focused');
            });

            // Tab visibility → pause trailer
            document.addEventListener('visibilitychange', function () {
                self.isVisible = !document.hidden;
                if (document.hidden) self.stopTrailer();
            });

            // Page navigation → show/hide
            var onNavChange = function () {
                if (self.isHomePage()) {
                    self.show();
                } else {
                    self.hide();
                }
            };
            window.addEventListener('hashchange', onNavChange);
            window.addEventListener('viewshow', onNavChange);
        },

        destroy: function () {
            this.stopAutoAdvance();
            this.stopTrailer();
            if (this.container) {
                this.container.remove();
                this.container = null;
            }
            document.body.classList.remove('moonfin-mediabar-active');
            this.initialized = false;
            this.items = [];
            this.currentIndex = 0;
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
