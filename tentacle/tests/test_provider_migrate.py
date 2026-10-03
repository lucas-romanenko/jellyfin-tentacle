"""Provider Migrate (Settings -> Providers -> Migrate, POST /api/radarr/migration/run)
never hands the new provider a film it does not list.

Run from the tentacle/ directory:  python -m unittest discover -s tests

migrate_provider matched the old provider's films to the new provider's VOD list
by title and year and rewrote the matches' .strm. A bulk UPDATE after the loop
then gave EVERY other film of the old provider to the new one as well, its .strm
still playing the old provider, and switched the old provider off. The new
provider's next two syncs did not list those films, so its prune
(_prune_removed_content) marked and then deleted them: row, .strm and .nfo.

Now only a film the new provider lists, in a movie category its sync reads,
on a stream no admin blocked or re-matched ("Wrong movie"), moves; everything
else (and every series) stays with the old provider, files untouched. No network: requests.Session.get is mocked; files live in a temp dir.
"""
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import BlockedStream, MatchOverride, Movie, Series, Provider, ProviderCategory
import services.migration as migration
import services.sync as sync
from nightly_harness import NightlyHarness, FakeClient, FakeTMDB
from tmp_dirs import temp_dir

# What the new provider (B) lists. category_id comes as an int from some panels.
B_VOD = [
    {"stream_id": 5, "name": "Heat (1995)", "container_extension": "mkv", "category_id": 20},
    # Listed, but in a category B's sync does not read (not whitelisted).
    {"stream_id": 6, "name": "Collateral (2004)", "container_extension": "mkv", "category_id": "99"},
]


class _Resp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def json(self):
        return self._data


