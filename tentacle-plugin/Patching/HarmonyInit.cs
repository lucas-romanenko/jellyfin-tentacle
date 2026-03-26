using System.Reflection;
using System.Runtime.Loader;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Patching;

/// <summary>
/// Loads the embedded 0Harmony.dll and applies Harmony patches.
/// Must be called from PluginServiceRegistrator.RegisterServices before Startup.Configure runs.
/// </summary>
public static class HarmonyInit
{
    private static bool _initialized;
    private static Assembly? _harmonyAssembly;

    public static void Initialize(ILogger? logger = null)
    {
        if (_initialized)
        {
            return;
        }

        _initialized = true;

        var pluginAssembly = typeof(HarmonyInit).Assembly;
        var resourceName = pluginAssembly.GetManifestResourceNames()
            .FirstOrDefault(n => n.EndsWith("0Harmony.dll"));

        if (resourceName == null)
        {
            logger?.LogWarning("[Tentacle] 0Harmony.dll not found as embedded resource. File transformation patches will be disabled.");
            return;
        }

        logger?.LogInformation("[Tentacle] Loading embedded 0Harmony.dll");

        using var stream = pluginAssembly.GetManifestResourceStream(resourceName)!;
        using var ms = new MemoryStream();
        stream.CopyTo(ms);
        ms.Position = 0;

        var alc = new AssemblyLoadContext("Tentacle.Harmony");
        _harmonyAssembly = alc.LoadFromStream(new MemoryStream(ms.ToArray()));

        AppDomain.CurrentDomain.AssemblyResolve += (_, args) =>
        {
            if (_harmonyAssembly != null && args.Name == _harmonyAssembly.FullName)
            {
                return _harmonyAssembly;
            }

            return null;
        };

        logger?.LogInformation("[Tentacle] 0Harmony.dll loaded: {Name}", _harmonyAssembly.FullName);

        IndexHtmlPatch.SetupPatches(logger);
    }
}
