"""The automatic duplicates rule (#872, services/duplicates_auto.py).

Off by default; download first keeps the download and removes the .strm;
stream first keeps the .strm and deletes the download through Radarr/Sonarr,
except when the title is on the prefer-download list, its stream is known
bad, or the download came from a protected indexer (the download stays), and
it leaves a pair alone while the download still seeds, nobody watched it,
the provider isn't answering, or the download has episodes the stream lacks.
Over the cap a run changes nothing and waits. A partly downloaded show never
loses an episode (#800). Radarr, Sonarr, qBittorrent and Jellyfin are fakes;
no provider is ever contacted (the rule judges streams only from what
Tentacle knows).

Run from tentacle/:  python -m unittest tests.test_duplicates_rule
"""
import json
import logging
import shutil
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Base, DeletionLog, Duplicate, Movie, Series, Setting, StreamHealth
from tmp_dirs import temp_dir


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class Arr:
    """Radarr and Sonarr in one: titles by TMDB id, their files and history,
    and every delete asked of them."""
    movies, series, files, history, calls = {}, {}, {}, {}, []

    def __init__(self, *a, **k):
        pass

    # Radarr
    def get_all_movies(self, raise_errors=False):
        return list(Arr.movies.values())

    def get_movie_by_tmdb(self, tmdb_id):
        return Arr.movies.get(tmdb_id)

    def get_movie_files(self, arr_id):
        return [dict(f) for f in Arr.files.get(("movie", arr_id), [])]

    def delete_movie_file(self, file_id):
        Arr.calls.append(("delete_movie_file", file_id))
        for f in Arr.files.get(("movie", file_id // 100), []):
            if f["id"] == file_id:
                Path(f["path"]).unlink(missing_ok=True)

    def delete_movie_by_id(self, arr_id, delete_files=True):
        Arr.calls.append(("delete_movie", arr_id, delete_files))
        return True

    # Sonarr
    def get_all_series(self, raise_errors=False):
        return list(Arr.series.values())

    def get_series_by_tmdb(self, tmdb_id, raise_errors=False):
        return Arr.series.get(tmdb_id)

    def get_episode_files(self, arr_id):
        return [dict(f) for f in Arr.files.get(("series", arr_id), [])]

    def delete_episode_files(self, ids):
        Arr.calls.append(("delete_episode_files", tuple(ids)))
        for f in Arr.files.get(("series", ids[0] // 100), []) if ids else []:
            if f["id"] in ids:
                Path(f["path"]).unlink(missing_ok=True)

    def delete_series_by_id(self, arr_id, delete_files=True):
        Arr.calls.append(("delete_series", arr_id, delete_files))
        return True

    def get_history(self, arr_id):
        return Arr.history.get(arr_id, [])

    def get_recycle_bin(self):
        return "/recycle"


def grab(download_id, indexer="NZBgeek", torrent=False):
    data = {"indexer": indexer, "protocol": "2" if torrent else "1"}
    if torrent:
        data["torrentInfoHash"] = download_id
    return [{"eventType": "grabbed", "downloadId": download_id, "data": data},
            {"eventType": "downloadFolderImported", "downloadId": download_id, "data": {}}]


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = Path(tmp)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r"),
                     ("sonarr_url", "http://sonarr"), ("sonarr_api_key", "s"),
                     ("provider_health", json.dumps({"1": {"state": "ok"}}))):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()
        Arr.movies, Arr.series, Arr.files, Arr.history, Arr.calls = {}, {}, {}, {}, []
        mock.patch("services.radarr.RadarrService", Arr).start()
        mock.patch("services.sonarr.SonarrService", Arr).start()
        # What Jellyfin says was played (file names); no Jellyfin otherwise,
        # so the user-data carry-over has nothing to do.
        self.played = set()
        mock.patch("services.duplicates_auto._Facts._read_played", lambda facts: set(self.played)).start()
        # No provider is ever contacted: any HTTP from the rule fails the test.
        mock.patch("requests.Session.request", side_effect=AssertionError("network used")).start()
        self.addCleanup(mock.patch.stopall)

    def set(self, key, value):
        mdb.set_setting(self.db, key, value)

    # A film: VOD .strm in its own folder, the download in Radarr's.
    def film(self, tmdb_id, title, history=None):
        vod = self.root / "vod" / "movies" / f"{title}"
        vod.mkdir(parents=True)
        strm = vod / f"{title}.strm"
        strm.write_text(f"http://tentacle/api/vod/movie/{tmdb_id}.mkv")
        (vod / f"{title}.nfo").write_text("<movie/>")
        dl = self.root / "movies" / f"{title}"
        dl.mkdir(parents=True)
        mkv = dl / f"{title}.mkv"
        mkv.write_bytes(b"\0" * 8)
        arr_id = tmdb_id
        Arr.movies[tmdb_id] = {"id": arr_id, "tmdbId": tmdb_id, "title": title, "path": str(dl)}
        Arr.files[("movie", arr_id)] = [{"id": arr_id * 100 + 1, "path": str(mkv)}]
        Arr.history[arr_id] = history if history is not None else grab(f"nzb{tmdb_id}")
        self.db.add(Movie(tmdb_id=tmdb_id, title=title, source="provider_1", provider_id=1,
                          strm_path=str(strm), radarr_path=str(mkv), tags=["Netflix Movies"]))
        dup = Duplicate(tmdb_id=tmdb_id, media_type="movie", resolution="pending",
                        sources=[{"source": "radarr", "path": str(mkv)},
                                 {"source": "provider_1", "path": str(strm)}])
        self.db.add(dup)
        self.db.commit()
        return strm, mkv

    # A show: 3 episodes as .strm; the download holds `downloaded` of them
    # (plus `extra` episodes the stream doesn't have).
    def show(self, tmdb_id, title, downloaded=(1, 2, 3), extra=()):
        vod = self.root / "vod" / "shows" / title
        season = vod / "Season 01"
        season.mkdir(parents=True)
        for n in (1, 2, 3):
            (season / f"{title} S01E0{n}.strm").write_text(f"http://tentacle/api/vod/series/{n}.mkv")
        dl = self.root / "shows" / title / "Season 01"
        dl.mkdir(parents=True)
        files = []
        for i, n in enumerate(list(downloaded) + list(extra)):
            f = dl / f"{title} S01E0{n}.mkv"
            f.write_bytes(b"\0")
            files.append({"id": tmdb_id * 100 + i, "path": str(f)})
        arr_id = tmdb_id
        Arr.series[tmdb_id] = {"id": arr_id, "tmdbId": tmdb_id, "title": title, "path": str(dl.parent)}
        Arr.files[("series", arr_id)] = files
        Arr.history[arr_id] = grab(f"nzb{tmdb_id}")
        self.db.add(Series(tmdb_id=tmdb_id, title=title, source="provider_1", provider_id=1,
                           strm_path=str(vod), sonarr_path=str(dl.parent), tags=["Netflix TV"]))
        self.db.add(Duplicate(tmdb_id=tmdb_id, media_type="series", resolution="pending",
                              sources=[{"source": "sonarr", "path": str(dl.parent)},
                                       {"source": "provider_1", "path": str(vod)}]))
        self.db.commit()
        return season, [Path(f["path"]) for f in files]

    def run_rule(self, confirm=None):
        from services import duplicates_auto
        return duplicates_auto.run(self.db, confirm=confirm)

    def dup(self, tmdb_id):
        self.db.expire_all()
        return self.db.query(Duplicate).filter_by(tmdb_id=tmdb_id).one()

    def decision(self, summary, tmdb_id):
        return next(i for i in summary["items"] if i["tmdb_id"] == tmdb_id)


class Off(_Base):
    def test_off_is_the_default_and_nothing_happens(self):
        from services import duplicates_auto
        from routers import duplicates
        strm, mkv = self.film(1, "Heat")
        self.assertEqual("off", duplicates_auto.mode(self.db))
        self.assertEqual("off", duplicates.get_rule(db=self.db)["mode"])
        self.assertEqual({"state": "off"}, self.run_rule())
        with mock.patch("models.database.SessionLocal", return_value=self.db):
            duplicates_auto.run_scheduled()
        self.assertTrue(strm.exists() and mkv.exists())
        self.assertEqual("pending", self.dup(1).resolution)
        self.assertEqual([], Arr.calls)
        self.assertEqual(0, self.db.query(DeletionLog).count())
        self.assertIsNone(duplicates_auto.last_run(self.db))


class DownloadFirst(_Base):
    def setUp(self):
        super().setUp()
        self.set("duplicates_rule", "download")

    def test_a_film_keeps_its_download_and_loses_the_strm(self):
        strm, mkv = self.film(1, "Heat")
        out = self.run_rule()
        self.assertEqual("done", out["state"])
        self.assertEqual(1, out["counts"]["keep_download"])
        self.assertFalse(strm.exists())
        self.assertTrue(mkv.exists())
        self.assertEqual([], Arr.calls)
        dup = self.dup(1)
        self.assertEqual(("keep_radarr", "rule"), (dup.resolution, dup.resolved_by))
        self.assertEqual("radarr", self.db.query(Movie).filter_by(tmdb_id=1).one().source)  # the tombstone
        log = self.db.query(DeletionLog).one()
        self.assertEqual("duplicates-rule", log.reason)
        self.assertIn("download first", log.detail)

    def test_a_partly_downloaded_show_never_loses_an_episode(self):
        season, _ = self.show(5, "Lanterns", downloaded=(1,))
        out = self.run_rule()
        d = self.decision(out, 5)
        self.assertEqual("skip", d["action"])
        self.assertIn("2 episodes", d["why"])
        self.assertEqual(3, len(list(season.glob("*.strm"))))
        self.assertEqual("pending", self.dup(5).resolution)

    def test_a_show_downloaded_in_full_loses_its_streams(self):
        season, mkvs = self.show(6, "Tides")
        self.run_rule()
        self.assertEqual([], list(season.glob("*.strm")))
        self.assertTrue(all(m.exists() for m in mkvs))
        self.assertEqual("keep_radarr", self.dup(6).resolution)

    def test_a_film_with_no_real_download_is_left(self):
        strm, mkv = self.film(2, "Ronin")
        Arr.files[("movie", 2)] = []
        out = self.run_rule()
        self.assertEqual("skip", self.decision(out, 2)["action"])
        self.assertTrue(strm.exists())


class StreamFirst(_Base):
    def setUp(self):
        super().setUp()
        self.set("duplicates_rule", "stream")

    def test_a_watched_download_goes_through_radarr_and_the_strm_stays(self):
        strm, mkv = self.film(1, "Heat")
        self.played = {"heat.mkv"}
        out = self.run_rule()
        self.assertEqual(1, out["counts"]["keep_stream"], out["items"])
        self.assertTrue(strm.exists())
        self.assertFalse(mkv.exists())
        self.assertEqual([("delete_movie_file", 101)], Arr.calls[:1])
        self.assertEqual("delete_movie", Arr.calls[1][0])
        self.assertEqual("keep_vod", self.dup(1).resolution)
        self.assertEqual("duplicates-rule", self.db.query(DeletionLog).one().reason)

    def test_an_unwatched_download_is_not_removed(self):
        strm, mkv = self.film(1, "Heat")
        d = self.decision(self.run_rule(), 1)
        self.assertEqual("skip", d["action"])
        self.assertIn("watched", d["why"])
        self.assertTrue(mkv.exists() and strm.exists())
        self.assertEqual([], Arr.calls)

    def test_prefer_download_keeps_the_download(self):
        from routers import duplicates
        strm, mkv = self.film(1, "Heat")
        self.played = {"heat.mkv"}
        duplicates.set_prefer_download(duplicates.PreferDownload(media_type="movie", tmdb_id=1, prefer=True), db=self.db)
        self.assertEqual(["Heat"], [r["title"] for r in duplicates.list_prefer_download(db=self.db)])
        d = self.decision(self.run_rule(), 1)
        self.assertEqual(("keep_download", "on the prefer-download list"), (d["action"], d["why"]))
        self.assertTrue(mkv.exists())
        self.assertFalse(strm.exists())
        self.assertEqual([], Arr.calls)

    def test_a_known_bad_stream_keeps_the_download(self):
        strm, mkv = self.film(1, "Heat")
        self.played = {"heat.mkv"}
        self.db.add(StreamHealth(media_type="movie", tmdb_id=1, title="Heat", strm_path=str(strm),
                                 first_failed_at=datetime(2026, 10, 20)))
        self.db.commit()
        d = self.decision(self.run_rule(), 1)
        self.assertEqual("keep_download", d["action"])
        self.assertIn("known bad since 2026-10-20", d["why"])
        self.assertTrue(mkv.exists())
        self.assertFalse(strm.exists())

    def test_a_protected_indexer_keeps_the_download(self):
        self.set("duplicates_rule_indexers", "PrivateHD, other")
        strm, mkv = self.film(1, "Heat", history=grab("ABC", indexer="PrivateHD", torrent=True))
        self.played = {"heat.mkv"}
        d = self.decision(self.run_rule(), 1)
        self.assertEqual("keep_download", d["action"])
        self.assertIn("PrivateHD", d["why"])
        self.assertTrue(mkv.exists())

    def test_a_download_still_seeding_is_left(self):
        self.set("qbittorrent_url", "http://qbit")
        self.film(1, "Heat", history=grab("ABC", torrent=True))
        self.film(2, "Ronin", history=grab("DEF", torrent=True))
        self.played = {"heat.mkv", "ronin.mkv"}
        with mock.patch("services.qbittorrent.torrent_hashes", return_value={"abc"}):
            out = self.run_rule()
        self.assertEqual(("skip", "still seeding in qBittorrent"),
                         (self.decision(out, 1)["action"], self.decision(out, 1)["why"]))
        self.assertEqual("keep_stream", self.decision(out, 2)["action"])
        self.assertEqual("pending", self.dup(1).resolution)

    def test_a_torrent_without_qbittorrent_is_not_sure(self):
        strm, mkv = self.film(1, "Heat", history=grab("ABC", torrent=True))
        self.played = {"heat.mkv"}
        d = self.decision(self.run_rule(), 1)
        self.assertEqual("skip", d["action"])
        self.assertIn("qBittorrent isn't connected", d["why"])
        self.assertTrue(mkv.exists())

    def test_a_provider_not_answering_is_not_sure(self):
        self.set("provider_health", json.dumps({"1": {"state": "failing"}}))
        strm, mkv = self.film(1, "Heat")
        self.played = {"heat.mkv"}
        d = self.decision(self.run_rule(), 1)
        self.assertEqual("skip", d["action"])
        self.assertIn("provider isn't answering", d["why"])
        self.assertTrue(mkv.exists())

    def test_a_show_with_episodes_only_downloaded_keeps_them(self):
        season, mkvs = self.show(6, "Tides", downloaded=(1, 2, 3), extra=(4,))
        self.played = {m.name.casefold() for m in mkvs}
        d = self.decision(self.run_rule(), 6)
        self.assertEqual("skip", d["action"])
        self.assertIn("only as downloads", d["why"])
        self.assertTrue(all(m.exists() for m in mkvs))

    def test_a_partly_downloaded_show_on_the_list_keeps_every_episode(self):
        season, mkvs = self.show(5, "Lanterns", downloaded=(1,))
        self.db.query(Series).filter_by(tmdb_id=5).one().prefer_download = True
        self.db.commit()
        d = self.decision(self.run_rule(), 5)
        self.assertEqual("skip", d["action"])
        self.assertIn("exist only as streams", d["why"])
        self.assertEqual(3, len(list(season.glob("*.strm"))))
        self.assertTrue(mkvs[0].exists())


class Cap(_Base):
    def test_over_the_cap_nothing_changes_until_applied(self):
        self.set("duplicates_rule", "download")
        self.set("duplicates_rule_cap", "2")
        films = [self.film(i, t) for i, t in ((1, "Heat"), (2, "Ronin"), (3, "Thief"))]
        out = self.run_rule()
        self.assertEqual("held", out["state"])
        self.assertEqual(3, out["changes"])
        self.assertTrue(all(s.exists() and m.exists() for s, m in films))
        self.assertEqual(0, self.db.query(DeletionLog).count())
        self.assertEqual({"pending"}, {d.resolution for d in self.db.query(Duplicate).all()})
        # A later run is held again; the admin applies what they were shown.
        self.assertEqual("held", self.run_rule()["state"])
        self.assertEqual("held", self.run_rule(confirm=2)["state"], "confirmed fewer than it would change")
        out = self.run_rule(confirm=3)
        self.assertEqual(("done", 3), (out["state"], out["counts"]["keep_download"]))
        self.assertFalse(any(s.exists() for s, _ in films))

    def test_the_preview_changes_nothing_and_counts_each_mode(self):
        from routers import duplicates
        self.film(1, "Heat")
        self.film(2, "Ronin")
        self.show(5, "Lanterns", downloaded=(1,))
        self.played = {"heat.mkv"}
        down = duplicates.preview_rule(mode="download", db=self.db)
        stream = duplicates.preview_rule(mode="stream", db=self.db)
        self.assertEqual(("dry-run", 2, 1), (down["state"], down["counts"]["keep_download"], down["counts"]["skip"]))
        self.assertEqual((1, 0, 2), (stream["counts"]["keep_stream"], stream["counts"]["keep_download"],
                                     stream["counts"]["skip"]))
        self.assertEqual({"pending"}, {d.resolution for d in self.db.query(Duplicate).all()})
        self.assertEqual([], Arr.calls)
        self.assertEqual(0, self.db.query(DeletionLog).count())


class Settings(_Base):
    def test_switching_on_and_the_password_stays_hidden(self):
        from routers import duplicates
        out = duplicates.put_rule(duplicates.RuleSettings(mode="download", cap=5, qbittorrent_url="http://qbit:8080",
                                                          qbittorrent_password="hunter22"), db=self.db)
        self.assertEqual(("download", 5), (out["mode"], out["cap"]))
        self.assertNotIn("hunter22", json.dumps(out))
        # Saving the masked form back keeps the password.
        duplicates.put_rule(duplicates.RuleSettings(mode="stream", qbittorrent_password=out["qbittorrent_password"]),
                            db=self.db)
        self.assertEqual("hunter22", mdb.get_setting(self.db, "qbittorrent_password"))
        from routers.settings import SENSITIVE_KEYS, PASSWORD_KEYS
        self.assertIn("qbittorrent_password", SENSITIVE_KEYS & PASSWORD_KEYS)


if __name__ == "__main__":
    unittest.main()
