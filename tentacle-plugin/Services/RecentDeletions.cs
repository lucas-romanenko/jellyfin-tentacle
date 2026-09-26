using System;
using System.Collections.Concurrent;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Deletions this server has forwarded to the Tentacle backend, so the backend
/// can ask whether a delete notification is real before acting on it.
/// </summary>
/// <remarks>
/// The backend's DELETE /api/library/item/{type}/{id} took no authentication: one
/// stray request on the LAN removed a title from the catalogue and from every
/// user's playlist (#139). The plugin has no shared secret with the backend, so
/// the backend confirms instead: POST /Tentacle/Deletions/{type}/{id}/Confirm with
/// the server's API key. Each forwarded deletion confirms once, for 15 minutes.
/// </remarks>
public static class RecentDeletions
{
    private static readonly TimeSpan Lifetime = TimeSpan.FromMinutes(15);

    private static readonly ConcurrentDictionary<string, (int Count, DateTime Expiry)> Entries = new();

    private static string Key(string mediaType, string tmdbId) =>
        $"{mediaType?.ToLowerInvariant()}:{tmdbId}";

    /// <summary>Records a deletion about to be forwarded to the backend.</summary>
    public static void Record(string mediaType, string tmdbId)
    {
        var now = DateTime.UtcNow;
        foreach (var entry in Entries)
        {
            if (entry.Value.Expiry <= now)
            {
                Entries.TryRemove(entry.Key, out _);
            }
        }

        Entries.AddOrUpdate(
            Key(mediaType, tmdbId),
            _ => (1, now + Lifetime),
            (_, old) => (old.Count + 1, now + Lifetime));
    }

    /// <summary>True, once per recorded deletion, while it is fresh.</summary>
    public static bool TryConsume(string mediaType, string tmdbId)
    {
        var key = Key(mediaType, tmdbId);
        while (Entries.TryGetValue(key, out var current))
        {
            if (current.Expiry <= DateTime.UtcNow)
            {
                Entries.TryRemove(key, out _);
                return false;
            }

            var updated = current.Count > 1
                ? Entries.TryUpdate(key, (current.Count - 1, current.Expiry), current)
                : Entries.TryRemove(new System.Collections.Generic.KeyValuePair<string, (int, DateTime)>(key, current));
            if (updated)
            {
                return true;
            }
        }

        return false;
    }
}
