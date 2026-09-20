using System;
using System.Linq;
using Microsoft.AspNetCore.Http;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// Cache policy for the JS/CSS that <see cref="Patching.IndexHtmlPatch"/> injects.
/// Every injected tag carries <c>?v=&lt;CacheBust&gt;</c>, a stamp that changes on every
/// plugin update and server restart, so a response to the current stamp can never go
/// stale and may be cached indefinitely. Without the current stamp it must not be.
/// </summary>
internal static class AssetCaching
{
    public const string Immutable = "public, max-age=31536000, immutable";
    public const string NoStore = "no-cache, no-store, must-revalidate";

    public static string CacheControlFor(HttpRequest request)
    {
        var stamp = request.Query["v"].FirstOrDefault();
        return string.Equals(stamp, Patching.IndexHtmlPatch.CacheBust, StringComparison.Ordinal)
            ? Immutable
            : NoStore;
    }
}
