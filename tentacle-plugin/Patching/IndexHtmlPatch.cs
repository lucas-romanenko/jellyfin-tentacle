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
    // plugin update / Jellyfin restart. Also served at GET /Tentacle/Boot so open tabs
    // can detect they were loaded under an older generation and reload themselves.
    internal static readonly string CacheBust =
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

        // Jellyfin's "restart" (the dashboard button, POST /System/Restart, and a
        // plugin update's restart) reboots the server INSIDE the same process. The
        // previous plugin version's assembly stays loaded and so does its postfix,
        // so every such restart after an update stacked one more copy of the
        // injected tags into index.html. The older copies carry an older ?v= stamp,
        // which the staleness watchdog read as "server updated" and reloaded the
        // page — over and over. Remove every earlier Tentacle patch before adding
        // this one; Harmony keeps patch state process-wide, across loaded copies.
        try
        {
            var existing = Harmony.GetPatchInfo(targetMethod);
            var stale = existing?.Postfixes.Count(p => p.owner == HarmonyInstance.Id) ?? 0;
            if (stale > 0)
            {
                HarmonyInstance.Unpatch(targetMethod, HarmonyPatchType.All, HarmonyInstance.Id);
                logger?.LogInformation("[Tentacle] Removed {Count} index.html patch(es) left by an earlier plugin version in this process", stale);
            }
        }
        catch (Exception ex)
        {
            logger?.LogWarning(ex, "[Tentacle] Could not remove earlier index.html patches — the injection still strips their tags");
        }

        var postfix = new HarmonyMethod(typeof(IndexHtmlPatch).GetMethod(
            nameof(Postfix),
            BindingFlags.NonPublic | BindingFlags.Static))
        {
            // Run after any other postfix, so the tags this version injects are the
            // ones that end up in the page even if an old patch could not be removed.
            priority = Priority.Last,
        };

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

    // Any tag this plugin injects: <link …/Tentacle/x.css?v=…> or <script …/Tentacle/x.js?v=…></script>.
    private static readonly System.Text.RegularExpressions.Regex InjectedTag = new(
        "<link rel=\"stylesheet\" href=\"/Tentacle/[a-z]+\\.css\\?v=[^\"]*\" />"
        + "|<script src=\"/Tentacle/[a-z]+\\.js\\?v=[^\"]*\" defer></script>",
        System.Text.RegularExpressions.RegexOptions.Compiled);

    /// <summary>
    /// Cache key for a source file: its timestamp and length. Stable across process
    /// restarts, unlike a randomised string hash code (#58).
    /// </summary>
    private static string SourceKey(IFileInfo file) =>
        file.LastModified.UtcTicks.ToString(System.Globalization.CultureInfo.InvariantCulture)
        + ":" + file.Length.ToString(System.Globalization.CultureInfo.InvariantCulture);

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

            // Tags another copy of this patch already injected (see SetupPatches) are
            // replaced, never added to: exactly one set, carrying this version's stamp.
            // The old check looked for "tentacle-home", which the tags never contain.
            content = InjectedTag.Replace(content, string.Empty);

            // Serve a cached transformation when the source index.html is unchanged so we
            // don't re-run the string replacements (and don't change the cache-buster) on
            // every request — that was forcing browsers to re-download all injected assets.
            // Keyed on the source file's own metadata rather than content.GetHashCode(),
            // which .NET randomises per process and so missed once after every restart (#58).
            var sourceHash = SourceKey(__result);
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
