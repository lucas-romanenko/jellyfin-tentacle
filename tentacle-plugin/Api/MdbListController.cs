using System.Text.Json;
using System.Text.Json.Serialization;
using Jellyfin.Plugin.Tentacle.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// Proxies MDBList rating lookups. Fetches API key from Tentacle backend.
/// </summary>
[ApiController]
[Route("Tentacle/MdbList")]
public class TentacleMdbListController : ControllerBase
{
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<TentacleMdbListController> _logger;

    private static readonly TimeSpan CacheDuration = TimeSpan.FromDays(7);
    private static readonly TimeSpan SettingsCacheDuration = TimeSpan.FromMinutes(10);

    private static string? _cachedApiKey;
    private static DateTime _apiKeyCacheExpiry = DateTime.MinValue;
    private static readonly SemaphoreSlim _apiKeyLock = new(1, 1);

    public TentacleMdbListController(
        IHttpClientFactory httpClientFactory,
        ILogger<TentacleMdbListController> logger)
    {
        _httpClientFactory = httpClientFactory;
        _logger = logger;
    }

    public static void ClearSettingsCache()
    {
        _cachedApiKey = null;
        _apiKeyCacheExpiry = DateTime.MinValue;
    }

    /// <summary>
    /// Get ratings for a movie or show by TMDB ID.
    /// </summary>
    [HttpGet("Ratings")]
    public async Task<ActionResult> GetRatings(
        [FromQuery] string type,
        [FromQuery] string tmdbId)
    {
        if (string.IsNullOrWhiteSpace(type) || string.IsNullOrWhiteSpace(tmdbId))
        {
            return BadRequest(new { error = "type and tmdbId are required" });
        }

        type = type.Trim().ToLowerInvariant();
        if (type != "movie" && type != "show")
        {
            return BadRequest(new { error = "type must be 'movie' or 'show'" });
        }

        var cacheKey = $"{type}:{tmdbId.Trim()}";
        if (MdbListCacheService.TryGet(cacheKey, CacheDuration, out var cached))
        {
            return Content(cached!, "application/json");
        }

        var apiKey = await GetMdbListApiKey();
        if (string.IsNullOrEmpty(apiKey))
        {
            return Ok(new { success = false, error = "No MDBList API key configured", ratings = Array.Empty<object>() });
        }

        try
        {
            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(15);
            client.DefaultRequestHeaders.UserAgent.ParseAdd("Tentacle/1.0");

            var url = $"https://api.mdblist.com/tmdb/{Uri.EscapeDataString(type)}/{Uri.EscapeDataString(tmdbId.Trim())}?apikey={Uri.EscapeDataString(apiKey)}";
            using var response = await client.GetAsync(url);

            if ((int)response.StatusCode == 429)
            {
                return Ok(new { success = false, error = "MDBList rate limit reached", ratings = Array.Empty<object>() });
            }

            if (!response.IsSuccessStatusCode)
            {
                return Ok(new { success = false, error = $"MDBList returned {(int)response.StatusCode}", ratings = Array.Empty<object>() });
            }

            var json = await response.Content.ReadAsStringAsync();
            var data = JsonSerializer.Deserialize<MdbListApiResponse>(json, JsonOpts);

            var result = new { success = true, ratings = data?.Ratings ?? new List<MdbListRating>() };
            var resultJson = JsonSerializer.Serialize(result, JsonOpts);

            MdbListCacheService.Set(cacheKey, resultJson);
            return Content(resultJson, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle MdbList] Failed for {Type}:{TmdbId}: {Error}", type, tmdbId, ex.Message);
            return Ok(new { success = false, error = ex.Message, ratings = Array.Empty<object>() });
        }
    }

    private async Task<string?> GetMdbListApiKey()
    {
        if (_cachedApiKey != null && DateTime.UtcNow < _apiKeyCacheExpiry)
            return _cachedApiKey;

        await _apiKeyLock.WaitAsync();
        try
        {
            if (_cachedApiKey != null && DateTime.UtcNow < _apiKeyCacheExpiry)
                return _cachedApiKey;

            var tentacleUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
            if (string.IsNullOrEmpty(tentacleUrl)) return null;

            var client = _httpClientFactory.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(10);
            var response = await client.GetStringAsync($"{tentacleUrl}/api/settings/raw");
            using var doc = JsonDocument.Parse(response);

            if (doc.RootElement.TryGetProperty("mdblist_api_key", out var el))
            {
                var key = el.GetString();
                if (!string.IsNullOrEmpty(key))
                {
                    _cachedApiKey = key;
                    _apiKeyCacheExpiry = DateTime.UtcNow.Add(SettingsCacheDuration);
                    return key;
                }
            }
            return null;
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle MdbList] Failed to get API key: {Error}", ex.Message);
            return null;
        }
        finally
        {
            _apiKeyLock.Release();
        }
    }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
}

public class MdbListRating
{
    [JsonPropertyName("source")]
    public string? Source { get; set; }

    [JsonPropertyName("value")]
    public double? Value { get; set; }

    [JsonPropertyName("score")]
    public double? Score { get; set; }

    [JsonPropertyName("votes")]
    public int? Votes { get; set; }

    [JsonPropertyName("url")]
    public string? Url { get; set; }
}

internal class MdbListApiResponse
{
    [JsonPropertyName("ratings")]
    public List<MdbListRating>? Ratings { get; set; }
}
