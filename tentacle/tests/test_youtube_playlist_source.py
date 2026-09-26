"""A YouTube playlist is its own source, not its owner's channel (#169).

Run from the tentacle/ directory:  python -m unittest discover -s tests

For a playlist, yt-dlp's tab extractor fills channel / channel_id with the
playlist's OWNER. So a playlist was titled with the owner's channel name (its
folder, playlist and home row all said "Kakė Makė" instead of the playlist),
and add_channel refused it with 409 "already added" whenever the owner's
channel, or another playlist of the same owner, was already a source.
"""
import logging
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import TentacleUser, YouTubeChannel
from routers import youtube
from services.youtube import client, indexer
from services.youtube import sync as ysync

OWNER = "UC" + "o" * 22
PL1, PL2 = "PL" + "1" * 32, "PL" + "2" * 32


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class PlaylistTitle(unittest.TestCase):
    def test_a_playlist_is_titled_by_its_own_title(self):
        listing = {"title": "Kids' songs", "channel": "Kakė Makė", "uploader": "Kakė Makė",
                   "channel_id": OWNER, "entries": [{"id": "a" * 11}], "thumbnails": []}
        with mock.patch.object(client, "flat_listing", lambda url, limit: dict(listing)):
            info = indexer.resolve_channel(f"https://www.youtube.com/playlist?list={PL1}")
        self.assertEqual("playlist", info["kind"])
        self.assertEqual("Kids' songs", info["title"])
        self.assertEqual(PL1, info["playlist_id"])

    def test_a_channel_is_still_titled_by_the_channel(self):
        listing = {"title": "Kakė Makė - Videos", "channel": "Kakė Makė", "channel_id": OWNER,
                   "entries": [], "thumbnails": []}
        with mock.patch.object(client, "flat_listing", lambda url, limit: dict(listing)):
            info = indexer.resolve_channel("https://www.youtube.com/channel/" + OWNER)
        self.assertEqual("Kakė Makė", info["title"])

    def test_resolve_channel_reports_the_owner(self):
        listing = {"title": "Favorites", "channel": "Owner B", "channel_id": OWNER,
                   "entries": [], "thumbnails": []}
        with mock.patch.object(client, "flat_listing", lambda url, limit: dict(listing)):
            info = indexer.resolve_channel(f"https://www.youtube.com/playlist?list={PL1}")
        self.assertEqual("Owner B", info["owner"])


class AddingNextToTheOwner(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add(TentacleUser(jellyfin_user_id="a" * 32, display_name="u", is_admin=True))
        self.db.commit()
        self.info = None
        patches = [
            mock.patch.object(youtube.client, "available", lambda: True),
            mock.patch.object(youtube.indexer, "resolve_channel", lambda url: dict(self.info)),
            mock.patch.object(youtube, "_start_refresh", lambda **kw: True),
            mock.patch.object(ysync, "detect_base_url",
                              lambda db, host=None, scheme="http": {"url": "http://192.168.2.10:8888", "tried": []}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    class _Req:
        headers = {"host": "192.168.2.10:8888"}

    def _add(self, kind, title, playlist_id=None, owner="Kakė Makė", channel_id=OWNER):
        self.info = {"kind": kind, "channel_id": channel_id, "handle": None, "playlist_id": playlist_id,
                     "title": title, "owner": owner, "avatar_url": None, "banner_url": None,
                     "canonical": "u", "has_uploads": True}
        body = youtube.ChannelCreate(url="https://youtube.com/x")
        return youtube.add_channel(body, request=self._Req(), db=self.db)

    def test_a_playlist_can_sit_next_to_its_owners_channel(self):
        self._add("channel", "Kakė Makė")
        self._add("playlist", "Kids' songs", PL1)
        self.assertEqual({"Kakė Makė", "Kids' songs"}, {c.title for c in self.db.query(YouTubeChannel)})

    def test_two_playlists_of_one_owner_can_both_be_added(self):
        self._add("playlist", "Kids' songs", PL1)
        self._add("playlist", "Lullabies", PL2)
        self.assertEqual(2, self.db.query(YouTubeChannel).count())

    def test_the_same_playlist_twice_is_still_refused(self):
        self._add("playlist", "Kids' songs", PL1)
        with self.assertRaises(HTTPException) as cm:
            self._add("playlist", "Kids' songs", PL1)
        self.assertEqual(409, cm.exception.status_code)

    def test_the_same_channel_twice_is_still_refused_even_after_its_playlist(self):
        self._add("playlist", "Kids' songs", PL1)
        self._add("channel", "Kakė Makė")
        with self.assertRaises(HTTPException) as cm:
            self._add("channel", "Kakė Makė")
        self.assertEqual(409, cm.exception.status_code)


class TitlesStayUnique(AddingNextToTheOwner):
    """Tentacle keys a source's playlist, home row, folder and collection on its
    title: a second source with the same title silently got no playlist."""

    def _titles(self):
        return sorted(c.title for c in self.db.query(YouTubeChannel))

    def test_a_playlist_named_like_its_channel_gets_a_number(self):
        self._add("channel", "Bluey", owner="Bluey")
        self._add("playlist", "bluey", PL1, owner="Bluey")
        self.assertEqual(["Bluey", "bluey (2)"], self._titles())

    def test_two_owners_playlists_with_one_name_take_the_owner(self):
        self._add("playlist", "Favorites", PL1, owner="Owner A", channel_id="UC" + "a" * 22)
        self._add("playlist", "Favorites", PL2, owner="Owner B", channel_id="UC" + "b" * 22)
        self.assertEqual(["Favorites", "Favorites (Owner B)"], self._titles())

    def test_a_third_clash_counts_on(self):
        self._add("channel", "Bluey", owner="Bluey")
        self._add("playlist", "Bluey", PL1, owner="Bluey")
        self._add("playlist", "Bluey", "PL" + "3" * 32, owner="Bluey")
        self.assertEqual(["Bluey", "Bluey (2)", "Bluey (3)"], self._titles())

    def test_distinct_titles_are_untouched_and_each_gets_its_own_slug(self):
        self._add("channel", "Kakė Makė")
        self._add("playlist", "Kids' songs", PL1)
        self.assertEqual(["Kakė Makė", "Kids' songs"], self._titles())
        self.assertEqual(2, len({c.slug for c in self.db.query(YouTubeChannel)}))


if __name__ == "__main__":
    unittest.main()
