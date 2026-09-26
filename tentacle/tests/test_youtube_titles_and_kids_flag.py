"""YouTube video metadata: placeholder titles are repaired (#131), and the
Made for Kids flag means what it says (#130).

Run from the tentacle/ directory:  python -m unittest discover -s tests

#131: four videos on a live install were stored as "youtube video #<id>" when
a details fetch came back without a title, and kept it for ever: a known
video's title was never read again, and it was baked into the NFO (locked, so
Jellyfin never re-reads it) and the Jellyfin item.

#130: is_made_for_kids was `age_limit == 0 and is_live is None or None`, which
parses as `(...) or None`: False could never be stored, and age_limit 0 means
"no age restriction", not "made for kids".
"""
import logging
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import YouTubeChannel, YouTubeVideo
from services.youtube import client, indexer, library
from services.youtube import sync as yt_sync

A, B, C, D = "a" * 11, "b" * 11, "c" * 11, "d" * 11


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class MadeForKids(unittest.TestCase):
    def test_it_is_read_from_the_field_that_carries_it(self):
        self.assertIs(True, indexer.made_for_kids({"is_made_for_kids": True}))
        self.assertIs(False, indexer.made_for_kids({"is_made_for_kids": False}))

    def test_age_limit_is_not_the_designation(self):
        self.assertIsNone(indexer.made_for_kids({"age_limit": 0, "is_live": None}))
        self.assertIsNone(indexer.made_for_kids({"age_limit": 18}))
        self.assertIsNone(indexer.made_for_kids({}))

    def test_the_stored_values_are_cleared_once(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        ch = YouTubeChannel(input_url="u", kind="channel", title="Ch", slug="ch", extra_tags=[])
        db.add(ch)
        db.commit()
        db.add(YouTubeVideo(channel_fk=ch.id, video_id=A, title="x", is_made_for_kids=True))
        db.commit()
        self.assertEqual(1, indexer.reset_made_for_kids_once(db))
        db.expire_all()
        self.assertIsNone(db.query(YouTubeVideo).one().is_made_for_kids)
        # A real flag set afterwards is left alone by later starts.
        db.query(YouTubeVideo).update({YouTubeVideo.is_made_for_kids: False})
        db.commit()
        self.assertEqual(0, indexer.reset_made_for_kids_once(db))
        self.assertIs(False, db.query(YouTubeVideo).one().is_made_for_kids)


class _Channel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.channel = YouTubeChannel(
            input_url="u", kind="channel", channel_id="UC" + "k" * 22, title="Kakė Makė", slug="kake-make",
            enabled=True, include_videos=True, include_streams=False, include_shorts=False,
            min_duration=0, keep_count=10, extra_tags=[])
        self.db.add(self.channel)
        self.db.commit()
        self.entries = []
        self.details = {}
        self.detail_calls = []

        def _details(vid):
            self.detail_calls.append(vid)
            return dict(self.details[vid])
        patches = [mock.patch.object(client, "flat_listing", lambda url, limit: {"entries": list(self.entries)}),
                   mock.patch.object(client, "video_details", _details),
                   mock.patch.object(indexer.time, "sleep", lambda s: None),
                   mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", self.tmp / "media"),
                   mock.patch.object(library, "fetch_artwork", lambda video, folder: 0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _stored(self, vid, title):
        v = YouTubeVideo(channel_fk=self.channel.id, video_id=vid, title=title, duration=600,
                         published_at=datetime(2026, 9, 21), first_seen=datetime(2026, 9, 21),
                         last_seen=datetime(2026, 9, 21), media_type="video")
        self.db.add(v)
        self.db.commit()
        library.write_video(v, self.channel, "http://tentacle:8888")
        self.db.commit()
        return v

    def _title(self, vid):
        self.db.expire_all()
        return self.db.query(YouTubeVideo).filter_by(video_id=vid).one().title


class PlaceholderTitles(_Channel):
    def test_the_listing_title_replaces_a_placeholder_without_a_details_fetch(self):
        self._stored(A, f"youtube video #{A}")
        self._stored(B, B)
        self.entries = [{"id": A, "title": "Dainelė apie katiną"}, {"id": B, "title": "Kita dainelė"}]
        result = indexer.index_channel(self.db, self.channel)
        self.assertEqual("Dainelė apie katiną", self._title(A))
        self.assertEqual("Kita dainelė", self._title(B))
        self.assertEqual([], self.detail_calls)
        self.assertEqual({A, B}, set(result["retitled"]))

    def test_details_are_fetched_when_the_listing_has_no_title_but_not_for_ever(self):
        vids = [ch * 11 for ch in "efghi"]
        for v in vids:
            self._stored(v, f"youtube video #{v}")
            self.details[v] = {"title": f"Title {v[0]}", "duration": 600}
        self.entries = [{"id": v, "title": None} for v in vids]
        indexer.index_channel(self.db, self.channel)
        self.assertEqual(indexer.MAX_RETITLE_FETCHES, len(self.detail_calls))
        fixed = [v for v in vids if not self._title(v).startswith("youtube video #")]
        self.assertEqual(indexer.MAX_RETITLE_FETCHES, len(fixed))

    def test_markers_and_real_titles_are_never_used_or_overwritten(self):
        self._stored(A, f"youtube video #{A}")
        self._stored(C, "A real title")
        self.entries = [{"id": A, "title": "[Private video]"}, {"id": C, "title": "Something else"}]
        self.details[A] = {"title": None}
        indexer.index_channel(self.db, self.channel)
        self.assertEqual(f"youtube video #{A}", self._title(A))
        self.assertEqual("A real title", self._title(C))

    def test_a_new_video_is_never_stored_under_its_id(self):
        self.entries = [{"id": D, "title": None}]
        self.details[D] = {"title": None, "duration": 600, "availability": "public"}
        result = indexer.index_channel(self.db, self.channel)
        self.assertEqual(0, result["new"])
        self.assertEqual(0, self.db.query(YouTubeVideo).filter_by(video_id=D).count(),
                         "left unrecorded so the next refresh tries again")

    def test_the_nfo_and_the_jellyfin_item_are_fixed_in_place(self):
        v = self._stored(A, f"youtube video #{A}")
        folder = Path(v.folder_path)
        self.entries = [{"id": A, "title": "Dainelė"}]
        renamed = []

        class FakeJF:
            def __init__(self, *a, **k):
                pass

            def query_items(self, include_types, tags=None, **kw):
                return [{"Id": "jf1", "Name": f"youtube video #{A}", "ProviderIds": {"Youtube": A}}]

            def set_item_name(self, item_id, name):
                renamed.append((item_id, name))
                return True

        mdb.set_setting(self.db, "jellyfin_url", "http://jf:8096")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        with mock.patch("services.jellyfin.JellyfinService", FakeJF):
            yt_sync.sync_channel(self.db, self.channel, "http://tentacle:8888")
        self.assertEqual([("jf1", "Dainelė")], renamed)
        nfo = (folder / "movie.nfo").read_text(encoding="utf-8")
        self.assertIn("<title>Dainelė</title>", nfo)
        self.assertIn("<lockdata>true</lockdata>", nfo)
        self.assertTrue(folder.exists(), "the folder must not move: a new path is a new Jellyfin item")


if __name__ == "__main__":
    unittest.main()
