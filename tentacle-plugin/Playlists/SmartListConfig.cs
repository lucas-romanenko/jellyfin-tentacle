using System.Text.Json.Serialization;

namespace Jellyfin.Plugin.Tentacle.Playlists;

/// <summary>
/// Represents a SmartList config.json written by the Tentacle server.
/// Simplified model covering the fields Tentacle actually uses.
/// </summary>
public class SmartListConfig
{
    [JsonPropertyName("Type")]
    public string Type { get; set; } = "Playlist";

    [JsonPropertyName("Id")]
    public string? Id { get; set; }

    [JsonPropertyName("Name")]
    public string Name { get; set; } = string.Empty;

    [JsonPropertyName("Enabled")]
    public bool Enabled { get; set; } = true;

    [JsonPropertyName("MediaTypes")]
    public List<string> MediaTypes { get; set; } = new();

    [JsonPropertyName("ExpressionSets")]
    public List<SmartListExpressionSet> ExpressionSets { get; set; } = new();

    [JsonPropertyName("MaxItems")]
    public int? MaxItems { get; set; }

    [JsonPropertyName("JellyfinPlaylistId")]
    public string? JellyfinPlaylistId { get; set; }

    [JsonPropertyName("UserPlaylists")]
    public List<UserPlaylistMapping>? UserPlaylists { get; set; }

    [JsonPropertyName("Order")]
    public SmartListOrder? Order { get; set; }
}

public class SmartListExpressionSet
{
    [JsonPropertyName("Expressions")]
    public List<SmartListExpression> Expressions { get; set; } = new();
}

public class SmartListExpression
{
    [JsonPropertyName("MemberName")]
    public string MemberName { get; set; } = string.Empty;

    [JsonPropertyName("Operator")]
    public string Operator { get; set; } = string.Empty;

    [JsonPropertyName("TargetValue")]
    public string TargetValue { get; set; } = string.Empty;
}

public class UserPlaylistMapping
{
    [JsonPropertyName("UserId")]
    public string UserId { get; set; } = string.Empty;

    [JsonPropertyName("JellyfinPlaylistId")]
    public string? JellyfinPlaylistId { get; set; }
}

public class SmartListOrder
{
    [JsonPropertyName("SortOptions")]
    public List<SmartListSortOption>? SortOptions { get; set; }
}

public class SmartListSortOption
{
    [JsonPropertyName("SortBy")]
    public string SortBy { get; set; } = "Name";

    [JsonPropertyName("SortOrder")]
    public string SortOrder { get; set; } = "Ascending";
}
