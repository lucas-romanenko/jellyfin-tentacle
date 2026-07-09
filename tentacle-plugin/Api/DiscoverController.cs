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
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, (string Data, DateTime Expiry)> _itemsCache = new();
    private static string? _cachedConfig;
    private static DateTime _configCacheExpiry = DateTime.MinValue;
    private static readonly object _configLock = new();


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
        lock (_configLock)
        {
            _cachedConfig = null;
            _configCacheExpiry = DateTime.MinValue;
        }
        _itemsCache.Clear();
    }

    private string GetTentacleUrl()
    {
        var config = Plugin.Instance?.Configuration;
        return config?.TentacleUrl?.TrimEnd('/') ?? "";
    }

    /// <summary>
    /// Gets the userId query param forwarded from the JS client.
    /// Used as a cache key discriminator (not for auth on its own).
    /// </summary>
    private string GetUserIdParam()
    {
        var userId = HttpContext.Request.Query["userId"].FirstOrDefault();
        return string.IsNullOrEmpty(userId) ? "" : $"userId={userId}";
    }

    /// <summary>
    /// Extracts the caller's Jellyfin access token from the (Jellyfin-authenticated)
    /// inbound request so it can be forwarded to the Tentacle backend as api_key.
    /// The backend validates this token against Jellyfin before trusting the userId
    /// claim, which prevents impersonation by anyone hitting the backend directly.
    /// </summary>
    private string GetApiKey()
    {
        var req = HttpContext.Request;
        var token = req.Query["api_key"].FirstOrDefault();
        if (string.IsNullOrEmpty(token))
            token = req.Headers["X-Emby-Token"].FirstOrDefault();
        if (string.IsNullOrEmpty(token))
            token = req.Headers["X-MediaBrowser-Token"].FirstOrDefault();
        if (string.IsNullOrEmpty(token))
        {
            var auth = req.Headers["Authorization"].FirstOrDefault()
                       ?? req.Headers["X-Emby-Authorization"].FirstOrDefault();
            if (!string.IsNullOrEmpty(auth))
            {
                var m = System.Text.RegularExpressions.Regex.Match(auth, "Token=\"?([^\",]+)\"?");
                if (m.Success) token = m.Groups[1].Value;
            }
        }
        return token ?? "";
    }

    /// <summary>
    /// Appends the forwarded userId AND the caller's Jellyfin token (api_key) to a
    /// backend URL so the backend can authenticate the request as that user.
    /// </summary>
    private string AppendUserId(string url)
    {
        var parts = new List<string>();
        var userId = HttpContext.Request.Query["userId"].FirstOrDefault();
        if (!string.IsNullOrEmpty(userId)) parts.Add($"userId={Uri.EscapeDataString(userId)}");
        var apiKey = GetApiKey();
        if (!string.IsNullOrEmpty(apiKey)) parts.Add($"api_key={Uri.EscapeDataString(apiKey)}");
        if (parts.Count == 0) return url;
        var qs = string.Join("&", parts);
        return url.Contains('?') ? $"{url}&{qs}" : $"{url}?{qs}";
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

        var cacheKey = $"{type}_{GetUserIdParam()}";
        if (_itemsCache.TryGetValue(cacheKey, out var cached) && DateTime.UtcNow < cached.Expiry)
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
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/discover?type={type}"));
            _itemsCache[cacheKey] = (response, DateTime.UtcNow.AddMinutes(30));
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
        lock (_configLock)
        {
            if (_cachedConfig != null && DateTime.UtcNow < _configCacheExpiry)
            {
                return Content(_cachedConfig, "application/json");
            }
        }

        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return Ok(new { discover_in_jellyfin = false });
        }

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover/config");
            lock (_configLock)
            {
                _cachedConfig = response;
                _configCacheExpiry = DateTime.UtcNow.AddMinutes(5);
            }
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
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/activity"));
            // Rewrite relative proxy paths to absolute plugin endpoint URLs
            var jellyfinBase = $"{Request.Scheme}://{Request.Host}";
            response = response.Replace("/api/discover/image-proxy/", $"{jellyfinBase}/TentacleDiscover/ImageProxy/");
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
    /// For series, enriches with following/sonarr state from library endpoint.
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
            var detailTask = HttpClient.GetAsync(
                    AppendUserId($"{baseUrl}/api/discover/detail/{mediaType}/{tmdbId}"));

            // For series, also fetch library data to get following/sonarr state
            Task<string>? libraryTask = null;
            if (mediaType == "series")
            {
                libraryTask = HttpClient.GetStringAsync(
                    AppendUserId($"{baseUrl}/api/library/item/series/{tmdbId}")).ContinueWith(t =>
                    t.IsCompletedSuccessfully ? t.Result : "");
            }

            var detailResponse = await detailTask;
            var detailJson = await detailResponse.Content.ReadAsStringAsync();

            // Surface the backend's status code (e.g. 404 for unknown items) instead of
            // masking everything as NotFound.
            if (!detailResponse.IsSuccessStatusCode)
            {
                return new ContentResult { Content = detailJson, ContentType = "application/json", StatusCode = (int)detailResponse.StatusCode };
            }

            if (libraryTask != null)
            {
                var libraryJson = await libraryTask;
                if (!string.IsNullOrEmpty(libraryJson))
                {
                    try
                    {
                        using var detailDoc = JsonDocument.Parse(detailJson);
                        using var libDoc = JsonDocument.Parse(libraryJson);
                        var merged = new Dictionary<string, JsonElement>();

                        foreach (var prop in detailDoc.RootElement.EnumerateObject())
                            merged[prop.Name] = prop.Value.Clone();

                        // Add following and status from library data
                        if (libDoc.RootElement.TryGetProperty("following", out var following))
                            merged["following"] = following.Clone();
                        if (libDoc.RootElement.TryGetProperty("status", out var status))
                            merged["series_status"] = status.Clone();

                        var result = JsonSerializer.Serialize(merged);
                        return Content(result, "application/json");
                    }
                    catch
                    {
                        // If merge fails, return detail as-is
                    }
                }
            }

            return Content(detailJson, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch detail: {Error}", ex.Message);
            return NotFound();
        }
    }

    /// <summary>
    /// Proxies TheTVDB detail requests to Tentacle (for items not on TMDB).
    /// </summary>
    [HttpGet("DetailTvdb/{tvdbId}")]
    [Authorize]
    public async Task<ActionResult> GetDetailTvdb(int tvdbId)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return NotFound();
        }

        try
        {
            var detailJson = await HttpClient.GetStringAsync(
                $"{baseUrl}/api/discover/detail-tvdb/{tvdbId}");
            // Rewrite relative proxy paths to absolute plugin endpoint URLs
            var jellyfinBase = $"{Request.Scheme}://{Request.Host}";
            detailJson = detailJson.Replace("/api/discover/image-proxy/", $"{jellyfinBase}/TentacleDiscover/ImageProxy/");
            return Content(detailJson, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch TVDB detail: {Error}", ex.Message);
            return NotFound();
        }
    }

    /// <summary>
    /// Proxies follow/unfollow toggle for a series to Tentacle.
    /// </summary>
    [HttpPost("Follow/{tmdbId}")]
    [Authorize]
    public async Task<ActionResult> ToggleFollow(int tmdbId, [FromBody] JsonElement body)
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
                AppendUserId($"{baseUrl}/api/library/follow/{tmdbId}"), content);
            var result = await response.Content.ReadAsStringAsync();
            return new ContentResult { Content = result, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to toggle follow: {Error}", ex.Message);
            return StatusCode(500, new { detail = ex.Message });
        }
    }

    /// <summary>
    /// Proxies downloaded content deletion to Tentacle.
    /// Full cleanup: Radarr/Sonarr (disk), Jellyfin, Tentacle DB, playlists.
    /// Permission check: admin or download requester.
    /// </summary>
    [HttpDelete("LibraryItem/{mediaType}/{tmdbId}")]
    [Authorize]
    public async Task<ActionResult> DeleteLibraryItem(string mediaType, int tmdbId, [FromQuery] string? jellyfinItemId = null)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return BadRequest("Tentacle URL not configured");
        }

        try
        {
            var url = $"{baseUrl}/api/library/delete-download/{tmdbId}?media_type={mediaType}";
            if (!string.IsNullOrEmpty(jellyfinItemId))
                url += $"&jellyfin_item_id={jellyfinItemId}";
            var response = await HttpClient.DeleteAsync(AppendUserId(url));
            var result = await response.Content.ReadAsStringAsync();
            return new ContentResult { Content = result, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to delete library item: {Error}", ex.Message);
            return StatusCode(500, new { detail = ex.Message });
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
            // Rewrite relative proxy paths to absolute plugin endpoint URLs so Android TV can reach them
            var jellyfinBase = $"{Request.Scheme}://{Request.Host}";
            response = response.Replace("/api/discover/image-proxy/", $"{jellyfinBase}/TentacleDiscover/ImageProxy/");
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
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/lists/radarr-profiles"));
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
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/lists/radarr-folders"));
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
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/lists/sonarr-profiles"));
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
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/lists/sonarr-folders"));
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
    /// Proxies season list for a TheTVDB-only series via Sonarr.
    /// </summary>
    [HttpGet("SeasonsTvdb/{tvdbId}")]
    [Authorize]
    public async Task<ActionResult> GetSeasonsTvdb(int tvdbId)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return NotFound();

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover/seasons-tvdb/{tvdbId}");
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch TVDB seasons: {Error}", ex.Message);
            return NotFound();
        }
    }

    /// <summary>
    /// Proxies episode list for a TheTVDB-only series season via Sonarr.
    /// </summary>
    [HttpGet("SeasonTvdb/{tvdbId}/{seasonNumber}")]
    [Authorize]
    public async Task<ActionResult> GetSeasonEpisodesTvdb(int tvdbId, int seasonNumber)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return NotFound();

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover/season-tvdb/{tvdbId}/{seasonNumber}");
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch TVDB episodes: {Error}", ex.Message);
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
    /// Proxies VOD episode scan for a series (what's already on disk as .strm).
    /// </summary>
    [HttpGet("VodEpisodes/{tmdbId}")]
    [Authorize]
    public async Task<ActionResult> GetVodEpisodes(int tmdbId)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return NotFound();

        try
        {
            var response = await HttpClient.GetStringAsync($"{baseUrl}/api/discover/vod-episodes/{tmdbId}");
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch VOD episodes: {Error}", ex.Message);
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

    /// <summary>
    /// Serves the Tentacle search JavaScript.
    /// </summary>
    [HttpGet("/Tentacle/search.js")]
    [ResponseCache(NoStore = true)]
    public ActionResult GetSearchJs()
    {
        var content = LoadEmbeddedResource("tentacle-search.js");
        if (content == null) return NotFound();
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        return Content(content, "application/javascript");
    }

    /// <summary>
    /// Serves the Tentacle search CSS.
    /// </summary>
    [HttpGet("/Tentacle/search.css")]
    [ResponseCache(NoStore = true)]
    public ActionResult GetSearchCss()
    {
        var content = LoadEmbeddedResource("tentacle-search.css");
        if (content == null) return NotFound();
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        return Content(content, "text/css");
    }

    /// <summary>
    /// Serves the Tentacle Live TV JavaScript.
    /// </summary>
    [HttpGet("/Tentacle/livetv.js")]
    [ResponseCache(NoStore = true)]
    public ActionResult GetLiveTvJs()
    {
        var content = LoadEmbeddedResource("tentacle-livetv.js");
        if (content == null) return NotFound();
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        return Content(content, "application/javascript");
    }

    /// <summary>
    /// Serves the Tentacle Live TV CSS.
    /// </summary>
    [HttpGet("/Tentacle/livetv.css")]
    [ResponseCache(NoStore = true)]
    public ActionResult GetLiveTvCss()
    {
        var content = LoadEmbeddedResource("tentacle-livetv.css");
        if (content == null) return NotFound();
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        return Content(content, "text/css");
    }

    /// <summary>
    /// Serves the Tentacle Favorites JavaScript.
    /// </summary>
    [HttpGet("/Tentacle/favorites.js")]
    [ResponseCache(NoStore = true)]
    public ActionResult GetFavoritesJs()
    {
        var content = LoadEmbeddedResource("tentacle-favorites.js");
        if (content == null) return NotFound();
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        return Content(content, "application/javascript");
    }

    /// <summary>
    /// Serves the Tentacle Favorites CSS.
    /// </summary>
    [HttpGet("/Tentacle/favorites.css")]
    [ResponseCache(NoStore = true)]
    public ActionResult GetFavoritesCss()
    {
        var content = LoadEmbeddedResource("tentacle-favorites.css");
        if (content == null) return NotFound();
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        return Content(content, "text/css");
    }

    /// <summary>
    /// Proxies TVDB image requests through Jellyfin to Tentacle backend.
    /// TVDB CDN blocks non-browser HTTP clients, so Tentacle fetches server-side.
    /// </summary>
    [HttpGet("ImageProxy/{cacheKey}")]
    public async Task<ActionResult> ImageProxy(string cacheKey, [FromQuery] string url = "")
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl) || string.IsNullOrEmpty(url))
        {
            return NotFound();
        }

        // This endpoint is anonymous (Android TV/web image tags can't send auth headers),
        // so validate the forwarded url and require the cacheKey to be the MD5 of the url.
        // This prevents using the plugin as an open proxy to arbitrary hosts.
        if (!IsAllowedImageHost(url))
        {
            _logger.LogWarning("[Tentacle Discover] Image proxy rejected disallowed host: {Url}", url);
            return NotFound();
        }

        if (!string.Equals(cacheKey, Md5Hex(url), StringComparison.OrdinalIgnoreCase))
        {
            return BadRequest();
        }

        try
        {
            var encodedUrl = System.Net.WebUtility.UrlEncode(url);
            var response = await HttpClient.GetAsync($"{baseUrl}/api/discover/image-proxy/{cacheKey}?url={encodedUrl}");
            if (!response.IsSuccessStatusCode)
            {
                return StatusCode((int)response.StatusCode);
            }

            var content = await response.Content.ReadAsByteArrayAsync();
            var contentType = response.Content.Headers.ContentType?.ToString() ?? "image/jpeg";
            return File(content, contentType);
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Image proxy failed: {Error}", ex.Message);
            return NotFound();
        }
    }

    /// <summary>
    /// Proxies notifications for the current user from Tentacle.
    /// No cache — polled periodically by JS/Android clients.
    /// </summary>
    [HttpGet("Notifications")]
    [Authorize]
    public async Task<ActionResult> GetNotifications()
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return Ok(new { notifications = Array.Empty<object>(), notifications_enabled = true });
        }

        try
        {
            var response = await HttpClient.GetStringAsync(AppendUserId($"{baseUrl}/api/notifications"));
            return Content(response, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to fetch notifications: {Error}", ex.Message);
            return Ok(new { notifications = Array.Empty<object>(), notifications_enabled = true });
        }
    }

    /// <summary>
    /// Dismiss a single notification.
    /// </summary>
    [HttpPost("Notifications/{notificationId}/Dismiss")]
    [Authorize]
    public async Task<ActionResult> DismissNotification(int notificationId)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return NotFound();

        try
        {
            var response = await HttpClient.PostAsync(
                AppendUserId($"{baseUrl}/api/notifications/{notificationId}/dismiss"),
                new StringContent(""));
            var body = await response.Content.ReadAsStringAsync();
            return new ContentResult { Content = body, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to dismiss notification: {Error}", ex.Message);
            return StatusCode(500);
        }
    }

    /// <summary>
    /// Dismiss all notifications for the current user.
    /// </summary>
    [HttpPost("Notifications/DismissAll")]
    [Authorize]
    public async Task<ActionResult> DismissAllNotifications()
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return NotFound();

        try
        {
            var response = await HttpClient.PostAsync(
                AppendUserId($"{baseUrl}/api/notifications/dismiss-all"),
                new StringContent(""));
            var body = await response.Content.ReadAsStringAsync();
            return new ContentResult { Content = body, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to dismiss all notifications: {Error}", ex.Message);
            return StatusCode(500);
        }
    }

    /// <summary>
    /// Toggle notifications on/off for the current user.
    /// </summary>
    [HttpPost("Notifications/Toggle")]
    [Authorize]
    public async Task<ActionResult> ToggleNotifications()
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl)) return NotFound();

        try
        {
            var response = await HttpClient.PostAsync(
                AppendUserId($"{baseUrl}/api/notifications/toggle"),
                new StringContent(""));
            var body = await response.Content.ReadAsStringAsync();
            return new ContentResult { Content = body, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Discover] Failed to toggle notifications: {Error}", ex.Message);
            return StatusCode(500);
        }
    }

    /// <summary>
    /// Allowlist check for the ImageProxy forward target. Only TheTVDB hosts are
    /// permitted (exact "thetvdb.com" or any "*.thetvdb.com" subdomain).
    /// </summary>
    private static bool IsAllowedImageHost(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            return false;
        }

        if (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)
        {
            return false;
        }

        var host = uri.Host;
        return string.Equals(host, "thetvdb.com", StringComparison.OrdinalIgnoreCase)
               || host.EndsWith(".thetvdb.com", StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// Lower-case hex MD5 of the raw string, matching the backend's cacheKey scheme.
    /// </summary>
    private static string Md5Hex(string value)
    {
        using var md5 = System.Security.Cryptography.MD5.Create();
        var bytes = md5.ComputeHash(System.Text.Encoding.UTF8.GetBytes(value));
        return Convert.ToHexString(bytes).ToLowerInvariant();
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
