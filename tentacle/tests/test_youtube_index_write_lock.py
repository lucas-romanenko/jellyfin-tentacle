"""#253: indexing held SQLite's write lock while yt-dlp read a new video.

index_channel marks every listed video as seen with one bulk UPDATE, which
opens a write transaction, and then read the first new video's details (a
network call of up to tens of seconds) before the next commit. Every other
writer in Tentacle waited on that lock and failed with "database is locked"
after the busy timeout: the music worker, a manual refresh, Live TV, the sync.

The database here is a real SQLite file in WAL mode, as in production; the
other writer uses a short busy timeout so a held lock shows at once.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_index_write_lock.py
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import YouTubeChannel, YouTubeVideo
from services.youtube import indexer, traffic


class NoWriteLockDuringANetworkRead(unittest.TestCase):
    def setUp(self):
        self.path = f"{tempfile.mkdtemp()}/t.db"
        engine = create_engine(f"sqlite:///{self.path}", connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _wal(conn, _):
            conn.execute("PRAGMA journal_mode=WAL")

        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False)()
        self.addCleanup(self.db.close)
        traffic.reset_for_tests()
        self.addCleanup(traffic.reset_for_tests)
        ch = YouTubeChannel(input_url="u", kind="channel", channel_id="UC" + "c" * 22, title="Chan",
                            slug="chan", enabled=True, include_videos=True, include_streams=False,
                            include_shorts=False, keep_count=5, min_duration=0, extra_tags=[],
                            live_enabled=False)
        self.db.add(ch)
        self.db.commit()
        # One video already known, so the listing's bulk "last seen" update has a row to touch.
        self.db.add(YouTubeVideo(channel_fk=ch.id, video_id="k" * 11, title="Known",
                                 first_seen=datetime(2026, 9, 1), last_seen=datetime(2026, 9, 1)))
        self.db.commit()
        self.channel = ch

    def _other_writer(self):
        """What any other part of Tentacle does meanwhile: one small write."""
        conn = sqlite3.connect(self.path, timeout=0.2)
        try:
            conn.execute("INSERT INTO settings (key, value) VALUES ('other', 'writer')")
            conn.commit()
            return "ok"
        except sqlite3.OperationalError as e:
            return str(e)
        finally:
            conn.close()

    def test_another_writer_is_not_locked_out_while_details_are_read(self):
        seen = []

        def slow_details(video_id, *a, **kw):
            seen.append(self._other_writer())
            return {"id": video_id, "title": "New", "availability": "public",
                    "live_status": "not_live", "duration": 600, "timestamp": 1758412800}

        listing = {"entries": [{"id": "n" * 11, "title": "New"}, {"id": "k" * 11, "title": "Known"}]}
        with mock.patch.object(indexer.client, "flat_listing", return_value=listing), \
             mock.patch.object(indexer.client, "video_details", side_effect=slow_details), \
             mock.patch.object(indexer, "DETAIL_SPACING_SECONDS", 0):
            indexer.index_channel(self.db, self.channel)
        self.assertEqual(["ok"], seen)


if __name__ == "__main__":
    unittest.main()
