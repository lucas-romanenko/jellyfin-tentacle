"""Channel syncs: one odd playlist line must not fail the sync, and new
channels follow their group.

Run from the tentacle/ directory:  python -m unittest discover -s tests

- #175: an M3U that lists a channel twice (same name + URL, typically under
  "Sports" and "Favourites") got both rows added under one stable id, and the
  flush failed on uq_live_channel_stream; a tvg-chno of "5.1" raised in int().
  Either way no channel of the playlist was saved.
- #158: every new channel was created disabled, and the group toggle only
  cascades to channels that exist, so the first sync after enabling groups
  reported "N channels (0 enabled)".
"""
import logging
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv
from models.database import LiveChannel, LiveChannelGroup
from services.m3u_parser import parse_m3u


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Client:
    def live_stream_url(self, sid, extension="m3u8"):
        return f"http://192.0.2.10/live/u/p/{sid}.{extension}"


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id

    def _group(self, name, enabled):
        self.db.add(LiveChannelGroup(provider_id=self.pid, name=name, enabled=enabled))
        self.db.commit()

    def _channels(self):
        self.db.expire_all()
        return {c.name: c for c in self.db.query(LiveChannel).filter_by(provider_id=self.pid)}


class M3uOddLines(_Base):
    def _sync(self, text):
        stats = livetv._upsert_channels_from_m3u(self.pid, parse_m3u(text), self.db)
        self.db.commit()
        return stats

    def test_a_channel_listed_twice_does_not_fail_the_sync(self):
        stats = self._sync(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="a.ca" group-title="Sports",TSN 1\nhttp://x/live/1.ts\n'
            '#EXTINF:-1 tvg-id="a.ca" group-title="Favourites",TSN 1\nhttp://x/live/1.ts\n'
            '#EXTINF:-1 tvg-id="b.ca" group-title="Sports",TSN 2\nhttp://x/live/2.ts\n')
        chans = self._channels()
        self.assertEqual({"TSN 1", "TSN 2"}, set(chans))
        self.assertEqual("Sports", chans["TSN 1"].group_title, "the first entry is the channel")
        self.assertEqual(2, stats["new"])

    def test_a_listed_twice_channel_still_follows_a_new_url(self):
        self._sync("#EXTM3U\n#EXTINF:-1,TSN 1\nhttp://old/1.ts\n")
        first = self._channels()["TSN 1"]
        first.enabled, first.channel_number = True, 42
        self.db.commit()
        self._sync("#EXTM3U\n#EXTINF:-1,TSN 1\nhttp://new/1.ts\n#EXTINF:-1,TSN 1\nhttp://new/1.ts\n")
        again = self._channels()["TSN 1"]
        self.assertEqual("http://new/1.ts", again.stream_url)
        self.assertTrue(again.enabled)
        self.assertEqual(42, again.channel_number)

    def test_a_non_integer_channel_number_is_left_empty(self):
        self._sync("#EXTM3U\n"
                   '#EXTINF:-1 tvg-chno="5.1",Five\nhttp://x/5.ts\n'
                   '#EXTINF:-1 tvg-chno="Sports",Six\nhttp://x/6.ts\n'
                   '#EXTINF:-1 tvg-chno="7",Seven\nhttp://x/7.ts\n'
                   '#EXTINF:-1 tvg-chno="²",Squared\nhttp://x/8.ts\n'      # "²".isdigit(), int() raises
                   '#EXTINF:-1 tvg-chno="５",Wide\nhttp://x/9.ts\n')
        chans = self._channels()
        self.assertIsNone(chans["Five"].channel_number)
        self.assertIsNone(chans["Six"].channel_number)
        self.assertEqual(7, chans["Seven"].channel_number)
        self.assertIsNone(chans["Squared"].channel_number)
        self.assertIsNone(chans["Wide"].channel_number)

    def test_an_existing_channel_is_not_renumbered_from_a_bad_value(self):
        self._sync('#EXTM3U\n#EXTINF:-1,Five\nhttp://x/5.ts\n')
        self._sync('#EXTM3U\n#EXTINF:-1 tvg-chno="5.1",Five\nhttp://x/5.ts\n')
        self.assertIsNone(self._channels()["Five"].channel_number)


class NewChannelsFollowTheirGroup(_Base):
    def _xtream(self, streams):
        livetv._upsert_channels(self.pid, streams, {"1": "Sports", "2": "News", "3": "Brand New"},
                                _Client(), self.db)
        self.db.commit()

    def test_xtream_new_channels_take_their_groups_state(self):
        self._group("Sports", True)
        self._group("News", False)
        self._xtream([{"stream_id": 1, "name": "TSN 1", "category_id": "1"},
                      {"stream_id": 2, "name": "CNN", "category_id": "2"},
                      {"stream_id": 3, "name": "New Thing", "category_id": "3"},
                      {"stream_id": 4, "name": "Loose", "category_id": ""}])
        chans = self._channels()
        self.assertTrue(chans["TSN 1"].enabled)
        self.assertFalse(chans["CNN"].enabled)
        self.assertFalse(chans["New Thing"].enabled, "a group the user never saw starts off")
        self.assertFalse(chans["Loose"].enabled)

    def test_xtream_a_channel_turned_off_stays_off(self):
        self._group("Sports", True)
        self._xtream([{"stream_id": 1, "name": "TSN 1", "category_id": "1"}])
        ch = self._channels()["TSN 1"]
        ch.enabled = False
        self.db.commit()
        self._xtream([{"stream_id": 1, "name": "TSN 1", "category_id": "1"},
                      {"stream_id": 5, "name": "TSN 5", "category_id": "1"}])
        chans = self._channels()
        self.assertFalse(chans["TSN 1"].enabled)
        self.assertTrue(chans["TSN 5"].enabled, "a channel the provider added later")

    def test_m3u_new_channels_take_their_groups_state(self):
        self._group("Sports", True)
        livetv._upsert_channels_from_m3u(self.pid, parse_m3u(
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="Sports",TSN 1\nhttp://x/1.ts\n'
            '#EXTINF:-1 group-title="Kids",Treehouse\nhttp://x/2.ts\n'), self.db)
        self.db.commit()
        chans = self._channels()
        self.assertTrue(chans["TSN 1"].enabled)
        self.assertFalse(chans["Treehouse"].enabled)

    def test_a_separator_row_in_an_enabled_group_starts_off(self):
        """A heading dressed as a channel plays nothing; Jellyfin would list it."""
        self._group("Sports", True)
        names = ["##### EVENTS #####", "=== SPORTS ===", "-----", "|||| PPV ||||",
                 "#1 Hits", "C-SPAN", "***Premium*** Movies", "TSN 1"]
        self._xtream([{"stream_id": i + 1, "name": n, "category_id": "1"} for i, n in enumerate(names)])
        chans = self._channels()
        for name in names[:4]:
            self.assertFalse(chans[name].enabled, name)
        for name in names[4:]:
            self.assertTrue(chans[name].enabled, name)


if __name__ == "__main__":
    unittest.main()
