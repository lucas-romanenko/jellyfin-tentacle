using System.Net.Http;
using System.Text;
using System.Text.Json;
using Jellyfin.Plugin.Tentacle.Services;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// Subtitles &amp; skipping for Jellyfin Web's player (Inject/tentacle-subs.js):
/// the caller's own choices, where a title's subtitles are, and "Subtitles
/// wrong? Get another". Each forwards the caller's token to Tentacle, which
/// answers for that person only.
/// </summary>
[ApiController]
[Route("TentacleSubs")]
public class SubtitlesController : ControllerBase
{
    private static readonly HttpClient Quick = new() { Timeout = TimeSpan.FromSeconds(15) };
    // Getting another reads the title's sound first when Tentacle hasn't yet.
    private static readonly HttpClient Slow = new() { Timeout = TimeSpan.FromMinutes(20) };
    private readonly ILogger<SubtitlesController> _logger;

    public SubtitlesController(ILogger<SubtitlesController> logger)
    {
        _logger = logger;
    }

    private string? Url(string path)
    {
        var baseUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
        if (string.IsNullOrEmpty(baseUrl))
        {
            return null;
        }

        var token = PluginKeysClient.ExtractToken(Request);
        return $"{baseUrl}{path}{(path.Contains('?') ? '&' : '?')}api_key={Uri.EscapeDataString(token)}";
    }

    private async Task<ActionResult> Forward(HttpClient client, HttpMethod method, string path, string? body = null)
    {
        var url = Url(path);
        if (url is null)
        {
            return StatusCode(503, new { detail = "Tentacle isn't set up in the plugin" });
        }

        try
        {
            using var req = new HttpRequestMessage(method, url);
            if (body is not null)
            {
                req.Content = new StringContent(body, Encoding.UTF8, "application/json");
            }

            using var resp = await client.SendAsync(req).ConfigureAwait(false);
            var text = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
            return new ContentResult { Content = text, ContentType = "application/json", StatusCode = (int)resp.StatusCode };
        }
        catch (Exception ex)
        {
            _logger.LogInformation("[Tentacle] Subtitles request to Tentacle failed: {Error}", ex.Message);
            return StatusCode(502, new { detail = "Tentacle didn't answer. Try again in a minute." });
        }
    }

    /// <summary>The caller's subtitle and skip choices.</summary>
    [HttpGet("Prefs")]
    [Authorize]
    public Task<ActionResult> Prefs() => Forward(Quick, HttpMethod.Get, "/api/watch/prefs");

    /// <summary>Where a title's subtitles are (being read, ready, which).</summary>
    [HttpGet("Status/{itemId}")]
    [Authorize]
    public Task<ActionResult> Status([FromRoute] Guid itemId)
        => Forward(Quick, HttpMethod.Get, $"/api/subtitles/{itemId:N}/status");

    /// <summary>"Subtitles wrong? Get another": body {"language": "eng"}.</summary>
    [HttpPost("Another/{itemId}")]
    [Authorize]
    public Task<ActionResult> Another([FromRoute] Guid itemId, [FromBody] JsonElement body)
        => Forward(Slow, HttpMethod.Post, $"/api/subtitles/{itemId:N}/another", body.GetRawText());
}
