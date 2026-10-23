"""An M3U provider (a playlist link or a playlist file) read once per sync or
test, and what Tentacle learns from it so the provider works like an Xtream
one with nothing extra for the admin to set:

- which entries are live channels, films or episodes (m3u_parser.classify),
  so Live TV gets the channels and the VOD import the films and episodes;
- the Xtream panel login its links use, when they are a panel's (most
  provider playlists are get.php): kept in the provider's server_url,
  username and password, as an Xtream provider's are. With it a channel is
  opened as one continuous .ts with .m3u8 as the fallback, films play
  through Tentacle's VOD relay (resumed after a drop, counted in the
  stream limit), the account's expiry and connection limit are read, and
  two provider rows on one account count as one (livetv._account_key);
- the guide (XMLTV) address the playlist names in its header (url-tvg /
  x-tvg-url), used when the admin set none.
"""
import logging

from models.database import get_setting, set_setting
from services import m3u_parser

logger = logging.getLogger(__name__)

# The playlist's own guide address(es), comma-separated; deleted with the
# provider (routers.livetv.PROVIDER_SETTING_KEYS).
GUIDE_URL_KEY = "livetv_m3u_guide_url_{}"

_UA_DEFAULT = "TiviMate/4.7.0 (Linux; Android 12)"


def is_m3u(provider) -> bool:
    return (getattr(provider, "provider_type", None) or "xtream") in ("m3u_url", "m3u_file")


class Playlist:
    """One read of a provider's playlist."""

    def __init__(self, entries: list, guide_urls: list):
        self.entries = entries
        self.guide_urls = guide_urls
        self.kinds = [m3u_parser.classify(e) for e in entries]
        self.login = m3u_parser.panel_login(entries)

    def of_kind(self, kind: str) -> list:
        return [e for e, k in zip(self.entries, self.kinds) if k == kind]

    def counts(self) -> dict:
        out = {"live": 0, "movie": 0, "series": 0}
        for k in self.kinds:
            out[k] += 1
        return out


def read(provider) -> Playlist:
    """Download or read the playlist. Raises on failure, with a message an
    admin can act on and no login in it."""
    text = m3u_parser.read_m3u_text(provider.m3u_url or "", provider.provider_type == "m3u_file",
                                    provider.user_agent or _UA_DEFAULT)
    return Playlist(m3u_parser.parse_m3u(text), m3u_parser.guide_urls(text))


def learn(db, provider, playlist: Playlist) -> None:
    """Keep what the playlist says about its provider: the panel login and
    the guide address. Not committed here (the caller's transaction)."""
    if playlist.login is not None:
        base, user, pw = playlist.login
        if (provider.server_url, provider.username, provider.password) != (base, user, pw):
            logger.info(f"[M3U] {provider.name}: its links are an Xtream panel's ({base}); "
                        f"live opens as .ts, films play through Tentacle")
        provider.server_url, provider.username, provider.password = base, user, pw
    elif playlist.entries and provider.server_url:
        # No longer a panel's links (another playlist was entered): forget it
        provider.server_url, provider.username, provider.password = "", "", ""
    key = GUIDE_URL_KEY.format(provider.id)
    value = ",".join(playlist.guide_urls)
    if provider.id and value != (get_setting(db, key, "") or ""):
        from models.database import Setting
        row = db.query(Setting).filter(Setting.key == key).first()
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value


def guide_url(db, provider_id: int) -> str:
    """The first guide address the provider's playlist names, or ""."""
    raw = (get_setting(db, GUIDE_URL_KEY.format(provider_id), "") or "").strip()
    return raw.split(",", 1)[0].strip() if raw else ""


def account_info(provider, timeout: float = 10.0) -> dict:
    """The panel's player_api answer for an M3U provider whose links are a
    panel's, or {} (no panel login, or the panel does not answer it: many
    serve get.php only, some only player_api). Never raises."""
    if not is_m3u(provider) or not (provider.server_url and provider.username):
        return {}
    import requests
    from services.xtream_client import quote_cred
    url = (f"{provider.server_url.rstrip('/')}/player_api.php"
           f"?username={quote_cred(provider.username)}&password={quote_cred(provider.password)}")
    try:
        r = requests.get(url, headers={"User-Agent": provider.user_agent or _UA_DEFAULT}, timeout=timeout)
        data = r.json() if r.status_code == 200 else {}
    except Exception as e:
        logger.info(f"[M3U] {provider.name}: the panel did not give account details ({type(e).__name__})")
        return {}
    info = data.get("user_info") if isinstance(data, dict) else None
    if not isinstance(info, dict) or str(info.get("auth", "")) not in ("1", "True", "true"):
        return {}
    return data


def guard_origin(provider) -> str:
    """The one LAN origin a provider's streams may use (services.ssrf.
    lan_origin_guard): the address the admin entered. For an Xtream provider
    its server; for a playlist link the playlist's own address; for a
    playlist file, which the admin put on the server, its panel's server."""
    if provider is None:
        return ""
    ptype = provider.provider_type or "xtream"
    if ptype == "m3u_url":
        return provider.m3u_url or ""
    return provider.server_url or ""
