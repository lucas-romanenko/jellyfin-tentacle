"""A show streamed and downloaded is ONE series in Jellyfin, whatever order things happened in (#889).

Jellyfin makes one series of one folder. Sonarr's copy under its own root and
the VOD copy under the VOD shows folder made two series, each with part of
the episodes: a download (S05E06) could only be found in the second one.
services.one_series points Sonarr at the show's VOD folder with Sonarr's own
"move files", parks the stream copy of an episode that is downloaded (Jellyfin
lists both otherwise) and brings it back when the download goes. Nothing is
deleted, and nothing moves when Sonarr's move would overwrite a file.

The fake Sonarr below moves folders as Sonarr 4 does (checked on a throwaway
Sonarr with the production image): into a missing target it renames the
folder, into an existing one it moves every file across, overwriting a file
of the same name, then removes the emptied source folder.

Run from tentacle/:  python -m unittest tests.test_one_series
"""
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermetic import no_tmdb
from tmp_dirs import temp_dir

VOD_NAME = "Special Forces World's Toughest Test (2023)"
ARR_NAME = "Special Forces - World's Toughest Test"
TMDB = 216562


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSonarr:
    """Sonarr with two root folders: /data/tv (Tentacle's /media/shows) and
    /data/vod/Series (Tentacle's /media/vod/shows), both in the test's temp dir."""
    series = []
    roots = [{"path": "/data/tv"}, {"path": "/data/vod/Series"}]
    disk = {}            # Sonarr root -> local folder
    puts = []
    move_fails = False

    def __init__(self, *a, **k):
        self.url = "http://sonarr"
        self.session = self

    # SonarrService
    def get_all_series(self, raise_errors=False):
        return [dict(s) for s in self.series]

    def get_root_folders(self, required=False):
        return list(self.roots)

    def get_episode_files(self, series_id):
        show = next(s for s in self.series if s["id"] == series_id)
        folder = self.local(show["path"])
        files = []
        if folder and folder.is_dir():
            for f in sorted(folder.rglob("*")):
                if f.is_file() and f.suffix.lower() in (".mkv", ".strm"):
                    files.append({"id": len(files) + 1, "path": show["path"] + "/" + f.relative_to(folder).as_posix(),
                                  "relativePath": f.relative_to(folder).as_posix()})
        return files

    @classmethod
    def local(cls, sonarr_path):
        for root, local in cls.disk.items():
            if sonarr_path.startswith(root + "/"):
                return Path(str(local) + sonarr_path[len(root):])
        return None

    # requests.Session
    def get(self, url, params=None, timeout=None):
        sid = int(url.rsplit("/", 1)[1])
        return _Resp(200, dict(next(s for s in self.series if s["id"] == sid)))

    def put(self, url, params=None, json=None, timeout=None):
        sid = int(url.rsplit("/", 1)[1])
        FakeSonarr.puts.append((sid, json["path"], (params or {}).get("moveFiles")))
        if self.move_fails:
            return _Resp(500)
        show = next(s for s in self.series if s["id"] == sid)
        src, dst = self.local(show["path"]), self.local(json["path"])
        if (params or {}).get("moveFiles") == "true" and src is not None and src.is_dir():
            _sonarr_move(src, dst)
        show["path"] = json["path"]
        return _Resp(202, dict(show))


def _sonarr_move(src: Path, dst: Path):
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return
    for d in sorted(p for p in src.rglob("*") if p.is_dir()):
        (dst / d.relative_to(src)).mkdir(parents=True, exist_ok=True)
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        os.replace(f, dst / f.relative_to(src))     # Sonarr overwrites a file of the same name
    shutil.rmtree(src)


def _tree(root: Path) -> list:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


