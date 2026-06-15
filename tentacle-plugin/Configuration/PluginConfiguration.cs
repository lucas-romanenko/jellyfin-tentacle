using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.Tentacle.Configuration;

/// <summary>
/// Plugin configuration for Tentacle.
/// </summary>
public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>
    /// Gets or sets the Tentacle server URL (e.g. http://localhost:8888).
    /// </summary>
    public string TentacleUrl { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the path to the SmartLists config directory (optional).
    /// Only needed if you want the plugin's scheduled task to refresh playlists independently.
    /// Leave empty to let the Tentacle backend handle playlist management via API.
    /// </summary>
    public string SmartListsPath { get; set; } = string.Empty;

}
