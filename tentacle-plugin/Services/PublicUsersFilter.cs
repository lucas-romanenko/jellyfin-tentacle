using System.Net;
using MediaBrowser.Common.Extensions;
using MediaBrowser.Common.Net;
using MediaBrowser.Controller.Configuration;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Shows the household's names on the login screen only at home.
///
/// Jellyfin's unauthenticated <c>GET /Users/Public</c> lists every account
/// not hidden from login screens to anyone who can reach the server, the
/// internet included: a server behind a public address tells strangers who
/// lives there and which account names to try passwords on. For a caller
/// outside Jellyfin's local network (Jellyfin's own rule, set under
/// Networking, proxies taken into account) the list comes back empty, and
/// every client (Jellyfin Web, the apps, the TV app) shows its "type your
/// name" sign-in instead. At home nothing changes. Tentacle's own sign-in
/// screen follows the same rule (services/sign_in.py).
///
/// A tunnel or reverse proxy (Cloudflare Tunnel, nginx) connects from inside
/// the home, and Jellyfin reads its forwarding headers only from proxies an
/// admin listed under Known proxies, which most never do: through the tunnel
/// every visitor looked local. So any address a forwarding header names must
/// be local too. Headers can only make a caller look further away, never
/// closer: the tunnel or proxy adds the real address after any made up one.
/// </summary>
public sealed class PublicUsersFilter : IActionFilter
{
    private readonly INetworkManager _network;
    private readonly IServerConfigurationManager _config;
    private readonly ILogger<PublicUsersFilter> _logger;

    public PublicUsersFilter(INetworkManager network, IServerConfigurationManager config, ILogger<PublicUsersFilter> logger)
    {
        _network = network;
        _config = config;
        _logger = logger;
    }

    /// <summary>Whether this is Jellyfin's public user list.</summary>
    internal static bool IsPublicUsers(ActionExecutingContext context) =>
        context.ActionDescriptor is ControllerActionDescriptor action
        && action.ControllerName == "User"
        && action.ActionName == "GetPublicUsers";

    /// <inheritdoc />
    public void OnActionExecuting(ActionExecutingContext context)
    {
        if (!IsPublicUsers(context))
        {
            return;
        }

        try
        {
            // Before the startup wizard is done Jellyfin lists everyone on
            // purpose (the wizard's first account); leave that alone.
            if (!_config.Configuration.IsStartupWizardCompleted)
            {
                return;
            }

            if (AtHome(context.HttpContext))
            {
                return;
            }

            context.Result = new OkObjectResult(Array.Empty<object>());
        }
        catch (Exception ex)
        {
            // Unsure where the caller is: names stay private.
            _logger.LogWarning(ex, "[Tentacle] Could not tell where a login screen is; showing no names");
            context.Result = new OkObjectResult(Array.Empty<object>());
        }
    }

    // Headers a proxy or tunnel names the real browser in.
    private static readonly string[] ForwardHeaders =
        { "CF-Connecting-IP", "True-Client-IP", "X-Real-IP", "X-Forwarded-For", "Forwarded" };

    private bool AtHome(Microsoft.AspNetCore.Http.HttpContext http)
    {
        if (!_network.IsInLocalNetwork(http.GetNormalizedRemoteIP()))
        {
            return false;
        }

        foreach (var name in ForwardHeaders)
        {
            if (!http.Request.Headers.TryGetValue(name, out var values))
            {
                continue;
            }

            foreach (var value in values)
            {
                foreach (var addr in Addresses(name, value ?? string.Empty))
                {
                    if (ParseAddress(addr) is not IPAddress ip || !_network.IsInLocalNetwork(ip))
                    {
                        return false;
                    }
                }
            }
        }

        return true;
    }

    /// <summary>The addresses in one header value ("a, b"; Forwarded's for=).</summary>
    internal static IEnumerable<string> Addresses(string header, string value)
    {
        if (!header.Equals("Forwarded", StringComparison.OrdinalIgnoreCase))
        {
            return value.Split(',');
        }

        var found = new List<string>();
        foreach (var element in value.Split(','))
        {
            foreach (var pair in element.Split(';'))
            {
                var kv = pair.Split('=', 2);
                if (kv.Length == 2 && kv[0].Trim().Equals("for", StringComparison.OrdinalIgnoreCase))
                {
                    found.Add(kv[1]);
                }
            }
        }

        return found.Count > 0 ? found : new List<string> { value };
    }

    /// <summary>An address as a header writes it: quoted, [v6]:port or v4:port.</summary>
    internal static IPAddress? ParseAddress(string raw)
    {
        var a = raw.Trim().Trim('"');
        if (a.StartsWith('[') && a.Contains(']', StringComparison.Ordinal))
        {
            a = a[1..a.IndexOf(']', StringComparison.Ordinal)];
        }
        else if (a.Count(c => c == ':') == 1)
        {
            a = a[..a.IndexOf(':', StringComparison.Ordinal)];
        }

        if (!IPAddress.TryParse(a, out var ip))
        {
            return null;
        }

        return ip.IsIPv4MappedToIPv6 ? ip.MapToIPv4() : ip;
    }

    /// <inheritdoc />
    public void OnActionExecuted(ActionExecutedContext context)
    {
    }
}