class _Base(unittest.TestCase):
    def setUp(self):
        no_tmdb(self)
        import models.database as mdb
        self.mdb = mdb
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("sonarr_url", "http://sonarr"), ("sonarr_api_key", "k"), ("data_dir", str(self.tmp))):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        self.shows = self.tmp / "shows"
        self.vodroot = self.tmp / "vod" / "shows"
        self.shows.mkdir(parents=True)
        self.vodroot.mkdir(parents=True)
        import services.media_files as media_files
        import services.sync as sync
        mock.patch.object(media_files, "SONARR_MEDIA_ROOT", self.shows).start()
        mock.patch.object(sync, "VOD_SERIES_ROOT", self.vodroot).start()
        mock.patch("services.one_series.MOVE_POLL_SECONDS", 0).start()
        mock.patch("services.one_series.MOVE_WAIT_SECONDS", 2).start()
        self.worker = mock.patch("services.duplicates.start_pending_user_data_worker").start()
        self.addCleanup(mock.patch.stopall)
        FakeSonarr.series, FakeSonarr.puts, FakeSonarr.move_fails = [], [], False
        FakeSonarr.disk = {"/data/tv": self.shows, "/data/vod/Series": self.vodroot}
        self.vod = self.vodroot / VOD_NAME
        self.arr = self.shows / ARR_NAME

    # The show: VOD S01 E1-2 and S05 E1-5; Sonarr has S05 E1-6 downloaded.
    def vod_copy(self, eps=(("01", "01"), ("01", "02"), ("05", "01"), ("05", "02"), ("05", "03"),
                            ("05", "04"), ("05", "05"))):
        for s, e in eps:
            d = self.vod / f"Season {s}"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{VOD_NAME} S{s}E{e}.strm").write_text(f"http://provider/series/u/p/{s}{e}.mp4")
        (self.vod / "tvshow.nfo").write_text("<tvshow><title>vod</title></tvshow>")
        row = self.mdb.Series(tmdb_id=TMDB, title="Special Forces: World's Toughest Test", year="2023",
                              source="provider_1", provider_id=1, strm_path=str(self.vod),
                              nfo_path=str(self.vod / "tvshow.nfo"), tags=["Disney+ TV", "Downloaded TV"])
        self.db.add(row)
        self.db.commit()
        return row

    def download(self, folder=None, eps=range(1, 7)):
        folder = folder or self.arr
        d = folder / "Season 5"
        d.mkdir(parents=True, exist_ok=True)
        for e in eps:
            (d / f"Special.Forces.Worlds.Toughest.Test.S05E{e:02d}.1080p.WEB.h264-EDITH.mkv").write_bytes(b"\0" * 32)
        (folder / "tvshow.nfo").write_text("<tvshow><title>download</title></tvshow>")

    def sonarr_show(self, path, files=6):
        FakeSonarr.series = [{"id": 63, "tmdbId": TMDB, "tvdbId": 424437, "title": "Special Forces",
                              "path": path, "statistics": {"episodeFileCount": files}}]

    def unify(self):
        from services import one_series
        out = one_series.unify(self.db, FakeSonarr(), FakeSonarr().get_all_series())
        self.db.expire_all()
        return out

    def row(self):
        return self.db.query(self.mdb.Series).filter_by(tmdb_id=TMDB).one()

    def dups(self):
        return self.db.query(self.mdb.Duplicate).filter_by(tmdb_id=TMDB, media_type="series").all()


