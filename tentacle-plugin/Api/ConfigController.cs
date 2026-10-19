using System.Net;
using System.Text.Json;
using Jellyfin.Plugin.Tentacle.Services;
using Microsoft.AspNetCore.Authorization;
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

    // _cachedConfig is volatile so the unlocked fast-path read in GetConfig sees the
    // latest write; a stale _configCacheExpiry read is benign (the locked
    // double-check below corrects it).
    private static volatile string? _cachedConfig;
    private static DateTime _configCacheExpiry = DateTime.MinValue;

    // The last answer built from a successful read. A failed read is never cached
    // (it switched ratings off for every user for 5 minutes, #258); it serves this
    // instead, so a backend restart doesn't flicker ratings off. Survives ClearCache.
    private static volatile string? _lastGoodConfig;

    // After a failed read, don't ask the backend again for a moment: every request
    // would wait out the 10 s timeout in turn behind _cacheLock.
    private static readonly TimeSpan RetryAfterFailure = TimeSpan.FromSeconds(15);
    private static DateTime _retryAfter = DateTime.MinValue;
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
        _retryAfter = DateTime.MinValue;
    }

    /// <summary>
    /// Returns the current feature configuration.
    /// Fetches API keys from the Tentacle backend and reports enabled/disabled status.
    /// Keys are masked in the response for security.
    /// </summary>
    [HttpGet]
    [Authorize]
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

            if (DateTime.UtcNow < _retryAfter)
            {
                return LastGoodOrUnavailable();
            }

            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(10);

            string? mdblistKey = null;
            string? tmdbKey = null;

            try
            {
                var response = await PluginKeysClient.GetSecuredStringAsync(client, $"{tentacleUrl}/api/settings/plugin-keys", HttpContext.Request);
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

                var mdblistEnabled = !string.IsNullOrEmpty(mdblistKey);
                var tmdbEnabled = !string.IsNullOrEmpty(tmdbKey);

                var configJson = BuildConfigJson(mdblistEnabled, mdblistKey, tmdbEnabled, tmdbKey);

                // Only a successful read is shared with everyone.
                _cachedConfig = configJson;
                _lastGoodConfig = configJson;
                _configCacheExpiry = DateTime.UtcNow.Add(CacheDuration);

                return Content(configJson, "application/json");
            }
            catch (HttpRequestException ex) when (ex.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
            {
                // The backend refused THIS caller's token (an expired session, a server
                // API key): that says nothing about the keys, so it goes to this caller
                // only and is never cached for the others.
                _logger.LogWarning("[Tentacle Config] Tentacle refused the caller's token: {Status}", (int)ex.StatusCode!);
                return StatusCode((int)ex.StatusCode!);
            }
            catch (Exception ex)
            {
                _logger.LogWarning("[Tentacle Config] Failed to fetch settings: {Error}", ex.Message);
                _retryAfter = DateTime.UtcNow.Add(RetryAfterFailure);
            }

            return LastGoodOrUnavailable();
        }
        finally
        {
            _cacheLock.Release();
        }
    }

    /// <summary>
    /// The answer while the backend can't be read (restarting, slow, failing): what was
    /// last known, else a 503, which the web client retries (tentacle-mdblist.js).
    /// </summary>
    private ActionResult LastGoodOrUnavailable()
    {
        var lastGood = _lastGoodConfig;
        if (lastGood != null)
        {
            return Content(lastGood, "application/json");
        }

        return StatusCode(503, new { error = "Tentacle did not answer" });
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
