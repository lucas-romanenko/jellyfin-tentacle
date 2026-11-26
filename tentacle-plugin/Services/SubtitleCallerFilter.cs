using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Remembers who asked Jellyfin for a title's subtitles, per media source:
/// a subtitle (<c>/Videos/{id}/{source}/Subtitles/…</c>) or a play
/// (PlaybackInfo, which starts Jellyfin extracting every text track in the
/// background). Jellyfin extracts outside those requests, and
/// <see cref="VodSubtitleExtractionPatch"/> asks Tentacle for its copy as that person.
/// </summary>
public sealed class SubtitleCallerFilter : IActionFilter
{
    /// <inheritdoc />
    public void OnActionExecuting(ActionExecutingContext context)
    {
        if (context.ActionDescriptor is not ControllerActionDescriptor action
            || !((action.ControllerName == "Subtitle" && action.ActionName.StartsWith("GetSubtitle", StringComparison.Ordinal))
                 || (action.ControllerName == "MediaInfo" && action.ActionName.EndsWith("PlaybackInfo", StringComparison.Ordinal))))
        {
            return;
        }

        var token = PluginKeysClient.ExtractToken(context.HttpContext.Request);
        foreach (var name in new[] { "routeMediaSourceId", "mediaSourceId", "routeItemId", "itemId" })
        {
            if (context.ActionArguments.TryGetValue(name, out var v) && v is not null)
            {
                VodSubtitleExtractionPatch.RememberCaller(v.ToString() ?? string.Empty, token);
            }
        }

        // PlaybackInfo's body names the version played.
        if (context.ActionArguments.TryGetValue("playbackInfoDto", out var body) && body is not null
            && body.GetType().GetProperty("MediaSourceId")?.GetValue(body) is string source)
        {
            VodSubtitleExtractionPatch.RememberCaller(source, token);
        }
    }

    /// <inheritdoc />
    public void OnActionExecuted(ActionExecutedContext context)
    {
    }
}
