using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using HarmonyLib;
using Jellyfin.Data.Events.Users;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Events;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.LiveTv;
using MediaBrowser.Model.LiveTv;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Live TV per person. Tentacle gives Jellyfin one M3U tuner for everyone on
/// "All channels" and one per person with a list of their own; each tuner's
/// channels carry its tag (tentacle-livetv-all / tentacle-livetv-&lt;user id&gt;)
/// and everyone blocks the other tuners' tags. The server sets all of that
/// through the API; this closes the gaps it can't:
///   - a channel Jellyfin creates gets its tag at once (the server only tags
///     after the guide refresh, minutes later);
///   - a new Jellyfin user blocks every person's own tuner at once (the
///     server notices new users within the hour);
///   - each programme gets its channel's tag, so programme lists ("On now",
///     the Programs tab, search) are filtered in Jellyfin's database. Without
///     it nobody sees another person's programmes either (Jellyfin drops them
///     when it builds the list), but lists come back short and counts wrong.
/// </summary>
public class LiveTvIsolation : IHostedService
{
    public const string TagPrefix = "tentacle-livetv-";
    public const string AllTag = TagPrefix + "all";

    private static readonly Regex UserTunerRe = new(@"/api/live/users/([0-9a-fA-F]{32})/tuner\.m3u$", RegexOptions.Compiled);

    private readonly ILibraryManager _libraryManager;
    private readonly IConfigurationManager _config;
    private readonly ILogger<LiveTvIsolation> _logger;

    public LiveTvIsolation(ILibraryManager libraryManager, IConfigurationManager config, ILogger<LiveTvIsolation> logger)
    {
        _libraryManager = libraryManager;
        _config = config;
        _logger = logger;
    }

    /// <summary>The tag a Tentacle tuner's channels carry; null for any other tuner.</summary>
    public static string? TagForTunerUrl(string? url)
    {
        var path = (url ?? string.Empty).Split('?')[0].TrimEnd('/');
        if (path.EndsWith("/api/live/tuner/all.m3u", StringComparison.OrdinalIgnoreCase))
        {
            return AllTag;
        }

        var m = UserTunerRe.Match(path);
        return m.Success ? TagPrefix + m.Groups[1].Value.ToLowerInvariant() : null;
    }

    /// <summary>Jellyfin's string.GetMD5() printed as "N": an M3U channel's id prefix uses it.</summary>
    public static string JellyfinMd5(string text)
        => new Guid(MD5.HashData(Encoding.Unicode.GetBytes(text))).ToString("N");

    /// <summary>"m3u_" + MD5(tuner URL) for every Tentacle tuner, with its tag.</summary>
    public static Dictionary<string, string> TunerPrefixes(IEnumerable<TunerHostInfo> tuners)
    {
        var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var t in tuners)
        {
            if (!string.Equals(t.Type, "m3u", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var tag = TagForTunerUrl(t.Url);
            if (tag != null && !string.IsNullOrEmpty(t.Url))
            {
                map["m3u_" + JellyfinMd5(t.Url)] = tag;
            }
        }

        return map;
    }

    /// <summary>The tag a channel should carry, from its external id; null if it isn't Tentacle's.</summary>
    public static string? TagForChannel(string? externalId, Dictionary<string, string> prefixes)
    {
        if (string.IsNullOrEmpty(externalId) || externalId.Length < 36)
        {
            return null;
        }

        return prefixes.TryGetValue(externalId[..36], out var tag) ? tag : null;
    }

    /// <summary>Tags with Tentacle's tuner tag set to <paramref name="tag"/> (null when nothing changes).</summary>
    public static string[]? WithTag(string[]? tags, string tag)
    {
        var current = tags ?? Array.Empty<string>();
        if (current.Contains(tag, StringComparer.OrdinalIgnoreCase))
        {
            return null;
        }

        return current.Where(t => !t.StartsWith(TagPrefix, StringComparison.OrdinalIgnoreCase)).Append(tag).ToArray();
    }

    /// <summary>A new person's blocked tags: their own kept, plus every person's own tuner (they start on "All channels").</summary>
    public static string[] NewUserBlockedTags(string[]? blocked, IEnumerable<string> tunerTags)
        => (blocked ?? Array.Empty<string>())
            .Where(t => !t.StartsWith(TagPrefix, StringComparison.OrdinalIgnoreCase))
            .Concat(tunerTags.Where(t => t != AllTag).Distinct().OrderBy(t => t, StringComparer.Ordinal))
            .ToArray();

    internal IEnumerable<TunerHostInfo> Tuners()
        => (_config.GetConfiguration("livetv") as LiveTvOptions)?.TunerHosts ?? Array.Empty<TunerHostInfo>();

    public Task StartAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemAdded += OnItemChanged;
        _libraryManager.ItemUpdated += OnItemChanged;
        LiveTvTagPatches.Apply(_libraryManager, _config, _logger);
        return Task.CompletedTask;
    }

