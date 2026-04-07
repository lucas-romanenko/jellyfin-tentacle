using System.Collections.Concurrent;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// In-memory cache for MDBList ratings data.
/// Keyed by "{mediaType}:{id}" (e.g. "movie:tt1234567" or "show:tt1234567").
/// </summary>
public static class MdbListCacheService
{
    private static readonly ConcurrentDictionary<string, CacheEntry> _cache = new();

    /// <summary>
    /// Try to get a cached value. Returns true if found and not expired.
    /// </summary>
    public static bool TryGet(string key, TimeSpan maxAge, out string? data)
    {
        if (_cache.TryGetValue(key, out var entry) && DateTime.UtcNow - entry.CachedAt < maxAge)
        {
            data = entry.Data;
            return true;
        }

        data = null;
        return false;
    }

    /// <summary>
    /// Store a value in the cache.
    /// </summary>
    public static void Set(string key, string data)
    {
        _cache[key] = new CacheEntry(data, DateTime.UtcNow);
    }

    /// <summary>
    /// Clear all cached entries.
    /// </summary>
    public static void Clear()
    {
        _cache.Clear();
    }

    private record CacheEntry(string Data, DateTime CachedAt);
}
