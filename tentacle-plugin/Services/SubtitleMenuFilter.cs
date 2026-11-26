using System.Collections.Concurrent;
using System.Net.Http;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.MediaInfo;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Each person sees only the subtitles in their own languages.
///
/// Why: a provider episode carries up to 48 text tracks; Jellyfin listed
/// them all ("Ca", "Eu", "Gl" …), and picking one made Jellyfin read the
/// whole episode for all 48 while the person waited.
///
/// How: an MVC filter on PlaybackInfo (what every player's menu is built
/// from) and on the item's page (/Items/{id}: the details page's picker).
/// After Jellyfin answered, a source's subtitle streams are cut to the
/// person's languages (Tentacle's <c>/api/watch/prefs</c>, <see cref="WatchPrefs"/>):
/// per language, kind (plain, SDH, forced) one entry, the file Tentacle wrote
/// beside the title (external) before the same track inside the file, whose
/// reading Jellyfin would start again. Names are plain ("English",
/// "English SDH"). The default track Jellyfin picked by the person's
/// language and mode is kept, or moved to the entry that stands for it.
/// Nothing changes when Tentacle can't say what the person reads.
///
/// A play (PlaybackInfo) also tells Tentacle the person started the title
/// (<c>/api/subtitles/playing</c>): it gets that title's subtitles ready
/// and its season's next episodes', in the background.
/// </summary>
public sealed class SubtitleMenuFilter : IAsyncActionFilter
{
    private static readonly HashSet<(string Controller, string Action)> PlaybackActions = new()
    {
        ("MediaInfo", "GetPostedPlaybackInfo"),
        ("MediaInfo", "GetPlaybackInfo"),
    };

    private static readonly HashSet<(string Controller, string Action)> ItemActions = new()
    {
        ("UserLibrary", "GetItem"),
        ("UserLibrary", "GetItemLegacy"),
    };

    // Tentacle is told once per person and title in this long (PlaybackInfo
    // comes again at every seek, audio or subtitle change).
    private static readonly TimeSpan PlayingEvery = TimeSpan.FromMinutes(30);
    private static readonly ConcurrentDictionary<string, DateTime> ToldPlaying = new();
    private static readonly HttpClient Client = new() { Timeout = TimeSpan.FromSeconds(10) };

    private readonly ILogger<SubtitleMenuFilter> _logger;

    public SubtitleMenuFilter(ILogger<SubtitleMenuFilter> logger)
    {
        _logger = logger;
    }

    /// <inheritdoc />
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        var executed = await next().ConfigureAwait(false);
        if (context.ActionDescriptor is not ControllerActionDescriptor action)
        {
            return;
        }

        var key = (action.ControllerName, action.ActionName);
        var playback = PlaybackActions.Contains(key);
        if (!playback && !ItemActions.Contains(key))
        {
            return;
        }

