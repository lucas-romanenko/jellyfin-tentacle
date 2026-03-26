using System;
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
/// Handles fetching hero/spotlight items from a designated playlist.
/// Registered as a separate section with Home Screen Sections plugin.
/// </summary>
public class TentacleHeroHandler
{
    private readonly ILibraryManager _libraryManager;
    private readonly IUserManager _userManager;
    private readonly IDtoService _dtoService;
    private readonly ILogger<TentacleHeroHandler> _logger;

    public TentacleHeroHandler(
        ILibraryManager libraryManager,
        IUserManager userManager,
        IDtoService dtoService,
        ILogger<TentacleHeroHandler> logger)
    {
        _libraryManager = libraryManager;
        _userManager = userManager;
        _dtoService = dtoService;
        _logger = logger;
    }

    /// <summary>
    /// Gets hero spotlight items from the configured playlist.
    /// Returns items with full backdrop/logo image data for hero display.
    /// </summary>
    public QueryResult<BaseItemDto> GetHeroResults(HomeScreenSectionPayload payload)
    {
        var playlistId = payload.AdditionalData;
        if (string.IsNullOrEmpty(playlistId))
        {
            return new QueryResult<BaseItemDto>();
        }

        var dtoOptions = new DtoOptions
        {
            Fields = new[]
            {
                ItemFields.PrimaryImageAspectRatio,
                ItemFields.Overview,
                ItemFields.Genres,
                ItemFields.MediaSourceCount,
            },
            ImageTypes = new[]
            {
                ImageType.Primary,
                ImageType.Backdrop,
                ImageType.Logo,
                ImageType.Banner,
                ImageType.Thumb,
            },
            ImageTypeLimit = 3,
        };

        if (!Guid.TryParse(playlistId, out var playlistGuid))
        {
            _logger.LogWarning("Invalid hero playlist ID: {PlaylistId}", playlistId);
            return new QueryResult<BaseItemDto>();
        }

        var item = _libraryManager.GetItemById(playlistGuid);
        if (item is not Playlist playlist)
        {
            _logger.LogWarning("Hero playlist not found: {PlaylistId}", playlistId);
            return new QueryResult<BaseItemDto>();
        }

        var user = _userManager.GetUserById(payload.UserId);
        if (user == null)
        {
            return new QueryResult<BaseItemDto>();
        }

        // Get items that have backdrop images (important for hero display)
        var rawItems = playlist.GetManageableItems()
            .Where(i => i.Item2.IsVisible(user))
            .ToArray();

        var heroItems = rawItems
            .GroupBy(x => x.Item2 is Episode ep ? (BaseItem)ep.Series : x.Item2)
            .Take(10)
            .Select(g => g.Key)
            .Where(i => i.GetImages(ImageType.Backdrop).Any())
            .ToList();

        return new QueryResult<BaseItemDto>(
            _dtoService.GetBaseItemDtos(heroItems, dtoOptions, user));
    }
}
