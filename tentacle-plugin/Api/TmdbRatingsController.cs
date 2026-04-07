using System.Collections.Concurrent;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// API controller that proxies TMDB episode/season ratings lookups.
/// Fetches the TMDB API key from the Tentacle backend settings.
/// </summary>
[ApiController]
[Route("Tentacle/Tmdb")]
public class TentacleTmdbController : ControllerBase
{
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<TentacleTmdbController> _logger;

    private static readonly TimeSpan CacheDuration = TimeSpan.FromHours(24);
    private static readonly TimeSpan SettingsCacheDuration = TimeSpan.FromMinutes(10);

    // In-memory caches with 24h TTL
    private static readonly ConcurrentDictionary<string, (string Data, DateTime CachedAt)> _episodeCache = new();
    private static readonly ConcurrentDictionary<string, (string Data, DateTime CachedAt)> _seasonCache = new();

    // Cache for the TMDB token fetched from Tentacle backend
    private static string? _cachedToken;
    private static DateTime _tokenCacheExpiry = DateTime.MinValue;
    private static readonly SemaphoreSlim _tokenLock = new(1, 1);

    public TentacleTmdbController(
        IHttpClientFactory httpClientFactory,
        ILogger<TentacleTmdbController> logger)
    {
        _httpClientFactory = httpClientFactory;
        _logger = logger;
    }

    /// <summary>
    /// Clear cached token and rating data.
    /// </summary>
    public static void ClearCache()
    {
        _cachedToken = null;
        _tokenCacheExpiry = DateTime.MinValue;
        _episodeCache.Clear();
        _seasonCache.Clear();
    }

