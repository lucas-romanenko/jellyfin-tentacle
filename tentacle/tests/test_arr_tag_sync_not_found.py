"""The Radarr/Sonarr scan counts titles Jellyfin does not have apart from failed tag writes.

Run from the tentacle/ directory:  python -m unittest discover -s tests

After every Radarr/Sonarr scan, a pass looks each tagged title up in one
Jellyfin listing and pushes the tags its item is missing. A title the listing
did not have (Jellyfin has not scanned it yet, or the listing itself came back
empty) was counted as "failed", the same as a tag write Jellyfin refused. On a
live install a movie listing that timed out was logged as
"Jellyfin tag sync: 0 pushed, 17421 failed" although nothing had failed, and a
real failure could not be told apart from a title Jellyfin has not scanned.

The fake below runs the real listing and lookup code over a fake /Items
answer; only the HTTP layer and the tag writes are faked.
"""
import copy
import os
import random
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from hermetic import no_tmdb
from services.jellyfin import JellyfinService as _RealJellyfin
from tmp_dirs import temp_dir

SEEDS = int(os.environ.get("SEEDS", "1000"))


class FakeJellyfin(_RealJellyfin):
    listing = {"Movie": [], "Series": []}
    listing_down = False     # /Items answers nothing, as _get does on a timeout
    refuse = set()           # item ids whose tag write fails
    writes = []

    def __init__(self, *a, **k):
        self.url, self.api_key, self.user_id = "http://jellyfin.test", "k", ""

    def _get(self, path, params=None):
        if path != "/Items" or type(self).listing_down:
            return None
        items = [copy.deepcopy(i) for i in type(self).listing[params["IncludeItemTypes"]]]
        start = int(params.get("StartIndex", 0))
        return {"Items": items[start:start + int(params["Limit"])], "TotalRecordCount": len(items)}

    def trigger_library_scan(self):
        return True

    def set_item_tags(self, item_id, tags):
        type(self).writes.append(item_id)
        return item_id not in type(self).refuse

    def refresh_item_metadata(self, item_id, replace_all=False):
        return True


def _item(item_id, title, year, tmdb_id, tags):
    it = {"Id": item_id, "Name": title, "ProviderIds": {}, "Tags": list(tags),
          "ImageTags": {"Primary": "p"}}
    if year:
        it["ProductionYear"] = int(year)
    if tmdb_id:
        it["ProviderIds"]["Tmdb"] = str(tmdb_id)
    return it


