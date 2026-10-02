"""Resolving a duplicate keeps every user's watched state on the copy that stays (#297).

Jellyfin 10.11 keeps played / play count / resume point / favourite per item,
and a VOD .strm and a download of the same film in separate folders are two
items that don't share it. "Keep Downloaded" deleted the .strm, and with it
everything users had on the VOD item: the kept download started from zero.
"Keep VOD" did the same to the download's item. Now each user's data on the
removed copy is merged onto the kept one first, and nothing is deleted when
that can't be done.

The fake Jellyfin answers the calls the carry-over makes: /Items listings and
GET/POST /UserItems/{id}/UserData?userId=.

Run from tentacle/:  python -m unittest tests.test_duplicate_keeps_user_data
"""
import logging
import shutil
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Duplicate, Movie, Series, Setting
from routers import duplicates
from tests.test_duplicate_keep_vod_merged_folder import FakeRadarr, FakeSonarr, _FakeArr
from tmp_dirs import temp_dir


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeJellyfin:
    items = []          # {"Id", "Type", "Path", "ProviderIds", "ParentId"?, "ParentIndexNumber"?, "IndexNumber"?}
    users = ["A", "B"]
    data = {}           # (user, item) -> UserData dict
    posts = []
    down = False
    on_post = None      # called on each POST (to see what is on disk then)

    def __init__(self, url, key, user_id=""):
        self.url = url
        self.session = self

    def get_user_ids(self):
        return None if self.down else list(self.users)

    def get(self, url, params=None, timeout=None):
        if self.down:
            raise ConnectionError("Jellyfin is down")
        path = url.split("http://jf", 1)[1]
        if path == "/Items":
            if "Ids" in params:   # a film's versions (#333): none here
                rows = [i for i in self.items if i["Id"] in params["Ids"].split(",")]
            elif "ParentId" in params:
                rows = [i for i in self.items if i.get("ParentId") == params["ParentId"]]
            else:
                rows = [i for i in self.items if i["Type"] == params["IncludeItemTypes"]]
            page = rows[params["StartIndex"]:params["StartIndex"] + params["Limit"]]
            return _Resp(200, {"Items": [dict(r) for r in page], "TotalRecordCount": len(rows)})
        if path.startswith("/UserItems/"):
            item = path.split("/")[2]
            if not any(i["Id"] == item for i in self.items):
                return _Resp(404)
            return _Resp(200, dict(self.data.get((params["userId"], item), {})))
        return _Resp(404)

    def post(self, url, params=None, json=None, timeout=None):
        item = url.split("/UserItems/", 1)[1].split("/")[0]
        if FakeJellyfin.on_post:
            FakeJellyfin.on_post()
        self.posts.append((params["userId"], item, dict(json)))
        self.data.setdefault((params["userId"], item), {}).update(json)
        return _Resp(200)


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
                     ("jellyfin_url", "http://jf"), ("jellyfin_api_key", "j")):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()
        _FakeArr.titles, _FakeArr.calls, _FakeArr.fail_file_delete = {}, [], False
        FakeJellyfin.items, FakeJellyfin.data, FakeJellyfin.posts = [], {}, []
        FakeJellyfin.users, FakeJellyfin.down, FakeJellyfin.on_post = ["A", "B"], False, None
        mock.patch("services.radarr.RadarrService", FakeRadarr).start()
        mock.patch("services.sonarr.SonarrService", FakeSonarr).start()
        mock.patch("services.jellyfin.JellyfinService", FakeJellyfin).start()
        self.addCleanup(mock.patch.stopall)


