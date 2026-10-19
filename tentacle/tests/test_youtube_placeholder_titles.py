"""A video stored under a placeholder title gets its real title back (#131).

index_channel() only ever inserts a video. A row written while the details
fetch had no title keeps that title for ever — the bare video id today, or
"youtube video #<id>" from an earlier build — and it reaches Jellyfin through
the NFO. The flat listing names every video on every refresh, so the fix
takes the title from there (or, when the listing has none either, from a
bounded number of details fetches), rewrites the NFO in place, and renames an
item Jellyfin already has through its API — the NFO says <lockdata>true</lockdata>
and Jellyfin does not re-read a locked item's NFO, even on a full refresh.

Listing and details dicts are shaped like yt-dlp's (flat entries carry id and
title; unavailable playlist entries are listed as "[Private video]").
Run from tentacle/:  python -m unittest discover -s tests -p "test_youtube_placeholder_titles.py"
"""
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from test_youtube_indexer_followup import _channel, _fresh_db, _tabs
from tmp_dirs import temp_dir

BASE = "http://192.0.2.20:8888"
VID = "dn4JZxXnbDk"


def _details(title="Kakė Makė: real title", vid=VID):
    return {"id": vid, "title": title, "duration": 600, "availability": "public",
            "live_status": "not_live", "is_live": False, "age_limit": 0,
            "timestamp": 1758412800}


