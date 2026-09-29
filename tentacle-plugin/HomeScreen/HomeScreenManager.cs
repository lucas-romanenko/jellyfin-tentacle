using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.HomeScreen;

/// <summary>
/// What one home-config read gave: the config (null when the user has none), and
/// whether the read failed (timeout, refused, non-2xx, unreadable body). A failed
/// read is not "no home": clients keep what they show instead of tearing it down.
/// </summary>
public readonly record struct HomeConfigResult(HomeConfig? Config, bool Failed);

/// <summary>
/// Fetches and caches home configuration from the Tentacle API.
/// </summary>
public class HomeScreenManager
{
    private readonly ILogger<HomeScreenManager> _logger;
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly object _cacheLock = new();
    private readonly Dictionary<string, (HomeConfigResult Value, DateTime Expiry)> _userCache = new();
    private static readonly TimeSpan CacheDuration = TimeSpan.FromSeconds(5);
    // One fetch per user and token at a time, shared by every request that asks
    // meanwhile. A home page load asks for the same config from several endpoints
    // at once. They await the running fetch: waiting on the backend never holds a
    // Jellyfin thread. The blocking fetch behind a per-user lock this replaces
    // starved Jellyfin's thread pool on a burst of home loads (#256). Guarded by
    // _cacheLock.
    private readonly Dictionary<string, Task<HomeConfigResult>> _inflight = new();
    private const int PruneAbove = 64;

    public HomeScreenManager(ILogger<HomeScreenManager> logger, IHttpClientFactory httpClientFactory)
    {
        _logger = logger;
        _httpClientFactory = httpClientFactory;
    }

    /// <summary>
    /// Gets the current home configuration for a specific Jellyfin user, with 5-second caching.
    /// Fetches from Tentacle API with userId + api_key (the caller's Jellyfin access token,
    /// which the backend validates before trusting the userId). Returns null if unavailable.
    /// </summary>
    public async Task<HomeConfig?> GetHomeConfigAsync(Guid userId = default, string apiKey = "", CancellationToken cancellationToken = default)
    {
        var home = await GetHomeConfigResultAsync(userId, apiKey, cancellationToken).ConfigureAwait(false);
        return home.Config;
    }

    /// <summary>
    /// Like <see cref="GetHomeConfigAsync"/>, but tells a failed read apart from "no home config".
    /// </summary>
    public Task<HomeConfigResult> GetHomeConfigResultAsync(Guid userId = default, string apiKey = "", CancellationToken cancellationToken = default)
    {
        var plugin = Plugin.Instance;
        if (plugin == null || string.IsNullOrEmpty(plugin.Configuration.TentacleUrl))
        {
            return Task.FromResult(new HomeConfigResult(null, false));
        }

        var cacheKey = userId == default ? "_global" : userId.ToString("N");
        // The shared fetch is keyed by the caller's token too: a 401/403 is a verdict
        // on that token, and must not be handed to the user's other requests (#95).
        var flightKey = cacheKey + "\n" + apiKey;

        Task<HomeConfigResult>? fetch;
        lock (_cacheLock)
        {
            if (_userCache.TryGetValue(cacheKey, out var entry) && DateTime.UtcNow < entry.Expiry)
            {
                return Task.FromResult(entry.Value);
            }

            if (!_inflight.TryGetValue(flightKey, out fetch))
            {
                fetch = FetchAndCacheAsync(plugin.Configuration.TentacleUrl, userId, apiKey, cacheKey, flightKey);
                _inflight[flightKey] = fetch;
            }
        }

        // The fetch runs to its own timeout whoever waits for it; a caller whose
        // client went away stops waiting at once.
        return fetch.WaitAsync(cancellationToken);
    }

    /// <summary>
    /// Clears all cached configs so the next call re-fetches from the API.
    /// </summary>
    public void ClearCache()
    {
        lock (_cacheLock)
        {
            _userCache.Clear();
        }

        _logger.LogInformation("[Tentacle] Home config cache cleared");
    }

    private async Task<HomeConfigResult> FetchAndCacheAsync(string tentacleUrl, Guid userId, string apiKey, string cacheKey, string flightKey)
    {
        await Task.Yield(); // never finish inline: the caller registers this task first
        try
        {
            var (home, callerRefused) = await FetchFromApiAsync(tentacleUrl, userId, apiKey).ConfigureAwait(false);

            // The cache is keyed by user, but a 401/403 is about THIS caller's token,
            // not about the user's config: caching it would hand the refusal to the
            // user's other, validly-authenticated requests (blank toolbar, rows with
            // no sort settings) until the entry expires. Other failures stay cached
            // briefly: an unreachable backend must not cost every home request its
            // full timeout.
            if (!callerRefused)
            {
                lock (_cacheLock)
                {
                    _userCache[cacheKey] = (home, DateTime.UtcNow.Add(CacheDuration));
                    if (_userCache.Count > PruneAbove)
                    {
                        var now = DateTime.UtcNow;
                        foreach (var stale in _userCache.Where(e => now >= e.Value.Expiry).Select(e => e.Key).ToList())
                        {
                            _userCache.Remove(stale);
                        }
                    }
                }
            }

            return home;
        }
        finally
        {
            lock (_cacheLock)
            {
                _inflight.Remove(flightKey);
            }
        }
    }