class OneFolder(_Base):
    def test_vod_after_sonarr_moves_the_download_into_the_vod_folder(self):
        """Special Forces: Sonarr had it in /data/tv, the VOD copy came later."""
        self.vod_copy()
        self.download()
        self.db.add(self.mdb.Duplicate(tmdb_id=TMDB, media_type="series", resolution="pending",
                                       sources=[{"source": "sonarr", "path": f"/data/tv/{ARR_NAME}"},
                                                {"source": "provider_1", "path": str(self.vod)}]))
        self.db.commit()
        self.sonarr_show(f"/data/tv/{ARR_NAME}")

        out = self.unify()

        self.assertEqual(1, out["moved"], out)
        self.assertEqual([(63, f"/data/vod/Series/{VOD_NAME}", "true")], FakeSonarr.puts)
        self.assertFalse(self.arr.exists(), "Sonarr's old folder is still there: Jellyfin keeps a second series")
        files = _tree(self.vod)
        for e in range(1, 7):
            self.assertIn(f"Season 5/Special.Forces.Worlds.Toughest.Test.S05E{e:02d}.1080p.WEB.h264-EDITH.mkv", files)
        # Every stream copy is still there; the downloaded ones are parked.
        for e in range(1, 6):
            self.assertIn(f"Season 05/{VOD_NAME} S05E{e:02d}.strm.parked", files)
        self.assertIn(f"Season 01/{VOD_NAME} S01E01.strm", files)
        self.assertIn(f"Season 01/{VOD_NAME} S01E02.strm", files)
        row = self.row()
        self.assertEqual(f"/data/vod/Series/{VOD_NAME}", row.sonarr_path)
        self.assertEqual(str(self.vod / "tvshow.nfo"), row.nfo_path)
        self.assertEqual([("keep_both", "one_series")], [(d.resolution, d.resolved_by) for d in self.dups()])

    def test_every_episode_is_kept_and_plays_once(self):
        """Partly downloaded (#800/#774): no episode is lost, none shows twice."""
        self.vod_copy()
        self.download(eps=(1, 2, 6))
        self.sonarr_show(f"/data/tv/{ARR_NAME}", files=3)
        self.unify()
        from services.one_series import episode_sources
        src = episode_sources(self.vod)
        self.assertEqual({"S01E01", "S01E02", "S05E01", "S05E02", "S05E03", "S05E04", "S05E05", "S05E06"}, set(src))
        visible = [f for f in _tree(self.vod) if f.endswith((".strm", ".mkv"))]
        keys = [k for f in visible for k in __import__("services.duplicates", fromlist=["x"])._episode_keys(f)]
        self.assertEqual(sorted(set(keys)), sorted(keys), "an episode Jellyfin would list twice")
        self.assertEqual(8, len(keys))

    def test_sonarr_already_in_the_vod_folder_moves_nothing(self):
        """Requested from Tentacle after the VOD copy existed (the old path)."""
        self.vod_copy()
        self.download(folder=self.vod, eps=(5,))
        self.sonarr_show(f"/data/vod/Series/{VOD_NAME}", files=1)
        out = self.unify()
        self.assertEqual([], FakeSonarr.puts)
        self.assertEqual(1, out["parked"])
        self.assertTrue((self.vod / "Season 05" / f"{VOD_NAME} S05E05.strm.parked").exists())

    def test_a_show_added_in_sonarr_with_nothing_downloaded_yet_is_pointed_there(self):
        self.vod_copy()
        self.sonarr_show(f"/data/tv/{ARR_NAME}", files=0)
        out = self.unify()
        self.assertEqual(1, out["pointed"], out)
        self.assertEqual(f"/data/vod/Series/{VOD_NAME}", FakeSonarr.series[0]["path"])
        self.assertEqual(f"/data/vod/Series/{VOD_NAME}", self.row().sonarr_path)

    def test_twice_is_once(self):
        self.vod_copy()
        self.download()
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        self.unify()
        before = _tree(self.vod)
        out = self.unify()
        self.assertEqual(0, out["moved"])
        self.assertEqual(1, len(FakeSonarr.puts))
        self.assertEqual(before, _tree(self.vod))


