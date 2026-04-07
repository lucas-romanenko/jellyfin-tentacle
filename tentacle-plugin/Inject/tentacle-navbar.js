// Tentacle Navbar — custom navigation bar replacing Jellyfin's default header
// Adapted from Moonfin navbar.js — uses window.ApiClient directly, no settings storage
// Integrates with Details overlay (global Details object) and Discover tab (TentacleDiscover)
(function () {
    'use strict';

    // Known user-facing routes — everything else is admin
    var USER_ROUTES = [
        'home', 'home.html', 'movies', 'tv', 'tvshows', 'music', 'livetv',
        'details', 'search', 'favorites', 'list', 'homevideos', 'books',
        'mypreferencesmenu', 'mypreferencesmenudisplay', 'mypreferencesmenusubtitles',
        'mypreferencesmenuhome', 'mypreferencesmenuplayback', 'mypreferencesmenuquickconnect',
        'mypreferencesmenucontrol', 'video', 'queue', 'nowplaying', 'playlists'
    ];

    var Navbar = {
        container: null,
        clockInterval: null,
        initialized: false,
        libraries: [],
        currentUser: null,
        librariesExpanded: false,
        librariesTimeout: null,
        _onViewShow: null,
        _navObserver: null,
        _lastHash: '',

        getFallbackUserIconSvg: function () {
            return '<svg class="moonfin-user-fallback-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="#FFFFFF"><path d="M372-523q-42-42-42-108t42-108q42-42 108-42t108 42q42 42 42 108t-42 108q-42 42-108 42t-108-42ZM160-160v-94q0-38 19-65t49-41q67-30 128.5-45T480-420q62 0 123 15.5T731-360q31 14 50 41t19 65v94H160Zm60-60h520v-34q0-16-9.5-30.5T707-306q-64-31-117-42.5T480-360q-57 0-111 11.5T252-306q-14 7-23 21.5t-9 30.5v34Zm324.5-346.5Q570-592 570-631t-25.5-64.5Q519-721 480-721t-64.5 25.5Q390-670 390-631t25.5 64.5Q441-541 480-541t64.5-25.5ZM480-631Zm0 411Z"/></svg>';
        },

        isMobile: function () {
            return window.innerWidth <= 768;
        },

        isHomePage: function () {
            var h = (location.hash || '').replace('#', '').replace(/^\//, '').split('?')[0].split('.')[0];
            return h === '' || h === 'home';
        },

        isUserPage: function () {
            var hash = (location.hash || '').replace('#', '').replace(/^\//, '');
            if (hash === '' || hash === '/') return true; // empty = home
            var route = hash.toLowerCase().split('?')[0].split('.')[0];
            for (var i = 0; i < USER_ROUTES.length; i++) {
                if (route === USER_ROUTES[i]) return true;
            }
            return false;
        },

        navigateTo: function (path) {
            try {
                if (window.Emby && window.Emby.Page && window.Emby.Page.show) {
                    window.Emby.Page.show(path);
                } else if (window.appRouter && window.appRouter.show) {
                    window.appRouter.show(path);
                } else {
                    window.location.hash = '#' + path;
                }
            } catch (e) {
                console.warn('[Tentacle] Navigation failed:', e);
                window.location.hash = '#' + path;
            }
        },

        init: function () {
            if (this.initialized) return;
            var self = this;

            console.log('[Tentacle] Initializing navbar...');

            this.waitForApi().then(function () {
                self.createNavbar();
                self.loadUserData();
                self.setupEventListeners();
                self.startClock();
                self.initialized = true;
                self.updateActiveState(); // Initial state check
                console.log('[Tentacle] Navbar initialized');
            }).catch(function (e) {
                console.error('[Tentacle] Navbar: Failed to initialize -', e.message);
            });
        },

        waitForApi: function () {
            return new Promise(function (resolve, reject) {
                var attempts = 0;
                var maxAttempts = 100;

                var check = function () {
                    var api = window.ApiClient;
                    if (api && api.getCurrentUserId && api.getCurrentUserId()) {
                        resolve();
                    } else if (attempts >= maxAttempts) {
                        reject(new Error('API timeout'));
                    } else {
                        attempts++;
                        setTimeout(check, 100);
                    }
                };
                check();
            });
        },

        createNavbar: function () {
            var existing = document.querySelector('.moonfin-navbar');
            if (existing) existing.remove();

            // Default pill background — semi-transparent dark
            var overlayColor = 'rgba(0, 0, 0, 0.45)';

            this.container = document.createElement('nav');
            this.container.className = 'moonfin-navbar';
            this.container.innerHTML = [
                '<div class="moonfin-navbar-left">',
                '    <button class="moonfin-user-btn" title="User Menu">',
                '        <div class="moonfin-user-avatar">',
                '            ' + this.getFallbackUserIconSvg(),
                '        </div>',
                '    </button>',
                '    <button class="moonfin-nav-back" title="Back" style="display:none">',
                '        <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>',
                '    </button>',
                '</div>',
                '',
                '<div class="moonfin-navbar-center">',
                '    <div class="moonfin-nav-pill" style="background: ' + overlayColor + '">',
                '',
                '        <button class="moonfin-nav-btn moonfin-expandable-btn moonfin-nav-home" data-action="home" title="Home">',
                '            <svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>',
                '            <span class="moonfin-expand-label">Home</span>',
                '        </button>',
                '',
                '        <button class="moonfin-nav-btn moonfin-expandable-btn moonfin-nav-search" data-action="search" title="Search">',
                '            <svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>',
                '            <span class="moonfin-expand-label">Search</span>',
                '        </button>',
                '',
                '        <button class="moonfin-nav-btn moonfin-expandable-btn moonfin-nav-discover" data-action="discover" title="Discover">',
                '            <svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M12 10.9c-.61 0-1.1.49-1.1 1.1s.49 1.1 1.1 1.1c.61 0 1.1-.49 1.1-1.1s-.49-1.1-1.1-1.1zM12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm2.19 12.19L6 18l3.81-8.19L18 6l-3.81 8.19z"/></svg>',
                '            <span class="moonfin-expand-label">Discover</span>',
                '        </button>',
                '',
                '        <button class="moonfin-nav-btn moonfin-expandable-btn moonfin-nav-activity" data-action="activity" title="Activity">',
                '            <svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M11 7h2v2h-2zm0 4h2v6h-2zm1-9C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z"/></svg>',
                '            <span class="moonfin-expand-label">Activity</span>',
                '            <span class="moonfin-activity-badge hidden">0</span>',
                '        </button>',
                '',
                '        <button class="moonfin-nav-btn moonfin-expandable-btn moonfin-nav-favorites" data-action="favorites" title="Favorites">',
                '            <svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>',
                '            <span class="moonfin-expand-label">Favorites</span>',
                '        </button>',
                '',
                '        <button class="moonfin-nav-btn moonfin-expandable-btn moonfin-nav-cast" data-action="cast" title="Cast">',
                '            <svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M1 18v3h3c0-1.66-1.34-3-3-3m0-4v2c2.76 0 5 2.24 5 5h2c0-3.87-3.13-7-7-7m0-4v2a9 9 0 0 1 9 9h2c0-6.08-4.93-11-11-11m20-7H3c-1.1 0-2 .9-2 2v3h2V5h18v14h-7v2h7c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2"/></svg>',
                '            <span class="moonfin-expand-label">Cast</span>',
                '        </button>',
                '',
                '        <button class="moonfin-nav-btn moonfin-expandable-btn moonfin-nav-syncplay" data-action="syncplay" title="SyncPlay">',
                '            <svg class="moonfin-nav-icon" viewBox="0 -960 960 960"><path d="M0-240v-63q0-43 44-70t116-27q13 0 25 .5t23 2.5q-14 21-21 44t-7 48v65H0Zm240 0v-65q0-32 17.5-58.5T307-410q32-20 76.5-30t96.5-10q53 0 97.5 10t76.5 30q32 20 49 46.5t17 58.5v65H240Zm540 0v-65q0-26-6.5-49T754-397q11-2 22.5-2.5t23.5-.5q72 0 116 26.5t44 70.5v63H780Zm-455-80h311q-10-20-55.5-35T480-370q-55 0-100.5 15T325-320ZM160-440q-33 0-56.5-23.5T80-520q0-34 23.5-57t56.5-23q34 0 57 23t23 57q0 33-23 56.5T160-440Zm640 0q-33 0-56.5-23.5T720-520q0-34 23.5-57t56.5-23q34 0 57 23t23 57q0 33-23 56.5T800-440Zm-320-40q-50 0-85-35t-35-85q0-51 35-85.5t85-34.5q51 0 85.5 34.5T600-600q0 50-34.5 85T480-480Zm0-80q17 0 28.5-11.5T520-600q0-17-11.5-28.5T480-640q-17 0-28.5 11.5T440-600q0 17 11.5 28.5T480-560Zm1 240Zm-1-280Z"/></svg>',
                '            <span class="moonfin-expand-label">SyncPlay</span>',
                '        </button>',
                '',
                '        <div class="moonfin-libraries-group">',
                '            <button class="moonfin-nav-btn moonfin-expandable-btn moonfin-libraries-btn" data-action="libraries-toggle" title="Libraries">',
                '                <svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-8 12.5v-9l6 4.5-6 4.5z"/></svg>',
                '                <span class="moonfin-expand-label">Libraries</span>',
                '            </button>',
                '            <div class="moonfin-libraries-list">',
                '            </div>',
                '        </div>',
                '',
                '    </div>',
                '</div>',
                '',
                '<div class="moonfin-navbar-right">',
                '    <div class="moonfin-clock">',
                '        <span class="moonfin-clock-time">--:--</span>',
                '    </div>',
                '</div>'
            ].join('\n');

            document.body.insertBefore(this.container, document.body.firstChild);
            document.body.classList.add('moonfin-navbar-active');
        },

        loadUserData: function () {
            var self = this;
            var api = window.ApiClient;
            if (!api) return;

            api.getCurrentUser().then(function (user) {
                self.currentUser = user;
                self.updateUserAvatar();
            }).catch(function (e) {
                console.warn('[Tentacle] Failed to load user data:', e);
            });

            var userId = api.getCurrentUserId();
            api.getUserViews(userId).then(function (result) {
                self.libraries = (result && result.Items) || [];
                self.updateLibraries();
            }).catch(function (e) {
                console.warn('[Tentacle] Failed to load libraries:', e);
            });
        },

        updateUserAvatar: function () {
            var avatarContainer = this.container ? this.container.querySelector('.moonfin-user-avatar') : null;
            if (!avatarContainer || !this.currentUser) return;

            var api = window.ApiClient;
            if (this.currentUser.PrimaryImageTag && api) {
                var url = api.getUserImageUrl(this.currentUser.Id, {
                    type: 'Primary',
                    tag: this.currentUser.PrimaryImageTag,
                    height: 88
                });
                if (url) {
                    avatarContainer.innerHTML = '<img src="' + url + '" alt="' + (this.currentUser.Name || '') + '" class="moonfin-user-img">';
                    return;
                }
            }
            avatarContainer.innerHTML = this.getFallbackUserIconSvg();
        },

        updateLibraries: function () {
            var librariesList = this.container ? this.container.querySelector('.moonfin-libraries-list') : null;
            if (!librariesList) return;

            librariesList.innerHTML = this.libraries.map(function (lib) {
                var collectionType = lib.CollectionType || '';
                return '<button class="moonfin-nav-btn moonfin-library-btn" data-action="library" data-library-id="' + lib.Id + '" data-collection-type="' + collectionType + '" title="' + lib.Name + '">' +
                    '<span class="moonfin-library-name">' + lib.Name + '</span>' +
                    '</button>';
            }).join('');
        },

        getLibraryUrl: function (libraryId, collectionType) {
            var type = (collectionType || '').toLowerCase();
            switch (type) {
                case 'movies':
                    return '/movies?topParentId=' + libraryId + '&collectionType=' + collectionType;
                case 'tvshows':
                    return '/tv?topParentId=' + libraryId + '&collectionType=' + collectionType;
                case 'music':
                    return '/music?topParentId=' + libraryId + '&collectionType=' + collectionType;
                case 'livetv':
                    return '/livetv?collectionType=' + collectionType;
                case 'homevideos':
                    return '/homevideos?topParentId=' + libraryId;
                case 'books':
                    return '/list?parentId=' + libraryId;
                default:
                    return '/list?parentId=' + libraryId;
            }
        },

        positionLibrariesDropdown: function () {
            if (this.isMobile()) return;
            var btn = this.container ? this.container.querySelector('.moonfin-libraries-btn') : null;
            var list = this.container ? this.container.querySelector('.moonfin-libraries-list') : null;
            if (!btn || !list) return;

            var rect = btn.getBoundingClientRect();
            list.style.top = (rect.bottom + 8) + 'px';
            list.style.left = rect.left + 'px';

            var pill = this.container.querySelector('.moonfin-nav-pill');
            if (pill) {
                list.style.background = pill.style.background;
            }
        },

        toggleLibraries: function () {
            var group = this.container ? this.container.querySelector('.moonfin-libraries-group') : null;
            if (!group) return;

            this.librariesExpanded = !this.librariesExpanded;
            group.classList.toggle('expanded', this.librariesExpanded);

            if (this.librariesExpanded) {
                this.positionLibrariesDropdown();
            }

            if (this.isMobile() && this.librariesExpanded) {
                var self = this;
                setTimeout(function () {
                    group.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' });
                }, 50);
            }
        },

        collapseLibraries: function () {
            if (this.isMobile()) return;

            var self = this;
            if (this.librariesTimeout) {
                clearTimeout(this.librariesTimeout);
            }
            this.librariesTimeout = setTimeout(function () {
                self.librariesExpanded = false;
                var group = self.container ? self.container.querySelector('.moonfin-libraries-group') : null;
                if (group) group.classList.remove('expanded');
            }, 150);
        },

        cancelCollapseLibraries: function () {
            if (this.librariesTimeout) {
                clearTimeout(this.librariesTimeout);
                this.librariesTimeout = null;
            }
        },

        setupEventListeners: function () {
            var self = this;

            // Nav button clicks
            this.container.addEventListener('click', function (e) {
                var btn = e.target.closest('.moonfin-nav-btn');
                if (!btn) return;

                var action = btn.dataset.action;
                if (action === 'libraries-toggle') {
                    self.toggleLibraries();
                    return;
                }
                self.handleNavigation(action, btn);
            });

            // User button → preferences
            var userBtn = this.container.querySelector('.moonfin-user-btn');
            if (userBtn) {
                userBtn.addEventListener('click', function () {
                    if (typeof Details !== 'undefined' && Details.isVisible) Details.hide(true);
                    self.navigateTo('/mypreferencesmenu');
                });
            }

            // Back button — works for details overlay or general navigation
            var navBack = this.container.querySelector('.moonfin-nav-back');
            if (navBack) {
                navBack.addEventListener('click', function () {
                    if (typeof Details !== 'undefined' && Details.isVisible) {
                        Details.goBack();
                    } else {
                        history.back();
                    }
                });
            }

            // Libraries dropdown hover/focus behavior
            var librariesGroup = this.container.querySelector('.moonfin-libraries-group');
            if (librariesGroup) {
                librariesGroup.addEventListener('mouseenter', function () {
                    if (!self.isMobile()) {
                        self.cancelCollapseLibraries();
                        self.librariesExpanded = true;
                        librariesGroup.classList.add('expanded');
                        self.positionLibrariesDropdown();
                    }
                });
                librariesGroup.addEventListener('mouseleave', function () {
                    if (!self.isMobile()) self.collapseLibraries();
                });
                librariesGroup.addEventListener('focusin', function () {
                    if (!self.isMobile()) {
                        self.cancelCollapseLibraries();
                        self.librariesExpanded = true;
                        librariesGroup.classList.add('expanded');
                        self.positionLibrariesDropdown();
                    }
                });
                librariesGroup.addEventListener('focusout', function (e) {
                    if (self.isMobile()) return;
                    if (e.relatedTarget && librariesGroup.contains(e.relatedTarget)) return;
                    self.collapseLibraries();
                });

                var librariesList = librariesGroup.querySelector('.moonfin-libraries-list');
                if (librariesList) {
                    librariesList.addEventListener('mouseenter', function () {
                        if (!self.isMobile()) self.cancelCollapseLibraries();
                    });
                    librariesList.addEventListener('mouseleave', function () {
                        if (!self.isMobile()) self.collapseLibraries();
                    });
                }
            }

            // Track active view — multiple detection methods for Jellyfin SPA
            this._onViewShow = function () {
                self.updateActiveState();
            };
            window.addEventListener('viewshow', this._onViewShow);
            window.addEventListener('hashchange', this._onViewShow);
            window.addEventListener('popstate', this._onViewShow);

            // Observe DOM changes as fallback — Jellyfin SPA doesn't always fire events
            this._lastHash = location.hash;
            this._navObserver = new MutationObserver(function () {
                var currentHash = location.hash;
                if (currentHash !== self._lastHash) {
                    self._lastHash = currentHash;
                    self.updateActiveState();
                }
            });
            this._navObserver.observe(document.body, { childList: true, subtree: false });

            // Listen for activity badge updates from discover module
            window.addEventListener('tentacle-activity-count', function (e) {
                self.updateActivityBadge(e.detail);
            });
        },

        handleNavigation: function (action, btn) {
            // Close details overlay for navigation actions
            if (action !== 'cast' && action !== 'syncplay' && typeof Details !== 'undefined' && Details.isVisible) {
                Details.hide(true);
            }

            switch (action) {
                case 'home':
                    this.navigateTo('/home');
                    break;
                case 'search':
                    this.navigateTo('/search');
                    break;
                case 'discover':
                    this.activateDiscover();
                    break;
                case 'activity':
                    this.activateActivity();
                    break;
                case 'favorites':
                    this.navigateTo('/home?tab=1');
                    break;
                case 'cast':
                    this.showCastMenu();
                    break;
                case 'syncplay':
                    this.showSyncPlayMenu();
                    break;
                case 'library':
                    var libraryId = btn.dataset.libraryId;
                    var collectionType = btn.dataset.collectionType;
                    if (libraryId) {
                        this.navigateTo(this.getLibraryUrl(libraryId, collectionType));
                    }
                    this.librariesExpanded = false;
                    var group = this.container ? this.container.querySelector('.moonfin-libraries-group') : null;
                    if (group) group.classList.remove('expanded');
                    break;
            }
        },

        activateDiscover: function () {
            if (window.TentacleDiscover && window.TentacleDiscover.show) {
                window.TentacleDiscover.show();
                return;
            }
            var self = this;
            var tryClick = function () {
                var tab = document.querySelector('#mdDiscoverTab');
                if (tab) {
                    tab.click();
                } else {
                    var h = location.hash || '';
                    if (h !== '' && h !== '#/' && h !== '#/home.html' && h !== '#/home') {
                        self.navigateTo('/home');
                        setTimeout(tryClick, 500);
                    }
                }
            };
            tryClick();
        },

        activateActivity: function () {
            if (window.TentacleDiscover && window.TentacleDiscover.showActivity) {
                window.TentacleDiscover.showActivity();
                return;
            }
            this.activateDiscover();
        },

        showCastMenu: function () {
            var nativeCastBtn = document.querySelector('.headerCastButton, .castButton');
            if (nativeCastBtn) nativeCastBtn.click();
        },

        showSyncPlayMenu: function () {
            var nativeSyncBtn = document.querySelector('.headerSyncButton, .syncButton');
            if (nativeSyncBtn) nativeSyncBtn.click();
        },

        updateVisibility: function () {
            if (!this.container) return;
            var isUser = this.isUserPage();
            this.container.style.display = isUser ? '' : 'none';
            document.body.classList.toggle('moonfin-navbar-active', isUser);
        },

        updateActiveState: function () {
            if (!this.container) return;

            this.updateVisibility();

            var hash = (location.hash || '').replace('#', '');

            this.container.querySelectorAll('.moonfin-nav-btn').forEach(function (btn) {
                btn.classList.remove('active');
            });

            var isHome = this.isHomePage();

            if (isHome) {
                var homeBtn = this.container.querySelector('.moonfin-nav-home');
                if (homeBtn) homeBtn.classList.add('active');
            } else if (hash.indexOf('/search') !== -1) {
                var searchBtn = this.container.querySelector('.moonfin-nav-search');
                if (searchBtn) searchBtn.classList.add('active');
            }

            // Show back button on non-home pages OR when details overlay is open
            var backBtn = this.container.querySelector('.moonfin-nav-back');
            if (backBtn) {
                var detailsOpen = typeof Details !== 'undefined' && Details.isVisible;
                backBtn.style.display = (isHome && !detailsOpen) ? 'none' : '';
            }

            // Library active state
            var urlParams = new URLSearchParams(window.location.search);
            var parentId = urlParams.get('parentId') || urlParams.get('topParentId');
            if (parentId) {
                var libraryBtn = this.container.querySelector('[data-library-id="' + parentId + '"]');
                if (libraryBtn) libraryBtn.classList.add('active');
            }
        },

        updateActivityBadge: function (count) {
            var badge = this.container ? this.container.querySelector('.moonfin-activity-badge') : null;
            if (!badge) return;
            if (count > 0) {
                badge.textContent = count;
                badge.classList.remove('hidden');
            } else {
                badge.classList.add('hidden');
            }
        },

        startClock: function () {
            var self = this;
            var updateClock = function () {
                var clockElement = self.container ? self.container.querySelector('.moonfin-clock-time') : null;
                if (!clockElement) return;

                var now = new Date();
                var hours = now.getHours();
                var minutes = now.getMinutes();
                var suffix = hours >= 12 ? ' PM' : ' AM';
                hours = hours % 12 || 12;

                clockElement.textContent = hours + ':' + minutes.toString().padStart(2, '0') + suffix;
            };

            updateClock();
            this.clockInterval = setInterval(updateClock, 1000);
        },

        // Called by details overlay to force back button visible
        showBackButton: function (show) {
            var btn = this.container ? this.container.querySelector('.moonfin-nav-back') : null;
            if (btn) btn.style.display = show ? '' : 'none';
        },

        destroy: function () {
            if (this.clockInterval) {
                clearInterval(this.clockInterval);
                this.clockInterval = null;
            }
            if (this.librariesTimeout) {
                clearTimeout(this.librariesTimeout);
                this.librariesTimeout = null;
            }
            if (this.container) {
                this.container.remove();
                this.container = null;
            }
            if (this._onViewShow) {
                window.removeEventListener('viewshow', this._onViewShow);
                window.removeEventListener('hashchange', this._onViewShow);
                window.removeEventListener('popstate', this._onViewShow);
                this._onViewShow = null;
            }
            if (this._navObserver) {
                this._navObserver.disconnect();
                this._navObserver = null;
            }
            document.body.classList.remove('moonfin-navbar-active');
            this.librariesExpanded = false;
            this.initialized = false;
        }
    };

    // Expose globally for other modules (Details overlay, Discover)
    window.TentacleNavbar = Navbar;

    // Boot
    function boot() {
        if (window.ApiClient && window.ApiClient.getCurrentUserId && window.ApiClient.getCurrentUserId()) {
            Navbar.init();
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
