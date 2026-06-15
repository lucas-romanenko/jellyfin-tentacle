using System.Net.Http;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Shared helper for fetching the secret-gated /api/settings/plugin-keys endpoint
/// from the Tentacle backend. Sends the shared X-Tentacle-Secret header (copied from
/// the Tentacle dashboard) when configured.
/// </summary>
public static class PluginKeysClient
{
    /// <summary>
    /// GET <paramref name="url"/> with the X-Tentacle-Secret header attached
    /// (when a secret is configured) and return the response body as a string.
    /// Throws on non-success status, mirroring <see cref="HttpClient.GetStringAsync(string)"/>.
    /// </summary>
    public static async Task<string> GetSecuredStringAsync(HttpClient client, string url)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, url);
        var secret = Plugin.Instance?.Configuration?.TentacleSecret;
        if (!string.IsNullOrEmpty(secret))
        {
            request.Headers.Add("X-Tentacle-Secret", secret);
        }

        using var response = await client.SendAsync(request).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadAsStringAsync().ConfigureAwait(false);
    }
}
