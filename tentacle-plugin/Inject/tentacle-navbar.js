// Tentacle Navbar — custom navigation bar replacing Jellyfin's default header
// Adapted from Moonfin navbar.js — uses window.ApiClient directly, no settings storage
// Integrates with Details overlay (global Details object) and Discover tab (TentacleDiscover)
(function () {
    'use strict';

    // ── Compatibility guard ─────────────────────────────────────────────
    // Jellyfin's "TV" layout (Display → Layout) renders a completely
    // different DOM than desktop/mobile — the Tentacle injections (navbar,
    // home, search, overlays) target the standard DOM and would break under
    // it. The layout setting is stored per-device (localStorage), so the
    // server can't enforce it — but we load on every page, so we can:
    // detect the TV layout, reset it to Desktop once, and reload.
    // sessionStorage-guarded so a failed reset can never cause a reload loop.
    (function layoutGuard() {
        try {
            var check = function () {
                var html = document.documentElement;
                if (!html || !html.classList.contains('layout-tv')) return;
                if (sessionStorage.getItem('tentacleLayoutGuard')) {
                    console.warn('[Tentacle] TV layout is active and could not be reset — Tentacle UI may not render correctly. Set Settings → Display → Layout to Desktop or Auto.');
                    return;
                }
                sessionStorage.setItem('tentacleLayoutGuard', '1');
                console.warn('[Tentacle] TV layout detected — switching to Desktop layout for Tentacle compatibility.');
                try { localStorage.setItem('layout', 'desktop'); } catch (e) { }
                location.reload();
            };
            // Layout classes are applied during app boot — check after it settles
            setTimeout(check, 1500);
        } catch (e) { /* never break the page over a guard */ }
    })();

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
        toolbarConfig: null,

        getFallbackUserIconSvg: function () {
            return '<svg class="moonfin-user-fallback-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="#FFFFFF"><path d="M372-523q-42-42-42-108t42-108q42-42 108-42t108 42q42 42 42 108t-42 108q-42 42-108 42t-108-42ZM160-160v-94q0-38 19-65t49-41q67-30 128.5-45T480-420q62 0 123 15.5T731-360q31 14 50 41t19 65v94H160Zm60-60h520v-34q0-16-9.5-30.5T707-306q-64-31-117-42.5T480-360q-57 0-111 11.5T252-306q-14 7-23 21.5t-9 30.5v34Zm324.5-346.5Q570-592 570-631t-25.5-64.5Q519-721 480-721t-64.5 25.5Q390-670 390-631t25.5 64.5Q441-541 480-541t64.5-25.5ZM480-631Zm0 411Z"/></svg>';
        },

        // ── HTML escaping helpers (library/user names are attacker-influenceable) ──
        esc: function (str) {
            if (str == null) return '';
            return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },

        escAttr: function (str) {
            if (str == null) return '';
            return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },

        isMobile: function () {
            return window.innerWidth <= 768;
        },

        isHomePage: function () {
            var h = (location.hash || '').replace('#', '').replace(/^\//, '').split('?')[0].split('.')[0];
            return h === '' || h === 'home';
        },

        isVideoPage: function () {
            return (location.hash || '').indexOf('#/video') !== -1;
        },

        isUserPage: function () {
            if (this.isVideoPage()) return false;
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
                return self.fetchToolbarConfig();
            }).then(function () {
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

        fetchToolbarConfig: function () {
            var self = this;
            var api = window.ApiClient;
            if (!api) return Promise.resolve();

            var userId = api.getCurrentUserId();
            var serverUrl = '';
            if (typeof api.serverAddress === 'function') serverUrl = api.serverAddress();
            else if (api._serverAddress) serverUrl = api._serverAddress;
            var token = '';
            if (typeof api.accessToken === 'function') token = api.accessToken();
            else if (api._accessToken) token = api._accessToken;

            var url = serverUrl + '/TentacleHome/Toolbar?userId=' + userId;
            if (token) url += '&api_key=' + token;

            return fetch(url).then(function (resp) {
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                return resp.json();
            }).then(function (data) {
                if (data && data.buttons && data.buttons.length > 0) {
                    self.toolbarConfig = data.buttons;
                    console.log('[Tentacle] Toolbar config loaded:', self.toolbarConfig.map(function (b) { return b.id + ':' + b.enabled; }));
                }
            }).catch(function (e) {
                console.warn('[Tentacle] Failed to load toolbar config, using defaults:', e.message);
            });
        },

        _isButtonEnabled: function (id) {
            // No local fallback — Tentacle backend always provides toolbar config
            if (!this.toolbarConfig || this.toolbarConfig.length === 0) return false;
            for (var i = 0; i < this.toolbarConfig.length; i++) {
                if (this.toolbarConfig[i].id === id) return this.toolbarConfig[i].enabled;
            }
            return false; // Unknown button = hide (dashboard didn't include it)
        },

        // Re-fetch toolbar config and rebuild buttons in-place (called on version change)
        refreshToolbar: function () {
            var self = this;
            this.fetchToolbarConfig().then(function () {
                var pill = document.querySelector('.moonfin-nav-pill');
                if (!pill) return;

                // Remove all configurable buttons (keep Home which is always first)
                var btns = pill.querySelectorAll('.moonfin-nav-btn:not(.moonfin-nav-home)');
                for (var i = 0; i < btns.length; i++) btns[i].remove();

                // Rebuild from new config
                var config = self.toolbarConfig;
                var defaultOrder = ['search', 'discover', 'activity', 'favorites', 'libraries'];
                var buttonsHtml = '';

                if (config && config.length > 0) {
                    for (var j = 0; j < config.length; j++) {
                        if (config[j].enabled) buttonsHtml += self._buildButtonHtml(config[j].id);
                    }
                } else {
                    for (var k = 0; k < defaultOrder.length; k++) {
                        buttonsHtml += self._buildButtonHtml(defaultOrder[k]);
                    }
                }

                // Append new buttons after Home
                var temp = document.createElement('div');
                temp.innerHTML = buttonsHtml;
                while (temp.firstChild) pill.appendChild(temp.firstChild);

                self.updateActiveState();
                console.log('[Tentacle] Toolbar refreshed in-place');
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

        _buttonDefs: {
            search: {
                cls: 'moonfin-nav-search',
                action: 'search',
                label: 'Search',
                icon: '<svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>'
            },
            discover: {
                cls: 'moonfin-nav-discover',
                action: 'discover',
                label: 'Discover',
                icon: '<svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M12 10.9c-.61 0-1.1.49-1.1 1.1s.49 1.1 1.1 1.1c.61 0 1.1-.49 1.1-1.1s-.49-1.1-1.1-1.1zM12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm2.19 12.19L6 18l3.81-8.19L18 6l-3.81 8.19z"/></svg>'
            },
            activity: {
                cls: 'moonfin-nav-activity',
                action: 'activity',
                label: 'Activity',
                icon: '<svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M11 7h2v2h-2zm0 4h2v6h-2zm1-9C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z"/></svg>',
                extra: '<span class="moonfin-activity-badge hidden">0</span>'
            },
            favorites: {
                cls: 'moonfin-nav-favorites',
                action: 'favorites',
                label: 'Favorites',
                icon: '<svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>'
            },
            libraries: {
                cls: 'moonfin-libraries-group',
                wrapper: true, // Wrapped in a div, not a plain button
                action: 'libraries-toggle',
                label: 'Libraries',
                icon: '<svg class="moonfin-nav-icon" viewBox="0 0 24 24"><path d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-8 12.5v-9l6 4.5-6 4.5z"/></svg>'
            }
        },

        _buildButtonHtml: function (id) {
            var def = this._buttonDefs[id];
            if (!def) return '';
            if (def.wrapper) {
                return '<div class="' + def.cls + '">' +
                    '<button class="moonfin-nav-btn moonfin-expandable-btn moonfin-libraries-btn" data-action="' + def.action + '" title="' + def.label + '">' +
                    def.icon +
                    '<span class="moonfin-expand-label">' + def.label + '</span>' +
                    '</button></div>';
            }
            return '<button class="moonfin-nav-btn moonfin-expandable-btn ' + def.cls + '" data-action="' + def.action + '" title="' + def.label + '">' +
                def.icon +
                '<span class="moonfin-expand-label">' + def.label + '</span>' +
                (def.extra || '') +
                '</button>';
        },

        createNavbar: function () {
            var existing = document.querySelector('.moonfin-navbar');
            if (existing) existing.remove();

            // Default pill background — semi-transparent dark
            var overlayColor = 'rgba(0, 0, 0, 0.45)';

            // Build configurable buttons in toolbar order
            var buttonsHtml = '';
            var config = this.toolbarConfig;
            var defaultOrder = ['search', 'discover', 'activity', 'favorites', 'libraries'];

            if (config && config.length > 0) {
                for (var i = 0; i < config.length; i++) {
                    if (config[i].enabled) {
                        buttonsHtml += this._buildButtonHtml(config[i].id);
                    }
                }
            } else {
                // No config — show all in default order
                for (var j = 0; j < defaultOrder.length; j++) {
                    buttonsHtml += this._buildButtonHtml(defaultOrder[j]);
                }
            }

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
                buttonsHtml,
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

            // Create libraries dropdown as a direct child of body to avoid
            // backdrop-filter on .moonfin-nav-pill breaking position:fixed
            this.librariesDropdown = document.createElement('div');
            this.librariesDropdown.className = 'moonfin-libraries-list';
            document.body.appendChild(this.librariesDropdown);
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

            this.loadLibraries();
        },

        loadLibraries: function () {
            var self = this;
            var api = window.ApiClient;
            if (!api) return;

            var userId = api.getCurrentUserId();
            if (!userId) {
                console.warn('[Tentacle] No userId for loading libraries');
                return;
            }

            // Try the ApiClient method first, fall back to direct REST call
            var tryApiClient = function () {
                if (typeof api.getUserViews === 'function') {
                    return api.getUserViews(userId).then(function (result) {
                        return (result && result.Items) || [];
                    });
                }
                return Promise.reject(new Error('getUserViews not available'));
            };

            var tryDirectFetch = function () {
                var serverUrl = '';
                if (typeof api.serverAddress === 'function') serverUrl = api.serverAddress();
                else if (api._serverAddress) serverUrl = api._serverAddress;
                var token = '';
                if (typeof api.accessToken === 'function') token = api.accessToken();
                else if (api._accessToken) token = api._accessToken;

                var headers = {};
                if (token) headers['Authorization'] = 'MediaBrowser Token="' + token + '"';

                return fetch(serverUrl + '/Users/' + userId + '/Views', {
                    method: 'GET',
                    headers: headers
                }).then(function (resp) {
                    if (!resp.ok) throw new Error('HTTP ' + resp.status);
                    return resp.json();
                }).then(function (data) {
                    return (data && data.Items) || [];
                });
            };

            tryApiClient().catch(function (e) {
                console.warn('[Tentacle] getUserViews failed, trying direct fetch:', e.message);
                return tryDirectFetch();
            }).then(function (items) {
                self.libraries = items || [];
                console.log('[Tentacle] Loaded ' + self.libraries.length + ' libraries:', self.libraries.map(function (l) { return l.Name; }));
                self.updateLibraries();

                // Hide libraries button if no libraries found
                var group = self.container ? self.container.querySelector('.moonfin-libraries-group') : null;
                if (group) {
                    group.classList.toggle('hidden', self.libraries.length === 0);
                }
            }).catch(function (e) {
                console.error('[Tentacle] Failed to load libraries (all methods):', e);
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
                    avatarContainer.innerHTML = '<img src="' + this.escAttr(url) + '" alt="' + this.escAttr(this.currentUser.Name || '') + '" class="moonfin-user-img">';
                    return;
                }
            }
            avatarContainer.innerHTML = this.getFallbackUserIconSvg();
        },

        updateLibraries: function () {
            if (!this.librariesDropdown) return;

            this.librariesDropdown.innerHTML = this.libraries.map(function (lib) {
                var collectionType = lib.CollectionType || '';
                return '<button class="moonfin-nav-btn moonfin-library-btn" data-action="library" data-library-id="' + Navbar.escAttr(lib.Id) + '" data-collection-type="' + Navbar.escAttr(collectionType) + '" title="' + Navbar.escAttr(lib.Name) + '">' +
                    '<span class="moonfin-library-name">' + Navbar.esc(lib.Name) + '</span>' +
                    '</button>';
            }).join('');
        },

        getLibraryUrl: function (libraryId, collectionType) {
            var type = (collectionType || '').toLowerCase();
            var sid = window.ApiClient && window.ApiClient.serverId ? '&serverId=' + window.ApiClient.serverId() : '';
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
                    return '/list?parentId=' + libraryId + sid;
                default:
                    return '/list?parentId=' + libraryId + sid;
            }
        },

        positionLibrariesDropdown: function () {
            if (this.isMobile()) return;
            var btn = this.container ? this.container.querySelector('.moonfin-libraries-btn') : null;
            if (!btn || !this.librariesDropdown) return;

            var rect = btn.getBoundingClientRect();
            this.librariesDropdown.style.top = (rect.bottom + 8) + 'px';
            this.librariesDropdown.style.left = rect.left + 'px';
        },

        toggleLibraries: function () {
            this.librariesExpanded = !this.librariesExpanded;
            this.applyLibrariesExpanded();

            if (this.librariesExpanded) {
                this.positionLibrariesDropdown();
            }
        },

        applyLibrariesExpanded: function () {
            var group = this.container ? this.container.querySelector('.moonfin-libraries-group') : null;
            if (group) group.classList.toggle('expanded', this.librariesExpanded);
            if (this.librariesDropdown) this.librariesDropdown.classList.toggle('expanded', this.librariesExpanded);
        },

        collapseLibraries: function () {
            if (this.isMobile()) return;

            var self = this;
            if (this.librariesTimeout) {
                clearTimeout(this.librariesTimeout);
            }
            this.librariesTimeout = setTimeout(function () {
                self.librariesExpanded = false;
                self.applyLibrariesExpanded();
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
                        self.applyLibrariesExpanded();
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
                        self.applyLibrariesExpanded();
                        self.positionLibrariesDropdown();
                    }
                });
                librariesGroup.addEventListener('focusout', function (e) {
                    if (self.isMobile()) return;
                    if (e.relatedTarget && librariesGroup.contains(e.relatedTarget)) return;
                    self.collapseLibraries();
                });
            }

            // Dropdown is a separate element on document.body — needs its own hover listeners
            if (this.librariesDropdown) {
                this.librariesDropdown.addEventListener('mouseenter', function () {
                    if (!self.isMobile()) self.cancelCollapseLibraries();
                });
                this.librariesDropdown.addEventListener('mouseleave', function () {
                    if (!self.isMobile()) self.collapseLibraries();
                });
                // Handle clicks on library buttons inside the dropdown
                this.librariesDropdown.addEventListener('click', function (e) {
                    var btn = e.target.closest('.moonfin-nav-btn');
                    if (!btn) return;
                    self.handleNavigation(btn.dataset.action, btn);
                });
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
            // Close overlays when navigating away
            if (action !== 'discover' && action !== 'activity') {
                if (window.TentacleDiscover && window.TentacleDiscover.isActive && window.TentacleDiscover.isActive()) {
                    window.TentacleDiscover.hide();
                }
                if (window.TentacleActivity && window.TentacleActivity.isActive && window.TentacleActivity.isActive()) {
                    window.TentacleActivity.hide();
                }
                // Clear overlay button active state immediately
                var self = this;
                ['moonfin-nav-discover', 'moonfin-nav-activity'].forEach(function (cls) {
                    var b = self.container && self.container.querySelector('.' + cls);
                    if (b) b.classList.remove('active');
                });
            }
            // Close details overlay for navigation actions
            if (typeof Details !== 'undefined' && Details.isVisible) {
                Details.hide(true);
            }

            switch (action) {
                case 'home':
                    // From a home route with params ('?tentacle=...' overlays or
                    // '?tab=1' favorites) the SPA router may no-op (already
                    // "home") and leave the stale param in the URL — force a
                    // clean hash so the route matches reality.
                    if (/^#\/home(\.html)?\?/.test(location.hash || '')) {
                        window.location.hash = '#/home.html';
                    } else {
                        this.navigateTo('/home');
                    }
                    break;
                case 'search':
                    // Hide overlays that may have covered search without changing hash
                    if (window.TentacleDiscover && window.TentacleDiscover.isActive && window.TentacleDiscover.isActive()) window.TentacleDiscover.hide();
                    if (window.TentacleActivity && window.TentacleActivity.isActive && window.TentacleActivity.isActive()) window.TentacleActivity.hide();
                    this.navigateTo('/search');
                    // If hash was already /search, navigateTo is a no-op and no events fire.
                    // Force search to re-initialize if it was hidden by an overlay.
                    if (window.TentacleSearch && !window.TentacleSearch.isActive()) {
                        window.TentacleSearch.activate();
                    }
                    break;
                case 'discover':
                    this.activateDiscover();
                    break;
                case 'activity':
                    this.activateActivity();
                    break;
                case 'favorites':
                    // navigateTo('/home?tab=1') routes via Emby.Page.show, which no-ops
                    // when already on /home (the usual case, since the navbar replaces the
                    // home header), so the Favorites tab never activates. Force a real hash
                    // change so the router re-selects the Favorites tab (index 1 on home).
                    window.location.hash = '#/home.html?tab=1';
                    break;
                case 'library':
                    var libraryId = btn.dataset.libraryId;
                    var collectionType = btn.dataset.collectionType;
                    if (libraryId) {
                        this.navigateTo(this.getLibraryUrl(libraryId, collectionType));
                    }
                    this.librariesExpanded = false;
                    this.applyLibrariesExpanded();
                    break;
            }
        },

        activateDiscover: function () {
            // Route-driven: Discover has a real URL. The discover module opens
            // the overlay on the hashchange, so back/forward and refresh all
            // work, and the overlay covers the home view before it can flash.
            if ((location.hash || '').indexOf('tentacle=discover') !== -1) {
                // Already on the route — hashchange won't fire, re-assert directly
                if (window.TentacleDiscover && window.TentacleDiscover.show) window.TentacleDiscover.show();
            } else {
                window.location.hash = '#/home.html?tentacle=discover';
            }
            this.setOverlayActive('discover');
        },

        activateActivity: function () {
            if ((location.hash || '').indexOf('tentacle=activity') !== -1) {
                if (window.TentacleActivity && window.TentacleActivity.show) window.TentacleActivity.show();
            } else {
                window.location.hash = '#/home.html?tentacle=activity';
            }
            this.setOverlayActive('activity');
        },

        setOverlayActive: function (which) {
            if (!this.container) return;
            this.container.querySelectorAll('.moonfin-nav-btn').forEach(function (btn) {
                btn.classList.remove('active');
            });
            var selectors = {
                discover: '.moonfin-nav-discover',
                activity: '.moonfin-nav-activity',
                favorites: '.moonfin-nav-favorites',
            };
            var btn = this.container.querySelector(selectors[which] || '.moonfin-nav-activity');
            if (btn) btn.classList.add('active');
        },

        updateVisibility: function () {
            if (!this.container) return;
            // Hide completely during video playback (matches Moonfin behavior)
            if (this.isVideoPage()) {
                this.container.classList.add('hidden');
                if (this.librariesDropdown) this.librariesDropdown.style.display = 'none';
                document.body.classList.remove('moonfin-navbar-active');
                document.body.classList.remove('moonfin-mediabar-active');
                document.body.classList.add('tentacle-video-active');
                // Also hide overlays during playback
                if (window.TentacleDiscover && window.TentacleDiscover.isActive && window.TentacleDiscover.isActive()) {
                    window.TentacleDiscover.hide();
                }
                if (window.TentacleActivity && window.TentacleActivity.isActive && window.TentacleActivity.isActive()) {
                    window.TentacleActivity.hide();
                }
                return;
            }
            this.container.classList.remove('hidden');
            document.body.classList.remove('tentacle-video-active');
            if (this.librariesDropdown) this.librariesDropdown.style.display = '';
            var isUser = this.isUserPage();
            this.container.style.display = isUser ? '' : 'none';
            document.body.classList.toggle('moonfin-navbar-active', isUser);

            // Ensure page-specific tabs (Live TV Guide/Channels/Recordings) are visible
            // The CSS hides .skinHeader > *, but tabs may be nested deeper — force ancestors visible via JS
            this.ensurePageTabsVisible();
        },

        ensurePageTabsVisible: function () {
            var self = this;
            this._showPageTabs();
            // Tabs may render after the viewshow event — retry at increasing intervals
            if (this._tabRetryTimer) clearTimeout(this._tabRetryTimer);
            if (this._tabRetryTimer2) clearTimeout(this._tabRetryTimer2);
            this._tabRetryTimer = setTimeout(function () { self._showPageTabs(); }, 300);
            this._tabRetryTimer2 = setTimeout(function () { self._showPageTabs(); }, 1000);

            // Also watch for tabs being added dynamically to the header
            this._watchForTabs();
        },

        _watchForTabs: function () {
            var self = this;
            // Disconnect any previous observer and cancel its pending auto-disconnect
            // timer. Without clearing the old timer, a rapid re-entry would let the
            // stale timeout disconnect the freshly-created observer early.
            if (this._tabObserver) {
                this._tabObserver.disconnect();
                this._tabObserver = null;
            }
            if (this._tabObserverTimer) {
                clearTimeout(this._tabObserverTimer);
                this._tabObserverTimer = null;
            }
            var skinHeader = document.querySelector('.skinHeader');
            if (!skinHeader) return;

            var observer = new MutationObserver(function () {
                self._showPageTabs();
            });
            this._tabObserver = observer;
            observer.observe(skinHeader, { childList: true, subtree: true });

            // Auto-disconnect after 5s to avoid permanent overhead. Only disconnect
            // if this is still the active observer (guards against rapid re-entry).
            this._tabObserverTimer = setTimeout(function () {
                self._tabObserverTimer = null;
                if (self._tabObserver === observer) {
                    observer.disconnect();
                    self._tabObserver = null;
                }
            }, 5000);
        },

        _showPageTabs: function () {
            var skinHeader = document.querySelector('.skinHeader');
            if (!skinHeader) return;

            // Reset any previously forced ancestors
            var prev = skinHeader.querySelectorAll('[data-tentacle-tab-ancestor]');
            for (var i = 0; i < prev.length; i++) {
                prev[i].style.display = '';
                prev[i].removeAttribute('data-tentacle-tab-ancestor');
            }

            // Only show page tabs on pages that actually need them (Live TV, library views)
            // Do NOT show on home page — those "Home/Favorites/Discover" tabs are replaced by Tentacle navbar
            var hash = (location.hash || '').replace('#', '').replace(/^\//, '').split('?')[0].toLowerCase();
            var pagesWithTabs = ['livetv', 'livetv.html', 'music', 'music.html'];
            var needsTabs = false;
            for (var p = 0; p < pagesWithTabs.length; p++) {
                if (hash === pagesWithTabs[p] || hash.indexOf(pagesWithTabs[p]) === 0) {
                    needsTabs = true;
                    break;
                }
            }
            if (!needsTabs) return;

            // Try multiple selectors for page-specific tabs
            var tabs = skinHeader.querySelector('.sectionTabs, .emby-tabs-slider, [data-role="tabscontainer"], .headerTabs');
            if (!tabs) return;

            // Walk up from tabs to skinHeader, forcing all intermediate ancestors visible
            var el = tabs;
            while (el && el !== skinHeader) {
                el.style.display = '';
                el.setAttribute('data-tentacle-tab-ancestor', '');
                el = el.parentElement;
            }
        },

        updateActiveState: function () {
            if (!this.container) return;

            this.updateVisibility();

            // Route-driven overlays: highlight from the URL, so back/forward
            // navigation into an overlay route highlights the right button
            // even without a click.
            var rawHash = location.hash || '';
            if (rawHash.indexOf('tentacle=discover') !== -1) { this.setOverlayActive('discover'); return; }
            if (rawHash.indexOf('tentacle=activity') !== -1) { this.setOverlayActive('activity'); return; }
            // Favorites is home tab=1 — highlight its own button, not Home
            if (/^#\/home(\.html)?\?/.test(rawHash) && /[?&]tab=1\b/.test(rawHash)) {
                this.setOverlayActive('favorites');
                return;
            }

            // Don't override active state if an overlay is open
            var discoverOpen = window.TentacleDiscover && window.TentacleDiscover.isActive && window.TentacleDiscover.isActive();
            var activityOpen = window.TentacleActivity && window.TentacleActivity.isActive && window.TentacleActivity.isActive();
            if (discoverOpen || activityOpen) return;

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
