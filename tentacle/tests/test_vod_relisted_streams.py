"""A film's stream relabelled or re-listed by the provider (#262, #263).

#262: the provider changes a film's label (its year, or its name) but lists
the very stream the film's .strm plays. The new label found no TMDB match, so
the stream was skipped, the film was not "seen", and the two-strike prune
deleted it on the second sync. A stream never leaves the film whose .strm
already plays it (#185): the sync now places it there.

#263: the provider replaces a film's upload under a new stream id and drops
the old one. The .strm kept playing the old, dead id for good (the "never
flip between two listed ids" rule), and Stream health then offered to delete
the film. Once the old id is no longer listed anywhere in a complete fetch,
the .strm is rewritten in place to the film's current stream.

Built on the #185 harness, with a real XtreamClient subclass so the real
rewrite rules run. No network.
"""
import tempfile
import unittest
from pathlib import Path as _RealPath

import services.sync as sync
from models.database import Movie, Provider
from test_vod_namesakes import Base, TMDB, stream


class XClient(sync.XtreamClient):
    """The real Xtream client (URLs, rewrite rules) over a fixed catalogue."""

    def __init__(self, provider):
        super().__init__(provider)
        self.movies = {}
        self.raise_for = set()

    def get_vod_streams(self, cat):
        if cat in self.raise_for:
            raise RuntimeError("provider timeout")
        return [dict(s) for s in self.movies.get(cat, [])]

    def get_series_list(self, cat):
        return []

    def get_series_info(self, sid):
        return {"episodes": {}}


class YearStrictTMDB(TMDB):
    """Name search honours the year, like TMDB's year filter (and the #265
    retry only reaches one year either side)."""

    def search_movie(self, name, year=None, **k):
        TMDB.calls.append(("search", name))
        tid = TMDB.search.get(name)
        if not tid:
            return None
        title, fyear = TMDB.films[tid]
        if year and abs(int(year) - int(fyear)) > 1:
            return None
        return sync_meta(tid, title, fyear)


def sync_meta(tid, title, year):
    return {"tmdb_id": tid, "title": title, "year": year, "overview": "", "genres": [],
            "poster_path": None, "backdrop_path": None, "rating": None, "runtime": None}


FILLER = [(f"Filler {i}", 5000 + i) for i in range(40)]   # so the prune's 5% cap allows a removal


class Relisted(Base):
    def setUp(self):
        super().setUp()
        self.client = XClient(self.p)
        sync.make_provider_client = lambda p: self.client
        sync.TMDBService = YearStrictTMDB
        TMDB.films = {194: ("Amelie", "2001"), 603: ("The Matrix", "1999")}
        TMDB.search = {"Amelie": 194, "The Matrix": 603}
        for name, tid in FILLER:
            TMDB.films[tid] = (name, "2010")
            TMDB.search[name] = tid

    def catalogue(self, *streams):
        filler = [stream(f"{n} (2010)", tid, tid) for n, tid in FILLER]
        self.client.movies = {"a": list(streams) + filler}

    def strm(self, tid):
        rows = self.db.query(Movie).filter_by(tmdb_id=tid).all()
        return _RealPath(rows[0].strm_path) if rows else None


class RelabelledStreamKeepsItsFilm(Relisted):
    """#262"""

    def _relabel_twice(self, new_label, hint):
        self.catalogue(stream("Amelie (2001)", 103, 194))
        self.night()
        path = self.strm(194)
        self.assertTrue(path.exists())
        self.catalogue(stream(new_label, 103, hint))
        self.night()
        self.night()
        return path

    def test_a_year_change_with_the_same_stream_and_hint(self):
        path = self._relabel_twice("Amelie (2004)", 194)
        self.assertIsNotNone(self.strm(194), "a film still listed (same stream, same hint) was pruned")
        self.assertTrue(path.exists())
        self.assertIn("/103.mp4", path.read_text())

    def test_a_new_name_without_a_hint(self):
        path = self._relabel_twice("Le Fabuleux Destin d'Amelie Poulain (2001)", None)
        self.assertIsNotNone(self.strm(194), "a film still listed under a new name was pruned")
        self.assertTrue(path.exists())

    def test_it_counts_as_existing_and_keeps_its_tag(self):
        self.catalogue(stream("Amelie (2001)", 103, 194))
        self.night()
        self.catalogue(stream("Amelie (2004)", 103, 194))
        run = self.night()
        self.assertEqual(0, run.movies_skipped)
        self.assertIsNone(self.row(194).provider_missing_since)

    def test_a_stream_of_another_provider_with_the_same_number_is_not_ours(self):
        other = Provider(name="Other", server_url="http://other", username="x", password="y", active=True)
        self.db.add(other)
        self.db.commit()
        self.catalogue(stream("Amelie (2001)", 103, 194))
        self.night()
        # The film now plays another provider's stream 103 (a takeover by hand)
        path = self.strm(194)
        path.write_text("http://other/movie/x/y/103.mp4", encoding="utf-8")
        self.catalogue(stream("Something Else (2004)", 103))
        self.night()
        self.night()
        self.assertIsNone(self.strm(194), "another provider's stream kept our row")


