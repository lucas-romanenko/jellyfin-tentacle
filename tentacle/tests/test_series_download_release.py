"""Series side of #378/#549: a VOD show whose Sonarr download goes away (#617).

Run from tentacle/:  python tests/hermetic.py discover -s tests -p test_series_download_release.py

A VOD show (row source provider_1) that a user also downloads through Sonarr
gets "Downloaded TV" and the requester's "<name>'s Downloads" from the Sonarr
scan, and a download date. When the download goes (Sonarr SeriesDelete, the
show leaves Sonarr or loses its episode files and the next scan runs, or Keep
VOD), the row is the VOD show only again: it must no longer claim the
download (no "Downloaded TV", no downloaded_at, its NFO the VOD show's), or
Refresh Tags puts it on the VOD show and it stays in every user's Downloaded
TV playlist for good. Films were fixed in #378 (scan + webhooks) and #549
(Keep VOD).

A VOD show gets sonarr_path as soon as it is added to Sonarr, before any
download: a show requested but not downloaded yet (downloaded_at empty) is not
released by the scan.
"""
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import DownloadRequest, Duplicate, Series, TentacleUser, set_setting  # noqa: E402
import routers.sonarr as sonarr_router  # noqa: E402
import services.sonarr as sonarr_svc  # noqa: E402
from tests import test_duplicate_keep_vod_delete_webhook as keep_vod  # noqa: E402
from tests.test_duplicate_keep_vod_merged_folder import _FakeArr  # noqa: E402
from tests.test_duplicate_keeps_user_data import setUpModule, tearDownModule  # noqa: E402,F401

TMDB = 990077

VOD_NFO = """<?xml version="1.0" encoding="utf-8"?>
<tvshow>
  <title>Show</title>
  <tag>Netflix TV</tag>
</tvshow>
"""


class HybridShowLosesItsDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        set_setting(self.db, "sonarr_url", "http://sonarr")
        set_setting(self.db, "sonarr_api_key", "k")
        set_setting(self.db, "data_dir", str(self.tmp))
        user = TentacleUser(jellyfin_user_id="u1", display_name="requester", is_admin=True)
        self.db.add(user)
        self.db.commit()
        self.user_id = user.id
        self.vod = self.tmp / "vod" / "shows" / "Show (2020)"
        (self.vod / "Season 01").mkdir(parents=True)
        (self.vod / "Season 01" / "Show (2020) S01E01.strm").write_text("http://p/series/1.mp4")
        self.vod_nfo = self.vod / "tvshow.nfo"
        self.vod_nfo.write_text(VOD_NFO)
        self.dl = self.tmp / "tv" / "Show (2020)"
        (self.dl / "Season 01").mkdir(parents=True)
        (self.dl / "Season 01" / "Show (2020) S01E01.mkv").write_bytes(b"\0" * 64)
        self.db.add(Series(tmdb_id=TMDB, title="Show", year="2020", source="provider_1", provider_id=1,
                           source_tag="Netflix", strm_path=str(self.vod), nfo_path=str(self.vod_nfo),
                           tags=["Netflix TV"]))
        self.db.add(DownloadRequest(tmdb_id=TMDB, media_type="series", user_id=user.id))
        self.db.commit()
        self.listed = [self._show(files=1), self._other()]
        for target, value in (
                ("services.sonarr.SonarrService.get_all_series", lambda s: self.listed),
                ("services.sonarr.series_has_real_download", lambda *a, **k: True),
                ("services.tmdb.get_tmdb_token", lambda *a, **k: None),
                ("routers.sonarr._check_webhook_auth", lambda *a, **k: None),
                ("routers.sonarr.emit_library_event", lambda *a, **k: None),
                ("routers.sonarr.log_activity", lambda *a, **k: None),
                ("routers.library._cleanup_playlists_all_users", lambda *a, **k: None),
                ("threading.Thread.start", lambda self_: None)):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def _show(self, files, tmdb_id=TMDB, sonarr_id=5, path=None):
        return {"id": sonarr_id, "tmdbId": tmdb_id, "tvdbId": sonarr_id, "title": "Show", "year": 2020,
                "path": path or str(self.dl), "added": "2026-09-01T00:00:00Z", "monitorNewItems": "all",
                "statistics": {"episodeFileCount": files}}

    def _other(self):
        return {"id": 6, "tmdbId": 990078, "tvdbId": 2, "title": "Other", "year": 2019,
                "path": str(self.tmp / "tv" / "Other (2019)"), "statistics": {"episodeFileCount": 0}}

    def scan(self):
        try:
            return sonarr_svc._scan_sonarr_library(self.db)
        finally:
            self.db.expire_all()

    def row(self, tmdb_id=TMDB):
        self.db.expire_all()
        return self.db.query(Series).filter(Series.tmdb_id == tmdb_id).one()

    def claims(self):
        r = self.row()
        return {"sonarr_path": r.sonarr_path, "downloaded_at": r.downloaded_at, "nfo_path": r.nfo_path,
                "Downloaded TV": "Downloaded TV" in (r.tags or []),
                "requester's Downloads": "requester's Downloads" in (r.tags or []),
                "Netflix": "Netflix" in (r.tags or []) or "Netflix TV" in (r.tags or []),
                "requests": self.db.query(DownloadRequest).count()}

    def downloaded_first(self):
        stats = self.scan()
        before = self.claims()
        print(f"\nafter the download scan: {stats} -> {before}")
        self.assertTrue(before["Downloaded TV"], f"precondition: the scan tags the hybrid show {before}")
        self.assertIsNotNone(before["downloaded_at"], f"precondition: the scan dates the download {before}")
        return before

    def assert_vod_only(self, after):
        self.assertFalse(after["Downloaded TV"], f"VOD-only show still tagged Downloaded TV: {after}")
        self.assertFalse(after["requester's Downloads"], f"still in the requester's Downloads: {after}")
        self.assertIsNone(after["downloaded_at"], f"VOD-only show still has a download date: {after}")
        self.assertTrue(after["Netflix"], f"the VOD show's own source tag went too: {after}")
        self.assertEqual(str(self.vod_nfo), after["nfo_path"], f"still the download's NFO: {after}")
        self.assertNotIn("<tag>Downloaded TV</tag>", self.vod_nfo.read_text(),
                         "the VOD show's tvshow.nfo brings the tag back")
        self.assertEqual(0, after["requests"], f"the download's request stays: {after}")

    def series_delete(self):
        sonarr_router.sonarr_webhook({"eventType": "SeriesDelete", "series": {
            "tmdbId": TMDB, "title": "Show", "path": str(self.dl)}}, None, self.db)

    def test_series_delete_webhook(self):
        self.downloaded_first()
        self.series_delete()
        after = self.claims()
        print(f"after SeriesDelete: {after}")
        self.assertEqual(after["sonarr_path"], None)
        self.assert_vod_only(after)

    def test_refresh_tags_puts_it_back_on_the_vod_show(self):
        """What Jellyfin gets: with the download gone, the TMDB id is one item
        again (the VOD show), and Refresh Tags writes the row's tags onto it."""
        self.downloaded_first()
        self.series_delete()
        from services.jellyfin import sync_owned_tags
        written = {}

        class FakeJellyfin:
            def get_tmdb_lookup_with_fallback(self, media_type, with_counts=False):
                if media_type != "Series":
                    return {}, {}, {}
                return {TMDB: {"Id": "vod-show", "Tags": ["Netflix TV"]}}, {}, {TMDB: 1}

            def set_item_owned_tags(self, item_id, desired, owned, add_only=False):
                written[item_id] = list(desired)
                return "written"

        sync_owned_tags(self.db, FakeJellyfin(), "Refresh")
        print(f"Refresh Tags wrote: {written}")
        self.assertNotIn("Downloaded TV", written.get("vod-show", []),
                         f"the VOD show (no download any more) is put in Downloaded TV: {written}")

    def test_show_left_sonarr_then_scan(self):
        self.downloaded_first()
        self.listed = [self._other()]
        stats = self.scan()
        after = self.claims()
        print(f"after the show left Sonarr + scan: {stats} -> {after}")
        self.assertIsNone(after["sonarr_path"], f"still points at the deleted download: {after}")
        self.assert_vod_only(after)
        self.assertEqual(1, stats.get("released"), f"the scan does not count the released show: {stats}")

    def test_files_deleted_show_still_in_sonarr_then_scan(self):
        """The show stays in Sonarr: it keeps sonarr_path (an intentional add,
        so a later download is no duplicate), only the download's claims go."""
        self.downloaded_first()
        self.listed = [self._show(files=0), self._other()]
        self.scan()
        after = self.claims()
        print(f"after the episode files went (show still in Sonarr) + scan: {after}")
        self.assertEqual(str(self.dl), after["sonarr_path"], f"still in Sonarr, sonarr_path must stay: {after}")
        self.assert_vod_only(after)

    def test_requested_not_downloaded_yet_is_left_alone(self):
        """Add to Sonarr on a VOD show sets sonarr_path at once, while Sonarr
        still searches: no files, no downloaded_at. Not a lost download."""
        row = self.row()
        row.sonarr_path = str(self.dl)
        self.db.commit()
        self.listed = [self._show(files=0), self._other()]
        self.scan()
        after = self.claims()
        self.assertEqual(str(self.dl), after["sonarr_path"], f"a pending request lost its Sonarr path: {after}")
        self.assertEqual(1, after["requests"], f"a pending request was dropped: {after}")
        self.assertIsNone(after["downloaded_at"])
        self.assertEqual(0, self.db.query(Duplicate).count(), "duplicate detection came back for it")

    def test_many_vod_shows_losing_files_at_once_is_an_outage(self):
        """The storage guard (#106, per kind #505) covers VOD shows' downloads too."""
        shows = [(TMDB, 5, self.dl)]
        for n in (1, 2):
            dl = self.tmp / "tv" / f"Show {n} (2020)"
            (dl / "Season 01").mkdir(parents=True)
            shows.append((TMDB + 10 * n, 50 + n, dl))
            self.db.add(Series(tmdb_id=TMDB + 10 * n, title=f"Show {n}", year="2020", source="provider_1",
                               provider_id=1, strm_path=str(self.tmp / "vod" / f"Show {n}"), tags=["Netflix TV"]))
        self.db.commit()
        self.listed = [self._show(1, t, i, str(p)) for t, i, p in shows] + [self._other()]
        self.scan()
        self.listed = [self._show(0, t, i, str(p)) for t, i, p in shows] + [self._other()]
        stats = self.scan()
        for t, _, _ in shows:
            r = self.row(t)
            self.assertIn("Downloaded TV", r.tags or [], f"released during a storage outage: {stats}")
            self.assertIsNotNone(r.downloaded_at)
        self.assertEqual(3, stats.get("removals_refused"), stats)


