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

    public DateTimeOffset LastModified => DateTimeOffset.UtcNow;

    public long Length => _content.Length;

    public string Name => _original.Name;

    public string? PhysicalPath => null;

    public Stream CreateReadStream() => new MemoryStream(_content);
}
