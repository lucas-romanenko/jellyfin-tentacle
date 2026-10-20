"""A provider's new address (or login) reaches its VOD .strm files (#142).

A direct .strm is {server}/movie|series/{user}/{pass}/{id}.{ext}, written once
by the sync. When the provider's link broke and the new one went into the
provider's settings, only the provider row changed: every film and episode
already in the library kept the old, dead host ("Fatal playback error"), and
the sync never repaired them, since _strm_needs_rewrite leaves a file on
another host alone. Live TV channels follow a save at once (#469); the
library now does too:

  * a save that changes the server or login rewrites the provider's own
    direct files from the old form to the new one
    (services.provider_relink.relink_strms; the save starts it in the
    background with start_relink);
  * the sync heals a file that plays this provider's stream with this
    provider's login on another host (the provider moved), so installs that
    changed the address before the fix are repaired by their next sync.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import types
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Provider, Series
from tmp_dirs import temp_dir

OLD = "http://old-panel.example:8080"
NEW = "http://new-panel.example"
SECRET = "k" * 64


class _Base(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.dir = Path(temp_dir(self))
        self.provider = Provider(name="P", provider_type="xtream", server_url=OLD, username="u", password="p1")
        self.other = Provider(name="Other", provider_type="xtream", server_url=OLD, username="u", password="p1")
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

    def _show(self, tmdb_id, name, episodes):
        show = self.dir / "Shows" / name
        files = []
        for fname, text in episodes:
            f = show / "Season 01" / fname
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
            files.append(f)
        self.db.add(Series(tmdb_id=tmdb_id, title=name, year="2020", source="provider",
                           provider_id=self.provider.id, strm_path=str(show)))
        self.db.commit()
        return files


class SaveRewritesTheLibrary(_Base):
    """relink_strms(db, provider, before): `before` is (server, user, pass)
    from before the save, as for rewrite_xtream_channel_urls."""

    def _save(self, **changes):
        from services.provider_relink import relink_strms
        before = (self.provider.server_url, self.provider.username, self.provider.password)
        for k, v in changes.items():
            setattr(self.provider, k, v)
        self.db.commit()
        with mock.patch("services.sync.chown_path", lambda p: None):
            return relink_strms(self.db, self.provider, before)

    def test_new_address_reaches_films_and_episodes(self):
        film = self._movie(1, "Film (2020)", f"{OLD}/movie/u/p1/42.mkv")
        eps = self._show(2, "Show (2020)", [("Show S01E01.strm", f"{OLD}/series/u/p1/700.mp4"),
                                            ("Show S01E02.strm", f"{OLD}/series/u/p1/701.mkv")])
        self._save(server_url=NEW + "/")
        self.assertEqual(f"{NEW}/movie/u/p1/42.mkv", film.read_text(encoding="utf-8"))
        self.assertEqual(f"{NEW}/series/u/p1/700.mp4", eps[0].read_text(encoding="utf-8"))
        self.assertEqual(f"{NEW}/series/u/p1/701.mkv", eps[1].read_text(encoding="utf-8"))

    def test_new_address_and_login_together(self):
        film = self._movie(1, "Film (2020)", f"{OLD}/movie/u/p1/42.mkv")
        self._save(server_url=NEW, username="u2", password="p#2")
        self.assertEqual(f"{NEW}/movie/u2/p%232/42.mkv", film.read_text(encoding="utf-8"))

    def test_only_this_providers_own_old_urls_change(self):
        """Another provider's rows (even on the same old panel), a Tentacle
        VOD link (it reads the provider's address when played), a hand-made
        file and a proxy are left alone."""
        from services import vod_tokens
        theirs = self._movie(1, "Theirs (2020)", f"{OLD}/movie/u/p1/42.mkv", provider=self.other)
        token = vod_tokens.Links("http://192.168.2.52:8888", SECRET, self.provider.id).movie(43, "mkv")
        via_tentacle = self._movie(2, "Routed (2020)", token)
        hand_made = self._movie(3, "Mine (2020)", "http://nas.local/films/mine.mkv")
        proxy = self._movie(4, "Proxied (2020)", "http://192.168.2.10:9000/play?url=" + f"{OLD}/movie/u/p1/44.mkv")
        self._save(server_url=NEW)
        self.assertEqual(f"{OLD}/movie/u/p1/42.mkv", theirs.read_text(encoding="utf-8"))
        self.assertEqual(token, via_tentacle.read_text(encoding="utf-8"))
        self.assertEqual("http://nas.local/films/mine.mkv", hand_made.read_text(encoding="utf-8"))
        self.assertIn("192.168.2.10:9000/play?url=", proxy.read_text(encoding="utf-8"))

    def test_counts_what_it_rewrote(self):
        self._movie(1, "Film (2020)", f"{OLD}/movie/u/p1/42.mkv")
        self._show(2, "Show (2020)", [("Show S01E01.strm", f"{OLD}/series/u/p1/700.mp4")])
        result = self._save(server_url=NEW)
        self.assertEqual({"movies": 1, "episodes": 1}, {k: result[k] for k in ("movies", "episodes")})

    def test_nothing_changed_nothing_rewritten(self):
        film = self._movie(1, "Film (2020)", f"{OLD}/movie/u/p1/42.mkv")
        before = film.stat().st_mtime_ns
        result = self._save(name="Renamed")
        self.assertEqual(0, result["movies"] + result["episodes"])
        self.assertEqual(before, film.stat().st_mtime_ns)

    def test_m3u_providers_are_left_alone(self):
        film = self._movie(1, "Film (2020)", f"{OLD}/movie/u/p1/42.mkv")
        self.provider.provider_type = "m3u_url"
        self.db.commit()
        self._save(server_url=NEW)
        self.assertEqual(f"{OLD}/movie/u/p1/42.mkv", film.read_text(encoding="utf-8"))


class TheProvidersPageStartsIt(_Base):
    def test_a_new_address_or_login_starts_the_rewrite(self):
        import routers.providers as providers
        with mock.patch("services.provider_relink.start_relink") as start:
            providers.update_provider(self.provider.id, providers.ProviderUpdate(server_url=NEW), self.db)
        start.assert_called_once_with(self.provider.id, (OLD, "u", "p1"))

    def test_other_edits_start_nothing(self):
        import routers.providers as providers
        with mock.patch("services.provider_relink.start_relink") as start:
            providers.update_provider(self.provider.id, providers.ProviderUpdate(name="Renamed", priority=3),
                                      self.db)
        start.assert_not_called()

class TheBackgroundRun(_Base):
    """start_relink's run: rewrite, then a Jellyfin scan (Jellyfin keeps the
    old URL until it sees the file changed) and an Activity entry that
    names neither the address nor the login."""

    def _run(self):
        from services import provider_relink
        Session = sessionmaker(bind=self.db.get_bind())
        scans = []

        class _Jellyfin:
            def __init__(self, *a):
                pass

            def trigger_library_scan(self, library_id=None):
                scans.append(library_id)
                return True
        mdb.set_setting(self.db, "jellyfin_url", "http://jellyfin.test")
        mdb.set_setting(self.db, "jellyfin_api_key", "x")
        before = (OLD, "u", "p1")
        self.provider.server_url = NEW
        self.db.commit()
        with mock.patch("models.database.SessionLocal", Session), \
                mock.patch("services.jellyfin.JellyfinService", _Jellyfin), \
                mock.patch("services.sync.chown_path", lambda p: None):
            provider_relink._run(self.provider.id, before)
        self.db.expire_all()
        return scans

    def test_rewrites_scans_and_says_so(self):
        film = self._movie(1, "Film (2020)", f"{OLD}/movie/u/p1/42.mkv")
        scans = self._run()
        self.assertEqual(f"{NEW}/movie/u/p1/42.mkv", film.read_text(encoding="utf-8"))
        self.assertEqual([None], scans)
        entry = self.db.query(mdb.ActivityLog).filter_by(event="provider_relinked").one()
        self.assertIn("1 films and 0 episodes", entry.message)
        self.assertNotIn("new-panel", entry.message)
        self.assertNotIn("p1", entry.message)

    def test_nothing_to_rewrite_no_scan_no_entry(self):
        self._movie(1, "Mine (2020)", "http://nas.local/films/mine.mkv")
        self.assertEqual([], self._run())
        self.assertEqual(0, self.db.query(mdb.ActivityLog).count())


class TheSyncHealsAFileOnTheProvidersFormerHost(unittest.TestCase):
    """For installs that saved the new address before the fix: the same
    account and the same stream on another host is this provider, moved."""

    def setUp(self):
        self.strm = Path(temp_dir(self)) / "Film (2020).strm"

    def _client(self, links=None):
        from services.sync import XtreamClient
        c = XtreamClient(types.SimpleNamespace(id=3, server_url=NEW, username="u", password="p1"))
        c.vod_links = links
        return c

    def test_same_login_same_stream_on_the_old_host_is_rewritten(self):
        from services.sync import _strm_needs_rewrite
        self.strm.write_text(f"{OLD}/movie/u/p1/42.mkv", encoding="utf-8")
        c = self._client()
        self.assertTrue(_strm_needs_rewrite(self.strm, c.movie_stream_url(42, "mkv"), c))

    def test_and_moved_to_tentacles_route_when_that_is_on(self):
        from services import vod_tokens
        from services.sync import _strm_needs_rewrite
        self.strm.write_text(f"{OLD}/movie/u/p1/42.mkv", encoding="utf-8")
        c = self._client(vod_tokens.Links("http://192.168.2.52:8888", SECRET, 3))
        self.assertTrue(_strm_needs_rewrite(self.strm, c.movie_stream_url(42, "mkv"), c))

    def test_another_stream_or_another_login_is_still_left_alone(self):
        from services.sync import _strm_needs_rewrite
        c = self._client()
        for text in (f"{OLD}/movie/u/p1/43.mkv",          # another stream: a second listing
                     f"{OLD}/movie/someone/else/42.mkv",  # another account
                     f"{OLD}/movie/u/p1/42.mkv?token=x"):  # not a plain provider URL
            self.strm.write_text(text, encoding="utf-8")
            self.assertFalse(_strm_needs_rewrite(self.strm, c.movie_stream_url(42, "mkv"), c), text)


if __name__ == "__main__":
    unittest.main()
