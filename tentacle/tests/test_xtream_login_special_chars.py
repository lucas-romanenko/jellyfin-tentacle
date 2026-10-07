"""An Xtream login with characters that mean something in a URL (#529).

Every Xtream URL put the stored username and password in as typed. In the
player_api.php / xmltv.php query a '#' cut the rest off as a fragment, '&'
split the value, '+' arrived as a space and '%41' as 'A'; in the stream path
(/movie|series|live/<user>/<pass>/<id>.<ext>) a '#', '?' or '/' cut the path.
Test, categories and the sync got a wrong login, and .strm files and live
channels never played. The login is now percent-encoded wherever a URL is
built, a login of plain characters gives the same URLs as before, and a .strm
written in the old form is repaired by the next sync.
"""
import re
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from tmp_dirs import temp_dir

import services.sync as sync
from services.xtream_client import XtreamClient, live_stream_url

LOGINS = [("u", "Ab#12&cd"), ("u", "Ab+12"), ("u", "p%41"), ("john+tv@example.com", "Ab/12?cd")]
# Letters, digits, -._~ and !$()*,;=:@ need no encoding: such a login keeps
# byte-identical URLs, so no .strm, channel URL or cached guide changes.
SAFE = ("john@example.com", "Ab!$()*,;=:@-._~9")


def _query_login(url):
    """What a PHP panel reads from the query ('+' is a space, '&' splits, no fragment is sent)."""
    q = parse_qs(urlsplit(url).query, keep_blank_values=True)
    return (q.get("username") or [None])[0], (q.get("password") or [None])[0]


def _action(url):
    return (parse_qs(urlsplit(url).query).get("action") or [None])[0]


def _path_login(url):
    """/<kind>/<user>/<pass>/<id>.<ext>, each segment decoded on its own."""
    segs = urlsplit(url).path.split("/")
    return (unquote(segs[2]), unquote(segs[3])) if len(segs) == 5 else None


def _provider(user, password, pid=1, server="http://panel.test"):
    return types.SimpleNamespace(id=pid, name="P", server_url=server, username=user, password=password,
                                 provider_type="xtream", user_agent=None)


class VodSyncClient(unittest.TestCase):
    def test_api_query_and_stream_paths(self):
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                c = sync.XtreamClient(_provider(user, password))
                self.assertEqual((user, password), _query_login(c.base))
                self.assertEqual((user, password), _path_login(c.movie_stream_url(42, "mkv")))
                self.assertEqual((user, password), _path_login(c.episode_stream_url(7, "mp4")))

    def test_the_request_carries_the_login_and_the_action(self):
        user, password = "john+tv@example.com", "Ab#1&2+c/d?e f"
        c = sync.XtreamClient(_provider(user, password))
        seen = []

        def get(url, **kw):
            seen.append(url)
            r = mock.Mock()
            r.json.return_value = []
            return r
        c.session.get = get
        c.get_vod_streams("5")
        self.assertEqual((user, password), _query_login(seen[0]))
        self.assertEqual("get_vod_streams", _action(seen[0]))


class LiveTvClient(unittest.TestCase):
    def test_api_guide_and_stream_urls(self):
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                c = XtreamClient("http://panel.test", user, password)
                self.assertEqual((user, password), _query_login(c._api_url("get_live_streams")))
                self.assertEqual("get_live_streams", _action(c._api_url("get_live_streams")))
                self.assertEqual((user, password), _query_login(c.get_xmltv_url()))
                self.assertEqual((user, password), _path_login(c.live_stream_url(44, "ts")))
                self.assertEqual((user, password), _path_login(c.movie_stream_url(42, "mkv")))
                self.assertEqual((user, password), _path_login(c.series_stream_url(7, "mp4")))

    def test_channel_sync_and_provider_edit_write_the_same_url(self):
        user, password = LOGINS[3]
        c = XtreamClient("http://panel.test", user, password)
        self.assertEqual(c.live_stream_url(44, "ts"), live_stream_url("http://panel.test", user, password, 44, "ts"))


