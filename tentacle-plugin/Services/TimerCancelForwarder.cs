using System;
using System.Collections.Concurrent;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Jellyfin.Data.Events;
using MediaBrowser.Controller.LiveTv;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Someone cancelled a recording in Jellyfin (the Record button again, the TV app, the
/// schedule page): Tentacle is told, so a recording one of its rules made is skipped for
/// good instead of being booked again. Jellyfin raises TimerCancelled only for a cancel
/// through its API, never for a guide refresh dropping a timer, which is exactly the
/// difference Tentacle can't see from the timer list.
/// </summary>
/// <remarks>
/// The plugin has no shared secret with Tentacle, so Tentacle confirms: it asks
/// GET /Tentacle/TimerCancelled/{id} with the server's API key (see <see cref="RecentTimerCancels"/>).
/// </remarks>
public sealed class TimerCancelForwarder : IHostedService, IDisposable
{
    private readonly ILiveTvManager _liveTv;
    private readonly ILogger<TimerCancelForwarder> _logger;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(10) };

    public TimerCancelForwarder(ILiveTvManager liveTv, ILogger<TimerCancelForwarder> logger)
    {
        _liveTv = liveTv;
        _logger = logger;
    }

    public Task StartAsync(CancellationToken cancellationToken)
    {
        _liveTv.TimerCancelled += OnTimerCancelled;
        return Task.CompletedTask;
    }

    public Task StopAsync(CancellationToken cancellationToken)
    {
        _liveTv.TimerCancelled -= OnTimerCancelled;
        return Task.CompletedTask;
    }

    private void OnTimerCancelled(object? sender, GenericEventArgs<TimerEventInfo> e)
    {
        var id = e?.Argument?.Id;
        var baseUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
        if (string.IsNullOrEmpty(id) || string.IsNullOrEmpty(baseUrl))
        {
            return;
        }

        RecentTimerCancels.Record(id);
        _ = Task.Run(async () =>
        {
            try
            {
                var body = new StringContent(JsonSerializer.Serialize(new { timer_id = id }), Encoding.UTF8, "application/json");
                using var response = await _http.PostAsync($"{baseUrl}/api/live/recordings/cancelled", body).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                _logger.LogDebug("[Tentacle] Recording cancel not passed on: {Error}", ex.Message);
            }
        });
    }

    public void Dispose() => _http.Dispose();
}

/// <summary>Timer ids cancelled through Jellyfin in the last 15 minutes, for Tentacle to confirm.</summary>
public static class RecentTimerCancels
{
    private static readonly TimeSpan Lifetime = TimeSpan.FromMinutes(15);
    private static readonly ConcurrentDictionary<string, DateTime> Entries = new();

    private static string Key(string id) => id.Replace("-", string.Empty, StringComparison.Ordinal).ToLowerInvariant();

    public static void Record(string id)
    {
        var now = DateTime.UtcNow;
        foreach (var entry in Entries)
        {
            if (entry.Value <= now)
            {
                Entries.TryRemove(entry.Key, out _);
            }
        }

        Entries[Key(id)] = now + Lifetime;
    }

    public static bool Contains(string id) =>
        Entries.TryGetValue(Key(id), out var expiry) && expiry > DateTime.UtcNow;
}