class _Case(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        self.channel = _channel(self.db, live_enabled=False, title="Kakė Makė")
        self.root = Path(temp_dir(self))

    def tearDown(self):
        self.db.close()

    def _stored(self, title):
        """A library video already written to disk under `title`."""
        from models.database import YouTubeVideo
        from services.youtube import library
        video = YouTubeVideo(channel_fk=self.channel.id, video_id=VID, title=title,
                             published_at=datetime(2026, 9, 21), duration=600,
                             live_status="not_live", first_seen=datetime(2026, 9, 21),
                             last_seen=datetime(2026, 9, 21))
        self.db.add(video)
        self.db.commit()
        with mock.patch.object(library, "fetch_artwork", return_value=0):
            library.write_video(video, self.channel, BASE, root=self.root)
        self.db.commit()
        return video

    def _sync(self, entries, details=None):
        from services.youtube import indexer, library, sync
        calls = mock.Mock(side_effect=lambda vid: details or _details(vid=vid))
        with mock.patch.object(indexer.client, "flat_listing",
                               side_effect=_tabs(videos_entries=entries)), \
             mock.patch.object(indexer.client, "video_details", calls), \
             mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", self.root), \
             mock.patch.object(library, "fetch_artwork", return_value=0), \
             mock.patch.object(indexer.time, "sleep"):
            result = sync.sync_channel(self.db, self.channel, BASE)
        return result, calls

    def _nfo_title(self, video):
        import re
        text = (Path(video.folder_path) / "movie.nfo").read_text(encoding="utf-8")
        return re.search(r"<title>(.*)</title>", text).group(1)


class TestPlaceholderIsRepaired(_Case):
    def test_old_builds_placeholder_is_replaced_from_the_listing(self):
        video = self._stored(f"youtube video #{VID}")
        self._sync([{"id": VID, "title": "Kakė Makė: real title"}])
        self.db.refresh(video)
        self.assertEqual(video.title, "Kakė Makė: real title")
        self.assertEqual(self._nfo_title(video), "Kakė Makė: real title")

    def test_bare_id_placeholder_is_replaced(self):
        video = self._stored(VID)
        self._sync([{"id": VID, "title": "Kakė Makė: real title"}])
        self.db.refresh(video)
        self.assertEqual(video.title, "Kakė Makė: real title")

    def test_listing_without_a_title_falls_back_to_details(self):
        video = self._stored(VID)
        _, calls = self._sync([{"id": VID}])
        self.db.refresh(video)
        self.assertEqual(video.title, "Kakė Makė: real title")
        self.assertEqual(calls.call_count, 1)


class TestNothingElseChanges(_Case):
    def test_a_real_title_is_never_overwritten(self):
        video = self._stored("The title the uploader chose")
        _, calls = self._sync([{"id": VID, "title": "A different listing title"}])
        self.db.refresh(video)
        self.assertEqual(video.title, "The title the uploader chose")
        calls.assert_not_called()

    def test_a_listing_that_names_it_by_its_id_costs_no_fetch(self):
        video = self._stored(VID)
        _, calls = self._sync([{"id": VID, "title": VID}])
        self.db.refresh(video)
        self.assertEqual(video.title, VID)
        calls.assert_not_called()

    def test_private_video_marker_is_not_a_title(self):
        video = self._stored(VID)
        self._sync([{"id": VID, "title": "[Private video]"}], details=_details(title=None))
        self.db.refresh(video)
        self.assertEqual(video.title, VID)

    def test_folder_and_strm_are_left_alone(self):
        """Renaming the folder would make it a new Jellyfin item (watched state lost)."""
        video = self._stored(f"youtube video #{VID}")
        folder, strm = video.folder_path, Path(video.strm_path)
        mtime = strm.stat().st_mtime_ns
        self._sync([{"id": VID, "title": "Kakė Makė: real title"}])
        self.db.refresh(video)
        self.assertEqual(video.folder_path, folder)
        self.assertEqual(strm.stat().st_mtime_ns, mtime)
        self.assertEqual(sorted(p.name for p in Path(folder).parent.iterdir()),
                         [Path(folder).name])

    def test_details_fetches_for_repairs_are_bounded(self):
        from models.database import YouTubeVideo
        from services.youtube import indexer
        ids = [f"{i:011d}" for i in range(indexer.TITLE_REPAIRS_PER_RUN + 4)]
        for vid in ids:
            self.db.add(YouTubeVideo(channel_fk=self.channel.id, video_id=vid, title=vid,
                                     published_at=datetime(2026, 9, 21), live_status="not_live"))
        self.db.commit()
        self.channel.keep_count = 50
        self.db.commit()
        _, calls = self._sync([{"id": vid} for vid in ids])
        self.assertEqual(calls.call_count, indexer.TITLE_REPAIRS_PER_RUN)


class TestJellyfinItemIsRenamed(_Case):
    """Jellyfin never re-reads a locked item's NFO, so an imported item is renamed via the API."""

    def test_an_imported_item_is_renamed_in_place(self):
        from models.database import Setting
        from services import jellyfin
        self.db.add_all([Setting(key="jellyfin_url", value="http://jf.invalid:8096"),
                         Setting(key="jellyfin_api_key", value="k")])
        self.db.commit()
        self._stored(f"youtube video #{VID}")
        fake = mock.Mock()
        fake.query_items.return_value = [
            {"Id": "jf-1", "Name": f"youtube video #{VID}", "ProviderIds": {"youtube": VID}},
            {"Id": "jf-2", "Name": "Another upload", "ProviderIds": {"youtube": "zzzzzzzzzzz"}},
        ]
        fake.set_item_name.return_value = True
        with mock.patch.object(jellyfin, "JellyfinService", return_value=fake):
            self._sync([{"id": VID, "title": "Kakė Makė: real title"}])
        fake.query_items.assert_called_once_with(include_types=["Movie"], tags=["yt:ch"])
        fake.set_item_name.assert_called_once_with("jf-1", "Kakė Makė: real title")

    def test_no_jellyfin_call_when_nothing_was_retitled(self):
        from services import jellyfin
        self._stored("A real title")
        with mock.patch.object(jellyfin, "JellyfinService") as svc:
            self._sync([{"id": VID, "title": "A real title"}])
        svc.assert_not_called()


class TestSetItemNameKeepsTheLock(unittest.TestCase):
    def test_lockdata_is_sent_back(self):
        """An ItemUpdate without LockData unlocks the item (seen on Jellyfin 10.11.8)."""
        from services.jellyfin import JellyfinService
        jf = JellyfinService("http://jf.invalid:8096", "k")
        jf.session = mock.Mock()
        jf.session.post.return_value = mock.Mock(status_code=204, text="")
        with mock.patch.object(jf, "_get", return_value={
                "Id": "jf-1", "Name": "old", "Tags": ["youtube", "yt:ch"], "LockData": True,
                "ProviderIds": {"youtube": VID}}):
            self.assertTrue(jf.set_item_name("jf-1", "new"))
        sent = jf.session.post.call_args.kwargs["json"]
        self.assertEqual(sent["Name"], "new")
        self.assertIs(sent["LockData"], True)
        self.assertEqual(sent["Tags"], ["youtube", "yt:ch"])
        self.assertEqual(sent["ProviderIds"], {"youtube": VID})


if __name__ == "__main__":
    unittest.main()
