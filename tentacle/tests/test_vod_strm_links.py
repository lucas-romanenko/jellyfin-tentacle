"""With `vod_via_tentacle_enabled` on, the sync writes .strm files that point
at Tentacle's /api/vod route; existing files are rewritten in place either
way; and the code that keys titles by their provider stream id still works.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import types
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tmp_dirs import temp_dir

SECRET = "k" * 64


def _db():
    import models.database as mdb
    engine = create_engine(f"sqlite:///{temp_dir()}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _client(links=None):
    from services.sync import XtreamClient
    c = XtreamClient(types.SimpleNamespace(id=3, server_url="http://cf.panel.test", username="u", password="p"))
    c.vod_links = links
    return c


def _links():
    from services import vod_tokens
    return vod_tokens.Links("http://192.168.2.52:8888", SECRET, 3)


class UrlsTheClientWrites(unittest.TestCase):
    def test_direct_by_default(self):
        c = _client()
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", c.movie_stream_url(42, "mkv"))
        self.assertEqual("http://cf.panel.test/series/u/p/7.mp4", c.episode_stream_url(7, "mp4"))

    def test_through_tentacle_when_links_are_set(self):
        c = _client(_links())
        self.assertRegex(c.movie_stream_url(42, "mkv"), r"^http://192\.168\.2\.52:8888/api/vod/movie/3\.42\.[0-9a-f]{20}\.mkv$")
        self.assertRegex(c.episode_stream_url(7, "mp4"), r"^http://192\.168\.2\.52:8888/api/vod/series/3\.7\.[0-9a-f]{20}\.mp4$")


class LinksComeFromTheSettings(unittest.TestCase):
    """VOD streams through Tentacle unless the admin opted out, and only at
    an address players can use: a wrong one would break every title, so
    without a safe one the files keep their direct provider links."""

    def setUp(self):
        self.db = _db()
        self.addCleanup(self.db.close)
        from models.database import Provider
        self.provider = Provider(name="P", server_url="http://cf.panel.test", username="u", password="p",
                                 provider_type="xtream")
        self.db.add(self.provider)
        self.db.commit()
        self.probed = []

    def _links(self, plugin=None, youtube=None, answers=True):
        from models.database import get_setting, set_setting
        from services.sync import vod_links_for
        if youtube is not None:
            set_setting(self.db, "youtube_base_url", youtube)

        def check(url, timeout=10):
            self.probed.append(url)
            ok = answers(url) if callable(answers) else answers
            return {"ok": ok, "detail": "ok" if ok else "This address answered HTTP 302."}
        with mock.patch("services.youtube.sync.plugin_tentacle_url", lambda db: plugin), \
                mock.patch("services.youtube.sync.check_base_url", check):
            links = vod_links_for(self.db, self.provider)
        return links, get_setting(self.db, "vod_base_url", "")

    def test_on_by_default_at_the_jellyfin_plugins_address(self):
        """The plugin's TentacleUrl is how Jellyfin already reaches Tentacle;
        once it answers as Tentacle it is kept, so the files never flip."""
        links, saved = self._links(plugin="http://192.168.2.77:8888/")
        self.assertRegex(links.movie(1, "mp4"), r"^http://192\.168\.2\.77:8888/api/vod/movie/")
        self.assertEqual("http://192.168.2.77:8888", saved)
        self.assertEqual(["http://192.168.2.77:8888"], self.probed)
        self.probed.clear()
        links, _ = self._links(plugin=None)
        self.assertIsNotNone(links, "the saved address is used without asking again")
        self.assertEqual([], self.probed)

    def test_opted_out_keeps_direct_links(self):
        from models.database import set_setting
        for off in ("false", "0", "off", "no"):
            set_setting(self.db, "vod_via_tentacle_enabled", off)
            links, _ = self._links(plugin="http://192.168.2.77:8888")
            self.assertIsNone(links, off)
        set_setting(self.db, "vod_via_tentacle_enabled", "true")
        self.assertIsNotNone(self._links(plugin="http://192.168.2.77:8888")[0])

    def test_no_address_known_keeps_direct_links_and_says_why(self):
        with self.assertLogs("services.sync", level="WARNING") as logs:
            links, saved = self._links(plugin=None)
        self.assertIsNone(links)
        self.assertEqual("", saved)
        self.assertIn("direct provider links", "\n".join(logs.output))

    def test_an_address_that_does_not_answer_as_tentacle_is_not_used(self):
        with self.assertLogs("services.sync", level="WARNING") as logs:
            links, saved = self._links(plugin="http://192.168.2.77:8888", answers=False)
        self.assertIsNone(links)
        self.assertEqual("", saved, "nothing saved: the next sync tries again")
        self.assertIn("HTTP 302", "\n".join(logs.output))

    def test_container_names_and_loopback_are_never_used(self):
        """Inside Docker "tentacle" resolves, so it would answer the probe,
        but the TV and other containers' players can't use it."""
        for bad in ("http://tentacle:8888", "http://localhost:8888", "http://127.0.0.1:8888"):
            self.probed.clear()
            links, saved = self._links(plugin=bad)
            self.assertIsNone(links, bad)
            self.assertEqual([], self.probed, "not even probed")

    def test_the_youtube_address_is_the_fallback(self):
        links, saved = self._links(plugin=None, youtube="http://192.168.2.52:8888")
        self.assertEqual("http://192.168.2.52:8888", saved)
        links, saved = self._links(plugin="http://tentacle:8888")
        self.assertIsNotNone(links)

    def test_vod_base_url_wins_over_everything(self):
        from models.database import set_setting
        set_setting(self.db, "vod_base_url", "http://192.168.2.52:8888/")
        links, _ = self._links(plugin="http://192.168.2.77:8888", youtube="https://tentacle.example.net")
        self.assertRegex(links.movie(1, "mp4"), r"^http://192\.168\.2\.52:8888/api/vod/movie/")
        self.assertEqual([], self.probed)

    def test_m3u_providers_keep_their_own_urls(self):
        self.provider.provider_type = "m3u_url"
        self.assertIsNone(self._links(plugin="http://192.168.2.77:8888")[0])


class ExistingFilesAreRewrittenInPlace(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        self.addCleanup(self.db.close)
        from models.database import Movie, Provider
        self.provider = Provider(name="P", server_url="http://cf.panel.test", username="u", password="p")
        self.db.add(self.provider)
        self.db.commit()
        self.dir = Path(temp_dir(self))
        self.strm = self.dir / "Film (2026).strm"
        self.movie = Movie(tmdb_id=99, title="Film", year="2026", source="provider_1",
                           provider_id=self.provider.id, strm_path=str(self.strm))
        self.db.add(self.movie)
        self.db.commit()
        self.stream = {"stream_id": 42, "container_extension": "mkv"}

    def _repair(self, client):
        from services.sync import _repair_movie_strm
        with mock.patch("services.sync.chown_path", lambda p: None):
            return _repair_movie_strm(client, self.stream, 99, self.provider, self.db)

    def test_direct_url_becomes_tentacle_url_when_turned_on(self):
        self.strm.write_text("http://cf.panel.test/movie/u/p/42.mkv")
        self._repair(_client(_links()))
        self.assertIn("/api/vod/movie/3.42.", self.strm.read_text())

    def test_tentacle_url_becomes_direct_again_when_turned_off(self):
        self.strm.write_text(_links().movie(42, "mkv"))
        self._repair(_client())
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", self.strm.read_text())

    def test_a_resume_proxy_wrapping_the_provider_url_is_ours_to_rewrite(self):
        self.strm.write_text("http://192.168.2.52:8889/proxy/stream/Film.mkv?d=http%3A%2F%2Fcf.panel.test%2Fmovie%2Fu%2Fp%2F42.mkv&api_password=x")
        self._repair(_client(_links()))
        self.assertIn("/api/vod/movie/3.42.", self.strm.read_text())

    def test_a_second_listing_of_the_same_title_does_not_flip_the_file(self):
        """A provider that lists one film in two categories offers it twice
        (two stream ids). The sync sees both; whichever comes second must
        not rewrite the file to its own copy -- with VOD off that would
        change the file every night for no reason, and with VOD on it would
        flip which copy plays. Only the SAME stream in a different form is
        rewritten."""
        self.strm.write_text("http://cf.panel.test/movie/u/p/42.mkv")
        self.stream = {"stream_id": 43, "container_extension": "mkv"}
        self._repair(_client())
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", self.strm.read_text())
        self._repair(_client(_links()))
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", self.strm.read_text())
        self.strm.write_text(_links().movie(42, "mkv"))
        self._repair(_client(_links()))
        self.assertIn("/api/vod/movie/3.42.", self.strm.read_text(), "the Tentacle form is kept, for 42")

    def test_new_credentials_for_the_same_stream_are_a_rewrite(self):
        self.strm.write_text("http://cf.panel.test:8080/movie/olduser/oldpass/42.mp4")
        self._repair(_client())
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", self.strm.read_text())

    def test_files_that_are_not_this_providers_own_url_are_left_alone(self):
        """Whatever carries the same stream id: another host with another
        login, a proxy with the provider URL unencoded in its query, a proxy
        that puts the provider's path under its own prefix, a URL with a
        query. (Another host with this provider's own login is the provider
        moved, and is rewritten: test_provider_address_change_strms, #142.)"""
        for text in ("http://other.example/movie/someone/else/42.mkv",
                     "http://192.168.2.10:9000/play?url=http://cf.panel.test/movie/u/p/42.mkv",
                     "http://myproxy.lan/xtream/movie/u/p/42.mkv",
                     "http://cf.panel.test/movie/u/p/42.mkv?token=abc"):
            for links in (None, _links()):
                self.strm.write_text(text)
                self._repair(_client(links))
                self.assertEqual(text, self.strm.read_text(), (text, links is not None))

    def test_a_provider_whose_address_has_a_path_prefix(self):
        """server_url = http://prov.example.com:8080/xc -> stream URLs are
        /xc/movie/<u>/<p>/<id>.<ext>; the rule must follow the prefix."""
        from services.sync import XtreamClient, _strm_needs_rewrite
        c = XtreamClient(types.SimpleNamespace(id=3, server_url="http://prov.example.com:8080/xc/",
                                               username="u", password="p"))
        c.vod_links = None
        expected = c.movie_stream_url(42, "mkv")
        self.assertEqual("http://prov.example.com:8080/xc/movie/u/p/42.mkv", expected)
        self.strm.write_text("http://prov.example.com:8080/xc/movie/old/pw/42.mkv")
        self.assertTrue(_strm_needs_rewrite(self.strm, expected, c), "new credentials")
        self.assertTrue(_strm_needs_rewrite(self.strm, _links().movie(42, "mkv"), c), "off -> on")
        self.strm.write_text(_links().movie(42, "mkv"))
        self.assertTrue(_strm_needs_rewrite(self.strm, expected, c), "on -> off")
        self.strm.write_text("http://prov.example.com:8080/movie/u/p/42.mkv")
        self.assertFalse(_strm_needs_rewrite(self.strm, expected, c), "not under the provider's prefix")

    def test_a_tentacle_link_of_another_provider_is_left_alone(self):
        from services import vod_tokens
        other = vod_tokens.Links("http://192.168.2.52:8888", SECRET, 4).movie(42, "mkv")
        self.strm.write_text(other)
        self._repair(_client())
        self.assertEqual(other, self.strm.read_text())

    def test_m3u_providers_never_rewrite_existing_files(self):
        """Their playlist URLs may carry a rotating token or a load-balanced
        host; rewriting on every change would touch every file nightly."""
        from services.sync import _strm_needs_rewrite, M3UClient
        client = M3UClient.__new__(M3UClient)
        self.strm.write_text("http://cdn1.example/movie/u/p/42.mkv?token=abc")
        self.assertFalse(_strm_needs_rewrite(self.strm, "http://cdn2.example/movie/u/p/42.mkv?token=def", client))

    def test_a_proxy_wrapper_is_left_alone_while_vod_through_tentacle_is_off(self):
        """Somebody's own resume proxy in front of the provider is a
        deliberate setup; only the migration TO Tentacle's route unwraps it."""
        wrapped = "http://192.168.2.52:8889/proxy/stream/Film.mkv?d=http%3A%2F%2Fcf.panel.test%2Fmovie%2Fu%2Fp%2F42.mkv&api_password=x"
        self.strm.write_text(wrapped)
        self._repair(_client())
        self.assertEqual(wrapped, self.strm.read_text())

    def test_a_hand_made_file_pointing_elsewhere_is_left_alone(self):
        self.strm.write_text("http://nas.local/films/film.mkv")
        self._repair(_client(_links()))
        self.assertEqual("http://nas.local/films/film.mkv", self.strm.read_text())

    def test_an_opted_out_title_is_left_alone(self):
        self.movie.strm_disabled = True
        self.db.commit()
        self.strm.write_text("http://cf.panel.test/movie/u/p/42.mkv")
        self._repair(_client(_links()))
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", self.strm.read_text())

    def test_unchanged_files_are_not_touched(self):
        self.strm.write_text("http://cf.panel.test/movie/u/p/42.mkv")
        before = self.strm.stat().st_mtime_ns
        self._repair(_client())
        self.assertEqual(before, self.strm.stat().st_mtime_ns)

    def test_episodes_follow_the_same_rule(self):
        from services.sync import _write_episode_strms
        show = self.dir / "Show (2020)"
        episodes = {"1": [{"id": 7, "episode_num": 1, "container_extension": "mp4", "title": "Pilot"}]}
        with mock.patch("services.sync.chown_path", lambda p: None):
            n = _write_episode_strms(_client(), episodes, show, "Show (2020)")
            self.assertEqual(1, n)
            f = next((show / "Season 01").glob("*.strm"))
            self.assertEqual("http://cf.panel.test/series/u/p/7.mp4", f.read_text())
            self.assertEqual(0, _write_episode_strms(_client(_links()), episodes, show, "Show (2020)"),
                             "a rewrite is not a new episode")
            self.assertIn("/api/vod/series/3.7.", f.read_text())


class StreamIdsStayReadable(unittest.TestCase):
    def test_wrong_match_key_and_health_parse_see_through_the_tentacle_url(self):
        from services import stream_health, wrong_match
        u = _links().movie(42, "mkv")
        self.assertEqual("42", wrong_match.stream_key_for_url(u))
        self.assertEqual(("movie", 42), stream_health._parse_stream_id(u))
        self.assertEqual(("series", 7), stream_health._parse_stream_id(_links().episode(7, "mp4")))
        self.assertEqual("42", wrong_match.stream_key_for_url("http://cf.panel.test/movie/u/p/42.mkv"))

    def test_the_health_probe_goes_to_the_provider_not_through_tentacle(self):
        from services import stream_health
        provider = types.SimpleNamespace(server_url="http://cf.panel.test", username="u", password="p")
        u = _links().movie(42, "mkv")
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", stream_health._direct_url(u, "movie", 42, provider))
        self.assertEqual("http://x/y.mkv", stream_health._direct_url("http://x/y.mkv", "movie", 42, provider))


class _Catalogue:
    """An XtreamClient (so the sync's rewrite rules apply to it) whose
    listings come from the test, never from the network."""

    @staticmethod
    def make(provider, movies, series):
        from services.sync import XtreamClient

        class Client(XtreamClient):
            def get_vod_streams(self, cat_id):
                return [{"name": f"{t} (2010)", "stream_id": sid, "container_extension": "mkv"}
                        for t, sid in movies.get(cat_id, [])]

            def get_series_list(self, cat_id):
                return [{"name": f"{t} (2010)", "series_id": sid} for t, sid in series.get(cat_id, [])]

            def get_series_info(self, series_id):
                return {"episodes": {"1": [{"id": series_id * 10 + 1, "episode_num": 1,
                                            "container_extension": "mp4"}]}}
        return Client(provider)


class TheSyncFollowsTheSetting(unittest.TestCase):
    """Sync now / the nightly sync, end to end: films and episodes already
    in the library move to Tentacle's route (the default), and back to the
    provider's own URL for an install that opted out."""

    def setUp(self):
        from nightly_harness import FakeTMDB, NightlyHarness
        import services.sync as sync
        self.sync, self.FakeTMDB = sync, FakeTMDB
        harness = NightlyHarness("run")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.addCleanup(harness.tearDown)
        self.h = harness
        harness.add_category("1", "movie")
        harness.add_category("2", "series")
        FakeTMDB.ids = {"Film": 1000, "Show": 5000}
        client = _Catalogue.make(harness.provider, {"1": [("Film", 42)]}, {"2": [("Show", 70)]})
        sync.make_provider_client = lambda provider: client

    def _night(self):
        with mock.patch("services.sync.chown_path", lambda p: None):
            self.h.sync_only()
        film = next((self.h.vod / "movies").rglob("*.strm")).read_text(encoding="utf-8")
        ep = next((self.h.vod / "shows").rglob("*.strm")).read_text(encoding="utf-8")
        return film, ep

    def test_an_upgraded_install_moves_over_and_an_opt_out_moves_back(self):
        from models.database import Setting, set_setting
        pid = self.h.provider.id
        set_setting(self.h.db, "vod_via_tentacle_enabled", "false")
        self.assertEqual(("http://provider/movie/u/p/42.mkv", "http://provider/series/u/p/701.mp4"), self._night())
        # Upgraded: the setting was never set (the opt-out is gone here), and
        # Tentacle's address is known.
        self.h.db.query(Setting).filter(Setting.key == "vod_via_tentacle_enabled").delete()
        self.h.db.commit()
        set_setting(self.h.db, "vod_base_url", "http://192.168.2.52:8888")
        film, ep = self._night()
        self.assertRegex(film, rf"^http://192\.168\.2\.52:8888/api/vod/movie/{pid}\.42\.[0-9a-f]{{20}}\.mkv$")
        self.assertRegex(ep, rf"^http://192\.168\.2\.52:8888/api/vod/series/{pid}\.701\.[0-9a-f]{{20}}\.mp4$")
        self.assertEqual((film, ep), self._night(), "a second sync changes nothing")
        set_setting(self.h.db, "vod_via_tentacle_enabled", "false")
        self.assertEqual(("http://provider/movie/u/p/42.mkv", "http://provider/series/u/p/701.mp4"), self._night())


class TheLibraryMovesOverWithoutASync(unittest.TestCase):
    """An upgraded install moves its existing .strm files to Tentacle's
    route a minute or two after start-up (main.py schedules
    provider_relink.start_vod_route_relink), with no request to the
    provider; a save that opts out (or back in) does the same at once. Every
    run is idempotent."""

    def setUp(self):
        import models.database as mdb
        from models.database import Movie, Provider, Series, set_setting
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.provider = Provider(name="P", provider_type="xtream", server_url="http://cf.panel.test",
                                 username="u", password="p")
        self.other = Provider(name="O", provider_type="xtream", server_url="http://other.test",
                              username="x", password="y")
        self.db.add_all([self.provider, self.other])
        self.db.commit()
        set_setting(self.db, "vod_token_secret", SECRET)
        set_setting(self.db, "vod_base_url", "http://192.168.2.52:8888")
        root = Path(temp_dir(self))
        self.film = root / "Film (2020)" / "Film (2020).strm"
        self.film.parent.mkdir(parents=True)
        self.film.write_text("http://cf.panel.test/movie/u/p/42.mkv", encoding="utf-8")
        show = root / "Show (2020)"
        self.ep = show / "Season 01" / "Show (2020) S01E01.strm"
        self.ep.parent.mkdir(parents=True)
        self.ep.write_text("http://cf.panel.test/series/u/p/701.mp4", encoding="utf-8")
        self.mine = root / "Mine (2020).strm"
        self.mine.write_text("http://nas.local/films/mine.mkv", encoding="utf-8")
        self.theirs = root / "Theirs (2020).strm"
        self.theirs.write_text("http://other.test/movie/x/y/9.mkv", encoding="utf-8")
        self.db.add_all([
            Movie(tmdb_id=1, title="Film", year="2020", source="provider", provider_id=self.provider.id,
                  strm_path=str(self.film)),
            Movie(tmdb_id=2, title="Mine", year="2020", source="provider", provider_id=self.provider.id,
                  strm_path=str(self.mine)),
            Movie(tmdb_id=3, title="Theirs", year="2020", source="provider", provider_id=self.other.id,
                  strm_path=str(self.theirs)),
            Series(tmdb_id=4, title="Show", year="2020", source="provider", provider_id=self.provider.id,
                   strm_path=str(show)),
        ])
        self.db.commit()
        self.scans = []
        scans = self.scans

        class _Jellyfin:
            def __init__(self, *a):
                pass

            def trigger_library_scan(self, library_id=None):
                scans.append(library_id)
                return True
        self._jellyfin = _Jellyfin
        set_setting(self.db, "jellyfin_url", "http://jellyfin.test")
        set_setting(self.db, "jellyfin_api_key", "x")

    def _relink(self):
        """The rewrite as the start-up job and a save run it, in place of its thread."""
        from services import provider_relink
        with mock.patch("models.database.SessionLocal", self.Session), \
                mock.patch("services.jellyfin.JellyfinService", self._jellyfin), \
                mock.patch("services.youtube.sync.plugin_tentacle_url", lambda db: None), \
                mock.patch("services.sync.chown_path", lambda p: None):
            provider_relink._run_vod_route()

    def _save(self, on: bool):
        """POST /api/settings with `vod_via_tentacle_enabled`, as an admin
        opting out (or back in) does; the rewrite it starts runs in place."""
        import routers.settings as settings
        from services import provider_relink
        started = []
        with mock.patch.object(provider_relink, "start_vod_route_relink", lambda: started.append(1)):
            settings.update_settings(settings.SettingsUpdate(
                settings={"vod_via_tentacle_enabled": "true" if on else "false"}), self.db)
        if started:
            self._relink()
        return len(started)

    def _activity(self):
        import models.database as mdb
        self.db.expire_all()
        return [e.message for e in self.db.query(mdb.ActivityLog).filter_by(event="vod_route_relinked").all()]

    def test_start_up_moves_an_upgraded_install_over_once(self):
        pid = self.provider.id
        self._relink()
        self.assertRegex(self.film.read_text(encoding="utf-8"),
                         rf"^http://192\.168\.2\.52:8888/api/vod/movie/{pid}\.42\.[0-9a-f]{{20}}\.mkv$")
        self.assertRegex(self.ep.read_text(encoding="utf-8"),
                         rf"^http://192\.168\.2\.52:8888/api/vod/series/{pid}\.701\.[0-9a-f]{{20}}\.mp4$")
        self.assertEqual([None], self.scans, "Jellyfin rescans to pick the new addresses up")
        self.assertEqual(["2 films and 1 episodes now play through Tentacle"], self._activity())
        before = (self.film.stat().st_mtime_ns, self.ep.stat().st_mtime_ns)
        self._relink()                                 # the next start-up
        self.assertEqual(before, (self.film.stat().st_mtime_ns, self.ep.stat().st_mtime_ns))
        self.assertEqual([None], self.scans, "nothing changed: no scan")
        self.assertEqual(1, len(self._activity()), "and no new entry")

    def test_the_providers_host_with_another_port_or_scheme_is_its_own(self):
        """Matched by host and login, as the sync reads them: a file written
        with :80 or https is still this provider's."""
        self.film.write_text("http://cf.panel.test:80/movie/u/p/42.mkv", encoding="utf-8")
        self.ep.write_text("https://CF.panel.test/series/u/p/701.mp4", encoding="utf-8")
        with self.assertLogs("services.provider_relink", level="INFO") as logs:
            self._relink()
        self.assertIn("/api/vod/movie/", self.film.read_text(encoding="utf-8"))
        self.assertIn("/api/vod/series/", self.ep.read_text(encoding="utf-8"))
        self.assertTrue(any("4 .strm files checked, 2 films and 1 episodes rewritten" in line
                            for line in logs.output), logs.output)

    def test_a_run_that_changes_nothing_still_says_so(self):
        self._relink()
        with self.assertLogs("services.provider_relink", level="INFO") as logs:
            self._relink()
        self.assertTrue(any("0 films and 0 episodes rewritten" in line for line in logs.output), logs.output)

    def test_an_opt_out_puts_them_back_and_opting_in_again_moves_them_again(self):
        self._relink()
        self.assertEqual(1, self._save(False))
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", self.film.read_text(encoding="utf-8"))
        self.assertEqual("http://cf.panel.test/series/u/p/701.mp4", self.ep.read_text(encoding="utf-8"))
        self.assertIn("now play straight from the provider", self._activity()[-1])
        self.assertEqual(1, self._save(True))
        self.assertIn("/api/vod/movie/", self.film.read_text(encoding="utf-8"))

    def test_a_save_that_changes_nothing_starts_nothing(self):
        self.assertEqual(0, self._save(True), "unset already means on")
        self.assertEqual(1, self._save(False))
        self.assertEqual(0, self._save(False))

    def test_files_that_are_not_the_providers_own_are_left_alone(self):
        self._relink()
        self.assertEqual("http://nas.local/films/mine.mkv", self.mine.read_text(encoding="utf-8"))
        # The other provider's own file does move, through its own link.
        self.assertIn(f"/api/vod/movie/{self.other.id}.9.", self.theirs.read_text(encoding="utf-8"))
        self._save(False)
        self.assertEqual("http://other.test/movie/x/y/9.mkv", self.theirs.read_text(encoding="utf-8"))

    def test_without_a_safe_address_nothing_is_rewritten(self):
        import models.database as mdb
        mdb.set_setting(self.db, "vod_base_url", "")
        self._relink()
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", self.film.read_text(encoding="utf-8"))
        self.assertEqual([], self.scans)
        self.assertEqual([], self._activity())

    def test_an_opted_out_title_is_left_alone(self):
        from models.database import Movie
        self.db.query(Movie).filter(Movie.tmdb_id == 1).one().strm_disabled = True
        self.db.commit()
        self._relink()
        self.assertEqual("http://cf.panel.test/movie/u/p/42.mkv", self.film.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
