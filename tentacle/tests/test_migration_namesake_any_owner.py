"""Provider Migrate (#460, #508): a film of the old provider stays with it
when another film anywhere in the library has the same title and year.

The new provider's sync files its one "Title (Year)" stream under one film.
When the new provider (or a download) already holds a namesake under another
TMDB id, a moved film is never listed by that sync, and its two-sync prune
deletes it: row, .strm and .nfo. Namesakes were counted among the old
provider's films only.

Run from tentacle/:  python tests/hermetic.py discover -s tests -p test_migration_namesake_any_owner.py
"""
import unittest

import services.migration as migration
import services.sync as sync
from models.database import DeletionLog, Movie, Provider, ProviderCategory
from nightly_harness import FakeClient, FakeTMDB, NightlyHarness


class BClient(FakeClient):
    def movie_stream_url(self, stream_id, ext):
        return f"http://b.example/movie/ub/pb/{stream_id}.{ext}"


class MigrateKeepsNamesakes(NightlyHarness):

    def setUp(self):
        super().setUp()
        db = self.db
        self.a = self.provider
        self.a.name, self.a.server_url, self.a.username, self.a.password = "A", "http://a.example", "ua", "pa"
        self.b = Provider(name="B", server_url="http://b.example", username="ub", password="pb", active=True)
        db.add(self.b)
        db.commit()
        self.clients = {self.a.id: FakeClient(), self.b.id: BClient()}
        sync.make_provider_client = lambda p: self.clients[p.id]   # NightlyHarness restores it
        db.add(ProviderCategory(provider_id=self.b.id, category_id="20", category_name="20", type="movie",
                                whitelisted=True, source_tag="T20"))
        db.commit()
        self.clients[self.b.id].movies["20"] = [("Heat", 5)]   # B lists "Heat (2010)"
        # A's "Heat (2010)" is TMDB 949 (an admin re-matched A's stream there)
        folder = self.vod / "movies" / "Heat (2010) [949]"
        folder.mkdir()
        self.strm = folder / "Heat (2010).strm"
        self.strm.write_text("http://a.example/movie/ua/pa/101.mp4", encoding="utf-8")
        db.add(Movie(tmdb_id=949, title="Heat", year="2010", source=f"provider_{self.a.id}",
                     provider_id=self.a.id, strm_path=str(self.strm)))
        db.commit()

    def _b_nights(self, n=2):
        for _ in range(n):
            run = sync.sync_provider(self.db.get(Provider, self.b.id), "full", self.db)
            self.assertEqual(run.status, "completed", run.error_message)
            sync.sweep_orphaned_vod_records(self.db)
            self.db.expire_all()

    def _assert_kept_by_a(self):
        heat = self.movie(949)
        self.assertIsNotNone(heat, [(d.kind, d.name, d.detail) for d in self.db.query(DeletionLog).all()])
        self.assertEqual(heat.provider_id, self.a.id)
        self.assertEqual(self.strm.read_text(encoding="utf-8"), "http://a.example/movie/ua/pa/101.mp4")

    def test_new_provider_already_has_a_namesake(self):
        FakeTMDB.ids["Heat"] = 1002            # B's sync files its "Heat" under 1002
        self._b_nights(1)
        self.assertIsNotNone(self.movie(1002))
        from routers import radarr
        out = radarr.run_migration(radarr.MigrateRequest(from_provider_id=self.a.id, to_provider_id=self.b.id),
                                   self.db)
        self.db.expire_all()
        self.assertEqual((out["movies_rewritten"], out["movies_skipped"]), (0, 1), out)
        self._b_nights(2)
        self._assert_kept_by_a()

    def test_a_downloaded_namesake(self):
        self.db.add(Movie(tmdb_id=1002, title="Heat", year="2010", source="radarr",
                          radarr_path="/movies/Heat (2010)/Heat.mkv"))
        self.db.commit()
        stats = migration.migrate_provider(self.a.id, self.b.id, self.db)
        self.db.expire_all()
        self.assertEqual((stats["movies_rewritten"], stats["movies_skipped"]), (0, 1), stats)
        self._assert_kept_by_a()

    def test_preview_counts_the_namesake_as_staying(self):
        FakeTMDB.ids["Heat"] = 1002
        self._b_nights(1)
        out = migration.preview_migration(self.db.get(Provider, self.a.id), self.db.get(Provider, self.b.id),
                                          self.db)
        self.assertEqual((out["movies_rewritten"], out["movies_skipped"]), (0, 1), out)

    def test_control_no_namesake_moves(self):
        FakeTMDB.ids["Heat"] = 949
        stats = migration.migrate_provider(self.a.id, self.b.id, self.db)
        self.db.expire_all()
        self.assertEqual(stats["movies_rewritten"], 1, stats)
        self.assertEqual(self.movie(949).provider_id, self.b.id)
        self._b_nights(2)
        self.assertIsNotNone(self.movie(949))
        self.assertEqual(self.strm.read_text(encoding="utf-8"), "http://b.example/movie/ub/pb/5.mp4")


if __name__ == "__main__":
    unittest.main()
