using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Makes Jellyfin's own Download of a provider's title (a .strm) send the
/// video instead of the .strm text file.
///
/// Why: <c>GET /Items/{id}/Download</c> serves <c>item.Path</c> as it is,
/// and for a .strm that is a line of text with the stream's address. The
/// Jellyfin phone apps saved that in their Downloads tab (nothing to play
/// offline), a browser saved a tiny file, and Tentacle's Offline page had
/// nothing to keep.
///
/// How: an MVC filter around that action. Jellyfin first decides as always
/// (signed in, "Allow media downloading", the person may open the title, its
/// CanDownload), and only when its answer is the .strm file is the answer
/// replaced: the video is pulled from the address in the .strm and passed
/// on with its size, byte ranges (a download that broke off asks for the
/// rest) and a video file name.
///
/// Tentacle's signed /api/vod/ address is pulled with Tentacle's offline
/// header: there it is a "download" (routers/vod.py), one provider
/// connection that starts only on a free one and waits, the client's
/// connection open, while someone who wants to watch has it. A .strm with
/// the provider's own address is asked of Tentacle first (/api/vod/link) so
/// it goes the same way; any other address is pulled as it is, as Jellyfin
/// would to play it. Library files and everything else are untouched.
/// </summary>
public sealed class StrmDownloadFilter : IAsyncActionFilter
{
    /// <summary>The request header that marks a download for Tentacle's relay.</summary>
    public const string OfflineHeader = "X-Tentacle-Offline";

    // No timeout: the relay holds a download while someone watches, and a
    // film is gigabytes. The client's own connection ends it (RequestAborted).
    private static readonly HttpClient Client = new(new SocketsHttpHandler
    {
        AllowAutoRedirect = true,
        MaxAutomaticRedirections = 5,
        AutomaticDecompression = DecompressionMethods.None,
        ConnectTimeout = TimeSpan.FromSeconds(15),
    })
    { Timeout = Timeout.InfiniteTimeSpan };

    private readonly ILibraryManager _libraryManager;
    private readonly ILogger<StrmDownloadFilter> _logger;

    public StrmDownloadFilter(ILibraryManager libraryManager, ILogger<StrmDownloadFilter> logger)
    {
        _libraryManager = libraryManager;
        _logger = logger;
    }

    /// <inheritdoc />
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        if (context.ActionDescriptor is not ControllerActionDescriptor action
            || action.ControllerName != "Library" || action.ActionName != "GetDownload")
        {
            await next().ConfigureAwait(false);
            return;
        }

        var executed = await next().ConfigureAwait(false);
        try
        {
            if (executed.Exception is not null
                || executed.Result is not PhysicalFileResult file
                || !file.FileName.EndsWith(".strm", StringComparison.OrdinalIgnoreCase)
                || !context.ActionArguments.TryGetValue("itemId", out var raw) || raw is not Guid itemId)
            {
                return;
            }

            var item = _libraryManager.GetItemById(itemId);
            var target = item is Video { IsShortcut: true } video ? video.ShortcutPath : null;
            if (string.IsNullOrWhiteSpace(target))
            {
                target = (await File.ReadAllLinesAsync(file.FileName).ConfigureAwait(false))
                    .Select(l => l.Trim()).FirstOrDefault(l => l.Length > 0 && !l.StartsWith('#'));
            }

            if (string.IsNullOrWhiteSpace(target)
                || !Uri.TryCreate(target.Trim(), UriKind.Absolute, out var uri)
                || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
            {
                return;
            }

            var name = Path.GetFileNameWithoutExtension(file.FileName);
            executed.Result = new StrmVideoResult(uri, name, itemId, item?.Name ?? name, _logger);
        }
        catch (Exception ex)
        {
            // Jellyfin's own answer (the .strm) goes out rather than an error.
            _logger.LogWarning(ex, "[Tentacle] Download of a .strm: could not send the video instead");
        }
    }

    /// <summary>The file extension of a stream address ("mkv"), "mp4" when it has none.</summary>
    public static string ExtensionOf(Uri uri)
    {
        var ext = Path.GetExtension(uri.AbsolutePath).TrimStart('.').ToLowerInvariant();
        return ext.Length is > 0 and <= 5 && ext.All(char.IsLetterOrDigit) && ext != "strm" ? ext : "mp4";
    }

    /// <summary>The video's type for its extension.</summary>
    public static string ContentTypeFor(string ext) => ext switch
    {
        "mkv" => "video/x-matroska",
        "mp4" or "m4v" => "video/mp4",
        "avi" => "video/x-msvideo",
        "ts" => "video/mp2t",
        "webm" => "video/webm",
        "mov" => "video/quicktime",
        _ => "application/octet-stream",
    };

