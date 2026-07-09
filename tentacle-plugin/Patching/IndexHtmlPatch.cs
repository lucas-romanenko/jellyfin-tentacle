using System.Reflection;
using HarmonyLib;
using Microsoft.Extensions.FileProviders;
using Microsoft.Extensions.FileProviders.Physical;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Patching;

/// <summary>
/// Harmony postfix on PhysicalFileProvider.GetFileInfo to inject
/// Tentacle CSS/JS into Jellyfin's index.html and discover tab
/// content into the home-html chunk at serve-time.
/// </summary>
public static class IndexHtmlPatch
{
    private static readonly Harmony HarmonyInstance = new("jellyfin.plugin.tentacle");
    private static ILogger? _logger;

    // Stable cache-buster for the plugin process lifetime so browsers cache injected
    // JS/CSS instead of re-downloading them on every page load. Derived from the plugin
    // assembly version (falling back to process start), so it only changes after a
    // plugin update / Jellyfin restart.
    private static readonly string CacheBust =
        (typeof(IndexHtmlPatch).Assembly.GetName().Version?.ToString() ?? "0")
            + "-" + DateTimeOffset.UtcNow.ToUnixTimeSeconds();

    // Cache of the transformed index.html bytes, keyed by the original content so a
    // changed source (Jellyfin update) invalidates it. Avoids re-running the string
    // replacements on every request.
    private static string? _cachedSourceHash;
    private static byte[]? _cachedTransformed;
    private static readonly object _transformLock = new();

    public static void SetupPatches(ILogger? logger = null)
    {
        _logger = logger;
        var targetMethod = typeof(PhysicalFileProvider).GetMethod(
            nameof(PhysicalFileProvider.GetFileInfo),
            BindingFlags.Public | BindingFlags.Instance);

        if (targetMethod == null)
        {
            logger?.LogError("[Tentacle] Could not find PhysicalFileProvider.GetFileInfo to patch");
            return;
        }

        var postfix = new HarmonyMethod(typeof(IndexHtmlPatch).GetMethod(
            nameof(Postfix),
            BindingFlags.NonPublic | BindingFlags.Static));

        HarmonyInstance.Patch(targetMethod, postfix: postfix);
        logger?.LogInformation("[Tentacle] Harmony patch applied to PhysicalFileProvider.GetFileInfo");
    }

    private static void Postfix(string subpath, ref IFileInfo __result)
    {
        if (__result == null || !__result.Exists)
        {
            return;
        }

        var trimmed = subpath.TrimStart('/');

        if (trimmed.Equals("index.html", StringComparison.OrdinalIgnoreCase))
        {
            PatchIndexHtml(ref __result);
        }
    }

    /// <summary>
    /// Inject CSS/JS tags into index.html.
    /// </summary>
    private static void PatchIndexHtml(ref IFileInfo __result)
    {
        try
        {
            string content;
            using (var stream = __result.CreateReadStream())
            using (var reader = new StreamReader(stream))
            {
                content = reader.ReadToEnd();
            }

            if (!content.Contains("Jellyfin") || !content.Contains("</head>"))
            {
                return;
            }

            if (content.Contains("tentacle-home"))
            {
                return;
            }

            // Serve a cached transformation when the source index.html is unchanged so we
            // don't re-run the string replacements (and don't change the cache-buster) on
            // every request — that was forcing browsers to re-download all injected assets.
            var sourceHash = content.Length + ":" + content.GetHashCode();
            lock (_transformLock)
            {
                if (_cachedTransformed != null && _cachedSourceHash == sourceHash)
                {
                    __result = new TransformedFileInfo(__result, _cachedTransformed);
                    return;
                }
            }

            var cacheBust = CacheBust;
            var cssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/home.css?v={cacheBust}\" />";
            var jsTag = $"<script src=\"/Tentacle/home.js?v={cacheBust}\" defer></script>";
            var discoverCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/discover.css?v={cacheBust}\" />";
            var discoverJsTag = $"<script src=\"/Tentacle/discover.js?v={cacheBust}\" defer></script>";
            var detailsCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/details.css?v={cacheBust}\" />";
            var detailsJsTag = $"<script src=\"/Tentacle/details.js?v={cacheBust}\" defer></script>";
            var mdblistCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/mdblist.css?v={cacheBust}\" />";
            var mdblistJsTag = $"<script src=\"/Tentacle/mdblist.js?v={cacheBust}\" defer></script>";
            var tmdbJsTag = $"<script src=\"/Tentacle/tmdb.js?v={cacheBust}\" defer></script>";
            var navbarCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/navbar.css?v={cacheBust}\" />";
            var navbarJsTag = $"<script src=\"/Tentacle/navbar.js?v={cacheBust}\" defer></script>";
            var mediabarCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/mediabar.css?v={cacheBust}\" />";
            var mediabarJsTag = $"<script src=\"/Tentacle/mediabar.js?v={cacheBust}\" defer></script>";
            var searchCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/search.css?v={cacheBust}\" />";
            var searchJsTag = $"<script src=\"/Tentacle/search.js?v={cacheBust}\" defer></script>";
            var livetvCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/livetv.css?v={cacheBust}\" />";
            var livetvJsTag = $"<script src=\"/Tentacle/livetv.js?v={cacheBust}\" defer></script>";
            var favoritesCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/favorites.css?v={cacheBust}\" />";
            var favoritesJsTag = $"<script src=\"/Tentacle/favorites.js?v={cacheBust}\" defer></script>";
            var notifCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/notifications.css?v={cacheBust}\" />";
            var notifJsTag = $"<script src=\"/Tentacle/notifications.js?v={cacheBust}\" defer></script>";

            content = content
                .Replace("</head>", $"{cssTag}{discoverCssTag}{detailsCssTag}{mdblistCssTag}{navbarCssTag}{mediabarCssTag}{searchCssTag}{livetvCssTag}{favoritesCssTag}{notifCssTag}</head>")
                .Replace("</body>", $"{mdblistJsTag}{tmdbJsTag}{navbarJsTag}{mediabarJsTag}{jsTag}{discoverJsTag}{searchJsTag}{livetvJsTag}{favoritesJsTag}{detailsJsTag}{notifJsTag}</body>");

            var bytes = System.Text.Encoding.UTF8.GetBytes(content);
            lock (_transformLock)
            {
                _cachedTransformed = bytes;
                _cachedSourceHash = sourceHash;
            }
            __result = new TransformedFileInfo(__result, bytes);
        }
        catch (Exception ex)
        {
            _logger?.LogWarning(ex, "[Tentacle] Failed to inject Tentacle assets into index.html");
        }
    }

}