class Film(_Base):
    """A VOD .strm and a download of the same film in separate folders."""
    def setUp(self):
        super().setUp()
        vod = self.root / "vod" / "movies" / "Film (2001)"
        vod.mkdir(parents=True)
        self.strm = vod / "Film (2001).strm"
        self.strm.write_text("http://p/movie/1.mp4")
        dl = self.root / "movies" / "Film (2001)"
        dl.mkdir(parents=True)
        self.mkv = dl / "Film (2001).mkv"
        self.mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[101] = {"id": 7, "path": str(dl), "files": [{"id": 70, "path": str(self.mkv)}]}
        self.db.add(Movie(tmdb_id=101, title="Film", year="2001", source="provider_1", strm_path=str(self.strm)))
        self.dup = Duplicate(tmdb_id=101, media_type="movie", resolution="pending",
                             sources=[{"source": "radarr", "path": str(self.mkv)},
                                      {"source": "provider_1", "path": str(self.strm)}])
        self.db.add(self.dup)
        self.db.commit()
        # Jellyfin mounts the folders elsewhere.
        FakeJellyfin.items = [
            {"Id": "vod", "Type": "Movie", "Path": "/vod-movies/Film (2001)/Film (2001).strm",
             "ProviderIds": {"Tmdb": "101"}},
            {"Id": "dl", "Type": "Movie", "Path": "/data/movies/Film (2001)/Film (2001).mkv",
             "ProviderIds": {"Tmdb": "101"}},
            {"Id": "other", "Type": "Movie", "Path": "/data/movies/Other/Other.mkv",
             "ProviderIds": {"Tmdb": "102"}},
        ]

    def test_keep_downloaded_carries_played_favourite_and_resume_point(self):
        FakeJellyfin.data = {
            ("A", "vod"): {"Played": True, "IsFavorite": True, "PlayCount": 1,
                           "LastPlayedDate": "2026-09-20T20:00:00.0000000Z"},
            ("B", "vod"): {"PlaybackPositionTicks": 7_000_000_000},
        }
        FakeJellyfin.on_post = lambda: self.assertTrue(self.strm.exists(), "the copy was deleted before its data moved")
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())
        a, b = FakeJellyfin.data[("A", "dl")], FakeJellyfin.data[("B", "dl")]
        self.assertTrue(a["Played"] and a["IsFavorite"])
        self.assertEqual(1, a["PlayCount"])
        self.assertEqual("2026-09-20T20:00:00.0000000Z", a["LastPlayedDate"])
        self.assertEqual(7_000_000_000, b["PlaybackPositionTicks"])
        self.assertNotIn(("A", "other"), FakeJellyfin.data)

    def test_keep_vod_carries_the_downloads_data(self):
        FakeJellyfin.data = {("A", "dl"): {"Played": True, "PlayCount": 2}}
        FakeJellyfin.on_post = lambda: self.assertTrue(self.mkv.exists(), "the copy was deleted before its data moved")
        duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.assertFalse(self.mkv.exists())
        self.assertEqual({"Played": True, "PlayCount": 2}, FakeJellyfin.data[("A", "vod")])

    def test_merge_never_takes_anything_away(self):
        FakeJellyfin.data = {
            ("A", "vod"): {"PlaybackPositionTicks": 5, "PlayCount": 1},
            ("A", "dl"): {"Played": True, "PlayCount": 3, "IsFavorite": True,
                          "LastPlayedDate": "2026-09-25T00:00:00Z"},
        }
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual([], FakeJellyfin.posts, "nothing on the kept copy should change")

    def test_nothing_to_carry_writes_nothing(self):
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual([], FakeJellyfin.posts)
        self.assertFalse(self.strm.exists())

    def test_kept_copy_not_in_jellyfin_yet_refuses(self):
        FakeJellyfin.items = [i for i in FakeJellyfin.items if i["Id"] != "dl"]
        FakeJellyfin.data = {("B", "vod"): {"PlaybackPositionTicks": 7_000_000_000}}
        with self.assertRaises(HTTPException) as cm:
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(409, cm.exception.status_code)
        self.assertTrue(self.strm.exists())
        self.assertEqual("provider_1", self.db.query(Movie).one().source)

    def test_kept_copy_not_in_jellyfin_without_user_data_proceeds(self):
        FakeJellyfin.items = [i for i in FakeJellyfin.items if i["Id"] != "dl"]
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())

    def test_jellyfin_down_deletes_nothing(self):
        FakeJellyfin.down = True
        with self.assertRaises(HTTPException) as cm:
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(502, cm.exception.status_code)
        self.assertTrue(self.strm.exists())
        with self.assertRaises(HTTPException):
            duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.assertTrue(self.mkv.exists())
        self.assertIn(101, _FakeArr.titles)


class Show(_Base):
    """A VOD show and a downloaded show in separate folders: per episode."""
    def setUp(self):
        super().setUp()
        vod = self.root / "vod" / "shows" / "Dark (2017)"
        (vod / "Season 01").mkdir(parents=True)
        self.strm = vod / "Season 01" / "Dark (2017) S01E01.strm"
        self.strm.write_text("http://p/series/1.mp4")
        dl = self.root / "tv" / "Dark (2017)"
        (dl / "Season 01").mkdir(parents=True)
        mkv = dl / "Season 01" / "Dark - S01E01.mkv"
        mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[70523] = {"id": 9, "path": str(dl), "files": [{"id": 90, "path": str(mkv)}]}
        self.db.add(Series(tmdb_id=70523, title="Dark", year="2017", source="provider_1", strm_path=str(vod)))
        self.dup = Duplicate(tmdb_id=70523, media_type="series", resolution="pending",
                             sources=[{"source": "sonarr", "path": str(dl)},
                                      {"source": "provider_1", "path": str(vod)}])
        self.db.add(self.dup)
        self.db.commit()
        # Same folder name under two mounts: the VOD show is the one with .strm episodes.
        FakeJellyfin.items = [
            {"Id": "s-dl", "Type": "Series", "Path": "/data/tv/Dark (2017)", "ProviderIds": {"Tmdb": "70523"}},
            {"Id": "s-vod", "Type": "Series", "Path": "/vod-shows/Dark (2017)", "ProviderIds": {"Tmdb": "70523"}},
            {"Id": "e-vod-1", "Type": "Episode", "ParentId": "s-vod", "ParentIndexNumber": 1, "IndexNumber": 1,
             "Path": "/vod-shows/Dark (2017)/Season 01/Dark (2017) S01E01.strm"},
            {"Id": "e-vod-2", "Type": "Episode", "ParentId": "s-vod", "ParentIndexNumber": 1, "IndexNumber": 2,
             "Path": "/vod-shows/Dark (2017)/Season 01/Dark (2017) S01E02.strm"},
            {"Id": "e-dl-1", "Type": "Episode", "ParentId": "s-dl", "ParentIndexNumber": 1, "IndexNumber": 1,
             "Path": "/data/tv/Dark (2017)/Season 01/Dark - S01E01.mkv"},
            {"Id": "e-dl-missing", "Type": "Episode", "ParentId": "s-dl", "ParentIndexNumber": 1, "IndexNumber": 2},
        ]

    def test_keep_downloaded_carries_each_episode_and_the_show(self):
        FakeJellyfin.data = {
            ("A", "e-vod-1"): {"Played": True, "PlayCount": 1},
            ("B", "e-vod-2"): {"PlaybackPositionTicks": 42},   # no downloaded counterpart: gone with it
            ("A", "s-vod"): {"IsFavorite": True},
        }
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())
        self.assertEqual({"Played": True, "PlayCount": 1}, FakeJellyfin.data[("A", "e-dl-1")])
        self.assertEqual({"IsFavorite": True}, FakeJellyfin.data[("A", "s-dl")])
        self.assertNotIn(("B", "e-dl-missing"), FakeJellyfin.data)


if __name__ == "__main__":
    unittest.main()
