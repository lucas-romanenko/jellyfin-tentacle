"""Tests for M3U live-channel sync (routers.livetv._upsert_channels_from_m3u).

The M3U path unconditionally deletes every LiveChannel that is not in the
playlist it just parsed. A provider that answers HTTP 200 with a truncated
body, a maintenance page, or an empty playlist therefore wipes the whole
channel table for that provider — taking the user's enabled flags, channel
numbers and sort order with it, and emptying the HDHomeRun lineup Jellyfin
scanned. This is the same failure shape as the VOD category guard in
services.sync (EMPTY_CATEGORY_STRIKES) and the EPG guard in
routers.livetv._run_epg_sync_background, neither of which protects this path.

Requires: fastapi (routers.livetv imports it). Run from the tentacle/
directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import LiveChannel, Provider
import routers.livetv as livetv
from services.m3u_parser import parse_m3u


def _session():
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _parsed(n, start=0):
    return [{"name": f"Channel {i}", "stream_url": f"http://p.example/{i}.ts",
             "group_title": "CA| SPORTS"} for i in range(start, start + n)]


class M3UChannelSyncGuardTests(unittest.TestCase):
    def setUp(self):
        self.db = _session()
        self.provider = Provider(name="P", server_url="http://p.example",
                                 username="u", password="p",
                                 provider_type="m3u_url", live_tv_enabled=True)
        self.db.add(self.provider)
        self.db.commit()
        self.pid = self.provider.id
        # A synced, curated lineup: 500 channels, 200 of them enabled.
        livetv._upsert_channels_from_m3u(self.pid, _parsed(500), self.db)
        self.db.commit()
        for ch in self.db.query(LiveChannel).limit(200).all():
            ch.enabled = True
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _count(self):
        return self.db.query(LiveChannel).filter(
            LiveChannel.provider_id == self.pid).count()

    def test_empty_playlist_does_not_wipe_the_lineup(self):
        """A provider answering with nothing must not delete 500 channels."""
        livetv._upsert_channels_from_m3u(self.pid, [], self.db)
        self.db.commit()
        self.assertEqual(self._count(), 500,
                         "an empty M3U response deleted the whole lineup")

    def test_truncated_playlist_does_not_wipe_the_lineup(self):
        """A half-downloaded playlist must not delete the missing half."""
        livetv._upsert_channels_from_m3u(self.pid, _parsed(40), self.db)
        self.db.commit()
        self.assertEqual(self._count(), 500,
                         "a truncated M3U response deleted 460 channels")

    def test_enabled_flags_survive_a_bad_response(self):
        livetv._upsert_channels_from_m3u(self.pid, [], self.db)
        self.db.commit()
        self.assertEqual(
            self.db.query(LiveChannel).filter(
                LiveChannel.provider_id == self.pid,
                LiveChannel.enabled == True).count(),  # noqa: E712
            200, "the user's enabled channels were lost")

    def test_genuine_removals_are_still_applied(self):
        """The guard must not block a real, proportionate removal."""
        livetv._upsert_channels_from_m3u(self.pid, _parsed(480), self.db)
        self.db.commit()
        self.assertEqual(self._count(), 480)

    def test_html_error_page_parses_to_zero_channels(self):
        """Context: an HTTP 200 maintenance page is what reaches the upsert."""
        body = "<html><body><h1>503 Service Unavailable</h1></body></html>"
        self.assertEqual(parse_m3u(body), [])


class M3USmallLineupGuardTests(unittest.TestCase):
    """The 25-channel removal floor must not swallow a whole small lineup.

    Found live: a curated 20-channel playlist (what a tuliprox/Threadfin
    front-end typically serves) truncated to 2 entries deleted the other 18 —
    `max(25, 20%)` lets ANY lineup of up to 25 channels lose everything but
    one entry as long as the body parses to something.
    """

    def setUp(self):
        self.db = _session()
        self.provider = Provider(name="P", server_url="http://p.example",
                                 username="u", password="p",
                                 provider_type="m3u_url", live_tv_enabled=True)
        self.db.add(self.provider)
        self.db.commit()
        self.pid = self.provider.id
        livetv._upsert_channels_from_m3u(self.pid, _parsed(20), self.db)
        self.db.commit()
        for ch in self.db.query(LiveChannel).all():
            ch.enabled = True
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _count(self, **kw):
        q = self.db.query(LiveChannel).filter(LiveChannel.provider_id == self.pid)
        if kw.get("enabled"):
            q = q.filter(LiveChannel.enabled == True)  # noqa: E712
        return q.count()

    def test_truncated_small_playlist_does_not_wipe_the_lineup(self):
        stats = livetv._upsert_channels_from_m3u(self.pid, _parsed(2), self.db)
        self.db.commit()
        self.assertEqual(self._count(), 20, "a truncated playlist deleted 18 of 20 channels")
        self.assertEqual(self._count(enabled=True), 20)
        self.assertEqual(stats["removals_refused"], 18)

    def test_a_few_removals_from_a_small_lineup_still_apply(self):
        livetv._upsert_channels_from_m3u(self.pid, _parsed(17), self.db)
        self.db.commit()
        self.assertEqual(self._count(), 17)

    def test_a_same_size_replacement_is_not_a_failed_download(self):
        """Every URL changed (new host/token) but the playlist is as long as
        before: that cannot be a truncated body, so the old rows must go —
        refusing here would double the lineup on every sync, for ever."""
        moved = [{"name": f"Channel {i}", "stream_url": f"http://new.example/{i}.ts",
                  "group_title": "CA| SPORTS"} for i in range(20)]
        stats = livetv._upsert_channels_from_m3u(self.pid, moved, self.db)
        self.db.commit()
        self.assertEqual(self._count(), 20)
        self.assertEqual(stats["removals_refused"], 0)


class M3UParserPairingTests(unittest.TestCase):
    """An #EXTINF with no URL of its own must not consume the next entry's URL."""

    def test_extinf_without_a_url_does_not_steal_the_next_stream(self):
        body = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="a" group-title="G",Channel A\n'
            '#EXTINF:-1 tvg-id="b" group-title="G",Channel B\n'
            "http://p.example/b.ts\n"
        )
        channels = parse_m3u(body)
        by_name = {c["name"]: c["stream_url"] for c in channels}
        self.assertNotIn(
            "Channel A", by_name,
            "Channel A was given Channel B's stream URL")
        self.assertEqual(by_name.get("Channel B"), "http://p.example/b.ts",
                         "Channel B was dropped from the playlist")

    def test_vlc_option_lines_are_still_skipped(self):
        body = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="a",Channel A\n'
            "#EXTVLCOPT:http-user-agent=TiviMate\n"
            "http://p.example/a.ts\n"
        )
        channels = parse_m3u(body)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["stream_url"], "http://p.example/a.ts")


if __name__ == "__main__":
    unittest.main()
