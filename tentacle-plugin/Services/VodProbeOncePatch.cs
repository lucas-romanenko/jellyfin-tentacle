using System.Reflection;
using HarmonyLib;
using Jellyfin.Database.Implementations.Entities;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Model.Entities;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// A Tentacle VOD title is probed once, not at every play.
///
/// Why: Jellyfin (MediaSourceManager.GetPlaybackMediaSources, 10.11) runs a
/// full metadata refresh with a remote ffprobe on EVERY PlaybackInfo of any
/// .strm, even when the item already has its streams from an earlier probe
/// (the check is on the ".strm" path alone). For a provider film that probe
/// is several range requests through Tentacle's relay to the provider, about
/// 3.4 s before Jellyfin even answers PlaybackInfo (measured Oct 2026, TV app,
/// direct play); the streams it finds are the ones it saved the first time.
///
/// How: a Harmony prefix sets <c>allowMediaProbe</c> to false for a Tentacle
/// VOD item (VodNoVideoCopyFilter.IsTentacleVod) that already has a video
/// stream. A title never probed (no streams yet) is probed as before, and
/// so are library files, Live TV and other .strm files. If a later Jellyfin
/// renames the method, nothing is patched and a warning is logged: VOD then
/// starts as before, a few seconds slower.
/// </summary>
public sealed class VodProbeOncePatch : IHostedService
{
    private static readonly Harmony HarmonyInstance = new("jellyfin.plugin.tentacle.vod.probeonce");

    private readonly ILogger<VodProbeOncePatch> _hostLogger;

    public VodProbeOncePatch(ILogger<VodProbeOncePatch> logger)
    {
        _hostLogger = logger;
    }

    /// <inheritdoc />
    public Task StartAsync(CancellationToken cancellationToken)
    {
        Apply(_hostLogger);
        return Task.CompletedTask;
    }

    /// <inheritdoc />
    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    public static void Apply(ILogger logger)
    {
        try
        {
            var type = AppDomain.CurrentDomain.GetAssemblies()
                .Select(a => a.GetType("Emby.Server.Implementations.Library.MediaSourceManager", false))
                .FirstOrDefault(t => t != null);
            var method = type?.GetMethod("GetPlaybackMediaSources", BindingFlags.Instance | BindingFlags.Public,
                new[] { typeof(BaseItem), typeof(User), typeof(bool), typeof(bool), typeof(CancellationToken) });

            // An earlier copy of this plugin in the same process (Jellyfin restarts in-process) patched it too.
            HarmonyInstance.UnpatchAll(HarmonyInstance.Id);
            if (method != null && method.GetParameters()[2].Name == "allowMediaProbe")
            {
                HarmonyInstance.Patch(method, prefix: new HarmonyMethod(typeof(VodProbeOncePatch).GetMethod(nameof(Prefix), BindingFlags.NonPublic | BindingFlags.Static)));
                logger.LogInformation("[Tentacle] Harmony patch applied to MediaSourceManager.GetPlaybackMediaSources (a Tentacle VOD title is probed once, not at every play)");
            }
            else
            {
                logger.LogWarning("[Tentacle] Jellyfin's MediaSourceManager.GetPlaybackMediaSources not found: Tentacle VOD is probed at every play, as before");
            }
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "[Tentacle] Could not patch MediaSourceManager.GetPlaybackMediaSources");
        }
    }

    /// <summary>Whether Jellyfin may skip the probe: a Tentacle VOD item whose streams it already knows.</summary>
    public static bool SkipProbe(bool isTentacleVod, IEnumerable<MediaStream>? knownStreams)
        => isTentacleVod && (knownStreams ?? Array.Empty<MediaStream>()).Any(s => s.Type == MediaStreamType.Video);

    private static void Prefix(BaseItem item, ref bool allowMediaProbe)
    {
        try
        {
            if (allowMediaProbe && item != null && VodNoVideoCopyFilter.IsTentacleVod(item)
                && SkipProbe(true, item.GetMediaStreams()))
            {
                allowMediaProbe = false;
            }
        }
        catch
        {
            // Jellyfin's own probe, as before.
        }
    }
}
