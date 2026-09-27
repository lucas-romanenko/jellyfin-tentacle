using System.Net.Http;
using System.Text.Json;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// Proxies Tentacle's music module (search, album / artist / song pages,
/// requests) for the Jellyfin web UI. Tentacle answers 404 unless both its music
/// module and its Jellyfin music integration are on, so nothing shows otherwise.
/// Status codes are forwarded so a refusal reaches the user with its reason.
/// </summary>
[ApiController]
[Route("[controller]")]
public class TentacleMusicController : ControllerBase
{
    private readonly ILogger<TentacleMusicController> _logger;

    // MusicBrainz allows one request a second, so a first look at an artist
    // with many releases can take a while; later looks come from Tentacle's cache.
    private static readonly HttpClient Client = new() { Timeout = TimeSpan.FromSeconds(90) };

    public TentacleMusicController(ILogger<TentacleMusicController> logger)
    {
        _logger = logger;
    }

    private static string GetTentacleUrl()
    {
        return Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/') ?? "";
    }

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

    /// <summary>Adds the caller's userId and Jellyfin token, which Tentacle verifies.</summary>
    private string WithCaller(string url)
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

    private async Task<ActionResult> Forward(HttpMethod method, string path, string? body = null)
    {
        var baseUrl = GetTentacleUrl();
        if (string.IsNullOrEmpty(baseUrl))
        {
            return StatusCode(503, new { detail = "Tentacle URL not configured" });
        }

        try
        {
            using var request = new HttpRequestMessage(method, WithCaller(baseUrl + path));
            if (body != null)
            {
                request.Content = new StringContent(body, System.Text.Encoding.UTF8, "application/json");
            }

            using var response = await Client.SendAsync(request).ConfigureAwait(false);
            var result = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return new ContentResult { Content = result, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Music] {Path} failed: {Error}", path, ex.Message);
            return StatusCode(502, new { detail = "Tentacle could not be reached." });
        }
    }

    [HttpGet("Config")]
    [Authorize]
    public Task<ActionResult> GetConfig() => Forward(HttpMethod.Get, "/api/music/config");

    [HttpGet("Search")]
    [Authorize]
    public Task<ActionResult> Search([FromQuery] string q) =>
        Forward(HttpMethod.Get, "/api/music/search?q=" + Uri.EscapeDataString(q ?? ""));

    [HttpGet("Album/{id}")]
    [Authorize]
    public Task<ActionResult> Album([FromRoute] string id) =>
        Guid.TryParse(id, out var g)
            ? Forward(HttpMethod.Get, "/api/music/album/" + g.ToString("D"))
            : Task.FromResult<ActionResult>(BadRequest(new { detail = "Not a MusicBrainz id" }));

    [HttpGet("Artist/{id}")]
    [Authorize]
    public Task<ActionResult> Artist([FromRoute] string id) =>
        Guid.TryParse(id, out var g)
            ? Forward(HttpMethod.Get, "/api/music/artist/" + g.ToString("D"))
            : Task.FromResult<ActionResult>(BadRequest(new { detail = "Not a MusicBrainz id" }));

    [HttpGet("Song")]
    [Authorize]
    public Task<ActionResult> Song([FromQuery] string title, [FromQuery] string artist) =>
        Guid.TryParse(artist, out var g)
            ? Forward(HttpMethod.Get, "/api/music/song?title=" + Uri.EscapeDataString(title ?? "") + "&artist=" + g.ToString("D"))
            : Task.FromResult<ActionResult>(BadRequest(new { detail = "Not a MusicBrainz id" }));

    [HttpPost("Request")]
    [Authorize]
    public Task<ActionResult> RequestAlbum([FromBody] JsonElement body) =>
        Forward(HttpMethod.Post, "/api/music/request", body.GetRawText());
}
