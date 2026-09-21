using System.Reflection;
using Microsoft.AspNetCore.Http;
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

        // Match the exact embedded resource name (e.g. "Jellyfin.Plugin.Tentacle.Assets.imdb.svg")
        // rather than a ".{fileName}" suffix — the latter can resolve the wrong resource
        // (e.g. "rt-fresh.svg" matching "fresh.svg").
        var expectedSuffix = $".Assets.{fileName}";
        var resourceName = assembly.GetManifestResourceNames()
            .FirstOrDefault(n => n.EndsWith(expectedSuffix, StringComparison.OrdinalIgnoreCase));

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

        // Same rule as the injected JS/CSS (#56): only a URL carrying the current
        // ?v= stamp can never go stale, so only that may be cached for good. The
        // icon URLs the injected scripts build carry no stamp, and `immutable`
        // on those meant a changed icon was never fetched again. Without the
        // stamp the browser may keep its copy but must revalidate it; the boot
        // stamp is the validator, since the embedded bytes cannot change without
        // a restart.
        var cacheControl = AssetCaching.CacheControlFor(Request);
        if (cacheControl != AssetCaching.Immutable)
        {
            var etag = $"\"{Patching.IndexHtmlPatch.CacheBust}\"";
            Response.Headers["Cache-Control"] = AssetCaching.Revalidate;
            Response.Headers["ETag"] = etag;
            if (string.Equals(Request.Headers["If-None-Match"].ToString(), etag, StringComparison.Ordinal))
            {
                stream.Dispose();
                return StatusCode(StatusCodes.Status304NotModified);
            }
        }
        else
        {
            Response.Headers["Cache-Control"] = cacheControl;
        }

        return File(stream, contentType);
    }
}