class NothingIsOverwrittenOrDeleted(_Base):
    def test_a_file_in_both_folders_stops_the_move(self):
        self.vod_copy()
        self.download()
        (self.vod / "Season 5").mkdir()
        clash = self.vod / "Season 5" / "Special.Forces.Worlds.Toughest.Test.S05E01.1080p.WEB.h264-EDITH.mkv"
        clash.write_bytes(b"vod side")
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        before = (_tree(self.vod), _tree(self.arr))
        out = self.unify()
        self.assertEqual([], FakeSonarr.puts, "Sonarr's move would have overwritten it")
        self.assertEqual(1, len(out["blocked"]))
        self.assertIn("in both folders", out["blocked"][0])
        self.assertEqual(before, (_tree(self.vod), _tree(self.arr)))
        self.assertEqual(b"vod side", clash.read_bytes())

    def test_stream_files_in_sonarrs_folder_stop_the_move(self):
        self.vod_copy()
        self.download()
        (self.arr / "Season 5" / "Old S05E07.strm").write_text("http://x")
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        out = self.unify()
        self.assertEqual([], FakeSonarr.puts)
        self.assertIn("stream file", out["blocked"][0])

    def test_only_tvshow_nfo_may_be_replaced(self):
        """Tentacle writes tvshow.nfo on both sides; the row's tags go back into it."""
        self.vod_copy()
        self.download()
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        self.unify()
        nfo = (self.vod / "tvshow.nfo").read_text()
        self.assertIn("<tag>Disney+ TV</tag>", nfo)

    def test_no_vod_root_folder_in_sonarr_moves_nothing(self):
        self.vod_copy()
        self.download()
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        FakeSonarr.roots = [{"path": "/data/tv"}]
        self.addCleanup(setattr, FakeSonarr, "roots", [{"path": "/data/tv"}, {"path": "/data/vod/Series"}])
        out = self.unify()
        self.assertEqual([], FakeSonarr.puts)
        self.assertIn("no root folder on the VOD shows directory", out["blocked"][0])

    def test_a_failed_move_changes_nothing_on_disk_and_is_tried_again(self):
        self.vod_copy()
        self.download()
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        FakeSonarr.move_fails = True
        before = (_tree(self.vod), _tree(self.arr))
        out = self.unify()
        self.assertEqual(0, out["moved"])
        self.assertEqual(before, (_tree(self.vod), _tree(self.arr)))
        FakeSonarr.move_fails = False
        self.assertEqual(1, self.unify()["moved"])

    def test_a_folder_already_moved_by_hand_is_only_repointed(self):
        """Renamed or moved outside Sonarr: the downloads are in the VOD folder, Sonarr's path is stale."""
        self.vod_copy()
        self.download(folder=self.vod)
        self.sonarr_show(f"/data/tv/{ARR_NAME}")   # that folder no longer exists
        FakeSonarr.series[0]["path"] = f"/data/vod/Series/{VOD_NAME}"
        files = FakeSonarr().get_episode_files(63)
        FakeSonarr.series[0]["path"] = f"/data/tv/{ARR_NAME}"
        with mock.patch.object(FakeSonarr, "get_episode_files", lambda self, sid: files):
            out = self.unify()
        self.assertEqual(1, out["pointed"], out)
        self.assertEqual(f"/data/vod/Series/{VOD_NAME}", FakeSonarr.series[0]["path"])

    def test_sonarrs_folder_out_of_sight_with_downloads_moves_nothing(self):
        self.vod_copy()
        self.sonarr_show("/elsewhere/Special Forces", files=6)
        out = self.unify()
        self.assertEqual([], FakeSonarr.puts)
        self.assertIn("can't see Sonarr's folder", out["blocked"][0])


class StreamCopies(_Base):
    def test_a_download_deleted_later_brings_the_stream_back(self):
        self.vod_copy()
        self.download()
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        self.unify()
        (self.vod / "Season 5" / "Special.Forces.Worlds.Toughest.Test.S05E03.1080p.WEB.h264-EDITH.mkv").unlink()
        out = self.unify()
        self.assertEqual(1, out["restored"])
        self.assertTrue((self.vod / "Season 05" / f"{VOD_NAME} S05E03.strm").exists())
        self.assertFalse((self.vod / "Season 05" / f"{VOD_NAME} S05E03.strm.parked").exists())

    def test_the_vod_sync_keeps_a_parked_copy_and_writes_no_second_one(self):
        import services.sync as sync
        self.vod_copy()
        parked = self.vod / "Season 05" / f"{VOD_NAME} S05E01.strm.parked"
        (self.vod / "Season 05" / f"{VOD_NAME} S05E01.strm").rename(parked)

        class Client:
            def episode_stream_url(self, ep_id, ext):
                return f"http://provider/series/u/p/{ep_id}.{ext}"

        with mock.patch.object(sync, "_strm_plays_other_provider", return_value=False), \
                mock.patch.object(sync, "_strm_needs_rewrite", return_value=True):
            new = sync._write_episode_strms(Client(), {"5": [{"id": 9001, "episode_num": 1}]},
                                            self.vod, VOD_NAME, slots=sync._EpisodeSlots())
        self.assertEqual(0, new)
        self.assertFalse((self.vod / "Season 05" / f"{VOD_NAME} S05E01.strm").exists())
        self.assertEqual("http://provider/series/u/p/9001.mp4", parked.read_text().strip())

    def test_the_provider_dropping_the_show_removes_parked_copies_not_downloads(self):
        from services.media_files import delete_series_files
        self.vod_copy()
        self.download(folder=self.vod)
        from services.one_series import settle_episode_copies
        settle_episode_copies(self.vod)
        delete_series_files(self.vod)
        left = _tree(self.vod)
        self.assertFalse([f for f in left if ".strm" in f])
        self.assertEqual(6, len([f for f in left if f.endswith(".mkv")]))

    def test_parked_copies_are_not_downloads_and_still_count_as_the_vod_copy(self):
        from services.duplicates import is_downloaded_file, vod_copy_on_disk
        from services.sync import _holds_own_file
        self.assertFalse(is_downloaded_file("/x/Show S01E01.strm.parked"))
        d = self.vodroot / "Only Parked (2020)" / "Season 01"
        d.mkdir(parents=True)
        (d / "Only Parked (2020) S01E01.strm.parked").write_text("http://x")
        self.assertTrue(_holds_own_file(d.parent))
        dup = self.mdb.Duplicate(tmdb_id=1, media_type="series",
                                 sources=[{"source": "provider_1", "path": str(d.parent)}])
        self.assertTrue(vod_copy_on_disk(dup, None))


