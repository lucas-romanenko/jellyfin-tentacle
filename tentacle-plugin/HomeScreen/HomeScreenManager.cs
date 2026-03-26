using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.HomeScreen;

/// <summary>
/// Reads and caches tentacle-home.json configuration.
/// </summary>
public class HomeScreenManager
{
    private readonly ILogger<HomeScreenManager> _logger;
    private readonly object _cacheLock = new();
    private HomeConfig? _cachedConfig;
    private DateTime _cacheExpiry = DateTime.MinValue;
    private static readonly TimeSpan CacheDuration = TimeSpan.FromSeconds(5);

    public HomeScreenManager(ILogger<HomeScreenManager> logger)
    {
        _logger = logger;
    }

    /// <summary>
    /// Gets the current home configuration, with 5-second caching.
    /// Returns null if the file doesn't exist or is invalid.
    /// </summary>
    public HomeConfig? GetHomeConfig()
    {
        var plugin = Plugin.Instance;
        if (plugin == null || !plugin.Configuration.Enabled)
        {
            return null;
        }

        lock (_cacheLock)
        {
            if (_cachedConfig != null && DateTime.UtcNow < _cacheExpiry)
            {
                return _cachedConfig;
            }
        }

        var config = LoadFromDisk(plugin.Configuration.HomeConfigPath);

        lock (_cacheLock)
        {
            _cachedConfig = config;
            _cacheExpiry = DateTime.UtcNow.Add(CacheDuration);
        }

        return config;
    }

    /// <summary>
    /// Clears the cached config so the next call re-reads from disk.
    /// </summary>
    public void ClearCache()
    {
        lock (_cacheLock)
        {
            _cachedConfig = null;
            _cacheExpiry = DateTime.MinValue;
        }

        _logger.LogInformation("[Tentacle] Home config cache cleared");
    }

    private HomeConfig? LoadFromDisk(string path)
    {
        try
        {
            if (!File.Exists(path))
            {
                _logger.LogDebug("Tentacle home config not found at {Path}", path);
                return null;
            }

            var json = File.ReadAllText(path);
            var config = JsonSerializer.Deserialize<HomeConfig>(json, JsonOptions);

            if (config == null)
            {
                _logger.LogWarning("Tentacle home config at {Path} deserialized to null", path);
                return null;
            }

            _logger.LogInformation("[Tentacle] Loaded home config with {RowCount} rows from {Path}", config.Rows?.Count ?? 0, path);
            return config;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to read Tentacle home config from {Path}", path);
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