    /// <summary>
    /// Where to pull the video from, and whether that is Tentacle's relay
    /// (then the offline header goes with it).
    /// </summary>
    internal static async Task<(Uri Url, bool Relay)> SourceFor(Uri target, CancellationToken token)
    {
        if (VodNoVideoCopyFilter.IsTentacleVodUrl(target.ToString()))
        {
            return (target, true);
        }

        var tentacle = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
        if (string.IsNullOrEmpty(tentacle))
        {
            return (target, false);
        }

        try
        {
            using var body = new StringContent(JsonSerializer.Serialize(new { url = target.ToString() }), Encoding.UTF8, "application/json");
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(token);
            cts.CancelAfter(TimeSpan.FromSeconds(10));
            using var resp = await Client.PostAsync($"{tentacle}/api/vod/link", body, cts.Token).ConfigureAwait(false);
            if (resp.IsSuccessStatusCode)
            {
                using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false));
                if (doc.RootElement.TryGetProperty("path", out var path) && path.GetString() is { Length: > 0 } p
                    && Uri.TryCreate(tentacle + p, UriKind.Absolute, out var relay))
                {
                    return (relay, true);
                }
            }
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException)
        {
            // Tentacle not answering: pulled as Jellyfin would to play it.
        }

        return (target, false);
    }

    /// <summary>The video of a .strm, passed on as a download.</summary>
    private sealed class StrmVideoResult : IActionResult
    {
        private readonly Uri _target;
        private readonly string _fileBase;
        private readonly Guid _itemId;
        private readonly string _title;
        private readonly ILogger _logger;

        public StrmVideoResult(Uri target, string fileBase, Guid itemId, string title, ILogger logger)
        {
            _target = target;
            _fileBase = fileBase;
            _itemId = itemId;
            _title = title;
            _logger = logger;
        }

        public async Task ExecuteResultAsync(ActionContext context)
        {
            var http = context.HttpContext;
            var aborted = http.RequestAborted;
            var (url, relay) = await SourceFor(_target, aborted).ConfigureAwait(false);
            var ext = ExtensionOf(_target);

            using var request = new HttpRequestMessage(HttpMethod.Get, url);
            if (relay)
            {
                request.Headers.TryAddWithoutValidation(OfflineHeader, "1");
            }

            var range = http.Request.Headers.Range.ToString();
            var ifRange = http.Request.Headers.IfRange.ToString();
            // A resume for an earlier copy of the file (another size) starts over.
            if (!string.IsNullOrEmpty(range) && (string.IsNullOrEmpty(ifRange) || ifRange.StartsWith($"\"strm-{_itemId:N}-", StringComparison.Ordinal)))
            {
                request.Headers.TryAddWithoutValidation("Range", range);
            }

            HttpResponseMessage upstream;
            try
            {
                upstream = await Client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, aborted).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
            {
                _logger.LogWarning("[Tentacle] Download of {Title}: the stream did not answer ({Error})", _title, ex.GetType().Name);
                await Plain(http, 502, "The video can't be reached right now. Try again in a minute.").ConfigureAwait(false);
                return;
            }

            using (upstream)
            {
                var status = (int)upstream.StatusCode;
                if (status != 200 && status != 206)
                {
                    var text = await upstream.Content.ReadAsStringAsync(aborted).ConfigureAwait(false);
                    _logger.LogInformation("[Tentacle] Download of {Title}: the stream answered {Status}", _title, status);
                    await Plain(http, status == 503 ? 503 : status == 416 ? 416 : 502, DetailOf(text, status)).ConfigureAwait(false);
                    return;
                }

                var length = upstream.Content.Headers.ContentLength;
                var contentRange = upstream.Content.Headers.ContentRange;
                long? total = contentRange?.Length ?? (status == 200 ? length : null);
                var response = http.Response;
                response.StatusCode = status;
                response.ContentType = ContentTypeFor(ext);
                if (length is not null)
                {
                    response.ContentLength = length;
                }

                if (contentRange is not null)
                {
                    response.Headers.ContentRange = contentRange.ToString();
                }

                response.Headers.AcceptRanges = "bytes";
                if (total is not null)
                {
                    response.Headers.ETag = $"\"strm-{_itemId:N}-{total}\"";
                }

                var fileName = _fileBase + "." + ext;
                response.Headers.ContentDisposition = new ContentDispositionHeaderValue("attachment")
                {
                    FileName = "\"" + new string(fileName.Select(c => c < 32 || c > 126 || c == '"' || c == '\\' ? '_' : c).ToArray()) + "\"",
                    FileNameStar = fileName,
                }.ToString();
                _logger.LogInformation(
                    "[Tentacle] Download of {Title}: sending the video ({Range}{Via})",
                    _title,
                    string.IsNullOrEmpty(range) ? "from the start" : range,
                    relay ? ", through Tentacle" : ", straight from its address");

                long sent = 0;
                try
                {
                    await using var body = await upstream.Content.ReadAsStreamAsync(aborted).ConfigureAwait(false);
                    var buffer = new byte[81920];
                    int n;
                    while ((n = await body.ReadAsync(buffer, aborted).ConfigureAwait(false)) > 0)
                    {
                        await response.Body.WriteAsync(buffer.AsMemory(0, n), aborted).ConfigureAwait(false);
                        sent += n;
                    }
                }
                catch (Exception ex) when (ex is IOException or HttpRequestException or OperationCanceledException)
                {
                    // The client went away, or the stream broke off: below.
                }

                if (length is not null && sent < length && !aborted.IsCancellationRequested)
                {
                    // Short of what was promised: break the connection, so the
                    // client sees an interrupted download, not a finished one.
                    _logger.LogInformation("[Tentacle] Download of {Title} broke off after {Sent} bytes", _title, sent);
                    http.Abort();
                }
            }
        }

        private static string DetailOf(string text, int status)
        {
            try
            {
                using var doc = JsonDocument.Parse(text);
                if (doc.RootElement.ValueKind == JsonValueKind.Object
                    && doc.RootElement.TryGetProperty("detail", out var d) && d.ValueKind == JsonValueKind.String
                    && d.GetString() is { Length: > 0 } detail)
                {
                    return detail;
                }
            }
            catch (JsonException)
            {
            }

            return status == 503
                ? "The server is busy right now. Try the download again in a few minutes."
                : "The video can't be downloaded right now.";
        }

        private static async Task Plain(HttpContext http, int status, string text)
        {
            http.Response.StatusCode = status;
            http.Response.ContentType = "text/plain; charset=utf-8";
            await http.Response.WriteAsync(text).ConfigureAwait(false);
        }
    }
}