class SonarrScanRunsIt(_Base):
    def test_the_sonarr_scan_puts_the_show_into_one_folder(self):
        """Nightly, webhooks (OnImportComplete, SeriesAdd) and Scan now all run the scan."""
        import services.sonarr as sonarr
        self.vod_copy()
        self.download()
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        FakeSonarr.series[0]["monitorNewItems"] = "all"
        with mock.patch.object(sonarr, "SonarrService", FakeSonarr), \
                mock.patch.object(sonarr, "series_has_real_download", return_value=True):
            out = sonarr.scan_sonarr_library(self.db)
        self.assertEqual(1, out["one_series"]["moved"], out)
        self.assertFalse(self.arr.exists())
        self.assertEqual("keep_both", self.dups()[0].resolution)


class DownloadedFirst(_Base):
    """A show Sonarr downloaded before the provider offered it gets its VOD episodes too."""

    def test_the_vod_sync_adopts_the_downloaded_show(self):
        import services.sync as sync
        sonarr_row = self.mdb.Series(tmdb_id=TMDB, title="Special Forces", source="sonarr",
                                     sonarr_path=f"/data/tv/{ARR_NAME}", tags=["Downloaded TV"])
        self.db.add(sonarr_row)
        self.db.commit()
        provider = self.mdb.Provider(id=1, name="P", priority=1)
        answer = sync.check_and_record_duplicate(TMDB, "series", "provider_1", str(self.vod), provider, self.db)
        self.assertEqual(sync.ADOPT, answer)
        self.assertEqual([], self.dups(), "no duplicate to choose: it becomes one show")

    def test_keep_downloaded_is_respected(self):
        import services.sync as sync
        self.db.add(self.mdb.Series(tmdb_id=TMDB, title="Special Forces", source="sonarr",
                                    sonarr_path=f"/data/tv/{ARR_NAME}"))
        self.db.add(self.mdb.Duplicate(tmdb_id=TMDB, media_type="series", resolution="keep_radarr", sources=[]))
        self.db.commit()
        provider = self.mdb.Provider(id=1, name="P", priority=1)
        self.assertIs(True, sync.check_and_record_duplicate(TMDB, "series", "provider_1", str(self.vod),
                                                            provider, self.db))

    def test_films_are_not_adopted(self):
        import services.sync as sync
        self.db.add(self.mdb.Movie(tmdb_id=77, title="Film", source="radarr", radarr_path="/data/movies/F/F.mkv"))
        self.db.commit()
        provider = self.mdb.Provider(id=1, name="P", priority=1)
        self.assertIs(True, sync.check_and_record_duplicate(77, "movie", "provider_1", "/media/vod/movies/F/F.strm",
                                                            provider, self.db))


