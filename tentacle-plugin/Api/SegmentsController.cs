using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.MediaSegments;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// Tentacle found or changed where these items' intro and end credits are
/// (services/segments.py): Jellyfin asks the plugin's segment provider again
/// and keeps the answer as the items' media segments, which every app reads.
/// </summary>
[ApiController]
[Route("Tentacle/Segments")]
public class SegmentsController : ControllerBase
{
    private readonly ILibraryManager _libraryManager;
    private readonly IMediaSegmentManager _segments;
    private readonly ILogger<SegmentsController> _logger;

    public SegmentsController(ILibraryManager libraryManager, IMediaSegmentManager segments, ILogger<SegmentsController> logger)
    {
        _libraryManager = libraryManager;
        _segments = segments;
        _logger = logger;
    }

    /// <summary>Refresh the media segments of these items (comma-separated ids).</summary>
    [HttpPost("Refresh")]
    [Authorize(Policy = "RequiresElevation")]
    public async Task<ActionResult> Refresh([FromQuery] string? itemIds, CancellationToken cancellationToken)
    {
        var n = 0;
        foreach (var raw in (itemIds ?? string.Empty).Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            if (!Guid.TryParse(raw, out var id) || _libraryManager.GetItemById(id) is not { } item)
            {
                continue;
            }

            try
            {
                await _segments.RunSegmentPluginProviders(item, _libraryManager.GetLibraryOptions(item), true, cancellationToken).ConfigureAwait(false);
                n++;
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                _logger.LogWarning(ex, "[Tentacle] Media segments of {Item} not refreshed", item.Name);
            }
        }

        _logger.LogInformation("[Tentacle] Skip intro/credits: media segments refreshed for {Count} item(s)", n);
        return Ok(new { refreshed = n });
    }
}
