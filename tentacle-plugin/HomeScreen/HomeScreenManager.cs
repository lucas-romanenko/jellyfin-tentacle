using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.HomeScreen;

/// <summary>
/// Fetches and caches home configuration from the Tentacle API.
/// </summary>
public class HomeScreenManager
{
    private readonly ILogger<HomeScreenManager> _logger;
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly object _cacheLock = new();
    private readonly Dictionary<string, (HomeConfig? Config, DateTime Expiry)> _userCache = new();
    private static readonly TimeSpan CacheDuration = TimeSpan.FromSeconds(5);

    public HomeScreenManager(ILogger<HomeScreenManager> logger, IHttpClientFactory httpClientFactory)
    {
        _logger = logger;
        _httpClientFactory = httpClientFactory;
    }

    /// <summary>
    /// Gets the current home configuration for a specific Jellyfin user, with 5-second caching.
    /// Fetches from Tentacle API with userId query param. Returns null if unavailable.
    /// </summary>
    public HomeConfig? GetHomeConfig(Guid userId = default)
    {
        var plugin = Plugin.Instance;
        if (plugin == null || string.IsNullOrEmpty(plugin.Configuration.TentacleUrl))
        {
            return null;
        }

        var cacheKey = userId == default ? "_global" : userId.ToString("N");

        lock (_cacheLock)
        {
            if (_userCache.TryGetValue(cacheKey, out var entry) && DateTime.UtcNow < entry.Expiry)
            {
                return entry.Config;
            }
        }

        var config = FetchFromApi(plugin.Configuration.TentacleUrl, userId);

        lock (_cacheLock)
        {
            _userCache[cacheKey] = (config, DateTime.UtcNow.Add(CacheDuration));
        }

        return config;
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

    private HomeConfig? FetchFromApi(string tentacleUrl, Guid userId = default)
    {
        if (string.IsNullOrEmpty(tentacleUrl))
        {
            _logger.LogDebug("Tentacle URL not configured");
            return null;
        }

        try
        {
            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(3);
            var url = $"{tentacleUrl.TrimEnd('/')}/api/smartlists/home-config";
            if (userId != default)
            {
                url += $"?userId={userId:N}";
            }

            var response = client.GetAsync(url).GetAwaiter().GetResult();

            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning("Tentacle API returned {Status} for home-config", response.StatusCode);
                return null;
            }

            var json = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            var wrapper = JsonSerializer.Deserialize<HomeConfigResponse>(json, JsonOptions);

            if (wrapper?.Config == null)
            {
                _logger.LogDebug("Tentacle returned empty home config");
                return null;
            }

            _logger.LogInformation("[Tentacle] Loaded home config with {RowCount} rows from API", wrapper.Config.Rows?.Count ?? 0);
            return wrapper.Config;
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "Failed to fetch home config from Tentacle API");
            return null;
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

    /// <summary>
    /// Returns true if this is a built-in Jellyfin section (not a Tentacle playlist).
    /// </summary>
    [JsonIgnore]
    public bool IsBuiltin => string.Equals(Type, "builtin", StringComparison.OrdinalIgnoreCase);
}
