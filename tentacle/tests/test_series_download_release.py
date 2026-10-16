"""Series side of #378/#549: a VOD show whose Sonarr download goes away.

A VOD show (row source provider_1) that a user also downloads through Sonarr
gets "Downloaded TV" and the requester's "<name>'s Downloads" from the Sonarr
scan. When the download goes (Sonarr SeriesDelete, or the show leaves Sonarr
and the next scan runs), the row is the VOD show only again: it should no
longer claim the download (no "Downloaded TV" tag, no downloaded_at), or it
stays in every user's Downloaded TV playlist for good. Films were fixed in
#378 (scan + webhooks) and #549 (Keep VOD).
"""
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import DownloadRequest, Series, TentacleUser, set_setting  # noqa: E402
import routers.sonarr as sonarr_router  # noqa: E402
import services.sonarr as sonarr_svc  # noqa: E402
from datetime import datetime  # noqa: E402
from models.database import Duplicate  # noqa: E402
from tests import test_duplicate_keep_vod_delete_webhook as keep_vod  # noqa: E402
from tests.test_duplicate_keep_vod_merged_folder import _FakeArr  # noqa: E402
from tests.test_duplicate_keeps_user_data import setUpModule, tearDownModule  # noqa: E402,F401

TMDB = 990077


class HybridShowLosesItsDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        set_setting(self.db, "sonarr_url", "http://sonarr")
        set_setting(self.db, "sonarr_api_key", "k")
        set_setting(self.db, "data_dir", str(self.tmp))
        user = TentacleUser(jellyfin_user_id="u1", display_name="requester", is_admin=True)
        self.db.add(user)
        self.db.commit()
        self.vod = self.tmp / "vod" / "shows" / "Show (2020)"
        (self.vod / "Season 01").mkdir(parents=True)
        (self.vod / "Season 01" / "Show (2020) S01E01.strm").write_text("http://p/series/1.mp4")
        self.dl = self.tmp / "tv" / "Show (2020)"
        (self.dl / "Season 01").mkdir(parents=True)
        (self.dl / "Season 01" / "Show (2020) S01E01.mkv").write_bytes(b"\0" * 64)
        self.db.add(Series(tmdb_id=TMDB, title="Show", year="2020", source="provider_1", provider_id=1,
                           source_tag="Netflix", strm_path=str(self.vod), tags=["Netflix TV"]))
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
            try:
                p = mock.patch(target, value)
                p.start()
                self.addCleanup(p.stop)
            except AttributeError:
                pass

    def _show(self, files):
        return {"id": 5, "tmdbId": TMDB, "tvdbId": 1, "title": "Show", "year": 2020, "path": str(self.dl),
                "added": "2026-09-01T00:00:00Z", "monitorNewItems": "all",
                "statistics": {"episodeFileCount": files}}

    def _other(self):
        return {"id": 6, "tmdbId": 990078, "tvdbId": 2, "title": "Other", "year": 2019,
                "path": str(self.tmp / "tv" / "Other (2019)"), "statistics": {"episodeFileCount": 0}}

    def scan(self):
        try:
            return sonarr_svc._scan_sonarr_library(self.db)
        finally:
            self.db.expire_all()

    def row(self):
        self.db.expire_all()
        return self.db.query(Series).filter(Series.tmdb_id == TMDB).one()

    def claims(self):
        r = self.row()
        return {"sonarr_path": r.sonarr_path, "downloaded_at": r.downloaded_at,
                "Downloaded TV": "Downloaded TV" in (r.tags or []),
                "requester's Downloads": "requester's Downloads" in (r.tags or []),
                "requests": self.db.query(DownloadRequest).count()}

    def downloaded_first(self):
        stats = self.scan()
        before = self.claims()
        print(f"\nafter the download scan: {stats} -> {before}")
        self.assertTrue(before["Downloaded TV"], f"precondition: the scan tags the hybrid show {before}")
        return before

    def test_series_delete_webhook(self):
        self.downloaded_first()
        sonarr_router.sonarr_webhook({"eventType": "SeriesDelete", "series": {
            "tmdbId": TMDB, "title": "Show", "path": str(self.dl)}}, None, self.db)
        after = self.claims()
        print(f"after SeriesDelete: {after}")
        self.assertEqual(after["sonarr_path"], None)
        self.assertFalse(after["Downloaded TV"], f"VOD-only show still tagged Downloaded TV: {after}")
        self.assertIsNone(after["downloaded_at"], f"VOD-only show still has a download date: {after}")
        self.assertEqual(0, self.db.query(Duplicate).filter(Duplicate.resolution == "pending").count(),
                         "the pending duplicate of a download that is gone stays")

    def test_refresh_tags_puts_it_back_on_the_vod_show(self):
        """What Jellyfin gets: with the download gone, the TMDB id is one item
        again (the VOD show), and Refresh Tags writes the row's tags onto it."""
        self.downloaded_first()
        sonarr_router.sonarr_webhook({"eventType": "SeriesDelete", "series": {
            "tmdbId": TMDB, "title": "Show", "path": str(self.dl)}}, None, self.db)
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
        self.scan()
        after = self.claims()
        print(f"after the show left Sonarr + scan: {after}")
        self.assertFalse(after["Downloaded TV"], f"VOD-only show still tagged Downloaded TV: {after}")
        self.assertIsNone(after["sonarr_path"], f"still points at the deleted download: {after}")

    def test_files_deleted_show_still_in_sonarr_then_scan(self):
        self.downloaded_first()
        self.listed = [self._show(files=0), self._other()]
        self.scan()
        after = self.claims()
        print(f"after the episode files went (show still in Sonarr) + scan: {after}")
        self.assertFalse(after["Downloaded TV"], f"VOD-only show still tagged Downloaded TV: {after}")

    def test_a_show_requested_but_not_downloaded_yet_is_left_alone(self):
        """Add to Sonarr on a VOD show sets sonarr_path at once
        (services.media_requests), before anything downloaded."""
        r = self.row()
        r.sonarr_path = str(self.dl)
        self.db.commit()
        self.listed = [self._show(files=0), self._other()]
        self.scan()
        after = self.claims()
        self.assertEqual(str(self.dl), after["sonarr_path"], after)
        self.assertEqual(1, after["requests"], f"a pending request was dropped: {after}")

    def test_a_show_an_older_series_delete_half_released_is_finished(self):
        """Left by a SeriesDelete before this fix: sonarr_path cleared, the
        download date and "Downloaded TV" kept, the show gone from Sonarr."""
        self.downloaded_first()
        r = self.row()
        r.sonarr_path = None
        self.db.query(DownloadRequest).delete()
        self.db.commit()
        self.listed = [self._other()]
        self.scan()
        after = self.claims()
        self.assertFalse(after["Downloaded TV"], f"still tagged Downloaded TV: {after}")
        self.assertIsNone(after["downloaded_at"], after)

    def test_many_vod_shows_losing_their_files_at_once_is_an_outage(self):
        extra = []
        for n in (1, 2):
            tid = TMDB + 10 + n
            folder = self.tmp / "vod" / "shows" / f"Show {n} (2020)"
            folder.mkdir(parents=True)
            self.db.add(Series(tmdb_id=tid, title=f"Show {n}", year="2020", source="provider_1",
                               provider_id=1, strm_path=str(folder), tags=["Netflix TV"]))
            extra.append(dict(self._show(files=1), id=50 + n, tmdbId=tid, title=f"Show {n}",
                              path=str(self.tmp / "tv" / f"Show {n} (2020)")))
        self.db.commit()
        self.listed = [self._show(files=1), self._other()] + extra
        self.scan()
        self.listed = [self._show(files=0), self._other()] + [dict(e, statistics={"episodeFileCount": 0})
                                                              for e in extra]
        stats = self.scan()
        self.assertEqual(3, stats.get("removals_refused"), stats)
        self.assertTrue(self.claims()["Downloaded TV"], "an outage released every VOD show's download")


