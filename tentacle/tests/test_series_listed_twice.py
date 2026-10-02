"""One show listed twice by the provider (two series ids, e.g. an
EN and a DE category) that both match the same TMDB show.

Each night leaves the show's episode .strm files alone once written (one
listing owns the file; the other listing's episodes are still listed), and
which listing a new file plays doesn't depend on the order the categories
are read in. It was judged one listing at a time (#376): listing B saw A's
episode id as "no longer listed" (#263 rule), A then saw B's the same way,
so every sync rewrote every shared episode twice and the show ended on
whichever category was read last. No network.
"""
import shutil
import unittest
from pathlib import Path as _RealPath

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Provider, ProviderCategory, Series
import services.sync as sync
from tmp_dirs import temp_dir


class TMDB:
    def __init__(self, *a, **k):
        pass

    def search_movie(self, *a, **k):
        return None

    def get_movie_details(self, *a, **k):
        return None

    def search_series(self, name, year=None, **k):
        if "dark" in name.lower():
            return {"tmdb_id": 70523, "title": "Dark", "year": "2017", "overview": "", "genres": [],
                    "poster_path": None, "backdrop_path": None, "status": "Ended", "rating": None}
        return None

    def get_series_details(self, *a, **k):
        return None

    def cleanup_cache(self):
        pass


class XClient(sync.XtreamClient):
    def __init__(self, provider):
        super().__init__(provider)
        self.series = {}     # category -> [series dict]
        self.info = {}       # series_id -> episodes dict

    def get_vod_streams(self, cat):
        return []

    def get_series_list(self, cat):
        return [dict(s) for s in self.series.get(cat, [])]

    def get_series_info(self, sid):
        return {"episodes": self.info.get(str(sid), {})}


