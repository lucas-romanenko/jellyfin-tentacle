using System.Net.Http;
using System.Reflection;
using Jellyfin.Plugin.Tentacle.HomeScreen;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Playlists;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Querying;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// API controller that serves homepage section data, JS, and CSS.
/// Replaces HSS + Collection Sections + Media Bar functionality.
/// </summary>
[ApiController]
[Route("[controller]")]
public class TentacleHomeController : ControllerBase
{
    private readonly HomeScreenManager _homeScreenManager;
    private readonly ILibraryManager _libraryManager;
    private readonly IUserManager _userManager;
    private readonly IDtoService _dtoService;
    private readonly ILogger<TentacleHomeController> _logger;
    private static readonly HttpClient ProxyClient = new() { Timeout = TimeSpan.FromSeconds(15) };

    public TentacleHomeController(
        HomeScreenManager homeScreenManager,
        ILibraryManager libraryManager,
        IUserManager userManager,
        IDtoService dtoService,
        ILogger<TentacleHomeController> logger)
    {
        _homeScreenManager = homeScreenManager;
        _libraryManager = libraryManager;
        _userManager = userManager;
        _dtoService = dtoService;
        _logger = logger;
    }

    /// <summary>
    /// Returns the list of homepage sections (rows) for the current user.
    /// </summary>
    [HttpGet("Sections")]
    [Authorize]
    public ActionResult GetSections([FromQuery] Guid userId)
    {
        var config = _homeScreenManager.GetHomeConfig(userId);
        if (config == null)
        {
            return Ok(new { enabled = false, sections = Array.Empty<object>() });
        }

        var sections = new List<object>();

        // Hero section
        if (config.Hero is { Enabled: true } hero && !string.IsNullOrEmpty(hero.PlaylistId))
        {
            sections.Add(new
            {
                id = "tentacle_hero",
                type = "hero",
                displayText = hero.DisplayName,
                playlistId = hero.PlaylistId,
            });
        }

        // Row sections (both Tentacle playlists and built-in Jellyfin sections)
        if (config.Rows != null)
        {
            foreach (var row in config.Rows.OrderBy(r => r.Order))
            {
                if (row.IsBuiltin)
                {
                    sections.Add(new
                    {
                        id = $"tentacle_builtin_{row.SectionId}",
                        type = "builtin",
                        sectionId = row.SectionId,
                        displayText = row.DisplayName,
                        playlistId = (string?)null,
                    });
                }
                else
                {
                    if (string.IsNullOrEmpty(row.PlaylistId))
                    {
                        continue;
                    }

                    sections.Add(new
                    {
                        id = $"tentacle_row_{row.PlaylistId}",
                        type = "row",
                        sectionId = (string?)null,
                        displayText = row.DisplayName,
                        playlistId = row.PlaylistId,
                    });
                }
            }
        }

        return Ok(new { enabled = true, sections });
    }

