"""youtube_videos.is_made_for_kids must mean YouTube's designation (#130).

The indexer stored

    details.get("age_limit") == 0 and details.get("is_live") is None or None

which Python reads as ((age_limit == 0) and (is_live is None)) or None: it can
never be False, and age_limit == 0 is what yt-dlp reports for every video
without an age restriction, not for Made for Kids. The details dicts below are
shaped like yt-dlp's (age_limit is 0 or 18; is_live is filled in as a bool by
YoutubeDL.process_video_result, so a real extraction never has it as None).

Run from tentacle/:  python -m unittest discover -s tests -p "test_youtube_made_for_kids.py"
"""
import sqlite3
import unittest
from unittest import mock

from test_youtube_indexer_followup import _channel, _fresh_db, _tabs
from tmp_dirs import temp_dir

VID = "abcdefghijk"


def _details(**kw):
    d = {"id": VID, "title": "An upload", "duration": 600, "availability": "public",
         "live_status": "not_live", "is_live": False, "was_live": False,
         "age_limit": 0, "timestamp": 1758000000}
    d.update(kw)
    return d


class TestStoredValue(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        self.channel = _channel(self.db, live_enabled=False)

    def tearDown(self):
        self.db.close()

    def _index(self, details):
        from models.database import YouTubeVideo
        from services.youtube import indexer
        with mock.patch.object(indexer.client, "flat_listing",
                               side_effect=_tabs(videos_entries=[{"id": VID, "title": "An upload"}])), \
             mock.patch.object(indexer.client, "video_details", return_value=details), \
             mock.patch.object(indexer.time, "sleep"):
            indexer.index_channel(self.db, self.channel)
        return self.db.query(YouTubeVideo).filter_by(video_id=VID).one().is_made_for_kids

    def test_an_ordinary_unrestricted_video_is_not_flagged(self):
        # age_limit 0 = "no age restriction"; says nothing about Made for Kids.
        self.assertIsNone(self._index(_details(age_limit=0, is_live=None)))

    def test_an_age_restricted_video_is_not_flagged(self):
        self.assertIsNot(self._index(_details(age_limit=18)), True)

    def test_false_is_recorded_when_the_designation_says_so(self):
        self.assertIs(self._index(_details(is_made_for_kids=False)), False)

    def test_true_is_recorded_when_the_designation_says_so(self):
        self.assertIs(self._index(_details(is_made_for_kids=True)), True)

    def test_unknown_stays_none(self):
        self.assertIsNone(self._index(_details()))


class TestGuessedValuesAreReset(unittest.TestCase):
    """The rows the old expression wrote are cleared once, and only once."""

    def _db(self):
        from sqlalchemy import create_engine
        import models.database as mdb
        path = f"{temp_dir(self)}/t.db"
        mdb.Base.metadata.create_all(create_engine(f"sqlite:///{path}"))
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO youtube_channels (id, input_url, kind, title, slug) "
                     "VALUES (1, 'u', 'channel', 'Ch', 'ch')")
        for i, value in enumerate((1, None, 1)):
            conn.execute("INSERT INTO youtube_videos (channel_fk, video_id, title, is_made_for_kids) "
                         "VALUES (1, ?, 't', ?)", (f"vid{i:08d}", value))
        conn.commit()
        return conn

    def _values(self, conn):
        return sorted((r[0] is None, r[0]) for r in
                      conn.execute("SELECT is_made_for_kids FROM youtube_videos"))

    def test_guessed_true_values_become_unknown(self):
        from models.database import _reset_guessed_made_for_kids
        conn = self._db()
        _reset_guessed_made_for_kids(conn.cursor(), conn)
        self.assertEqual([r[0] for r in conn.execute("SELECT is_made_for_kids FROM youtube_videos")],
                         [None, None, None])

    def test_it_runs_only_once(self):
        from models.database import _reset_guessed_made_for_kids
        conn = self._db()
        _reset_guessed_made_for_kids(conn.cursor(), conn)
        # A real value written after the reset must survive the next start.
        conn.execute("UPDATE youtube_videos SET is_made_for_kids = 1 WHERE video_id = 'vid00000000'")
        conn.commit()
        _reset_guessed_made_for_kids(conn.cursor(), conn)
        self.assertEqual(conn.execute("SELECT is_made_for_kids FROM youtube_videos "
                                      "WHERE video_id = 'vid00000000'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
