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
        serviceCollection.AddScoped<IEventConsumer<UserCreatedEventArgs>, LiveTvNewUser>();

        // Tentacle VOD served as HLS re-encodes its video instead of copying
        // it (a resumed play otherwise skips to the next episode, #17966)
        serviceCollection.AddSingleton<VodNoVideoCopyFilter>();
        serviceCollection.Configure<MvcOptions>(options => options.Filters.AddService<VodNoVideoCopyFilter>());

        // Names on the login screen only at home (Jellyfin's /Users/Public)
        serviceCollection.AddSingleton<PublicUsersFilter>();
        serviceCollection.Configure<MvcOptions>(options => options.Filters.AddService<PublicUsersFilter>());

        logger?.LogInformation("[Tentacle] Services registered successfully.");
    }
}
