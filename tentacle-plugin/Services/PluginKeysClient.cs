using System;
using System.Linq;
using System.Net.Http;
using Microsoft.AspNetCore.Http;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Shared helper for fetching the token-gated /api/settings/plugin-keys endpoint from
/// the Tentacle backend. Forwards the *caller's* Jellyfin access token (which the
/// backend validates against Jellyfin), so no manually-configured shared secret is
/// required — zero config for end users.
/// </summary>
public static class PluginKeysClient
{
    /// <summary>
    /// GET <paramref name="url"/> with the caller's Jellyfin token forwarded as
    /// ?api_key= and return the response body. Throws on non-success status,
    /// mirroring <see cref="HttpClient.GetStringAsync(string)"/>.
    /// </summary>
    public static async Task<string> GetSecuredStringAsync(HttpClient client, string url, HttpRequest request)
    {
        var token = ExtractToken(request);
        if (!string.IsNullOrEmpty(token))
        {
            var sep = url.Contains('?') ? '&' : '?';
            url = $"{url}{sep}api_key={Uri.EscapeDataString(token)}";
        }

        return await client.GetStringAsync(url).ConfigureAwait(false);
    }

    /// <summary>
    /// Pulls the Jellyfin access token off the (already Jellyfin-authenticated) inbound
    /// request — from ?api_key=, the X-Emby-Token / X-MediaBrowser-Token headers, or the
    /// Authorization / X-Emby-Authorization header's Token="..." field.
    /// </summary>
    private static string ExtractToken(HttpRequest req)
    {
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
}
