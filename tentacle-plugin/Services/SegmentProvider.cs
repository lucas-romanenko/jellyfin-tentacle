using System.Net.Http;
using System.Text.Json;
using Jellyfin.Database.Implementations.Enums;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Model;
using MediaBrowser.Model.MediaSegments;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Skip intro / Skip credits: the intro and end credits Tentacle found
/// (services/segments.py: chapter marks, the same sound in two episodes,
/// credits on black) as Jellyfin media segments, so Jellyfin Web, the phone
/// apps and the TV app offer Skip intro / Skip credits and next-episode at
/// the credits. Jellyfin asks when it refreshes an item's segments; Tentacle
/// has that done (<c>/Tentacle/Segments/Refresh</c>) whenever it finds or
/// changes them. Per item: a stream and a download of one episode are two
/// items, each with its own times.
/// </summary>
public sealed class SegmentProvider : IMediaSegmentProvider
{
    private static readonly HttpClient Client = new() { Timeout = TimeSpan.FromSeconds(10) };
    private readonly ILogger<SegmentProvider> _logger;

    public SegmentProvider(ILogger<SegmentProvider> logger)
    {
        _logger = logger;
    }

    /// <inheritdoc />
    public string Name => "Tentacle";

    /// <inheritdoc />
    public ValueTask<bool> Supports(BaseItem item) => ValueTask.FromResult(item is Episode or Movie);

    /// <inheritdoc />
    public async Task<IReadOnlyList<MediaSegmentDto>> GetMediaSegments(MediaSegmentGenerationRequest request, CancellationToken cancellationToken)
    {
        var baseUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
        if (string.IsNullOrEmpty(baseUrl))
        {
            return Array.Empty<MediaSegmentDto>();
        }

        try
        {
            var body = await Client.GetStringAsync($"{baseUrl}/api/segments/{request.ItemId:N}", cancellationToken).ConfigureAwait(false);
            return Parse(request.ItemId, body);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _logger.LogInformation("[Tentacle] Skip intro/credits times for {Item} not read from Tentacle: {Error}", request.ItemId, ex.Message);
            return Array.Empty<MediaSegmentDto>();
        }
    }

    /// <summary>Tentacle's answer as Jellyfin segments.</summary>
    public static IReadOnlyList<MediaSegmentDto> Parse(Guid itemId, string json)
    {
        using var doc = JsonDocument.Parse(json);
        var list = new List<MediaSegmentDto>();
        if (!doc.RootElement.TryGetProperty("segments", out var segs) || segs.ValueKind != JsonValueKind.Array)
        {
            return list;
        }

        foreach (var s in segs.EnumerateArray())
        {
            var type = s.TryGetProperty("type", out var t) && t.GetString() == "Intro" ? MediaSegmentType.Intro
                : s.TryGetProperty("type", out t) && t.GetString() == "Outro" ? MediaSegmentType.Outro
                : (MediaSegmentType?)null;
            if (type is null || !s.TryGetProperty("start_ms", out var a) || !s.TryGetProperty("end_ms", out var b))
            {
                continue;
            }

            var start = a.GetInt64() * TimeSpan.TicksPerMillisecond;
            var end = b.GetInt64() * TimeSpan.TicksPerMillisecond;
            if (end <= start)
            {
                continue;
            }

            list.Add(new MediaSegmentDto { ItemId = itemId, Type = type.Value, StartTicks = start, EndTicks = end });
        }

        return list;
    }
}