class ManageEpisodesSources(_Base):
    """Manage Episodes says per episode: downloaded, stream, both, missing, not aired."""

    def test_sources(self):
        from routers.discover import with_episode_sources
        self.vod_copy()
        self.download(folder=self.vod, eps=(1, 6))
        from services.one_series import settle_episode_copies
        settle_episode_copies(self.vod)
        self.sonarr_show(f"/data/vod/Series/{VOD_NAME}", files=7)
        files = FakeSonarr().get_episode_files(63)
        by_name = {f["relativePath"].rsplit("/", 1)[1]: f["id"] for f in files}

        def ep(s, e, name=None, air="2024-01-01T00:00:00Z"):
            return {"id": s * 100 + e, "seasonNumber": s, "episodeNumber": e, "airDateUtc": air,
                    "hasFile": name is not None, "episodeFileId": by_name.get(name, 0)}

        mkv = "Special.Forces.Worlds.Toughest.Test.S05E{:02d}.1080p.WEB.h264-EDITH.mkv"
        episodes = [ep(5, 1, mkv.format(1)), ep(5, 2, f"{VOD_NAME} S05E02.strm"), ep(5, 6, mkv.format(6)),
                    ep(5, 7), ep(5, 8, air="2999-01-01T00:00:00Z")]
        out = {e["episodeNumber"]: e for e in with_episode_sources(self.db, TMDB, FakeSonarr(), 63, episodes)}
        self.assertEqual("both", out[1]["source"])
        self.assertEqual("stream", out[2]["source"])
        self.assertFalse(out[2]["hasFile"], "a .strm is no download")
        self.assertTrue(out[2]["sonarrHasFile"])
        self.assertEqual("download", out[6]["source"])
        self.assertEqual("missing", out[7]["source"])
        self.assertEqual("upcoming", out[8]["source"])

    def test_vod_episodes_include_parked_copies(self):
        from routers.discover import get_vod_episodes
        self.vod_copy()
        self.download(folder=self.vod, eps=(1,))
        from services.one_series import settle_episode_copies
        settle_episode_copies(self.vod)
        out = get_vod_episodes(TMDB, self.db)
        self.assertEqual([1, 2, 3, 4, 5], out["episodes"][5])


