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
        ProgrammeTagPatch.Apply(_libraryManager, _logger);
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

    public async Task OnEvent(UserCreatedEventArgs eventArgs)
    {
        try
        {
            var tuners = (_config.GetConfiguration("livetv") as LiveTvOptions)?.TunerHosts ?? Array.Empty<TunerHostInfo>();
            var tags = tuners.Select(t => LiveTvIsolation.TagForTunerUrl(t.Url)).OfType<string>().ToList();
            if (tags.Count == 0)
            {
                return;
            }

            var user = eventArgs.Argument;
            var policy = _userManager.GetUserDto(user).Policy;
            policy.BlockedTags = LiveTvIsolation.NewUserBlockedTags(policy.BlockedTags, tags);
            await _userManager.UpdatePolicyAsync(user.Id, policy).ConfigureAwait(false);
            _logger.LogInformation("[Tentacle] New user {Name}: Live TV set to All channels", user.Username);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "[Tentacle] Could not set up Live TV for a new user");
        }
    }
}

/// <summary>
/// Harmony postfix on Jellyfin's GuideManager.GetProgram (Jellyfin.LiveTv,
/// 10.11): the programme it builds from the guide gets its channel's tuner tag.
/// GetProgram sets a programme's tags afresh from the guide every refresh, so
/// the prefix notes whether the stored programme already had the tag; only a
/// programme that didn't is marked updated (saved once, never again after).
/// If a later Jellyfin renames the method, nothing is patched and a warning is
/// logged: who sees what stays right, only programme lists come back short.
/// </summary>
public static class ProgrammeTagPatch
{
    private static readonly Harmony HarmonyInstance = new("jellyfin.plugin.tentacle.livetv");
    private static ILibraryManager? _libraryManager;
    private static bool _applied;

    public static void Apply(ILibraryManager libraryManager, ILogger logger)
    {
        _libraryManager = libraryManager;
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
            var method = type?.GetMethod("GetProgram", BindingFlags.Instance | BindingFlags.NonPublic,
                new[] { typeof(ProgramInfo), typeof(Dictionary<Guid, LiveTvProgram>), typeof(LiveTvChannel) });
            if (method == null || method.ReturnType != typeof(ValueTuple<LiveTvProgram, bool, bool>))
            {
                logger.LogWarning("[Tentacle] Jellyfin's GuideManager.GetProgram not found: programmes won't carry their channel's Live TV tag (lists may come back short)");
                return;
            }

            // An earlier copy of this plugin in the same process (Jellyfin restarts in-process) patched it too.
            HarmonyInstance.UnpatchAll(HarmonyInstance.Id);
            HarmonyInstance.Patch(
                method,
                prefix: new HarmonyMethod(typeof(ProgrammeTagPatch).GetMethod(nameof(Prefix), BindingFlags.NonPublic | BindingFlags.Static)),
                postfix: new HarmonyMethod(typeof(ProgrammeTagPatch).GetMethod(nameof(Postfix), BindingFlags.NonPublic | BindingFlags.Static)));
            _applied = true;
            logger.LogInformation("[Tentacle] Harmony patch applied to GuideManager.GetProgram (Live TV per person)");
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "[Tentacle] Could not patch GuideManager.GetProgram");
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
