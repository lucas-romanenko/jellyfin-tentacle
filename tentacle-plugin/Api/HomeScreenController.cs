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

        // Read max_items from home config for this row (default 20)
        var limit = 20;
        var config = _homeScreenManager.GetHomeConfig(userId);
        if (config?.Rows != null)
        {
            var row = config.Rows.FirstOrDefault(r => r.PlaylistId == playlistId);
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

        // Group episodes by series, taking only what we need
        var grouped = playlist.GetManageableItems()
            .Where(i => i.Item2.IsVisible(user))
            .GroupBy(x => x.Item2 is Episode ep ? (BaseItem)ep.Series : x.Item2)
            .Take(limit)
            .Select(g => g.Key)
            .Where(i => i != null)
            .ToList();

        var dtos = _dtoService.GetBaseItemDtos(grouped, dtoOptions, user);

        return Ok(new QueryResult<BaseItemDto>(dtos));
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
            "datecreated" => descending
                ? filtered.OrderByDescending(i => i.DateCreated)
                : filtered.OrderBy(i => i.DateCreated),
            _ => filtered.OrderBy(_ => Random.Shared.Next()), // random
        };

        var heroItems = sorted.Take(10).ToList();

        var dtos = _dtoService.GetBaseItemDtos(heroItems, dtoOptions, user);

        return Ok(new QueryResult<BaseItemDto>(dtos));
    }

    /// <summary>
    /// Serves the Tentacle homepage JavaScript (injected into index.html).
    /// </summary>
    [HttpGet("/Tentacle/home.js")]
    public ActionResult GetHomeJs()
    {
        var content = LoadEmbeddedResource("tentacle-home.js");
        if (content == null)
        {
            return NotFound();
        }

        return Content(content, "application/javascript");
    }

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
    public ActionResult GetHomeCss()
    {
        var content = LoadEmbeddedResource("tentacle-home.css");
        if (content == null)
        {
            return NotFound();
        }

        return Content(content, "text/css");
    }

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
            using var httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
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
            return Ok(new { enabled = true, playlistId = hero.PlaylistId, displayName = hero.DisplayName });
        }

        return Ok(new { enabled = false, playlistId = "", displayName = "" });
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
            using var httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
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
            using var httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
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

    /// <summary>
    /// Serves the Tentacle details overlay JavaScript.
    /// </summary>
    [HttpGet("/Tentacle/details.js")]
    public ActionResult GetDetailsJs()
    {
        var content = LoadEmbeddedResource("tentacle-details.js");
        if (content == null)
        {
            return NotFound();
        }

        return Content(content, "application/javascript");
    }

    /// <summary>
    /// Serves the Tentacle details overlay CSS.
    /// </summary>
    [HttpGet("/Tentacle/details.css")]
    public ActionResult GetDetailsCss()
    {
        var content = LoadEmbeddedResource("tentacle-details.css");
        if (content == null)
        {
            return NotFound();
        }

        return Content(content, "text/css");
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
