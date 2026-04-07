using System.Reflection;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// Serves rating source icon assets (SVGs, PNGs) as embedded resources.
/// Route: /Tentacle/Assets/{fileName}
/// </summary>
[ApiController]
[Route("Tentacle/Assets")]
public class TentacleAssetsController : ControllerBase
{
    private static readonly Dictionary<string, string> ContentTypes = new(StringComparer.OrdinalIgnoreCase)
    {
        { ".svg", "image/svg+xml" },
        { ".png", "image/png" },
        { ".jpg", "image/jpeg" },
        { ".jpeg", "image/jpeg" },
        { ".gif", "image/gif" },
        { ".webp", "image/webp" },
        { ".ico", "image/x-icon" },
        { ".css", "text/css" },
        { ".js", "application/javascript" },
    };

    /// <summary>
    /// Serve an asset file from embedded resources.
    /// </summary>
    /// <param name="fileName">The file name to serve (e.g. "imdb.svg").</param>
    [HttpGet("{fileName}")]
    public ActionResult GetAsset(string fileName)
    {
        if (string.IsNullOrWhiteSpace(fileName))
        {
            return NotFound();
        }

        // Sanitize: only allow alphanumeric, dash, underscore, dot
        if (fileName.Any(c => !char.IsLetterOrDigit(c) && c != '-' && c != '_' && c != '.'))
        {
            return BadRequest("Invalid file name");
        }

        var assembly = typeof(TentacleAssetsController).Assembly;
        var resourceName = assembly.GetManifestResourceNames()
            .FirstOrDefault(n => n.EndsWith($".{fileName}", StringComparison.OrdinalIgnoreCase));

        if (resourceName == null)
        {
            return NotFound();
        }

        var stream = assembly.GetManifestResourceStream(resourceName);
        if (stream == null)
        {
            return NotFound();
        }

        var ext = Path.GetExtension(fileName);
        var contentType = ContentTypes.GetValueOrDefault(ext, "application/octet-stream");

        // Set long cache headers (1 year) — assets are versioned by plugin version
        Response.Headers["Cache-Control"] = "public, max-age=31536000, immutable";

        return File(stream, contentType);
    }
}
