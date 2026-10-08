"""Keep VOD gives the film back to VOD only, as a deleted download does (#549).

Run from tentacle/:  python tests/hermetic.py discover -s tests -p test_duplicate_keep_vod_releases_download.py

Keep VOD deletes the download through Radarr and then cleared only the row's
radarr_path. When Radarr sends no delete webhook (Connect not set up for
deletes) or sends it after the resolution, nothing released the row (#378's
release needs radarr_path): downloaded_at, the deleted download's Jellyfin
item id and "Downloaded Movies" (plus the requester's "<name>'s Downloads")
stayed, so the VOD film stayed in every Downloaded Movies playlist through
every nightly and Refresh Tags.
"""
import unittest
from datetime import datetime
from unittest import mock

from models.database import DownloadRequest, Duplicate, Movie, TentacleUser
from tests import test_duplicate_keep_vod_delete_webhook as keep_vod
from tests.test_duplicate_keep_vod_merged_folder import _FakeArr
from tests.test_duplicate_keeps_user_data import setUpModule, tearDownModule  # noqa: F401

NFO = """<?xml version="1.0" encoding="utf-8"?>
<movie>
  <title>Film</title>
  <tag>Netflix Movies</tag>
  <tag>Downloaded Movies</tag>
  <tag>requester's Downloads</tag>
</movie>
"""


class KeepVodReleasesTheDownload(keep_vod._Base):
    """A VOD .strm and a Radarr download in separate folders, one row."""
    def setUp(self):
        super().setUp()
        vod = self.root / "vod" / "movies" / "Film (2001)"
        vod.mkdir(parents=True)
        self.strm = vod / "Film (2001).strm"
        self.strm.write_text("http://p/movie/1.mp4")
        dl = self.root / "movies" / "Film (2001) [1080p]"
        dl.mkdir(parents=True)
        self.mkv = dl / "Film (2001).mkv"
        self.mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[101] = {"id": 7, "path": str(dl), "files": [{"id": 70, "path": str(self.mkv)}]}
        self.vod_nfo = self.strm.with_suffix(".nfo")
        self.vod_nfo.write_text(NFO)
        user = TentacleUser(jellyfin_user_id="u1", display_name="requester")
        self.db.add(user)
        self.db.commit()
        # The state the Download webhook + Radarr scan leave: one row, both copies.
        self.db.add(Movie(tmdb_id=101, title="Film", year="2001", source="provider_1", provider_id=1,
                          source_tag="Netflix", strm_path=str(self.strm),
                          nfo_path=str(self.mkv.with_suffix(".nfo")), radarr_path=str(self.mkv),
                          jellyfin_item_id="dl-item", downloaded_at=datetime(2026, 9, 1),
                          tags=["Netflix Movies", "Downloaded Movies", "requester's Downloads"]))
        dup = Duplicate(tmdb_id=101, media_type="movie", resolution="pending",
                        sources=[{"source": "radarr", "path": str(self.mkv)},
                                 {"source": "provider_1", "path": str(self.strm)}])
        self.db.add(dup)
        self.db.add(DownloadRequest(tmdb_id=101, media_type="movie", user_id=user.id))
        self.db.commit()
        self.dup_id = dup.id

    def assert_vod_only(self):
        row = self.fresh().query(Movie).one()
        self.assertEqual(("provider_1", str(self.strm)), (row.source, row.strm_path), "the VOD copy must stay")
        self.assertTrue(self.strm.exists())
        self.assertIsNone(row.radarr_path)
        self.assertIsNone(row.downloaded_at, "the VOD film still claims the deleted download's date")
        self.assertIsNone(row.jellyfin_item_id, "still the deleted download's Jellyfin item")
        self.assertEqual(str(self.vod_nfo), row.nfo_path, "still the download's NFO")
        self.assertNotIn("Downloaded Movies", row.tags,
                         "VOD-only film still tagged 'Downloaded Movies' (stays in that playlist for every user)")
        self.assertNotIn("requester's Downloads", row.tags, "the request went with the film from Radarr")
        self.assertIn("Netflix Movies", row.tags)
        nfo = self.vod_nfo.read_text()
        self.assertNotIn("<tag>Downloaded Movies</tag>", nfo, "the .strm's NFO brings the tag back")
        self.assertIn("<tag>Netflix Movies</tag>", nfo)
        self.assertEqual(0, self.db.query(DownloadRequest).count(), "the film left Radarr; its request stays")
        self.assert_recorded()

    def test_no_delete_webhook(self):
        # Radarr's Connect not set up for deletes: nothing but Keep VOD itself.
        with mock.patch.object(self, "radarr_webhook", lambda payload: None):
            self.assertEqual({"success": True}, self.resolve(self.dup_id))
        self.assertFalse(self.mkv.exists())
        self.assert_vod_only()

    def test_delete_webhook_after_the_resolution(self):
        # Radarr's Connect posting late: by then radarr_path is gone, so the
        # webhook no longer finds a row holding a download.
        late = []
        with mock.patch.object(self, "radarr_webhook", late.append):
            self.assertEqual({"success": True}, self.resolve(self.dup_id))
        for payload in late:
            self.radarr_webhook(payload)
        self.assert_vod_only()

    def test_delete_webhook_during_the_resolution(self):
        # Already right before #549 (the webhook releases the row); stays so.
        self.assertEqual({"success": True}, self.resolve(self.dup_id))
        self.assert_vod_only()


if __name__ == "__main__":
    unittest.main()
