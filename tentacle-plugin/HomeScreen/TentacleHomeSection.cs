using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Playlists;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Querying;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.HomeScreen;

/// <summary>
/// Payload matching the HomeScreenSectionPayload contract from Home Screen Sections plugin.
/// </summary>
public class HomeScreenSectionPayload
{
    public Guid UserId { get; set; }

    public string? AdditionalData { get; set; }
}

/// <summary>
/// Handles fetching playlist items for Tentacle home screen sections.
/// Registered with Home Screen Sections plugin via reflection.
/// </summary>
public class TentacleResultsHandler
{
    private readonly ILibraryManager _libraryManager;
    private readonly IUserManager _userManager;
    private readonly IDtoService _dtoService;
    private readonly HomeScreenManager _homeScreenManager;
    private readonly ILogger<TentacleResultsHandler> _logger;

    // Cache playlist items for 5 minutes to avoid hammering Jellyfin API
    private static readonly ConcurrentDictionary<string, (DateTime Expiry, QueryResult<BaseItemDto> Result)> _itemCache = new();
    private static readonly TimeSpan ItemCacheDuration = TimeSpan.FromMinutes(5);

    public TentacleResultsHandler(
        ILibraryManager libraryManager,
        IUserManager userManager,
        IDtoService dtoService,
        HomeScreenManager homeScreenManager,
        ILogger<TentacleResultsHandler> logger)
    {
        _libraryManager = libraryManager;
        _userManager = userManager;
        _dtoService = dtoService;
        _homeScreenManager = homeScreenManager;
        _logger = logger;
    }

    /// <summary>
    /// Gets playlist results by playlist ID (stored in AdditionalData).
    /// Called by Home Screen Sections plugin via reflection.
    /// </summary>
    public QueryResult<BaseItemDto> GetPlaylistResults(HomeScreenSectionPayload payload)
    {
        var playlistId = payload.AdditionalData;
        if (string.IsNullOrEmpty(playlistId))
        {
            _logger.LogWarning("Tentacle section called with no playlist ID");
            return new QueryResult<BaseItemDto>();
        }

        // Check cache
        var cacheKey = $"{payload.UserId}_{playlistId}";
        if (_itemCache.TryGetValue(cacheKey, out var cached) && DateTime.UtcNow < cached.Expiry)
        {
            return cached.Result;
        }

        var dtoOptions = new DtoOptions
        {
            Fields = new[]
            {
                ItemFields.PrimaryImageAspectRatio,
                ItemFields.MediaSourceCount,
            },
            ImageTypes = new[]
            {
                ImageType.Primary,
                ImageType.Backdrop,
                ImageType.Banner,
                ImageType.Thumb,
            },
            ImageTypeLimit = 1,
        };

        // Resolve playlist by Jellyfin ID
        if (!Guid.TryParse(playlistId, out var playlistGuid))
        {
            _logger.LogWarning("Invalid playlist ID format: {PlaylistId}", playlistId);
            return new QueryResult<BaseItemDto>();
        }

        var item = _libraryManager.GetItemById(playlistGuid);
        if (item is not Playlist playlist)
        {
            _logger.LogWarning("Playlist not found for ID: {PlaylistId}", playlistId);
            return new QueryResult<BaseItemDto>();
        }

        var user = _userManager.GetUserById(payload.UserId);
        if (user == null)
        {
            _logger.LogWarning("User not found: {UserId}", payload.UserId);
            return new QueryResult<BaseItemDto>();
        }

        var rawItems = playlist.GetManageableItems()
            .Where(i => i.Item2.IsVisible(user))
            .ToArray();

        // Read max_items from home config for this row (default 20)
        var limit = 20;
        var config = _homeScreenManager.GetHomeConfig(payload.UserId);
        if (config?.Rows != null)
        {
            var row = config.Rows.FirstOrDefault(r => r.PlaylistId == playlistId);
            if (row?.MaxItems is > 0)
            {
                limit = row.MaxItems.Value;
            }
        }

        // Group episodes by series to avoid showing multiple episodes from same show
        var grouped = rawItems
            .GroupBy(x => x.Item2 is Episode ep ? (BaseItem)ep.Series : x.Item2)
            .Take(limit)
            .Select(g => g.Key)
            .ToList();

        var result = new QueryResult<BaseItemDto>(
            _dtoService.GetBaseItemDtos(grouped, dtoOptions, user));

        // Cache the result
        _itemCache[cacheKey] = (DateTime.UtcNow.Add(ItemCacheDuration), result);

        _logger.LogDebug("Tentacle section {PlaylistId}: returned {Count} items", playlistId, result.TotalRecordCount);
        return result;
    }

    /// <summary>
    /// Clears the item cache. Called when Tentacle triggers a refresh.
    /// </summary>
    public static void ClearItemCache()
    {
        _itemCache.Clear();
    }
}