class TwoListings(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(lambda: (self.db.close(), engine.dispose()))
        vod = _RealPath(tmp) / "vod"
        (vod / "movies").mkdir(parents=True)
        (vod / "shows").mkdir(parents=True)
        self.vod = vod

        def mapped(*parts):
            s = str(_RealPath(*parts))
            return _RealPath(str(vod) + s[len("/media/vod"):]) if s.startswith("/media/vod") else _RealPath(*parts)

        saved = {k: getattr(sync, k) for k in
                 ("Path", "TMDBService", "make_provider_client", "VOD_MOVIES_ROOT", "VOD_SERIES_ROOT")}
        self.addCleanup(lambda: [setattr(sync, k, v) for k, v in saved.items()])
        sync.Path = mapped
        sync.TMDBService = TMDB
        sync.VOD_MOVIES_ROOT = vod / "movies"
        sync.VOD_SERIES_ROOT = vod / "shows"
        self.p = Provider(name="P", server_url="http://provider", username="u", password="p", active=True)
        self.db.add(self.p)
        self.db.commit()
        self.client = XClient(self.p)
        sync.make_provider_client = lambda p: self.client
        for c in ("en", "de"):
            self.db.add(ProviderCategory(provider_id=self.p.id, category_id=c, category_name=c, type="series",
                                         whitelisted=True, source_tag=f"T{c}"))
        self.db.commit()
        self.client.series = {"en": [{"name": "EN| Dark (2017)", "series_id": 11}],
                              "de": [{"name": "DE| Dark (2017)", "series_id": 22}]}
        ep = lambda i, n: {"id": i, "episode_num": n, "container_extension": "mkv"}
        self.client.info = {"11": {"1": [ep(1101, 1), ep(1102, 2)]},
                            "22": {"1": [ep(2201, 1), ep(2202, 2)]}}

    def night(self):
        run = sync.sync_provider(self.p, "full", self.db)
        self.assertEqual(run.status, "completed", run.error_message)
        self.db.expire_all()

    def files(self):
        show = _RealPath(self.db.query(Series).one().strm_path)
        return {f.name: (f.read_text(), f.stat().st_mtime_ns) for f in sorted(show.rglob("*.strm"))}

    def test_second_night_rewrites_nothing(self):
        self.night()
        first = self.files()
        self.assertEqual(1, self.db.query(Series).count())
        import time; time.sleep(0.02)
        with self.assertLogs(sync.logger, level="INFO") as logs:
            self.night()
        rewrites = [l for l in logs.output if "Rewrote" in l]
        second = self.files()
        self.assertEqual(first, second, f"episode files changed on an unchanged catalogue; rewrites={rewrites}")
        self.assertEqual([], rewrites)

    def plays(self):
        return {n: t.rsplit("/", 1)[1] for n, (t, _) in self.files().items()}

    def test_files_stay_on_the_listing_they_play_and_new_ones_take_the_lowest_series_id(self):
        both = dict(self.client.series)
        self.client.series = {"en": [], "de": both["de"]}
        self.night()   # only DE (series 22) lists the show: imported from it
        self.client.series = both
        self.night()   # EN (series 11) listed too: DE's files are listed, they stay
        self.assertEqual({"Dark (2017) S01E01.strm": "2201.mkv", "Dark (2017) S01E02.strm": "2202.mkv"}, self.plays())
        self.client.info["22"]["1"].append({"id": 2203, "episode_num": 3, "container_extension": "mkv"})
        self.client.info["11"]["1"].append({"id": 1103, "episode_num": 3, "container_extension": "mkv"})
        self.night()   # a new episode in both: the lowest series id's
        self.assertEqual("1103.mkv", self.plays()["Dark (2017) S01E03.strm"])
        self.assertEqual("2201.mkv", self.plays()["Dark (2017) S01E01.strm"])

    def nights_in_order(self, *orders):
        out = []
        for order in orders:
            self.db.query(ProviderCategory).delete()
            for c in order:
                self.db.add(ProviderCategory(provider_id=self.p.id, category_id=c, category_name=c, type="series",
                                             whitelisted=True, source_tag=f"T{c}"))
            self.db.commit()
            with self.assertLogs(sync.logger, level="DEBUG") as logs:
                self.night()
            self.assertEqual([], [l for l in logs.output if "Rewrote" in l], f"order {order}")
            out.append(self.plays())
        return out

    def test_a_new_show_listed_twice_keeps_the_files_it_was_imported_with_en_first(self):
        first, second = self.nights_in_order(["en", "de"], ["de", "en"])
        self.assertEqual({"Dark (2017) S01E01.strm": "1101.mkv", "Dark (2017) S01E02.strm": "1102.mkv"}, first)
        self.assertEqual(first, second)

    def test_a_new_show_listed_twice_keeps_the_files_it_was_imported_with_de_first(self):
        first, second = self.nights_in_order(["de", "en"], ["en", "de"])
        self.assertEqual({"Dark (2017) S01E01.strm": "2201.mkv", "Dark (2017) S01E02.strm": "2202.mkv"}, first)
        self.assertEqual(first, second)

    def test_an_empty_twin_listing_does_not_stop_a_repair(self):
        # The DE listing has no episodes (a placeholder); EN re-lists E01 under a new id (#263).
        self.client.info["22"] = {}
        self.night()
        self.client.info["11"]["1"][0] = {"id": 1199, "episode_num": 1, "container_extension": "mkv"}
        self.night()
        self.assertEqual("1199.mkv", self.plays()["Dark (2017) S01E01.strm"])

    def cancel_at(self, item):
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] >= item
        with self.assertRaises(sync.SyncCancelledError):
            sync._sync_series(self.p, self.client, sync.TMDBService(), self.db, sync.VOD_SERIES_ROOT, 30,
                              cancel_check=cancel)
        self.db.expire_all()

    def test_a_cancelled_sync_still_writes_a_new_shows_episodes(self):
        self.cancel_at(2)   # cancelled at the DE listing, after EN created the show
        self.assertEqual({"Dark (2017) S01E01.strm": "1101.mkv", "Dark (2017) S01E02.strm": "1102.mkv"}, self.plays())

    def test_a_cancelled_sync_writes_the_new_episodes_it_read_and_rewrites_nothing(self):
        self.night()
        self.client.info["11"]["1"] = [{"id": 1199, "episode_num": 1, "container_extension": "mkv"},
                                       {"id": 1102, "episode_num": 2, "container_extension": "mkv"},
                                       {"id": 1103, "episode_num": 3, "container_extension": "mkv"}]
        self.cancel_at(2)
        self.assertEqual({"Dark (2017) S01E01.strm": "1101.mkv", "Dark (2017) S01E02.strm": "1102.mkv",
                          "Dark (2017) S01E03.strm": "1103.mkv"}, self.plays())

    def test_the_episode_writes_stop_on_a_full_disk(self):
        self.night()
        self.client.info["11"]["1"].append({"id": 1103, "episode_num": 3, "container_extension": "mkv"})
        real = sync.check_disk_space
        self.addCleanup(setattr, sync, "check_disk_space", real)
        sync.check_disk_space = lambda path: 10   # MB: below the stop threshold from here on
        with self.assertRaises(sync.SyncCancelledError):
            sync._backfill_noted_series(self.client, {70523: [(11, sync._compact_episodes(self.client.info["11"]))]},
                                        self.p, self.db, sync.VOD_SERIES_ROOT)
        self.assertNotIn("Dark (2017) S01E03.strm", self.plays())

    def test_a_listing_that_fails_to_load_changes_no_file(self):
        self.night()
        first = self.files()
        self.client.info["11"]["1"] = [{"id": 1199, "episode_num": 1, "container_extension": "mkv"}]
        real = self.client.get_series_info
        self.client.get_series_info = lambda sid: (_ for _ in ()).throw(ConnectionError("down")) \
            if str(sid) == "22" else real(sid)
        self.night()
        self.assertEqual(first, self.files())
        self.client.get_series_info = real
        self.night()   # both read: E01's 1101 is listed nowhere now, 1199 is EN's E01
        self.assertEqual("1199.mkv", self.plays()["Dark (2017) S01E01.strm"])


if __name__ == "__main__":
    unittest.main()