class MigrateLeavesUnlistedFilms(unittest.TestCase):

    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(engine.dispose)
        self.addCleanup(self.db.close)

        self.a = Provider(name="A", server_url="http://a.example", username="ua",
                          password="pa", active=True)
        self.b = Provider(name="B", server_url="http://b.example", username="ub",
                          password="pb", active=True)
        self.db.add_all([self.a, self.b])
        self.db.commit()
        for cid, type_, on in (("20", "movie", True), ("99", "movie", False), ("21", "series", True)):
            self.db.add(ProviderCategory(provider_id=self.b.id, category_id=cid, category_name=f"B{cid}",
                                         type=type_, whitelisted=on))
        self.db.commit()

        self.movies = Path(tmp) / "vod" / "movies"
        self.requests = []
        self.heat_strm = self.film("Heat", "1995", 949, 101)
        self.ronin_strm = self.film("Ronin", "1998", 8195, 102)
        self.collateral_strm = self.film("Collateral", "2004", 1538, 103)

        show_dir = Path(tmp) / "vod" / "shows" / "The Wire (2002)"
        (show_dir / "Season 01").mkdir(parents=True)
        self.ep_strm = show_dir / "Season 01" / "The Wire S01E01.strm"
        self.ep_strm.write_text("http://a.example/series/ua/pa/9001.mp4", encoding="utf-8")
        self.db.add(Series(tmdb_id=1438, title="The Wire", year="2002",
                           source=f"provider_{self.a.id}", provider_id=self.a.id,
                           strm_path=str(show_dir)))
        self.db.commit()

    def film(self, title, year, tmdb_id, stream_id, folder=None):
        folder = self.movies / (folder or f"{title} ({year})")
        folder.mkdir(parents=True)
        strm = folder / f"{folder.name}.strm"
        strm.write_text(f"http://a.example/movie/ua/pa/{stream_id}.mp4", encoding="utf-8")
        (folder / f"{folder.name}.nfo").write_text("<movie/>", encoding="utf-8")
        self.db.add(Movie(tmdb_id=tmdb_id, title=title, year=year,
                          source=f"provider_{self.a.id}", provider_id=self.a.id,
                          strm_path=str(strm)))
        self.db.commit()
        return strm

    def _fake_get(self, session, url, *args, **kwargs):
        self.requests.append(url)
        if not url.startswith("http://b.example/"):
            raise AssertionError("Migrate called a provider other than the new one")
        if "action=get_vod_streams" in url:
            return _Resp(B_VOD)
        return _Resp([])

    def _migrate(self, **kwargs):
        def fake_get(session, url, *args, **kw):
            return self._fake_get(session, url, *args, **kw)
        with mock.patch("requests.Session.get", fake_get):
            stats = migration.migrate_provider(self.a.id, self.b.id, self.db, **kwargs)
        self.db.expire_all()
        return stats

    def _movie(self, tmdb_id):
        return self.db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()

    def _assert_left_with_a(self, tmdb_id, strm, stream_id, why):
        row = self._movie(tmdb_id)
        self.assertIsNotNone(row, why)
        self.assertEqual((row.provider_id, row.source), (self.a.id, f"provider_{self.a.id}"),
                         f"{why}: the row was given to B, but its .strm still plays A")
        self.assertEqual(strm.read_text(encoding="utf-8"),
                         f"http://a.example/movie/ua/pa/{stream_id}.mp4", why)

    def test_a_listed_film_moves_and_plays_the_new_provider(self):
        stats = self._migrate()
        self.assertEqual(stats["movies_rewritten"], 1)
        self.assertEqual(self.heat_strm.read_text(encoding="utf-8"), "http://b.example/movie/ub/pb/5.mkv")
        heat = self._movie(949)
        self.assertEqual((heat.provider_id, heat.source), (self.b.id, f"provider_{self.b.id}"))
        self.assertFalse(self.db.get(Provider, self.a.id).active)
        self.assertTrue(self.db.get(Provider, self.b.id).active)

    def test_a_film_the_new_provider_does_not_list_stays_with_the_old_one(self):
        stats = self._migrate()
        self.assertEqual(stats["movies_not_found"], 2)
        self._assert_left_with_a(8195, self.ronin_strm, 102, "Ronin (not listed by B)")

    def test_a_film_listed_only_in_a_category_the_new_provider_does_not_sync_stays(self):
        self._migrate()
        self._assert_left_with_a(1538, self.collateral_strm, 103,
                                 "Collateral (listed by B only in a category B does not sync)")

    def test_the_new_providers_next_two_syncs_delete_nothing_migrate_left(self):
        self._migrate()
        b = self.db.get(Provider, self.b.id)
        for _ in range(2):  # B's next two nightly syncs list only Heat
            sync._prune_removed_content(self.db, b, "movie", {949})
            self.db.expire_all()
        self.assertIsNotNone(self._movie(949))
        for tmdb_id, strm in ((8195, self.ronin_strm), (1538, self.collateral_strm)):
            self.assertIsNotNone(self._movie(tmdb_id), f"B's prune deleted tmdb:{tmdb_id}")
            self.assertTrue(strm.exists() and strm.with_suffix(".nfo").exists(),
                            f"B's prune deleted the files of tmdb:{tmdb_id}")

    def test_namesakes_stay_with_the_old_provider(self):
        # Two films named "Heat (1995)": one name can't tell which one B lists,
        # and B's sync prunes the one it doesn't.
        other = self.film("Heat", "1995", 1001, 104, folder="Heat (1995) [tmdbid-1001]")
        self._migrate()
        self._assert_left_with_a(949, self.heat_strm, 101, "Heat (namesake)")
        self._assert_left_with_a(1001, other, 104, "the other Heat (namesake)")

    def test_a_namesake_of_a_downloaded_film_stays(self):
        self.db.add(Movie(tmdb_id=1002, title="Heat", year="1995", source="radarr",
                          radarr_path="/movies/Heat (1995)/Heat.mkv"))
        self.db.commit()
        self._migrate()
        self._assert_left_with_a(949, self.heat_strm, 101, "Heat (a downloaded namesake exists)")

    def test_a_failed_rewrite_leaves_the_film_with_the_old_provider(self):
        # Disk full: a write in Heat's folder empties the file it opens
        # (O_TRUNC), then fails. Migrate switches A off, so A's sync would
        # never refill an emptied .strm (#283): the old one must stay whole.
        real_write = Path.write_text
        folder = sorted(p.name for p in self.heat_strm.parent.iterdir())

        def write_text(path, *args, **kwargs):
            if Path(path).parent == self.heat_strm.parent:
                with open(path, "w", encoding="utf-8"):
                    pass
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", write_text), \
                self.assertLogs("services.migration", "ERROR") as logs:
            stats = self._migrate()
        self.assertEqual(stats["errors"], 1)
        self.assertIn("disk full", logs.output[0])
        self._assert_left_with_a(949, self.heat_strm, 101, "Heat (its .strm could not be rewritten)")
        self.assertEqual(sorted(p.name for p in self.heat_strm.parent.iterdir()), folder,
                         "a temp file was left behind")

    def test_series_and_every_file_are_left_alone(self):
        files = sorted(p for p in self.movies.rglob("*") if p.is_file())
        stats = self._migrate()
        self.assertEqual(stats["series_rewritten"], 0)
        wire = self.db.query(Series).filter(Series.tmdb_id == 1438).one()
        self.assertEqual(wire.provider_id, self.a.id)
        self.assertEqual(self.ep_strm.read_text(encoding="utf-8"), "http://a.example/series/ua/pa/9001.mp4")
        self.assertEqual(files, sorted(p for p in self.movies.rglob("*") if p.is_file()))
        self.assertEqual(self.db.query(Movie).count(), 3)

    def test_no_synced_movie_category_refuses_and_changes_nothing(self):
        self.db.query(ProviderCategory).filter(ProviderCategory.category_id == "20").update(
            {"whitelisted": False})
        self.db.commit()
        stats = self._migrate()
        self.assertIn("movie categories", stats.get("error", ""))
        self.assertEqual(self.requests, [], "nothing can move, so B is not asked")
        self.assertTrue(self.db.get(Provider, self.a.id).active, "A must stay on")
        self._assert_left_with_a(949, self.heat_strm, 101, "Heat (B syncs no movie category)")

    def test_dry_run_changes_nothing(self):
        stats = self._migrate(dry_run=True)
        self.assertEqual(stats["movies_rewritten"], 1)
        for tmdb_id, strm, sid in ((949, self.heat_strm, 101), (8195, self.ronin_strm, 102)):
            self._assert_left_with_a(tmdb_id, strm, sid, "dry run")
        self.assertTrue(self.db.get(Provider, self.a.id).active)


