using System.Diagnostics;
using System.Reflection;
using HarmonyLib;
using MediaBrowser.Common;
using MediaBrowser.Controller.MediaEncoding;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Embedded subtitles of a Tentacle VOD come from Tentacle's own copy; failing
/// that, they are kept only when they were extracted from the whole file.
///
/// Tentacle reads a provider title's subtitles itself, in the background, on
/// a provider slot of its own that belongs to no playback
/// (services/subtitles.py): Jellyfin's read rode on the viewer's playback,
/// got the connection only while the player didn't want it, and died with the
/// playback when the episode ended or the player moved on (25 Nov 2026: three
/// minutes of reading thrown away, "fetching" and then nothing). So when
/// Jellyfin is asked for a Tentacle VOD subtitle it doesn't have, this asks
/// Tentacle for the tracks it has (first in line if they aren't ready, waiting
/// up to <see cref="WaitFor"/>) and puts them where Jellyfin keeps them.
/// Only when Tentacle has none of them (not answering, or a language nobody in
/// the household reads) does Jellyfin read the file as below.
///
/// Why: to serve an embedded text subtitle, Jellyfin (SubtitleEncoder, 10.11)
/// runs one ffmpeg over the whole file and caches what it wrote, per media
/// source, for good. For a provider title that read is 2 GB through Tentacle,
/// and it outlives the player: Tentacle closes it a few seconds after the
/// last viewer stopped (routers/vod.py, one person = one provider
/// connection). ffmpeg then ends as if the file had ended there, and Jellyfin,
/// which checks only that the files are not empty, logged "extraction
/// completed" and kept subtitles that stop at minute 50 of a 140-minute film,
/// for every later play.
///
/// How: a Harmony prefix on SubtitleEncoder.ExtractSubtitlesForFile, for a
/// Tentacle VOD input only. The same ffmpeg command writes to
/// "&lt;n&gt;.partial.&lt;ext&gt;" next to Jellyfin's files; they are renamed
/// into place only when ffmpeg exited 0 without an input error (a cut stream
/// is "Stream ends prematurely" in its log). Otherwise nothing is kept, so the
/// next play extracts again. Every other input, and a Jellyfin whose method
/// changed, is extracted by Jellyfin as before.
/// </summary>
public sealed class VodSubtitleExtractionPatch : IHostedService
{
    private static readonly Harmony HarmonyInstance = new("jellyfin.plugin.tentacle.vod.subtitles");
    private static ILogger? _logger;
    private static MethodInfo? _setAssFont;

    // What ffmpeg prints when its input ended before the file did (the stream
    // was closed, the connection broke): the output then holds only part of it.
    private static readonly string[] InputErrors =
    {
        "Stream ends prematurely",
        "Error during demuxing",
        "Input/output error",
        "Connection reset by peer",
    };

    /// <summary>How long a person's pick waits for Tentacle to read the title.</summary>
    public static readonly TimeSpan WaitFor = TimeSpan.FromMinutes(20);

    private static readonly HttpClient TentacleClient = new() { Timeout = WaitFor + TimeSpan.FromMinutes(1) };
    // Who asked Jellyfin for a subtitle of which media source lately
    // (SubtitleCallerFilter): Jellyfin extracts outside the request, so the
    // caller is remembered to ask Tentacle as them.
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, (string Token, DateTime At)> Callers = new();

    private readonly ILogger<VodSubtitleExtractionPatch> _hostLogger;

    public VodSubtitleExtractionPatch(ILogger<VodSubtitleExtractionPatch> logger)
    {
        _hostLogger = logger;
    }

    /// <summary>Remember who asked for a subtitle of this media source (or item).</summary>
    public static void RememberCaller(string id, string token)
    {
        if (string.IsNullOrEmpty(token) || !Guid.TryParse(id, out var g))
        {
            return;
        }

        if (Callers.Count > 500)
        {
            Callers.Clear();
        }

        Callers[g.ToString("N")] = (token, DateTime.UtcNow);
    }

