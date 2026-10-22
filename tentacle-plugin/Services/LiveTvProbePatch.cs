using System.Reflection;
using HarmonyLib;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.LiveTv;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.LiveTv;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// A Tentacle channel opens without Jellyfin's fixed 3 s wait in front of its
/// first probe. The first time a channel is opened, Jellyfin
/// (LiveStreamHelper.AddMediaInfoWithProbe, 10.11) sleeps 3 s "before probing
/// the live stream" and only then runs ffprobe, which on a Tentacle stream
/// takes well under a second: the tuner's open has already waited for the
/// first bytes, and ffprobe reads the stream as it arrives. The probe and its
/// cache (/cache/mediainfo, per channel) stay Jellyfin's; only the sleep goes.
/// Other tuners keep Jellyfin's behaviour. If a later Jellyfin renames the
/// method, nothing is patched and a warning is logged: channels then open as
/// before, 3 s slower the first time.
/// </summary>
public static class LiveTvProbePatch
{
    private static readonly Harmony HarmonyInstance = new("jellyfin.plugin.tentacle.livetv.probe");
    private static ILibraryManager? _libraryManager;
    private static IConfigurationManager? _config;
    private static bool _applied;

    public static void Apply(ILibraryManager libraryManager, IConfigurationManager config, ILogger logger)
    {
        _libraryManager = libraryManager;
        _config = config;
        if (_applied)
        {
            return;
        }

        try
        {
            var type = AppDomain.CurrentDomain.GetAssemblies()
                .Select(a => a.GetType("Emby.Server.Implementations.Library.LiveStreamHelper", false))
                .FirstOrDefault(t => t != null);
            var method = type?.GetMethod("AddMediaInfoWithProbe", BindingFlags.Instance | BindingFlags.Public,
                new[] { typeof(MediaSourceInfo), typeof(bool), typeof(string), typeof(bool), typeof(CancellationToken) });

            // An earlier copy of this plugin in the same process (Jellyfin restarts in-process) patched it too.
            HarmonyInstance.UnpatchAll(HarmonyInstance.Id);
            if (method != null && method.GetParameters()[3].Name == "addProbeDelay")
            {
                HarmonyInstance.Patch(method, prefix: new HarmonyMethod(typeof(LiveTvProbePatch).GetMethod(nameof(Prefix), BindingFlags.NonPublic | BindingFlags.Static)));
                logger.LogInformation("[Tentacle] Harmony patch applied to LiveStreamHelper.AddMediaInfoWithProbe (Live TV opens without the 3 s probe wait)");
            }
            else
            {
                logger.LogWarning("[Tentacle] Jellyfin's LiveStreamHelper.AddMediaInfoWithProbe not found: a channel's first open keeps Jellyfin's 3 s probe wait");
            }

            _applied = true;
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "[Tentacle] Could not patch LiveStreamHelper.AddMediaInfoWithProbe");
        }
    }

    /// <summary>The Live TV channel item id in a live stream's open token
    /// ("&lt;provider&gt;_LiveTvChannel_&lt;item id&gt;_&lt;media source id&gt;"); null for anything else.</summary>
    public static Guid? ChannelItemId(string? openToken)
    {
        var parts = (openToken ?? string.Empty).Split('_');
        var i = Array.IndexOf(parts, nameof(LiveTvChannel));
        return i >= 0 && i + 1 < parts.Length && Guid.TryParse(parts[i + 1], out var id) ? id : null;
    }

    /// <summary>Whether the channel comes from a Tentacle tuner, by its external id.</summary>
    public static bool IsTentacleChannel(string? externalId, IEnumerable<TunerHostInfo> tuners)
        => LiveTvIsolation.TagForChannel(externalId, LiveTvIsolation.TunerPrefixes(tuners)) != null;

    private static void Prefix(string? cacheKey, ref bool addProbeDelay)
    {
        try
        {
            if (!addProbeDelay || _libraryManager == null || _config == null || ChannelItemId(cacheKey) is not Guid id)
            {
                return;
            }

            var tuners = (_config.GetConfiguration("livetv") as LiveTvOptions)?.TunerHosts ?? Array.Empty<TunerHostInfo>();
            if (_libraryManager.GetItemById(id) is LiveTvChannel channel && IsTentacleChannel(channel.ExternalId, tuners))
            {
                addProbeDelay = false;
            }
        }
        catch
        {
            // Jellyfin's own wait, as before.
        }
    }
}
