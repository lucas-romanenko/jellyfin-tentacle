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

    // The plugin process start, as a timestamp with second precision (what the
    // HTTP validator carries).
    private static readonly DateTimeOffset PluginLoaded =
        DateTimeOffset.FromUnixTimeSeconds(DateTimeOffset.UtcNow.ToUnixTimeSeconds());

    // Report a timestamp that is stable within a process but changes exactly
    // when the transformed body does. Returning UtcNow gave every response a
    // fresh Last-Modified/ETag, so a conditional request could never be answered
    // with 304 and every page load re-sent the whole document (#58). Reporting
    // only the SOURCE file's mtime went too far the other way: the injected
    // cache-buster changes on every restart or plugin update while the source and
    // the length stay the same, so ASP.NET's ETag (mtime ^ length) matched the
    // OLD body and browsers kept stale HTML — and with it the old script URLs —
    // after an upgrade, while the boot-stamp watchdog reloaded in a loop.
    public DateTimeOffset LastModified =>
        _original.LastModified > PluginLoaded ? _original.LastModified : PluginLoaded;

    public long Length => _content.Length;

    public string Name => _original.Name;

    public string? PhysicalPath => null;

    public Stream CreateReadStream() => new MemoryStream(_content);
}
