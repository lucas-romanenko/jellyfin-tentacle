using System;
using System.Linq;
using System.Threading.Tasks;
using MediaBrowser.Controller.Net;
using MediaBrowser.Controller.Playlists;
using Microsoft.AspNetCore.Http;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// Resolves the authenticated caller behind a request and answers the two
/// questions every user-scoped Tentacle endpoint has to ask:
///   * may this caller act as the <c>userId</c> it sent us, and
///   * may that user read this playlist?
///
/// <see cref="Microsoft.AspNetCore.Authorization.AuthorizeAttribute"/> only proves
/// *someone* is signed in. Without these checks a signed-in user can pass another
/// user's id (or any playlist GUID) and read/write data that is not theirs.
/// </summary>
internal static class CallerIdentity
{
    /// <summary>
    /// Resolves the user a request is allowed to act as.
    /// Returns <see langword="false"/> when the caller asked to act as somebody else.
    /// A server API key (no user behind it) may act as any user; a user token may not.
    /// </summary>
    public static async Task<(bool Allowed, Guid UserId)> ResolveAsync(
        IAuthorizationContext authContext,
        HttpContext httpContext,
        Guid requestedUserId)
    {
        var info = await authContext.GetAuthorizationInfo(httpContext).ConfigureAwait(false);

        // A server-level API key has no user behind it; it may address any user.
        if (info.IsApiKey || info.UserId.Equals(default))
        {
            return (true, requestedUserId);
        }

        if (requestedUserId.Equals(default) || requestedUserId.Equals(info.UserId))
        {
            return (true, info.UserId);
        }

        return (false, info.UserId);
    }

    /// <summary>
    /// Mirrors Jellyfin's own playlist access rule
    /// (<c>PlaylistsController.GetPlaylistItems</c>): a playlist may be read by its
    /// owner, by anyone it is shared with, or when it is open-access. As in Jellyfin,
    /// being an administrator is not on its own a reason to read someone's playlist.
    /// </summary>
    public static bool CanReadPlaylist(Playlist playlist, Jellyfin.Database.Implementations.Entities.User user)
    {
        if (playlist.OpenAccess || playlist.OwnerUserId.Equals(user.Id))
        {
            return true;
        }

        return playlist.Shares.Any(s => s.UserId.Equals(user.Id));
    }
}