    /// <summary>
    /// Get TMDB rating for a specific episode.
    /// </summary>
    /// <param name="seriesId">TMDB series ID.</param>
    /// <param name="seasonNumber">Season number.</param>
    /// <param name="episodeNumber">Episode number.</param>
    [HttpGet("EpisodeRating")]
    public async Task<ActionResult> GetEpisodeRating(
        [FromQuery] string tmdbId,
        [FromQuery] int season,
        [FromQuery] int episode)
    {
        if (string.IsNullOrWhiteSpace(tmdbId))
        {
            return BadRequest(new { error = "tmdbId is required" });
        }

        var seriesId = tmdbId.Trim();
        var seasonNumber = season;
        var episodeNumber = episode;
        var cacheKey = $"ep:{seriesId}:{seasonNumber}:{episodeNumber}";
        if (_episodeCache.TryGetValue(cacheKey, out var cached) && DateTime.UtcNow - cached.CachedAt < CacheDuration)
        {
            return Content(cached.Data, "application/json");
        }

        var token = await GetTmdbToken();
        if (string.IsNullOrEmpty(token))
        {
            return Ok(new TmdbEpisodeRatingResponse());
        }

        try
        {
            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(10);

            var url = $"https://api.themoviedb.org/3/tv/{seriesId}/season/{seasonNumber}/episode/{episodeNumber}";
            AddTmdbAuth(client, url, token, out var requestUrl);

            var response = await client.GetAsync(requestUrl);
            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning("[Tentacle TMDB] API returned {Status} for episode {Series}/S{Season}E{Episode}",
                    response.StatusCode, seriesId, seasonNumber, episodeNumber);
                return Ok(new TmdbEpisodeRatingResponse());
            }

            var json = await response.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            var result = new TmdbEpisodeRatingResponse
            {
                VoteAverage = root.TryGetProperty("vote_average", out var va) ? va.GetDouble() : null,
                VoteCount = root.TryGetProperty("vote_count", out var vc) ? vc.GetInt32() : null,
            };

            var resultJson = JsonSerializer.Serialize(result, new JsonSerializerOptions
            {
                PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
                DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
            });

            _episodeCache[cacheKey] = (resultJson, DateTime.UtcNow);
            return Content(resultJson, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle TMDB] Failed to fetch episode rating: {Error}", ex.Message);
            return Ok(new TmdbEpisodeRatingResponse());
        }
    }

    /// <summary>
    /// Get TMDB ratings for all episodes in a season (batch).
    /// </summary>
    /// <param name="seriesId">TMDB series ID.</param>
    /// <param name="seasonNumber">Season number.</param>
    [HttpGet("SeasonRatings")]
    public async Task<ActionResult> GetSeasonRatings(
        [FromQuery] string tmdbId,
        [FromQuery] int season)
    {
        if (string.IsNullOrWhiteSpace(tmdbId))
        {
            return BadRequest(new { error = "tmdbId is required" });
        }

        var seriesId = tmdbId.Trim();
        var seasonNumber = season;
        var cacheKey = $"season:{seriesId}:{seasonNumber}";
        if (_seasonCache.TryGetValue(cacheKey, out var cached) && DateTime.UtcNow - cached.CachedAt < CacheDuration)
        {
            return Content(cached.Data, "application/json");
        }

        var token = await GetTmdbToken();
        if (string.IsNullOrEmpty(token))
        {
            return Ok(new TmdbSeasonRatingsResponse());
        }

        try
        {
            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(10);

            var url = $"https://api.themoviedb.org/3/tv/{seriesId}/season/{seasonNumber}";
            AddTmdbAuth(client, url, token, out var requestUrl);

            var response = await client.GetAsync(requestUrl);
            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning("[Tentacle TMDB] API returned {Status} for season {Series}/S{Season}",
                    response.StatusCode, seriesId, seasonNumber);
                return Ok(new TmdbSeasonRatingsResponse());
            }

            var json = await response.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            var episodes = new List<TmdbEpisodeRating>();
            if (root.TryGetProperty("episodes", out var episodesArray))
            {
                foreach (var ep in episodesArray.EnumerateArray())
                {
                    episodes.Add(new TmdbEpisodeRating
                    {
                        EpisodeNumber = ep.TryGetProperty("episode_number", out var epNum) ? epNum.GetInt32() : 0,
                        VoteAverage = ep.TryGetProperty("vote_average", out var va) ? va.GetDouble() : null,
                        VoteCount = ep.TryGetProperty("vote_count", out var vc) ? vc.GetInt32() : null,
                        Name = ep.TryGetProperty("name", out var name) ? name.GetString() : null,
                    });
                }
            }

            var result = new TmdbSeasonRatingsResponse { Episodes = episodes };

            var resultJson = JsonSerializer.Serialize(result, new JsonSerializerOptions
            {
                PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
                DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
            });

            _seasonCache[cacheKey] = (resultJson, DateTime.UtcNow);
            return Content(resultJson, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle TMDB] Failed to fetch season ratings: {Error}", ex.Message);
            return Ok(new TmdbSeasonRatingsResponse());
        }
    }

    /// <summary>
    /// Configure HTTP client auth for TMDB.
    /// Bearer token (v4, starts with "eyJ") uses Authorization header.
    /// API key (v3) is appended as query parameter.
    /// </summary>
    private static void AddTmdbAuth(HttpClient client, string baseUrl, string token, out string requestUrl)
    {
        if (token.StartsWith("eyJ", StringComparison.Ordinal))
        {
            // v4 Bearer token
            client.DefaultRequestHeaders.Authorization =
                new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
            requestUrl = baseUrl;
        }
        else
        {
            // v3 API key
            requestUrl = baseUrl + (baseUrl.Contains('?') ? "&" : "?") + $"api_key={token}";
        }
    }

    /// <summary>
    /// Fetch the TMDB token from the Tentacle backend settings.
    /// Cached for 10 minutes. Checks tmdb_bearer_token first, then tmdb_api_key.
    /// </summary>
    private async Task<string?> GetTmdbToken()
    {
        if (_cachedToken != null && DateTime.UtcNow < _tokenCacheExpiry)
        {
            return _cachedToken;
        }

        await _tokenLock.WaitAsync();
        try
        {
            // Double-check after acquiring lock
            if (_cachedToken != null && DateTime.UtcNow < _tokenCacheExpiry)
            {
                return _cachedToken;
            }

            var tentacleUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
            if (string.IsNullOrEmpty(tentacleUrl))
            {
                _logger.LogWarning("[Tentacle TMDB] TentacleUrl not configured");
                return null;
            }

            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(10);

            var response = await client.GetStringAsync($"{tentacleUrl}/api/settings/plugin-keys");
            using var doc = JsonDocument.Parse(response);

            // Try bearer token first (v4), then API key (v3)
            string? token = null;
            if (doc.RootElement.TryGetProperty("tmdb_bearer_token", out var bearerElement))
            {
                token = bearerElement.GetString();
            }

            if (string.IsNullOrEmpty(token) && doc.RootElement.TryGetProperty("tmdb_api_key", out var apiKeyElement))
            {
                token = apiKeyElement.GetString();
            }

            if (!string.IsNullOrEmpty(token))
            {
                _cachedToken = token;
                _tokenCacheExpiry = DateTime.UtcNow.Add(SettingsCacheDuration);
                return token;
            }

            _logger.LogDebug("[Tentacle TMDB] No TMDB token found in settings");
            return null;
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle TMDB] Failed to fetch token from settings: {Error}", ex.Message);
            return null;
        }
        finally
        {
            _tokenLock.Release();
        }
    }
}

// --- Model Classes ---

/// <summary>
/// Response for a single episode rating.
/// </summary>
public class TmdbEpisodeRatingResponse
{
    [JsonPropertyName("voteAverage")]
    public double? VoteAverage { get; set; }

    [JsonPropertyName("voteCount")]
    public int? VoteCount { get; set; }
}

/// <summary>
/// Response for all episode ratings in a season.
/// </summary>
public class TmdbSeasonRatingsResponse
{
    [JsonPropertyName("episodes")]
    public List<TmdbEpisodeRating> Episodes { get; set; } = new();
}

/// <summary>
/// Rating data for a single episode within a season batch response.
/// </summary>
public class TmdbEpisodeRating
{
    [JsonPropertyName("episodeNumber")]
    public int EpisodeNumber { get; set; }

    [JsonPropertyName("voteAverage")]
    public double? VoteAverage { get; set; }

    [JsonPropertyName("voteCount")]
    public int? VoteCount { get; set; }

    [JsonPropertyName("name")]
    public string? Name { get; set; }
}