class RelistedUnderANewId(Relisted):
    """#263"""

    def test_the_strm_follows_the_film_to_its_new_id(self):
        self.catalogue(stream("The Matrix (1999)", 102, 603))
        self.night()
        path = self.strm(603)
        self.assertTrue(path.read_text().endswith("/102.mp4"))
        self.catalogue(stream("The Matrix [4K] (1999)", 1102, 603))   # 102 no longer listed
        TMDB.search["The Matrix [4K]"] = 603
        self.night()
        self.assertEqual(path, self.strm(603), "the film moved: its Jellyfin item would be lost")
        self.assertTrue(path.read_text().endswith("/1102.mp4"), path.read_text())

    def test_same_label_new_id(self):
        self.catalogue(stream("The Matrix (1999)", 102, 603))
        self.night()
        self.catalogue(stream("The Matrix (1999)", 1102, 603))
        self.night()
        self.assertTrue(self.strm(603).read_text().endswith("/1102.mp4"))

    def test_while_both_ids_are_listed_nothing_flips(self):
        self.catalogue(stream("The Matrix (1999)", 102, 603), stream("The Matrix (1999)", 1102, 603))
        self.night()
        before = self.strm(603).read_text()
        self.catalogue(stream("The Matrix (1999)", 1102, 603), stream("The Matrix (1999)", 102, 603))
        self.night()
        self.assertEqual(before, self.strm(603).read_text())

    def test_an_incomplete_fetch_repoints_nothing(self):
        self.catalogue(stream("The Matrix (1999)", 102, 603))
        self.client.movies["b"] = [stream("Heat (1995)", 900)]
        self.night()
        self.catalogue(stream("The Matrix (1999)", 1102, 603))
        self.client.raise_for = {"b"}   # 102 might be in the category that failed
        run = sync.sync_provider(self.p, "full", self.db)
        self.db.expire_all()
        self.assertEqual("completed", run.status)
        self.assertTrue(self.strm(603).read_text().endswith("/102.mp4"))

    def test_an_opted_out_film_is_left_alone(self):
        self.catalogue(stream("The Matrix (1999)", 102, 603))
        self.night()
        row = self.row(603)
        row.strm_disabled = True
        self.db.commit()
        path = self.strm(603)
        path.write_text("http://provider/movie/u/p/102.mp4", encoding="utf-8")
        self.catalogue(stream("The Matrix (1999)", 1102, 603))
        self.night()
        self.assertTrue(path.read_text().endswith("/102.mp4"))

    def test_a_new_id_another_film_plays_is_not_taken(self):
        # Row A plays 102 (now gone); row B already plays 1102, and 1102 is listed
        # under A's label: 1102 stays B's (#185), A is not pointed at it.
        TMDB.films[777] = ("Other Film", "1999")
        TMDB.search["Other Film"] = 777
        self.catalogue(stream("The Matrix (1999)", 102, 603), stream("Other Film (1999)", 1102, 777))
        self.night()
        self.catalogue(stream("The Matrix (1999)", 1102))
        self.night()
        self.assertTrue(self.strm(603).read_text().endswith("/102.mp4"))
        self.assertTrue(self.strm(777).read_text().endswith("/1102.mp4"))


class EpisodeRelistedUnderANewId(unittest.TestCase):
    """#263, episodes: Pokemon S01E01 re-listed from episode id 3201 to 3299."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.show = _RealPath(d.name) / "Pokemon (1997)"
        self.client = sync.XtreamClient(Provider(id=1, name="P", server_url="http://provider",
                                                 username="u", password="p"))

    def write(self, *eps):
        return sync._write_episode_strms(
            self.client, {"1": [{"id": i, "episode_num": n, "container_extension": "mp4"} for i, n in eps]},
            self.show, self.show.name)

    def ep(self, n):
        return (self.show / "Season 01" / f"Pokemon (1997) S01E{n:02d}.strm").read_text()

    def test_a_delisted_id_is_replaced(self):
        self.write((3201, 1), (3202, 2))
        self.write((3299, 1), (3202, 2))
        self.assertTrue(self.ep(1).endswith("/3299.mp4"), self.ep(1))
        self.assertTrue(self.ep(2).endswith("/3202.mp4"))

    def test_while_both_are_listed_nothing_flips(self):
        self.write((3201, 1))
        self.write((3299, 1), (3201, 1))
        self.assertTrue(self.ep(1).endswith("/3201.mp4"))

    def test_another_hosts_link_is_left_alone(self):
        self.write((3201, 1))
        f = self.show / "Season 01" / "Pokemon (1997) S01E01.strm"
        f.write_text("http://elsewhere/series/a/b/3201.mp4", encoding="utf-8")
        self.write((3299, 1))
        self.assertEqual("http://elsewhere/series/a/b/3201.mp4", self.ep(1))


if __name__ == "__main__":
    unittest.main()