        try
        {
            var sources = executed.Result switch
            {
                ObjectResult { Value: PlaybackInfoResponse info } => info.MediaSources,
                ObjectResult { Value: BaseItemDto dto } => dto.MediaSources,
                _ => null,
            };
            if (sources is null || sources.Count == 0)
            {
                return;
            }

            // The caller's own choices (Tentacle answers for the token's person).
            Guid.TryParse(context.HttpContext.User.FindFirst("Jellyfin-UserId")?.Value, out var userId);
            var prefs = await WatchPrefs.ForAsync(userId, context.HttpContext.Request).ConfigureAwait(false);
            if (prefs is null || prefs.Languages.Count == 0)
            {
                return;
            }

            foreach (var source in sources)
            {
                Filter(source, prefs.Languages);
            }

            if (playback && context.ActionArguments.TryGetValue("itemId", out var raw) && raw is Guid itemId)
            {
                TellPlaying(userId, itemId, context.HttpContext.Request);
            }
        }
        catch (Exception ex)
        {
            // Never fail a play over this: Jellyfin's answer goes out as it was.
            _logger.LogWarning(ex, "[Tentacle] Could not cut the subtitle list to the person's languages");
        }
    }

    private void TellPlaying(Guid userId, Guid itemId, Microsoft.AspNetCore.Http.HttpRequest request)
    {
        var key = $"{userId:N}:{itemId:N}";
        var now = DateTime.UtcNow;
        if (ToldPlaying.TryGetValue(key, out var at) && now - at < PlayingEvery)
        {
            return;
        }

        ToldPlaying[key] = now;
        if (ToldPlaying.Count > 2000)
        {
            ToldPlaying.Clear();
        }

        var baseUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
        var token = PluginKeysClient.ExtractToken(request);
        if (string.IsNullOrEmpty(baseUrl) || string.IsNullOrEmpty(token))
        {
            return;
        }

        var url = $"{baseUrl}/api/subtitles/playing/{itemId:N}?api_key={Uri.EscapeDataString(token)}";
        _ = Task.Run(async () =>
        {
            try
            {
                using var resp = await Client.PostAsync(url, null).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                _logger.LogDebug("[Tentacle] Tentacle not told about a play: {Error}", ex.Message);
            }
        });
    }

    /// <summary>
    /// Cut one source's subtitle streams to <paramref name="languages"/>
    /// (see the class). Returns how many were removed.
    /// </summary>
    public static int Filter(MediaSourceInfo source, IReadOnlyList<string> languages)
    {
        var streams = source.MediaStreams;
        if (streams is null)
        {
            return 0;
        }

        var wanted = languages.Select(Languages.Normalize).Where(l => l.Length > 0).ToList();
        var subs = streams.Where(s => s.Type == MediaStreamType.Subtitle).ToList();
        if (subs.Count == 0 || wanted.Count == 0)
        {
            return 0;
        }

        var keep = new List<MediaStream>();
        var seen = new HashSet<(string, bool, bool)>();
        // Files beside the title first: Tentacle wrote them (or Bazarr, or a
        // person); the same track inside the file would be read again.
        foreach (var s in subs.Where(s => s.IsExternal).Concat(subs.Where(s => !s.IsExternal)))
        {
            var key = KeyOf(s);
            if (!wanted.Contains(key.Item1) || !seen.Add(key))
            {
                continue;
            }

            keep.Add(s);
        }

        // A file whose tracks name no language: keep those rather than show nothing.
        if (keep.Count == 0)
        {
            keep.AddRange(subs.Where(s => Languages.Normalize(s.Language).Length == 0));
        }

        var removed = subs.Where(s => !keep.Contains(s)).ToList();
        foreach (var s in keep)
        {
            var (lang, hi, forced) = KeyOf(s);
            if (lang.Length == 0)
            {
                continue;
            }

            s.Title = Languages.Name(lang) + (hi ? " SDH" : string.Empty) + (forced ? " (forced)" : string.Empty);
            // Jellyfin's display name adds each word the title doesn't already
            // contain: these are words it contains, so only the title shows
            // (and the format, which Jellyfin always adds).
            s.LocalizedExternal = s.Title;
            s.LocalizedDefault = s.Title;
            s.LocalizedHearingImpaired = "SDH";
            s.LocalizedForced = "forced";
        }

        if (removed.Count == 0)
        {
            return 0;
        }

        var defaultIndex = source.DefaultSubtitleStreamIndex;
        var gone = removed.FirstOrDefault(s => s.Index == defaultIndex);
        if (gone is not null)
        {
            var standIn = keep.FirstOrDefault(s => KeyOf(s) == KeyOf(gone))
                          ?? keep.FirstOrDefault(s => KeyOf(s).Item1 == KeyOf(gone).Item1);
            source.DefaultSubtitleStreamIndex = standIn?.Index ?? -1;
        }

        // Their languages in their order; plain, then SDH, then forced.
        int Rank(MediaStream m)
        {
            var (l, h, f) = KeyOf(m);
            var at = wanted.IndexOf(l);
            return ((at < 0 ? wanted.Count : at) * 4) + (f ? 2 : h ? 1 : 0);
        }

        source.MediaStreams = streams.Where(s => s.Type != MediaStreamType.Subtitle)
            .Concat(keep.OrderBy(Rank).ThenBy(m => m.Index)).ToList();
        return removed.Count;
    }

    private static (string, bool, bool) KeyOf(MediaStream s)
    {
        var title = s.Title ?? string.Empty;
        var hi = s.IsHearingImpaired
                 || title.Contains("SDH", StringComparison.OrdinalIgnoreCase)
                 || title.Contains("Hearing", StringComparison.OrdinalIgnoreCase);
        var forced = s.IsForced || title.Contains("forced", StringComparison.OrdinalIgnoreCase);
        return (Languages.Normalize(s.Language), hi, forced);
    }
}

/// <summary>Language codes as files, Jellyfin and Tentacle write them, folded to ISO 639-2/B.</summary>
public static class Languages
{
    private static readonly Dictionary<string, string> TToB = new(StringComparer.Ordinal)
    {
        ["fra"] = "fre", ["deu"] = "ger", ["nld"] = "dut", ["ces"] = "cze", ["slk"] = "slo", ["ron"] = "rum",
        ["ell"] = "gre", ["fas"] = "per", ["zho"] = "chi", ["msa"] = "may", ["isl"] = "ice", ["eus"] = "baq",
        ["cym"] = "wel", ["sqi"] = "alb", ["hye"] = "arm", ["kat"] = "geo", ["mkd"] = "mac", ["mri"] = "mao",
        ["mya"] = "bur", ["bod"] = "tib",
    };

