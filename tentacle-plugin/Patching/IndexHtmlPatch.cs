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

            var noCache = "<meta http-equiv=\"Cache-Control\" content=\"no-cache, no-store, must-revalidate\" /><meta http-equiv=\"Pragma\" content=\"no-cache\" /><meta http-equiv=\"Expires\" content=\"0\" />";

            // Critical overrides injected as inline <style> at end of body — always wins cascade
            var inlineOverrides = @"<style id=""tentacle-overrides"">
.detailPagePrimaryContainer{max-width:800px!important;margin-left:auto!important;margin-right:auto!important;gap:1.5em!important}
.detailImageContainer.hide-mobile{max-width:200px!important;width:200px!important;flex:0 0 200px!important}
.detailRibbon.padded-left{padding-left:0!important}
.detailRibbon.padded-right{padding-right:0!important}
.itemDetailPage #itemBackdrop{height:100px!important;min-height:100px!important;max-height:100px!important}
.itemDetailPage .detailLogo{display:none!important}
.itemDetailPage .detailPageWrapperContainer{padding-top:2em!important;margin-top:0!important}
.detailPagePrimaryContent{max-width:800px!important;margin-left:auto!important;margin-right:auto!important}
.detailPageSecondaryContainer{max-width:800px!important;margin-left:auto!important;margin-right:auto!important}
.itemDetailPage .detailImageContainer .cardBox{border-radius:12px!important;overflow:hidden!important;box-shadow:0 8px 32px rgba(0,0,0,0.4)!important}
.itemDetailPage .detailImageContainer img{border-radius:12px!important}
.itemDetailPage .listItem{border-radius:8px!important}
.itemDetailPage .listItem:hover{background:rgba(255,255,255,0.05)!important}
.itemDetailPage .detailButton{border-radius:8px!important}
html,body,#reactRoot,.backdropContainer,.backgroundContainer,.mainAnimatedPages,.skinBody,.page,.mainAnimatedPage,.libraryPage,.itemDetailPage,.noBackdropTransparency,#itemDetailPage,#indexPage{background:#0F0D1A!important;background-color:#0F0D1A!important}
.detailPageWrapperContainer,#itemBackdrop{background:transparent!important;background-color:transparent!important}
</style>";

            content = content
                .Replace("</head>", $"{noCache}{cssTag}{discoverCssTag}{detailsCssTag}{mdblistCssTag}{navbarCssTag}{mediabarCssTag}{searchCssTag}{livetvCssTag}</head>")
                .Replace("</body>", $"{inlineOverrides}{mdblistJsTag}{tmdbJsTag}{navbarJsTag}{mediabarJsTag}{jsTag}{discoverJsTag}{searchJsTag}{livetvJsTag}{detailsJsTag}</body>");

            var bytes = System.Text.Encoding.UTF8.GetBytes(content);
            __result = new TransformedFileInfo(__result, bytes);
        }
        catch
        {
        }
    }

}