    public Task StopAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemAdded -= OnItemChanged;
        _libraryManager.ItemUpdated -= OnItemChanged;
        return Task.CompletedTask;
    }

    private void OnItemChanged(object? sender, ItemChangeEventArgs e)
    {
        if (e.Item is not LiveTvChannel channel)
        {
            return;
        }

        try
        {
            var tag = TagForChannel(channel.ExternalId, TunerPrefixes(Tuners()));
            var tags = tag == null ? null : WithTag(channel.Tags, tag);
            if (tags == null)
            {
                return;
            }

            channel.Tags = tags;
            // Our own update raises ItemUpdated again; the tag is there by then.
            channel.UpdateToRepositoryAsync(ItemUpdateType.MetadataEdit, CancellationToken.None).GetAwaiter().GetResult();
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "[Tentacle] Could not tag Live TV channel {Name}", channel.Name);
        }
    }
}

/// <summary>A new Jellyfin user starts on "All channels": block every person's own tuner right away.</summary>
public class LiveTvNewUser : IEventConsumer<UserCreatedEventArgs>
{
    private readonly IUserManager _userManager;
    private readonly IConfigurationManager _config;
    private readonly ILogger<LiveTvNewUser> _logger;

    public LiveTvNewUser(IUserManager userManager, IConfigurationManager config, ILogger<LiveTvNewUser> logger)
    {
        _userManager = userManager;
        _config = config;
        _logger = logger;
    }

    /// <summary>How long after the event the policy is set: the request creating the user
    /// is still saving it, and changing it under that request makes it fail (500).</summary>
    public static TimeSpan Delay { get; set; } = TimeSpan.FromSeconds(5);

    public Task OnEvent(UserCreatedEventArgs eventArgs)
    {
        var userId = eventArgs.Argument.Id;
        _ = Task.Run(async () =>
        {
            try
            {
                await Task.Delay(Delay).ConfigureAwait(false);
                var tuners = (_config.GetConfiguration("livetv") as LiveTvOptions)?.TunerHosts ?? Array.Empty<TunerHostInfo>();
                var tags = tuners.Select(t => LiveTvIsolation.TagForTunerUrl(t.Url)).OfType<string>().ToList();
                var user = _userManager.GetUserById(userId);
                if (tags.Count == 0 || user == null)
                {
                    return;
                }

                var policy = _userManager.GetUserDto(user).Policy;
                var blocked = LiveTvIsolation.NewUserBlockedTags(policy.BlockedTags, tags);
                if (blocked.SequenceEqual(policy.BlockedTags ?? Array.Empty<string>()))
                {
                    return;
                }

                policy.BlockedTags = blocked;
                await _userManager.UpdatePolicyAsync(userId, policy).ConfigureAwait(false);
                _logger.LogInformation("[Tentacle] New user {Name}: Live TV set to All channels", user.Username);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "[Tentacle] Could not set up Live TV for a new user");
            }
        });
        return Task.CompletedTask;
    }
}

/// <summary>
/// Harmony patches on Jellyfin's GuideManager (Jellyfin.LiveTv, 10.11), which
/// builds the Live TV channels and programmes at every guide refresh. Live TV
/// items raise no ItemAdded event, so the tags are set here:
///   - GetChannel (prefix): a Tentacle tuner's channel is created (and kept)
///     with its tuner's tag, so nobody else sees it for a moment first;
///   - GetProgram (prefix + postfix): the programme gets its channel's tag.
///     GetProgram sets a programme's tags afresh from the guide, so the prefix
///     notes whether the stored programme had it; one that didn't is marked
///     updated (Jellyfin saves XMLTV programmes every refresh anyway).
/// If a later Jellyfin renames them, nothing is patched and a warning is
/// logged: the server still tags channels and who sees what stays right; only
/// programme lists come back short.
/// </summary>
public static class LiveTvTagPatches
{
    private static readonly Harmony HarmonyInstance = new("jellyfin.plugin.tentacle.livetv");
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
                .Select(a => a.GetType("Jellyfin.LiveTv.Guide.GuideManager", false))
                .FirstOrDefault(t => t != null)
                ?? Assembly.Load("Jellyfin.LiveTv").GetType("Jellyfin.LiveTv.Guide.GuideManager", false);
            var getProgram = type?.GetMethod("GetProgram", BindingFlags.Instance | BindingFlags.NonPublic,
                new[] { typeof(ProgramInfo), typeof(Dictionary<Guid, LiveTvProgram>), typeof(LiveTvChannel) });
            var getChannel = type?.GetMethod("GetChannel", BindingFlags.Instance | BindingFlags.NonPublic,
                new[] { typeof(ChannelInfo), typeof(string), typeof(BaseItem), typeof(CancellationToken) });

