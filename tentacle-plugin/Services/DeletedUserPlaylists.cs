using System;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Xml.Linq;
using Jellyfin.Data.Events.Users;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Controller.Events;
using MediaBrowser.Controller.Library;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Finishes Jellyfin's own user delete for playlists. DELETE /Users/{id}
/// (10.11 UserController.DeleteUser → PlaylistManager.RemovePlaylistsAsync)
/// takes the user's private, unshared playlists out of the library with
/// DeleteFileLocation = false: their folders stay in data/playlists, and the
/// next scan resolves them again as ownerless, empty, open-access playlists
/// that everyone sees ("Drama", "Ann's Downloads1"). Once the user is gone,
/// a folder whose playlist.xml still names them and that is no library item
/// any more is removed. A shared playlist (handed to the first share) or an
/// open-access one Jellyfin kept is still an item, so it stays.
/// </summary>
public class DeletedUserPlaylists : IEventConsumer<UserDeletedEventArgs>
{
    private readonly IApplicationPaths _paths;
    private readonly ILibraryManager _libraryManager;
    private readonly ILogger<DeletedUserPlaylists> _logger;

    public DeletedUserPlaylists(IApplicationPaths paths, ILibraryManager libraryManager, ILogger<DeletedUserPlaylists> logger)
    {
        _paths = paths;
        _libraryManager = libraryManager;
        _logger = logger;
    }

    public Task OnEvent(UserDeletedEventArgs eventArgs)
    {
        var userId = eventArgs.Argument.Id;
        var name = eventArgs.Argument.Username;
        var removed = 0;
        try
        {
            foreach (var folder in LeftOver(Path.Combine(_paths.DataPath, "playlists"), userId,
                         f => _libraryManager.FindByPath(f, true) is not null))
            {
                try
                {
                    Directory.Delete(folder, true);
                    removed++;
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex, "[Tentacle] Could not remove playlist folder {Folder} of deleted user {Name}", folder, name);
                }
            }

            if (removed > 0)
            {
                _logger.LogInformation("[Tentacle] Deleted user {Name}: removed {Count} playlist folder(s) Jellyfin left behind", name, removed);
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "[Tentacle] Could not check the playlist folders of deleted user {Name}", name);
        }

        return Task.CompletedTask;
    }

    /// <summary>The folders under <paramref name="playlistsDir"/> whose playlist.xml is owned by
    /// <paramref name="userId"/> and that aren't library items (<paramref name="isItem"/>).</summary>
    public static string[] LeftOver(string playlistsDir, Guid userId, Func<string, bool> isItem)
    {
        if (userId.Equals(Guid.Empty) || !Directory.Exists(playlistsDir))
        {
            return Array.Empty<string>();
        }

        return Directory.EnumerateDirectories(playlistsDir)
            .Where(f => Owner(Path.Combine(f, "playlist.xml")) == userId && !isItem(f))
            .ToArray();
    }

    /// <summary>The OwnerUserId in a playlist.xml, or Guid.Empty when there is none or it can't be read.</summary>
    public static Guid Owner(string playlistXml)
    {
        try
        {
            if (!File.Exists(playlistXml))
            {
                return Guid.Empty;
            }

            var owner = XDocument.Load(playlistXml).Root?.Element("OwnerUserId")?.Value;
            return Guid.TryParse(owner, out var id) ? id : Guid.Empty;
        }
        catch (Exception)
        {
            return Guid.Empty;
        }
    }
}
