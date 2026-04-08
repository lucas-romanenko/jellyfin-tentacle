var MdbList = {
    _cache: {},
    _cacheTtlMs: 30 * 60 * 1000,

    init: function() {
        // No event listener needed — config comes from window.TentacleConfig
    },

    // Rating source metadata with icon filenames served from Tentacle/Assets/
    sources: {
        imdb:           { name: 'IMDb',            iconFile: 'imdb.svg',            color: '#F5C518', textColor: '#000' },
        tmdb:           { name: 'TMDb',            iconFile: 'tmdb.svg',            color: '#01D277', textColor: '#fff' },
        trakt:          { name: 'Trakt',           iconFile: 'trakt.svg',           color: '#ED1C24', textColor: '#fff' },
        tomatoes:       { name: 'Rotten Tomatoes', iconFile: 'rt-fresh.svg',        color: '#FA320A', textColor: '#fff' },
        popcorn:        { name: 'RT Audience',     iconFile: 'rt-audience-up.svg',  color: '#FA320A', textColor: '#fff' },
        metacritic:     { name: 'Metacritic',      iconFile: 'metacritic.svg',      color: '#FFCC34', textColor: '#000' },
        metacriticuser: { name: 'Metacritic User', iconFile: 'metacritic-user.svg', color: '#00CE7A', textColor: '#000' },
        letterboxd:     { name: 'Letterboxd',      iconFile: 'letterboxd.svg',      color: '#00E054', textColor: '#fff' },
        rogerebert:     { name: 'RogerEbert',      iconFile: 'rogerebert.svg',      color: '#E50914', textColor: '#fff' },
        myanimelist:    { name: 'MyAnimeList',     iconFile: 'mal.svg',             color: '#2E51A2', textColor: '#fff' },
        anilist:        { name: 'AniList',         iconFile: 'anilist.svg',         color: '#02A9FF', textColor: '#fff' }
    },

    getIconUrl: function(source, rating) {
        var info = this.sources[source];
        if (!info) return '';
        var api = window.ApiClient;
        if (!api) return '';
        var serverUrl = api.serverAddress();

        // Special icon variants based on score
        var score = rating ? rating.score : null;

        // Rotten Tomatoes tomatometer: Certified Fresh >= 75, Fresh >= 60, Rotten < 60
        if (source === 'tomatoes' && score != null && score > 0) {
            if (score >= 75) return serverUrl + '/Tentacle/Assets/rt-certified.svg';
            if (score < 60) return serverUrl + '/Tentacle/Assets/rt-rotten.svg';
        }

        // RT Audience: Verified Hot >= 90, upright popcorn >= 60, spilled < 60
        if (source === 'popcorn' && score != null && score > 0) {
            if (score >= 90) return serverUrl + '/Tentacle/Assets/rt-verified.svg';
            if (score < 60) return serverUrl + '/Tentacle/Assets/rt-audience-down.svg';
        }

        // Metacritic: Must-play/Must-see badge >= 81
        if (source === 'metacritic' && score != null && score >= 81) {
            return serverUrl + '/Tentacle/Assets/metacritic-score.svg';
        }

        return serverUrl + '/Tentacle/Assets/' + info.iconFile;
    },

    isEnabled: function() {
        return window.TentacleConfig && window.TentacleConfig.mdblistEnabled;
    },

    // Returns 'movie' or 'show', or null if unsupported
    getContentType: function(item) {
        if (!item) return null;
        var type = item.Type || item.type;
        if (type === 'Movie') return 'movie';
        if (type === 'Series') return 'show';
        // Episodes and Seasons map to their parent series
        if (type === 'Episode' || type === 'Season') return 'show';
        return null;
    },

    getTmdbId: function(item) {
        if (!item) return null;
        var providerIds = item.ProviderIds || item.providerIds;
        if (!providerIds) return null;
        return providerIds.Tmdb || providerIds.tmdb || null;
    },

    fetchRatings: function(item) {
        if (!this.isEnabled()) return Promise.resolve([]);

        var contentType = this.getContentType(item);
        var tmdbId = this.getTmdbId(item);

        if (!contentType || !tmdbId) return Promise.resolve([]);

        return this.fetchRatingsByTmdb(contentType, tmdbId);
    },

    fetchRatingsByTmdb: function(type, tmdbId) {
        var self = this;
        var cacheKey = type + ':' + tmdbId;

        // Check client cache
        var cached = this._cache[cacheKey];
        if (cached && (Date.now() - cached.fetchedAt) < this._cacheTtlMs) {
            return Promise.resolve(cached.ratings);
        }

        var api = window.ApiClient;
        if (!api) return Promise.resolve([]);

        var serverUrl = api.serverAddress();
        var url = serverUrl + '/Tentacle/MdbList/Ratings?type=' + encodeURIComponent(type) + '&tmdbId=' + encodeURIComponent(tmdbId);

        return fetch(url, {
            method: 'GET',
            headers: {
                'Authorization': 'MediaBrowser Token="' + api.accessToken() + '"'
            }
        }).then(function(response) {
            if (!response.ok) {
                console.warn('[Tentacle] MDBList fetch failed: HTTP ' + response.status);
                return [];
            }
            return response.json();
        }).then(function(resp) {
            if (resp && resp.success && resp.ratings) {
                var ratings = resp.ratings;
                self._cache[cacheKey] = { ratings: ratings, fetchedAt: Date.now() };
                return ratings;
            } else {
                if (resp && resp.error) {
                    console.warn('[Tentacle] MDBList:', resp.error);
                }
                return [];
            }
        }).catch(function(err) {
            console.warn('[Tentacle] MDBList fetch failed:', err);
            return [];
        });
    },

    // MDBList returns `value` (native scale) and `score` (0-100 normalized)
    formatRating: function(rating) {
        if (!rating || !rating.source) return null;
        var source = rating.source.toLowerCase();
        var value = rating.value;
        var score = rating.score;

        if (value == null && score == null) return null;

        // Use native value when available for better display
        switch (source) {
            case 'imdb':
                // IMDb: 0-10 scale
                return value != null ? value.toFixed(1) : (score != null ? (score / 10).toFixed(1) : null);
            case 'tmdb':
                // TMDb: 0-10 scale
                return value != null ? value.toFixed(0) + '%' : (score != null ? score.toFixed(0) + '%' : null);
            case 'tomatoes':
            case 'popcorn':
            case 'metacritic':
            case 'metacriticuser':
                // Percentage-based
                return score != null ? score.toFixed(0) + '%' : (value != null ? value.toFixed(0) + '%' : null);
            case 'letterboxd':
                // Letterboxd: 0-5 scale (value), score is 0-100
                return value != null ? value.toFixed(1) + '/5' : (score != null ? (score / 20).toFixed(1) + '/5' : null);
            case 'trakt':
                // Trakt: percentage
                return score != null ? score.toFixed(0) + '%' : null;
            case 'rogerebert':
                // Roger Ebert: 0-4 scale (value), score is 0-100
                return value != null ? value.toFixed(1) + '/4' : (score != null ? score.toFixed(0) + '%' : null);
            case 'myanimelist':
                // MAL: 0-10 scale
                return value != null ? value.toFixed(1) : (score != null ? (score / 10).toFixed(1) : null);
            case 'anilist':
                // AniList: percentage
                return score != null ? score.toFixed(0) + '%' : null;
            default:
                return score != null ? score.toFixed(0) + '%' : (value != null ? String(value) : null);
        }
    },

    getSourceInfo: function(source) {
        return this.sources[source] || { name: source, icon: source, color: '#666', textColor: '#fff' };
    },

    clearCache: function() {
        this._cache = {};
    },

    buildRatingsHtml: function(ratings, mode) {
        if (!ratings || ratings.length === 0) return '';

        var showNames = true;
        var html = '';

        for (var i = 0; i < ratings.length; i++) {
            var rating = ratings[i];
            if (!rating || !rating.source) continue;

            var source = rating.source.toLowerCase();
            var formatted = this.formatRating(rating);
            if (!formatted) continue;

            var info = this.getSourceInfo(source);
            var iconUrl = this.getIconUrl(source, rating);

            if (mode === 'compact') {
                html += '<span class="moonfin-mdblist-rating-compact">' +
                    '<img class="moonfin-mdblist-icon" src="' + iconUrl + '" alt="' + info.name + '" title="' + info.name + '" loading="lazy">' +
                    '<span class="moonfin-mdblist-value">' + formatted + '</span>' +
                '</span>';
            } else {
                html += '<div class="moonfin-mdblist-rating-full">' +
                    '<img class="moonfin-mdblist-icon-lg" src="' + iconUrl + '" alt="' + info.name + '" title="' + info.name + '" loading="lazy">' +
                    '<div class="moonfin-mdblist-rating-info">' +
                        '<span class="moonfin-mdblist-rating-value">' + formatted + '</span>' +
                        (showNames ? '<span class="moonfin-mdblist-rating-name">' + info.name + '</span>' : '') +
                    '</div>' +
                '</div>';
            }
        }

        return html;
    }
};

// Load TentacleConfig early — mdblist.js is the first injected script.
// Other scripts (tentacle-tmdb.js, tentacle-details.js) depend on window.TentacleConfig.
(function() {
    function loadTentacleConfig() {
        if (!window.ApiClient) {
            setTimeout(loadTentacleConfig, 500);
            return;
        }
        var serverUrl = window.ApiClient.serverAddress();
        var token = window.ApiClient.accessToken();
        fetch(serverUrl + '/Tentacle/Config', {
            headers: { 'Authorization': 'MediaBrowser Token="' + token + '"' }
        }).then(function(r) { return r.json(); }).then(function(cfg) {
            window.TentacleConfig = cfg;
            console.log('[Tentacle] Config loaded early:', cfg.mdblistEnabled ? 'MDBList ON' : 'MDBList OFF', cfg.tmdbEnabled ? 'TMDB ON' : 'TMDB OFF');
        }).catch(function() {
            window.TentacleConfig = { mdblistEnabled: false, tmdbEnabled: false };
        });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', loadTentacleConfig);
    } else {
        loadTentacleConfig();
    }
})();
