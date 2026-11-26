using System.Collections.Concurrent;
using System.Net.Http;
using System.Text.Json;
using Microsoft.AspNetCore.Http;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Each person's subtitle and skip choices, as Tentacle keeps them
/// (<c>GET /api/watch/prefs</c>, asked with the person's own token). Kept
/// <see cref="KeepFor"/>; forgotten at once when Tentacle says they changed
/// (<c>/Tentacle/Notify</c> topic "watch").
/// </summary>
public static class WatchPrefs
{
    /// <summary>How long one person's answer is kept.</summary>
    public static readonly TimeSpan KeepFor = TimeSpan.FromMinutes(10);

    private static readonly ConcurrentDictionary<Guid, (Prefs Value, DateTime Until)> Cache = new();
    private static readonly HttpClient Client = new() { Timeout = TimeSpan.FromSeconds(4) };

    /// <summary>One person's choices.</summary>
    /// <param name="Languages">ISO 639-2/B codes, first = their main language.</param>
    /// <param name="SubtitleMode">off | always | foreign.</param>
    /// <param name="SkipIntro">button | auto | off.</param>
    /// <param name="SkipCredits">button | auto | off.</param>
    public sealed record Prefs(IReadOnlyList<string> Languages, string SubtitleMode, string SkipIntro, string SkipCredits);

    /// <summary>Forget these people's answers (they changed something).</summary>
    public static void Forget(IEnumerable<Guid> userIds)
    {
        foreach (var id in userIds)
        {
            Cache.TryRemove(id, out _);
        }
    }

    /// <summary>For the tests: a person's answer as if Tentacle had given it.</summary>
    public static void Remember(Guid userId, Prefs prefs) => Cache[userId] = (prefs, DateTime.UtcNow + KeepFor);

    /// <summary>
    /// The person's choices; null when Tentacle can't say (not set up, not
    /// answering): then nothing is changed for them.
    /// </summary>
    public static async Task<Prefs?> ForAsync(Guid userId, HttpRequest request)
    {
        if (userId == Guid.Empty)
        {
            return null;
        }

        if (Cache.TryGetValue(userId, out var hit) && DateTime.UtcNow < hit.Until)
        {
            return hit.Value;
        }

        var baseUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
        if (string.IsNullOrEmpty(baseUrl))
        {
            return null;
        }

        try
        {
            var body = await PluginKeysClient.GetSecuredStringAsync(Client, $"{baseUrl}/api/watch/prefs", request).ConfigureAwait(false);
            var prefs = Parse(body);
            if (prefs is not null)
            {
                Cache[userId] = (prefs, DateTime.UtcNow + KeepFor);
            }

            return prefs;
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>Tentacle's answer as <see cref="Prefs"/>; null when it isn't one.</summary>
    public static Prefs? Parse(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        if (!root.TryGetProperty("subtitle_languages", out var langs) || langs.ValueKind != JsonValueKind.Array)
        {
            return null;
        }

        string Text(string name, string fallback)
            => root.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? fallback : fallback;

        return new Prefs(
            langs.EnumerateArray().Where(e => e.ValueKind == JsonValueKind.String).Select(e => e.GetString()!).ToList(),
            Text("subtitle_mode", "off"),
            Text("skip_intro", "button"),
            Text("skip_credits", "button"));
    }
}
