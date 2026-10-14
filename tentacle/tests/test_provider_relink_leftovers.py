"""Every film and episode follows a provider's new address, not only the ones
the save's rewrite or a successful category read reached (#149, after #142).

One household saved its provider's new link before #142's fix was deployed,
so the save rewrote nothing. The next sync repaired only the titles of the
categories it could read: the provider refused 14 of its 42 categories
(HTTP 403), and their files kept the dead host. Every later sync was refused
from the start, so they were never repaired. And the save's rewrite runs in a
background thread: a restart before it ran (a deploy, a crash) lost it for
good.

  * relink_strms(db, provider, None): no old address known, it rewrites this
    provider's own direct files that carry the provider's current login on
    another host (the provider moved), the same rule the sync's
    _strm_needs_rewrite applies to listed titles;
  * the sync does that first, before any request to the provider, so a
    provider that refuses the sync still gets its library repointed;
  * a save records the old address (setting provider_relink_pending) until
    the rewrite has run, and resume_pending() runs what a restart left.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Provider, Series
from nightly_harness import FakeTMDB, NightlyHarness
import services.sync as sync
from tmp_dirs import temp_dir

OLD = "http://old-panel.example:8080"
NEW = "http://new-panel.example"


class _Base(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.dir = Path(temp_dir(self))
        self.provider = Provider(name="P", provider_type="xtream", server_url=NEW, username="u", password="p1")
        self.other = Provider(name="Other", provider_type="xtream", server_url=NEW, username="u", password="p1")
        self.db.add_all([self.provider, self.other])
        self.db.commit()

    def _movie(self, tmdb_id, name, text, provider=None):
        strm = self.dir / "Movies" / name / f"{name}.strm"
        strm.parent.mkdir(parents=True)
        strm.write_text(text, encoding="utf-8")
        self.db.add(Movie(tmdb_id=tmdb_id, title=name, year="2020", source="provider",
                          provider_id=(provider or self.provider).id, strm_path=str(strm)))
        self.db.commit()
        return strm

    def _episode(self, tmdb_id, name, text):
        show = self.dir / "Shows" / name
        f = show / "Season 01" / f"{name} S01E01.strm"
        f.parent.mkdir(parents=True)
        f.write_text(text, encoding="utf-8")
        self.db.add(Series(tmdb_id=tmdb_id, title=name, year="2020", source="provider",
                           provider_id=self.provider.id, strm_path=str(show)))
        self.db.commit()
        return f


class NoOldAddressKnown(_Base):
    def _relink(self):
        from services.provider_relink import relink_strms
        with mock.patch("services.sync.chown_path", lambda p: None):
            return relink_strms(self.db, self.provider, None)

    def test_files_with_the_current_login_on_a_former_host_follow(self):
        film = self._movie(1, "Film (2020)", f"{OLD}/movie/u/p1/42.mkv")
        ep = self._episode(2, "Show (2020)", f"{OLD}/series/u/p1/700.mp4")
        counts = self._relink()
        self.assertEqual(f"{NEW}/movie/u/p1/42.mkv", film.read_text(encoding="utf-8"))
        self.assertEqual(f"{NEW}/series/u/p1/700.mp4", ep.read_text(encoding="utf-8"))
        self.assertEqual((1, 1), (counts["movies"], counts["episodes"]))

    def test_another_login_another_provider_and_current_files_are_left_alone(self):
        stranger = self._movie(1, "Theirs (2020)", f"{OLD}/movie/someone/else/42.mkv")
        others = self._movie(2, "Other (2020)", f"{OLD}/movie/u/p1/43.mkv", provider=self.other)
        current = self._movie(3, "Current (2020)", f"{NEW}/movie/u/p1/44.mkv")
        before = current.stat().st_mtime_ns
        self._relink()
        self.assertEqual(f"{OLD}/movie/someone/else/42.mkv", stranger.read_text(encoding="utf-8"))
        self.assertEqual(f"{OLD}/movie/u/p1/43.mkv", others.read_text(encoding="utf-8"))
        self.assertEqual(before, current.stat().st_mtime_ns)

    def test_a_file_on_another_providers_host_with_the_same_login_is_left_alone(self):
        """A title a higher-priority provider took over whose file still plays
        the other provider: the sync heals it by stream id (#154), not this."""
        self.other.server_url = "http://backup.example"
        self.db.commit()
        film = self._movie(1, "Film (2020)", "http://backup.example/movie/u/p1/42.mkv")
        self._relink()
        self.assertEqual("http://backup.example/movie/u/p1/42.mkv", film.read_text(encoding="utf-8"))


class ARestartDoesNotLoseTheRewrite(_Base):
    """The save's background thread never ran (the process restarted): the
    rewrite it owed is still done, once."""

    def test_resume_pending_runs_what_the_save_started(self):
        import routers.providers as providers
        from services import provider_relink
        self.provider.server_url, self.provider.username, self.provider.password = OLD, "u", "p1"
        self.db.commit()
        film = self._movie(1, "Film (2020)", f"{OLD}/movie/u/p1/42.mkv")

        class _NeverStarted:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass
        with mock.patch.object(provider_relink.threading, "Thread", _NeverStarted):
            providers.update_provider(self.provider.id,
                                      providers.ProviderUpdate(server_url=NEW, username="u2", password="p2"),
                                      self.db)
        self.assertEqual(f"{OLD}/movie/u/p1/42.mkv", film.read_text(encoding="utf-8"))
        pending = json.loads(mdb.get_setting(self.db, "provider_relink_pending", "") or "{}")
        self.assertIn(str(self.provider.id), pending)

        with mock.patch("models.database.SessionLocal", self.Session), \
                mock.patch("services.sync.chown_path", lambda p: None):
            provider_relink.resume_pending()
        self.assertEqual(f"{NEW}/movie/u2/p2/42.mkv", film.read_text(encoding="utf-8"))
        self.db.expire_all()
        pending = json.loads(mdb.get_setting(self.db, "provider_relink_pending", "") or "{}")
        self.assertNotIn(str(self.provider.id), pending)


class TheSyncRepointsFirst(NightlyHarness):
    """Before its first request: a provider that refuses the whole sync still
    gets its films and episodes off the former host."""

    def test_files_on_the_former_host_follow_even_when_every_category_fails(self):
        self.add_category("1", "movie")
        self.add_category("2", "series")
        self.client.raise_for = {"1", "2"}
        root = Path(temp_dir(self))
        film = root / "Film (2020).strm"
        film.write_text("http://old-host.example/movie/u/p/42.mp4", encoding="utf-8")
        ep = root / "Show (2020)" / "Season 01" / "Show (2020) S01E01.strm"
        ep.parent.mkdir(parents=True)
        ep.write_text("http://old-host.example/series/u/p/700.mkv", encoding="utf-8")
        self.db.add_all([Movie(tmdb_id=1, title="Film", year="2020", source=f"provider_{self.provider.id}",
                               provider_id=self.provider.id, strm_path=str(film)),
                         Series(tmdb_id=2, title="Show", year="2020", source=f"provider_{self.provider.id}",
                                provider_id=self.provider.id, strm_path=str(ep.parent.parent))])
        self.db.commit()
        with mock.patch("services.sync.chown_path", lambda p: None):
            sync.sync_provider(self.provider, "full", self.db)
        self.assertEqual("http://provider/movie/u/p/42.mp4", film.read_text(encoding="utf-8"))
        self.assertEqual("http://provider/series/u/p/700.mkv", ep.read_text(encoding="utf-8"))


class AnM3uPlaylistThatMoved(NightlyHarness):
    """An M3U provider's playlist at a new address lists its titles on a new
    host. After a save of the new playlist address the next full sync points
    every listed film and episode at it, once; without a save nothing is
    rewritten (playlist tokens may rotate on every fetch)."""

    def setUp(self):
        super().setUp()
        import routers.providers as providers
        self.providers = providers
        sync.make_provider_client = self._saved["make_provider_client"]
        root = Path(temp_dir(self))
        self.playlist = root / "new.m3u"
        self.playlist.write_text("#EXTM3U\n"
                                 '#EXTINF:-1 group-title="Films",Film (2010)\n'
                                 "http://new-host.example/movie/u/p/42.mkv\n"
                                 '#EXTINF:-1 group-title="Shows",Show (2010) S01E01\n'
                                 "http://new-host.example/series/u/p/700.mkv\n", encoding="utf-8")
        self.provider.provider_type, self.provider.m3u_url = "m3u_file", str(root / "old.m3u")
        self.db.commit()
        self.add_category("Films", "movie")
        self.add_category("Shows", "series")
        FakeTMDB.ids = {"Film": 1, "Show": 2}
        self.film = self.vod / "movies" / "Film (2010)" / "Film (2010).strm"
        self.film.parent.mkdir(parents=True)
        self.film.write_text("http://old-host.example/movie/u/p/42.mkv", encoding="utf-8")
        show = self.vod / "shows" / "Show (2010)"
        self.ep = show / "Season 01" / "Show (2010) S01E01.strm"
        self.ep.parent.mkdir(parents=True)
        self.ep.write_text("http://old-host.example/series/u/p/700.mkv", encoding="utf-8")
        source = f"provider_{self.provider.id}"
        self.db.add_all([Movie(tmdb_id=1, title="Film", year="2010", source=source, provider_id=self.provider.id,
                               strm_path=str(self.film)),
                         Series(tmdb_id=2, title="Show", year="2010", source=source, provider_id=self.provider.id,
                                strm_path=str(show))])
        self.db.commit()

    def _sync(self):
        with mock.patch("services.sync.chown_path", lambda p: None):
            return sync.sync_provider(self.provider, "full", self.db)

    def test_a_saved_new_playlist_moves_every_listed_file(self):
        self.providers.update_provider(self.provider.id, self.providers.ProviderUpdate(m3u_url=str(self.playlist)),
                                       self.db)
        run = self._sync()
        self.assertEqual("completed", run.status, run.error_message)
        self.assertEqual("http://new-host.example/movie/u/p/42.mkv", self.film.read_text(encoding="utf-8"))
        self.assertEqual("http://new-host.example/series/u/p/700.mkv", self.ep.read_text(encoding="utf-8"))
        pending = json.loads(mdb.get_setting(self.db, "provider_relink_pending", "") or "{}")
        self.assertNotIn(str(self.provider.id), pending)

    def test_without_a_save_nothing_is_rewritten(self):
        self.provider.m3u_url = str(self.playlist)
        self.db.commit()
        self._sync()
        self.assertEqual("http://old-host.example/movie/u/p/42.mkv", self.film.read_text(encoding="utf-8"))
        self.assertEqual("http://old-host.example/series/u/p/700.mkv", self.ep.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