    private async Task<(HomeConfigResult Home, bool CallerRefused)> FetchFromApiAsync(string tentacleUrl, Guid userId, string apiKey)
    {
        if (string.IsNullOrEmpty(tentacleUrl))
        {
            _logger.LogDebug("Tentacle URL not configured");
            return (new HomeConfigResult(null, false), false);
        }

        try
        {
            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(3);
            var url = $"{tentacleUrl.TrimEnd('/')}/api/smartlists/home-config";
            var query = new List<string>();
            if (userId != default)
            {
                query.Add($"userId={userId:N}");
            }
            if (!string.IsNullOrEmpty(apiKey))
            {
                query.Add($"api_key={Uri.EscapeDataString(apiKey)}");
            }
            if (query.Count > 0)
            {
                url += "?" + string.Join("&", query);
            }

            using var response = await client.GetAsync(url).ConfigureAwait(false);

            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning("Tentacle API returned {Status} for home-config", response.StatusCode);
                var callerRefused = response.StatusCode is System.Net.HttpStatusCode.Unauthorized
                    or System.Net.HttpStatusCode.Forbidden;
                return (new HomeConfigResult(null, true), callerRefused);
            }

            var json = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            var wrapper = JsonSerializer.Deserialize<HomeConfigResponse>(json, JsonOptions);

            if (wrapper?.Config == null)
            {
                _logger.LogDebug("Tentacle returned empty home config");
                return (new HomeConfigResult(null, false), false);
            }

            _logger.LogInformation("[Tentacle] Loaded home config with {RowCount} rows from API", wrapper.Config.Rows?.Count ?? 0);
            return (new HomeConfigResult(wrapper.Config, false), false);
        }
        catch (Exception ex)
        {
            // Warning, not Debug: a timeout or a dropped connection is the usual
            // outage, and the only trace of it in Jellyfin's log. Failures are
            // cached for CacheDuration, so this is at most one line per user per 5 s.
            _logger.LogWarning("Could not read the home config from Tentacle: {Error}", ex.Message);
            return (new HomeConfigResult(null, true), false);
        }
    }

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };
}

/// <summary>
/// Wrapper for the /api/smartlists/home-config response.
/// </summary>
internal class HomeConfigResponse
{
    [JsonPropertyName("exists")]
    public bool Exists { get; set; }

    [JsonPropertyName("config")]
    public HomeConfig? Config { get; set; }
}

/// <summary>
/// Represents the tentacle-home.json structure.
/// </summary>
public class HomeConfig
{
    [JsonPropertyName("hero")]
    public HeroConfig? Hero { get; set; }

    [JsonPropertyName("rows")]
    public List<RowConfig>? Rows { get; set; }

    [JsonPropertyName("toolbar")]
    public List<ToolbarButton>? Toolbar { get; set; }

    /// <summary>Combine Continue Watching + Next Up into a single home row.</summary>
    [JsonPropertyName("merge_continue_watching")]
    public bool MergeContinueWatching { get; set; }

    /// <summary>
    /// What a client may play as a preview when a card is merely focused:
    /// "all", "local_only" (never a provider stream) or "off". A focus preview of
    /// a provider (.strm) title is a provider connection, and on a
    /// connection-limited account that can cut a running recording off.
    /// </summary>
    [JsonPropertyName("card_previews")]
    public string? CardPreviews { get; set; }
}

/// <summary>
/// A toolbar button configuration (visibility and order).
/// </summary>
public class ToolbarButton
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = string.Empty;

    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; } = true;
}

/// <summary>
/// Hero spotlight configuration.
/// </summary>
public class HeroConfig
{
    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; }

    [JsonPropertyName("playlist_id")]
    public string PlaylistId { get; set; } = string.Empty;

    [JsonPropertyName("display_name")]
    public string DisplayName { get; set; } = string.Empty;

    [JsonPropertyName("sort_by")]
    public string SortBy { get; set; } = "random";

    [JsonPropertyName("sort_order")]
    public string SortOrder { get; set; } = "Descending";

    [JsonPropertyName("require_logo")]
    public bool RequireLogo { get; set; } = true;

    [JsonPropertyName("require_trailer")]
    public bool RequireTrailer { get; set; } = false;

    [JsonPropertyName("trailer_audio")]
    public bool TrailerAudio { get; set; } = false;

    [JsonPropertyName("item_count")]
    public int ItemCount { get; set; } = 10;
}

/// <summary>
/// A single row in the homepage layout.
/// Type is "playlist" for Tentacle playlists or "builtin" for native Jellyfin sections.
/// </summary>
public class RowConfig
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = "playlist";

    [JsonPropertyName("playlist_id")]
    public string PlaylistId { get; set; } = string.Empty;

    [JsonPropertyName("section_id")]
    public string SectionId { get; set; } = string.Empty;

    [JsonPropertyName("display_name")]
    public string DisplayName { get; set; } = string.Empty;

    [JsonPropertyName("order")]
    public int Order { get; set; }

    [JsonPropertyName("max_items")]
    public int? MaxItems { get; set; }

    [JsonPropertyName("sort_by")]
    public string? SortBy { get; set; }

    [JsonPropertyName("sort_order")]
    public string? SortOrder { get; set; }

    /// <summary>
    /// How this row's cards are drawn: "poster" (2:3) or "wide" (16:9).
    /// YouTube rows default to wide, since YouTube artwork has no portrait form
    /// and a thumbnail in a poster slot is cropped to a strip of its middle.
    /// </summary>
    [JsonPropertyName("shape")]
    public string? Shape { get; set; }

    /// <summary>
    /// Returns true if this is a built-in Jellyfin section (not a Tentacle playlist).
    /// </summary>
    [JsonIgnore]
    public bool IsBuiltin => string.Equals(Type, "builtin", StringComparison.OrdinalIgnoreCase);
}