class KeepVodReleasesTheSeriesDownload(keep_vod._Base):
    """Keep VOD on a VOD show that Sonarr downloaded too (one row, source
    provider_1, with sonarr_path), with no SeriesDelete webhook (Sonarr's
    Connect not set up for deletes): Keep VOD itself releases the row."""
    def setUp(self):
        super().setUp()
        self.show = self.root / "vod" / "shows" / "Show (2010)"
        (self.show / "Season 01").mkdir(parents=True)
        (self.show / "Season 01" / "Show S01E01.strm").write_text("http://p/series/1.mp4")
        self.vod_nfo = self.show / "tvshow.nfo"
        self.vod_nfo.write_text(VOD_NFO.replace("Netflix TV", "Netflix TV</tag>\n  <tag>Downloaded TV"))
        dl = self.root / "tv" / "Show (2010) [sonarr]"
        (dl / "Season 01").mkdir(parents=True)
        self.mkv = dl / "Season 01" / "Show - S01E02.mkv"
        self.mkv.write_bytes(b"\0" * 64)
        (dl / "tvshow.nfo").write_text(VOD_NFO)
        _FakeArr.titles[1418] = {"id": 5, "path": str(dl), "files": [{"id": 50, "path": str(self.mkv)}]}
        user = TentacleUser(jellyfin_user_id="u1", display_name="requester")
        self.db.add(user)
        self.db.commit()
        self.db.add(Series(tmdb_id=1418, title="Show", year="2010", source="provider_1", provider_id=1,
                           source_tag="Netflix", strm_path=str(self.show), sonarr_path=str(dl),
                           nfo_path=str(dl / "tvshow.nfo"), downloaded_at=datetime(2026, 9, 1),
                           tags=["Netflix TV", "Downloaded TV", "requester's Downloads"]))
        dup = Duplicate(tmdb_id=1418, media_type="series", resolution="pending",
                        sources=[{"source": "sonarr", "path": str(dl)},
                                 {"source": "provider_1", "path": str(self.show)}])
        self.db.add(dup)
        self.db.add(DownloadRequest(tmdb_id=1418, media_type="series", user_id=user.id))
        self.db.commit()
        self.dup_id = dup.id

    def test_no_delete_webhook(self):
        with mock.patch.object(self, "sonarr_webhook", lambda payload: None):
            self.assertEqual({"success": True}, self.resolve(self.dup_id))
        self.assertFalse(self.mkv.exists())
        row = self.fresh().query(Series).one()
        self.assertEqual(("provider_1", str(self.show)), (row.source, row.strm_path), "the VOD show must stay")
        self.assertIsNone(row.sonarr_path)
        self.assertIsNone(row.downloaded_at, "the VOD show still claims the deleted download's date")
        self.assertEqual(str(self.vod_nfo), row.nfo_path, "still the download's NFO")
        self.assertNotIn("Downloaded TV", row.tags, "VOD-only show still tagged 'Downloaded TV'")
        self.assertNotIn("requester's Downloads", row.tags, "the request went with the show from Sonarr")
        self.assertIn("Netflix TV", row.tags)
        self.assertNotIn("<tag>Downloaded TV</tag>", self.vod_nfo.read_text())
        self.assertEqual(0, self.db.query(DownloadRequest).count(), "the show left Sonarr; its request stays")
        self.assert_recorded("series")


if __name__ == "__main__":
    unittest.main()