            // An earlier copy of this plugin in the same process (Jellyfin restarts in-process) patched them too.
            HarmonyInstance.UnpatchAll(HarmonyInstance.Id);
            if (getChannel != null && getChannel.ReturnType == typeof(Task<LiveTvChannel>))
            {
                HarmonyInstance.Patch(getChannel, prefix: new HarmonyMethod(Method(nameof(ChannelPrefix))));
                logger.LogInformation("[Tentacle] Harmony patch applied to GuideManager.GetChannel (Live TV per person)");
            }
            else
            {
                logger.LogWarning("[Tentacle] Jellyfin's GuideManager.GetChannel not found: new channels get their Live TV tag from Tentacle after the guide refresh");
            }

            if (getProgram != null && getProgram.ReturnType == typeof(ValueTuple<LiveTvProgram, bool, bool>))
            {
                HarmonyInstance.Patch(getProgram, prefix: new HarmonyMethod(Method(nameof(Prefix))), postfix: new HarmonyMethod(Method(nameof(Postfix))));
                logger.LogInformation("[Tentacle] Harmony patch applied to GuideManager.GetProgram (Live TV per person)");
            }
            else
            {
                logger.LogWarning("[Tentacle] Jellyfin's GuideManager.GetProgram not found: programmes won't carry their channel's Live TV tag (lists may come back short)");
            }

            _applied = true;
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "[Tentacle] Could not patch GuideManager.GetProgram");
        }
    }

    private static MethodInfo Method(string name)
        => typeof(LiveTvTagPatches).GetMethod(name, BindingFlags.NonPublic | BindingFlags.Static)!;

    /// <summary>The tags a channel Jellyfin is about to build should get: its stored tags with
    /// the tuner's tag, or null to leave Jellyfin's own handling alone.</summary>
    public static string[]? ChannelTags(string? channelId, string[]? storedTags, Dictionary<string, string> prefixes)
    {
        var tag = LiveTvIsolation.TagForChannel(channelId, prefixes);
        if (tag == null)
        {
            return null;
        }

        return LiveTvIsolation.WithTag(storedTags, tag) ?? storedTags;
    }

    private static void ChannelPrefix(ChannelInfo channelInfo)
    {
        try
        {
            if (_libraryManager == null || _config == null || channelInfo?.Id == null)
            {
                return;
            }

            var tuners = (_config.GetConfiguration("livetv") as LiveTvOptions)?.TunerHosts ?? Array.Empty<TunerHostInfo>();
            var prefixes = LiveTvIsolation.TunerPrefixes(tuners);
            if (prefixes.Count == 0)
            {
                return;
            }

            // LiveTvDtoService.GetInternalChannelId with the built-in service "Emby"
            var id = _libraryManager.GetNewItemId(("Emby" + channelInfo.Id + "4").ToLowerInvariant(), typeof(LiveTvChannel));
            var stored = (_libraryManager.GetItemById(id) as LiveTvChannel)?.Tags;
            // Jellyfin keeps a channel's tags when the tuner gives none, and takes these when it
            // gives some (adding "Kids" itself later, as before).
            channelInfo.Tags = ChannelTags(channelInfo.Id, stored, prefixes)!;
        }
        catch
        {
            // Tentacle tags the channel after the refresh instead.
        }
    }

    public static string? ChannelTag(LiveTvChannel? channel)
        => channel?.Tags?.FirstOrDefault(t => t.StartsWith(LiveTvIsolation.TagPrefix, StringComparison.OrdinalIgnoreCase));

    private static void Prefix(ProgramInfo info, Dictionary<Guid, LiveTvProgram> allExistingPrograms, LiveTvChannel channel, out bool __state)
    {
        __state = false;
        try
        {
            var tag = ChannelTag(channel);
            if (tag == null || _libraryManager == null || info?.Id == null)
            {
                return;
            }

            // LiveTvDtoService.GetInternalProgramId
            var id = _libraryManager.GetNewItemId(("Emby" + info.Id + "4").ToLowerInvariant(), typeof(LiveTvProgram));
            __state = allExistingPrograms.TryGetValue(id, out var existing)
                && existing.Tags.Contains(tag, StringComparer.OrdinalIgnoreCase);
        }
        catch
        {
            __state = false;
        }
    }

    private static void Postfix(LiveTvChannel channel, ref ValueTuple<LiveTvProgram, bool, bool> __result, bool __state)
    {
        var tag = ChannelTag(channel);
        var item = __result.Item1;
        if (tag == null || item == null)
        {
            return;
        }

        var tags = LiveTvIsolation.WithTag(item.Tags, tag);
        if (tags != null)
        {
            item.Tags = tags;
        }

        if (!__state && !__result.Item2)
        {
            __result.Item3 = true;
        }
    }
}