class ProviderRoutes(unittest.TestCase):
    def test_test_button(self):
        from routers import providers
        user, password = LOGINS[3]
        seen = []

        def get(url, **kw):
            seen.append(url)
            r = mock.Mock()
            r.json.return_value = {"user_info": {"auth": 1}}
            return r
        with mock.patch.object(providers.requests, "get", side_effect=get):
            providers.test_provider_connection(_provider(user, password))
        self.assertEqual((user, password), _query_login(seen[0]))

    def test_categories(self):
        from routers import providers
        user, password = LOGINS[0]
        seen = []

        class Sess:
            headers = {}

            def get(self, url, **kw):
                seen.append(url)
                r = mock.Mock()
                r.json.return_value = []
                return r
        with mock.patch.object(providers.requests, "Session", return_value=Sess()):
            providers.fetch_provider_categories(_provider(user, password))
        self.assertTrue(seen)
        for url in seen:
            self.assertEqual((user, password), _query_login(url), url)
            self.assertIsNotNone(_action(url), url)

    def test_no_url_puts_a_login_in_unencoded(self):
        """Every builder goes through the encoding helper: the capability
        probes of the Test route and the sync preview included."""
        root = Path(__file__).resolve().parent.parent
        raw = re.compile(r"\{[A-Za-z_.]*(?:username|password)\}")
        found = []
        for f in sorted((root / "services").rglob("*.py")) + sorted((root / "routers").rglob("*.py")):
            if f.name == "secret_mask.py":
                continue   # puts back a proxy URL's userinfo as urlsplit read it: already in URL form
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if raw.search(line) and not line.lstrip().startswith("#"):
                    found.append(f"{f.relative_to(root)}:{n}")
        self.assertEqual([], found)


class HealthProbeAndVodRoute(unittest.TestCase):
    def test_health_probe_of_a_tentacle_link(self):
        from services import stream_health, vod_tokens
        user, password = LOGINS[3]
        link = vod_tokens.Links("http://192.168.2.52:8888", "k" * 64, 1).movie(42, "mkv")
        url = stream_health._direct_url(link, "movie", 42, _provider(user, password))
        self.assertEqual((user, password), _path_login(url))

    def test_vod_through_tentacle_opens_the_right_upstream_url(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from routers import vod
        from services import vod_tokens
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        user, password = LOGINS[3]
        db.add(mdb.Provider(id=1, name="P", server_url="http://panel.test", username=user, password=password,
                            provider_type="xtream", active=True))
        db.commit()
        secret = vod_tokens.token_secret(db)
        db.commit()
        token = vod_tokens.Links("http://t:8888", secret, 1).movie(42, "mkv").rsplit("/", 1)[1]
        with mock.patch.object(vod, "lan_origin_guard", return_value=lambda u: True):
            url = vod._resolve(db, "movie", token)[0]
        self.assertEqual((user, password), _path_login(url))


class PlainLoginsKeepTheirUrls(unittest.TestCase):
    def test_byte_identical(self):
        user, password = SAFE
        c = sync.XtreamClient(_provider(user, password))
        self.assertEqual(f"http://panel.test/player_api.php?username={user}&password={password}", c.base)
        self.assertEqual(f"http://panel.test/movie/{user}/{password}/42.mkv", c.movie_stream_url(42, "mkv"))
        lc = XtreamClient("http://panel.test", user, password)
        self.assertEqual(f"http://panel.test/live/{user}/{password}/44.ts", lc.live_stream_url(44, "ts"))
        self.assertEqual(f"http://panel.test/xmltv.php?username={user}&password={password}", lc.get_xmltv_url())


class OldFilesAreRepaired(unittest.TestCase):
    def test_a_strm_written_with_the_raw_login_is_rewritten(self):
        d = Path(temp_dir(self))
        f = d / "Film (2020).strm"
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                c = sync.XtreamClient(_provider(user, password))
                expected = c.movie_stream_url(42, "mkv")
                f.write_text(f"http://panel.test/movie/{user}/{password}/42.mkv", encoding="utf-8")
                self.assertTrue(sync._strm_needs_rewrite(f, expected, c))
                f.write_text(expected, encoding="utf-8")
                self.assertFalse(sync._strm_needs_rewrite(f, expected, c))

    def test_another_stream_in_the_old_form_is_left_alone(self):
        user, password = "u", "Ab/12?cd"
        f = Path(temp_dir(self)) / "Film (2020).strm"
        f.write_text(f"http://panel.test/movie/{user}/{password}/43.mkv", encoding="utf-8")
        c = sync.XtreamClient(_provider(user, password))
        self.assertFalse(sync._strm_needs_rewrite(f, c.movie_stream_url(42, "mkv"), c))


class OtherProvidersAreStillRecognised(unittest.TestCase):
    """The username read back from a URL is compared decoded with the stored one."""
    OTHER = ("john+tv@example.com", "Ab#12&cd")

    def _other_file(self):
        other = sync.XtreamClient(_provider(*self.OTHER, pid=2, server="http://other.test"))
        f = Path(temp_dir(self)) / "Film (2020).strm"
        f.write_text(other.movie_stream_url(42, "mkv"), encoding="utf-8")
        return f

    def test_strm_plays_other_provider(self):
        f = self._other_file()
        c = sync.XtreamClient(_provider("u", "p"))
        c.other_providers = {"ids": {2}, "accounts": {("other.test", self.OTHER[0])}}
        self.assertTrue(sync._strm_plays_other_provider(f, c))

    def test_stream_origin(self):
        f = self._other_file()
        self.assertEqual(("host", "other.test", self.OTHER[0]), sync._stream_origin(f.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