    private static readonly Dictionary<string, string> TwoLetter = new(StringComparer.Ordinal)
    {
        ["en"] = "eng", ["fr"] = "fre", ["es"] = "spa", ["de"] = "ger", ["it"] = "ita", ["pt"] = "por", ["nl"] = "dut",
        ["ru"] = "rus", ["uk"] = "ukr", ["pl"] = "pol", ["cs"] = "cze", ["sk"] = "slo", ["hu"] = "hun", ["ro"] = "rum",
        ["bg"] = "bul", ["el"] = "gre", ["tr"] = "tur", ["ar"] = "ara", ["he"] = "heb", ["fa"] = "per", ["hi"] = "hin",
        ["ur"] = "urd", ["bn"] = "ben", ["ta"] = "tam", ["te"] = "tel", ["zh"] = "chi", ["ja"] = "jpn", ["ko"] = "kor",
        ["vi"] = "vie", ["th"] = "tha", ["id"] = "ind", ["ms"] = "may", ["sv"] = "swe", ["no"] = "nor", ["nb"] = "nor",
        ["nn"] = "nor", ["da"] = "dan", ["fi"] = "fin", ["is"] = "ice", ["hr"] = "hrv", ["sr"] = "srp", ["sl"] = "slv",
        ["et"] = "est", ["lv"] = "lav", ["lt"] = "lit", ["ca"] = "cat", ["eu"] = "baq", ["gl"] = "glg", ["cy"] = "wel",
        ["ga"] = "gle", ["af"] = "afr", ["sw"] = "swa", ["tl"] = "fil",
    };

    private static readonly Dictionary<string, string> Names = new(StringComparer.Ordinal)
    {
        ["eng"] = "English", ["fre"] = "French", ["spa"] = "Spanish", ["ger"] = "German", ["ita"] = "Italian",
        ["por"] = "Portuguese", ["dut"] = "Dutch", ["rus"] = "Russian", ["ukr"] = "Ukrainian", ["pol"] = "Polish",
        ["cze"] = "Czech", ["slo"] = "Slovak", ["hun"] = "Hungarian", ["rum"] = "Romanian", ["bul"] = "Bulgarian",
        ["gre"] = "Greek", ["tur"] = "Turkish", ["ara"] = "Arabic", ["heb"] = "Hebrew", ["per"] = "Persian",
        ["hin"] = "Hindi", ["urd"] = "Urdu", ["ben"] = "Bengali", ["tam"] = "Tamil", ["tel"] = "Telugu",
        ["chi"] = "Chinese", ["jpn"] = "Japanese", ["kor"] = "Korean", ["vie"] = "Vietnamese", ["tha"] = "Thai",
        ["ind"] = "Indonesian", ["may"] = "Malay", ["fil"] = "Filipino", ["swe"] = "Swedish", ["nor"] = "Norwegian",
        ["dan"] = "Danish", ["fin"] = "Finnish", ["ice"] = "Icelandic", ["hrv"] = "Croatian", ["srp"] = "Serbian",
        ["slv"] = "Slovenian", ["est"] = "Estonian", ["lav"] = "Latvian", ["lit"] = "Lithuanian", ["cat"] = "Catalan",
        ["baq"] = "Basque", ["glg"] = "Galician", ["wel"] = "Welsh", ["gle"] = "Irish", ["afr"] = "Afrikaans",
        ["swa"] = "Swahili",
    };

    /// <summary>"en", "eng", "fra", "pt-BR" → one ISO 639-2/B code; "" when unknown.</summary>
    public static string Normalize(string? code)
    {
        var c = (code ?? string.Empty).Trim().ToLowerInvariant().Replace('_', '-').Split('-')[0];
        if (c.Length == 2)
        {
            return TwoLetter.TryGetValue(c, out var three) ? three : string.Empty;
        }

        if (c.Length == 3 && c is not ("und" or "mis" or "mul" or "zxx"))
        {
            return TToB.TryGetValue(c, out var b) ? b : c;
        }

        return string.Empty;
    }

    /// <summary>The language's English name ("English", "French").</summary>
    public static string Name(string code)
        => Names.TryGetValue(code, out var n) ? n : code;
}
