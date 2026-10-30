using System.Diagnostics;
using System.Globalization;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// A finished Live TV recording whose clock jumps (#869): the provider closed the stream,
/// Tentacle dialled again and the new stream came with its own clock, hours away. Nothing
/// is lost, but ffprobe (and so Jellyfin) takes the length from the first and last
/// timestamps: a 3 h 45 min game shows as 21.7 h and seeking past the jump misbehaves.
///
/// The check: the probed length against the file's wall-clock window (its creation, as
/// Jellyfin's DateCreated, to its last write). Far over it, ffmpeg copies the file
/// (<c>-fflags +genpts+discardcorrupt -c copy</c>: its MPEG-TS input carries on across a
/// timestamp jump instead of keeping it) into a hidden temp file next to it. The copy
/// takes the recording's place (one rename) only when its length fits the window, it
/// kept nearly every byte and the recording didn't change meanwhile; otherwise the copy is
/// deleted and the recording stays as it was.
///
/// Only the file work is here (no Jellyfin types); RecordingRetimeController finds the
/// item, refuses a recording that is still being written and refreshes it afterwards.
/// </summary>
public static class RecordingRetime
{
    /// <summary>The probed length may exceed the wall-clock window by this factor…</summary>
    public const double Slack = 1.25;

    /// <summary>…plus this many seconds before the recording counts as broken.</summary>
    public const double SlackSeconds = 600;

    /// <summary>A file written to this recently may still be recording.</summary>
    public static readonly TimeSpan Settle = TimeSpan.FromMinutes(10);

    /// <summary>The copy must keep this share of the recording's bytes.</summary>
    public const double MinSizeShare = 0.95;

    private const long SpareBytes = 2L * 1024 * 1024 * 1024;

    private static readonly SemaphoreSlim OneAtATime = new(1, 1);

    /// <summary>What the check found or did.</summary>
    public sealed record Outcome(
        string State,
        double? Duration = null,
        double? Wall = null,
        double? After = null,
        string? Error = null);

    /// <summary>True when a probed length is far over its wall-clock window.</summary>
    public static bool TooLong(double duration, double wall) => duration > (wall * Slack) + SlackSeconds;

    /// <summary>The hidden temp file the copy is written to (same folder, so the swap is one rename).</summary>
    public static string TempPath(string path) =>
        Path.Combine(Path.GetDirectoryName(path) ?? ".", "." + Path.GetFileName(path) + ".retime.tmp");

