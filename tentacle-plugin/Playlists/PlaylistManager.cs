using System.Text.Json;
using Jellyfin.Data.Enums;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Database.Implementations.Enums;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Playlists;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Playlists;
using MediaBrowser.Model.Querying;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Playlists;

/// <summary>
/// Manages Jellyfin playlists from SmartList config.json files.
/// Reads configs from the smartlists directory, queries Jellyfin library,
/// creates/updates playlists, and persists JellyfinPlaylistId back to config.
/// </summary>
public class PlaylistManager
{
    private readonly ILibraryManager _libraryManager;
    private readonly IPlaylistManager _playlistManager;
    private readonly IUserManager _userManager;
    private readonly ILogger<PlaylistManager> _logger;

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
    };

    public PlaylistManager(
        ILibraryManager libraryManager,
        IPlaylistManager playlistManager,
        IUserManager userManager,
        ILogger<PlaylistManager> logger)
    {
        _libraryManager = libraryManager;
        _playlistManager = playlistManager;
        _userManager = userManager;
        _logger = logger;
    }

    /// <summary>
    /// Refreshes all playlists from SmartList config files.
    /// Returns the number of playlists processed.
    /// </summary>
    public async Task<int> RefreshAllPlaylists(CancellationToken cancellationToken = default)
    {
        var smartListsPath = Plugin.Instance?.Configuration.SmartListsPath;
        if (string.IsNullOrEmpty(smartListsPath) || !Directory.Exists(smartListsPath))
        {
            _logger.LogWarning("SmartLists path not configured or does not exist: {Path}", smartListsPath);
            return 0;
        }

        var configs = LoadAllConfigs(smartListsPath);
        _logger.LogInformation("Found {Count} SmartList configs to process", configs.Count);

        var userId = GetDefaultUserId();
        if (userId == null)
        {
            _logger.LogError("No Jellyfin users found. Cannot create playlists.");
            return 0;
        }

        int processed = 0;
        foreach (var (filePath, config) in configs)
        {
            if (cancellationToken.IsCancellationRequested)
            {
                break;
            }

            if (!config.Enabled || config.Type != "Playlist")
            {
                continue;
            }

            try
            {
                await ProcessPlaylist(filePath, config, userId.Value, cancellationToken);
                processed++;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Failed to process SmartList: {Name}", config.Name);
            }
        }

        _logger.LogInformation("Processed {Count} SmartList playlists", processed);
        return processed;
    }

    private List<(string FilePath, SmartListConfig Config)> LoadAllConfigs(string basePath)
    {
        var results = new List<(string, SmartListConfig)>();

        // Scan for config.json files in subdirectories (new format: smartlists/{guid}/config.json)
        foreach (var dir in Directory.GetDirectories(basePath))
        {
            var configPath = Path.Combine(dir, "config.json");
            if (File.Exists(configPath))
            {
                var config = TryLoadConfig(configPath);
                if (config != null)
                {
                    results.Add((configPath, config));
                }
            }
        }

        // Also scan for flat .json files (legacy format: smartlists/{guid}.json)
        foreach (var jsonFile in Directory.GetFiles(basePath, "*.json"))
        {
            var config = TryLoadConfig(jsonFile);
            if (config != null)
            {
                // Avoid duplicates if both formats exist
                if (!results.Any(r => r.Item2.Id == config.Id))
                {
                    results.Add((jsonFile, config));
                }
            }
        }

        return results;
    }

    private SmartListConfig? TryLoadConfig(string path)
    {
        try
        {
            var json = File.ReadAllText(path);
            return JsonSerializer.Deserialize<SmartListConfig>(json, JsonOptions);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to parse SmartList config: {Path}", path);
            return null;
        }
    }

    private async Task ProcessPlaylist(
        string configPath,
        SmartListConfig config,
        Guid userId,
        CancellationToken cancellationToken)
    {
        _logger.LogDebug("Processing SmartList: {Name}", config.Name);

        // Query library items matching the expression rules
        var matchingItems = QueryLibraryItems(config, userId);

        if (matchingItems.Count == 0)
        {
            _logger.LogDebug("No matching items for SmartList: {Name}", config.Name);
        }

        var itemIds = matchingItems.Select(i => i.Id).ToArray();

        // Check if playlist already exists
        if (!string.IsNullOrEmpty(config.JellyfinPlaylistId)
            && Guid.TryParse(config.JellyfinPlaylistId, out var existingId))
        {
            var existing = _libraryManager.GetItemById(existingId);
            if (existing is Playlist playlist)
            {
                // Update existing playlist items
                await UpdatePlaylistItems(playlist, itemIds, userId, cancellationToken);
                _logger.LogInformation("Updated playlist '{Name}' with {Count} items", config.Name, itemIds.Length);
                return;
            }

            _logger.LogWarning("Playlist {Id} not found, will create new one for '{Name}'", config.JellyfinPlaylistId, config.Name);
        }

        // Create new playlist
        var request = new PlaylistCreationRequest
        {
            Name = config.Name,
            UserId = userId,
            MediaType = GetMediaType(config),
            ItemIdList = itemIds,
        };

        var result = await _playlistManager.CreatePlaylist(request);

        if (!string.IsNullOrEmpty(result.Id))
        {
            config.JellyfinPlaylistId = result.Id;
            SaveConfig(configPath, config);
            _logger.LogInformation("Created playlist '{Name}' (ID: {Id}) with {Count} items",
                config.Name, config.JellyfinPlaylistId, itemIds.Length);
        }
        else
        {
            _logger.LogError("Failed to create playlist for SmartList: {Name}", config.Name);
        }
    }

    private List<BaseItem> QueryLibraryItems(SmartListConfig config, Guid userId)
    {
        var user = _userManager.GetUserById(userId);
        var query = new InternalItemsQuery(user)
        {
            Recursive = true,
            IsVirtualItem = false,
        };

        // Set media types
        var itemTypes = new List<BaseItemKind>();
        foreach (var mt in config.MediaTypes)
        {
            if (Enum.TryParse<BaseItemKind>(mt, true, out var kind))
            {
                itemTypes.Add(kind);
            }
        }

        if (itemTypes.Count > 0)
        {
            query.IncludeItemTypes = itemTypes.ToArray();
        }

        // Apply expression filters (simplified: supports Tags, Genres, Year)
        foreach (var exprSet in config.ExpressionSets)
        {
            foreach (var expr in exprSet.Expressions)
            {
                ApplyExpression(query, expr);
            }
        }

        // Apply sorting
        if (config.Order?.SortOptions is { Count: > 0 } sorts)
        {
            var orderBy = new List<(ItemSortBy, SortOrder)>();
            foreach (var sort in sorts)
            {
                if (Enum.TryParse<ItemSortBy>(sort.SortBy, true, out var sortBy))
                {
                    var order = sort.SortOrder.Equals("Descending", StringComparison.OrdinalIgnoreCase)
                        ? SortOrder.Descending
                        : SortOrder.Ascending;
                    orderBy.Add((sortBy, order));
                }
            }

            if (orderBy.Count > 0)
            {
                query.OrderBy = orderBy;
            }
        }

        // Apply limit
        if (config.MaxItems.HasValue && config.MaxItems.Value > 0)
        {
            query.Limit = config.MaxItems.Value;
        }

        var result = _libraryManager.GetItemsResult(query);
        return result.Items.ToList();
    }

    private static void ApplyExpression(InternalItemsQuery query, SmartListExpression expr)
    {
        switch (expr.MemberName.ToLowerInvariant())
        {
            case "tags":
                if (expr.Operator.Equals("Contains", StringComparison.OrdinalIgnoreCase))
                {
                    query.Tags = query.Tags is { Length: > 0 }
                        ? query.Tags.Append(expr.TargetValue).ToArray()
                        : new[] { expr.TargetValue };
                }

                break;

            case "genres":
                if (expr.Operator.Equals("Contains", StringComparison.OrdinalIgnoreCase))
                {
                    query.Genres = query.Genres?.Count > 0
                        ? query.Genres.Append(expr.TargetValue).ToArray()
                        : new[] { expr.TargetValue };
                }

                break;

            case "productionyear":
            case "year":
                if (int.TryParse(expr.TargetValue, out var year))
                {
                    if (expr.Operator.Equals("Equals", StringComparison.OrdinalIgnoreCase))
                    {
                        query.Years = new[] { year };
                    }
                    else if (expr.Operator.Equals("GreaterThan", StringComparison.OrdinalIgnoreCase))
                    {
                        query.MinPremiereDate = new DateTime(year + 1, 1, 1);
                    }
                    else if (expr.Operator.Equals("LessThan", StringComparison.OrdinalIgnoreCase))
                    {
                        query.MaxPremiereDate = new DateTime(year, 1, 1);
                    }
                }

                break;

            case "communityrating":
            case "rating":
                if (double.TryParse(expr.TargetValue, out var rating))
                {
                    if (expr.Operator.Equals("GreaterThan", StringComparison.OrdinalIgnoreCase))
                    {
                        query.MinCommunityRating = rating;
                    }
                }

                break;
        }
    }

    private async Task UpdatePlaylistItems(
        Playlist playlist,
        Guid[] newItemIds,
        Guid userId,
        CancellationToken cancellationToken)
    {
        // Get current items
        var currentItems = playlist.GetManageableItems()
            .Select(i => i.Item2.Id)
            .ToHashSet();

        // Remove items no longer matching
        var toRemove = currentItems.Except(newItemIds).ToList();
        if (toRemove.Count > 0)
        {
            await _playlistManager.RemoveItemFromPlaylistAsync(
                playlist.Id.ToString("N"),
                toRemove.Select(id => id.ToString("N")).ToArray());
        }

        // Add new items not already in playlist
        var toAdd = newItemIds.Except(currentItems).ToArray();
        if (toAdd.Length > 0)
        {
            await _playlistManager.AddItemToPlaylistAsync(
                playlist.Id,
                toAdd,
                userId);
        }
    }

    private static MediaType GetMediaType(SmartListConfig config)
    {
        if (config.MediaTypes.Any(m => m.Equals("Movie", StringComparison.OrdinalIgnoreCase)))
        {
            return MediaType.Video;
        }

        if (config.MediaTypes.Any(m => m.Equals("Audio", StringComparison.OrdinalIgnoreCase)))
        {
            return MediaType.Audio;
        }

        return MediaType.Video;
    }

    private Guid? GetDefaultUserId()
    {
        // Use the first admin user, or first user
        var users = _userManager.Users;
        var admin = users.FirstOrDefault(u =>
            u.Permissions.Any(p => p.Kind == PermissionKind.IsAdministrator && p.Value));
        var user = admin ?? users.FirstOrDefault();
        return user?.Id;
    }

    private void SaveConfig(string path, SmartListConfig config)
    {
        try
        {
            var json = JsonSerializer.Serialize(config, JsonOptions);
            var tempPath = path + ".tmp";
            File.WriteAllText(tempPath, json);

            if (File.Exists(path))
            {
                File.Replace(tempPath, path, null);
            }
            else
            {
                File.Move(tempPath, path);
            }

            _logger.LogDebug("Saved JellyfinPlaylistId to config: {Path}", path);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to save config: {Path}", path);
        }
    }
}
