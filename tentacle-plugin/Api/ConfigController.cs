using System.Text.Json;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// API controller that returns Tentacle feature configuration.
/// Used by the frontend JS to know which features (MDBList, TMDB ratings) are enabled.
/// Route: /Tentacle/Config
/// </summary>
[ApiController]
[Route("Tentacle/Config")]
public class TentacleConfigController : ControllerBase
{
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<TentacleConfigController> _logger;

    private static readonly TimeSpan CacheDuration = TimeSpan.FromMinutes(5);
    private static string? _cachedConfig;
    private static DateTime _configCacheExpiry = DateTime.MinValue;
    private static readonly SemaphoreSlim _cacheLock = new(1, 1);

    public TentacleConfigController(
        IHttpClientFactory httpClientFactory,
        ILogger<TentacleConfigController> logger)
    {
        _httpClientFactory = httpClientFactory;
        _logger = logger;
    }

    /// <summary>
    /// Clear the cached config. Called when settings change.
    /// </summary>
    public static void ClearCache()
    {
        _cachedConfig = null;
        _configCacheExpiry = DateTime.MinValue;
    }

    /// <summary>
    /// Returns the current feature configuration.
    /// Fetches API keys from the Tentacle backend and reports enabled/disabled status.
    /// Keys are masked in the response for security.
    /// </summary>
    [HttpGet]
    public async Task<ActionResult> GetConfig()
    {
        if (_cachedConfig != null && DateTime.UtcNow < _configCacheExpiry)
        {
            return Content(_cachedConfig, "application/json");
        }

        await _cacheLock.WaitAsync();
        try
        {
            // Double-check after lock
            if (_cachedConfig != null && DateTime.UtcNow < _configCacheExpiry)
            {
                return Content(_cachedConfig, "application/json");
            }

            var tentacleUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
            if (string.IsNullOrEmpty(tentacleUrl))
            {
                var fallback = BuildConfigJson(false, null, false, null);
                return Content(fallback, "application/json");
            }

            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(10);

            string? mdblistKey = null;
            string? tmdbKey = null;

            try
            {
                var response = await client.GetStringAsync($"{tentacleUrl}/api/settings/raw");
                using var doc = JsonDocument.Parse(response);

                if (doc.RootElement.TryGetProperty("mdblist_api_key", out var mdbElement))
                {
                    mdblistKey = mdbElement.GetString();
                }

                if (doc.RootElement.TryGetProperty("tmdb_bearer_token", out var tmdbBearerElement))
                {
                    tmdbKey = tmdbBearerElement.GetString();
                }

                if (string.IsNullOrEmpty(tmdbKey) && doc.RootElement.TryGetProperty("tmdb_api_key", out var tmdbApiElement))
                {
                    tmdbKey = tmdbApiElement.GetString();
                }
            }
            catch (Exception ex)
            {
                _logger.LogWarning("[Tentacle Config] Failed to fetch settings: {Error}", ex.Message);
            }

            var mdblistEnabled = !string.IsNullOrEmpty(mdblistKey);
            var tmdbEnabled = !string.IsNullOrEmpty(tmdbKey);

            var configJson = BuildConfigJson(mdblistEnabled, mdblistKey, tmdbEnabled, tmdbKey);

            _cachedConfig = configJson;
            _configCacheExpiry = DateTime.UtcNow.Add(CacheDuration);

            return Content(configJson, "application/json");
        }
        finally
        {
            _cacheLock.Release();
        }
    }

    private static string BuildConfigJson(bool mdblistEnabled, string? mdblistKey, bool tmdbEnabled, string? tmdbKey)
    {
        var result = new
        {
            mdblistEnabled,
            mdblistApiKey = MaskKey(mdblistKey),
            tmdbEnabled,
            tmdbApiKey = MaskKey(tmdbKey),
        };

        return JsonSerializer.Serialize(result, new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        });
    }

    /// <summary>
    /// Mask an API key for display: show first 4 and last 4 characters.
    /// </summary>
    private static string? MaskKey(string? key)
    {
        if (string.IsNullOrEmpty(key))
        {
            return null;
        }

        if (key.Length <= 8)
        {
            return "****";
        }

        return $"{key[..4]}...{key[^4..]}";
    }
}
