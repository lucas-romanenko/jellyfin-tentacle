"""With `vod_via_tentacle_enabled` on, the sync writes .strm files that point
at Tentacle's /api/vod route; existing files are rewritten in place either
way; and the code that keys titles by their provider stream id still works.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SECRET = "k" * 64


def _db():
    import models.database as mdb
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})
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
    def setUp(self):
        self.db = _db()
        self.addCleanup(self.db.close)
        from models.database import Provider
        self.provider = Provider(name="P", server_url="http://cf.panel.test", username="u", password="p",
                                 provider_type="xtream")
        self.db.add(self.provider)
        self.db.commit()

    def test_off_by_default(self):
        from services.sync import vod_links_for
        self.assertIsNone(vod_links_for(self.db, self.provider))

    def test_on_needs_tentacles_address(self):
        from models.database import set_setting
        from services.sync import vod_links_for
        set_setting(self.db, "vod_via_tentacle_enabled", "true")
        with mock.patch("services.youtube.sync.base_url", lambda db, *a, **k: ""):
            self.assertIsNone(vod_links_for(self.db, self.provider), "no address: direct URLs, with a warning")
        with mock.patch("services.youtube.sync.base_url", lambda db, *a, **k: "http://192.168.2.52:8888"):
            links = vod_links_for(self.db, self.provider)
        self.assertIsNotNone(links)
        self.assertEqual(self.provider.id, links.provider_id)
        self.assertRegex(links.movie(1, "mp4"), r"^http://192\.168\.2\.52:8888/api/vod/movie/")

    def test_m3u_providers_keep_their_own_urls(self):
        from models.database import set_setting
        from services.sync import vod_links_for
        set_setting(self.db, "vod_via_tentacle_enabled", "true")
        self.provider.provider_type = "m3u_url"
        with mock.patch("services.youtube.sync.base_url", lambda db, *a, **k: "http://t"):
            self.assertIsNone(vod_links_for(self.db, self.provider))


class ExistingFilesAreRewrittenInPlace(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        self.addCleanup(self.db.close)
        from models.database import Movie, Provider
        self.provider = Provider(name="P", server_url="http://cf.panel.test", username="u", password="p")
        self.db.add(self.provider)
        self.db.commit()
        self.dir = Path(tempfile.mkdtemp())
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


if __name__ == "__main__":
    unittest.main()
