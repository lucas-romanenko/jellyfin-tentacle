using System.Collections.Concurrent;
using System.Text.Json;
using Jellyfin.Data.Enums;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Plugin.Tentacle.Api;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Net;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Querying;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// "More Like This" that is like this (#292).
///
/// Jellyfin 10.11's <c>GET /Items/{id}/Similar</c> returns a random sample of
/// everything that shares any genre or any tag with the title, and Tentacle's
/// own tags ("Recently Added Movies", "Netflix Movies") are on most of a
/// library: the row changed completely at every visit and the franchise
/// showed up only by luck. Every details page reads that endpoint (Tentacle's
/// in Jellyfin Web, Jellyfin's own, the TV app), so this filter answers it
/// for a movie or a show:
///
///   1. Candidates: TMDB's collection (the other films of the franchise),
///      recommendations and similar titles for it that are in the library
///      (Tentacle's <c>/api/similar</c>, mapped by TMDB id), the director's
///      and the first five actors' other titles, and the most-rated titles
///      sharing its genres. Every query runs as the caller, so Jellyfin
///      applies their libraries, parental rating and blocked tags.
///   2. A local score: same collection 100; TMDB's recommendation 40 down
///      to 21 by rank, similar 15 down to 6; same director 20; each of the
///      first five actors 10; each shared genre 10; each shared keyword 5
///      (never Tentacle's own tags, nor a tag on more than a sixth of the
///      genre pool); each shared studio 3. At least 20 to be shown (two
///      genres, or a genre and a person or keyword), ties by closer year,
///      rating, name. One per TMDB id (a VOD and a downloaded copy).
///   3. Fewer than four: topped up with the highest-rated titles sharing a
///      genre. Nothing at all: Jellyfin answers as before.
///
/// The ids are kept per title and user for six hours (a title removed
/// meanwhile is left out when the row is built); TMDB's answer is kept by the Tentacle
/// server for a week, here for six hours; when TMDB or Tentacle can't be
/// reached within <see cref="BackendTimeout"/> the row is the local score
/// alone. The same title gives the same row at every visit.
/// </summary>
public sealed class SimilarItemsFilter : IAsyncActionFilter
{
    private static readonly TimeSpan ResultTtl = TimeSpan.FromHours(6);
    private static readonly TimeSpan RelatedTtl = TimeSpan.FromHours(6);
    private static readonly TimeSpan RelatedFailTtl = TimeSpan.FromMinutes(5);
    internal static readonly TimeSpan BackendTimeout = TimeSpan.FromSeconds(3);
    private const int GenrePool = 300;
    private const int PersonPool = 60;
    private const int MinScore = 20;
    private const int MinResults = 4;
    private const int DefaultLimit = 12;
    private const int MaxIdLookups = 10;   // TMDB ids Tentacle couldn't map, looked up one by one

    private static readonly ConcurrentDictionary<(Guid Item, Guid User), (Guid[] Ids, DateTime At)> Results = new();
    // One computation per title and user at a time: Jellyfin Web asks twice per page.
    private static readonly ConcurrentDictionary<(Guid Item, Guid User), Lazy<Task<Guid[]>>> InFlight = new();
    private static readonly ConcurrentDictionary<string, (Related Value, DateTime Until)> RelatedCache = new();

    private readonly ILibraryManager _library;
    private readonly IUserManager _users;
    private readonly IDtoService _dto;
    private readonly IAuthorizationContext _auth;
    private readonly IHttpClientFactory _http;
    private readonly ILogger<SimilarItemsFilter> _logger;

    public SimilarItemsFilter(ILibraryManager library, IUserManager users, IDtoService dto,
        IAuthorizationContext auth, IHttpClientFactory http, ILogger<SimilarItemsFilter> logger)
    {
        _library = library;
        _users = users;
        _dto = dto;
        _auth = auth;
        _http = http;
        _logger = logger;
    }

