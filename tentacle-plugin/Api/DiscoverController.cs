using System.Net.Http;
using System.Reflection;
using System.Text.Json;
using Jellyfin.Plugin.Tentacle.Configuration;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// API controller that proxies discover/trending data from Tentacle
/// and serves the discover tab JS/CSS.
/// </summary>
[ApiController]
[Route("[controller]")]
public class TentacleDiscoverController : ControllerBase
{
    private readonly ILogger<TentacleDiscoverController> _logger;
    private static readonly HttpClient HttpClient = new() { Timeout = TimeSpan.FromSeconds(15) };

    // In-memory cache for discover data (30 min), keyed by type param
    private static readonly Dictionary<string, (string Data, DateTime Expiry)> _itemsCache = new();
    private static string? _cachedConfig;
    private static DateTime _configCacheExpiry = DateTime.MinValue;

    // No cache for activity data — always fetch fresh for real-time progress
    private static string? _cachedActivity;
    private static DateTime _activityCacheExpiry = DateTime.MinValue;

    public TentacleDiscoverController(ILogger<TentacleDiscoverController> logger)
    {
        _logger = logger;
    }

    /// <summary>
    /// Clears the discover config and items caches.
    /// Called from TentacleController.Refresh().
    /// </summary>
    public static void ClearCache()
    {
        _cachedConfig = null;
        _configCacheExpiry = DateTime.MinValue;
        _itemsCache.Clear();
        _cachedActivity = null;
        _activityCacheExpiry = DateTime.MinValue;
    }

    private string GetTentacleUrl()
    {
        var config = Plugin.Instance?.Configuration;
        return config?.TentacleUrl?.TrimEnd('/') ?? "";
    }

    /// <summary>
    /// Gets the userId query param forwarded from the JS client.
    /// Used to authenticate proxy requests to Tentacle backend.
    /// </summary>
    private string GetUserIdParam()
    {
        var userId = HttpContext.Request.Query["userId"].FirstOrDefault();
        return string.IsNullOrEmpty(userId) ? "" : $"userId={userId}";
    }

    private string AppendUserId(string url)
    {
        var param = GetUserIdParam();
        if (string.IsNullOrEmpty(param)) return url;
        return url.Contains('?') ? $"{url}&{param}" : $"{url}?{param}";
    }

