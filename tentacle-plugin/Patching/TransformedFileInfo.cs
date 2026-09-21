using Microsoft.Extensions.FileProviders;

namespace Jellyfin.Plugin.Tentacle.Patching;

/// <summary>
/// Wraps an IFileInfo with transformed (in-memory) content.
/// Used by IndexHtmlPatch to serve modified index.html.
/// </summary>
public class TransformedFileInfo : IFileInfo
{
    private readonly IFileInfo _original;
    private readonly byte[] _content;

    public TransformedFileInfo(IFileInfo original, byte[] content)
    {
        _original = original;
        _content = content;
    }

    public bool Exists => true;

    public bool IsDirectory => false;

    // Report the SOURCE file's timestamp, not "now". Returning UtcNow gave every
    // response a fresh Last-Modified/ETag, so a conditional request could never be
    // answered with 304 and every page load re-sent the whole document (#58).
    public DateTimeOffset LastModified => _original.LastModified;

    public long Length => _content.Length;

    public string Name => _original.Name;

    public string? PhysicalPath => null;

    public Stream CreateReadStream() => new MemoryStream(_content);
}