class AClient(FakeClient):
    def movie_stream_url(self, stream_id, ext):
        return f"http://a.example/movie/ua/pa/{stream_id}.{ext}"

    def episode_stream_url(self, ep_id, ext):
        return f"http://a.example/series/ua/pa/{ep_id}.{ext}"


class BClient(FakeClient):
    def movie_stream_url(self, stream_id, ext):
        return f"http://b.example/movie/ub/pb/{stream_id}.{ext}"

    def episode_stream_url(self, ep_id, ext):
        return f"http://b.example/series/ub/pb/{ep_id}.{ext}"


class DetailsTMDB(FakeTMDB):
    """FakeTMDB, plus the details of the film an admin re-matched a stream to."""

    def get_movie_details(self, tmdb_id):
        if tmdb_id != 7777:
            return None
        return {"tmdb_id": 7777, "title": "The Decline", "year": "2020", "overview": "", "genres": [],
                "poster_path": None, "backdrop_path": None, "rating": None, "runtime": None}


class MigrateThenTheNewProvidersNights(NightlyHarness):
    """End to end: a real sync of A, the real route, then real nights of B."""

    def test_unlisted_film_survives_and_the_rest_moves_once_a_ranks_lower(self):
        db = self.db
        a = self.provider
        a.name, a.server_url, a.username, a.password = "A", "http://a.example", "ua", "pa"
        b = Provider(name="B", server_url="http://b.example", username="ub", password="pb", active=True)
        db.add(b)
        db.commit()
        clients = {a.id: AClient(), b.id: BClient()}
        sync.make_provider_client = lambda p: clients[p.id]   # NightlyHarness restores it
        for provider, cats in ((a, ("10", "11")), (b, ("20", "21"))):
            for cid, type_ in zip(cats, ("movie", "series")):
                db.add(ProviderCategory(provider_id=provider.id, category_id=cid, category_name=cid,
                                        type=type_, whitelisted=True, source_tag=f"T{cid}"))
        db.commit()
        FakeTMDB.ids.update({"Heat": 949, "Ronin": 8195, "The Wire": 1438})
        clients[a.id].movies["10"] = [("Heat", 101), ("Ronin", 102)]
        clients[a.id].series["11"] = [("The Wire", 301)]
        self.assertEqual(sync.sync_provider(a, "full", db).status, "completed")
        db.expire_all()
        ronin_strm = Path(self.movie(8195).strm_path)
        heat_strm = Path(self.movie(949).strm_path)

        # B lists Heat and The Wire, not Ronin.
        clients[b.id].movies["20"] = [("Heat", 5)]
        clients[b.id].series["21"] = [("The Wire", 77)]
        listing = [dict(s, category_id="20") for s in clients[b.id].get_vod_streams("20")]
        from routers import radarr
        with mock.patch("requests.Session.get", return_value=_Resp(listing)):
            out = radarr.run_migration(radarr.MigrateRequest(from_provider_id=a.id, to_provider_id=b.id), db)
        self.assertEqual((out["movies_rewritten"], out["movies_not_found"]), (1, 1))
        db.expire_all()
        self.assertFalse(db.get(Provider, a.id).active)

        for _ in range(2):  # B's next two nights
            run = sync.sync_provider(db.get(Provider, b.id), "full", db)
            self.assertEqual(run.status, "completed", run.error_message)
            sync.sweep_orphaned_vod_records(db)
            db.expire_all()
        ronin = self.movie(8195)
        self.assertIsNotNone(ronin, "B's prune deleted Ronin, which B never listed")
        self.assertEqual(ronin.provider_id, a.id)
        self.assertEqual(ronin_strm.read_text(encoding="utf-8"), "http://a.example/movie/ua/pa/102.mp4")
        self.assertEqual(self.movie(949).provider_id, b.id)
        self.assertEqual(heat_strm.read_text(encoding="utf-8"), "http://b.example/movie/ub/pb/5.mp4")
        self.assertEqual(self.series_row(1438).provider_id, a.id, "series stay with A")

        # What the docs say moves the rest: rank A below B. B's next sync takes
        # over every title it lists (#154); Ronin, which B doesn't list, stays.
        db.get(Provider, a.id).priority = 2
        db.commit()
        self.assertEqual(sync.sync_provider(db.get(Provider, b.id), "full", db).status, "completed")
        db.expire_all()
        wire = self.series_row(1438)
        self.assertEqual(wire.provider_id, b.id)
        episodes = [p.read_text(encoding="utf-8") for p in Path(wire.strm_path).rglob("*.strm")]
        self.assertTrue(episodes and all(e.startswith("http://b.example/") for e in episodes), episodes)
        self.assertEqual(self.movie(8195).provider_id, a.id)
        self.assertTrue(ronin_strm.exists())

    # An admin fixed B's "Heat" stream with "Wrong movie" before Migrate: B's
    # sync skips a blocked stream and files a re-matched one under the film it
    # really is. A film moved onto it is never seen by B, so B's prune deletes it.

    def _a_synced_and_b_lists_heat(self):
        db = self.db
        a = self.provider
        a.name, a.server_url, a.username, a.password = "A", "http://a.example", "ua", "pa"
        b = Provider(name="B", server_url="http://b.example", username="ub", password="pb", active=True)
        db.add(b)
        db.commit()
        self.a_id, self.b_id = a.id, b.id
        clients = {a.id: AClient(), b.id: BClient()}
        sync.make_provider_client = lambda p: clients[p.id]   # NightlyHarness restores it
        for provider, cid in ((a, "10"), (b, "20")):
            db.add(ProviderCategory(provider_id=provider.id, category_id=cid, category_name=cid,
                                    type="movie", whitelisted=True, source_tag=f"T{cid}"))
        db.commit()
        FakeTMDB.ids.update({"Heat": 949, "Ronin": 8195, "Collateral": 1538})
        clients[a.id].movies["10"] = [("Heat", 101), ("Ronin", 102)]
        self.assertEqual(sync.sync_provider(a, "full", db).status, "completed")
        db.expire_all()
        self.heat_strm = Path(self.movie(949).strm_path)
        clients[b.id].movies["20"] = [("Heat", 5), ("Collateral", 6)]
        self.b_listing = [dict(s, category_id="20") for s in clients[b.id].get_vod_streams("20")]

    def _migrate_then_two_b_nights(self):
        with mock.patch("requests.Session.get", return_value=_Resp(self.b_listing)):
            stats = migration.migrate_provider(self.a_id, self.b_id, self.db)
        self.db.expire_all()
        for _ in range(2):
            run = sync.sync_provider(self.db.get(Provider, self.b_id), "full", self.db)
            self.assertEqual(run.status, "completed", run.error_message)
            sync.sweep_orphaned_vod_records(self.db)
            self.db.expire_all()
        return stats

    def _assert_heat_stayed_with_a(self, stats, why):
        heat = self.movie(949)
        self.assertIsNotNone(heat, f"B's prune deleted Heat, which Migrate moved onto {why}")
        self.assertEqual(heat.provider_id, self.a_id, why)
        self.assertEqual(self.heat_strm.read_text(encoding="utf-8"), "http://a.example/movie/ua/pa/101.mp4", why)
        self.assertEqual((stats["movies_rewritten"], stats["movies_not_found"]), (0, 2), why)

    def test_a_blocked_stream_on_the_new_provider_is_not_used(self):
        self._a_synced_and_b_lists_heat()
        self.db.add(BlockedStream(provider_id=self.b_id, media_type="movie", stream_key="5",
                                  tmdb_id=949, title="Heat", reason="wrong movie"))
        self.db.commit()
        stats = self._migrate_then_two_b_nights()
        self._assert_heat_stayed_with_a(stats, "B's blocked stream")

    def test_a_re_matched_stream_on_the_new_provider_is_not_used(self):
        self._a_synced_and_b_lists_heat()
        sync.TMDBService = DetailsTMDB   # NightlyHarness restores it
        self.db.add(MatchOverride(provider_id=self.b_id, media_type="movie", stream_key="5",
                                  tmdb_id=7777, previous_tmdb_id=949, title="The Decline"))
        self.db.commit()
        stats = self._migrate_then_two_b_nights()
        self._assert_heat_stayed_with_a(stats, "a stream B's sync files under another film")
        self.assertIsNotNone(self.movie(7777), "B's sync files its stream under the film it really is")


if __name__ == "__main__":
    unittest.main()