class WatchedStateFollows(_Base):
    """Users' played marks, resume points and favourites on the download's
    old items end up on the items Jellyfin makes for the moved files."""

    def setUp(self):
        super().setUp()
        from tests.test_duplicate_keeps_user_data import FakeJellyfin
        self.jf = FakeJellyfin

        class JF(FakeJellyfin):
            notified = []

            def notify_media_updated(self, paths, update_type="Created"):
                JF.notified.append((list(paths), update_type))
                return True

            def trigger_library_scan(self, library_id=None):
                return True

            def post(self, url, params=None, json=None, timeout=None):
                if url.endswith("/Refresh"):
                    JF.refreshed.append(url.rsplit("/", 2)[1])
                    return _Resp(204)
                return super().post(url, params, json, timeout)

            def get(self, url, params=None, timeout=None):
                if (params or {}).get("ParentId") in JF.gone:
                    return _Resp(400)   # Jellyfin lists a removed show for a while; its episodes: 400
                if url.endswith("/Sessions"):
                    if self.down:
                        raise ConnectionError("Jellyfin is down")
                    return _Resp(200, [{"NowPlayingItem": i} for i in JF.playing])
                return super().get(url, params, timeout)

        JF.playing, JF.refreshed, JF.gone = [], [], set()

        self.JF = JF
        FakeJellyfin.items, FakeJellyfin.data, FakeJellyfin.posts = [], {}, []
        FakeJellyfin.users, FakeJellyfin.down, FakeJellyfin.on_post = ["A", "B"], False, None
        for k, v in (("jellyfin_url", "http://jf"), ("jellyfin_api_key", "j")):
            self.mdb.set_setting(self.db, k, v)
        self.db.commit()
        mock.patch("services.jellyfin.JellyfinService", JF).start()

    def test_played_resume_and_favourite_carry_over(self):
        from services import duplicates as dup_service
        self.vod_copy()
        self.download(eps=(5, 6))
        self.sonarr_show(f"/data/tv/{ARR_NAME}", files=2)
        jv, ja = f"/data/media/vod/Series/{VOD_NAME}", f"/data/media/tv/{ARR_NAME}"
        mkv = "Special.Forces.Worlds.Toughest.Test.S05E{:02d}.1080p.WEB.h264-EDITH.mkv"
        self.jf.items = [
            {"Id": "vod", "Type": "Series", "Path": jv, "ProviderIds": {"Tmdb": str(TMDB)}},
            {"Id": "dl", "Type": "Series", "Path": ja, "ProviderIds": {"Tmdb": str(TMDB)}},
            {"Id": "s5e5", "Type": "Episode", "ParentId": "vod", "Path": f"{jv}/Season 05/{VOD_NAME} S05E05.strm",
             "ParentIndexNumber": 5, "IndexNumber": 5},
            {"Id": "d5", "Type": "Episode", "ParentId": "dl", "Path": f"{ja}/Season 5/{mkv.format(5)}",
             "ParentIndexNumber": 5, "IndexNumber": 5},
            {"Id": "d6", "Type": "Episode", "ParentId": "dl", "Path": f"{ja}/Season 5/{mkv.format(6)}",
             "ParentIndexNumber": 5, "IndexNumber": 6},
        ]
        self.jf.data = {("A", "s5e5"): {"Played": True, "PlayCount": 1},
                        ("A", "d6"): {"PlaybackPositionTicks": 6_761_970_000},
                        ("B", "dl"): {"IsFavorite": True}}

        out = self.unify()
        self.assertEqual(1, out["moved"], out)
        self.assertEqual({"IsFavorite": True}, self.jf.data[("B", "vod")], "the favourite show")
        self.assertEqual(([ja], "Deleted"), self.JF.notified[0])
        self.assertEqual(["vod"], self.JF.refreshed, "Jellyfin makes items of the moved files only on a refresh")

        # Jellyfin's scan: the download's old items go, the moved files get new ones in the VOD show.
        # It still lists the old show for a while, and answers 400 for its episodes.
        self.JF.gone = {"dl"}
        self.jf.items = [
            {"Id": "vod", "Type": "Series", "Path": jv, "ProviderIds": {"Tmdb": str(TMDB)}},
            {"Id": "dl", "Type": "Series", "Path": ja, "ProviderIds": {"Tmdb": str(TMDB)}},
            {"Id": "n5", "Type": "Episode", "ParentId": "vod", "Path": f"{jv}/Season 5/{mkv.format(5)}",
             "ParentIndexNumber": 5, "IndexNumber": 5},
            {"Id": "n6", "Type": "Episode", "ParentId": "vod", "Path": f"{jv}/Season 5/{mkv.format(6)}",
             "ParentIndexNumber": 5, "IndexNumber": 6},
        ]
        self.assertEqual(0, dup_service.apply_pending_user_data(self.db))
        self.assertTrue(self.jf.data[("A", "n5")]["Played"], "watched as a stream, now the download")
        self.assertEqual(6_761_970_000, self.jf.data[("A", "n6")]["PlaybackPositionTicks"], "Continue Watching")

    def test_nothing_moves_under_someone_watching_it(self):
        self.vod_copy()
        self.download()
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        ja = f"/data/media/tv/{ARR_NAME}"
        self.jf.items = [{"Id": "dl", "Type": "Series", "Path": ja, "ProviderIds": {"Tmdb": str(TMDB)}}]
        self.JF.playing = [{"Id": "x", "SeriesId": "dl", "Path": f"{ja}/Season 5/E6.mkv"}]
        out = self.unify()
        self.assertEqual([], FakeSonarr.puts, "the file would vanish under the player")
        self.assertIn("someone is watching it", out["blocked"][0])
        self.JF.playing = []
        self.assertEqual(1, self.unify()["moved"])

    def test_jellyfin_down_moves_nothing(self):
        self.vod_copy()
        self.download()
        self.sonarr_show(f"/data/tv/{ARR_NAME}")
        self.jf.items = [{"Id": "dl", "Type": "Series", "Path": f"/data/media/tv/{ARR_NAME}",
                          "ProviderIds": {"Tmdb": str(TMDB)}}]
        self.jf.down = True
        out = self.unify()
        self.assertEqual([], FakeSonarr.puts, "moved without saving users' watched state")
        self.assertEqual(1, len(out["blocked"]))


if __name__ == "__main__":
    unittest.main()
