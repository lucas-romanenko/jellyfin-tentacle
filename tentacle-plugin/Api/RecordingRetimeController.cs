using Jellyfin.Plugin.Tentacle.Services;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.LiveTv;
using MediaBrowser.Controller.MediaEncoding;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Model.IO;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// The Tentacle server's after-recording check (#869), called with the server's API key
/// for each finished Live TV recording: the clock-jump check and fix (see
/// <see cref="RecordingRetime"/>), and the recording's real start as its PremiereDate
/// (Jellyfin leaves it empty on recordings). Only files in Jellyfin's recording folders,
/// never one Jellyfin is still recording.
/// </summary>
[ApiController]
[Route("Tentacle/Recordings")]
[Authorize(Policy = "RequiresElevation")]
public class RecordingRetimeController : ControllerBase
{
    private readonly ILibraryManager _libraryManager;
    private readonly IRecordingsManager _recordingsManager;
    private readonly IMediaEncoder _mediaEncoder;
    private readonly IFileSystem _fileSystem;
    private readonly ILogger<RecordingRetimeController> _logger;

    public RecordingRetimeController(
        ILibraryManager libraryManager,
        IRecordingsManager recordingsManager,
        IMediaEncoder mediaEncoder,
        IFileSystem fileSystem,
        ILogger<RecordingRetimeController> logger)
    {
        _libraryManager = libraryManager;
        _recordingsManager = recordingsManager;
        _mediaEncoder = mediaEncoder;
        _fileSystem = fileSystem;
        _logger = logger;
    }

    /// <summary>
    /// Checks one finished recording's clock and, when it jumps, puts a retimed copy in its
    /// place and refreshes the item (its DateCreated kept). 200 with the state (ok, retimed,
    /// would_retime, not_fixed, no_window, no_space, missing, failed); 409 while it records
    /// or another check runs; 404/400 for an item that isn't a recording.
    /// </summary>
    /// <param name="itemId">The recording's item id.</param>
    /// <param name="dryRun">Copy and check, but keep the recording as it is.</param>
    /// <param name="cancellationToken">The request's token.</param>
    [HttpPost("{itemId}/Retime")]
    public async Task<ActionResult> Retime([FromRoute] Guid itemId, [FromQuery] bool dryRun = false, CancellationToken cancellationToken = default)
    {
        var (item, refusal) = Recording(itemId);
        if (item is null)
        {
            return refusal!;
        }

        if (_recordingsManager.GetActiveRecordingInfo(item.Path) is not null)
        {
            return Conflict(new { state = "recording" });
        }

        var created = item.DateCreated;
        var outcome = await RecordingRetime.Run(
            _mediaEncoder.EncoderPath, _mediaEncoder.ProbePath, item.Path, created, dryRun, _logger, cancellationToken).ConfigureAwait(false);
        if (outcome.State == "retimed")
        {
            await Refresh(itemId, created, cancellationToken).ConfigureAwait(false);
        }

        var body = new { state = outcome.State, duration = outcome.Duration, wall = outcome.Wall, after = outcome.After, error = outcome.Error };
        return outcome.State is "recording" or "busy" ? Conflict(body) : Ok(body);
    }

    /// <summary>
    /// Sets a recording's PremiereDate (and ProductionYear) to its programme's real start,
    /// only when Jellyfin has none: 200 {set: true|false}.
    /// </summary>
    /// <param name="itemId">The recording's item id.</param>
    /// <param name="date">The programme's start (UTC).</param>
    /// <param name="cancellationToken">The request's token.</param>
    [HttpPost("{itemId}/Premiere")]
    public async Task<ActionResult> Premiere([FromRoute] Guid itemId, [FromQuery] DateTime date, CancellationToken cancellationToken = default)
    {
        var (item, refusal) = Recording(itemId);
        if (item is null)
        {
            return refusal!;
        }

        if (date == default)
        {
            return BadRequest(new { state = "no_date" });
        }

        if (item.PremiereDate.HasValue)
        {
            return Ok(new { set = false });
        }

        var utc = date.Kind == DateTimeKind.Unspecified ? DateTime.SpecifyKind(date, DateTimeKind.Utc) : date.ToUniversalTime();
        item.PremiereDate = utc;
        item.ProductionYear ??= utc.Year;
        await item.UpdateToRepositoryAsync(ItemUpdateType.MetadataEdit, cancellationToken).ConfigureAwait(false);
        return Ok(new { set = true });
    }

    private (Video? Item, ActionResult? Refusal) Recording(Guid itemId)
    {
        if (_libraryManager.GetItemById(itemId) is not Video item || string.IsNullOrEmpty(item.Path))
        {
            return (null, NotFound(new { state = "missing" }));
        }

        var roots = _recordingsManager.GetRecordingFolders()
            .SelectMany(f => f.Locations ?? Array.Empty<string>())
            .Where(l => !string.IsNullOrEmpty(l))
            .Select(l => Path.TrimEndingDirectorySeparator(Path.GetFullPath(l)) + Path.DirectorySeparatorChar);
        var path = Path.GetFullPath(item.Path);
        if (!path.EndsWith(".ts", StringComparison.OrdinalIgnoreCase)
            || !roots.Any(r => path.StartsWith(r, StringComparison.Ordinal)))
        {
            return (null, BadRequest(new { state = "not_a_recording" }));
        }

        return (item, null);
    }

    private async Task Refresh(Guid itemId, DateTime created, CancellationToken cancellationToken)
    {
        if (_libraryManager.GetItemById(itemId) is not { } item)
        {
            return;
        }

        // Jellyfin probes the file again (its length, streams); the metadata and pictures stay.
        var options = new MetadataRefreshOptions(new DirectoryService(_fileSystem))
        {
            MetadataRefreshMode = MetadataRefreshMode.FullRefresh,
            ImageRefreshMode = MetadataRefreshMode.ValidationOnly,
            ReplaceAllMetadata = false,
            ReplaceAllImages = false,
        };
        await item.RefreshMetadata(options, cancellationToken).ConfigureAwait(false);

        // The new file was born now: the recording keeps the date it was made.
        if (_libraryManager.GetItemById(itemId) is { } fresh && fresh.DateCreated != created)
        {
            fresh.DateCreated = created;
            await fresh.UpdateToRepositoryAsync(ItemUpdateType.MetadataEdit, cancellationToken).ConfigureAwait(false);
        }
    }
}
