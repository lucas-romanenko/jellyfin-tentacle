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

    public static void SetupPatches(ILogger? logger = null)
    {
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

            var cacheBust = DateTimeOffset.UtcNow.ToUnixTimeSeconds().ToString();
            var cssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/home.css?v={cacheBust}\" />";
            var jsTag = $"<script src=\"/Tentacle/home.js?v={cacheBust}\" defer></script>";
            var discoverCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/discover.css?v={cacheBust}\" />";
            var discoverJsTag = $"<script src=\"/Tentacle/discover.js?v={cacheBust}\" defer></script>";
            var detailsCssTag = $"<link rel=\"stylesheet\" href=\"/Tentacle/details.css?v={cacheBust}\" />";
            var detailsJsTag = $"<script src=\"/Tentacle/details.js?v={cacheBust}\" defer></script>";

            content = content
                .Replace("</head>", $"{cssTag}{discoverCssTag}{detailsCssTag}</head>")
                .Replace("</body>", $"{jsTag}{discoverJsTag}{detailsJsTag}</body>");

            var bytes = System.Text.Encoding.UTF8.GetBytes(content);
            __result = new TransformedFileInfo(__result, bytes);
        }
        catch
        {
        }
    }

}
