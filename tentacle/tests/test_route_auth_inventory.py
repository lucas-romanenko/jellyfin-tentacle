"""Every /api route must require authentication unless it is on the allowlist.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Walks the real router objects (no app start, no scheduler) and checks each
route's dependency tree for get_user_from_request / require_admin. A route
that authenticates in its body instead (webhooks, delete-download) or is
public by design (tuner, login picker, image proxy) must be listed in ALLOW
with a reason, so adding a new unauthenticated route fails this test instead
of shipping silently (#55, #74).
"""
import importlib
import unittest

from fastapi.routing import APIRoute

ROUTER_MODULES = [
    "auth", "settings", "providers", "sync", "library", "duplicates", "lists",
    "widget", "radarr", "sonarr", "tags", "collections", "smartlists",
    "discover", "activity", "livetv", "notifications", "health", "youtube",
]

# (METHOD, path) -> reason. Keep this list short and justified.
ALLOW = {
    ("GET", "/api/auth/users"): "login picker, before anyone is signed in",
    ("POST", "/api/auth/login"): "login",
    ("POST", "/api/auth/logout"): "logout",
    ("GET", "/api/discover/config"): "constant feature flag",
    ("GET", "/api/discover/image-proxy/{cache_key}"): "<img> tags cannot send auth; guarded by cache_key (#37)",
    ("POST", "/api/radarr/webhook"): "authenticates in body (_check_webhook_auth)",
    ("POST", "/api/sonarr/webhook"): "authenticates in body (_check_webhook_auth)",
    ("DELETE", "/api/library/delete-download/{tmdb_id}"): "authenticates in body (get_user_from_request)",
    ("DELETE", "/api/library/item/{media_type}/{tmdb_id}"): "plugin ItemRemoved hook, unauthenticated by design (#49)",
    ("GET", "/api/live/stream/{channel_id}"): "HDHomeRun tuner, Jellyfin cannot send credentials",
    ("GET", "/api/live/playlist.m3u"): "M3U tuner",
    ("GET", "/api/live/xmltv.xml"): "XMLTV guide for Jellyfin",
    ("GET", "/api/youtube/v/{video_id}/master.m3u8"): ".strm playback",
    ("GET", "/api/youtube/v/{video_id}/r/{token}"): ".strm playback, opaque token",
    ("GET", "/api/youtube/live/{channel_id}/stream.ts"): "tuner",
    ("GET", "/api/youtube/live/{channel_id}/master.m3u8"): "tuner",
    ("GET", "/api/youtube/ping"): "liveness",
    ("HEAD", "/api/live/stream/{channel_id}"): "tuner probe",
    ("HEAD", "/api/youtube/v/{video_id}/master.m3u8"): ".strm probe",
    ("HEAD", "/api/youtube/live/{channel_id}/stream.ts"): "tuner probe",
    ("HEAD", "/api/youtube/live/{channel_id}/master.m3u8"): "tuner probe",
    ("GET", "/api/widget/status"): "Homepage dashboard widget (counts only)",
    ("GET", "/api/smartlists/version"): "plugin poll (version number only)",
}

# require_internal_or_admin: a trusted server-side caller with the shared
# internal secret (constant-time compared), or an admin session.
AUTH_DEPS = {"get_user_from_request", "require_admin", "get_current_user", "require_internal_or_admin"}


def _dep_names(dependant):
    out = set()
    for d in dependant.dependencies:
        if d.call is not None:
            out.add(getattr(d.call, "__name__", repr(d.call)))
        out |= _dep_names(d)
    return out


def _routes():
    for name in ROUTER_MODULES:
        mod = importlib.import_module(f"routers.{name}")
        for attr in ("router", "plugin_router", "webhook_router"):
            r = getattr(mod, attr, None)
            if r is None:
                continue
            for route in r.routes:
                if isinstance(route, APIRoute) and route.path.startswith("/api/"):
                    for m in route.methods:
                        yield m, route.path, route


class RouteAuthInventory(unittest.TestCase):
    def test_every_api_route_is_authenticated_or_allowlisted(self):
        missing = []
        for method, path, route in _routes():
            if (method, path) in ALLOW:
                continue
            if not (_dep_names(route.dependant) & AUTH_DEPS):
                missing.append(f"{method} {path}")
        self.assertEqual(sorted(missing), [], "routes answering without authentication")

    def test_allowlist_has_no_stale_entries(self):
        seen = {(m, p) for m, p, _ in _routes()}
        self.assertEqual(sorted(set(ALLOW) - seen), [], "allowlisted routes that no longer exist")


if __name__ == "__main__":
    unittest.main()
