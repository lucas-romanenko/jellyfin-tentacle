using System;
using System.Collections.Generic;
using Jellyfin.Plugin.Tentacle.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle;

/// <summary>
/// Tentacle plugin for Jellyfin — self-contained homepage replacement.
/// Absorbs SmartLists, Home Screen Sections, Collection Sections,
/// Media Bar, Plugin Pages, and File Transformation into a single plugin.
///
/// Homepage rendering is done via injected JS/CSS (Harmony patch on index.html).
/// Playlist management is handled by PlaylistManager.
/// Section data is served by HomeScreenController API endpoints.
/// No external plugin dependencies.
/// </summary>
public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    private readonly ILogger<Plugin> _logger;

    public Plugin(
        IApplicationPaths applicationPaths,
        IXmlSerializer xmlSerializer,
        ILogger<Plugin> logger)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;
        _logger = logger;

        _logger.LogInformation("Tentacle plugin v{Version} initialized (self-contained mode)", Version);
    }

    /// <summary>
    /// Gets the singleton instance.
    /// </summary>
    public static Plugin? Instance { get; private set; }

    /// <inheritdoc />
    public override string Name => "Tentacle";

    /// <inheritdoc />
    public override Guid Id => Guid.Parse("b7e3f1a9-2c4d-4856-9a1b-8def23456789");

    /// <inheritdoc />
    public IEnumerable<PluginPageInfo> GetPages()
    {
        var ns = GetType().Namespace;
        yield return new PluginPageInfo
        {
            Name = "Tentacle",
            EmbeddedResourcePath = $"{ns}.Configuration.configPage.html",
        };
    }
}