    /// <summary>
    /// Returns items for a specific section/playlist.
    /// </summary>
    [HttpGet("Section/{playlistId}")]
    [Authorize]
    public ActionResult GetSectionItems(string playlistId, [FromQuery] Guid userId)
    {
        if (!Guid.TryParse(playlistId, out var playlistGuid))
        {
            return BadRequest("Invalid playlist ID");
        }

        var user = _userManager.GetUserById(userId);
        if (user == null)
        {
            return NotFound("User not found");
        }

        var item = _libraryManager.GetItemById(playlistGuid);
        if (item is not Playlist playlist)
        {
            return NotFound("Playlist not found");
        }

        // Read row config (max_items, sort) from home config
        var limit = 20;
        RowConfig? row = null;
        var config = _homeScreenManager.GetHomeConfig(userId);
        if (config?.Rows != null)
        {
            row = config.Rows.FirstOrDefault(r => r.PlaylistId == playlistId);
            if (row?.MaxItems is > 0)
            {
                limit = row.MaxItems.Value;
            }
        }

        var dtoOptions = new DtoOptions
        {
            Fields = new[]
            {
                ItemFields.PrimaryImageAspectRatio,
                ItemFields.MediaSourceCount,
                ItemFields.Overview,
                ItemFields.Genres,
            },
            ImageTypes = new[]
            {
                ImageType.Primary,
                ImageType.Backdrop,
                ImageType.Thumb,
            },
            ImageTypeLimit = 1,
        };

        // Group episodes by series
        var grouped = playlist.GetManageableItems()
            .Where(i => i.Item2.IsVisible(user))
            .GroupBy(x => x.Item2 is Episode ep ? (BaseItem)ep.Series : x.Item2)
            .Select(g => g.Key)
            .Where(i => i != null)
            .ToList();

        // Apply sort from home config (same pattern as hero sort)
        // For "datecreated", trust the playlist order set by the Python backend
        // (which uses Tentacle's date_added — more reliable than Jellyfin's DateCreated
        // which can shift after metadata refreshes or library rescans).
        var sortBy = row?.SortBy?.ToLowerInvariant() ?? "releasedate";
        var descending = !string.Equals(row?.SortOrder, "Ascending", StringComparison.OrdinalIgnoreCase);
        IEnumerable<BaseItem> sorted = sortBy switch
        {
            "communityrating" => descending
                ? grouped.OrderByDescending(i => i.CommunityRating ?? 0)
                : grouped.OrderBy(i => i.CommunityRating ?? 0),
            "releasedate" => descending
                ? grouped.OrderByDescending(i => i.PremiereDate ?? DateTime.MinValue)
                : grouped.OrderBy(i => i.PremiereDate ?? DateTime.MinValue),
            "name" => descending
                ? grouped.OrderByDescending(i => i.SortName)
                : grouped.OrderBy(i => i.SortName),
            "datecreated" => grouped,
            _ => grouped.OrderBy(_ => Random.Shared.Next()),
        };

        var finalItems = sorted.Take(limit).ToList();
        var dtos = _dtoService.GetBaseItemDtos(finalItems, dtoOptions, user);

        return Ok(new QueryResult<BaseItemDto>(dtos));
    }

    /// <summary>
    /// Returns a version counter that increments whenever playlists are modified.
    /// Polled by the home page JS to detect changes and live-update rows.
    /// </summary>
    [HttpGet("Version")]
    [Authorize]
    public async Task<ActionResult> GetPlaylistVersion()
    {
        var plugin = Plugin.Instance;
        if (plugin == null || string.IsNullOrEmpty(plugin.Configuration.TentacleUrl))
        {
            return Ok(new { version = 0 });
        }

        try
        {
            var response = await ProxyClient.GetStringAsync(
                $"{plugin.Configuration.TentacleUrl.TrimEnd('/')}/api/smartlists/version");
            return Content(response, "application/json");
        }
        catch
        {
            return Ok(new { version = 0 });
        }
    }