    /// <summary>
    /// Proxies trending items from Tentacle /api/discover.
    /// Cached for 30 minutes.
    /// </summary>
    [HttpGet("Items")]
    [Authorize]
    public async Task<ActionResult> GetDiscoverItems([FromQuery] string type = "all")
    {
        // Sanitize type param
        if (type != "all" && type != "movies" && type != "series")
            type = "all";

        if (_itemsCache.TryGetValue(type, out var cached) && DateTime.UtcNow < cached.Expiry)
        {
            return Content(cached.Data, "application/json");
        }

        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return Ok(new { sections = Array.Empty<object>() });
        }

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover?type={type}");
            _itemsCache[type] = (response, DateTime.UtcNow.AddMinutes(30));
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch discover: {Error}", ex.Message);
            return Ok(new { sections = Array.Empty<object>() });
        }
    }

    /// <summary>
    /// Proxies discover config (enabled/disabled) from Tentacle.
    /// Cached for 5 minutes.
    /// </summary>
    [HttpGet("Config")]
    [Authorize]
    public async Task<ActionResult> GetDiscoverConfig()
    {
        if (_cachedConfig != null && DateTime.UtcNow < _configCacheExpiry)
        {
            return Content(_cachedConfig, "application/json");
        }

        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return Ok(new { discover_in_jellyfin = false });
        }

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover/config");
            _cachedConfig = response;
            _configCacheExpiry = DateTime.UtcNow.AddMinutes(5);
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch config: {Error}", ex.Message);
            return Ok(new { discover_in_jellyfin = false });
        }
    }

    /// <summary>
    /// Proxies activity data (downloads + unreleased) from Tentacle.
    /// No cache — always fetches fresh data for real-time progress.
    /// </summary>
    [HttpGet("Activity")]
    [Authorize]
    public async Task<ActionResult> GetActivity()
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return Ok(new { downloads = Array.Empty<object>(), unreleased = Array.Empty<object>() });
        }

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/activity");
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch activity: {Error}", ex.Message);
            return Ok(new { downloads = Array.Empty<object>(), unreleased = Array.Empty<object>() });
        }
    }

    /// <summary>
    /// Proxies item detail request to Tentacle (for modal metadata).
    /// </summary>
    [HttpGet("Detail/{mediaType}/{tmdbId}")]
    [Authorize]
    public async Task<ActionResult> GetDetail(string mediaType, int tmdbId)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return NotFound();
        }

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover/detail/{mediaType}/{tmdbId}");
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch detail: {Error}", ex.Message);
            return NotFound();
        }
    }

    /// <summary>
    /// Proxies TMDB search requests to Tentacle.
    /// </summary>
    [HttpGet("Search")]
    [Authorize]
    public async Task<ActionResult> SearchDiscover([FromQuery] string q = "", [FromQuery] string type = "all")
    {
        if (string.IsNullOrWhiteSpace(q))
        {
            return Ok(new { items = Array.Empty<object>() });
        }

        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return Ok(new { items = Array.Empty<object>() });
        }

        try
        {
            var encodedQ = System.Net.WebUtility.UrlEncode(q);
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover/search?q={encodedQ}&type={type}");
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to search: {Error}", ex.Message);
            return Ok(new { items = Array.Empty<object>() });
        }
    }

    /// <summary>
    /// Proxies add-to-Radarr requests to Tentacle.
    /// </summary>
    [HttpPost("AddToRadarr")]
    [Authorize]
    public async Task<ActionResult> AddToRadarr([FromBody] JsonElement body)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return BadRequest("Tentacle URL not configured");
        }

        try
        {
            var content = new StringContent(body.GetRawText(), System.Text.Encoding.UTF8, "application/json");
            var response = await HttpClient.PostAsync(AppendUserId($"{baseUrl}/api/lists/add-to-radarr"), content);
            var result = await response.Content.ReadAsStringAsync();
            return new ContentResult { Content = result, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to add to Radarr: {Error}", ex.Message);
            return StatusCode(500, new { detail = ex.Message });
        }
    }

    /// <summary>
    /// Proxies add-to-Sonarr requests to Tentacle.
    /// </summary>
    [HttpPost("AddToSonarr")]
    [Authorize]
    public async Task<ActionResult> AddToSonarr([FromBody] JsonElement body)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return BadRequest("Tentacle URL not configured");
        }

        try
        {
            var content = new StringContent(body.GetRawText(), System.Text.Encoding.UTF8, "application/json");
            var response = await HttpClient.PostAsync(AppendUserId($"{baseUrl}/api/lists/add-to-sonarr"), content);
            var result = await response.Content.ReadAsStringAsync();
            return new ContentResult { Content = result, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to add to Sonarr: {Error}", ex.Message);
            return StatusCode(500, new { detail = ex.Message });
        }
    }

    /// <summary>
    /// Proxies Radarr quality profiles request to Tentacle.
    /// </summary>
    [HttpGet("RadarrProfiles")]
    [Authorize]
    public async Task<ActionResult> GetRadarrProfiles()
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return Ok(Array.Empty<object>());

        try
        {
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/radarr/quality-profiles"));
            return Content(response, "application/json");
        }
        catch
        {
            return Ok(Array.Empty<object>());
        }
    }

    /// <summary>
    /// Proxies Radarr root folders request to Tentacle.
    /// </summary>
    [HttpGet("RadarrFolders")]
    [Authorize]
    public async Task<ActionResult> GetRadarrFolders()
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return Ok(Array.Empty<object>());

        try
        {
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/radarr/rootfolders"));
            return Content(response, "application/json");
        }
        catch
        {
            return Ok(Array.Empty<object>());
        }
    }

    /// <summary>
    /// Proxies Sonarr quality profiles request to Tentacle.
    /// </summary>
    [HttpGet("SonarrProfiles")]
    [Authorize]
    public async Task<ActionResult> GetSonarrProfiles()
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return Ok(Array.Empty<object>());

        try
        {
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/sonarr/quality-profiles"));
            return Content(response, "application/json");
        }
        catch
        {
            return Ok(Array.Empty<object>());
        }
    }

    /// <summary>
    /// Proxies Sonarr root folders request to Tentacle.
    /// </summary>
    [HttpGet("SonarrFolders")]
    [Authorize]
    public async Task<ActionResult> GetSonarrFolders()
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return Ok(Array.Empty<object>());

        try
        {
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/sonarr/rootfolders"));
            return Content(response, "application/json");
        }
        catch
        {
            return Ok(Array.Empty<object>());
        }
    }

    /// <summary>
    /// Proxies season list for a TV series from Tentacle.
    /// </summary>
    [HttpGet("Seasons/{tmdbId}")]
    [Authorize]
    public async Task<ActionResult> GetSeasons(int tmdbId)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return NotFound();

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover/seasons/{tmdbId}");
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch seasons: {Error}", ex.Message);
            return NotFound();
        }
    }

    /// <summary>
    /// Proxies episode list for a specific season from Tentacle.
    /// </summary>
    [HttpGet("Season/{tmdbId}/{seasonNumber}")]
    [Authorize]
    public async Task<ActionResult> GetSeasonEpisodes(int tmdbId, int seasonNumber)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return NotFound();

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover/season/{tmdbId}/{seasonNumber}");
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch episodes: {Error}", ex.Message);
            return NotFound();
        }
    }

    /// <summary>
    /// Proxies Sonarr episode monitoring state for an existing series.
    /// </summary>
    [HttpGet("SonarrEpisodes/{tmdbId}")]
    [Authorize]
    public async Task<ActionResult> GetSonarrEpisodes(int tmdbId)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return NotFound();

        try
        {
            var response = await HttpClient.GetStringAsync(
                AppendUserId($"{baseUrl}/api/discover/sonarr-episodes/{tmdbId}"));
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch Sonarr episodes: {Error}", ex.Message);
            return NotFound();
        }
    }

    /// <summary>
    /// Proxies episode monitoring changes to Tentacle.
    /// </summary>
    [HttpPost("ManageEpisodes")]
    [Authorize]
    public async Task<ActionResult> ManageEpisodes([FromBody] JsonElement body)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return BadRequest("Tentacle URL not configured");
        }

        try
        {
            var content = new StringContent(body.GetRawText(), System.Text.Encoding.UTF8, "application/json");
            var response = await HttpClient.PostAsync(
                AppendUserId($"{baseUrl}/api/discover/manage-episodes"), content);
            var result = await response.Content.ReadAsStringAsync();
            return new ContentResult { Content = result, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to manage episodes: {Error}", ex.Message);
            return StatusCode(500, new { detail = ex.Message });
        }
    }

    /// <summary>
    /// Serves the Tentacle discover JavaScript.
    /// </summary>
    [HttpGet("/Tentacle/discover.js")]
    [ResponseCache(NoStore = true)]
    public ActionResult GetDiscoverJs()
    {
        var content = LoadEmbeddedResource("tentacle-discover.js");
        if (content == null) return NotFound();
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        return Content(content, "application/javascript");
    }

    /// <summary>
    /// Serves the Tentacle discover CSS.
    /// </summary>
    [HttpGet("/Tentacle/discover.css")]
    [ResponseCache(NoStore = true)]
    public ActionResult GetDiscoverCss()
    {
        var content = LoadEmbeddedResource("tentacle-discover.css");
        if (content == null) return NotFound();
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        return Content(content, "text/css");
    }

    private static string? LoadEmbeddedResource(string resourceSuffix)
    {
        var assembly = typeof(TentacleDiscoverController).Assembly;
        var name = assembly.GetManifestResourceNames()
            .FirstOrDefault(n => n.EndsWith(resourceSuffix));

        if (name == null) return null;

        using var stream = assembly.GetManifestResourceStream(name);
        if (stream == null) return null;

        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    }
}
