namespace Jellyfin.Plugin.Tentacle.HomeScreen;

/// <summary>
/// Tentacle's own home rows that aren't playlists (the server's services/home_rows.py):
/// Continue Watching, On Now and Recordings. No Jellyfin home section is drawn any more
/// (Lucas, 2026-10-26); each of these is configured in Tentacle like any row and looks the
/// same on the web and the TV app.
/// </summary>
public static class TentacleRows
{
    /// <summary>Continue Watching's section id.</summary>
    public const string ContinueWatching = "resumevideo";

    /// <summary>On Now's section id.</summary>
    public const string OnNow = "onnow";

    /// <summary>Recordings' section id.</summary>
    public const string Recordings = "recordings";

    /// <summary>Items in a row unless it says otherwise.</summary>
    public const int DefaultLimit = 20;

    private const int MaxLimit = 30;

    /// <summary>One of Tentacle's rows: its id, name and starting shape (null: its own card).</summary>
    public sealed record Row(string SectionId, string DisplayName, string? DefaultShape);

    private static readonly Row[] All =
    {
        new(ContinueWatching, "Continue Watching", "wide"),
        new(OnNow, "On Now", null),
        new(Recordings, "Recordings", "poster"),
    };

    /// <summary>
    /// The Tentacle row a saved section id stands for: Jellyfin's "resume" is Continue
    /// Watching; any other Jellyfin section is none.
    /// </summary>
    public static Row? Find(string? sectionId)
    {
        var id = string.Equals(sectionId, "resume", StringComparison.OrdinalIgnoreCase) ? ContinueWatching : sectionId;
        return All.FirstOrDefault(r => string.Equals(r.SectionId, id, StringComparison.OrdinalIgnoreCase));
    }

    /// <summary>
    /// How the row's cards are drawn: "poster" or "wide" as chosen in Tentacle, else the
    /// row's starting shape (a layout saved before rows had one: Continue Watching wide,
    /// as the web drew it); "onnow" for On Now's own card.
    /// </summary>
    public static string ShapeOf(RowConfig row)
    {
        var r = Find(row.SectionId);
        if (r?.DefaultShape is null)
        {
            return "onnow";
        }

        return row.Shape is "poster" or "wide" ? row.Shape : r.DefaultShape;
    }

    /// <summary>The row's item limit (at most 30, as for playlist rows).</summary>
    public static int Limit(RowConfig row) => Math.Min(row.MaxItems is > 0 ? row.MaxItems.Value : DefaultLimit, MaxLimit);

    /// <summary>
    /// Continue Watching's order: most recently watched first, one card per show (an
    /// episode in progress wins over the show's next one; the first listed wins a tie),
    /// at most <paramref name="limit"/>.
    /// </summary>
    /// <param name="entries">Item id, its show's id (empty for a film) and when it was last watched; resume items first.</param>
    /// <param name="limit">How many to keep.</param>
    public static List<Guid> ContinueWatchingOrder(IReadOnlyList<(Guid Id, Guid SeriesId, DateTime Played)> entries, int limit)
    {
        var seen = new HashSet<Guid>();
        var kept = new List<(Guid Id, DateTime Played, int Index)>();
        for (var i = 0; i < entries.Count; i++)
        {
            var e = entries[i];
            if (!seen.Add(e.Id) || (e.SeriesId != Guid.Empty && !seen.Add(e.SeriesId)))
            {
                continue;
            }

            kept.Add((e.Id, e.Played, i));
        }

        return kept
            .OrderByDescending(k => k.Played)
            .ThenBy(k => k.Index)
            .Take(limit)
            .Select(k => k.Id)
            .ToList();
    }
}
