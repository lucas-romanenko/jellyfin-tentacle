using System.Reflection;
using Jellyfin.Plugin.Tentacle.HomeScreen;
using Jellyfin.Plugin.Tentacle.Patching;
using Jellyfin.Plugin.Tentacle.Playlists;
using Jellyfin.Plugin.Tentacle.Services;
using Jellyfin.Data.Events.Users;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Events;
using MediaBrowser.Controller.Plugins;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle;

/// <summary>
/// Registers plugin services with the Jellyfin DI container
/// and initializes Harmony patches for index.html injection.
/// </summary>
public class PluginServiceRegistrator : IPluginServiceRegistrator
{
    /// <inheritdoc />
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        // Initialize Harmony (loads 0Harmony.dll, patches PhysicalFileProvider)
        ILoggerFactory? loggerFactory = (ILoggerFactory?)applicationHost.GetType()
            .GetProperty("LoggerFactory", BindingFlags.Instance | BindingFlags.NonPublic)?
            .GetValue(applicationHost);

        var logger = loggerFactory?.CreateLogger(typeof(PluginServiceRegistrator).FullName
                                                 ?? nameof(PluginServiceRegistrator));

        logger?.LogInformation("[Tentacle] RegisterServices called. Initializing Harmony patches...");
        HarmonyInit.Initialize(logger);

        // Register services
        serviceCollection.AddSingleton<HomeScreenManager>();
        serviceCollection.AddSingleton<PlaylistManager>();
        serviceCollection.AddSingleton<IHostedService, LibraryDeleteHandler>();
        // Live TV per person: tags on channels and programmes, new users' blocked tags
        serviceCollection.AddSingleton<IHostedService, LiveTvIsolation>();
        serviceCollection.AddSingleton<IHostedService, TimerCancelForwarder>();
        serviceCollection.AddScoped<IEventConsumer<UserCreatedEventArgs>, LiveTvNewUser>();
        // A deleted user's playlist folders Jellyfin leaves behind (a scan would bring them back for everyone)
        serviceCollection.AddScoped<IEventConsumer<UserDeletedEventArgs>, DeletedUserPlaylists>();

        // Tentacle VOD served as HLS re-encodes its video instead of copying
        // it (a resumed play otherwise skips to the next episode, #17966)
        serviceCollection.AddSingleton<VodNoVideoCopyFilter>();
        serviceCollection.Configure<MvcOptions>(options => options.Filters.AddService<VodNoVideoCopyFilter>());
        // Tentacle VOD subtitles are cached only when extracted from the whole
        // file (Tentacle closes a pull nobody watches; Jellyfin would keep the part)
        serviceCollection.AddSingleton<IHostedService, VodSubtitleExtractionPatch>();
        serviceCollection.AddSingleton<IHostedService, VodProbeOncePatch>();

        // One Live TV stream per device: opening a channel closes the one it
        // had open (one-connection IPTV accounts, Jellyfin Web's retries)
        serviceCollection.AddSingleton<OneLiveStreamPerDeviceFilter>();
        serviceCollection.Configure<MvcOptions>(options => options.Filters.AddService<OneLiveStreamPerDeviceFilter>());

        // A browser gets a live channel's AAC Main audio re-encoded from the
        // start (it cannot decode it; Jellyfin Web's retries flash for ~10 s)
        serviceCollection.AddSingleton<LiveAacMainAudioFilter>();
        serviceCollection.Configure<MvcOptions>(options => options.Filters.AddService<LiveAacMainAudioFilter>());

        // Names on the login screen only at home (Jellyfin's /Users/Public)
        serviceCollection.AddSingleton<PublicUsersFilter>();
        serviceCollection.Configure<MvcOptions>(options => options.Filters.AddService<PublicUsersFilter>());

        // "More Like This" from TMDB and the library's own metadata, not
        // Jellyfin 10.11's random sample (#292): every details page and the TV app
        serviceCollection.AddSingleton<SimilarItemsFilter>();
        serviceCollection.Configure<MvcOptions>(options => options.Filters.AddService<SimilarItemsFilter>());

        logger?.LogInformation("[Tentacle] Services registered successfully.");
    }
}