class _Cases:
    """The same cases for the Radarr and the Sonarr scan (subclasses below)."""
    N = 4               # downloaded titles in the *arr
    TYPE = MODEL = LOGGER = None

    def setUp(self):
        no_tmdb(self)
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"),
                     ("data_dir", self.tmp)):
            mdb.set_setting(self.db, k, v)
        # A provider title with a tag of its own, and one with none (skipped).
        self.db.add(self.MODEL(tmdb_id=9001, title="Vod One", year="1999", source="provider_1",
                               tags=["Netflix Movies" if self.TYPE == "Movie" else "Netflix Series"]))
        self.db.add(self.MODEL(tmdb_id=9002, title="Vod Two", year="1998", source="provider_1", tags=[]))
        self.db.commit()
        # First scan without Jellyfin: creates the rows and their tags.
        self._scan()
        for k, v in (("jellyfin_url", "http://jellyfin.test"), ("jellyfin_api_key", "k")):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        self.rows = {r.tmdb_id: (r.title, r.year, list(r.tags or []))
                     for r in self.db.query(self.MODEL).all()}
        self.tagged = sorted(t for t, (_, _, tags) in self.rows.items() if tags)
        self.assertEqual(self.N + 1, len(self.tagged))
        FakeJellyfin.listing = {"Movie": [], "Series": []}
        FakeJellyfin.listing_down = False
        FakeJellyfin.refuse = set()
        FakeJellyfin.writes = []

    def _list(self, items):
        FakeJellyfin.listing = {"Movie": [], "Series": []}
        FakeJellyfin.listing[self.TYPE] = items

    def _found(self, tmdb_id, tags=None, how="tmdb"):
        """The Jellyfin item of a row, found by TMDB id, by title + year, or by
        title only; it carries the row's tags unless `tags` says otherwise."""
        title, year, desired = self.rows[tmdb_id]
        return _item(f"i{tmdb_id}", title, None if how == "title" else year,
                     tmdb_id if how == "tmdb" else None, desired if tags is None else tags)

    def _run(self):
        # All loggers: the listing's own warning stays out of the test output.
        with mock.patch("services.jellyfin.JellyfinService", FakeJellyfin), \
                self.assertLogs(level="INFO") as logs:
            stats = self._scan()
        mine = [r for r in logs.records if r.name == self.LOGGER]
        line = [r.getMessage() for r in mine
                if r.levelname == "INFO" and "Jellyfin tag sync" in r.getMessage()]
        self.assertEqual(1, len(line), logs.output)
        warnings = [r.getMessage() for r in mine if r.levelname == "WARNING"]
        return stats, line[0], warnings

    # --- one of each outcome -------------------------------------------------

    def test_title_jellyfin_does_not_have_is_not_a_failure(self):
        t = self.tagged
        self._list([
            self._found(t[0], tags=[]),     # missing its tags: pushed
            self._found(t[1], tags=[]),     # missing its tags, write refused: failed
            self._found(t[2]),              # already right: nothing to write
        ])                                  # t[3] and t[4] are not in Jellyfin
        FakeJellyfin.refuse = {f"i{t[1]}"}
        stats, line, warnings = self._run()
        self.assertEqual([f"i{t[0]}", f"i{t[1]}"], FakeJellyfin.writes)
        self.assertEqual(1, stats["jf_tags_pushed"])
        self.assertEqual(1, stats["jf_tags_failed"])
        self.assertEqual(2, stats["jf_tags_not_found"])
        self.assertIn("1 pushed, 1 failed, 2 not in Jellyfin yet", line)
        self.assertFalse([w for w in warnings if "listed no" in w], warnings)

    def test_empty_listing_is_reported_and_counts_no_failures(self):
        FakeJellyfin.listing_down = True
        stats, line, warnings = self._run()
        n = len(self.tagged)
        self.assertEqual([], FakeJellyfin.writes)
        self.assertEqual(0, stats["jf_tags_pushed"])
        self.assertEqual(0, stats["jf_tags_failed"])
        self.assertEqual(n, stats["jf_tags_not_found"])
        self.assertIn(f"0 pushed, 0 failed, {n} not in Jellyfin yet", line)
        self.assertTrue([w for w in warnings if "listed no" in w], warnings)

    def test_failed_counts_refused_writes(self):
        self._list([self._found(t, tags=[]) for t in self.tagged])
        FakeJellyfin.refuse = {f"i{t}" for t in self.tagged}
        stats, line, warnings = self._run()
        n = len(self.tagged)
        self.assertEqual(n, stats["jf_tags_failed"])
        self.assertEqual(0, stats["jf_tags_not_found"])
        self.assertIn(f"0 pushed, {n} failed, 0 not in Jellyfin yet", line)

    # --- property: each tagged title lands in exactly the right count ----------

    def test_counts_match_what_happened_over_many_states(self):
        for seed in range(SEEDS):
            rnd = random.Random(seed)
            down = rnd.random() < 0.1
            items, refuse, writes = [], set(), []
            exp = {"pushed": 0, "failed": 0, "not_found": 0}
            for t in self.tagged:
                how = rnd.choice(["absent", "tmdb", "tmdb", "title_year", "title"])
                needs = rnd.random() < 0.6
                if how != "absent":
                    items.append(self._found(t, tags=[] if needs else None, how=how))
                if how == "absent" or down:
                    exp["not_found"] += 1
                elif needs:
                    writes.append(f"i{t}")
                    if rnd.random() < 0.3:
                        refuse.add(f"i{t}")
                        exp["failed"] += 1
                    else:
                        exp["pushed"] += 1
            # Jellyfin items no row has (other titles) change nothing.
            for k in range(rnd.randint(0, 3)):
                items.append(_item(f"x{k}", f"Other {k}", "2010", 70000 + k, []))
            rnd.shuffle(items)
            self._list(items)
            FakeJellyfin.listing_down = down
            FakeJellyfin.refuse = refuse
            FakeJellyfin.writes = []
            stats, line, warnings = self._run()
            got = {"pushed": stats["jf_tags_pushed"], "failed": stats["jf_tags_failed"],
                   "not_found": stats["jf_tags_not_found"]}
            self.assertEqual(exp, got, f"seed {seed}")
            self.assertEqual(sorted(writes), sorted(FakeJellyfin.writes), f"seed {seed}")
            self.assertIn(f"{exp['pushed']} pushed, {exp['failed']} failed, "
                          f"{exp['not_found']} not in Jellyfin yet", line)
            listed_nothing = down or not items
            self.assertEqual(listed_nothing, any("listed no" in w for w in warnings),
                             f"seed {seed}: {warnings}")


class RadarrTagSync(_Cases, unittest.TestCase):
    TYPE, MODEL, LOGGER = "Movie", mdb.Movie, "services.radarr"

    def _scan(self):
        import services.radarr as radarr
        movies = [{"tmdbId": 1000 + i, "title": f"Film {i}", "year": 2000 + i, "hasFile": True,
                   "path": f"/movies/Film {i} ({2000 + i})",
                   "movieFile": {"path": f"/movies/Film {i} ({2000 + i})/f.mkv"}}
                  for i in range(self.N)]

        class Radarr:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return copy.deepcopy(movies)

        with mock.patch.object(radarr, "RadarrService", Radarr), \
                mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            out = radarr.scan_radarr_library(self.db)
        self.db.commit()
        return out


class SonarrTagSync(_Cases, unittest.TestCase):
    TYPE, MODEL, LOGGER = "Series", mdb.Series, "services.sonarr"

    def _scan(self):
        import services.sonarr as sonarr
        shows = [{"tmdbId": 3000 + i, "tvdbId": 500 + i, "title": f"Show {i}", "year": 2000 + i,
                  "path": f"/tv/Show {i}", "monitorNewItems": "none",
                  "statistics": {"episodeFileCount": 2}}
                 for i in range(self.N)]

        class Sonarr:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self, *a, **k):
                return copy.deepcopy(shows)

            def __getattr__(self, name):
                return lambda *a, **k: []

        with mock.patch.object(sonarr, "SonarrService", Sonarr), \
                mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None):
            out = sonarr.scan_sonarr_library(self.db)
        self.db.commit()
        return out


if __name__ == "__main__":
    unittest.main()