    /// <summary>
    /// Returns hero/spotlight items with full image data.
    /// </summary>
    [HttpGet("Hero")]
    [Authorize]
    public ActionResult GetHeroItems([FromQuery] Guid userId)
    {
        var config = _homeScreenManager.GetHomeConfig(userId);
        if (config?.Hero is not { Enabled: true } hero || string.IsNullOrEmpty(hero.PlaylistId))
        {
            return Ok(new QueryResult<BaseItemDto>());
        }

        if (!Guid.TryParse(hero.PlaylistId, out var playlistGuid))
        {
            return Ok(new QueryResult<BaseItemDto>());
        }

        var user = _userManager.GetUserById(userId);
        if (user == null)
        {
            return Ok(new QueryResult<BaseItemDto>());
        }

        var item = _libraryManager.GetItemById(playlistGuid);
        if (item is not Playlist playlist)
        {
            return Ok(new QueryResult<BaseItemDto>());
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

        var rawItems = playlist.GetManageableItems()
            .Where(i => i.Item2.IsVisible(user))
            .ToArray();

        // Group episodes by series, then optionally filter for polished hero look
        var grouped = rawItems
            .GroupBy(x => x.Item2 is Episode ep ? (BaseItem)ep.Series : x.Item2)
            .Select(g => g.Key)
            .Where(i => i != null);

        // When require_logo is true, only show items with both backdrop AND logo (title image)
        // When false, only require a backdrop image
        var filtered = hero.RequireLogo
            ? grouped.Where(i => i.GetImages(ImageType.Backdrop).Any() && i.GetImages(ImageType.Logo).Any())
            : grouped.Where(i => i.GetImages(ImageType.Backdrop).Any());

        // When require_trailer is true, only show items that have at least one trailer URL
        if (hero.RequireTrailer)
        {
            filtered = filtered.Where(i => i.RemoteTrailers != null && i.RemoteTrailers.Count > 0);
        }

        // Apply hero-specific sort from config
        // For "datecreated", trust the playlist order from the Python backend
        // (uses Tentacle's date_added, more reliable than Jellyfin's DateCreated).
        var heroSort = hero.SortBy?.ToLowerInvariant() ?? "random";
        var descending = !string.Equals(hero.SortOrder, "Ascending", StringComparison.OrdinalIgnoreCase);
        IEnumerable<BaseItem> sorted = heroSort switch
        {
            "communityrating" => descending
                ? filtered.OrderByDescending(i => i.CommunityRating ?? 0)
                : filtered.OrderBy(i => i.CommunityRating ?? 0),
            "releasedate" => descending
                ? filtered.OrderByDescending(i => i.PremiereDate ?? DateTime.MinValue)
                : filtered.OrderBy(i => i.PremiereDate ?? DateTime.MinValue),
            "name" => descending
                ? filtered.OrderByDescending(i => i.SortName)
                : filtered.OrderBy(i => i.SortName),
            "datecreated" => filtered,
            _ => filtered.OrderBy(_ => Random.Shared.Next()), // random
        };

        var count = hero.ItemCount > 0 ? hero.ItemCount : 10;
        var heroItems = sorted.Take(count).ToList();

        var dtos = _dtoService.GetBaseItemDtos(heroItems, dtoOptions, user);

        return Ok(new QueryResult<BaseItemDto>(dtos));
    }

    /// <summary>
    /// Serves the Tentacle homepage JavaScript (injected into index.html).
    /// </summary>
    [HttpGet("/Tentacle/home.js")]
    public ActionResult GetHomeJs() => ServeAsset("tentacle-home.js", "application/javascript");

    /// <summary>
    /// Serves the Tentacle logo image (embedded resource).
    /// </summary>
    [HttpGet("/Tentacle/logo.png")]
    public ActionResult GetLogo()
    {
        var assembly = typeof(TentacleHomeController).Assembly;
        var name = assembly.GetManifestResourceNames()
            .FirstOrDefault(n => n.EndsWith("tentacle-logo.png"));

        if (name == null)
        {
            return NotFound();
        }

        var stream = assembly.GetManifestResourceStream(name);
        if (stream == null)
        {
            return NotFound();
        }

        return File(stream, "image/png");
    }

    /// <summary>
    /// Serves the Tentacle homepage CSS (injected into index.html).
    /// </summary>
    [HttpGet("/Tentacle/home.css")]
    public ActionResult GetHomeCss() => ServeAsset("tentacle-home.css", "text/css");

    /// <summary>
    /// Returns user-specific section visibility settings.
    /// </summary>
    [HttpGet("UserSettings")]
    [Authorize]
    public ActionResult GetUserSettings([FromQuery] Guid userId)
    {
        var settings = LoadUserSettings(userId);
        return Ok(settings);
    }

    /// <summary>
    /// Saves user-specific section visibility settings.
    /// </summary>
    [HttpPost("UserSettings")]
    [Authorize]
    public ActionResult SaveUserSettings([FromBody] UserSectionSettings settings)
    {
        SaveUserSettingsToDisk(settings);
        return Ok(new { status = "ok" });
    }

    /// <summary>
    /// Returns all available playlists from Tentacle (for hero picker, etc.).
    /// </summary>
    [HttpGet("Playlists")]
    [Authorize]
    public async Task<ActionResult> GetPlaylists()
    {
        var config = Plugin.Instance?.Configuration;
        var baseUrl = config?.TentacleUrl?.TrimEnd('/') ?? "";
        if (string.IsNullOrEmpty(baseUrl))
        {
            return BadRequest("Tentacle URL not configured");
        }

        try
        {
            var httpClient = ProxyClient;
            var response = await httpClient.GetAsync($"{baseUrl}/api/smartlists/all-playlists");
            var result = await response.Content.ReadAsStringAsync();
            return Content(result, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Home] Failed to fetch playlists: {Error}", ex.Message);
            return StatusCode(500, new { success = false, message = ex.Message });
        }
    }

    /// <summary>
    /// Returns the current hero playlist ID and display name.
    /// </summary>
    [HttpGet("HeroConfig")]
    [Authorize]
    public ActionResult GetHeroConfig([FromQuery] Guid userId)
    {
        var homeConfig = _homeScreenManager.GetHomeConfig(userId);
        if (homeConfig?.Hero is { Enabled: true } hero && !string.IsNullOrEmpty(hero.PlaylistId))
        {
            return Ok(new { enabled = true, playlistId = hero.PlaylistId, displayName = hero.DisplayName, trailerAudio = hero.TrailerAudio, itemCount = hero.ItemCount });
        }

        // Return whatever the backend has — defaults are set by the Tentacle dashboard, not the plugin
        var fallbackHero = homeConfig?.Hero;
        return Ok(new { enabled = false, playlistId = "", displayName = "", trailerAudio = fallbackHero?.TrailerAudio ?? false, itemCount = fallbackHero?.ItemCount ?? 10 });
    }

    /// <summary>
    /// Returns toolbar button configuration (visibility and order).
    /// </summary>
    [HttpGet("Toolbar")]
    [Authorize]
    public ActionResult GetToolbar([FromQuery] Guid userId)
    {
        var homeConfig = _homeScreenManager.GetHomeConfig(userId);
        var toolbar = homeConfig?.Toolbar;
        if (toolbar != null && toolbar.Count > 0)
        {
            return Ok(new { buttons = toolbar });
        }

        // No local defaults — Tentacle backend always provides toolbar config via write_home_config()
        return Ok(new { buttons = Array.Empty<object>() });
    }

    /// <summary>
    /// Proxies a hero set request to the Tentacle backend.
    /// Accepts {"playlist_id": "some-guid"}
    /// </summary>
    [HttpPost("Hero")]
    [Authorize]
    public async Task<ActionResult> SetHero([FromBody] System.Text.Json.JsonElement body)
    {
        var config = Plugin.Instance?.Configuration;
        var baseUrl = config?.TentacleUrl?.TrimEnd('/') ?? "";
        if (string.IsNullOrEmpty(baseUrl))
        {
            return BadRequest("Tentacle URL not configured");
        }

        try
        {
            var httpClient = ProxyClient;
            var content = new StringContent(body.GetRawText(), System.Text.Encoding.UTF8, "application/json");
            var response = await httpClient.PostAsync($"{baseUrl}/api/smartlists/hero", content);
            var result = await response.Content.ReadAsStringAsync();

            // Clear the home screen cache so the new hero takes effect
            _homeScreenManager.ClearCache();

            return Content(result, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Home] Failed to set hero: {Error}", ex.Message);
            return StatusCode(500, new { success = false, message = ex.Message });
        }
    }

    /// <summary>
    /// Proxies a reorder request to the Tentacle backend.
    /// Accepts {"order": ["playlist-id-1", "playlist-id-2", ...]}
    /// </summary>
    [HttpPost("Reorder")]
    [Authorize]
    public async Task<ActionResult> ReorderSections([FromBody] System.Text.Json.JsonElement body)
    {
        var config = Plugin.Instance?.Configuration;
        var baseUrl = config?.TentacleUrl?.TrimEnd('/') ?? "";
        if (string.IsNullOrEmpty(baseUrl))
        {
            return BadRequest("Tentacle URL not configured");
        }

        try
        {
            var httpClient = ProxyClient;
            var content = new StringContent(body.GetRawText(), System.Text.Encoding.UTF8, "application/json");
            var response = await httpClient.PostAsync($"{baseUrl}/api/smartlists/reorder", content);
            var result = await response.Content.ReadAsStringAsync();

            // Clear the home screen cache so the new order takes effect
            _homeScreenManager.ClearCache();

            return Content(result, "application/json");
        }
        catch (Exception ex)
        {
            _logger.LogWarning("[Tentacle Home] Failed to reorder: {Error}", ex.Message);
            return StatusCode(500, new { success = false, message = ex.Message });
        }
    }

    [HttpGet("/Tentacle/details.js")]
    public ActionResult GetDetailsJs() => ServeAsset("tentacle-details.js", "application/javascript");

    [HttpGet("/Tentacle/details.css")]
    public ActionResult GetDetailsCss() => ServeAsset("tentacle-details.css", "text/css");

    [HttpGet("/Tentacle/navbar.js")]
    public ActionResult GetNavbarJs() => ServeAsset("tentacle-navbar.js", "application/javascript");

    [HttpGet("/Tentacle/navbar.css")]
    public ActionResult GetNavbarCss() => ServeAsset("tentacle-navbar.css", "text/css");

    [HttpGet("/Tentacle/mediabar.js")]
    public ActionResult GetMediaBarJs() => ServeAsset("tentacle-mediabar.js", "application/javascript");

    [HttpGet("/Tentacle/mediabar.css")]
    public ActionResult GetMediaBarCss() => ServeAsset("tentacle-mediabar.css", "text/css");

    [HttpGet("/Tentacle/mdblist.js")]
    public ActionResult GetMdbListJs() => ServeAsset("tentacle-mdblist.js", "application/javascript");

    [HttpGet("/Tentacle/tmdb.js")]
    public ActionResult GetTmdbJs() => ServeAsset("tentacle-tmdb.js", "application/javascript");

    [HttpGet("/Tentacle/mdblist.css")]
    public ActionResult GetMdbListCss() => ServeAsset("tentacle-mdblist.css", "text/css");

    [HttpGet("/Tentacle/notifications.js")]
    public ActionResult GetNotificationsJs() => ServeAsset("tentacle-notifications.js", "application/javascript");

    [HttpGet("/Tentacle/notifications.css")]
    public ActionResult GetNotificationsCss() => ServeAsset("tentacle-notifications.css", "text/css");

    /// <summary>
    /// Returns an embedded CSS/JS resource with no-cache headers so the browser
    /// always revalidates after a plugin update (cache-buster query params alone
    /// are not sufficient when the page is refreshed without a server restart).
    /// </summary>
    private ActionResult ServeAsset(string resourceSuffix, string contentType)
    {
        var content = LoadEmbeddedResource(resourceSuffix);
        if (content == null)
        {
            return NotFound();
        }

        Response.Headers["Cache-Control"] = "no-cache, no-store, must-revalidate";
        return Content(content, contentType);
    }

    private static string? LoadEmbeddedResource(string resourceSuffix)
    {
        var assembly = typeof(TentacleHomeController).Assembly;
        var name = assembly.GetManifestResourceNames()
            .FirstOrDefault(n => n.EndsWith(resourceSuffix));

        if (name == null)
        {
            return null;
        }

        using var stream = assembly.GetManifestResourceStream(name);
        if (stream == null)
        {
            return null;
        }

        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    }

    private UserSectionSettings LoadUserSettings(Guid userId)
    {
        var settingsPath = GetUserSettingsPath();
        if (!System.IO.File.Exists(settingsPath))
        {
            return new UserSectionSettings { UserId = userId };
        }

        try
        {
            var json = System.IO.File.ReadAllText(settingsPath);
            var allSettings = System.Text.Json.JsonSerializer.Deserialize<List<UserSectionSettings>>(json)
                              ?? new List<UserSectionSettings>();

            return allSettings.FirstOrDefault(s => s.UserId == userId)
                   ?? new UserSectionSettings { UserId = userId };
        }
        catch
        {
            return new UserSectionSettings { UserId = userId };
        }
    }

    private void SaveUserSettingsToDisk(UserSectionSettings settings)
    {
        var settingsPath = GetUserSettingsPath();
        var allSettings = new List<UserSectionSettings>();

        if (System.IO.File.Exists(settingsPath))
        {
            try
            {
                var json = System.IO.File.ReadAllText(settingsPath);
                allSettings = System.Text.Json.JsonSerializer.Deserialize<List<UserSectionSettings>>(json)
                              ?? new List<UserSectionSettings>();
            }
            catch
            {
                allSettings = new List<UserSectionSettings>();
            }
        }

        allSettings.RemoveAll(s => s.UserId == settings.UserId);
        allSettings.Add(settings);

        var dir = Path.GetDirectoryName(settingsPath);
        if (!string.IsNullOrEmpty(dir))
        {
            Directory.CreateDirectory(dir);
        }

        var output = System.Text.Json.JsonSerializer.Serialize(allSettings, new System.Text.Json.JsonSerializerOptions { WriteIndented = true });
        System.IO.File.WriteAllText(settingsPath, output);
    }

    private static string GetUserSettingsPath()
    {
        var plugin = Plugin.Instance;
        if (plugin == null)
        {
            return "/config/plugins/configurations/Jellyfin.Plugin.Tentacle/UserSettings.json";
        }

        var configDir = Path.GetDirectoryName(plugin.ConfigurationFilePath) ?? "/config";
        return Path.Combine(configDir, "UserSettings.json");
    }
}

/// <summary>
/// Per-user section visibility settings.
/// </summary>
public class UserSectionSettings
{
    public Guid UserId { get; set; }

    public List<string> EnabledSections { get; set; } = new();

    public List<string> DisabledSections { get; set; } = new();
}
