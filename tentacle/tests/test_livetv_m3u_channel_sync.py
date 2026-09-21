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


class M3UUrlRotationTests(unittest.TestCase):
    """A channel whose URL changed is still the same channel.

    The stable id hashes name + URL, so a provider that rotates a token in its
    URLs (or moves host) turned every channel into "one removed, one new" on
    every sync: the user's enabled flags, channel numbers and sort order were
    thrown away each time, and Jellyfin saw a lineup of brand-new channel ids.
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
        for n, ch in enumerate(self.db.query(LiveChannel).order_by(LiveChannel.id).all()):
            ch.enabled = True
            ch.channel_number = 100 + n
        self.db.commit()
        self.before = {ch.name: (ch.id, ch.channel_number)
                       for ch in self.db.query(LiveChannel).all()}

    def tearDown(self):
        self.db.close()

    def _rotated(self, n=20, token="t2"):
        return [{"name": f"Channel {i}", "stream_url": f"http://p.example/{i}.ts?token={token}",
                 "group_title": "CA| SPORTS"} for i in range(n)]

    def _rows(self):
        return self.db.query(LiveChannel).filter(LiveChannel.provider_id == self.pid).all()

    def test_a_rotated_token_keeps_the_rows_and_their_settings(self):
        stats = livetv._upsert_channels_from_m3u(self.pid, self._rotated(), self.db)
        self.db.commit()
        rows = self._rows()
        self.assertEqual(20, len(rows))
        self.assertEqual(self.before, {r.name: (r.id, r.channel_number) for r in rows},
                         "rows were re-created: Jellyfin sees new channel ids and the numbers are gone")
        self.assertTrue(all(r.enabled for r in rows), "the user's enabled flags were thrown away")
        self.assertTrue(all("token=t2" in r.stream_url for r in rows), "the new URL was not taken")
        self.assertEqual(0, stats["new"])
        self.assertEqual(0, stats["removed"])

    def test_it_keeps_working_sync_after_sync(self):
        for token in ("t2", "t3", "t4"):
            livetv._upsert_channels_from_m3u(self.pid, self._rotated(token=token), self.db)
            self.db.commit()
        rows = self._rows()
        self.assertEqual(20, len(rows), "the lineup grew or shrank across rotations")
        self.assertTrue(all(r.enabled and "token=t4" in r.stream_url for r in rows))

    def test_a_rotation_that_is_also_truncated_is_still_refused(self):
        """Re-identifying moved channels must not open a way round the guard."""
        stats = livetv._upsert_channels_from_m3u(self.pid, self._rotated(n=2), self.db)
        self.db.commit()
        self.assertEqual(20, len(self._rows()))
        self.assertEqual(18, stats["removals_refused"])

    def test_two_channels_with_one_name_are_not_guessed_at(self):
        """Same name twice (an HD and an SD feed, say): which row moved where is
        not knowable, so they are left to the ordinary add/remove path."""
        self.db.query(LiveChannel).delete()
        self.db.commit()
        twins = [{"name": "News", "stream_url": f"http://p.example/news{i}.ts", "group_title": "G"} for i in (1, 2)]
        livetv._upsert_channels_from_m3u(self.pid, twins + _parsed(10), self.db)
        self.db.commit()
        moved = [{"name": "News", "stream_url": f"http://p.example/news{i}.ts?x=1", "group_title": "G"} for i in (1, 2)]
        livetv._upsert_channels_from_m3u(self.pid, moved + _parsed(10), self.db)
        self.db.commit()
        news = [r for r in self._rows() if r.name == "News"]
        self.assertEqual(2, len(news))
        self.assertTrue(all("x=1" in r.stream_url for r in news))


class RefusedRemovalIsSaid(unittest.TestCase):
    def test_a_sync_that_refused_removals_does_not_just_say_synced(self):
        import routers.livetv as livetv
        msg = livetv._sync_done_message({"message": "Groups synced", "removals_refused": 18})
        self.assertIn("18", msg)
        self.assertIn("REFUSED", msg)

    def test_an_ordinary_sync_reads_as_before(self):
        import routers.livetv as livetv
        self.assertEqual("Groups synced", livetv._sync_done_message({"removals_refused": 0}))
        self.assertEqual("Done", livetv._sync_done_message({"message": "Done"}))


if __name__ == "__main__":
    unittest.main()
