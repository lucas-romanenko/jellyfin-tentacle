"""#376: one show listed twice by the provider (two series ids, e.g. an EN and
a DE category) that both match the same TMDB show, so one show folder.

Each listing judged "no longer listed" (#263) against its own episodes only,
so the two rewrote each other's episode files every night and the show ended
on whichever category was read last. Once a file plays an episode some
listing offers at its number, a sync leaves it alone. No network.
"""
import time
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
        self.broken = set()  # categories whose fetch fails

    def get_vod_streams(self, cat):
        return []

    def get_series_list(self, cat):
        if cat in self.broken:
            raise ConnectionError("provider down")
        return [dict(s) for s in self.series.get(cat, [])]

    def get_series_info(self, sid):
        return {"episodes": self.info.get(str(sid), {})}


def ep(i, n):
    return {"id": i, "episode_num": n, "container_extension": "mkv"}


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
        self.cats = {}
        for c in ("en", "de"):
            self.cats[c] = ProviderCategory(provider_id=self.p.id, category_id=c, category_name=c, type="series",
                                            whitelisted=True, source_tag=f"T{c}")
            self.db.add(self.cats[c])
        self.db.commit()
        self.client.series = {"en": [{"name": "EN| Dark (2017)", "series_id": 11}],
                              "de": [{"name": "DE| Dark (2017)", "series_id": 22}]}
        self.client.info = {"11": {"1": [ep(1101, 1), ep(1102, 2)]},
                            "22": {"1": [ep(2201, 1), ep(2202, 2)]}}

    def night(self):
        run = sync.sync_provider(self.p, "full", self.db)
        self.assertEqual(run.status, "completed", run.error_message)
        self.db.expire_all()

    def files(self):
        show = _RealPath(self.db.query(Series).one().strm_path)
        return {f.name: (f.read_text(), f.stat().st_mtime_ns) for f in sorted(show.rglob("*.strm"))}

    def plays(self):
        return {name: int(text.rsplit("/", 1)[1].split(".")[0]) for name, (text, _) in self.files().items()}

    def test_second_night_rewrites_nothing(self):
        self.night()
        first = self.files()
        self.assertEqual(1, self.db.query(Series).count())
        time.sleep(0.02)
        with self.assertLogs(sync.logger, level="INFO") as logs:
            self.night()
        rewrites = [l for l in logs.output if "Rewrote" in l]
        self.assertEqual(first, self.files(), f"episode files changed on an unchanged catalogue; rewrites={rewrites}")
        self.assertEqual([], rewrites)

    def test_a_listing_whitelisted_later_takes_nothing_over(self):
        self.cats["de"].whitelisted = False
        self.db.commit()
        self.night()
        self.assertEqual({"Dark (2017) S01E01.strm": 1101, "Dark (2017) S01E02.strm": 1102}, self.plays())
        self.cats["de"].whitelisted = True
        self.db.commit()
        self.night()
        self.night()
        self.assertEqual({"Dark (2017) S01E01.strm": 1101, "Dark (2017) S01E02.strm": 1102}, self.plays())

    def test_an_empty_twin_listing_does_not_stop_a_repair(self):
        # The DE listing answers with no episodes (a placeholder); EN re-lists
        # E01 under a new id (#263). The empty twin offers nothing, so EN decides.
        self.client.info["22"] = {}
        self.night()
        self.client.info["11"]["1"][0] = ep(1199, 1)
        self.night()
        self.assertEqual({"Dark (2017) S01E01.strm": 1199, "Dark (2017) S01E02.strm": 1102}, self.plays())

    def test_an_empty_twin_listing_rewrites_nothing_else(self):
        self.client.info["22"] = {}
        self.night()
        first = self.files()
        time.sleep(0.02)
        self.night()
        self.night()
        self.assertEqual(first, self.files())

    def test_a_twin_listing_that_fails_to_load_still_repoints_nothing(self):
        self.night()
        self.client.info["11"]["1"][0] = ep(1199, 1)
        self.client.info["22"]["1"][0] = ep(2299, 1)
        real = self.client.get_series_info

        def info(sid):
            if str(sid) == "22":
                raise ConnectionError("provider timeout")
            return real(sid)
        self.client.get_series_info = info
        self.night()
        self.assertEqual(1101, self.plays()["Dark (2017) S01E01.strm"],
                         "a listing that could not be read may be the one E01 plays")

    def test_a_replaced_upload_follows_its_own_listing(self):
        self.night()
        before = self.plays()["Dark (2017) S01E01.strm"]
        owner = "11" if before == 1101 else "22"
        self.client.info[owner]["1"][0] = ep(before + 98, 1)   # re-uploaded under a new id
        self.night()
        self.assertEqual(before + 98, self.plays()["Dark (2017) S01E01.strm"])

    def test_an_incomplete_fetch_repoints_nothing(self):
        self.cats["de"].whitelisted = False
        self.db.commit()
        self.night()
        self.cats["de"].whitelisted = True
        self.db.commit()
        self.client.info["11"] = {"1": [ep(1199, 1), ep(1102, 2)]}
        self.client.broken = {"de"}
        self.night()
        self.assertEqual(1101, self.plays()["Dark (2017) S01E01.strm"])
        self.client.broken = set()
        self.night()
        self.assertEqual(1199, self.plays()["Dark (2017) S01E01.strm"])


if __name__ == "__main__":
    unittest.main()