    private static string CallerToken(IReadOnlyList<string> outputPaths)
    {
        var target = outputPaths.Select(CacheTarget).FirstOrDefault(t => t is not null);
        return target is not null && Callers.TryGetValue(target.Value.SourceId, out var hit)
               && DateTime.UtcNow - hit.At < TimeSpan.FromMinutes(10)
            ? hit.Token
            : string.Empty;
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
        _logger = logger;
        try
        {
            var type = AppDomain.CurrentDomain.GetAssemblies()
                .Select(a => a.GetType("MediaBrowser.MediaEncoding.Subtitles.SubtitleEncoder", false))
                .FirstOrDefault(t => t != null);
            var method = type?.GetMethod("ExtractSubtitlesForFile", BindingFlags.Instance | BindingFlags.NonPublic,
                new[] { typeof(string), typeof(string), typeof(List<string>), typeof(CancellationToken) });
            _setAssFont = type?.GetMethod("SetAssFont", BindingFlags.Instance | BindingFlags.NonPublic,
                new[] { typeof(string), typeof(CancellationToken) });

            // An earlier copy of this plugin in the same process (Jellyfin restarts in-process) patched it too.
            HarmonyInstance.UnpatchAll(HarmonyInstance.Id);
            if (method != null && method.ReturnType == typeof(Task)
                && type!.GetField("_mediaEncoder", BindingFlags.Instance | BindingFlags.NonPublic) != null)
            {
                HarmonyInstance.Patch(method, prefix: new HarmonyMethod(typeof(VodSubtitleExtractionPatch).GetMethod(nameof(Prefix), BindingFlags.NonPublic | BindingFlags.Static)));
                logger.LogInformation("[Tentacle] Harmony patch applied to SubtitleEncoder.ExtractSubtitlesForFile (Tentacle VOD subtitles are kept only when extracted in full)");
            }
            else
            {
                logger.LogWarning("[Tentacle] Jellyfin's SubtitleEncoder.ExtractSubtitlesForFile not found: Tentacle VOD subtitles are extracted by Jellyfin as before");
            }
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "[Tentacle] Could not patch SubtitleEncoder.ExtractSubtitlesForFile");
        }
    }

    /// <summary>The address in ffmpeg's input argument (<c>"http://…"</c>, quoted).</summary>
    public static string InputUrl(string? inputPath) => (inputPath ?? string.Empty).Trim().Trim('"');

    /// <summary>
    /// The User-Agent the extraction reads the file with, so Tentacle can tell it from the transcode
    /// that feeds the player: both come from Jellyfin's address, and with equal turns on the one
    /// provider connection the extraction starved the player (a TV stopped 5-9 s into a film).
    /// Tentacle's <c>routers/vod.py</c> <c>WORK_USER_AGENT</c> is the same.
    /// </summary>
    public const string WorkUserAgent = "Tentacle-JellyfinWork";

    /// <summary>Jellyfin's arguments with the extraction's User-Agent (an input option, before them all).</summary>
    public static string WithWorkUserAgent(string args) => "-user_agent \"" + WorkUserAgent + "\" " + args;

    /// <summary>Where the extraction writes before it is known to be whole: <c>2.srt</c> -> <c>2.partial.srt</c>.</summary>
    public static string PartialPath(string outputPath)
        => Path.ChangeExtension(outputPath, ".partial" + Path.GetExtension(outputPath));

    /// <summary>Whether a line of ffmpeg's log says its input ended early.</summary>
    public static bool IsInputError(string? line)
        => !string.IsNullOrEmpty(line) && InputErrors.Any(e => line.Contains(e, StringComparison.OrdinalIgnoreCase));

    /// <summary>
    /// Jellyfin's arguments with each output path ("…/2.srt", quoted as
    /// Jellyfin writes it) swapped for its partial path; null when one is not
    /// found (then Jellyfin extracts as before).
    /// </summary>
    public static string? PartialArguments(string args, IReadOnlyList<string> outputPaths)
    {
        foreach (var output in outputPaths)
        {
            var quoted = "\"" + output + "\"";
            if (!args.Contains(quoted, StringComparison.Ordinal))
            {
                return null;
            }

            args = args.Replace(quoted, "\"" + PartialPath(output) + "\"", StringComparison.Ordinal);
        }

        return args;
    }

    private static bool Prefix(object __instance, string inputPath, string args, List<string> outputPaths,
        CancellationToken cancellationToken, IMediaEncoder ____mediaEncoder, ref Task __result)
    {
        try
        {
            if (!VodNoVideoCopyFilter.IsTentacleVodUrl(InputUrl(inputPath)) || outputPaths.Count == 0
                || string.IsNullOrEmpty(____mediaEncoder?.EncoderPath))
            {
                return true;
            }

            var partialArgs = PartialArguments(args, outputPaths);
            if (partialArgs is null)
            {
                return true;
            }

            partialArgs = WithWorkUserAgent(partialArgs);

            var token = CallerToken(outputPaths);
            __result = FromTentacleOrWhole(__instance, ____mediaEncoder.EncoderPath, InputUrl(inputPath), partialArgs,
                outputPaths, token, cancellationToken);
            return false;
        }
        catch (Exception ex)
        {
            _logger?.LogWarning(ex, "[Tentacle] Tentacle VOD subtitle check failed; Jellyfin extracts as before");
            return true;
        }
    }

    /// <summary>
    /// The media source id and stream index of one of Jellyfin's subtitle
    /// cache paths (<c>…/subtitles/67/67c6357e-…/22.srt</c>); null when it isn't one.
    /// </summary>
    public static (string SourceId, int Index)? CacheTarget(string outputPath)
    {
        var dir = Path.GetFileName(Path.GetDirectoryName(outputPath) ?? string.Empty);
        return Guid.TryParse(dir, out var id) && int.TryParse(Path.GetFileNameWithoutExtension(outputPath), out var index)
            ? (id.ToString("N"), index)
            : null;
    }

    private static async Task FromTentacleOrWhole(object encoder, string ffmpeg, string url, string args,
        List<string> outputPaths, string token, CancellationToken cancellationToken)
    {
        var got = await FromTentacle(outputPaths, token, cancellationToken).ConfigureAwait(false);
        if (got > 0)
        {
            return;
        }

        await ExtractWhole(encoder, ffmpeg, url, args, outputPaths, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// Tentacle's copies of the tracks Jellyfin wants, written where Jellyfin
    /// keeps them. How many there were (0: Jellyfin reads the file itself).
    /// </summary>
    private static async Task<int> FromTentacle(List<string> outputPaths, string token, CancellationToken cancellationToken)
    {
        var baseUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
        var srts = outputPaths.Where(p => p.EndsWith(".srt", StringComparison.OrdinalIgnoreCase))
            .Select(p => (Path: p, Target: CacheTarget(p))).Where(x => x.Target is not null).ToList();
        if (string.IsNullOrEmpty(baseUrl) || string.IsNullOrEmpty(token) || srts.Count == 0)
        {
            _logger?.LogInformation(
                "[Tentacle] Tentacle's copy of these subtitles not asked for ({Why}); Jellyfin reads the file",
                string.IsNullOrEmpty(baseUrl) ? "no Tentacle address" : string.IsNullOrEmpty(token) ? "no caller to ask as" : "no SRT tracks");
            return 0;
        }

        var sourceId = srts[0].Target!.Value.SourceId;
        var key = "api_key=" + Uri.EscapeDataString(token);
        try
        {
            // Which tracks Tentacle has; first in line and waited for when it is still reading.
            using var list = await TentacleClient.GetAsync(
                $"{baseUrl}/api/subtitles/{sourceId}/embedded?wait={(int)WaitFor.TotalSeconds}&{key}", cancellationToken).ConfigureAwait(false);
            if (!list.IsSuccessStatusCode)
            {
                return 0;
            }

            using var doc = System.Text.Json.JsonDocument.Parse(await list.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false));
            var have = doc.RootElement.TryGetProperty("tracks", out var t) && t.ValueKind == System.Text.Json.JsonValueKind.Array
                ? t.EnumerateArray().Where(e => e.ValueKind == System.Text.Json.JsonValueKind.Number).Select(e => e.GetInt32()).ToHashSet()
                : new HashSet<int>();
            var n = 0;
            foreach (var (path, target) in srts.Where(x => have.Contains(x.Target!.Value.Index)))
            {
                using var one = await TentacleClient.GetAsync(
                    $"{baseUrl}/api/subtitles/{sourceId}/stream/{target!.Value.Index}.srt?{key}", cancellationToken).ConfigureAwait(false);
                if (!one.IsSuccessStatusCode)
                {
                    continue;
                }

                var bytes = await one.Content.ReadAsByteArrayAsync(cancellationToken).ConfigureAwait(false);
                if (bytes.Length == 0)
                {
                    continue;
                }

                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                var partial = PartialPath(path);
                await File.WriteAllBytesAsync(partial, bytes, cancellationToken).ConfigureAwait(false);
                File.Move(partial, path, true);
                n++;
            }

            _logger?.LogInformation(
                "[Tentacle] Subtitles of Tentacle VOD {SourceId}: {Count} of the {Asked} tracks Jellyfin asked for came from Tentacle's copy",
                sourceId, n, outputPaths.Count);
            return n;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            _logger?.LogInformation("[Tentacle] Tentacle's copy of the subtitles of {SourceId} not reached ({Error}); Jellyfin reads the file", sourceId, ex.Message);
            return 0;
        }
    }

    private static async Task ExtractWhole(object encoder, string ffmpeg, string url, string args,
        List<string> outputPaths, CancellationToken cancellationToken)
    {
        var partials = outputPaths.Select(PartialPath).ToList();
        partials.ForEach(DeleteQuietly);
        var inputError = string.Empty;

        using var process = new Process
        {
            StartInfo = new ProcessStartInfo
            {
                CreateNoWindow = true,
                UseShellExecute = false,
                FileName = ffmpeg,
                Arguments = args,
                RedirectStandardError = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                ErrorDialog = false
            },
            EnableRaisingEvents = true
        };
        process.ErrorDataReceived += (_, e) =>
        {
            if (inputError.Length == 0 && IsInputError(e.Data))
            {
                inputError = e.Data!.Trim();
            }
        };

        _logger?.LogInformation("[Tentacle] Extracting the subtitles of Tentacle VOD {Url}: {File} {Arguments}", url, ffmpeg, args);
        process.Start();
        process.BeginErrorReadLine();

        // Jellyfin's own limit for one extraction.
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromMinutes(30));
        int exitCode;
        try
        {
            await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
            exitCode = process.ExitCode;
        }
        catch (OperationCanceledException)
        {
            try
            {
                process.Kill(true);
            }
            catch (InvalidOperationException)
            {
            }

            exitCode = -1;
        }

        var missing = partials.Where(p => !File.Exists(p) || new FileInfo(p).Length == 0).ToList();
        if (exitCode != 0 || inputError.Length > 0 || missing.Count > 0)
        {
            partials.ForEach(DeleteQuietly);
            var why = exitCode != 0 ? $"ffmpeg exited {exitCode}"
                : inputError.Length > 0 ? $"the stream ended early ({inputError})"
                : $"{missing.Count} of {partials.Count} tracks came out empty";
            _logger?.LogWarning(
                "[Tentacle] Subtitles of Tentacle VOD {Url} not kept: {Why}. Nothing cached; the next play extracts them again",
                url, why);
            throw new FfmpegException($"Tentacle VOD subtitle extraction did not read the whole file: {why}");
        }

        for (var i = 0; i < outputPaths.Count; i++)
        {
            File.Move(partials[i], outputPaths[i], true);
            if (outputPaths[i].EndsWith("ass", StringComparison.OrdinalIgnoreCase) && _setAssFont?.Invoke(encoder, new object[] { outputPaths[i], cancellationToken }) is Task font)
            {
                await font.ConfigureAwait(false);
            }
        }

        _logger?.LogInformation("[Tentacle] Subtitles of Tentacle VOD {Url} extracted in full ({Count} tracks)", url, outputPaths.Count);
    }

    private static void DeleteQuietly(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
}