    internal sealed record Related(int[] Collection, int[] Recommendations, int[] Similar, HashSet<string> TentacleTags,
        Dictionary<int, Guid[]> Library);

    private static bool IsSimilar(ActionExecutingContext context) =>
        context.ActionDescriptor is ControllerActionDescriptor action
        && action.ControllerName == "Library"
        && action.ActionName == "GetSimilarItems";

    /// <inheritdoc />
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        if (!IsSimilar(context))
        {
            await next().ConfigureAwait(false);
            return;
        }

        try
        {
            var answer = await AnswerAsync(context).ConfigureAwait(false);
            if (answer is not null)
            {
                context.Result = new OkObjectResult(answer);
                return;
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "[Tentacle] More Like This: falling back to Jellyfin's own list");
        }

        await next().ConfigureAwait(false);
    }

    private static T? Arg<T>(ActionExecutingContext context, string name)
        => context.ActionArguments.TryGetValue(name, out var v) && v is T t ? t : default;

    private async Task<QueryResult<BaseItemDto>?> AnswerAsync(ActionExecutingContext context)
    {
        var itemId = Arg<Guid>(context, "itemId");
        if (itemId.Equals(Guid.Empty))
        {
            return null;
        }

        var requested = Arg<Guid?>(context, "userId") ?? Guid.Empty;
        var (allowed, userId) = await CallerIdentity.ResolveAsync(_auth, context.HttpContext, requested).ConfigureAwait(false);
        if (!allowed || userId.Equals(Guid.Empty))
        {
            return null;   // Jellyfin decides (and refuses) as before
        }

        var user = _users.GetUserById(userId);
        if (user is null)
        {
            return null;
        }

        var item = _library.GetItemById<BaseItem>(itemId, user);
        item = item switch
        {
            Episode e => e.Series,
            Season s => s.Series,
            _ => item,
        };
        if (item is not Movie && item is not Series)
        {
            return null;   // music, people, folders: Jellyfin's own
        }

        var limit = Math.Clamp(Arg<int?>(context, "limit") ?? DefaultLimit, 1, 50);
        var key = (item.Id, user.Id);
        Guid[] ids;
        if (Results.TryGetValue(key, out var cached) && DateTime.UtcNow - cached.At < ResultTtl)
        {
            ids = cached.Ids;
        }
        else
        {
            var task = InFlight.GetOrAdd(key, _ => new Lazy<Task<Guid[]>>(async () =>
            {
                var clock = System.Diagnostics.Stopwatch.StartNew();
                var related = await RelatedAsync(item, context).ConfigureAwait(false);
                var backend = clock.ElapsedMilliseconds;
                var row = await Task.Run(() => Rank(item, user, related)).ConfigureAwait(false);
                var found = row.Select(i => i.Id).ToArray();
                Results[key] = (found, DateTime.UtcNow);
                _logger.LogInformation("[Tentacle] More Like This for {Name}: {Count} titles in {Ms} ms (Tentacle {Backend} ms)",
                    item.Name, found.Length, clock.ElapsedMilliseconds, backend);
                return found;
            })).Value;
            try
            {
                ids = await task.ConfigureAwait(false);
            }
            finally
            {
                InFlight.TryRemove(key, out _);
            }
        }

        var items = ids.Select(id => _library.GetItemById<BaseItem>(id, user)).Where(i => i is not null)
            .Take(limit).Cast<BaseItem>().ToList();
        if (items.Count == 0)
        {
            return null;
        }

        var fields = Arg<ItemFields[]>(context, "fields") ?? Array.Empty<ItemFields>();
        var options = new DtoOptions { Fields = fields, EnableImages = true, EnableUserData = true };
        var dtos = _dto.GetBaseItemDtos(items, options, user);
        return new QueryResult<BaseItemDto>(dtos);
    }

    /// <summary>Tentacle's answer for the title, or an empty one.</summary>
    private async Task<Related> RelatedAsync(BaseItem item, ActionExecutingContext context)
    {
        var none = new Related(Array.Empty<int>(), Array.Empty<int>(), Array.Empty<int>(), new HashSet<string>(StringComparer.OrdinalIgnoreCase),
            new Dictionary<int, Guid[]>());
        var tmdb = item.GetProviderId(MetadataProvider.Tmdb);
        var baseUrl = Plugin.Instance?.Configuration?.TentacleUrl?.TrimEnd('/');
        if (string.IsNullOrEmpty(baseUrl))
        {
            return none;
        }

        var kind = item is Movie ? "movie" : "tv";
        var cacheKey = $"{kind}:{tmdb}";
        if (RelatedCache.TryGetValue(cacheKey, out var hit) && DateTime.UtcNow < hit.Until)
        {
            return hit.Value;
        }

        // A title without a TMDB id still gets Tentacle's tags (tmdb id 0).
        var id = int.TryParse(tmdb, out var n) && n > 0 ? n : 0;
        try
        {
            var client = _http.CreateClient();
            client.Timeout = BackendTimeout;
            var body = await PluginKeysClient.GetSecuredStringAsync(client, $"{baseUrl}/api/similar/{kind}/{Math.Max(id, 1)}", context.HttpContext.Request).ConfigureAwait(false);
            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            int[] Ids(string name) => id > 0 && root.TryGetProperty(name, out var a) && a.ValueKind == JsonValueKind.Array
                ? a.EnumerateArray().Where(e => e.ValueKind == JsonValueKind.Number).Select(e => e.GetInt32()).ToArray()
                : Array.Empty<int>();
            var tags = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (root.TryGetProperty("tentacle_tags", out var t) && t.ValueKind == JsonValueKind.Array)
            {
                foreach (var e in t.EnumerateArray())
                {
                    if (e.GetString() is { Length: > 0 } s)
                    {
                        tags.Add(s);
                    }
                }
            }

            var library = new Dictionary<int, Guid[]>();
            if (root.TryGetProperty("library", out var lib) && lib.ValueKind == JsonValueKind.Object)
            {
                foreach (var entry in lib.EnumerateObject())
                {
                    if (int.TryParse(entry.Name, out var tmdbId) && entry.Value.ValueKind == JsonValueKind.Array)
                    {
                        library[tmdbId] = entry.Value.EnumerateArray()
                            .Select(e => Guid.TryParse(e.GetString(), out var g) ? g : Guid.Empty)
                            .Where(g => !g.Equals(Guid.Empty)).ToArray();
                    }
                }
            }

            var ok = root.TryGetProperty("tmdb_ok", out var okEl) && okEl.ValueKind == JsonValueKind.True;
            var value = new Related(Ids("collection"), Ids("recommendations"), Ids("similar"), tags, library);
            RelatedCache[cacheKey] = (value, DateTime.UtcNow + (ok || id == 0 ? RelatedTtl : RelatedFailTtl));
            return value;
        }
        catch (Exception ex)
        {
            _logger.LogInformation("[Tentacle] More Like This: Tentacle didn't answer for {Kind} {Id} ({Error}); library score only",
                kind, tmdb, ex.Message);
            RelatedCache[cacheKey] = (none, DateTime.UtcNow + RelatedFailTtl);
            return none;
        }
    }

    private sealed class Candidate
    {
        public required BaseItem Item { get; init; }

        public int Score { get; set; }
    }

    /// <summary>The scored row for one title and user, best first.</summary>
    private List<BaseItem> Rank(BaseItem item, User user, Related related)
    {
        var kind = item is Movie ? BaseItemKind.Movie : BaseItemKind.Series;
        var ownTmdb = item.GetProviderId(MetadataProvider.Tmdb);
        var pool = new Dictionary<Guid, Candidate>();

        Candidate Add(BaseItem i)
        {
            if (!pool.TryGetValue(i.Id, out var c))
            {
                c = new Candidate { Item = i };
                pool[i.Id] = c;
            }

            return c;
        }

        InternalItemsQuery Query() => new(user)
        {
            IncludeItemTypes = new[] { kind },
            Recursive = true,
            IsVirtualItem = false,
            ExcludeItemIds = new[] { item.Id },
            DtoOptions = new DtoOptions(false),
        };

        IEnumerable<BaseItem> ByTmdb(int id)
        {
            var q = Query();
            q.HasAnyProviderId = new Dictionary<string, string> { [MetadataProvider.Tmdb.ToString()] = id.ToString(System.Globalization.CultureInfo.InvariantCulture) };
            q.Limit = 2;
            return _library.GetItemList(q);
        }

        // TMDB: the franchise, then its recommendations and similar titles.
        // The ones Tentacle knows in the library come with their Jellyfin
        // ids: one query as the caller keeps those the caller may see. The
        // rest are looked up by TMDB id, at most MaxIdLookups of them.
        var wanted = related.Collection.Take(30).Select(id => (Id: id, Points: 100))
            .Concat(related.Recommendations.Take(20).Select((id, r) => (Id: id, Points: 40 - r)))
            .Concat(related.Similar.Take(20).Select((id, r) => (Id: id, Points: 15 - (r / 2))))
            .ToList();
        var known = wanted.Where(w => related.Library.ContainsKey(w.Id)).SelectMany(w => related.Library[w.Id]).Distinct().ToArray();
        var visible = new Dictionary<Guid, BaseItem>();
        if (known.Length > 0)
        {
            var q = Query();
            q.ItemIds = known;
            foreach (var i in _library.GetItemList(q))
            {
                visible[i.Id] = i;
            }
        }

        var lookups = 0;
        foreach (var (id, points) in wanted)
        {
            IEnumerable<BaseItem> hits;
            if (related.Library.TryGetValue(id, out var ids))
            {
                hits = ids.Where(visible.ContainsKey).Select(g => visible[g]);
            }
            else if (lookups < MaxIdLookups)
            {
                lookups++;
                hits = ByTmdb(id);
            }
            else
            {
                continue;
            }

            foreach (var i in hits)
            {
                Add(i).Score += points;
            }
        }

        // People: the director's and the first five actors' other titles.
        var people = _library.GetPeople(item);
        var director = people.FirstOrDefault(p => p.Type == PersonKind.Director);
        var actors = people.Where(p => p.Type == PersonKind.Actor).OrderBy(p => p.SortOrder ?? int.MaxValue).Take(5).ToList();
        IReadOnlyList<BaseItem> ByPerson(PersonInfo? p)
        {
            // By name: GetPerson(name) would make a Person item for a name the
            // library doesn't have, and PersonIds wants Person item ids.
            if (p is null || string.IsNullOrWhiteSpace(p.Name))
            {
                return Array.Empty<BaseItem>();
            }

            var q = Query();
            q.Person = p.Name;
            q.Limit = PersonPool;
            return _library.GetItemList(q);
        }

        // The genre pool: the most-rated titles sharing a genre. Side by side
        // with the people's titles.
        var genres = item.Genres ?? Array.Empty<string>();
        var who = new List<(PersonInfo? Person, int Points)> { (director, 20) };
        who.AddRange(actors.Select(a => ((PersonInfo?)a, 10)));
        var byPerson = new IReadOnlyList<BaseItem>[who.Count];
        var genrePool = new List<BaseItem>();
        Parallel.Invoke(new ParallelOptions { MaxDegreeOfParallelism = 6 },
            new Action[]
            {
                () =>
                {
                    if (genres.Length > 0)
                    {
                        var q = Query();
                        q.Genres = genres;
                        q.Limit = GenrePool;
                        q.OrderBy = new[] { (ItemSortBy.CommunityRating, SortOrder.Descending), (ItemSortBy.SortName, SortOrder.Ascending) };
                        genrePool = _library.GetItemList(q).ToList();
                    }
                },
            }.Concat(Enumerable.Range(0, who.Count).Select(k => (Action)(() => byPerson[k] = ByPerson(who[k].Person)))).ToArray());
        for (var k = 0; k < who.Count; k++)
        {
            foreach (var i in byPerson[k])
            {
                Add(i).Score += who[k].Points;
            }
        }

        foreach (var i in genrePool)
        {
            Add(i);
        }

        // Keywords: the title's own tags, without Tentacle's and without the
        // ones on a big part of the genre pool (a service, a list).
        var common = genrePool.SelectMany(i => (i.Tags ?? Array.Empty<string>()).Distinct(StringComparer.OrdinalIgnoreCase))
            .GroupBy(t => t, StringComparer.OrdinalIgnoreCase)
            .Where(g => genrePool.Count >= 12 && g.Count() > genrePool.Count / 6)
            .Select(g => g.Key);
        var skip = new HashSet<string>(related.TentacleTags, StringComparer.OrdinalIgnoreCase);
        skip.UnionWith(common);
        var keywords = new HashSet<string>((item.Tags ?? Array.Empty<string>()).Where(t => !skip.Contains(t)), StringComparer.OrdinalIgnoreCase);
        var genreSet = new HashSet<string>(genres, StringComparer.OrdinalIgnoreCase);
        var studios = new HashSet<string>(item.Studios ?? Array.Empty<string>(), StringComparer.OrdinalIgnoreCase);

        foreach (var c in pool.Values)
        {
            var i = c.Item;
            c.Score += 10 * (i.Genres ?? Array.Empty<string>()).Count(genreSet.Contains);
            c.Score += 5 * Math.Min(5, (i.Tags ?? Array.Empty<string>()).Count(keywords.Contains));
            c.Score += 3 * (i.Studios ?? Array.Empty<string>()).Count(studios.Contains);
        }

        var year = item.ProductionYear ?? 0;
        IOrderedEnumerable<Candidate> Order(IEnumerable<Candidate> cs) => cs
            .OrderByDescending(c => c.Score)
            .ThenBy(c => year > 0 && c.Item.ProductionYear is int y ? Math.Abs(y - year) : 999)
            .ThenByDescending(c => c.Item.CommunityRating ?? 0)
            .ThenBy(c => c.Item.SortName ?? c.Item.Name, StringComparer.OrdinalIgnoreCase)
            .ThenBy(c => c.Item.Id);

        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (!string.IsNullOrEmpty(ownTmdb))
        {
            seen.Add("tmdb:" + ownTmdb);
        }

        if (!string.IsNullOrEmpty(item.PresentationUniqueKey))
        {
            seen.Add("key:" + item.PresentationUniqueKey);
        }

        seen.Add("name:" + (item.Name ?? string.Empty).Trim().ToLowerInvariant() + ":"
                 + (item.ProductionYear?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "?"));

        bool First(BaseItem i)
        {
            var tmdb = i.GetProviderId(MetadataProvider.Tmdb);
            var keys = new[]
                {
                    string.IsNullOrEmpty(tmdb) ? null : "tmdb:" + tmdb,
                    string.IsNullOrEmpty(i.PresentationUniqueKey) ? null : "key:" + i.PresentationUniqueKey,
                    // Two copies of one title, one without a TMDB id: "Up" (2009) twice.
                    "name:" + (i.Name ?? string.Empty).Trim().ToLowerInvariant() + ":" + (i.ProductionYear?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "?"),
                }
                .Where(k => k is not null).Cast<string>().ToList();
            if (keys.Any(seen.Contains))
            {
                return false;
            }

            seen.UnionWith(keys);
            return true;
        }

        var row = Order(pool.Values.Where(c => c.Score >= MinScore)).Select(c => c.Item).Where(First).Take(50).ToList();
        if (row.Count < MinResults)
        {
            row.AddRange(Order(pool.Values.Where(c => c.Score < MinScore && c.Score > 0)).Select(c => c.Item).Where(First).Take(MinResults - row.Count + 8));
        }

        return row;
    }
}
