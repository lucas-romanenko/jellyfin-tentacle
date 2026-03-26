using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.Tentacle.Configuration;

/// <summary>
/// Plugin configuration for Tentacle.
/// </summary>
public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>
    /// Gets or sets the Tentacle server URL (e.g. http://192.168.2.75:8888).
    /// </summary>
    public string TentacleUrl { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the path to tentacle-home.json inside the Jellyfin container.
    /// </summary>
    public string HomeConfigPath { get; set; } = "/data/tentacle-home.json";

    /// <summary>
    /// Gets or sets the path to the SmartLists config directory.
    /// This is where SmartList config.json files are stored by the Tentacle server.
    /// </summary>
    public string SmartListsPath { get; set; } = "/data/smartlists";

    /// <summary>
    /// Gets or sets a value indicating whether the plugin is enabled.
    /// </summary>
    public bool Enabled { get; set; }
}