    /// <summary>
    /// Checks one finished recording and, when its clock jumps and <paramref name="dryRun"/>
    /// is false, puts a retimed copy in its place. States: ok, retimed, would_retime (dry
    /// run: the copy was good and is deleted), not_fixed (the copy wasn't good, nothing
    /// changed), recording (written to within <see cref="Settle"/>), busy (another check
    /// runs), no_window, no_space, missing, failed.
    /// </summary>
    public static async Task<Outcome> Run(
        string ffmpeg,
        string ffprobe,
        string path,
        DateTime createdUtc,
        bool dryRun,
        ILogger? logger,
        CancellationToken cancellationToken)
    {
        if (!await OneAtATime.WaitAsync(0, cancellationToken).ConfigureAwait(false))
        {
            return new Outcome("busy");
        }

        try
        {
            return await RunLocked(ffmpeg, ffprobe, path, createdUtc, dryRun, logger, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            OneAtATime.Release();
        }
    }

    private static async Task<Outcome> RunLocked(
        string ffmpeg,
        string ffprobe,
        string path,
        DateTime createdUtc,
        bool dryRun,
        ILogger? logger,
        CancellationToken cancellationToken)
    {
        var file = new FileInfo(path);
        if (!file.Exists)
        {
            return new Outcome("missing");
        }

        var written = file.LastWriteTimeUtc;
        var size = file.Length;
        if (DateTime.UtcNow - written < Settle)
        {
            return new Outcome("recording");
        }

        var wall = (written - createdUtc).TotalSeconds;
        if (wall <= 0)
        {
            return new Outcome("no_window");
        }

        var duration = await Probe(ffprobe, path, cancellationToken).ConfigureAwait(false);
        if (duration is null)
        {
            return new Outcome("failed", Wall: wall, Error: "ffprobe gave no duration");
        }

        if (!TooLong(duration.Value, wall))
        {
            return new Outcome("ok", duration, wall);
        }

        var free = new DriveInfo(file.DirectoryName ?? "/").AvailableFreeSpace;
        if (free < size + SpareBytes)
        {
            return new Outcome("no_space", duration, wall);
        }

        var temp = TempPath(path);
        DeleteQuietly(temp);
        logger?.LogInformation(
            "[Tentacle] Recording {Path}: probed {Duration:F0} s in a {Wall:F0} s window, copying it with continuous timestamps{Dry}",
            path, duration, wall, dryRun ? " (dry run)" : string.Empty);
        var swapped = false;
        try
        {
            // Copying runs at disk speed; allow 10 MB/s and never less than 10 minutes.
            var limit = TimeSpan.FromSeconds(Math.Max(600, size / (10 * 1024 * 1024)));
            var error = await Remux(ffmpeg, path, temp, limit, cancellationToken).ConfigureAwait(false);
            if (error is not null)
            {
                return new Outcome("failed", duration, wall, Error: error);
            }

            var after = await Probe(ffprobe, temp, cancellationToken).ConfigureAwait(false);
            var copied = new FileInfo(temp).Length;
            if (after is null || TooLong(after.Value, wall) || copied < size * MinSizeShare)
            {
                logger?.LogWarning(
                    "[Tentacle] Recording {Path}: the copy isn't right ({After} s, {Copied} of {Size} bytes); left as it was",
                    path, after?.ToString("F0", CultureInfo.InvariantCulture) ?? "no duration", copied, size);
                return new Outcome("not_fixed", duration, wall, after);
            }

            file.Refresh();
            if (!file.Exists || file.Length != size || file.LastWriteTimeUtc != written)
            {
                return new Outcome("recording", duration, wall, after);
            }

            if (dryRun)
            {
                return new Outcome("would_retime", duration, wall, after);
            }

            // The end of the recording stays its last write, so a later check finds the same window.
            File.SetLastWriteTimeUtc(temp, written);
            File.Move(temp, path, overwrite: true);
            swapped = true;
            logger?.LogInformation("[Tentacle] Recording {Path}: retimed, {Duration:F0} s → {After:F0} s", path, duration, after);
            return new Outcome("retimed", duration, wall, after);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return new Outcome("failed", duration, wall, Error: ex.Message);
        }
        finally
        {
            // Whatever happened (a bad copy, a dry run, a cancelled request), no copy is left behind.
            if (!swapped)
            {
                DeleteQuietly(temp);
            }
        }
    }

    /// <summary>The container's duration in seconds, as ffprobe reads it (Jellyfin does the same).</summary>
    public static async Task<double?> Probe(string ffprobe, string path, CancellationToken cancellationToken)
    {
        var (code, output, _) = await RunProcess(
            ffprobe,
            new[] { "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path },
            TimeSpan.FromMinutes(2),
            cancellationToken).ConfigureAwait(false);
        return code == 0 && double.TryParse(output.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var seconds) && seconds > 0
            ? seconds
            : null;
    }

    private static async Task<string?> Remux(string ffmpeg, string input, string output, TimeSpan limit, CancellationToken cancellationToken)
    {
        var (code, _, error) = await RunProcess(
            ffmpeg,
            new[]
            {
                "-nostdin", "-v", "error", "-y", "-fflags", "+genpts+discardcorrupt", "-i", input,
                "-map", "0", "-c", "copy", "-f", "mpegts", output,
            },
            limit,
            cancellationToken).ConfigureAwait(false);
        if (code == 0 && File.Exists(output))
        {
            return null;
        }

        var last = error.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).LastOrDefault();
        return code == -1 ? "ffmpeg took too long" : $"ffmpeg exited {code}: {last ?? "no message"}";
    }

    private static async Task<(int Code, string Output, string Error)> RunProcess(
        string file, IEnumerable<string> arguments, TimeSpan limit, CancellationToken cancellationToken)
    {
        var info = new ProcessStartInfo
        {
            FileName = file,
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var argument in arguments)
        {
            info.ArgumentList.Add(argument);
        }

        using var process = new Process { StartInfo = info };
        process.Start();
        try
        {
            // nice 19: a copy must never slow down someone watching
            process.PriorityClass = ProcessPriorityClass.Idle;
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception or PlatformNotSupportedException)
        {
        }

        var output = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var error = process.StandardError.ReadToEndAsync(cancellationToken);
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(limit);
        try
        {
            await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
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

            cancellationToken.ThrowIfCancellationRequested();
            return (-1, string.Empty, string.Empty);
        }

        return (process.ExitCode, await output.ConfigureAwait(false), await error.ConfigureAwait(false));
    }

    private static void DeleteQuietly(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
        }
    }
}
