using System.Diagnostics;
using System.Reflection;
using HarmonyLib;
using MediaBrowser.Common;
using MediaBrowser.Controller.MediaEncoding;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Embedded subtitles of a Tentacle VOD are kept only when they were
/// extracted from the whole file.
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

    private readonly ILogger<VodSubtitleExtractionPatch> _hostLogger;

    public VodSubtitleExtractionPatch(ILogger<VodSubtitleExtractionPatch> logger)
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

            __result = ExtractWhole(__instance, ____mediaEncoder.EncoderPath, InputUrl(inputPath), partialArgs, outputPaths, cancellationToken);
            return false;
        }
        catch (Exception ex)
        {
            _logger?.LogWarning(ex, "[Tentacle] Tentacle VOD subtitle check failed; Jellyfin extracts as before");
            return true;
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
