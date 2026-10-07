"""Provider Migrate moves only the films the new provider's sync would find (#460).

It handed the new provider every film of the old one, also the ones the
new provider doesn't list (their .strm still playing the old provider);
the new provider's next two syncs then deleted them. Only a film listed in
the new provider's chosen categories, on a stream its sync uses for that
film, moves; the rest stays with the old provider, files untouched.
"""
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import BlockedStream, MatchOverride, Movie, Provider, ProviderCategory, Series
import services.migration as migration
import services.sync as sync
from tmp_dirs import temp_dir


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _stream(sid, name, cat):
    return {"stream_id": sid, "name": name, "container_extension": "mkv", "category_id": cat}


class MigrateMovesOnlyListedFilms(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(engine.dispose)
        self.addCleanup(self.db.close)
        self.a = Provider(name="A", server_url="http://a.example", username="ua", password="pa", active=True)
        self.b = Provider(name="B", server_url="http://b.example", username="ub", password="pb", active=True)
        self.db.add_all([self.a, self.b])
        self.db.commit()
        self.strm = {}
        # B's listing per category; category 30 is not chosen.
        self.listing = {"20": [_stream(5, "Heat (1995)", "20")], "30": []}

    def film(self, title, year, tmdb_id, sid):
        folder = Path(self.tmp) / "movies" / f"{title} ({year}) [{tmdb_id}]"
        folder.mkdir(parents=True)
        strm = folder / f"{title} ({year}).strm"
        strm.write_text(f"http://a.example/movie/ua/pa/{sid}.mp4", encoding="utf-8")
        self.strm[tmdb_id] = strm
        self.db.add(Movie(tmdb_id=tmdb_id, title=title, year=year, source=f"provider_{self.a.id}",
                          provider_id=self.a.id, strm_path=str(strm)))
        self.db.commit()

    def choose(self, *cats):
        for cat in cats:
            self.db.add(ProviderCategory(provider_id=self.b.id, category_id=cat, category_name=f"B {cat}",
                                         type="movie", whitelisted=True))
        self.db.commit()

    def _get(self, url, *args, **kwargs):
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q.get("action"), ["get_vod_streams"], url)
        cat = q.get("category_id", [None])[0]
        if cat is None:   # the panel lists every category's streams
            return _Resp([s for streams in self.listing.values() for s in streams])
        return _Resp(self.listing.get(cat, []))

    def migrate(self, dry_run=False):
        with mock.patch("requests.Session.get", side_effect=self._get, autospec=False):
            stats = migration.migrate_provider(self.a.id, self.b.id, self.db, dry_run=dry_run)
        self.db.expire_all()
        return stats

    def row(self, tmdb_id):
        return self.db.query(Movie).filter(Movie.tmdb_id == tmdb_id).one()

    def assert_stays_with_a(self, tmdb_id, sid):
        row = self.row(tmdb_id)
        self.assertEqual(row.provider_id, self.a.id)
        self.assertEqual(row.source, f"provider_{self.a.id}")
        self.assertEqual(self.strm[tmdb_id].read_text(), f"http://a.example/movie/ua/pa/{sid}.mp4")

    def assert_moved_to_b(self, tmdb_id, sid):
        row = self.row(tmdb_id)
        self.assertEqual(row.provider_id, self.b.id)
        self.assertEqual(row.source, f"provider_{self.b.id}")
        self.assertEqual(self.strm[tmdb_id].read_text(), f"http://b.example/movie/ub/pb/{sid}.mkv")

    def test_a_film_the_new_provider_does_not_list_stays_and_survives_its_syncs(self):
        self.choose("20")
        self.film("Heat", "1995", 949, 101)
        self.film("Ronin", "1998", 8195, 102)
        stats = self.migrate()
        self.assertEqual((stats["movies_rewritten"], stats["movies_not_found"]), (1, 1))
        self.assert_moved_to_b(949, 5)
        self.assert_stays_with_a(8195, 102)
        b = self.db.get(Provider, self.b.id)
        for _ in range(2):   # B's next two syncs list Heat only
            sync._prune_removed_content(self.db, b, "movie", {949})
            self.db.expire_all()
        self.assertTrue(self.strm[8195].exists(), "Ronin's .strm was deleted")
        self.assert_stays_with_a(8195, 102)
        self.assertTrue(self.strm[949].exists())

    def test_a_film_listed_only_in_a_category_not_chosen_stays(self):
        self.choose("20")
        self.listing["30"] = [_stream(6, "Ronin (1998)", "30")]
        self.film("Ronin", "1998", 8195, 102)
        stats = self.migrate()
        self.assertEqual(stats["movies_rewritten"], 0)
        self.assert_stays_with_a(8195, 102)

    def test_no_chosen_movie_category_refuses_and_changes_nothing(self):
        self.film("Heat", "1995", 949, 101)
        stats = self.migrate()
        self.assertIn("error", stats)
        self.assert_stays_with_a(949, 101)
        self.assertTrue(self.db.get(Provider, self.a.id).active)

    def test_a_blocked_stream_is_not_used(self):
        self.choose("20")
        self.db.add(BlockedStream(provider_id=self.b.id, media_type="movie", stream_key="5", tmdb_id=949))
        self.db.commit()
        self.film("Heat", "1995", 949, 101)
        self.migrate()
        self.assert_stays_with_a(949, 101)

    def test_a_rematched_stream_goes_to_the_film_it_was_matched_to(self):
        self.choose("20")
        # Stream 5 is labelled Heat but an admin said it is really Ronin
        self.db.add(MatchOverride(provider_id=self.b.id, media_type="movie", stream_key="5", tmdb_id=8195))
        self.db.commit()
        self.film("Heat", "1995", 949, 101)
        self.film("Ronin", "1998", 8195, 102)
        self.migrate()
        self.assert_stays_with_a(949, 101)
        self.assert_moved_to_b(8195, 5)

    def test_namesakes_both_stay(self):
        self.choose("20")
        self.listing["20"] = [_stream(8, "The Thing (1982)", "20")]
        self.film("The Thing", "1982", 1091, 103)
        self.film("The Thing", "1982", 999001, 104)
        stats = self.migrate()
        self.assertEqual(stats["movies_skipped"], 2)
        self.assert_stays_with_a(1091, 103)
        self.assert_stays_with_a(999001, 104)

    def test_the_old_providers_missing_mark_does_not_follow_the_film(self):
        self.choose("20")
        self.film("Heat", "1995", 949, 101)
        from datetime import datetime
        row = self.row(949)
        row.provider_missing_since = datetime(2026, 1, 1)
        self.db.commit()
        self.migrate()
        self.assertIsNone(self.row(949).provider_missing_since)
        sync._prune_removed_content(self.db, self.db.get(Provider, self.b.id), "movie", set())
        self.db.expire_all()
        self.assertIsNotNone(self.db.query(Movie).filter(Movie.tmdb_id == 949).first(),
                             "one missed sync deleted a film that just moved")

    def test_series_stay_with_the_old_provider_and_are_counted(self):
        self.choose("20")
        self.db.add(Series(tmdb_id=1438, title="The Wire", year="2002", source=f"provider_{self.a.id}",
                           provider_id=self.a.id))
        self.db.commit()
        stats = self.migrate()
        self.assertEqual(stats["series_kept"], 1)
        self.assertEqual(self.db.query(Series).one().provider_id, self.a.id)

    def test_dry_run_changes_nothing(self):
        self.choose("20")
        self.film("Heat", "1995", 949, 101)
        stats = self.migrate(dry_run=True)
        self.assertEqual(stats["movies_rewritten"], 1)
        self.assert_stays_with_a(949, 101)
        self.assertTrue(self.db.get(Provider, self.a.id).active)


class MigrateDialogWording(unittest.TestCase):
    def test_the_dialog_no_longer_promises_a_flag(self):
        html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        start = html.index('id="modal-migrate"')
        dialog = html[start:html.index("<!-- Add List Modal -->", start)]
        self.assertNotIn("will be flagged", dialog)
        self.assertIn("stays with the old provider", dialog)


if __name__ == "__main__":
    unittest.main()