class HybridShowKeepVod(keep_vod._Base):
    """Keep VOD on a VOD show Sonarr downloaded too (one row, source provider_1)."""
    def setUp(self):
        super().setUp()
        self.show = self.root / "vod" / "shows" / "Show (2010)"
        (self.show / "Season 01").mkdir(parents=True)
        (self.show / "Season 01" / "Show S01E01.strm").write_text("http://p/series/1.mp4")
        dl = self.root / "tv" / "Show (2010)"
        (dl / "Season 01").mkdir(parents=True)
        self.mkv = dl / "Season 01" / "Show - S01E02.mkv"
        self.mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[1418] = {"id": 5, "path": str(dl), "files": [{"id": 50, "path": str(self.mkv)}]}
        user = TentacleUser(jellyfin_user_id="u1", display_name="requester")
        self.db.add(user)
        self.db.commit()
        self.db.add(Series(tmdb_id=1418, title="Show", year="2010", source="provider_1", provider_id=1,
                           strm_path=str(self.show), sonarr_path=str(dl), downloaded_at=datetime(2026, 9, 1),
                           tags=["Netflix TV", "Downloaded TV", "requester's Downloads"]))
        dup = Duplicate(tmdb_id=1418, media_type="series", resolution="pending",
                        sources=[{"source": "sonarr", "path": str(dl)},
                                 {"source": "provider_1", "path": str(self.show)}])
        self.db.add(dup)
        self.db.add(DownloadRequest(tmdb_id=1418, media_type="series", user_id=user.id))
        self.db.commit()
        self.dup_id = dup.id

    def assert_vod_only(self):
        row = self.fresh().query(Series).one()
        self.assertEqual("provider_1", row.source)
        self.assertTrue(list(self.show.rglob("*.strm")), "the VOD episodes stay")
        self.assertIsNone(row.sonarr_path)
        self.assertIsNone(row.downloaded_at)
        self.assertNotIn("Downloaded TV", row.tags)
        self.assertNotIn("requester's Downloads", row.tags)
        self.assertIn("Netflix TV", row.tags)
        self.assertEqual(0, self.db.query(DownloadRequest).count())

    def test_no_delete_webhook(self):
        with mock.patch.object(self, "sonarr_webhook", lambda payload: None):
            self.assertEqual({"success": True}, self.resolve(self.dup_id))
        self.assert_vod_only()

    def test_delete_webhook_during_the_resolution(self):
        self.assertEqual({"success": True}, self.resolve(self.dup_id))
        self.assert_vod_only()


if __name__ == "__main__":
    unittest.main()
